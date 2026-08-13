"""Project Knowledge Base model — what Q-Agent learned about a project's repo.

Produced by the `project-bootstrap` skill (via Claude CLI) and reused by every
downstream AI action (analysis, generation, automation).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UTCDateTime, timestamp_column, utcnow

KNOWLEDGE_STATUSES = ("not_indexed", "indexing", "indexed", "stale", "error")


def compose_key(project_key: str, repo: str = "") -> str:
    """Row key for a project's (optionally repo-scoped) knowledge base.

    Per-repo rows use ``"<project>::<repo>"``; a blank repo yields the bare
    project key (legacy / project-level knowledge).
    """
    return f"{project_key}::{repo}" if repo else project_key


class ProjectKnowledge(Base):
    __tablename__ = "project_knowledge"
    # ADR 0009 §3 — the same ``key`` can exist once per owner and once in the
    # shared namespace (``owner_id IS NULL``), so uniqueness is scoped to the
    # pair rather than global.
    __table_args__ = (UniqueConstraint("key", "owner_id", name="uq_project_knowledge_key_owner"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # Unique row identifier. Per-repo rows use the composite "<project>::<repo>";
    # legacy project-level rows use just "<project>". See ``compose_key``.
    key: Mapped[str] = mapped_column(String(320), index=True)
    # The owning project (the UI's project identifier). Many repos → many rows.
    #: The owning project's GUID (#585). See ProjectConfig.project_guid.
    project_guid: Mapped[str | None] = mapped_column(
        String(36), index=True, nullable=True, default=None
    )
    project_key: Mapped[str] = mapped_column(String(200), default="", index=True)
    name: Mapped[str] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(String(64), default="")
    # The specific repository this knowledge base describes ("" = project-level/legacy).
    repo: Mapped[str] = mapped_column(String(300), default="")
    framework: Mapped[str] = mapped_column(String(64), default="Playwright")

    status: Mapped[str] = mapped_column(String(16), default="not_indexed", index=True)
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[str] = mapped_column(String(16), default="v1")
    needs_refresh: Mapped[bool] = mapped_column(Boolean, default=False)
    last_indexed: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    # {branch, stack[], architecture, domain, locator, assets, pageObjects,
    #  fixtures, utilities[]}
    knowledge: Mapped[dict] = mapped_column(JSON, default=dict)
    # Directory holding the emitted knowledge.md + knowledge.json (skill artifacts).
    doc_path: Mapped[str] = mapped_column(String(600), default="")
    # Last build error message (when status == "error"); cleared on success.
    last_error: Mapped[str] = mapped_column(String(1000), default="")

    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column(onupdate=utcnow)
    # Per-user ownership (#91) — data is per-user private. Nullable until the
    # cleanup issue (#98) backfills every row and enforces non-null.
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
