"""Local Agent router — device pairing + the job-claim/push protocol.

Two endpoint groups (Local Agent feature — see the implementation plan):

**Device management** (``require_user`` — an already-authenticated SPA user):
  POST   /agent/devices/pair-code   -> {code, expiresIn}
  GET    /agent/devices             -> [{id, name, lastSeenAt, createdAt}]
  DELETE /agent/devices/{id}        -> {ok}
  POST   /agent/devices/redeem      -> {deviceToken, deviceId}  (auth is the
                                        pairing code itself, not a user token —
                                        this is what the Node CLI calls)

**Job protocol** (``require_agent`` — a paired device's bearer token; every
handler scopes to the device's owning user via ``get_owned_or_404``):
  POST /agent/jobs/next               -> job payload, or 204 if nothing queued
  POST /agent/jobs/{id}/events        -> re-emit {event, payload} onto the run's WS
  POST /agent/jobs/{id}/results       -> upsert one parsed ExecutionResult
  POST /agent/jobs/{id}/evidence      -> multipart artifact upload
  POST /agent/jobs/{id}/complete      -> finalize {passed, failed, log}

The ``/agent/jobs/next`` payload NEVER includes storageState/sessionStorage or
any other captured session — auth is handled locally by the agent (manual
login happens on the user's own machine); the server only tells it whether
manual auth is required and which origin(s) to expect it for.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Response, UploadFile
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal, get_db
from app.logging import logger
from app.deps_auth import require_agent, require_user
from app.models.automation_project import AutomationProject
from app.models.execution import Execution, ExecutionResult
from app.models.run import Run
from app.models.testcase import AutomationSpec, TestCase
from app.models.user import User
from app.schemas import (
    AuthoringClaimOut,
    AuthoringEventRequest,
    AuthoringFinalizeOut,
    AuthoringFinalizeRequest,
    ExploreClaimOut,
    ExploreDecideRequest,
    ExploreDecideStartOut,
    ExploreDecideStatusOut,
    ExploreEventRequest,
    ExploreFinalizeOut,
    ExploreFinalizeRequest,
    ExploreTarget,
)
from app.services import agent_authoring_service, agent_capture_service, agent_device_service, agent_explore_service, agent_project_bundle, automation_project_service, evidence_service, execution_service, exploration_agent, heal_service, knowledge_service, project_config_service, run_context, settings_store, spec_service
from app.services.auth_service import AuthError
from app.services.ownership import get_owned_or_404
from app.services.playwright_runner import _resolve_project_for_run
from app.ws import hub

router = APIRouter(prefix="/agent", tags=["agent"])


# --------------------------------------------------------------- device management
@router.post("/devices/pair-code")
def create_pair_code(user: User = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    """Issue a short-lived pairing code for the current user to give to their agent."""
    code = agent_device_service.create_pairing_code(db, user)
    return {"code": code, "expiresIn": int(agent_device_service.PAIR_TTL.total_seconds())}


@router.get("/devices")
def list_devices(user: User = Depends(require_user), db: Session = Depends(get_db)) -> list[dict]:
    """List the current user's paired (non-revoked) Local Agent devices."""
    return [
        {
            "id": d.id,
            "name": d.name,
            # Self-reported on the last job claim (#541); "" until the device runs
            # an agent new enough to report it.
            "agentVersion": d.agent_version or "",
            "lastSeenAt": d.last_seen_at,
            "createdAt": d.created_at,
        }
        for d in agent_device_service.list_devices(db, user)
    ]


