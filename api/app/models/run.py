"""Run model — the central QA-session entity, plus its per-ticket rows."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, UTCDateTime, timestamp_column

# Pipeline stages (also the Run.status state machine).
RUN_STATUSES = (
    "processing",  # AI analysis + test-case generation
    "review",
    "sync",  # create approved cases in the provider + link to work items. NOTE:
    # this is the pipeline's "Link" stage (UI route /sync, screen CreateLinkSync) —
    # NOT a "Sync tickets" stage. Ticket sync + selection happen before a run exists.
    "automation",
    "executing",
    "evidence",
    "comment",
    "done",
    "cancelled",  # user-requested cancel
    "failed",  # worker error
)
# Terminal statuses — see ADR 0005: a terminal run is never advanced by a worker.
TERMINAL_RUN_STATUSES = frozenset({"done", "cancelled", "failed"})
RUN_SCOPES = ("single", "selected", "assigned", "sprint")

# Per-ticket generation status inside a run.
GEN_STATUSES = ("queued", "analyzing", "generating", "done", "error")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # e.g. "RUN-205"
    name: Mapped[str] = mapped_column(String(300))
    scope: Mapped[str] = mapped_column(String(32), default="selected")
    scope_label: Mapped[str] = mapped_column(String(120), default="Selected tickets")

    framework: Mapped[str] = mapped_column(String(32), default="Playwright")
    browser: Mapped[str] = mapped_column(String(32), default="chromium")
    env: Mapped[str] = mapped_column(String(32), default="Staging")
    workers: Mapped[int] = mapped_column(Integer, default=4)
    retry_policy: Mapped[int] = mapped_column(Integer, default=2)  # retries on flaky

    status: Mapped[str] = mapped_column(String(32), default="processing", index=True)
    created_at: Mapped[datetime] = timestamp_column()
    # Lifecycle metadata (ADR 0005) — set exclusively via app.services.run_status.
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    failed_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Why the last automation-generation pass produced nothing (#641). JSON:
    # `{"at": iso, "attempted": n, "failures": [{"caseId", "code", "message"}]}`.
    # Per-case failures used to exist ONLY as a WebSocket progress event, so a
    # user who wasn't watching the screen at that moment saw the generic "No
    # automation yet" and could not tell a blocked prerequisite (no paired agent,
    # no project base URL) from "there was nothing to generate". Cleared at the
    # start of every pass, so a green pass never leaves a stale error behind.
    last_generation_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Per-user ownership (#91) — data is per-user private. Nullable until the
    # cleanup issue (#98) backfills every row and enforces non-null.
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)

    run_tickets: Mapped[list["RunTicket"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="RunTicket.position"
    )

    @property
    def ticket_ids(self) -> list[str]:
        return [rt.ticket_external_id for rt in self.run_tickets]


class RunTicket(Base):
    """Association of a ticket to a run, carrying per-ticket AI analysis + status."""

    __tablename__ = "run_tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    ticket_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ticket_external_id: Mapped[str] = mapped_column(String(64), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    gen_status: Mapped[str] = mapped_column(String(16), default="queued")
    # Target repository NAME for this work item ("" = use the project default repo).
    # Claude guesses it during analysis; the user can override it.
    repo: Mapped[str] = mapped_column(String(300), default="")
    # AI analysis output: {businessRules, functionalRequirements, validationRules,
    # risks, edgeCases, missingInformation, suggestedScope}
    analysis: Mapped[dict] = mapped_column(JSON, default=dict)
    analysis_error: Mapped[str] = mapped_column(Text, default="")

    run: Mapped["Run"] = relationship(back_populates="run_tickets")
