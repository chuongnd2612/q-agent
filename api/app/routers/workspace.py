"""Shared-namespace catalog, clone, and admin shared-project management (#120).

ADR 0009 §2/§4: the admin-managed shared namespace (``owner_id IS NULL``) holds
ready-built projects members clone from instead of rebuilding. Endpoints:

  GET  /shared/projects                                     -> catalog (any authenticated user)
  POST /shared/projects/{key}/clone                          -> clone into the caller
  POST /shared/projects/{key}                                -> admin: create/update the shared shell + config
  POST /shared/projects/{key}/knowledge/build                -> admin: build/rebuild shared project-level knowledge
  POST /shared/projects/{key}/repos/{repo}/knowledge/build   -> admin: build/rebuild a shared per-repo knowledge base

The catalog and clone use ``current_user`` (best-effort — matches the bridge
behavior of the already-migrated ``/projects`` routes when auth is disabled).
Every write to the shared namespace is gated by ``require_admin`` — members
never write ``owner_id IS NULL`` rows, only read/clone them. This never
weakens the existing owner scoping on the normal ``/projects`` routes.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps_auth import current_user, require_admin
from app.models.knowledge import ProjectKnowledge, compose_key
from app.models.project import Project
from app.models.project_config import ProjectConfig
from app.models.user import User
from app.schemas import (
    AuthStateOut,
    CloneResultOut,
    KnowledgeBuildRequest,
    ProjectConfigOut,
    ProjectKnowledgeOut,
    SharedProjectCreate,
    SharedProjectOut,
)
from app.services import clone_service, knowledge_service, playwright_runner, project_config_service, settings_store

router = APIRouter(prefix="/shared/projects", tags=["shared-projects"])


# --------------------------------------------------------------- catalog + clone
@router.get("", response_model=list[SharedProjectOut])
def list_shared_projects(
    db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> list[dict]:
    """Catalog of shared-namespace projects, so members can browse before cloning.

    One entry per distinct project key found among the shared (``owner_id IS
    NULL``) ``Project``/``ProjectConfig``/``ProjectKnowledge`` rows, annotated
    with each repo's knowledge status/confidence and whether the caller
    already owns a clone of it.
    """
    projects = db.query(Project).filter(Project.owner_id.is_(None)).all()
    configs = {c.key: c for c in db.query(ProjectConfig).filter(ProjectConfig.owner_id.is_(None)).all()}
    knowledge_rows = db.query(ProjectKnowledge).filter(ProjectKnowledge.owner_id.is_(None)).all()

    names: dict[str, str] = {}
    for p in projects:
        names.setdefault(p.name, p.name)
    for c in configs.values():
        names.setdefault(c.key, c.name or c.key)
    for k in knowledge_rows:
        project_key = k.project_key or k.key.split("::", 1)[0]
        names.setdefault(project_key, k.name or project_key)

    dest_owner_id = user.id if user else None
    catalog: list[dict] = []
    for key, name in sorted(names.items()):
        provider_kind = next((p.provider_kind for p in projects if p.name == key), "")
        cfg = configs.get(key)
        knowledge_out = [
            {
                "repo": k.repo,
                "status": k.status,
                "confidence": k.confidence,
                "version": k.version,
                "lastIndexed": k.last_indexed,
            }
            for k in knowledge_rows
            if (k.project_key or k.key.split("::", 1)[0]) == key
        ]
        catalog.append(
            {
                "key": key,
                "name": name,
                "providerKind": provider_kind,
                "hasConfig": cfg is not None,
                "baseUrl": cfg.base_url if cfg else "",
                "repos": project_config_service.get_repos(cfg),
                "workItemConnectionId": cfg.work_item_connection_id if cfg else None,
                "repositoryConnectionId": cfg.repository_connection_id if cfg else None,
                "testCaseConnectionId": cfg.test_case_connection_id if cfg else None,
                "knowledge": knowledge_out,
                "alreadyCloned": clone_service.dest_already_has_project(db, key, dest_owner_id),
            }
        )
    return catalog


@router.post("/{key}/clone", response_model=CloneResultOut)
def clone_project(
    key: str, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> clone_service.CloneResult:
    """Clone a shared project into the caller's own scope.

    404 if no shared project exists by ``key``; 409 if the caller already
    owns a project by that key (rows + files are copied — see
    ``clone_service.clone_shared_project``).
    """
    return clone_service.clone_shared_project(db, key, user)


# --------------------------------------------------------------- admin management
@router.get("/{key}/config", response_model=ProjectConfigOut)
def get_shared_project_config(
    key: str, db: Session = Depends(get_db), _admin: User = Depends(require_admin)
) -> dict:
    """Admin: the shared project's full runtime config (test-account passwords masked).

    Mirrors ``GET /projects/{key}/config`` but always reads the shared row
    (``owner_id IS NULL``) so the admin settings page shows the shared project's
    own settings, never a member's clone. Returns empty defaults if not created yet.
    """
    row = project_config_service.get_config_for_owner(db, key, None)
    return project_config_service.public_config(row, key)


@router.get("/{key}/auth", response_model=AuthStateOut)
def get_shared_project_auth(
    key: str, _admin: User = Depends(require_admin)
) -> dict:
    """Admin: whether the shared project has a saved manual-login session."""
    return {
        **project_config_service.auth_state(key, None),
        "capturing": playwright_runner.is_capturing(key),
    }


@router.post("/{key}/auth/capture", response_model=AuthStateOut)
def capture_shared_project_auth(
    key: str, db: Session = Depends(get_db), _admin: User = Depends(require_admin)
) -> dict:
    """Admin: open a headed browser to capture the shared project's login session.

    Saved under the shared scope so a member inherits it on clone (ADR 0009 §4).
    """
    # See capture_project_auth: server-side capture is unavailable / wrong in
    # Local Agent mode (the agent captures on the operator's machine).
    if settings_store.load_settings().get("executionTarget") == "local-agent":
        raise HTTPException(
            status_code=409,
            detail=(
                "Manual login runs on your Local Agent — a browser opens on your machine "
                "the first time a run needs it. There's no server-side capture in Local Agent mode."
            ),
        )
    if not playwright_runner.is_capturing(key):
        config = project_config_service.get_config_for_owner(db, key, None)
        base_url = (config.base_url if config else "") or ""
        if not base_url:
            raise HTTPException(status_code=400, detail="Set a base URL for the project first.")
        playwright_runner.start_capture(key, base_url, owner_id=None)
    return {
        **project_config_service.auth_state(key, None),
        "capturing": playwright_runner.is_capturing(key),
    }


@router.delete("/{key}/auth", response_model=AuthStateOut)
def clear_shared_project_auth(
    key: str, _admin: User = Depends(require_admin)
) -> dict:
    """Admin: delete the shared project's saved manual-login session."""
    return project_config_service.clear_auth(key, None)


