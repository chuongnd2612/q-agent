"""Project model — a connected project from a provider."""

from __future__ import annotations

import uuid

from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, timestamp_column


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Stable public identifier (#585). Generated once and never reused.
    #:
    #: Everything about a project used to hang off its **name**: routes were
    #: ``/projects/{name}/…`` and ``project_config`` / ``project_knowledge`` /
    #: ``automation_projects`` all keyed by that string. Two users with the same
    #: project name therefore collided (#583), and renaming a project orphaned its
    #: config, knowledge and automation. The name is display text; this is
    #: identity, and the two are now separate.
    guid: Mapped[str] = mapped_column(
        String(36), unique=True, index=True, default=lambda: str(uuid.uuid4())
    )
    # Set when this row MIRRORS a project EmeHub owns (#587), following the
    # ``hub_connection_id`` / ``hub_ticket_id`` convention. It is the hub's
    # **numeric id**, not its key: the hub's project screen deep-links by id
    # (``<hub web origin>/app/projects/{id}``, verified in the hub's
    # `screens/Projects/index.tsx`). NULL for a project discovered locally, which
    # is why the UI must degrade to a generic "manage in EmeHub" hint rather than
    # building a link it cannot complete.
    hub_project_id: Mapped[str | None] = mapped_column(
        String(64), index=True, nullable=True, default=None
    )
    provider_kind: Mapped[str] = mapped_column(String(16), index=True)
    external_id: Mapped[str] = mapped_column(String(128), index=True)  # ADO/Jira project id/key
    name: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(default=False)
    # The work-item connection that discovered this project (set during refresh);
    # convenience only — not the credential router (ADR 0006).
    connection_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("provider_connections.id"), nullable=True
    )
    meta: Mapped[dict] = mapped_column(JSON, default=dict)  # tickets/runs/rate cached stats
    created_at: Mapped[datetime] = timestamp_column()
    # Per-user ownership (#91) — data is per-user private. Nullable until the
    # cleanup issue (#98) backfills every row and enforces non-null.
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
