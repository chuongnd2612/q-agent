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

import json
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

#: How long a ``paused`` session may sit before it is torn down (#619).
#:
#: A pause is expensive on the *user's* machine, not ours: the device is holding a
#: Chrome window, a temp workdir and a ``CLAUDE_CONFIG_DIR`` open so the same
#: Claude session can be resumed. A pause nobody ever continues would leak all
#: three forever, so this mirrors :data:`STALE_CLAIM_AFTER` for the paused state:
#: past it the agent is told to tear down and the session is finalized. It is
#: deliberately shorter than the stale-claim window — a human deciding what to
#: type takes minutes, not hours, and the cost of being wrong is a live browser.
PAUSE_EXPIRES_AFTER = timedelta(hours=1)

#: Statuses in which the device is actively holding resources for the session.
LIVE_ON_DEVICE = ("running", "paused", "resuming")

#: How many times one session may be resumed (#619).
#:
#: The session budget (:func:`remaining_budget`) is the primary bound, but it can
#: only subtract spend the device actually *reported*: a Claude child killed for a
#: pause may not emit its ``result`` envelope, and that pass's cost is then
#: invisible. This cap makes the worst case bounded regardless — without it a user
#: could pause/continue indefinitely and each unreported pass would be free.
MAX_RESUMES_PER_SESSION = 8

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
        # pause/resume (#619)
        "pause_requested": bool(row.pause_requested),
        "paused_at": row.paused_at,
        "claude_session_id": row.claude_session_id or "",
        "guidance": _decode_guidance(row.guidance),
        "guidance_history": _decode_guidance(row.guidance_history),
        "cost_usd_so_far": float(row.cost_usd_so_far or 0.0),
        "resume_count": int(row.resume_count or 0),
        "remaining_budget_usd": remaining_budget(row),
        **{field: getattr(row, field) for field in _PAYLOAD_FIELDS},
    }


