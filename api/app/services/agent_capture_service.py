"""Standalone Local-Agent manual-login capture jobs (durable since #625).

In Local Agent mode the "Capture login now" browser can't open on the (headless)
server — it must open on the operator's OWN machine. This module queues a
capture request that the paired agent claims (``POST /agent/auth/next``), runs a
headed login capture locally, saving the session on THAT machine (never
uploaded), and reports back (``POST /agent/auth/{id}/complete``).

**State is persisted** in the ``agent_capture_requests`` table
(:class:`app.models.agent_capture.AgentCaptureRequest`), not in process memory.
That was the #625 bug — the same shape #605 closed one service over in
:mod:`agent_authoring_service`. The old module comment claimed the loss was
harmless ("the operator simply clicks again"), but it was not:

* an API restart silently dropped the queued capture, and the agent then polled
  forever for 204 — which reads as *"the agent isn't connected"*, not as *"your
  capture was lost, click again"*; and
* with more than one API worker the queue was simply wrong, losing captures with
  no restart at all: worker A's memory is invisible to a claim served by
  worker B.

And it is on the critical path: live authoring **requires** a pre-authenticated
``browser-profile`` per origin (the agent bails without one — #618), and this
capture is the only thing that creates it. A dropped capture therefore presents
as "authoring is broken".

The persistent "captured at" marker (so the UI can show a *completed* capture
across restarts) still lives in the project config's ``extra`` — see
``routers/agent.complete_auth_capture``. This table holds only work that is
still *in flight*.

**Multi-worker safety.** Every function reads and writes the shared table on its
own short-lived session (the ``audit_service.record`` pattern), so no call site
had to grow a ``db`` parameter. :func:`claim_next` claims with a conditional
``UPDATE … WHERE id = ? AND status = 'queued'`` and checks the affected row
count, so two workers polling concurrently cannot both hand out the same
capture. :func:`request_capture` leans on the ``dedupe_key`` UNIQUE index for
"one live capture per (owner, project_key)" rather than on :func:`is_capturing`
reading one process's list.

**Restart semantics.** A ``queued`` capture survives and is simply claimed
afterwards. A ``running`` capture claimed recently also survives: the headed
browser is open on the operator's machine and its ``/complete`` post-back must
still land — re-queueing it would open a *second* browser. What cannot survive
is an unbounded claim, which before #625 a restart cleared by accident; see
:data:`STALE_CLAIM_AFTER` / :data:`ABANDON_AFTER` and
``run_status.recover_orphaned_captures``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import db as db_module
from app.db import utcnow
from app.logging import logger
from app.models.agent_capture import AgentCaptureRequest, dedupe_key_for

#: A ``running`` capture claimed longer ago than this is treated as abandoned:
#: the device died, was unpaired, or the operator closed the headed browser
#: without finishing. A manual login is interactive and takes seconds to a few
#: minutes, so this is deliberately much shorter than the authoring queue's 3h
#: window (#605). Before #625 an API restart cleared such a claim by accident,
#: because the queue lived in memory; now the reset is explicit.
STALE_CLAIM_AFTER = timedelta(minutes=30)

#: A capture older than this is abandoned outright, whatever its status. Nobody
#: is watching for a login prompt they asked for half a day ago, and leaving the
#: row would both pin the project at "capturing…" in the UI and hand the agent a
#: surprise headed browser the next time it polls.
ABANDON_AFTER = timedelta(hours=12)


@contextmanager
def _session() -> Iterator[Session]:
    """Short-lived own session, like :func:`audit_service.record` uses.

    Keeping the session internal means no call site had to grow a ``db``
    parameter when the queue moved from memory to the database (#625).
    """
    db = db_module.SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _owner_filter(query, owner_id: int | None):  # noqa: ANN001, ANN201
    """Filter on ``owner_id``, treating ``None`` as SQL NULL (auth-disabled installs)."""
    if owner_id is None:
        return query.filter(AgentCaptureRequest.owner_id.is_(None))
    return query.filter(AgentCaptureRequest.owner_id == owner_id)


def _as_dict(row: AgentCaptureRequest) -> dict:
    """Render a row in the shape the pre-#625 in-memory dict had."""
    return {
        "id": row.id,
        "owner_id": row.owner_id,
        "project_key": row.project_key,
        "base_url": row.base_url,
        "origin": row.origin,
        "status": row.status,
    }


def _is_live(row: AgentCaptureRequest, now: datetime | None = None) -> bool:
    """True while this capture can still plausibly complete.

    A row past its staleness window is *not* live even though it is still in the
    table: it is waiting for the next boot sweep (or the operator's next click)
    to reset it, and reporting it as live would keep the UI spinning on a
    capture nobody is running.
    """
    now = now or utcnow()
    if now - row.created_at > ABANDON_AFTER:
        return False
    if row.status == "running":
        return row.claimed_at is not None and (now - row.claimed_at) <= STALE_CLAIM_AFTER
    return True


def origin_of(base_url: str) -> str:
    """Scheme+host origin for a base URL (what the agent keys its session on)."""
    parts = urlsplit(base_url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


def request_capture(owner_id: int | None, project_key: str, base_url: str) -> dict:
    """Queue a capture for ``owner_id``'s project and return it as a dict.

    At most one live capture exists per ``(owner_id, project_key)`` — enforced by
    the ``dedupe_key`` UNIQUE index (#625), not by a per-process check. When one
    already exists:

    * ``queued`` — its ``base_url``/``origin`` are refreshed in place (the
      project's base URL may have changed since) and it is returned as-is.
    * ``running`` and claimed recently — returned untouched; the headed browser
      is open on the operator's machine.
    * ``running`` past :data:`STALE_CLAIM_AFTER`, or anything past
      :data:`ABANDON_AFTER` — the device is gone, so the row is re-queued with
      the fresh payload. This is the explicit replacement for the accidental
      reset a restart used to perform.
    """
    origin = origin_of(base_url)
    key = dedupe_key_for(owner_id, project_key)
    with _session() as db:
        existing = (
            db.query(AgentCaptureRequest).filter(AgentCaptureRequest.dedupe_key == key).first()
        )
        if existing is not None:
            live = _is_live(existing)
            existing.base_url = base_url
            existing.origin = origin
            if not live:
                logger.warning(
                    "Re-queueing an abandoned login capture "
                    "(capture={} project={} owner={} status={} claimed={})",
                    existing.id,
                    project_key,
                    owner_id,
                    existing.status,
                    existing.claimed_at,
                )
                existing.status = "queued"
                existing.claimed_at = None
                existing.created_at = utcnow()
            db.add(existing)
            db.commit()
            db.refresh(existing)
            return _as_dict(existing)

        row = AgentCaptureRequest(
            owner_id=owner_id,
            project_key=project_key,
            base_url=base_url,
            origin=origin,
            dedupe_key=key,
            status="queued",
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            # Another worker queued the same owner+project between the read and
            # the insert — the dedupe_key UNIQUE index is the arbiter. Return
            # the winner so the caller still sees a live capture.
            db.rollback()
            winner = (
                db.query(AgentCaptureRequest)
                .filter(AgentCaptureRequest.dedupe_key == key)
                .first()
            )
            if winner is None:  # pragma: no cover - the winning row must exist
                raise
            return _as_dict(winner)
        db.refresh(row)
        logger.info(
            "Queued a Local-Agent login capture (capture={} project={} origin={} owner={})",
            row.id,
            project_key,
            origin,
            owner_id,
        )
        return _as_dict(row)


def claim_next(owner_id: int | None) -> dict | None:
    """Claim the oldest queued capture for ``owner_id``; flip it to running.

    The flip is a conditional UPDATE guarded on ``status = 'queued'`` and checked
    by row count, so two API workers polling concurrently cannot both claim the
    same capture — the loser moves on to the next queued row. Returns None when
    nothing is queued.
    """
    with _session() as db:
        while True:
            row = (
                _owner_filter(
                    db.query(AgentCaptureRequest).filter(AgentCaptureRequest.status == "queued"),
                    owner_id,
                )
                .order_by(AgentCaptureRequest.id)
                .first()
            )
            if row is None:
                return None
            claimed = (
                db.query(AgentCaptureRequest)
                .filter(
                    AgentCaptureRequest.id == row.id,
                    AgentCaptureRequest.status == "queued",
                )
                .update({"status": "running", "claimed_at": utcnow()}, synchronize_session=False)
            )
            db.commit()
            if claimed != 1:
                # Lost the race to another worker; look at the next queued row.
                db.expire_all()
                continue
            db.refresh(row)
            return _as_dict(row)


def finish(capture_id: int, owner_id: int | None) -> dict | None:
    """Remove + return the capture ``capture_id`` (scoped to ``owner_id``), or
    None if it's unknown/not owned.

    The queue holds live work only, so the row is deleted; the durable record of
    a *successful* capture is the project config's ``agentAuthCapturedAt`` marker
    written by the caller.
    """
    with _session() as db:
        row = _owner_filter(
            db.query(AgentCaptureRequest).filter(AgentCaptureRequest.id == capture_id),
            owner_id,
        ).first()
        if row is None:
            return None
        payload = _as_dict(row)
        db.delete(row)
        db.commit()
        return payload


def is_capturing(owner_id: int | None, project_key: str) -> bool:
    """True while a *live* capture for this owner+project is queued or running.

    Reads the shared table, so it reflects a capture queued by any worker — the
    pre-#625 version answered from one process's list, which is why a capture
    queued on worker A looked absent on worker B (#625).
    """
    with _session() as db:
        row = (
            db.query(AgentCaptureRequest)
            .filter(AgentCaptureRequest.dedupe_key == dedupe_key_for(owner_id, project_key))
            .first()
        )
        return row is not None and _is_live(row)


def sweep_stranded(db: Session) -> tuple[int, int]:
    """Reset captures a dead process/device left mid-flight (#625) — the boot sweep.

    Called by ``run_status.recover_orphaned_captures``. Two outcomes, both
    logged, neither silent:

    1. **Re-queued** — a ``running`` capture claimed longer ago than
       :data:`STALE_CLAIM_AFTER`. The operator's request is still legitimate, so
       it goes back to ``queued`` rather than being dropped.
    2. **Dropped** — any capture older than :data:`ABANDON_AFTER`. Nobody is
       waiting on it, and keeping it would pin the UI at "capturing…" and hand
       the agent a headed browser out of nowhere.

    A ``running`` capture claimed *recently* is deliberately left alone: the
    agent runs on a different machine and its browser is still open, so its
    ``/complete`` post-back must still resolve.

    Does not commit — the caller does.

    Returns:
        ``(requeued, dropped)``.
    """
    now = utcnow()
    rows = (
        db.query(AgentCaptureRequest)
        .filter(
            or_(
                AgentCaptureRequest.status == "running",
                AgentCaptureRequest.created_at < now - ABANDON_AFTER,
            )
        )
        .all()
    )
    requeued = dropped = 0
    for row in rows:
        if now - row.created_at > ABANDON_AFTER:
            logger.warning(
                "Dropping an abandoned login capture "
                "(capture={} project={} owner={} status={} created={})",
                row.id,
                row.project_key,
                row.owner_id,
                row.status,
                row.created_at,
            )
            db.delete(row)
            dropped += 1
            continue
        if row.status != "running":
            continue
        claimed_at = row.claimed_at
        if claimed_at is not None and (now - claimed_at) <= STALE_CLAIM_AFTER:
            continue  # still plausibly in flight on the operator's machine
        logger.warning(
            "Re-queueing a login capture stranded mid-flight "
            "(capture={} project={} owner={} claimed={})",
            row.id,
            row.project_key,
            row.owner_id,
            claimed_at,
        )
        row.status = "queued"
        row.claimed_at = None
        db.add(row)
        requeued += 1
    return requeued, dropped
