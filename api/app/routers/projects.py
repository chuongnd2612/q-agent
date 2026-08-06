"""Projects router.

Endpoints to implement:
  GET  /projects                 -> list[ProjectOut]
  POST /projects/refresh          -> list[ProjectOut]   (pull projects from connected providers)
"""

from __future__ import annotations

import threading
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import crypto
from app.config import settings
from app.db import SessionLocal, get_db
from app.deps_auth import current_user
from app.deps_hub import hub_token as hub_token_header
from app.logging import logger
from app.models.knowledge import ProjectKnowledge, compose_key
from app.models.project import Project
from app.models.provider_connection import ProviderConnection
from app.models.user import User
from app.schemas import (
    AuthStateOut,
    AvailableReposOut,
    ExploreRequest,
    ExploreStartOut,
    ExploreStatusOut,
    KnowledgeBuildRequest,
    ProjectConfigOut,
    ProjectConfigUpdate,
    ProjectKnowledgeOut,
    ProjectOut,
    RepoKnowledgeOut,
)
from app.models.agent_device import AgentDevice
from app.services import (
    agent_capture_service,
    agent_explore_service,
    audit_service,
    connection_service,
    exploration_agent,
    hub_workspace,
    knowledge_service,
    playwright_runner,
    project_config_service,
    settings_store,
)
from app.services.adapters import get_adapter
from app.services.adapters.base import ProviderError
from app.services.ownership import check_owned_or_404, owned, stamp_owner
from app.ws import hub

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("", response_model=list[ProjectOut])
def list_projects(
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
    hub_token: str | None = Depends(hub_token_header),
) -> list[ProjectOut]:
    """Projects scoped to ``user`` (#93 — private per-user data).

    Mirrors the hub's projects first when the integration is on (#514): a user who
    signed in through EmeHub owns nothing locally on first visit, so this screen
    would otherwise read "0 connected providers" while the hub holds their work.
    Never raises — a hub outage leaves the local query to serve what exists.
    """
    hub_workspace.ensure_for_user(db, user, hub_token)
    return [ProjectOut.model_validate(p) for p in owned(db.query(Project), Project, user).all()]


