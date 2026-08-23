"""Durable queue for agent-driven live-authoring sessions (#403, made durable in #605).

Mirrors :mod:`agent_explore_service` in shape: when authoring mode is
``live-harness`` and the execution target is the paired Local Agent, generation
enqueues one authoring session per case here. The agent claims it
(``POST /agent/authoring/next``), runs ``claude`` + ``browser-harness`` locally to
author the spec, and posts the result to
``POST /agent/authoring/{id}/finalize`` — which persists the spec via the shared
gate/write path.

**State is persisted** in the ``agent_authoring_sessions`` table
(:class:`app.models.agent_authoring.AgentAuthoringSession`), not in process
memory. That was the #605 bug: the queue was a module-level ``list``, so

* any API restart lost every queued session while the ``AutomationSpec`` row had
  already been committed at ``status="running"`` with empty ``code`` — nothing
  recovered those rows, so the spec hung at "authoring…" forever and the agent
  correctly had nothing to claim; and
* the queue was outright wrong with more than one API worker — a session queued
  in worker A's memory is invisible to a claim served by worker B.

**Multi-worker safety.** Every function here reads and writes the shared table on
its own short-lived session, so any number of API workers (and processes) see the
same queue. :func:`claim_next` claims with a conditional
``UPDATE … WHERE id = ? AND status = 'queued'`` and checks the affected row count,
so two workers racing for the same session cannot both win.
:func:`request_authoring` relies on the ``case_id`` UNIQUE index (the #419 guard)
rather than a process-local lock, so a concurrent duplicate enqueue is rejected by
the database.

**Restart semantics.** ``queued`` sessions survive a restart and are simply
claimed afterwards. A ``running`` session also survives — the agent lives on a
different machine and keeps authoring across an API restart, so its finalize
post-back must still resolve. What *cannot* be recovered is a spec left
``running`` with **no** session row at all; ``run_status.recover_orphaned_authoring``
sweeps those at boot (see :mod:`app.main`).

Terminal outcomes are not kept here. The pre-#605 module held a
``_results: dict[tuple[project_key, repo], dict]``, keyed by project/repo rather
than by case — which would have collided across cases of the same project — but
its only readers (``get_result_for`` / ``is_in_flight``) had no call sites
anywhere in the API, the app or the tests: it was dead code, so the mis-keying was
never observable. It is removed rather than re-keyed; :func:`set_result` logs and
audits the outcome and drops the row.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import db as db_module
from app.db import utcnow
from app.logging import logger
from app.models.agent_authoring import AgentAuthoringSession

#: A ``running`` session whose claim is older than this is treated as abandoned
#: (the device died, was unpaired, or crashed) and re-queued by the next
#: :func:`request_authoring` for the same case. Before #605 a restart implicitly
#: cleared such a claim, because the queue lived in memory; now that the queue is
#: durable an unbounded claim would wedge the case forever, so the staleness
#: window replaces that accidental reset with an explicit one.
STALE_CLAIM_AFTER = timedelta(hours=3)

#: Payload fields handed to the agent (everything except queue bookkeeping).
_PAYLOAD_FIELDS = (
    "project_key",
    "repo",
    "base_url",
    "origin",
    "spec_filename",
    "system_prompt",
    "task_prompt",
    "model",
    "max_budget_usd",
    "log_verbosity",
)


@contextmanager
def _session() -> Iterator[Session]:
    """Short-lived own session, like :func:`audit_service.record` uses.

    Keeping the session internal means no call site had to grow a ``db``
    parameter when the queue moved from memory to the database (#605).
    """
    db = db_module.SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _owner_filter(query, owner_id: int | None):  # noqa: ANN001, ANN201
    """Filter on ``owner_id``, treating ``None`` as SQL NULL (auth-disabled installs)."""
    if owner_id is None:
        return query.filter(AgentAuthoringSession.owner_id.is_(None))
    return query.filter(AgentAuthoringSession.owner_id == owner_id)


def _as_dict(row: AgentAuthoringSession) -> dict:
    """Render a row in the shape the pre-#605 in-memory dict had."""
    return {
        "session_id": row.session_id,
        "owner_id": row.owner_id,
        "case_id": row.case_id,
        "run_id": row.run_id,
        "status": row.status,
        **{field: getattr(row, field) for field in _PAYLOAD_FIELDS},
    }


def request_authoring(
    session_id: str,
    *,
    owner_id: int | None,
    project_key: str,
    repo: str,
    base_url: str,
    origin: str,
    case_id: int,
    run_id: int | None,
    spec_filename: str,
    system_prompt: str,
    task_prompt: str,
    model: str,
    max_budget_usd: float,
    log_verbosity: str = "concise",
) -> None:
    """Enqueue one authoring session for the paired agent to claim.

    Idempotent per case (#419): at most one live session exists per ``case_id``,
    enforced by that column's UNIQUE index, so a stale session left behind by an
    earlier generate pass can never be claimed twice and re-author a case that
    already has a spec. When a live session for the case already exists:

    * ``queued`` — its payload is refreshed in place, so the agent runs the
      newest prompt (e.g. a regenerate carrying a fresh reviewer comment) instead
      of a stale one.
    * ``running`` and claimed recently — left alone; the device is authoring it.
    * ``running`` but claimed longer ago than :data:`STALE_CLAIM_AFTER` — the
      device is gone, so the session is re-queued with the fresh payload.
    """
    payload = {
        "project_key": project_key,
        "repo": repo,
        "base_url": base_url,
        "origin": origin,
        "spec_filename": spec_filename,
        "system_prompt": system_prompt,
        "task_prompt": task_prompt,
        "model": model,
        "max_budget_usd": max_budget_usd,
        "log_verbosity": log_verbosity,
    }
    with _session() as db:
        existing = (
            db.query(AgentAuthoringSession)
            .filter(AgentAuthoringSession.case_id == case_id)
            .first()
        )
        if existing is not None:
            claimed_at = existing.claimed_at
            stale = claimed_at is None or (utcnow() - claimed_at) > STALE_CLAIM_AFTER
            if existing.status == "running" and not stale:
                return
            for key, value in payload.items():
                setattr(existing, key, value)
            if existing.status == "running":
                logger.warning(
                    "Re-queueing an abandoned authoring claim (session={} case={} claimed={})",
                    existing.session_id,
                    case_id,
                    claimed_at,
                )
                existing.status = "queued"
                existing.claimed_at = None
            db.add(existing)
            db.commit()
            return

        row = AgentAuthoringSession(
            session_id=session_id,
            owner_id=owner_id,
            case_id=case_id,
            run_id=run_id,
            status="queued",
            **payload,
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            # Another worker enqueued the same case between the read and the
            # insert — the case_id UNIQUE index is the arbiter (#419).
            db.rollback()
            return
        logger.info(
            "Queued live authoring (session={} run={} case={} owner={})",
            session_id,
            run_id,
            case_id,
            owner_id,
        )


def claim_next(owner_id: int | None) -> dict | None:
    """Claim the oldest queued session for ``owner_id``; flip it to running.

    The flip is a conditional UPDATE guarded on ``status = 'queued'`` and checked
    by row count, so two API workers polling concurrently cannot both claim the
    same session; the loser simply moves on to the next queued row.
    """
    with _session() as db:
        while True:
            row = _owner_filter(
                db.query(AgentAuthoringSession).filter(
                    AgentAuthoringSession.status == "queued"
                ),
                owner_id,
            ).order_by(AgentAuthoringSession.id).first()
            if row is None:
                return None
            claimed = (
                db.query(AgentAuthoringSession)
                .filter(
                    AgentAuthoringSession.id == row.id,
                    AgentAuthoringSession.status == "queued",
                )
                .update(
                    {"status": "running", "claimed_at": utcnow()},
                    synchronize_session=False,
                )
            )
            db.commit()
            if claimed != 1:
                # Lost the race to another worker; look at the next queued row.
                db.expire_all()
                continue
            db.refresh(row)
            return _as_dict(row)


def get_session(session_id: str, owner_id: int | None = None) -> dict | None:
    """Return a tracked session as a dict, optionally scoped to ``owner_id``."""
    with _session() as db:
        query = db.query(AgentAuthoringSession).filter(
            AgentAuthoringSession.session_id == session_id
        )
        if owner_id is not None:
            query = query.filter(AgentAuthoringSession.owner_id == owner_id)
        row = query.first()
        return _as_dict(row) if row is not None else None


def set_result(session_id: str, result: dict) -> None:
    """Record a session's terminal outcome and drop it from the queue.

    The queue table holds live work only, so the row is deleted. The outcome is
    written to the log (and, for a failure, the audit trail) instead of into a
    process-local dict — the pre-#605 ``_results`` map was both process-local and
    keyed by ``(project_key, repo)`` rather than by case, and had no readers.
    """
    with _session() as db:
        row = (
            db.query(AgentAuthoringSession)
            .filter(AgentAuthoringSession.session_id == session_id)
            .first()
        )
        if row is None:
            return
        run_id, case_id = row.run_id, row.case_id
        db.delete(row)
        db.commit()
    status = str(result.get("status") or "")
    log = logger.warning if status != "done" else logger.info
    log(
        "Live authoring finished (session={} run={} case={}): status={} summary={}",
        session_id,
        run_id,
        case_id,
        status or "(none)",
        str(result.get("summary") or "")[:400],
    )


def drop_queued_cases(case_ids: set[int]) -> int:
    """Remove not-yet-claimed (``queued``) sessions for the given cases (#419).

    A ``running`` session (already being authored on the agent) is left alone so
    an in-flight job still finalizes. Used before a fresh incremental generate to
    evict stale sessions for cases that now already have a spec.
    """
    if not case_ids:
        return 0
    with _session() as db:
        removed = (
            db.query(AgentAuthoringSession)
            .filter(
                AgentAuthoringSession.case_id.in_(case_ids),
                AgentAuthoringSession.status == "queued",
            )
            .delete(synchronize_session=False)
        )
        db.commit()
        return int(removed or 0)


def purge_run(run_id: int) -> int:
    """Drop every pending authoring session for a run and return how many were removed.

    Called when a run is cancelled/stopped (#420) and defensively before a fresh
    generate pass (#419), so an unclaimed session can never be picked up later and
    re-author a case that is no longer in flight.
    """
    with _session() as db:
        removed = (
            db.query(AgentAuthoringSession)
            .filter(AgentAuthoringSession.run_id == run_id)
            .delete(synchronize_session=False)
        )
        db.commit()
        return int(removed or 0)


def live_case_ids(db: Session) -> set[int]:
    """Case ids with a live (queued/running) authoring session.

    Used by the boot sweep to tell a spec that is legitimately mid-authoring from
    one whose session was lost (#605). Takes the caller's session so the read is
    consistent with the sweep's own transaction.
    """
    return {case_id for (case_id,) in db.query(AgentAuthoringSession.case_id).all()}


def prune_dead_sessions(db: Session, live_case_ids_with_running_spec: set[int]) -> int:
    """Delete sessions that can never legitimately be claimed any more (#605).

    A durable queue can outlive the work it describes: a spec may have been
    finalized, stopped or regenerated by another path, leaving a session row that
    would hand the agent a job for a case nobody is waiting on. Called from the
    boot sweep with the set of case ids whose spec is still ``running``; every
    other session is dropped. Does not commit — the caller does.
    """
    rows = db.query(AgentAuthoringSession).all()
    dropped = 0
    for row in rows:
        if row.case_id in live_case_ids_with_running_spec:
            continue
        logger.warning(
            "Dropping an authoring session with no spec awaiting it "
            "(session={} run={} case={} status={})",
            row.session_id,
            row.run_id,
            row.case_id,
            row.status,
        )
        db.delete(row)
        dropped += 1
    return dropped
