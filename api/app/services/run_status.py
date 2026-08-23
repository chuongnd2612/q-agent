"""Single transition point for ``Run.status`` — the terminal-guard invariant.

See ADR 0005. Every stage transition in the pipeline (AI generation, sync,
automation, execution, comment) goes through :func:`set_run_status` instead of
assigning ``run.status`` directly, so a worker thread that finishes a stage
*after* the run was cancelled/failed can never resurrect it into an
in-progress status.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db import utcnow
from app.logging import logger
from app.models.run import RUN_STATUSES, TERMINAL_RUN_STATUSES, Run
from app.models.testcase import AutomationSpec, TestCase
from app.services import audit_service
from app.ws import hub

# Active-work statuses with no live worker to recover after a restart —
# every non-terminal status except "review", which is a legitimate
# human-gated pause and must be left alone (see recover_orphaned_runs).
_ORPHANABLE_RUN_STATUSES = tuple(
    s for s in RUN_STATUSES if s not in TERMINAL_RUN_STATUSES and s != "review"
)


def set_run_status(db: Session, run: Run, new: str) -> bool:
    """Transition ``run.status`` to ``new``, enforcing the terminal guard.

    Args:
        db: Active session; the transition is committed here.
        run: The Run row to transition.
        new: The status to move to (one of ``RUN_STATUSES``).

    Returns:
        True if the transition was applied. False (no-op) if the run is
        already in a terminal status (``done``/``cancelled``/``failed``) —
        callers running in worker threads MUST check this and stop rather
        than continue the stage, so a cancel/failure can never be overwritten
        by a stage that was already in flight.
    """
    if run.status in TERMINAL_RUN_STATUSES:
        return False
    run.status = new
    if new in TERMINAL_RUN_STATUSES:
        run.finished_at = utcnow()
    db.add(run)
    db.commit()
    audit_service.record(
        category="run", actor_type="system", action="Run status changed",
        target=f"{run.code} · {new}",
    )
    hub.publish(str(run.id), "run.status", {"status": new})
    return True


def recover_orphaned_runs(db: Session) -> int:
    """Sweep runs left in a non-terminal "active work" status with no worker.

    Called once at API startup (ADR 0005 / ARCHITECTURE-REVIEW §4.1), after the
    process's own worker threads are known to be dead — a bare `threading.Thread`
    never survives a process restart, so any run still sitting in an in-progress
    stage (``processing``, ``sync``, ``automation``, ``executing``, ``evidence``,
    ``comment``) was abandoned mid-work by a crashed/killed/redeployed process.
    ``review`` is excluded: it is a legitimate human-gated pause, not a stuck
    worker, so it is left untouched.

    Each orphaned run is marked ``failed`` with ``failed_stage`` set to its
    abandoned status, via :func:`set_run_status` (so the normal audit row +
    ``run.status`` WS event fire exactly as any other failure would). This makes
    the run terminal and retryable through the existing ADR-0005
    ``_RETRY_RESUME_STAGE`` dispatch table.

    Args:
        db: Active session.

    Returns:
        The number of runs recovered.
    """
    orphaned = db.query(Run).filter(Run.status.in_(_ORPHANABLE_RUN_STATUSES)).all()
    for run in orphaned:
        run.failed_stage = run.status
        set_run_status(db, run, "failed")
    return len(orphaned)


#: Reason stamped on a spec whose live-authoring session did not survive a
#: restart. Deliberately actionable — the operator's next move is Regenerate.
ORPHANED_AUTHORING_REASON = (
    "Live authoring was interrupted by an API restart — nothing was authored. Regenerate to retry."
)


def reset_stuck_specs(
    db: Session,
    *,
    reason: str,
    run_id: int | None = None,
    skip_case_ids: frozenset[int] | set[int] = frozenset(),
) -> list[tuple[int, int, str, str]]:
    """Rewrite every ``AutomationSpec`` still marked ``running`` to an idle status.

    A spec sits at ``running`` only while live authoring or a self-heal is in
    flight, and nothing behind it survives the death of its worker/session — so a
    ``running`` spec with no live session is a perpetual spinner in the UI and a
    case that can never be re-triggered.

    Whatever was authored so far is kept (``-> draft``); a spec with no code at all
    becomes ``blocked`` with ``reason`` so the panel says *why* instead of
    rendering an empty state. This is the single query behind both callers — the
    per-run Stop button (#420, ``routers/runs.py``) and the boot sweep
    (:func:`recover_orphaned_authoring`, #605) — so the two can never drift.

    Args:
        db: Active session. **Not committed here** — the caller commits, so a Stop
            can batch this with the rest of its resets in one transaction.
        reason: ``block_reason`` for a spec with nothing authored (an existing
            non-empty reason is preserved).
        run_id: Restrict to one run's cases; ``None`` sweeps every run.
        skip_case_ids: Cases to leave alone — used by the boot sweep to spare a
            spec whose authoring session genuinely survived.

    Returns:
        One ``(run_id, case_id, ticket_external_id, case_code)`` tuple per spec
        reset, so callers can close the live trail on any open client.
    """
    query = (
        db.query(AutomationSpec, TestCase)
        .join(TestCase, AutomationSpec.test_case_id == TestCase.id)
        .filter(AutomationSpec.status == "running")
    )
    if run_id is not None:
        query = query.filter(TestCase.run_id == run_id)
    reset: list[tuple[int, int, str, str]] = []
    for spec, case in query.all():
        if case.id in skip_case_ids:
            continue
        if (spec.code or "").strip():
            spec.status = "draft"
        else:
            spec.status = "blocked"
            spec.block_reason = (spec.block_reason or "").strip() or reason
        db.add(spec)
        reset.append((case.run_id, case.id, case.ticket_external_id, case.code))
    return reset


def recover_orphaned_authoring(db: Session) -> tuple[int, int]:
    """Sweep live-authoring work abandoned by a prior process (#605).

    The sibling of :func:`recover_orphaned_runs`, for the layer it never touched:
    ``recover_orphaned_runs`` only looks at ``Run.status``, so a spec left at
    ``running`` by a lost authoring session stayed stuck forever — the reported
    "Regenerate is always empty and never triggers the local agent" bug. The only
    other code that reset such a spec was the per-run Stop button, which nobody
    presses for a run that already looks finished.

    Two passes, in this order:

    1. **Prune dead sessions.** Now that the queue is durable a session row can
       outlive its work (the spec was finalized, stopped or regenerated by another
       path). Such a row would hand the agent a job nobody is waiting on, so every
       session whose spec is no longer ``running`` is dropped.
    2. **Reset orphaned specs.** Every spec still ``running`` with no surviving
       session is rewritten to a re-triggerable status via
       :func:`reset_stuck_specs`, and audited so the reset is never silent.

    A spec whose session *did* survive is deliberately left ``running``: the agent
    runs on a different machine and keeps authoring across an API restart, so its
    finalize post-back must still land.

    Returns:
        ``(specs_recovered, sessions_dropped)``.
    """
    from app.services import agent_authoring_service

    session_cases = agent_authoring_service.live_case_ids(db)
    running_spec_cases = {
        case_id
        for (case_id,) in db.query(AutomationSpec.test_case_id)
        .filter(AutomationSpec.status == "running")
        .all()
    }
    dropped = agent_authoring_service.prune_dead_sessions(db, running_spec_cases)
    survivors = session_cases & running_spec_cases
    reset = reset_stuck_specs(
        db, reason=ORPHANED_AUTHORING_REASON, skip_case_ids=survivors
    )
    db.commit()
    for _run_id, _case_id, ticket, code in reset:
        audit_service.record(
            category="automation",
            actor_type="system",
            action="Recovered orphaned authoring spec",
            target=f"{ticket} · {code}",
            status="warning",
            meta=ORPHANED_AUTHORING_REASON,
        )
    if survivors:
        logger.info(
            "Kept {} live authoring session(s) across the restart (cases={})",
            len(survivors),
            sorted(survivors),
        )
    return len(reset), dropped


def force_status(db: Session, run: Run, new: str) -> None:
    """Directly set ``run.status``, bypassing the terminal guard.

    Used exclusively by the retry endpoint to intentionally move a terminal
    run back into the pipeline. Unlike :func:`set_run_status` this never
    stamps ``finished_at`` (the run is active again) but still broadcasts the
    same ``run.status`` WS event so the UI reflects the change immediately.
    """
    run.status = new
    db.add(run)
    db.commit()
    hub.publish(str(run.id), "run.status", {"status": new})


def recover_orphaned_captures(db: Session) -> tuple[int, int]:
    """Sweep Local-Agent login captures abandoned by a prior process (#625).

    The capture counterpart of :func:`recover_orphaned_authoring`. Now that the
    manual-login queue is a table
    (:class:`app.models.agent_capture.AgentCaptureRequest`) rather than one
    worker's ``list``, a capture claimed by a device that then died no longer
    disappears on restart — which is the whole point, but it means an unbounded
    claim would pin the project at "capturing…" forever where the restart used to
    clear it by accident. This sweep makes that reset explicit:

    * a ``running`` capture claimed longer ago than
      ``agent_capture_service.STALE_CLAIM_AFTER`` is **re-queued** — the
      operator's request is still legitimate, so the next poll picks it up;
    * any capture older than ``agent_capture_service.ABANDON_AFTER`` is
      **dropped** — nobody is waiting on a login prompt from half a day ago;
    * a ``running`` capture claimed *recently* is left alone: the headed browser
      is open on the operator's machine and its ``/complete`` post-back must
      still land, and re-queueing it would open a second browser.

    Returns:
        ``(requeued, dropped)``.
    """
    from app.services import agent_capture_service

    requeued, dropped = agent_capture_service.sweep_stranded(db)
    db.commit()
    return requeued, dropped