@router.post("/refresh", response_model=list[ProjectOut])
def refresh_projects(
    db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> list[ProjectOut]:
    """Pull projects from every connected work-item connection and upsert Project rows.

    Each project is stamped with the connection that discovered it
    (``Project.connection_id``) — a convenience reference, not the credential router.
    Both the source connections and the upserted projects are scoped to ``user``
    (#93) — a user only ever refreshes from, and creates, their own data.
    """
    connections = owned(
        db.query(ProviderConnection).filter(ProviderConnection.connected.is_(True)),
        ProviderConnection,
        user,
    ).all()

    for connection in connections:
        if connection_service.WORK_ITEM not in connection_service.categories_for(connection.kind):
            continue
        # A mirrored hub connection holds no PAT and never will (#501/#514), so an
        # adapter call through it cannot work. Its projects arrive via the hub
        # mirror instead; skipping keeps Refresh from failing on them.
        if connection.hub_connection_id:
            continue
        decrypted_secrets = {
            key: crypto.decrypt(value) for key, value in (connection.secrets or {}).items()
        }
        try:
            adapter = get_adapter(connection.kind, connection.config or {}, decrypted_secrets)
            fetched = adapter.list_projects()
        except ProviderError:
            continue

        for item in fetched:
            external_id = str(item.get("external_id", ""))
            if not external_id:
                continue
            meta = {k: v for k, v in item.items() if k not in ("external_id", "name")}
            existing = owned(
                db.query(Project).filter(
                    Project.provider_kind == connection.kind,
                    Project.external_id == external_id,
                ),
                Project,
                user,
            ).first()
            if existing:
                existing.name = item.get("name", existing.name)
                existing.active = True
                existing.meta = meta
                existing.connection_id = connection.id
            else:
                db.add(
                    stamp_owner(
                        Project(
                            provider_kind=connection.kind,
                            external_id=external_id,
                            name=item.get("name", external_id),
                            active=True,
                            connection_id=connection.id,
                            meta=meta,
                        ),
                        user,
                    )
                )

    db.commit()
    return [ProjectOut.model_validate(p) for p in owned(db.query(Project), Project, user).all()]


# ------------------------------------------------------------- Project Knowledge
@router.get("/knowledge", response_model=list[ProjectKnowledgeOut])
def list_knowledge(
    db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> list[ProjectKnowledge]:
    """All Project Knowledge Bases (drives the Projects grid's status badges).

    Scoped to ``user`` (#93 — private per-user data).
    """
    return owned(db.query(ProjectKnowledge), ProjectKnowledge, user).all()


@router.get("/{key}/knowledge", response_model=ProjectKnowledgeOut)
def get_knowledge(
    key: str, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> ProjectKnowledge:
    row = db.query(ProjectKnowledge).filter(ProjectKnowledge.key == key).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"No knowledge base for project '{key}'")
    check_owned_or_404(row, user, not_found=f"No knowledge base for project '{key}'")
    return row


@router.post("/{key}/knowledge/build", response_model=ProjectKnowledgeOut)
def build_knowledge(
    key: str,
    body: KnowledgeBuildRequest,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> ProjectKnowledge:
    """Build (or rebuild) a project's knowledge base via Claude (project-bootstrap).

    Scoped to ``user`` (#93): rebuilding another user's existing knowledge base
    404s; a new row is stamped with the current user's ownership.
    """
    row = db.query(ProjectKnowledge).filter(ProjectKnowledge.key == key).first()
    check_owned_or_404(row, user, not_found=f"No knowledge base for project '{key}'")
    if not row:
        row = stamp_owner(ProjectKnowledge(key=key, project_key=key, name=body.name or key), user)
        db.add(row)
    row.project_key = row.project_key or key
    if body.name:
        row.name = body.name
    if body.provider is not None:
        row.provider = body.provider
    if body.repo is not None:
        row.repo = body.repo
    if body.framework:
        row.framework = body.framework

    # Build asynchronously — repo clone + Claude traversal can take minutes, which
    # would otherwise block the request past the CLI timeout. Mark 'indexing' so the
    # UI reflects progress on return and cannot re-trigger while one is running.
    if row.status != "indexing":
        row.status = "indexing"
        row.last_error = ""
        db.commit()
        db.refresh(row)
        knowledge_service.start_build(row.key)
    return row


# --------------------------------------------------------------- Project Config
@router.get("/{key}/config", response_model=ProjectConfigOut)
def get_project_config(
    key: str, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> dict:
    """Return a project's runtime config (test accounts masked).

    Scoped to ``user`` (#93 — private per-user data): another user's config 404s.
    """
    row = project_config_service.get_config(db, key)
    check_owned_or_404(row, user, not_found=f"Project config '{key}' not found")
    return project_config_service.public_config(row, key)


@router.put("/{key}/config", response_model=ProjectConfigOut)
def save_project_config(
    key: str,
    body: ProjectConfigUpdate,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> dict:
    """Create or update a project's runtime config from the Project Details page.

    Scoped to ``user`` (#93): updating another user's config 404s; a newly
    created config is stamped with the current user's ownership.
    """
    existing = project_config_service.get_config(db, key)
    check_owned_or_404(existing, user, not_found=f"Project config '{key}' not found")
    patch = body.model_dump(exclude_none=True)
    row = project_config_service.upsert_config(db, key, patch)
    if existing is None:
        stamp_owner(row, user)
    db.commit()
    db.refresh(row)
    return project_config_service.public_config(row, key)


# ---------------------------------------------------------- Manual-login session
@router.get("/{key}/auth", response_model=AuthStateOut)
def get_project_auth(
    key: str, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> dict:
    """Report whether a project has a saved manual-login session (+ capture time).

    Scoped to ``user`` (#93) via the underlying project config's ownership.
    """
    config = project_config_service.get_config(db, key)
    check_owned_or_404(config, user, not_found=f"Project config '{key}' not found")
    owner_id = config.owner_id if config else None
    return _auth_response(key, owner_id, config)


def _auth_response(key: str, owner_id: int | None, config) -> dict:  # noqa: ANN001
    """Merged manual-login state: the server-captured session file, else the
    Local-Agent "captured" marker, plus whether a capture is in flight on either
    the server or a paired agent."""
    state = project_config_service.auth_state(key, owner_id)
    if not state["exists"]:
        marker = project_config_service.agent_auth_state(config)
        if marker is not None:
            state = marker
    capturing = playwright_runner.is_capturing(key) or agent_capture_service.is_capturing(
        owner_id, key
    )
    return {**state, "capturing": capturing}


@router.post("/{key}/auth/capture", response_model=AuthStateOut)
def capture_project_auth(
    key: str, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> dict:
    """Open a headed browser so the operator can log in and save the session.

    Runs the capture in a background thread and returns immediately with
    ``capturing: true`` — the login is long/interactive, so the UI polls
    ``GET /{key}/auth`` for completion. If a capture is already in flight for
    this project, returns the current state without opening a second browser.
    Scoped to ``user`` (#93) via the underlying project config's ownership.
    """
    config = project_config_service.get_config(db, key)
    check_owned_or_404(config, user, not_found=f"Project config '{key}' not found")
    owner_id = config.owner_id if config else None
    base_url = (config.base_url if config else "") or ""
    if not base_url:
        raise HTTPException(status_code=400, detail="Set a base URL for the project first.")

    # In Local Agent mode the browser must open on the operator's OWN machine, so
    # queue the capture for the paired agent to run (it saves the session locally
    # and reports back). Otherwise fall back to the server-side headed capture.
    if settings_store.load_settings().get("executionTarget") == "local-agent":
        has_device = (
            db.query(AgentDevice)
            .filter(AgentDevice.owner_id == owner_id, AgentDevice.revoked_at.is_(None))
            .first()
            is not None
        )
        if not has_device:
            raise HTTPException(
                status_code=409,
                detail="No Local Agent paired — start your Local Agent, then Capture login.",
            )
        agent_capture_service.request_capture(owner_id, key, base_url)
    elif not playwright_runner.is_capturing(key):
        playwright_runner.start_capture(key, base_url, owner_id=owner_id)
    return _auth_response(key, owner_id, config)


@router.delete("/{key}/auth", response_model=AuthStateOut)
def clear_project_auth(
    key: str, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> dict:
    """Delete a project's saved session file, forcing re-capture on the next run.

    Scoped to ``user`` (#93) via the underlying project config's ownership.
    """
    config = project_config_service.get_config(db, key)
    check_owned_or_404(config, user, not_found=f"Project config '{key}' not found")
    owner_id = config.owner_id if config else None
    result = project_config_service.clear_auth(key, owner_id)
    # Also drop the Local-Agent "captured" marker so the UI reflects the clear.
    # (The session itself lives on the agent's machine and is overwritten on the
    # next capture; there's nothing server-side to delete there.)
    if config is not None and (config.extra or {}).get("agentAuthCapturedAt"):
        extra = dict(config.extra or {})
        extra.pop("agentAuthCapturedAt", None)
        extra.pop("agentAuthOrigin", None)
        config.extra = extra
        db.commit()
    return {**result, "capturing": agent_capture_service.is_capturing(owner_id, key)}


# --------------------------------------------------------------- Project repos
def _repository_connection_for_project(
    db: Session, key: str, owner_id: int | None = None
) -> ProviderConnection | None:
    """The project's bound repository connection (ADR 0006), or None.

    ``owner_id`` (#93 — private per-user data) restricts resolution to that
    user's own connections.
    """
    try:
        return connection_service.resolve_repository_for_project(db, key, owner_id=owner_id)
    except ProviderError:
        return None


@router.get("/{key}/repos/available", response_model=AvailableReposOut)
def list_available_repos(
    key: str, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> dict:
    """Discover the repositories the project's repository connection exposes.

    Scoped to ``user`` (#93): another user's project config 404s, and the
    repository connection is resolved from the current user's own connections.
    """
    check_owned_or_404(
        project_config_service.get_config(db, key), user, not_found=f"Project config '{key}' not found"
    )
    connection = _repository_connection_for_project(db, key, owner_id=user.id if user else None)
    if not connection:
        return {"provider": "", "repos": [], "error": "No repository connection is bound to this project"}
    secrets = {k: crypto.decrypt(v) for k, v in (connection.secrets or {}).items()}
    try:
        adapter = get_adapter(connection.kind, connection.config or {}, secrets)
        repos = adapter.list_repos()
    except ProviderError as exc:
        return {"provider": connection.kind, "repos": [], "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - never 500 the picker on an upstream hiccup
        return {"provider": connection.kind, "repos": [], "error": f"Could not list repos: {exc}"}
    return {"provider": connection.kind, "repos": repos, "error": ""}


@router.get("/{key}/repos", response_model=list[RepoKnowledgeOut])
def list_project_repos(
    key: str, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> list[dict]:
    """The project's configured repos, each annotated with its knowledge-base status.

    Scoped to ``user`` (#93): another user's project config 404s.
    """
    config = project_config_service.get_config(db, key)
    check_owned_or_404(config, user, not_found=f"Project config '{key}' not found")
    repos = project_config_service.get_repos(config)
    out: list[dict] = []
    for repo in repos:
        kn = (
            db.query(ProjectKnowledge)
            .filter(ProjectKnowledge.key == compose_key(key, repo["name"]))
            .first()
        )
        out.append(
            {
                "name": repo["name"],
                "repoUrl": repo.get("repo_url", ""),
                "defaultBranch": repo.get("default_branch", ""),
                "localRepoPath": repo.get("local_repo_path", ""),
                "default": repo.get("default", False),
                "status": kn.status if kn else "not_indexed",
                "confidence": kn.confidence if kn else 0,
                "version": kn.version if kn else "v1",
                "needsRefresh": kn.needs_refresh if kn else False,
                "lastIndexed": kn.last_indexed if kn else None,
                "docPath": kn.doc_path if kn else "",
                "lastError": kn.last_error if kn else "",
            }
        )
    return out


@router.get("/{key}/repos/{repo}/knowledge", response_model=ProjectKnowledgeOut)
def get_repo_knowledge(
    key: str, repo: str, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> ProjectKnowledge:
    """Scoped to ``user`` (#93): another user's per-repo knowledge base 404s."""
    row = (
        db.query(ProjectKnowledge)
        .filter(ProjectKnowledge.key == compose_key(key, repo))
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"No knowledge base for repo '{repo}'")
    check_owned_or_404(row, user, not_found=f"No knowledge base for repo '{repo}'")
    return row


@router.post("/{key}/repos/{repo}/knowledge/build", response_model=ProjectKnowledgeOut)
def build_repo_knowledge(
    key: str,
    repo: str,
    body: KnowledgeBuildRequest,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> ProjectKnowledge:
    """Build (or rebuild) the per-repo knowledge base for one of a project's repos.

    Scoped to ``user`` (#93): another user's project config or existing
    per-repo knowledge base 404s; a new row is stamped with the current user's
    ownership.
    """
    config = project_config_service.get_config(db, key)
    check_owned_or_404(config, user, not_found=f"Project config '{key}' not found")
    repos = project_config_service.get_repos(config)
    repo_entry = next((r for r in repos if r["name"] == repo), None)
    if repo_entry is None:
        raise HTTPException(
            status_code=404, detail=f"Repo '{repo}' is not configured for project '{key}'"
        )

    row_key = compose_key(key, repo)
    row = db.query(ProjectKnowledge).filter(ProjectKnowledge.key == row_key).first()
    check_owned_or_404(row, user, not_found=f"No knowledge base for repo '{repo}'")
    if row is None:
        row = stamp_owner(ProjectKnowledge(key=row_key, project_key=key, name=key, repo=repo), user)
        db.add(row)
    row.project_key = key
    row.repo = repo
    if body.provider is not None:
        row.provider = body.provider
    elif not row.provider:
        connection = _repository_connection_for_project(db, key, owner_id=user.id if user else None)
        row.provider = connection.kind if connection else ""
    if body.framework:
        row.framework = body.framework

    # Build asynchronously (repo clone + Claude traversal can take minutes). Mark
    # the row 'indexing' so the UI reflects progress and can't double-trigger.
    if row.status != "indexing":
        row.status = "indexing"
        row.last_error = ""
        db.commit()
        db.refresh(row)
        knowledge_service.start_build(row_key)
    return row


# --- DOM exploration agent (ADR 0010 §7) ----------------------------------
# Exploration drives a real browser + one Claude call per step, so a session runs
# for minutes — inline behind a fronting proxy it would trip the ~100s edge cap
# (Cloudflare 524). So the POST starts a background thread and returns a session
# id immediately; the caller polls ``.../explore/status`` (and, when a runId is
# given, watches ``explore.progress`` on the run WS). Mirrors the self-heal
# async-start pattern (``routers/agent.py::_heal_fix_jobs``).
_explore_lock = threading.Lock()
# (project_key, repo) -> session_id of the currently in-flight session (guard so
# status can report in-flight and the UI can't double-trigger).
_exploring: dict[tuple[str, str], str] = {}
# (project_key, repo) -> latest terminal outcome: {"sessionId", "status", "result"|"error"}.
_explore_results: dict[tuple[str, str], dict] = {}


def _resolve_repo_or_404(db: Session, key: str, repo: str, user: User | None) -> None:
    """Resolve + authorize a project config and assert ``repo`` is configured.

    Mirrors :func:`build_repo_knowledge`'s resolve pattern: another user's config
    404s (ownership), and an unconfigured repo name 404s. Raises ``HTTPException``;
    returns nothing on success.
    """
    config = project_config_service.get_config(db, key)
    check_owned_or_404(config, user, not_found=f"Project config '{key}' not found")
    repos = project_config_service.get_repos(config)
    if next((r for r in repos if r["name"] == repo), None) is None:
        raise HTTPException(
            status_code=404, detail=f"Repo '{repo}' is not configured for project '{key}'"
        )


def _run_code_for(db: Session, run_id: int | None) -> str | None:
    """Resolve a run's code (e.g. "RUN-202") from its id, or None. Best-effort."""
    if not run_id:
        return None
    from app.models.run import Run

    run = db.query(Run).filter(Run.id == run_id).first()
    return run.code if run is not None else None


def _run_exploration(
    session_id: str,
    key: str,
    repo: str,
    target: dict,
    run_id: int | None,
    case_id: int | None,
    owner_id: int | None,
    allow_state_changing: bool,
) -> None:
    """Background worker: run :func:`exploration_agent.explore` off the request thread.

    Opens its own DB session (the request's is closed once the POST returns),
    streams each step to the run WebSocket as ``explore.progress`` (only when a
    ``run_id`` is given), and records the terminal outcome for status polling.
    Never raises — a failure is stored as an ``error`` result.
    """
    db = SessionLocal()

    def on_step(step: dict) -> None:
        if run_id is not None:
            hub.publish(str(run_id), "explore.progress", {**step, "sessionId": session_id})

    try:
        result = exploration_agent.explore(
            db,
            project_key=key,
            repo=repo,
            target=target,
            run_id=run_id,
            case_id=case_id,
            owner_id=owner_id,
            on_step=on_step,
            allow_state_changing=allow_state_changing,
        )
        with _explore_lock:
            _explore_results[(key, repo)] = {
                "sessionId": session_id, "status": "done", "result": result
            }
        # Durable per-run record of the outcome for the run's activity timeline (#394).
        run_code = _run_code_for(db, run_id)
        exploration_agent.audit_exploration_result(
            target=target,
            stop_reason=result.stop_reason,
            steps_taken=result.steps_taken,
            discovered_routes=len(result.discovered.get("routes", [])),
            discovered_selectors=len(result.discovered.get("selectors", [])),
            wrote_kb=result.wrote_kb,
            run_code=run_code,
            log=result.log,
            routes=result.discovered.get("routes", []),
            selectors=result.discovered.get("selectors", []),
        )
    except Exception as exc:  # noqa: BLE001 - surface via status, never crash the thread
        logger.error("Exploration session {} failed: {}", session_id, exc)
        with _explore_lock:
            _explore_results[(key, repo)] = {
                "sessionId": session_id, "status": "error", "error": str(exc)
            }
        audit_service.record(
            category="automation", actor_type="ai", action="Explored to unblock",
            target=f"Explore {(target or {}).get('screen') or 'target screen'}",
            status="error", meta=f"session crashed: {exc}"[:400],
            run_code=_run_code_for(db, run_id),
        )
    finally:
        with _explore_lock:
            if _exploring.get((key, repo)) == session_id:
                _exploring.pop((key, repo), None)
        db.close()


@router.post("/{key}/repos/{repo}/explore", response_model=ExploreStartOut)
def start_exploration(
    key: str,
    repo: str,
    body: ExploreRequest,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> ExploreStartOut:
    """Start a DOM-exploration session for a repo's target screen (ADR 0010 §7).

    Resolves + authorizes the project config and repo (foreign config / unconfigured
    repo 404), then spawns a background thread running the observe→decide→act loop
    and returns ``{started, sessionId}`` immediately. Progress streams as
    ``explore.progress`` on the run WebSocket when ``runId`` is set; poll
    :func:`exploration_status` for navigation-survival status.
    """
    _resolve_repo_or_404(db, key, repo, user)
    session_id = uuid.uuid4().hex
    owner_id = user.id if user else None
    target = body.target.model_dump()

    # Where the loop runs. The server image ships no Playwright + can't reach the
    # app-under-test, so on a local-agent deployment exploration must run on the
    # paired device: enqueue a session the agent claims via /agent/explore/next
    # (mirrors the heal dispatch in automation.py). Server-target keeps the
    # in-process background-thread loop below.
    if settings_store.load_settings().get("executionTarget", "server") == "local-agent":
        has_device = (
            db.query(AgentDevice)
            .filter(AgentDevice.owner_id == owner_id, AgentDevice.revoked_at.is_(None))
            .first()
            is not None
        )
        if not has_device:
            raise HTTPException(
                status_code=409, detail="No local agent paired — start your local agent"
            )
        base_url = exploration_agent._resolve_base_url(db, key, repo, owner_id)
        max_steps = max(1, min(int(settings.explore_max_steps), 20))
        agent_explore_service.request_exploration(
            session_id,
            owner_id=owner_id,
            project_key=key,
            repo=repo,
            base_url=base_url,
            origin=agent_capture_service.origin_of(base_url),
            target=target,
            max_steps=max_steps,
            allow_state_changing=body.allow_state_changing,
            run_id=body.run_id,
            case_id=body.case_id,
        )
        return ExploreStartOut(started=True, session_id=session_id, mode="local-agent")

    with _explore_lock:
        _exploring[(key, repo)] = session_id
    threading.Thread(
        target=_run_exploration,
        args=(
            session_id,
            key,
            repo,
            target,
            body.run_id,
            body.case_id,
            owner_id,
            body.allow_state_changing,
        ),
        daemon=True,
    ).start()
    return ExploreStartOut(started=True, session_id=session_id)


@router.get("/{key}/repos/{repo}/explore/status", response_model=ExploreStatusOut)
def exploration_status(
    key: str,
    repo: str,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> ExploreStatusOut:
    """Report whether an exploration session is in-flight for this repo (ADR 0010 §7).

    Lets the UI restore the 'exploring' state after navigating away/back. When idle
    and a session has completed, returns a summary of the latest terminal result
    (stop reason, steps, whether the KB was written, discovered counts).
    """
    _resolve_repo_or_404(db, key, repo, user)
    with _explore_lock:
        in_flight = _exploring.get((key, repo))
        last = _explore_results.get((key, repo))

    if in_flight is not None:
        return ExploreStatusOut(exploring=True, session_id=in_flight)

    # Agent-path (local-agent dispatch): a session queued/running on the paired
    # device, then its finalize summary. Reported alongside the server path so the
    # UI's in-flight/terminal state is correct regardless of executionTarget.
    agent_in_flight = agent_explore_service.in_flight_session_id(key, repo)
    if agent_in_flight is not None:
        return ExploreStatusOut(exploring=True, session_id=agent_in_flight)

    if last and last["status"] == "done":
        result = last["result"]
        discovered = result.discovered or {}
        return ExploreStatusOut(
            exploring=False,
            session_id=last["sessionId"],
            stop_reason=result.stop_reason,
            steps_taken=result.steps_taken,
            wrote_kb=result.wrote_kb,
            discovered_routes=len(discovered.get("routes") or []),
            discovered_selectors=len(discovered.get("selectors") or []),
        )

    agent_last = agent_explore_service.get_result_for(key, repo)
    if agent_last and agent_last.get("status") == "done":
        return ExploreStatusOut(
            exploring=False,
            session_id=agent_last.get("sessionId"),
            stop_reason=agent_last.get("stopReason"),
            steps_taken=agent_last.get("stepsTaken"),
            wrote_kb=agent_last.get("wroteKb"),
            discovered_routes=agent_last.get("discoveredRoutes"),
            discovered_selectors=agent_last.get("discoveredSelectors"),
        )
    return ExploreStatusOut(exploring=False, session_id=None)
