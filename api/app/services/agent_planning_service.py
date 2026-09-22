"""Durable queue for agent-driven live-PLANNING sessions (#900).

When ``testCaseMode="live-planner"`` meets ``executionTarget="local-agent"``,
:func:`app.services.planner_agent_service.plan_ticket` enqueues one session here
instead of launching Chrome on the API host. The paired device claims it
(``POST /agent/planning/next``), runs the ``playwright-test-planner``
methodology against the real app with its own captured login, and posts the plan
sidecar back (``POST /agent/planning/{id}/finalize``).

**The one way this differs from capture / exploration / authoring.** Those three
enqueue and return: the UI polls, or the device finalizes into a row read later.
The planner cannot, because its result is consumed *inline* by the very next
prompt in the same generation pass (``ai_service._process_run_ticket``). So this
module also owns the wait: :func:`await_result` blocks the calling worker thread
on the session **row** until the device finishes or a deadline passes.

Two things make that safe rather than reckless:

* ``_process_run_ticket`` already runs in a background worker thread, so
  blocking there stalls one ticket's generation, not a request.
* Every deadline degrades to ``None``, and ``plan_ticket``'s caller treats
  ``None`` as "plan unavailable → generate from text", which is exactly what a
  missing sidecar already meant. A dead, unpaired or slow device can therefore
  never fail a run — only make it plan blind, loudly.

The wait polls the row through its own short-lived session at
:data:`POLL_INTERVAL`, deliberately NOT by calling an HTTP endpoint in a loop:
the test client shares one session with the test and hammering it starves the
very pass being awaited (a wait that slows the thing it measures — see
CLAUDE.md / #641).

**Multi-worker safety** is the reason this is a table rather than a module-level
list like ``agent_explore_service``: the enqueue and the device's claim are
served by (potentially) different API workers, and with an in-memory queue the
waiter would simply never see the result. :func:`claim_next` claims with a
conditional ``UPDATE … WHERE id = ? AND status = 'queued'`` and checks the
rowcount, so two workers racing cannot both win.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

from sqlalchemy.orm import Session

from app import db as db_module
from app.db import utcnow
from app.logging import logger
from app.models.agent_planning import AgentPlanningSession

#: How long a queued session may wait for ANY device to claim it.
#:
#: Deliberately much shorter than the overall deadline: a paired-but-not-running
#: agent is the common failure (the row is checked for a paired *device*, which
#: says nothing about whether that device's process is polling), and there is no
#: reason to stall a generation pass for ten minutes to learn something the
#: first two idle poll cycles already prove. The agent polls every ~1s when
#: idle, so this is ~90 missed polls before we give up.
CLAIM_DEADLINE = timedelta(seconds=90)

#: How long the whole session may take, measured from the enqueue, before the
#: server stops waiting. The reference planner run costs ~$0.27 and takes a few
#: minutes; this is generous enough to absorb a slow app while still bounding
#: the pass.
RUN_DEADLINE = timedelta(minutes=12)

#: Gap between row reads while waiting. Seconds, not milliseconds — a planning
#: session lasts minutes, and a tight loop here would contend with the device's
#: finalize write on the same SQLite file.
POLL_INTERVAL = 3.0

#: Payload fields handed to the device (everything except queue bookkeeping).
_PAYLOAD_FIELDS = (
    "project_key",
    "repo",
    "base_url",
    "origin",
    "run_code",
    "ticket",
    "sidecar_filename",
    "system_prompt",
    "task_prompt",
    "model",
    "max_budget_usd",
    "log_verbosity",
)


@contextmanager
def _session() -> Iterator[Session]:
    """Short-lived own session, like :func:`audit_service.record` uses.

    Resolved through ``db_module`` on every call (not a module-level ``from
    app.db import SessionLocal``) so the test fixture's redirect at a per-test
    temp database is honoured here too.
    """
    db = db_module.SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _owner_filter(query, owner_id: int | None):  # noqa: ANN001, ANN201
    """Filter on ``owner_id``, treating ``None`` as SQL NULL (auth-disabled installs)."""
    if owner_id is None:
        return query.filter(AgentPlanningSession.owner_id.is_(None))
    return query.filter(AgentPlanningSession.owner_id == owner_id)


def _as_dict(row: AgentPlanningSession) -> dict:
    """Render a row as a plain dict for the endpoints and the waiter."""
    payload = {field: getattr(row, field) for field in _PAYLOAD_FIELDS}
    payload.update(
        {
            "session_id": row.session_id,
            "owner_id": row.owner_id,
            "run_id": row.run_id,
            "status": row.status,
            "plan_json": row.plan_json,
            "summary": row.summary,
            "cost_usd": row.cost_usd,
        }
    )
    return payload


def has_paired_device(owner_id: int | None) -> bool:
    """Is a non-revoked Local Agent device paired for this owner?

    The other dispatches make this check inline in a request handler, where a
    ``db`` is already in hand. Planning is dispatched from a background worker
    thread with no session, so it lives here alongside the queue that needs it.

    A paired device is necessary but not sufficient — it says nothing about
    whether that device's process is actually polling, which is what
    :data:`CLAIM_DEADLINE` covers.
    """
    from app.models.agent_device import AgentDevice

    with _session() as db:
        query = db.query(AgentDevice).filter(AgentDevice.revoked_at.is_(None))
        if owner_id is None:
            query = query.filter(AgentDevice.owner_id.is_(None))
        else:
            query = query.filter(AgentDevice.owner_id == owner_id)
        return query.first() is not None


def request_planning(
    session_id: str,
    *,
    owner_id: int | None,
    run_id: int | None,
    project_key: str,
    repo: str,
    base_url: str,
    origin: str,
    run_code: str,
    ticket: str,
    sidecar_filename: str,
    system_prompt: str,
    task_prompt: str,
    model: str,
    max_budget_usd: float,
    log_verbosity: str,
) -> None:
    """Queue one planning session for the owner's paired device.

    Args:
        session_id: Caller-generated id (a uuid hex); the wire handle.
        owner_id: The run owner, so only their device can claim it.
        run_id: The run being generated — progress events relay to its WebSocket.
        project_key, repo: Project scope, for the device's session/profile lookup.
        base_url, origin: The app under test; ``origin`` keys the device's
            captured login.
        run_code, ticket: Labels for logs and for superseding a stale attempt.
        sidecar_filename: The JSON file the plan must be written to.
        system_prompt: The planner agent definition's body (the methodology).
        task_prompt: The per-ticket planning instructions.
        model, max_budget_usd, log_verbosity: Claude run parameters.

    Any earlier non-terminal session for the same owner/run/ticket is expired
    first: re-planning the same ticket means the previous attempt is over by
    definition, and leaving it queued would let a device claim it later and open
    a browser for a plan nobody is waiting for.
    """
    with _session() as db:
        stale = _owner_filter(
            db.query(AgentPlanningSession).filter(
                AgentPlanningSession.run_code == run_code,
                AgentPlanningSession.ticket == ticket,
                AgentPlanningSession.status.in_(("queued", "running")),
            ),
            owner_id,
        ).all()
        for row in stale:
            row.status = "expired"
            row.finished_at = utcnow()
            row.summary = "Superseded by a newer planning attempt for the same ticket."
            logger.warning(
                "Superseding stale planning session {} for {} ({})", row.session_id, ticket, run_code
            )
        db.add(
            AgentPlanningSession(
                session_id=session_id,
                owner_id=owner_id,
                run_id=run_id,
                project_key=project_key,
                repo=repo,
                base_url=base_url,
                origin=origin,
                run_code=run_code,
                ticket=ticket,
                sidecar_filename=sidecar_filename,
                system_prompt=system_prompt,
                task_prompt=task_prompt,
                model=model,
                max_budget_usd=max_budget_usd,
                log_verbosity=log_verbosity,
                status="queued",
            )
        )
        db.commit()


def claim_next(owner_id: int | None) -> dict | None:
    """Claim the oldest queued session for ``owner_id``, or ``None``.

    Race-safe across API workers: the claim is a conditional UPDATE checked by
    rowcount, so a session lost to another worker is retried rather than
    double-claimed.
    """
    with _session() as db:
        while True:
            row = (
                _owner_filter(
                    db.query(AgentPlanningSession).filter(AgentPlanningSession.status == "queued"),
                    owner_id,
                )
                .order_by(AgentPlanningSession.id.asc())
                .first()
            )
            if row is None:
                return None
            claimed = (
                db.query(AgentPlanningSession)
                .filter(AgentPlanningSession.id == row.id, AgentPlanningSession.status == "queued")
                .update({"status": "running", "claimed_at": utcnow()}, synchronize_session=False)
            )
            db.commit()
            if claimed != 1:
                db.expire_all()
                continue
            db.refresh(row)
            return _as_dict(row)


def get_session(session_id: str, owner_id: int | None = None) -> dict | None:
    """Look up one session, optionally scoped to an owner (the endpoints' guard)."""
    with _session() as db:
        query = db.query(AgentPlanningSession).filter(AgentPlanningSession.session_id == session_id)
        if owner_id is not None:
            query = _owner_filter(query, owner_id)
        row = query.first()
        return _as_dict(row) if row else None


def set_result(
    session_id: str,
    *,
    plan_json: str,
    summary: str,
    ok: bool,
    cost_usd: float = 0.0,
) -> None:
    """Record the device's terminal outcome. Unknown ids are a logged no-op."""
    with _session() as db:
        row = (
            db.query(AgentPlanningSession)
            .filter(AgentPlanningSession.session_id == session_id)
            .first()
        )
        if row is None:
            logger.warning("Planning finalize for unknown session {}", session_id)
            return
        row.status = "done" if ok else "failed"
        row.plan_json = plan_json or ""
        row.summary = (summary or "")[:800]
        row.cost_usd = cost_usd or 0.0
        row.finished_at = utcnow()
        db.commit()


def _expire(session_id: str, reason: str) -> None:
    """Mark a session expired so a late claim cannot open a pointless browser."""
    with _session() as db:
        row = (
            db.query(AgentPlanningSession)
            .filter(
                AgentPlanningSession.session_id == session_id,
                AgentPlanningSession.status.in_(("queued", "running")),
            )
            .first()
        )
        if row is None:
            return
        row.status = "expired"
        row.summary = reason
        row.finished_at = utcnow()
        db.commit()


def await_result(
    session_id: str,
    *,
    claim_deadline: timedelta | None = None,
    run_deadline: timedelta | None = None,
    poll_interval: float | None = None,
) -> dict | None:
    """Block until the device finishes this session, or a deadline passes.

    Args:
        session_id: The session to wait for.
        claim_deadline: How long to wait for a device to claim it at all.
            Defaults to :data:`CLAIM_DEADLINE`.
        run_deadline: Overall wait, measured from the enqueue (so a late
            claim eats into it). Defaults to :data:`RUN_DEADLINE`.
        poll_interval: Seconds between row reads. Defaults to
            :data:`POLL_INTERVAL`.

    The three windows read their module constants at CALL time rather than
    binding them as default arguments, so a test can shrink them (the deadline
    paths are the ones most worth testing, and they are untestable at 90s/12min).

    Returns:
        The terminal session dict (``status`` ``"done"`` or ``"failed"``), or
        ``None`` when nothing claimed it in time, the device overran, or the row
        vanished. Every ``None`` path expires the row and logs why, so a device
        that wakes up late does not start planning for a discarded pass.

    Never raises: the caller's contract is "a plan, or fall back to text-only".
    """
    started = time.monotonic()
    claim_seconds = (claim_deadline or CLAIM_DEADLINE).total_seconds()
    run_seconds = (run_deadline or RUN_DEADLINE).total_seconds()
    gap = poll_interval if poll_interval is not None else POLL_INTERVAL
    while True:
        row = get_session(session_id)
        if row is None:
            logger.warning("Planning session {} disappeared while waiting", session_id)
            return None
        if row["status"] in ("done", "failed"):
            return row
        if row["status"] == "expired":
            logger.warning("Planning session {} was expired while waiting", session_id)
            return None
        waited = time.monotonic() - started
        if row["status"] == "queued" and waited > claim_seconds:
            _expire(session_id, "No Local Agent claimed the planning session in time.")
            logger.warning(
                "Planning session {} was never claimed within {}s — no agent is polling",
                session_id,
                int(claim_seconds),
            )
            return None
        if waited > run_seconds:
            _expire(session_id, "The Local Agent did not finish planning in time.")
            logger.warning(
                "Planning session {} exceeded the {}s device deadline", session_id, int(run_seconds)
            )
            return None
        time.sleep(gap)


def purge_run(run_id: int | None) -> int:
    """Expire every non-terminal session for a run (called when a run is stopped).

    Returns the number of sessions expired.
    """
    if run_id is None:
        return 0
    with _session() as db:
        rows = (
            db.query(AgentPlanningSession)
            .filter(
                AgentPlanningSession.run_id == run_id,
                AgentPlanningSession.status.in_(("queued", "running")),
            )
            .all()
        )
        for row in rows:
            row.status = "expired"
            row.summary = "Run stopped."
            row.finished_at = utcnow()
        db.commit()
        return len(rows)