@router.delete("/devices/{device_id}")
def revoke_device(
    device_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
) -> dict:
    """Revoke a paired device."""
    device = agent_device_service.revoke(db, user, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    return {"ok": True}


@router.post("/disconnect")
def disconnect_device(user: User = Depends(require_agent), db: Session = Depends(get_db)) -> dict:
    """Self-revoke the calling device (the Local Agent "Disconnect" button).

    Authenticated by the device's own bearer token (``require_agent``). The agent
    clears its local token on disconnect, so revoking here keeps the two in sync
    and removes the device from the owner's list on the SPA's next poll.
    """
    device = getattr(user, "_device", None)
    if device is not None:
        agent_device_service.revoke_self(db, device)
    return {"ok": True}


@router.post("/devices/redeem")
def redeem_device(body: dict, db: Session = Depends(get_db)) -> dict:
    """Redeem a pairing code (called by the Local Agent CLI, not the SPA).

    Body: ``{"code": str, "name": str}``. Auth is the pairing code itself — no
    user bearer token is involved here.
    """
    code = (body.get("code") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="code is required")
    try:
        device, token = agent_device_service.redeem_pairing_code(db, code, body.get("name", ""))
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return {"deviceToken": token, "deviceId": device.id}


# --------------------------------------------------------------------- job protocol
def _owned_execution(db: Session, execution_id: int, user: User) -> tuple[Execution, Run]:
    """Fetch an Execution + its Run, 404ing unless the run belongs to ``user``."""
    execution = db.get(Execution, execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Execution not found")
    run = get_owned_or_404(db, Run, execution.run_id, user)
    return execution, run


def _fail_claimed_execution(
    db: Session, execution: Execution, run: Run, results: list[ExecutionResult], message: str
) -> None:
    """Fail an already-claimed execution cleanly, with ``message`` on every case.

    The claim flips the row to ``running`` before we know whether the device can
    actually run it (version skew, an over-cap bundle). When it can't, the run
    must end with one legible reason rather than being left ``running`` forever
    or handed to an agent that will emit a wall of import errors.
    """
    for result in results:
        result.status = "fail"
        result.duration_ms = 0
        result.error_message = message
    execution.passed = 0
    execution.failed = len(results)
    db.commit()
    for result in results:
        hub.publish(
            str(run.id),
            "exec.case.result",
            {
                "ticket": result.ticket_external_id,
                "caseCode": result.case_code,
                "status": "fail",
                "durationMs": 0,
            },
        )
    execution_service.finalize(
        db, execution, run, message, advance_run=execution.heal_case_id is None
    )


def _spec_relpath(
    project: AutomationProject, spec: AutomationSpec, ticket_external_id: str, filename: str
) -> str:
    """The spec's path **relative to the project root**, for a nested bundle.

    Prefers the path ``automation_project_service.write_spec`` persisted on the
    row (``tests/<TICKET>/<file>.spec.ts``, set by #540) so server and device
    agree by construction; falls back to deriving it from
    :func:`automation_project_service.spec_dir` when the row still holds a bare
    filename (a spec generated before the project layout landed).
    """
    stored = (spec.filename or "").replace("\\", "/").strip("/")
    if stored.startswith("tests/"):
        return stored
    directory = automation_project_service.spec_dir(project, ticket_external_id)
    relative = directory.relative_to(automation_project_service.project_dir(project))
    return f"{relative.as_posix()}/{filename}"


@router.post("/jobs/next")
def claim_next_job(
    response: Response,
    body: dict | None = Body(default=None),
    user: User = Depends(require_agent),
    db: Session = Depends(get_db),
) -> dict | None:
    """Atomically claim the oldest queued local-agent Execution owned by this user.

    Uses a conditional UPDATE (``WHERE status='queued'``) so a concurrent claim
    from another poll can never double-claim the same row: only one caller's
    UPDATE affects a row. Returns 204 (empty) when nothing is queued.

    Body (optional): ``{"agentVersion": "0.2.1"}``. The version is recorded on
    the claiming device and gates the layered-project payload (#541) — see
    :mod:`app.services.agent_project_bundle`. A body-less claim from a pre-#541
    agent still works for legacy (``project_id IS NULL``) specs.
    """
    candidate = (
        db.query(Execution.id)
        .join(Run, Execution.run_id == Run.id)
        .filter(
            Execution.status == "queued",
            Execution.target == "local-agent",
            Run.owner_id == user.id,
        )
        .order_by(Execution.id)
        .first()
    )
    if candidate is None:
        response.status_code = 204
        return None
    execution_id = candidate[0]
    device = getattr(user, "_device", None)
    # Record the claiming build BEFORE anything else, so the paired-devices list
    # shows a stale agent even when the claim is then refused for version skew.
    reported_version = str((body or {}).get("agentVersion") or "").strip()
    if device is not None and reported_version and device.agent_version != reported_version:
        device.agent_version = reported_version[:32]
        db.commit()
    result = db.execute(
        update(Execution)
        .where(Execution.id == execution_id, Execution.status == "queued")
        .values(
            status="running",
            claimed_by_device_id=device.id if device else None,
            started_at=datetime.now(timezone.utc),
        )
    )
    db.commit()
    if result.rowcount == 0:
        # Lost the race to another poll between the select and the update.
        response.status_code = 204
        return None

    execution = db.get(Execution, execution_id)
    run = db.get(Run, execution.run_id)
    results = (
        db.query(ExecutionResult)
        .filter(ExecutionResult.execution_id == execution.id)
        .order_by(ExecutionResult.id)
        .all()
    )
    _project_key, base_url, manual_auth, _provider = _resolve_project_for_run(db, run, execution.env)
    auth_origins: list[str] = []
    if base_url:
        parts = urlsplit(base_url)
        if parts.scheme and parts.netloc:
            auth_origins = [f"{parts.scheme}://{parts.netloc}"]

    # Layered (#537/#541) vs legacy specs. A project-backed spec imports from
    # `../pages/…` and `@q-agent/playwright-base`, so it only runs on a device
    # that materializes the nested tree — hence the version guard below. Legacy
    # `project_id IS NULL` specs keep the exact pre-#541 payload and keep working
    # on every agent build, forever.
    specs: list[dict] = []
    # EVERY distinct project the execution's specs belong to, not just the last one
    # seen: a run's tickets can each carry their own repo, and projects are keyed
    # per repo, so this legitimately holds more than one — which the single-bundle
    # payload cannot serve and must refuse (#556, guarded right below the loop).
    claim_projects: dict[int, AutomationProject] = {}
    for r in results:
        spec = (
            db.query(AutomationSpec)
            .filter(AutomationSpec.test_case_id == r.test_case_id)
            .first()
        )
        if spec is None:
            continue
        filename = spec_service.spec_filename(r.ticket_external_id, r.case_code)
        if spec.project_id is not None:
            candidate_project = db.get(AutomationProject, spec.project_id)
            if candidate_project is not None:
                claim_projects[candidate_project.id] = candidate_project
                filename = _spec_relpath(candidate_project, spec, r.ticket_external_id, filename)
        specs.append(
            {
                "filename": filename,
                "code": spec.code,
                # Explicit identity so the agent can match /jobs/{id}/evidence by exact
                # ticket_external_id (the filename convention drops the provider prefix,
                # e.g. "SUR-1428" -> "1428", which would 404 the evidence upload).
                "ticketExternalId": r.ticket_external_id,
                "caseCode": r.case_code,
            }
        )

    # A claim ships exactly ONE project bundle, so an execution spanning two
    # projects would hand the device one library and leave the other project's
    # specs with unresolvable `../pages/…` imports — the same silent mass-failure
    # the version guard exists to prevent, arriving by another route. Refuse it,
    # loudly, before the device ever sees the job. A mixed project-backed/legacy
    # execution is deliberately NOT refused (see `multi_project_reason`).
    multi_project = agent_project_bundle.multi_project_reason(claim_projects.values())
    if multi_project is not None:
        logger.warning(
            "refusing multi-project claim for execution {}: projects {}",
            execution.id,
            sorted(claim_projects),
        )
        _fail_claimed_execution(db, execution, run, results, multi_project)
        raise HTTPException(status_code=409, detail=multi_project)

    project: AutomationProject | None = next(iter(claim_projects.values()), None)
    project_bundle: dict | None = None
    if project is not None:
        # Version skew is a SILENT mass-failure mode: an old agent flattens the
        # tree and every import fails collection. Refuse the claim and fail the
        # execution with one legible reason instead.
        if not agent_project_bundle.version_ok(reported_version):
            logger.warning(
                "refusing layered claim for execution {}: device {} reports version {!r} "
                "(minimum {})",
                execution.id,
                device.id if device else None,
                reported_version or None,
                agent_project_bundle.MIN_AGENT_VERSION,
            )
            _fail_claimed_execution(
                db, execution, run, results, agent_project_bundle.UPDATE_MESSAGE
            )
            raise HTTPException(status_code=409, detail=agent_project_bundle.UPDATE_MESSAGE)
        project_bundle, total_bytes = agent_project_bundle.bundle_payload(project)
        if total_bytes > agent_project_bundle.BUNDLE_MAX_BYTES:
            _fail_claimed_execution(
                db, execution, run, results, agent_project_bundle.OVERSIZE_MESSAGE
            )
            raise HTTPException(status_code=413, detail=agent_project_bundle.OVERSIZE_MESSAGE)

    payload = {
        "executionId": execution.id,
        "runCode": run.code,
        "env": execution.env,
        "browser": execution.browser,
        "workers": execution.workers,
        "headless": bool(settings_store.load_settings().get("headless", True)),
        # Honor the "Capture video" setting on the agent too (#456): ON records
        # video for every case (pass or fail); OFF records none.
        "captureVideo": bool(settings_store.load_settings().get("video", False)),
        "baseUrl": base_url,
        "manualAuth": manual_auth,
        "authOrigins": auth_origins,
        "specs": specs,
    }
    # The persistent automation project, shipped wholesale (#541). Present only
    # for layered runs, so a legacy claim is byte-identical to before.
    if project_bundle is not None:
        payload["project"] = project_bundle
    # An agent-executed self-heal (issue #260): the agent runs the heal LOOP for
    # this one case (run → on fail POST /agent/heal/{caseId}/fix → re-run → …),
    # then POSTs /agent/heal/{caseId}/finalize. Flagged so the agent branches into
    # heal mode instead of a one-shot run.
    if execution.heal_case_id is not None:
        payload["heal"] = {
            "caseId": execution.heal_case_id,
            "maxAttempts": settings.heal_max_attempts,
            # Shorter heal re-run timeouts (#398) — server-authoritative so the
            # agent fails fast on a broken locator instead of stalling 30s/attempt.
            "testTimeoutMs": settings.heal_test_timeout_ms,
            "actionTimeoutMs": settings.heal_action_timeout_ms,
        }
    return payload


@router.post("/jobs/{execution_id}/events")
def push_job_event(
    execution_id: int, body: dict, user: User = Depends(require_agent), db: Session = Depends(get_db)
) -> dict:
    """Re-emit an agent-reported progress event onto the run's WS channel.

    Body: ``{"event": str, "payload": dict}``. This is how the SPA's existing
    ``/ws/runs/{id}`` subscribers (unchanged) see ``exec.case.running`` /
    ``exec.case.result`` / ``exec.progress`` / ``exec.auth.waiting`` etc. from a
    Local Agent run.
    """
    execution, _run = _owned_execution(db, execution_id, user)
    event = body.get("event")
    if not event:
        raise HTTPException(status_code=400, detail="event is required")
    hub.publish(str(execution.run_id), event, body.get("payload") or {})
    return {"ok": True}


@router.post("/jobs/{execution_id}/results")
def push_job_result(
    execution_id: int, body: dict, user: User = Depends(require_agent), db: Session = Depends(get_db)
) -> dict:
    """Upsert one parsed result, shaped exactly like ``parse_playwright_report``'s
    output (``file``/``filename``, ``status``, ``duration_ms``, ``error_message``).
    Matched to its ExecutionResult by the spec filename convention (shared with
    the server runner via ``execution_service.match_result``).
    """
    execution, _run = _owned_execution(db, execution_id, user)
    results = (
        db.query(ExecutionResult)
        .filter(ExecutionResult.execution_id == execution.id)
        .order_by(ExecutionResult.id)
        .all()
    )
    result = execution_service.apply_result(db, results, body)
    if result is None:
        raise HTTPException(status_code=400, detail="No matching result for this file")
    return {"ok": True, "resultId": result.id}


@router.post("/jobs/{execution_id}/evidence")
async def push_job_evidence(
    execution_id: int,
    ticket_external_id: str = Form(...),
    case_code: str = Form(...),
    kind: str = Form(...),
    file: UploadFile = File(...),
    user: User = Depends(require_agent),
    db: Session = Depends(get_db),
) -> dict:
    """Upload one evidence artifact (multipart) for a case's result."""
    execution, run = _owned_execution(db, execution_id, user)
    result = (
        db.query(ExecutionResult)
        .filter(
            ExecutionResult.execution_id == execution.id,
            ExecutionResult.ticket_external_id == ticket_external_id,
            ExecutionResult.case_code == case_code,
        )
        .first()
    )
    if result is None:
        raise HTTPException(status_code=404, detail="No matching result for this ticket/case")
    content = await file.read()
    # Console/network (#456) are JSON data, not media — decode into the result's
    # console_logs/network_logs columns instead of storing a file.
    if evidence_service.is_log_capture(kind):
        evidence_service.apply_log_capture(result, kind, content)
        db.commit()
        return {"ok": True, "kind": kind}
    evidence = evidence_service.store_uploaded_evidence(
        db, run, result, kind, content, file.filename or "evidence"
    )
    if evidence is None:
        raise HTTPException(status_code=400, detail="Failed to store evidence")
    db.commit()
    return {"id": evidence.id, "kind": evidence.kind, "filename": evidence.filename}


@router.post("/auth/next")
def claim_next_auth_capture(
    response: Response, user: User = Depends(require_agent), db: Session = Depends(get_db)
) -> dict | None:
    """Claim the oldest queued manual-login capture for this device's owner.

    The agent opens a headed browser at ``baseUrl`` on the operator's machine,
    lets them log in, saves the session locally (keyed by ``origin``, never
    uploaded), then reports back via ``/agent/auth/{id}/complete``. 204 when
    nothing is queued.
    """
    capture = agent_capture_service.claim_next(user.id)
    if capture is None:
        response.status_code = 204
        return None
    return {
        "captureId": capture["id"],
        "projectKey": capture["project_key"],
        "baseUrl": capture["base_url"],
        "origin": capture["origin"],
    }


@router.post("/auth/{capture_id}/complete")
def complete_auth_capture(
    capture_id: int, body: dict, user: User = Depends(require_agent), db: Session = Depends(get_db)
) -> dict:
    """Finalize a capture. Body: ``{"ok": bool, "error"?: str}``.

    On success, stamp a persistent marker on the project config's ``extra`` so
    the SPA shows "captured on your Local Agent" across restarts (the session
    itself stays on the agent's machine).
    """
    capture = agent_capture_service.finish(capture_id, user.id)
    if capture is None:
        raise HTTPException(status_code=404, detail="Capture not found")
    if body.get("ok"):
        row = project_config_service.get_config_for_owner(db, capture["project_key"], user.id)
        if row is not None:
            extra = dict(row.extra or {})
            extra["agentAuthCapturedAt"] = datetime.now(timezone.utc).isoformat()
            extra["agentAuthOrigin"] = capture["origin"]
            row.extra = extra
            db.commit()
    return {"ok": True}


@router.post("/jobs/{execution_id}/complete")
def complete_job(
    execution_id: int, body: dict, user: User = Depends(require_agent), db: Session = Depends(get_db)
) -> dict:
    """Finalize an agent-run Execution. Body: ``{"passed": int, "failed": int, "log": str}``."""
    execution, run = _owned_execution(db, execution_id, user)
    execution.passed = int(body.get("passed") or 0)
    execution.failed = int(body.get("failed") or 0)
    # An agent heal execution (heal_case_id set) re-runs a single case and must not
    # advance the run's lifecycle (matches the server heal loop).
    execution_service.finalize(
        db, execution, run, body.get("log", ""), advance_run=execution.heal_case_id is None
    )
    return {"ok": True}


# --------------------------------------------------------------- agent self-heal (#260)
# The heal LOOP runs on the agent (Playwright + captured DOM). These two endpoints
# do the parts that need the server: Claude (fix generation) + the DB/KB. See
# app.services.heal_service.


def _owned_case_and_run(db: Session, case_id: int, user: User) -> tuple[TestCase, Run]:
    """Fetch a TestCase + its Run, 404ing unless the run belongs to ``user``."""
    case = db.get(TestCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Test case not found")
    run = get_owned_or_404(db, Run, case.run_id, user)
    return case, run


# In-flight agent heal-fix jobs (#313): job_id -> {"status": running|done|error,
# "result"|"error"}. The fix generation is a ~3-min Claude call; running it inline
# behind a proxy trips the ~100s edge timeout (Cloudflare 524). So the agent starts
# the job (fast POST) and polls for the result — no single request stays open long.
_heal_fix_jobs: dict[str, dict] = {}
_heal_fix_lock = threading.Lock()


def _run_heal_fix_job(
    job_id: str, case_id: int, current_code: str, error: str, output: str, dom: dict | None
) -> None:
    """Background worker: run :func:`heal_service.plan_fix` off the request thread.

    Uses its own DB session (the request's session is closed once the POST returns)
    and stores the terminal outcome in ``_heal_fix_jobs`` for the agent to poll.
    Never raises — a failure is recorded as an ``error`` job the agent can surface.
    """
    db = SessionLocal()
    try:
        case = db.get(TestCase, case_id)
        run = db.get(Run, case.run_id) if case else None
        if case is None or run is None:
            result = {"action": "rejected", "reason": "Case/run not found for heal fix."}
        else:
            result = heal_service.plan_fix(db, case, run, current_code, error, output, dom)
        with _heal_fix_lock:
            _heal_fix_jobs[job_id] = {"status": "done", "result": result}
    except Exception as exc:  # noqa: BLE001 - surface to the agent, never crash the thread
        logger.error("Heal fix job {} failed: {}", job_id, exc)
        with _heal_fix_lock:
            _heal_fix_jobs[job_id] = {"status": "error", "error": str(exc)}
    finally:
        db.close()


@router.post("/heal/{case_id}/fix")
def agent_heal_fix(
    case_id: int, body: dict, user: User = Depends(require_agent), db: Session = Depends(get_db)
) -> dict:
    """Start an async heal-fix job and return its id immediately (#313).

    The fix (classify + ask Claude, server holds the LLM creds + KB) is a ~3-min
    call, so it runs in a background thread instead of inline — a synchronous
    response would exceed a fronting proxy's ~100s cap (Cloudflare 524) and the
    agent would never receive the fix. Body: ``{currentCode, error, output,
    domDistilled, attempt}``. Returns ``{jobId, status:"running"}``; the agent then
    polls :func:`agent_heal_fix_status`.
    """
    _owned_case_and_run(db, case_id, user)  # ownership guard (404 if not the caller's)
    job_id = uuid.uuid4().hex
    with _heal_fix_lock:
        _heal_fix_jobs[job_id] = {"status": "running"}
    threading.Thread(
        target=_run_heal_fix_job,
        args=(
            job_id,
            case_id,
            body.get("currentCode") or "",
            body.get("error") or "",
            body.get("output") or "",
            body.get("domDistilled"),
        ),
        daemon=True,
    ).start()
    return {"jobId": job_id, "status": "running"}


@router.get("/heal/{case_id}/fix/{job_id}")
def agent_heal_fix_status(
    case_id: int, job_id: str, user: User = Depends(require_agent), db: Session = Depends(get_db)
) -> dict:
    """Poll a heal-fix job started by :func:`agent_heal_fix` (#313).

    Returns ``{status:"running"}`` while the fix is generating, or the terminal
    ``{status:"done", result:{…plan…}}`` / ``{status:"error", error}`` once ready.
    A terminal job is popped on delivery so the store doesn't grow unbounded. 404
    if the job id is unknown (e.g. the API restarted mid-fix).
    """
    _owned_case_and_run(db, case_id, user)  # ownership guard
    with _heal_fix_lock:
        job = _heal_fix_jobs.get(job_id)
        if job is not None and job["status"] in ("done", "error"):
            _heal_fix_jobs.pop(job_id, None)
    if job is None:
        raise HTTPException(status_code=404, detail="Heal fix job not found")
    if job["status"] == "done":
        return {"status": "done", "result": job["result"]}
    if job["status"] == "error":
        return {"status": "error", "error": job["error"]}
    return {"status": "running"}


@router.post("/heal/{case_id}/finalize")
def agent_heal_finalize(
    case_id: int, body: dict, user: User = Depends(require_agent), db: Session = Depends(get_db)
) -> dict:
    """Persist an agent heal's final outcome + feed a passing DOM-grounded heal into
    the KB. Body shape: see :func:`heal_service.finalize_agent_heal`."""
    case, run = _owned_case_and_run(db, case_id, user)
    heal_service.finalize_agent_heal(db, case, run, body)
    return {"ok": True}


# --------------------------------------------------- agent DOM exploration (#337)
# Mirrors the agent self-heal server-assist: the observe→decide→act LOOP runs on
# the paired device (Playwright + app access); these endpoints do the parts that
# need the server — claim a queued session, the per-step Claude decide (async
# start+poll, beating the ~100s proxy cap), progress relay, and the KB write.
# See app.services.agent_explore_service + app.services.exploration_agent.


def _run_explore_decide_job(
    job_id: str,
    *,
    target: dict,
    observation: dict,
    history: list[dict],
    steps_taken: int,
    run_id: int | None,
    owner_id: int | None,
    max_steps: int,
    allow_state_changing: bool,
    baseline_usd: float,
) -> None:
    """Background worker: run :func:`exploration_agent.decide_next_action` off the
    request thread (the decide is one ~minutes-long Claude call).

    Uses its own DB session (the request's closes once the POST returns) and runs
    under ``run_context.set_run(run_id)`` so the per-step Claude spend + credentials
    attribute to the run owner (mirrors :func:`heal_service.plan_fix`). Never raises
    — a failure is recorded as an ``error`` job the agent can surface.
    """
    db = SessionLocal()
    previous_run = run_context.get_run()
    if run_id is not None:
        run_context.set_run(run_id)
    try:
        result = exploration_agent.decide_next_action(
            db,
            target=target,
            observation=observation,
            history=history,
            steps_taken=steps_taken,
            run_id=run_id,
            owner_id=owner_id,
            max_steps=max_steps,
            allow_state_changing=allow_state_changing,
            baseline_usd=baseline_usd,
        )
        agent_explore_service.finish_decide_job(job_id, result=result)
    except Exception as exc:  # noqa: BLE001 - surface to the agent, never crash the thread
        logger.error("Explore decide job {} failed: {}", job_id, exc)
        agent_explore_service.finish_decide_job(job_id, error=str(exc))
    finally:
        if run_id is not None:
            run_context.set_run(previous_run)
        db.close()


@router.post("/explore/next")
def agent_explore_next(
    response: Response, user: User = Depends(require_agent), db: Session = Depends(get_db)
) -> dict | None:
    """Claim the oldest queued exploration session for this device's owner (#337).

    The paired agent then drives the observe→decide→act loop locally, calling
    ``/agent/explore/{id}/decide`` per step and ``/agent/explore/{id}/finalize`` at
    the end. Returns the frozen claim payload, or 204 when nothing is queued.
    """
    claim = agent_explore_service.claim_next(user.id)
    if claim is None:
        response.status_code = 204
        return None
    return ExploreClaimOut(
        session_id=claim["session_id"],
        base_url=claim["base_url"],
        origin=claim["origin"],
        target=ExploreTarget(**(claim["target"] or {})),
        max_steps=claim["max_steps"],
        allow_state_changing=claim["allow_state_changing"],
        project_key=claim["project_key"],
        repo=claim["repo"],
        run_id=claim.get("run_id"),
    ).model_dump(by_alias=True)


@router.post("/explore/{session_id}/decide", response_model=ExploreDecideStartOut)
def agent_explore_decide(
    session_id: str,
    body: ExploreDecideRequest,
    user: User = Depends(require_agent),
    db: Session = Depends(get_db),
) -> ExploreDecideStartOut:
    """Start an async decide job for one exploration step and return its id (#337).

    The decide is a Claude call (server holds the LLM creds + enforces the cost
    budget), so it runs on a background thread — a synchronous response would
    exceed a fronting proxy's ~100s cap (mirrors :func:`agent_heal_fix`). The agent
    then polls :func:`agent_explore_decide_status`.
    """
    session = agent_explore_service.get_session(session_id, user.id)
    if session is None:
        raise HTTPException(status_code=404, detail="Exploration session not found")
    run_id = session.get("run_id")
    # Capture the run's pre-exploration spend once, so the cost budget charges
    # only this session's decide calls (not the run's lifetime AI cost, which
    # would otherwise trip the ceiling on step 0).
    current_spend_usd = exploration_agent._session_spend(db, run_id)["usd"] if run_id is not None else 0.0
    baseline_usd = agent_explore_service.ensure_baseline(session_id, current_spend_usd)
    job_id = agent_explore_service.start_decide_job()
    threading.Thread(
        target=_run_explore_decide_job,
        kwargs={
            "job_id": job_id,
            "target": session["target"] or {},
            "observation": body.observation or {},
            "history": body.history or [],
            "steps_taken": body.steps_taken,
            "run_id": run_id,
            "owner_id": user.id,
            "max_steps": session["max_steps"],
            "allow_state_changing": session["allow_state_changing"],
            "baseline_usd": baseline_usd,
        },
        daemon=True,
    ).start()
    return ExploreDecideStartOut(job_id=job_id, status="running")


@router.get("/explore/{session_id}/decide/{job_id}", response_model=ExploreDecideStatusOut)
def agent_explore_decide_status(
    session_id: str,
    job_id: str,
    user: User = Depends(require_agent),
    db: Session = Depends(get_db),
) -> ExploreDecideStatusOut:
    """Poll a decide job started by :func:`agent_explore_decide` (#337).

    ``{status:"running"}`` while deciding, or the terminal
    ``{status:"done", result:{action,args,reasoning,stop?,stopReason?}}`` /
    ``{status:"error", error}``. Terminal jobs are popped on delivery. 404 for an
    unknown id (e.g. the API restarted mid-decide).
    """
    job = agent_explore_service.take_decide_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Explore decide job not found")
    if job["status"] == "done":
        return ExploreDecideStatusOut(status="done", result=job["result"])
    if job["status"] == "error":
        return ExploreDecideStatusOut(status="error", error=job["error"])
    return ExploreDecideStatusOut(status="running")


@router.post("/explore/{session_id}/events")
def agent_explore_events(
    session_id: str,
    body: ExploreEventRequest,
    user: User = Depends(require_agent),
    db: Session = Depends(get_db),
) -> dict:
    """Relay an agent-reported exploration progress event onto the run's WS channel.

    When the session carries a ``run_id`` the SPA's ``explore.progress`` subscribers
    see live steps exactly like the in-process loop's; a session with no run is a
    no-op (still ``{ok:true}``).
    """
    session = agent_explore_service.get_session(session_id, user.id)
    if session is None:
        raise HTTPException(status_code=404, detail="Exploration session not found")
    run_id = session.get("run_id")
    if run_id is not None:
        hub.publish(str(run_id), body.event, body.payload or {})
    return {"ok": True}


@router.post("/explore/{session_id}/finalize", response_model=ExploreFinalizeOut)
def agent_explore_finalize(
    session_id: str,
    body: ExploreFinalizeRequest,
    user: User = Depends(require_agent),
    db: Session = Depends(get_db),
) -> ExploreFinalizeOut:
    """Persist an exploration session's terminal outcome + KB-merge observed data (#337).

    Writes to the Knowledge Base ONLY when ``discovered`` carries observed
    routes/selectors (never invent — an unreachable target writes nothing and the
    case stays blocked, ADR 0010 §8), via
    :func:`knowledge_service.merge_verified_discovery`. Stores the terminal result
    so ``/explore/status`` can report the agent path. Returns ``{ok, wroteKb}``.
    """
    session = agent_explore_service.get_session(session_id, user.id)
    if session is None:
        raise HTTPException(status_code=404, detail="Exploration session not found")
    # Coerce the agent's raw discovered shape (route strings + {strategy,value}
    # selectors) into the KB writer's {path}/{screen,element,selector,strategy}
    # contract, else the merge writes nothing.
    screen = (session.get("target") or {}).get("screen") or ""
    discovered = exploration_agent.normalize_discovered(body.discovered or {}, screen=screen)
    routes = discovered["routes"]
    selectors = discovered["selectors"]
    wrote_kb = False
    if routes or selectors:
        merged = knowledge_service.merge_verified_discovery(
            session["project_key"],
            session["repo"],
            {"routes": routes, "selectors": selectors},
            owner_id=user.id,
        )
        wrote_kb = merged > 0
    agent_explore_service.set_result(
        session_id,
        {
            "status": "done",
            "stopReason": body.stop_reason,
            "stepsTaken": body.steps_taken,
            "wroteKb": wrote_kb,
            "discoveredRoutes": len(routes),
            "discoveredSelectors": len(selectors),
        },
    )
    # Durable per-run record of the outcome so the run's activity timeline shows
    # what this Explore did — including "ran but discovered nothing" and hard
    # failures (e.g. a Claude decide-error) — not just the transient WS trail (#394).
    run = (
        db.query(Run).filter(Run.id == session.get("run_id")).first()
        if session.get("run_id")
        else None
    )
    exploration_agent.audit_exploration_result(
        target=session.get("target") or {},
        stop_reason=body.stop_reason,
        steps_taken=body.steps_taken,
        discovered_routes=len(routes),
        discovered_selectors=len(selectors),
        wrote_kb=wrote_kb,
        run_code=run.code if run is not None else None,
        log=body.log,
        routes=routes,
        selectors=selectors,
    )
    logger.info(
        "Exploration finalize (session={} run={}): stopReason={} steps={} routes={} selectors={} wroteKb={} | log={}",
        session_id,
        session.get("run_id"),
        body.stop_reason,
        body.steps_taken,
        len(routes),
        len(selectors),
        wrote_kb,
        (body.log or [])[:6],
    )
    return ExploreFinalizeOut(ok=True, wrote_kb=wrote_kb)


# ------------------------------------------ Agent-driven live authoring (#400/403)
@router.post("/authoring/next")
def agent_authoring_next(
    response: Response, user: User = Depends(require_agent), db: Session = Depends(get_db)
) -> dict | None:
    """Claim the next queued live-authoring session for this device's owner.

    Returns everything the agent needs to author locally (prompts composed
    server-side), or 204 when nothing is queued.
    """
    claim = agent_authoring_service.claim_next(user.id)
    if claim is None:
        response.status_code = 204
        return None
    # Hand the agent the owner's effective saved Claude credential (own → shared),
    # so its local `claude` authenticates with the app's Settings credential rather
    # than a separate `claude login`. Best-effort: on any resolution error the
    # agent falls back to its own local login.
    creds = ""
    try:
        from app.services import claude_credentials

        config_dir = claude_credentials.resolve_effective_config_dir(db, user.id)
        if config_dir is not None:
            creds_file = config_dir / ".credentials.json"
            if creds_file.exists():
                creds = creds_file.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - creds are optional; log and continue
        logger.warning("Authoring claim: could not resolve Claude credential: {}", exc)
    return AuthoringClaimOut(
        session_id=claim["session_id"],
        base_url=claim["base_url"],
        origin=claim["origin"],
        project_key=claim["project_key"],
        repo=claim["repo"],
        case_id=claim["case_id"],
        run_id=claim.get("run_id"),
        spec_filename=claim["spec_filename"],
        system_prompt=claim["system_prompt"],
        task_prompt=claim["task_prompt"],
        model=claim["model"],
        max_budget_usd=claim["max_budget_usd"],
        log_verbosity=claim.get("log_verbosity", "concise"),
        claude_credentials=creds,
    ).model_dump(by_alias=True)


@router.post("/authoring/{session_id}/events")
def agent_authoring_events(
    session_id: str,
    body: AuthoringEventRequest,
    user: User = Depends(require_agent),
    db: Session = Depends(get_db),
) -> dict:
    """Relay an authoring progress event onto the run's WebSocket."""
    session = agent_authoring_service.get_session(session_id, user.id)
    if session is None:
        raise HTTPException(status_code=404, detail="Authoring session not found")
    run_id = session.get("run_id")
    if run_id is not None:
        hub.publish(str(run_id), body.event, body.payload or {})
    return {"ok": True}


@router.post("/authoring/{session_id}/finalize", response_model=AuthoringFinalizeOut)
def agent_authoring_finalize(
    session_id: str,
    body: AuthoringFinalizeRequest,
    user: User = Depends(require_agent),
    db: Session = Depends(get_db),
) -> AuthoringFinalizeOut:
    """Persist the agent-authored spec + KB-merge its runtime-verified discovery (#403).

    Runs the authored code through the shared gate/write/persist path
    (:func:`automation.finalize_authored_spec`), so a live-authored spec is
    gated and stored exactly like a blind/server-live one. Local import avoids a
    router import cycle.
    """
    session = agent_authoring_service.get_session(session_id, user.id)
    if session is None:
        raise HTTPException(status_code=404, detail="Authoring session not found")
    from app.routers.automation import finalize_authored_spec

    spec = finalize_authored_spec(
        db,
        session["run_id"],
        session["case_id"],
        body.code or "",
        body.discovered or {},
    )
    ok = spec is not None and (spec.code or "").strip() != ""
    # Auto-rotate the uploaded credential (#cred-rotate): the device ran `claude`
    # with the owner's credential and the CLI may have refreshed the OAuth token in
    # its (now-deleted) temp config dir. Capture the rotated token the agent posted
    # back so the stored credential's refresh token can't go stale. Best-effort.
    if body.refreshed_credentials:
        try:
            from app.services import claude_credentials

            if claude_credentials.persist_refreshed_from_raw(db, user.id, body.refreshed_credentials):
                logger.info("Captured a Claude token the agent refreshed (owner={})", user.id)
        except Exception as exc:  # noqa: BLE001 - rotation is additive; never break finalize
            logger.warning("Could not persist agent-refreshed Claude credential: {}", exc)
    # Record the agent's agentic Claude spend against the run (it runs on the
    # paired device, so the server never saw it) — rolls into the run cost
    # breakdown + AI stats and the authoring budget pre-check. Best-effort.
    if body.cost_usd and body.cost_usd > 0:
        try:
            from app.services import ai_usage_service

            case = db.get(TestCase, session["case_id"])
            ai_usage_service.record(
                model=session.get("model") or "",
                input_tokens=0,
                output_tokens=0,
                cache_read=0,
                cache_write=0,
                cost_usd=float(body.cost_usd),
                duration_ms=0,
                action="live-authoring",
                run_id=session.get("run_id"),
                owner_id=user.id,
                ticket_external_id=case.ticket_external_id if case else None,
            )
        except Exception as exc:  # noqa: BLE001 - cost recording is additive
            logger.warning("Authoring cost record skipped: {}", exc)
    agent_authoring_service.set_result(
        session_id,
        {"status": "done" if ok else "failed", "summary": (body.summary or "")[:800], "costUsd": body.cost_usd},
    )
    logger.info(
        "Authoring finalize (session={} run={} case={}): ok={} status={}",
        session_id,
        session.get("run_id"),
        session.get("case_id"),
        ok,
        spec.status if spec is not None else "no-spec",
    )
    return AuthoringFinalizeOut(ok=ok)
