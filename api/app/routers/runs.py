"""Runs + AI analysis + test-case generation router.

Endpoints implemented:
  GET    /runs                      -> list[RunOut]
  POST   /runs                      -> RunDetailOut     (body: RunCreate; kicks off async AI pipeline)
  GET    /runs/{run_id}             -> RunDetailOut
  GET    /runs/{run_id}/tickets     -> list[RunTicketOut]  (per-ticket analysis + gen status)
  POST   /runs/{run_id}/regenerate  -> RunDetailOut     (re-run analysis/generation)
  POST   /runs/{run_id}/cancel      -> RunOut            (ADR 0005 — cancel an in-progress run)
  POST   /runs/{run_id}/retry       -> RunOut            (ADR 0005 — resume a terminal run)
  DELETE /runs/{run_id}             -> 204                (ADR 0005 — hard delete + cascade)

On create: for each ticket -> Claude analyze (business rules, risks, edge cases…)
-> Claude generate ADO-style test cases -> persist TestCase rows -> advance
Run.status processing→review. Publish WS progress events per ticket/phase.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import get_db, utcnow
from app.deps_auth import current_user
from app.deps_hub import hub_token as hub_token_dep
from app.models.claude_usage import ClaudeUsage
from app.models.comment import TicketComment
from app.models.execution import Execution, ExecutionResult
from app.models.linked import LinkedTestCase
from app.models.project import Project
from app.models.project_config import ProjectConfig
from app.models.report import Report
from app.models.run import TERMINAL_RUN_STATUSES, Run, RunTicket
from app.models.testcase import TestCase
from app.models.ticket import Ticket
from app.models.user import User
from app.routers import automation as automation_router
from app.routers import comments as comments_router
from app.routers import execution as execution_router
from app.schemas import (
    RunCreate,
    RunDetailOut,
    RunOut,
    RunRepoOptionOut,
    RunTicketOut,
    RunTicketRepoUpdate,
)
from app.services import (
    ai_usage_service,
    audit_service,
    hub_credentials,
    link_service,
    project_config_service,
    run_control,
    run_status,
    sample_run_service,
)
from app.services.ai_service import run_generation_pipeline
from app.services.ownership import get_owned_or_404, owned, stamp_owner
from app.services.run_status import force_status, set_run_status
from app.ws import hub

router = APIRouter(prefix="/runs", tags=["runs"])

# ADR 0005 retry dispatch table: failed_stage (resume from) -> resume stage.
# "review" has nothing of its own to resume (it's a user-gated stop) so it
# re-runs AI generation; unknown/null falls back to "processing" too.
_RETRY_RESUME_STAGE = {
    "processing": "processing",
    "review": "processing",
    "sync": "sync",
    "automation": "automation",
    "executing": "executing",
    "evidence": "executing",
    "comment": "comment",
}

SCOPE_LABELS = {
    "single": "Single ticket",
    "selected": "Selected tickets",
    "assigned": "Assigned to me",
    "sprint": "Current sprint",
}


def _next_run_code(db: Session) -> str:
    """Compute the next RUN-{n} code: max existing numeric suffix + 1, starting at 200."""
    max_n = 199
    for (code,) in db.query(Run.code).all():
        match = re.match(r"RUN-(\d+)$", code or "")
        if match:
            max_n = max(max_n, int(match.group(1)))
    return f"RUN-{max_n + 1}"


# Re-exported for readability at the call sites below; defined in
# ``project_config_service`` so the routers that need it (runs, reports) share
# one constant instead of importing each other.
UNASSIGNED_PROJECT = project_config_service.UNASSIGNED_PROJECT


def _project_key_for_guid(db: Session, guid: str) -> str | None:
    """Translate a project GUID into the key (name) that config is stored under.

    Prefers the config row's own ``project_guid`` link over a name lookup: it is
    a stored reference, and its ``key`` is by definition the string the rest of
    the resolution code expects (which is not always identical in case to
    ``Project.name``).
    """
    cfg = db.query(ProjectConfig).filter(ProjectConfig.project_guid == guid).first()
    if cfg is not None:
        return cfg.key
    project = db.query(Project).filter(Project.guid == guid).first()
    return project.name if project is not None else None


def _resolve_run_project_guid(db: Session, run: Run) -> str | None:
    """The GUID of the project a run belongs to.

    The stamped column (#727) is authoritative. The first-ticket walk below is
    kept only as a fallback for rows that predate stamping and could not be
    backfilled — it is the fragility ADR 0013 recorded, so nothing new should
    depend on it.
    """
    if run.project_guid:
        return run.project_guid
    first = (
        db.query(RunTicket)
        .filter(RunTicket.run_id == run.id)
        .order_by(RunTicket.position)
        .first()
    )
    if first is None:
        return None
    ticket = (
        db.query(Ticket).filter(Ticket.external_id == first.ticket_external_id).first()
    )
    if ticket is None:
        return None
    return project_config_service.project_guid_for_ticket(db, ticket)


def _resolve_run_project_key(db: Session, run: Run) -> str | None:
    """Resolve the project key a run's tickets belong to.

    Reads the stamped ``Run.project_guid`` when there is one, and only falls back
    to walking the run's first ticket for un-backfilled legacy rows.
    """
    guid = run.project_guid
    if guid:
        key = _project_key_for_guid(db, guid)
        if key:
            return key
    first = (
        db.query(RunTicket)
        .filter(RunTicket.run_id == run.id)
        .order_by(RunTicket.position)
        .first()
    )
    if first is None:
        return None
    ticket = (
        db.query(Ticket).filter(Ticket.external_id == first.ticket_external_id).first()
    )
    if ticket is None:
        return None
    return project_config_service.project_key_for_ticket(db, ticket)


def _attach_run_aggregates(db: Session, runs: list[Run]) -> list[Run]:
    """Attach list-display aggregates to each run as transient attributes:
    ``case_count`` (# test cases), ``total``/``passed`` (from the run's latest
    execution — the "passed / N" progress), and ``pass_rate`` (0..100 from the
    run's latest report, else None). Batched — three grouped queries total, no
    per-run N+1.
    """
    if not runs:
        return runs
    run_ids = [r.id for r in runs]

    case_counts = dict(
        db.query(TestCase.run_id, func.count(TestCase.id))
        .filter(TestCase.run_id.in_(run_ids))
        .group_by(TestCase.run_id)
        .all()
    )

    # Latest execution / report per run via max(id) over the run's rows.
    latest_exec_ids = [
        eid
        for (eid,) in db.query(func.max(Execution.id))
        .filter(Execution.run_id.in_(run_ids))
        .group_by(Execution.run_id)
        .all()
    ]
    execs = {
        e.run_id: e for e in db.query(Execution).filter(Execution.id.in_(latest_exec_ids)).all()
    }
    latest_report_ids = [
        rid
        for (rid,) in db.query(func.max(Report.id))
        .filter(Report.run_id.in_(run_ids))
        .group_by(Report.run_id)
        .all()
    ]
    reports = {
        r.run_id: r for r in db.query(Report).filter(Report.id.in_(latest_report_ids)).all()
    }

    for run in runs:
        run.case_count = case_counts.get(run.id, 0)
        execution = execs.get(run.id)
        run.total = execution.total if execution else 0
        run.passed = execution.passed if execution else 0
        report = reports.get(run.id)
        run.pass_rate = report.pass_rate if report else None
        run.result = _run_result(execution)
    return runs


def _prepare_claude_credential(db: Session, run: Run, hub_token: str | None) -> None:
    """Pin the Claude credential this run will use, before any worker starts (#499).

    With the hub integration on and a fresh hub token on the request, the
    credential is resolved from EmeHub *here* — in the request, while the token is
    still valid — and materialized to disk; the background pipeline then reads
    that file and never calls the hub. With the flag off (or no token) this is a
    no-op and the run resolves locally exactly as before.

    A hub that authoritatively reports no usable credential fails the run
    immediately with 409 rather than letting it proceed on a possibly-stale local
    one. The run is marked ``failed`` first so it doesn't sit in ``processing``
    forever.
    """
    try:
        hub_credentials.prepare_run_credential(run.id, hub_token)
    except hub_credentials.HubCredentialRefusedError as exc:
        run.failed_stage = "processing"
        db.add(run)
        db.commit()
        force_status(db, run, "failed")
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _run_result(execution: Execution | None) -> str:
    """QA verdict for a run from its latest execution, independent of pipeline stage.

    Args:
        execution: The run's latest :class:`Execution`, or None if it never ran.

    Returns:
        ``"not_run"`` (no execution / zero cases), ``"failed"`` (>=1 failed),
        ``"mixed"`` (both passes and failures), or ``"passed"`` (all passed).
    """
    if execution is None or execution.total == 0:
        return "not_run"
    if execution.failed > 0 and execution.passed > 0:
        return "mixed"
    if execution.failed > 0:
        return "failed"
    return "passed"


@router.get("", response_model=list[RunOut])
def list_runs(
    project: str | None = Query(None),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> list[Run]:
    """The caller's runs, newest first.

    ``?project=<guid>`` narrows the list to one project (ADR 0015: every run list
    in the UI is now reached *through* a project). ``?project=unassigned``
    returns the runs whose project could not be resolved — without that bucket
    those rows would be unreachable from a project-scoped UI.
    """
    query = owned(db.query(Run), Run, user)
    if project == UNASSIGNED_PROJECT:
        query = query.filter(Run.project_guid.is_(None))
    elif project:
        query = query.filter(Run.project_guid == project)
    runs = query.order_by(Run.created_at.desc()).all()
    return _attach_run_aggregates(db, runs)


def _project_guid_for_ticket_ids(
    db: Session, ticket_ids: list[str], user: User | None
) -> str | None:
    """The single project every ticket in a new run belongs to (#727).

    Stamping happens here, at creation, rather than being derived on read — see
    ``Run.project_guid``. Resolving all of the tickets instead of just the first
    also makes the mixed-project invariant free: it is the same walk.

    A run spanning two projects is refused with 400. Once slice 6 scopes the
    ticket picker to the project the user is inside (ADR 0015 §9) this becomes
    unreachable through the UI, but it stays as a cheap server-side invariant —
    the API is public, and a mixed run would silently corrupt every
    project-scoped count downstream.

    Tickets that resolve to no project do not block creation (an install whose
    project is only *indexed* has no ``projects`` row to resolve to); they simply
    contribute nothing. When none of the tickets resolve, the run is stamped NULL
    and lands in the ``unassigned`` bucket.
    """
    tickets = owned(
        db.query(Ticket).filter(Ticket.external_id.in_(ticket_ids)), Ticket, user
    ).all()
    guids = {
        guid
        for guid in (
            project_config_service.project_guid_for_ticket(db, t) for t in tickets
        )
        if guid
    }
    if len(guids) > 1:
        raise HTTPException(
            status_code=400,
            detail=(
                "A run cannot span multiple projects "
                f"({len(guids)} projects in {len(ticket_ids)} ticket(s))."
            ),
        )
    return next(iter(guids), None)


@router.post("", response_model=RunDetailOut)
def create_run(
    body: RunCreate,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
    hub_token: str | None = Depends(hub_token_dep),
) -> Run:
    ticket_ids = list(body.ticket_ids or [])
    # For a sprint-scoped run without explicit ids, resolve the sprint's tickets
    # from the synced DB (matched on the sprint leaf name).
    if not ticket_ids and body.scope == "sprint" and body.sprint:
        ticket_ids = [
            t.external_id
            for t in db.query(Ticket).filter(Ticket.sprint == body.sprint).all()
        ]
        if not ticket_ids:
            raise HTTPException(
                status_code=400,
                detail=f"No synced tickets found for sprint '{body.sprint}'. Sync the sprint first.",
            )
    if not ticket_ids:
        raise HTTPException(status_code=400, detail="ticket_ids must not be empty")

    project_guid = _project_guid_for_ticket_ids(db, ticket_ids, user)

    run = Run(
        code=_next_run_code(db),
        name=f"Run over {len(ticket_ids)} ticket(s)",
        scope=body.scope,
        scope_label=SCOPE_LABELS.get(body.scope, body.scope),
        framework=body.framework,
        browser=body.browser,
        env=body.env,
        workers=body.workers,
        retry_policy=body.retry_policy,
        status="processing",
        project_guid=project_guid,
        # Link options (#732, ADR 0015 §5) — chosen in the Create Run modal now
        # that the `sync` stage is hidden. Stored, not acted on here: the Link
        # stage reads them when it runs.
        link_enabled=bool(body.link),
        link_dry_run=bool(body.dry_run),
        # Only ids that are actually in this run's scope. A subset naming a ticket
        # the run does not contain is not a smaller selection, it is a typo, and
        # silently carrying it would make the Link stage skip everything.
        link_ticket_ids=[tid for tid in (body.link_ticket_ids or []) if tid in set(ticket_ids)],
    )
    stamp_owner(run, user)
    db.add(run)
    db.flush()

    for position, ticket_external_id in enumerate(ticket_ids):
        db.add(
            RunTicket(
                run_id=run.id,
                ticket_external_id=ticket_external_id,
                position=position,
                gen_status="queued",
            )
        )
    db.commit()
    db.refresh(run)

    audit_service.record(
        category="run", actor_type="user", action="Created run",
        target=f"{run.code} · {run.name}",
        meta=f"{run.framework} · {run.env} · {run.workers} workers",
    )

    _prepare_claude_credential(db, run, hub_token)

    run_generation_pipeline(run.id, blocking=False)

    # If the pipeline ran synchronously (blocking, e.g. in tests) it committed via
    # its own session — refresh so this response reflects the final state.
    db.refresh(run)

    return run


@router.post("/sample", response_model=RunDetailOut)
def create_sample_run(
    db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> Run:
    """Seed (or return) a fully-populated DEMO run for the product tour.

    Idempotent — one ``RUN-DEMO`` run per user; a repeat call returns it
    unchanged. Inserts the whole run row graph directly (tickets, cases, specs,
    links, execution results, evidence, report, publish comments) so every
    run-scoped screen renders. Never runs the AI generation pipeline.
    """
    run = sample_run_service.ensure_sample_run(db, user)
    audit_service.record(
        category="run", actor_type="user", action="Seeded sample run",
        target=f"{run.code} · {run.name}",
    )
    return run


@router.get("/{run_id}", response_model=RunDetailOut)
def get_run(
    run_id: int, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> Run:
    run = get_owned_or_404(db, Run, run_id, user)
    _attach_run_aggregates(db, [run])
    return run


@router.get("/{run_id}/tickets", response_model=list[RunTicketOut])
def list_run_tickets(
    run_id: int, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> list[RunTicket]:
    get_owned_or_404(db, Run, run_id, user)
    return (
        db.query(RunTicket)
        .filter(RunTicket.run_id == run_id)
        .order_by(RunTicket.position)
        .all()
    )


@router.get("/{run_id}/ai-usage")
def get_run_ai_usage(
    run_id: int, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> dict:
    """Per-run Claude cost/token attribution, grouped by process (see contract)."""
    get_owned_or_404(db, Run, run_id, user)
    return ai_usage_service.run_breakdown(db, run_id)


@router.get("/{run_id}/repos", response_model=list[RunRepoOptionOut])
def list_run_repos(
    run_id: int, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> list[dict]:
    """The run's project repositories, each with its per-repo knowledge status.

    Resolves the project from the run's first work item's ticket provider; returns
    an empty list when no project can be resolved.
    """
    run = get_owned_or_404(db, Run, run_id, user)
    key = _resolve_run_project_key(db, run)
    if not key:
        return []
    return project_config_service.repo_options(db, key)


@router.post("/{run_id}/tickets/{tid}/repo", response_model=RunTicketOut)
def set_run_ticket_repo(
    run_id: int,
    tid: str,
    body: RunTicketRepoUpdate,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> RunTicket:
    """Set a work item's target repository.

    An empty ``repo`` resets it to the project default. A non-empty value must be
    one of the project's configured repo names, else HTTP 400.
    """
    run = get_owned_or_404(db, Run, run_id, user)
    run_ticket = (
        db.query(RunTicket)
        .filter(RunTicket.run_id == run.id, RunTicket.ticket_external_id == tid)
        .first()
    )
    if run_ticket is None:
        raise HTTPException(status_code=404, detail="Run ticket not found")

    repo = (body.repo or "").strip()
    if repo:
        key = _resolve_run_project_key(db, run)
        configured = {opt["name"] for opt in project_config_service.repo_options(db, key)} if key else set()
        if repo not in configured:
            raise HTTPException(
                status_code=400, detail=f"Repo '{repo}' is not configured for this project"
            )

    run_ticket.repo = repo
    db.add(run_ticket)
    db.commit()
    db.refresh(run_ticket)
    return run_ticket


@router.post("/{run_id}/regenerate", response_model=RunDetailOut)
def regenerate_run(
    run_id: int,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
    hub_token: str | None = Depends(hub_token_dep),
) -> Run:
    run = get_owned_or_404(db, Run, run_id, user)

    # Clear prior AI output so the pipeline starts fresh.
    db.query(TestCase).filter(TestCase.run_id == run.id).delete()
    for run_ticket in db.query(RunTicket).filter(RunTicket.run_id == run.id).all():
        run_ticket.gen_status = "queued"
        run_ticket.analysis = {}
        run_ticket.analysis_error = ""
        db.add(run_ticket)

    db.commit()
    set_run_status(db, run, "processing")

    audit_service.record(
        category="run", actor_type="user", action="Regenerated run",
        target=f"{run.code} · {run.name}",
    )

    _prepare_claude_credential(db, run, hub_token)

    run_generation_pipeline(run.id, blocking=False)

    # If the pipeline ran synchronously (blocking, e.g. in tests) it committed via
    # its own session — refresh so this response reflects the final state.
    db.refresh(run)

    return run


def _stop_run_work(db: Session, run: Run) -> None:
    """Stop every in-flight process for a run and reset stuck rows (#420).

    Complements the cooperative cancel (``run_control``): once the run is flipped
    to ``cancelled`` this evicts queued/in-memory work so nothing gets claimed or
    resumed later, and rewrites rows still marked in-flight to a terminal/idle
    value so the UI stops showing perpetual spinners. Completed rows are left
    untouched (see ADR 0005 / the confirmed "stop + reset stuck rows" scope).
    """
    run_id = run.id

    # 1) Specs left "running" by live-authoring or self-heal: keep whatever was
    #    authored so far (-> draft), else mark blocked with a reason. Collect their
    #    cases so we can close the live trail on any open client (step 5). Shared
    #    with the #605 boot sweep (`run_status.recover_orphaned_authoring`) so the
    #    two stuck-spec resets can't drift apart.
    stopped_cases = [
        (case_id, ticket, code)
        for (_run_id, case_id, ticket, code) in run_status.reset_stuck_specs(
            db, run_id=run_id, reason="Stopped before authoring finished."
        )
    ]

    # 2) In-flight executions + their pending/running case results.
    for execution in (
        db.query(Execution)
        .filter(Execution.run_id == run_id, Execution.status.in_(("queued", "running")))
        .all()
    ):
        execution.status = "failed"
        db.add(execution)
        for result in (
            db.query(ExecutionResult)
            .filter(
                ExecutionResult.execution_id == execution.id,
                ExecutionResult.status.in_(("pending", "running")),
            )
            .all()
        ):
            result.status = "skipped"
            db.add(result)

    # 3) Tickets stuck mid analyze/generate.
    for run_ticket in (
        db.query(RunTicket)
        .filter(RunTicket.run_id == run_id, RunTicket.gen_status.in_(("analyzing", "generating")))
        .all()
    ):
        run_ticket.gen_status = "error"
        if not (run_ticket.analysis_error or "").strip():
            run_ticket.analysis_error = "Stopped by user."
        db.add(run_ticket)

    db.commit()

    # 4) Purge in-memory queues/registries so no worker picks the run up again.
    from app.services import agent_authoring_service, agent_explore_service, playwright_runner

    agent_authoring_service.purge_run(run_id)
    agent_explore_service.purge_run(run_id)
    playwright_runner.purge_run(run_id)
    link_service.forget_run(run_id)
    automation_router.forget_generating(run_id)

    # 5) Close any live trail still open on a client. The agent's own progress
    #    posts now 404 (its session was just purged), so it can no longer re-open
    #    the trail — but the client is still holding the last non-terminal event,
    #    so without a terminal event here the panel keeps showing "authoring…"
    #    forever (the reported bug). Publish a terminal authoring.progress (covers
    #    live authoring AND live-heal, which reuse the authoring pipeline) plus a
    #    spec.regenerated refresh so the panel flips back to the (now draft) editor.
    for case_id, ticket, code in stopped_cases:
        hub.publish(
            str(run_id),
            "authoring.progress",
            {"case": case_id, "caseId": case_id, "ticket": ticket, "caseCode": code,
             "phase": "failed", "message": "Stopped by user."},
        )
        hub.publish(str(run_id), "spec.regenerated", {"caseId": case_id})


@router.post("/{run_id}/cancel", response_model=RunOut)
def cancel_run(
    run_id: int, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> Run:
    """Cancel an in-progress run (ADR 0005). 409 if it's already terminal.

    Persists ``cancel_requested``/``cancelled_at``, signals the in-memory
    cancel event, kills any tracked live subprocess (mid-case Playwright kill),
    transitions the run to ``cancelled``, then stops/cleans up every in-flight
    process + stuck DB row for the run (#420 — authoring, self-heal, execution,
    analysis). Authoritative because every worker checkpoint checks the terminal
    guard before advancing.
    """
    run = get_owned_or_404(db, Run, run_id, user)
    if run.status in TERMINAL_RUN_STATUSES:
        raise HTTPException(status_code=409, detail="Run is already terminal")

    run.cancel_requested = True
    run.cancelled_at = utcnow()
    db.add(run)
    db.commit()

    run_control.request_cancel(run.id)
    run_control.kill_processes(run.id)
    set_run_status(db, run, "cancelled")
    _stop_run_work(db, run)

    audit_service.record(
        category="run", actor_type="user", action="Cancelled run", target=run.code,
    )
    db.refresh(run)
    return run


@router.post("/{run_id}/stop", response_model=RunOut)
def stop_run(
    run_id: int, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> Run:
    """Stop a run's work and clean up stuck state (#420) — valid in ANY status.

    Unlike :func:`cancel_run` (which 409s on a terminal run), this always runs the
    cleanup, so it doubles as a "force clean up" for a run whose earlier pass
    crashed and left orphaned in-flight rows / stale agent-queue entries:

    - **In-progress run:** request cancel, kill tracked subprocesses, transition to
      ``cancelled``, then clean up.
    - **Terminal run:** leave the lifecycle status untouched (retry still keys on
      ``failed_stage``), but kill any stray tracked process and run the cleanup so
      orphaned ``running`` specs/executions + stale authoring/explore/heal queue
      entries are cleared.
    """
    run = get_owned_or_404(db, Run, run_id, user)
    terminal = run.status in TERMINAL_RUN_STATUSES

    if not terminal:
        run.cancel_requested = True
        run.cancelled_at = utcnow()
        db.add(run)
        db.commit()
        set_run_status(db, run, "cancelled")

    run_control.request_cancel(run.id)
    run_control.kill_processes(run.id)
    _stop_run_work(db, run)

    audit_service.record(
        category="run", actor_type="user",
        action="Cleaned up run" if terminal else "Cancelled run", target=run.code,
    )
    db.refresh(run)
    return run


@router.post("/{run_id}/retry", response_model=RunOut)
def retry_run(
    run_id: int,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
    hub_token: str | None = Depends(hub_token_dep),
) -> Run:
    """Resume a terminal run from ``failed_stage`` (ADR 0005 dispatch table).

    409 unless the run is terminal (``done``/``cancelled``/``failed``). Resets
    the cancel bookkeeping, clears the in-process cancel/process registry, then
    directly moves the run out of its terminal status (bypassing the guard —
    this is the one intentional exception to it) and re-dispatches the resume
    stage's existing worker entry point.
    """
    run = get_owned_or_404(db, Run, run_id, user)
    if run.status not in TERMINAL_RUN_STATUSES:
        raise HTTPException(status_code=409, detail="Run is not terminal — cancel it first")

    resume_stage = _RETRY_RESUME_STAGE.get(run.failed_stage or "", "processing")

    run_control.clear(run.id)
    run.cancel_requested = False
    run.cancelled_at = None
    run.finished_at = None
    run.failed_stage = None
    db.add(run)
    db.commit()

    force_status(db, run, resume_stage)

    audit_service.record(
        category="run", actor_type="user", action="Retried run",
        target=f"{run.code} · resumed at {resume_stage}",
    )

    # Every resume stage can end up calling Claude, so re-pin the credential here
    # (the retry request carries its own fresh hub token; the one the original run
    # used is long expired).
    _prepare_claude_credential(db, run, hub_token)

    if resume_stage == "processing":
        # Clear prior AI output so the pipeline starts fresh (mirrors regenerate_run).
        db.query(TestCase).filter(TestCase.run_id == run.id).delete()
        for run_ticket in db.query(RunTicket).filter(RunTicket.run_id == run.id).all():
            run_ticket.gen_status = "queued"
            run_ticket.analysis = {}
            run_ticket.analysis_error = ""
            db.add(run_ticket)
        db.commit()
        run_generation_pipeline(run.id, blocking=False)
    elif resume_stage == "sync":
        link_service.start_create_link(run.id, link=True, ticket_ids=None)
    elif resume_stage == "automation":
        automation_router.generate_automation(run.id, force=False, db=db, user=user)
    elif resume_stage == "executing":
        execution_router.start_execution(run.id, body={}, db=db, user=user)
    elif resume_stage == "comment":
        comments_router.retry_comments(run.id, db, user=user)

    db.refresh(run)
    return run


@router.delete("/{run_id}", status_code=204)
def delete_run(
    run_id: int, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> None:
    """Hard-delete a run and all related rows in one transaction (ADR 0005).

    409 if the run is still in progress — cancel it first. SQLite does not
    enforce ``ondelete`` without ``PRAGMA foreign_keys=ON``, so related rows are
    removed explicitly: executions and test cases are deleted via the ORM (so
    their own children — execution results/evidence, automation specs —
    cascade too); reports/comments/claude usage are bulk-deleted by run_id;
    linked test cases are kept but detached (``run_id`` set to ``NULL``).
    """
    run = get_owned_or_404(db, Run, run_id, user)
    if run.status not in TERMINAL_RUN_STATUSES:
        raise HTTPException(status_code=409, detail="Run is in progress — cancel it first")

    code = run.code

    for execution in db.query(Execution).filter(Execution.run_id == run_id).all():
        db.delete(execution)  # cascades to ExecutionResult -> Evidence
    for case in db.query(TestCase).filter(TestCase.run_id == run_id).all():
        db.delete(case)  # cascades to AutomationSpec

    db.query(Report).filter(Report.run_id == run_id).delete(synchronize_session=False)
    db.query(TicketComment).filter(TicketComment.run_id == run_id).delete(synchronize_session=False)
    db.query(ClaudeUsage).filter(ClaudeUsage.run_id == run_id).delete(synchronize_session=False)
    db.query(LinkedTestCase).filter(LinkedTestCase.run_id == run_id).update(
        {LinkedTestCase.run_id: None}, synchronize_session=False
    )

    db.delete(run)  # cascades to RunTicket via the ORM delete-orphan relationship
    db.commit()

    run_control.clear(run_id)
    audit_service.record(category="run", actor_type="user", action="Deleted run", target=code)
