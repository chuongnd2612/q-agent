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

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UTCDateTime, timestamp_column

#: ``paused``/``resuming`` were added by #619 (pause mid-authoring, feed guidance,
#: continue the SAME Claude session). A ``paused`` row is NOT stranded work: the
#: device is holding a live Chrome, a live temp workdir and a live
#: ``CLAUDE_CONFIG_DIR`` open for it, so anything that sweeps "abandoned" sessions
#: must treat it as alive until its pause expires.
AUTHORING_SESSION_STATUSES = ("queued", "running", "paused", "resuming")


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

    # ---------------------------------------------- pause / resume (#619)
    # Set by the user's Pause; the agent picks it up on its next progress post
    # (the channel it already calls once per Claude step), so pause needs no new
    # poller — the #625 rule is that nothing a poller waits on may live in
    # process memory, and this column is that state.
    pause_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # When the agent confirmed it had stopped Claude and parked. Drives the pause
    # expiry (a forgotten pause must not leak a browser + temp dir forever) and
    # the staleness check, which for a paused row is measured from HERE, not from
    # claimed_at (a long, legitimate pause is not an abandoned claim).
    paused_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, default=None)
    # Claude CLI's OWN session id, read off the `--output-format stream-json`
    # envelope. `session_id` above is Q-Agent's queue id and is NOT usable with
    # `claude --resume`. Empty when the envelope never carried one, which is
    # exactly when Continue must fall back to a fresh guided pass.
    claude_session_id: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    # JSON array of guidance strings the user typed while paused, not yet handed
    # to the device. Cleared when a resume delivers them.
    guidance: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Every guidance string ever accepted for this session, kept so a FALLBACK
    # (fresh pass) can carry the whole accumulated intent, not just the newest
    # turn — the resumed Claude session remembers earlier guidance, a fresh one
    # does not.
    guidance_history: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Claude spend across ALL passes of this session. The budget ceiling is a
    # SESSION budget, so each resume is handed `max_budget_usd - cost_usd_so_far`
    # rather than the full ceiling again (#619).
    cost_usd_so_far: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    resume_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
