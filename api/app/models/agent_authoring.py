"""Durable queue row for an agent-driven live-authoring session (#605).

Live authoring hands one job per test case to the paired Local Agent: the server
composes the prompts, queues a session, the agent claims it
(``POST /agent/authoring/next``), authors the spec locally and posts it back to
``POST /agent/authoring/{id}/finalize``.

Until #605 that queue lived in a module-level ``list`` inside
``agent_authoring_service``, which made it **process-local**:

* any API restart (deploy, crash, ``docker compose up -d --build``) silently lost
  every queued session, while the ``AutomationSpec`` row had already been
  committed at ``status="running"`` with empty ``code`` — so the spec hung at
  "authoring…" forever and the agent, correctly, had nothing to claim;
* with more than one API worker the queue was simply wrong: a session enqueued in
  worker A's memory is invisible to a claim that lands on worker B.

One row per **live** session. Rows are deleted on finalize / stop / eviction, so
this table only ever holds ``queued`` + ``running`` work — it is a queue, not a
history (terminal outcomes are logged and audited instead). ``case_id`` is
UNIQUE, which is the #419 "never author the same case twice concurrently" guard
enforced by the database rather than by a check inside one process's lock.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UTCDateTime, timestamp_column

AUTHORING_SESSION_STATUSES = ("queued", "running")


class AgentAuthoringSession(Base):
    __tablename__ = "agent_authoring_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Opaque hex id handed to the agent; every follow-up call addresses it.
    session_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # The device owner allowed to claim this session. Nullable because auth can be
    # disabled (local-first install), in which case owner_id is NULL on both sides.
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # One live session per case (the #419 guard, enforced by the DB).
    case_id: Mapped[int] = mapped_column(
        ForeignKey("test_cases.id", ondelete="CASCADE"), unique=True, index=True
    )
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=True, index=True
    )

    project_key: Mapped[str] = mapped_column(String(200), default="")
    repo: Mapped[str] = mapped_column(String(200), default="")
    base_url: Mapped[str] = mapped_column(String(500), default="")
    origin: Mapped[str] = mapped_column(String(500), default="")
    spec_filename: Mapped[str] = mapped_column(String(500), default="")

    system_prompt: Mapped[str] = mapped_column(Text, default="")
    task_prompt: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    max_budget_usd: Mapped[float] = mapped_column(Float, default=0.0)
    log_verbosity: Mapped[str] = mapped_column(String(24), default="concise")

    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    created_at: Mapped[datetime] = timestamp_column()
    # When a device claimed it (status -> running). A claim that never finalizes
    # would otherwise wedge the case forever now that the queue is durable, so
    # request_authoring re-queues a claim older than the stale-claim window.
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, default=None)
