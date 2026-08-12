"""Test case + generated automation spec models."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, timestamp_column

APPROVAL_STATUSES = ("pending", "approved", "rejected")
AUTOMATION_TYPES = ("Playwright", "Selenium", "Cypress", "Manual")
# "ai" = happy-path generator; "ai-review" = reviewer coverage expansion (#173);
# "manual" = authored by a QA engineer in the app.
SOURCES = ("ai", "ai-review", "manual")
# Lifecycle of a generated spec through the placeholder gate + execution/heal loop.
SPEC_STATUSES = ("draft", "blocked", "running", "passed", "failed", "product_defect")


class TestCase(Base):
    __tablename__ = "test_cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    ticket_external_id: Mapped[str] = mapped_column(String(64), index=True)
    code: Mapped[str] = mapped_column(String(32))  # e.g. "TC-01"

    title: Mapped[str] = mapped_column(String(500))
    objective: Mapped[str] = mapped_column(Text, default="")  # one-line "what this proves"
    precondition: Mapped[str] = mapped_column(Text, default="")
    steps: Mapped[list] = mapped_column(JSON, default=list)  # [{a, e}]
    test_data: Mapped[list] = mapped_column(JSON, default=list)  # [{field, value}]
    # Acceptance-criterion ids/text this case covers — the atomic traceability
    # link; the run's AC→cases coverage matrix is derived from these (#177).
    linked_ac: Mapped[list] = mapped_column(JSON, default=list)

    priority: Mapped[str] = mapped_column(String(16), default="Medium")
    test_type: Mapped[str] = mapped_column(String(48), default="Functional")
    automation: Mapped[str] = mapped_column(String(32), default="Playwright")
    platform: Mapped[str] = mapped_column(String(16), default="Web")
    duration: Mapped[str] = mapped_column(String(16), default="—")

    approval: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    source: Mapped[str] = mapped_column(String(16), default="ai")
    edited: Mapped[bool] = mapped_column(default=False)

    created_at: Mapped[datetime] = timestamp_column()

    spec: Mapped["AutomationSpec | None"] = relationship(
        back_populates="test_case", cascade="all, delete-orphan", uselist=False
    )


class AutomationSpec(Base):
    __tablename__ = "automation_specs"

    id: Mapped[int] = mapped_column(primary_key=True)
    test_case_id: Mapped[int] = mapped_column(
        ForeignKey("test_cases.id", ondelete="CASCADE"), index=True, unique=True
    )
    filename: Mapped[str] = mapped_column(String(200))  # e.g. "1428-TC-01.spec.ts"
    language: Mapped[str] = mapped_column(String(32), default="TypeScript")
    framework: Mapped[str] = mapped_column(String(32), default="Playwright")
    code: Mapped[str] = mapped_column(Text, default="")
    path: Mapped[str] = mapped_column(String(500), default="")  # on-disk spec path
    # Last self-heal run: JSON {finalStatus, maxAttempts, healedAt, attempts:[...]}.
    heal_report: Mapped[str] = mapped_column(Text, default="")
    # Placeholder-gate + execution lifecycle (see SPEC_STATUSES).
    status: Mapped[str] = mapped_column(String(24), default="draft", index=True)
    # Why the gate blocked this spec (human-readable; empty unless blocked).
    block_reason: Mapped[str] = mapped_column(Text, default="")
    # JSON string of the last placeholder-gate outcome (see placeholder_gate.gate_spec).
    gate_report: Mapped[str] = mapped_column(Text, default="")
    # The persistent git-backed automation project this spec lives in (#538).
    # Nullable on purpose and with no backfill: every pre-existing spec keeps
    # working as `project_id IS NULL` (per-run, self-contained, legacy model).
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("automation_projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # JSON string of the Reuse/Extend/Create planner outcome (#544), mirroring the
    # `gate_report`/`heal_report` convention. Nullable — NULL means "not planned".
    plan_report: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = timestamp_column()

    test_case: Mapped["TestCase"] = relationship(back_populates="spec")
