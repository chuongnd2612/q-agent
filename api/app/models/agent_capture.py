"""Durable queue row for a Local-Agent manual-login capture request (#625).

"Capture login now" cannot open a browser on the (headless) API server — it must
open on the operator's OWN machine. So the server queues a capture, the paired
Local Agent claims it (``POST /agent/auth/next``), runs a headed login capture
locally, saves the session on that machine (never uploaded) and reports back
(``POST /agent/auth/{id}/complete``).

Until #625 that queue was a module-level ``list`` in ``agent_capture_service``,
which made it **process-local** — the same defect #605 closed one service over:

* any API restart (deploy, crash, ``suite.sh up -d --build``) silently lost the
  queued capture. The agent kept polling and kept getting 204, which from the
  outside is indistinguishable from "the agent isn't connected"; and
* with more than one API worker the queue was outright wrong — a capture queued
  in worker A's memory is invisible to a claim served by worker B.

It matters more than the "click again" comment on the old module implied: live
authoring **requires** a pre-authenticated ``browser-profile`` per origin (the
agent bails without one — #618) and this capture is the only thing that creates
it, so a dropped capture presents as "authoring is broken".

One row per **live** capture (``queued``/``running``); the row is deleted on
completion, so this is a queue and not a history. ``dedupe_key`` is UNIQUE and
carries ``(owner_id, project_key)`` — the "one live capture per owner+project"
guard, enforced by the database instead of by ``is_capturing`` reading one
process's list. It is a derived string rather than a composite UNIQUE index on
the two columns because ``owner_id`` is NULL on auth-disabled installs, and SQL
treats NULLs as distinct — a composite index would not dedupe there at all.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UTCDateTime, timestamp_column

CAPTURE_STATUSES = ("queued", "running")


def dedupe_key_for(owner_id: int | None, project_key: str) -> str:
    """The UNIQUE key for "one live capture per owner+project".

    ``None`` (auth disabled) is encoded as ``-`` so it collides with itself,
    which a NULL column would not.
    """
    return f"{owner_id if owner_id is not None else '-'}\x1f{project_key}"


class AgentCaptureRequest(Base):
    __tablename__ = "agent_capture_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    # The device owner allowed to claim this capture. Nullable because auth can
    # be disabled (local-first install), in which case it is NULL on both sides.
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    project_key: Mapped[str] = mapped_column(String(200), default="")
    base_url: Mapped[str] = mapped_column(String(500), default="")
    # Scheme+host the agent keys the saved session on (~/.qagent-agent/sessions/<origin>/).
    origin: Mapped[str] = mapped_column(String(500), default="")
    # owner_id + project_key, UNIQUE: one live capture per project per owner.
    dedupe_key: Mapped[str] = mapped_column(String(280), unique=True, index=True)

    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    created_at: Mapped[datetime] = timestamp_column()
    # When a device claimed it (status -> running). Now that the queue is durable
    # an unbounded claim would leave the project "capturing…" forever, where a
    # restart used to clear it by accident, so staleness is explicit instead.
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, default=None)