@router.post("/{key}", response_model=ProjectConfigOut, status_code=201)
def create_shared_project(
    key: str,
    body: SharedProjectCreate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> dict:
    """Admin: create/update the shared project shell (``owner_id=None``).

    Upserts a ``Project`` row when ``providerKind``/``externalId`` are given
    (the discovery record), and always upserts the ``ProjectConfig`` (base
    URL, repos, environments, test accounts, extra, manual auth) — the config
    a knowledge build reads for grounding.
    """
    if body.provider_kind and body.external_id:
        proj = (
            db.query(Project)
            .filter(
                Project.provider_kind == body.provider_kind,
                Project.external_id == body.external_id,
                Project.owner_id.is_(None),
            )
            .first()
        )
        if proj is None:
            db.add(
                Project(
                    provider_kind=body.provider_kind,
                    external_id=body.external_id,
                    name=body.name or key,
                    active=True,
                    owner_id=None,
                )
            )
        else:
            proj.name = body.name or proj.name
            proj.active = True

    patch = body.model_dump(exclude={"provider_kind", "external_id"}, exclude_none=True)
    row = project_config_service.upsert_config_for_owner(db, key, patch, owner_id=None)
    db.commit()
    db.refresh(row)
    return project_config_service.public_config(row, key)


@router.post("/{key}/knowledge/build", response_model=ProjectKnowledgeOut)
def build_shared_knowledge(
    key: str,
    body: KnowledgeBuildRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> ProjectKnowledge:
    """Admin: build/rebuild the shared project's bare-key knowledge base.

    Mirrors ``POST /projects/{key}/knowledge/build`` (async build, ``project-
    bootstrap`` via Claude), but the row is stamped ``owner_id=None`` so it
    lands in the shared namespace instead of the admin's own scope.
    """
    row = (
        db.query(ProjectKnowledge)
        .filter(ProjectKnowledge.key == key, ProjectKnowledge.owner_id.is_(None))
        .first()
    )
    if row is None:
        row = ProjectKnowledge(key=key, project_key=key, name=body.name or key, owner_id=None)
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

    if row.status != "indexing":
        row.status = "indexing"
        row.last_error = ""
        db.commit()
        db.refresh(row)
        knowledge_service.start_build(row.key)
    return row


@router.post("/{key}/repos/{repo}/knowledge/build", response_model=ProjectKnowledgeOut)
def build_shared_repo_knowledge(
    key: str,
    repo: str,
    body: KnowledgeBuildRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> ProjectKnowledge:
    """Admin: build/rebuild a shared project's per-repo knowledge base.

    Mirrors ``POST /projects/{key}/repos/{repo}/knowledge/build``, stamped
    ``owner_id=None``.
    """
    row_key = compose_key(key, repo)
    row = (
        db.query(ProjectKnowledge)
        .filter(ProjectKnowledge.key == row_key, ProjectKnowledge.owner_id.is_(None))
        .first()
    )
    if row is None:
        row = ProjectKnowledge(key=row_key, project_key=key, name=key, repo=repo, owner_id=None)
        db.add(row)
    row.project_key = key
    row.repo = repo
    if body.provider is not None:
        row.provider = body.provider
    if body.framework:
        row.framework = body.framework

    if row.status != "indexing":
        row.status = "indexing"
        row.last_error = ""
        db.commit()
        db.refresh(row)
        knowledge_service.start_build(row_key)
    return row
