"""Project configuration model — user-authored runtime settings for a project.

Complements :class:`app.models.knowledge.ProjectKnowledge` (which is *discovered*
by the ``project-bootstrap`` skill). This model holds what a human configures on
the Project Details page — the values downstream Playwright generation needs to
emit runnable specs without placeholders: the application URL, test accounts,
per-environment URLs, and any other project-specific key/values.

Test-account passwords are stored **encrypted at rest** (see :mod:`app.crypto`),
mirroring how provider credentials are handled, and are never returned to the UI
in plaintext.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, timestamp_column, utcnow


class ProjectConfig(Base):
    __tablename__ = "project_config"
    # ADR 0009 §3 — the same project ``key`` can exist once per owner and once
    # in the shared namespace (``owner_id IS NULL``), so uniqueness is scoped
    # to the pair rather than global.
    __table_args__ = (UniqueConstraint("key", "owner_id", name="uq_project_config_key_owner"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # Keyed by project name (the UI's project identifier), matching ProjectKnowledge.key.
    #: The owning project's GUID (#585). Nullable during the G1 bridge; ``key``
    #: (the project NAME) is still written so nothing breaks mid-refactor.
    project_guid: Mapped[str | None] = mapped_column(
        String(36), index=True, nullable=True, default=None
    )
    key: Mapped[str] = mapped_column(String(200), index=True)
    name: Mapped[str] = mapped_column(String(200), default="")

    # Per-project provider bindings (ADR 0006). A project's tickets can come from
    # one work-item connection while its code lives on a separate repository
    # connection. Both nullable — un-bound projects degrade to first-of-category.
    work_item_connection_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("provider_connections.id"), nullable=True
    )
    repository_connection_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("provider_connections.id"), nullable=True
    )

    # Primary application URL the generated automation targets.
    base_url: Mapped[str] = mapped_column(String(500), default="")
    # Repositories that belong to this project (an ADO/GitHub project can hold
    # many). Each entry: {name, repo_url, local_repo_path, role, primary}. The
    # repo flagged ``primary`` is the app the Playwright tests drive and the one
    # project-bootstrap traverses; the rest are recorded as related context.
    repos: Mapped[list] = mapped_column(JSON, default=list)

    # --- Legacy single-repo fields (kept for backward compatibility) ----------
    # Superseded by ``repos``; still honored when ``repos`` is empty.
    local_repo_path: Mapped[str] = mapped_column(String(600), default="")
    repo_url: Mapped[str] = mapped_column(String(600), default="")

    # [{ "name": str, "base_url": str, "notes": str }]
    environments: Mapped[list] = mapped_column(JSON, default=list)
    # [{ "role": str, "username": str, "password": <encrypted>, "notes": str }]
    test_accounts: Mapped[list] = mapped_column(JSON, default=list)
    # Arbitrary project-specific config values downstream generation may reference.
    extra: Mapped[dict] = mapped_column(JSON, default=dict)

    # When True, a run captures a real (headed) browser login before executing
    # specs (if no saved session exists) and reuses the saved storageState.
    manual_auth: Mapped[bool] = mapped_column(default=False)

    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column(onupdate=utcnow)
    # Per-user ownership (#91) — data is per-user private. Nullable until the
    # cleanup issue (#98) backfills every row and enforces non-null.
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
