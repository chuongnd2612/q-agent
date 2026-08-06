"""Ticket model — a read-only work item imported from a provider."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, timestamp_column

# Work-item statuses used by the UI status pills.
STATUSES = ("Ready for QA", "In Progress", "Blocked", "Done")
PRIORITIES = ("High", "Medium", "Low")


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str] = mapped_column(String(64), index=True)  # e.g. "SUR-1428"
    provider_kind: Mapped[str] = mapped_column(String(16), index=True)
    project_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    # The work-item connection this ticket was synced from (ADR 0006). Nullable —
    # legacy rows and un-stamped tickets fall back to first-of-kind resolution.
    connection_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("provider_connections.id"), index=True, nullable=True
    )
    # The EmeHub ticket this row corresponds to (#500, docs/HUB-INTEGRATION.md §5).
    # NULL until a hub read matches it on ``(provider_kind, external_id)``.
    #
    # Deliberately **not unique**: tickets are per-user private data, so the same
    # hub work item legitimately appears once per owner. It is also not a foreign
    # key — the hub is a different database — and never a join *source*: nothing
    # reads a ticket *by* this column. It exists so the two stores are
    # reconcilable, which turns an eventual cutover into a join rather than a
    # re-import. Q-Agent keeps ownership of this table either way; the hub cannot
    # supply the provider PAT that ticket sync needs (#497 §4c).
    hub_ticket_id: Mapped[str | None] = mapped_column(
        String(64), index=True, nullable=True, default=None
    )

    title: Mapped[str] = mapped_column(String(500))
    work_item_type: Mapped[str] = mapped_column(String(32), default="User Story")
    status: Mapped[str] = mapped_column(String(32), default="Ready for QA")
    priority: Mapped[str] = mapped_column(String(16), default="Medium")
    assignee: Mapped[str] = mapped_column(String(120), default="")
    sprint: Mapped[str] = mapped_column(String(120), default="")
    area_path: Mapped[str] = mapped_column(String(300), default="")
    epic: Mapped[str] = mapped_column(String(300), default="")

    description: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")

    labels: Mapped[list] = mapped_column(JSON, default=list)
    acceptance_criteria: Mapped[list] = mapped_column(JSON, default=list)  # list[str]
    # Original provider AC as rich HTML — rendered read-only when the criteria
    # don't split cleanly into a numbered list (#225).
    acceptance_criteria_html: Mapped[str] = mapped_column(Text, default="", server_default="")
    comments: Mapped[list] = mapped_column(JSON, default=list)
    attachments: Mapped[list] = mapped_column(JSON, default=list)
    linked_prs: Mapped[list] = mapped_column(JSON, default=list)

    synced_at: Mapped[datetime] = timestamp_column()
    # Per-user ownership (#91) — data is per-user private. Nullable until the
    # cleanup issue (#98) backfills every row and enforces non-null.
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )

    @property
    def ac_count(self) -> int:
        return len(self.acceptance_criteria or [])