def _decode_guidance(raw: str | None) -> list[str]:
    """Guidance column -> list of strings, tolerating an empty/corrupt column.

    Stored as a JSON array rather than newline-joined text because a guidance turn
    is free-form multi-line prose typed by a human; splitting it on newlines would
    silently shred one instruction into several.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]


def remaining_budget(row: AgentAuthoringSession) -> float:
    """Budget left for this session, across every pass so far (#619).

    The cost ceiling is a **session** budget, not a per-pass one: without this a
    user could pause/continue in a loop and spend ``max_budget_usd`` again on each
    resume, so a bounded job becomes unbounded. Never negative — a resume with
    nothing left is refused rather than handed a nonsense ``--max-budget-usd``.
    """
    ceiling = float(row.max_budget_usd or 0.0)
    spent = float(row.cost_usd_so_far or 0.0)
    return max(0.0, ceiling - spent)


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
            if existing.status in ("paused", "resuming"):
                # A paused session is NOT stranded work: the device is holding a
                # live Chrome + workdir for it and the user is mid-thought. Only a
                # pause older than the expiry window may be recycled (#619).
                paused_at = existing.paused_at or existing.claimed_at
                if paused_at is not None and (utcnow() - paused_at) <= PAUSE_EXPIRES_AFTER:
                    return
            claimed_at = existing.claimed_at
            stale = claimed_at is None or (utcnow() - claimed_at) > STALE_CLAIM_AFTER
            if existing.status == "running" and not stale:
                return
            for key, value in payload.items():
                setattr(existing, key, value)
            if existing.status in LIVE_ON_DEVICE:
                logger.warning(
                    "Re-queueing an abandoned authoring claim (session={} case={} claimed={})",
                    existing.session_id,
                    case_id,
                    claimed_at,
                )
                existing.status = "queued"
                existing.claimed_at = None
                existing.paused_at = None
                existing.pause_requested = False
                existing.claude_session_id = ""
                existing.guidance = ""
                existing.cost_usd_so_far = 0.0
                existing.resume_count = 0
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
                    {
                        "status": "running",
                        "claimed_at": utcnow(),
                        # A fresh claim starts a fresh pause/resume ledger (#619).
                        "pause_requested": False,
                        "paused_at": None,
                        "claude_session_id": "",
                        "cost_usd_so_far": 0.0,
                        "resume_count": 0,
                    },
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


def cancel_case(case_id: int, owner_id: int | None = None) -> dict | None:
    """Cancel the live authoring session for one case; ``None`` if there is none.

    Deleting the row IS the cancel, and it needs nothing from the agent (#645):

    * a ``running`` device posts a progress event per Claude step, and
      ``/agent/authoring/{id}/events`` answers 404 once the row is gone — which
      the agent already treats as "the run was stopped, abort";
    * a ``paused`` device polls :func:`take_resume_directive`, which answers
      ``abort``/``session-gone`` for a missing row, and the agent already tears
      Chrome down on any non-``resume`` directive.

    So cancel works on devices that are already deployed, rather than waiting for
    a release — which matters because the state it rescues users from (a pause
    holding a browser open) lasts an hour otherwise.

    Returns the cancelled session's dict so the caller can audit and announce it.
    Cancelling nothing is not an error: the endpoint is idempotent, because the
    user's intent ("stop this") is already satisfied.
    """
    with _session() as db:
        # Narrow by owner only when there IS one, matching `live_session_for_case`
        # and `_lookup`. `_owner_filter`'s strict `owner_id IS NULL` belongs to the
        # agent-facing queue, where the device's own identity is the key; using it
        # here would make cancel a no-op on an auth-disabled install, whose rows
        # carry a real owner while the caller is None. The ownership guarantee for
        # this path comes from `_get_case_and_run_or_404` in the router.
        query = db.query(AgentAuthoringSession).filter(
            AgentAuthoringSession.case_id == case_id
        )
        if owner_id is not None:
            query = query.filter(AgentAuthoringSession.owner_id == owner_id)
        row = query.first()
        if row is None:
            return None
        snapshot = _as_dict(row)
        db.delete(row)
        db.commit()
    logger.info(
        "Live authoring CANCELLED (session={} case={} was={})",
        snapshot["session_id"],
        case_id,
        snapshot["status"],
    )
    return snapshot


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


# --------------------------------------------------------------- pause / resume (#619)
#
# The whole protocol is columns on the queue row, never process memory (the #605 /
# #625 rule: *anything a background poller waits on must never be process memory*).
# A pause is especially unforgiving here — the device is holding a live Chrome, a
# live temp workdir and a live CLAUDE_CONFIG_DIR open for it, so a lost pause does
# not just lose a queue entry, it strands a browser window on someone's desktop.
#
# Pause is delivered on the channel the agent ALREADY polls: it posts a progress
# event once per Claude step, and that response now carries a ``control`` field.
# So pause needs no new poller and arrives within one step.


def _encode_guidance(items: list[str]) -> str:
    return json.dumps([str(i) for i in items if str(i).strip()], ensure_ascii=False)


def request_pause(session_id: str, owner_id: int | None = None) -> str:
    """Ask a live authoring session to pause at its next step.

    Returns an outcome string rather than a bool so the caller can answer the user
    precisely: ``"requested"``, ``"already-pending"``, ``"already-paused"``,
    ``"not-running"`` (queued — nothing is on a device yet, so there is nothing to
    pause) or ``"not-found"``. Pausing something that is not authoring is a clean
    no-op by construction: there is no row to flag.
    """
    with _session() as db:
        row = _lookup(db, session_id, owner_id)
        if row is None:
            return "not-found"
        if row.status in ("paused", "resuming"):
            return "already-paused"
        if row.status != "running":
            return "not-running"
        if row.pause_requested:
            return "already-pending"
        row.pause_requested = True
        db.add(row)
        db.commit()
        logger.info("Pause requested for live authoring (session={} case={})", session_id, row.case_id)
        return "requested"


def _lookup(db: Session, session_id: str, owner_id: int | None) -> AgentAuthoringSession | None:
    query = db.query(AgentAuthoringSession).filter(
        AgentAuthoringSession.session_id == session_id
    )
    if owner_id is not None:
        query = query.filter(AgentAuthoringSession.owner_id == owner_id)
    return query.first()


def pending_control(session_id: str, owner_id: int | None = None) -> str:
    """The control directive to piggyback on the agent's next progress-event reply.

    ``"pause"`` while a pause is pending on a ``running`` session, else ``""``.
    """
    with _session() as db:
        row = _lookup(db, session_id, owner_id)
        if row is None:
            return ""
        return "pause" if (row.pause_requested and row.status == "running") else ""


def mark_paused(
    session_id: str,
    *,
    owner_id: int | None = None,
    claude_session_id: str = "",
    cost_usd: float = 0.0,
) -> dict | None:
    """Record that the device really stopped Claude and parked the session.

    ``claude_session_id`` is Claude CLI's OWN id, scraped off the ``stream-json``
    envelope on the device — the one thing ``claude --resume`` needs and the one
    thing nothing used to capture. It is stored even when empty, because an empty
    value is itself the signal that Continue must take the fallback path.

    ``cost_usd`` is the session's spend so far and is stored **absolutely**, not
    added: the agent tracks the running total across its own passes, so a retried
    post cannot double-count the budget.
    """
    with _session() as db:
        row = _lookup(db, session_id, owner_id)
        if row is None:
            return None
        row.status = "paused"
        row.pause_requested = False
        row.paused_at = utcnow()
        if claude_session_id:
            row.claude_session_id = claude_session_id[:120]
        row.cost_usd_so_far = max(float(row.cost_usd_so_far or 0.0), float(cost_usd or 0.0))
        db.add(row)
        db.commit()
        db.refresh(row)
        logger.info(
            "Live authoring PAUSED (session={} case={} claudeSession={} spent=${:.4f} left=${:.4f})",
            session_id,
            row.case_id,
            row.claude_session_id or "(none captured)",
            row.cost_usd_so_far,
            remaining_budget(row),
        )
        return _as_dict(row)


def add_guidance(session_id: str, text: str, owner_id: int | None = None) -> dict | None:
    """Append one guidance turn typed by the user; ``None`` if there is no session.

    Kept in two columns: ``guidance`` is the undelivered queue (cleared once a
    resume hands it over) and ``guidance_history`` is append-only. The history
    exists for the FALLBACK path — a fresh Claude pass has no memory of earlier
    turns, so it must carry every one of them, while a true ``--resume`` only needs
    the new turn.
    """
    text = (text or "").strip()
    if not text:
        return None
    with _session() as db:
        row = _lookup(db, session_id, owner_id)
        if row is None:
            return None
        row.guidance = _encode_guidance([*_decode_guidance(row.guidance), text])
        row.guidance_history = _encode_guidance([*_decode_guidance(row.guidance_history), text])
        db.add(row)
        db.commit()
        db.refresh(row)
        return _as_dict(row)


def live_session_for_case(case_id: int, owner_id: int | None = None) -> dict | None:
    """The live (queued/running/paused/resuming) authoring session for a case, if any.

    Used to route a spec-chat message to a paused authoring session instead of the
    spec-edit prompt (#619), and to tell the UI whether Pause/Continue apply.
    """
    with _session() as db:
        query = db.query(AgentAuthoringSession).filter(
            AgentAuthoringSession.case_id == case_id
        )
        if owner_id is not None:
            query = query.filter(AgentAuthoringSession.owner_id == owner_id)
        row = query.first()
        return _as_dict(row) if row is not None else None


def request_resume(session_id: str, *, owner_id: int | None = None, guidance: str = "") -> str:
    """The user's Continue: bank any guidance and flip ``paused`` -> ``resuming``.

    The flip is a conditional UPDATE guarded on ``status = 'paused'``, so two
    Continue clicks (or two API workers) cannot both resume one session. Returns
    ``"resuming"``, ``"not-paused"``, ``"expired"``, ``"budget-exhausted"`` or
    ``"not-found"``.
    """
    with _session() as db:
        row = _lookup(db, session_id, owner_id)
        if row is None:
            return "not-found"
        if row.status != "paused":
            return "not-paused"
        if _pause_expired(row):
            return "expired"
        if remaining_budget(row) <= 0:
            return "budget-exhausted"
        if int(row.resume_count or 0) >= MAX_RESUMES_PER_SESSION:
            return "resume-limit"
        text = (guidance or "").strip()
        if text:
            row.guidance = _encode_guidance([*_decode_guidance(row.guidance), text])
            row.guidance_history = _encode_guidance(
                [*_decode_guidance(row.guidance_history), text]
            )
            db.add(row)
            db.commit()
        flipped = (
            db.query(AgentAuthoringSession)
            .filter(
                AgentAuthoringSession.id == row.id,
                AgentAuthoringSession.status == "paused",
            )
            .update({"status": "resuming"}, synchronize_session=False)
        )
        db.commit()
        if flipped != 1:
            return "not-paused"
        logger.info("Continue requested for live authoring (session={} case={})", session_id, row.case_id)
        return "resuming"


def _pause_expired(row: AgentAuthoringSession) -> bool:
    since = row.paused_at or row.claimed_at
    if since is None:
        return False
    return (utcnow() - since) > PAUSE_EXPIRES_AFTER


def take_resume_directive(session_id: str, owner_id: int | None = None) -> dict:
    """What the parked device should do next — the agent polls this while paused.

    Exactly one of:

    * ``{"action": "wait"}`` — still paused; keep Chrome and the workdir alive.
    * ``{"action": "resume", ...}`` — the user pressed Continue. Carries the
      guidance turns (consumed here, so they are handed over exactly once), the
      full accumulated history for the fallback path, Claude's own session id, and
      the budget REMAINING for the whole session.
    * ``{"action": "abort", "reason": ...}`` — tear everything down and finalize:
      the pause expired, the session was stopped/purged server-side, or the
      session budget is spent.

    Consuming the guidance inside the same transaction that flips ``resuming`` ->
    ``running`` is what makes a duplicated poll safe: the second one sees
    ``running`` and gets ``abort``/``wait`` rather than replaying the turn.
    """
    with _session() as db:
        row = _lookup(db, session_id, owner_id)
        if row is None:
            return {"action": "abort", "reason": "session-gone"}
        if row.status == "paused":
            if _pause_expired(row):
                logger.warning(
                    "Pause EXPIRED after {} (session={} case={}) — telling the device to tear down",
                    PAUSE_EXPIRES_AFTER,
                    session_id,
                    row.case_id,
                )
                return {"action": "abort", "reason": "pause-expired"}
            return {"action": "wait"}
        if row.status != "resuming":
            # Something reset the session under us (stop, re-queue, restart sweep).
            return {"action": "abort", "reason": f"status-{row.status}"}
        if int(row.resume_count or 0) >= MAX_RESUMES_PER_SESSION:
            logger.warning(
                "Refusing to resume live authoring past the resume cap "
                "(session={} case={} resumes={})",
                session_id,
                row.case_id,
                row.resume_count,
            )
            return {"action": "abort", "reason": "resume-limit"}
        remaining = remaining_budget(row)
        if remaining <= 0:
            logger.warning(
                "Refusing to resume live authoring with no budget left "
                "(session={} case={} ceiling=${:.4f} spent=${:.4f})",
                session_id,
                row.case_id,
                float(row.max_budget_usd or 0.0),
                float(row.cost_usd_so_far or 0.0),
            )
            return {"action": "abort", "reason": "budget-exhausted"}
        guidance = _decode_guidance(row.guidance)
        history = _decode_guidance(row.guidance_history)
        claimed = (
            db.query(AgentAuthoringSession)
            .filter(
                AgentAuthoringSession.id == row.id,
                AgentAuthoringSession.status == "resuming",
            )
            .update(
                {
                    "status": "running",
                    "guidance": "",
                    "paused_at": None,
                    "pause_requested": False,
                    "resume_count": int(row.resume_count or 0) + 1,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        if claimed != 1:
            return {"action": "wait"}
        return {
            "action": "resume",
            "guidance": guidance,
            "guidanceHistory": history,
            "claudeSessionId": row.claude_session_id or "",
            "remainingBudgetUsd": remaining,
            "resumeCount": int(row.resume_count or 0) + 1,
        }


def expire_stale_pauses(db: Session) -> list[tuple[int | None, int, str]]:
    """Drop paused sessions whose pause window elapsed; return what was dropped.

    The device-side counterpart is :func:`take_resume_directive` returning
    ``abort`` — that is the normal path, and the only one that can actually close
    Chrome. This is the server's backstop for the case where the device never
    comes back (unpaired, powered off, agent killed), so the row does not sit in
    the queue forever blocking the case's UNIQUE slot. Does **not** commit — the
    caller batches it with the rest of its sweep.

    Returns ``(run_id, case_id, session_id)`` per expired row.
    """
    dropped: list[tuple[int | None, int, str]] = []
    for row in db.query(AgentAuthoringSession).filter(
        AgentAuthoringSession.status.in_(("paused", "resuming"))
    ):
        if not _pause_expired(row):
            continue
        logger.warning(
            "Expiring a forgotten authoring pause (session={} run={} case={} paused_at={})",
            row.session_id,
            row.run_id,
            row.case_id,
            row.paused_at,
        )
        dropped.append((row.run_id, row.case_id, row.session_id))
        db.delete(row)
    if dropped:
        # FLUSH, not commit. The sweep's very next step asks which cases still have
        # a live session, and the session it hands us has autoflush off — so
        # without this the rows we just deleted still answer that question and the
        # expired pause's spec is "kept" instead of being reset. Committing here
        # instead would break the caller's single-transaction sweep.
        db.flush()
    return dropped
