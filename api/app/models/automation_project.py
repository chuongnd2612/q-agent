"""Persistent, git-backed per-project Playwright automation project (#538).

Before this, every generated spec was per-run and throwaway
(``spec_service.write_spec_file`` -> ``workspace/<scope>/specs/<RUN-CODE>/``).
:class:`AutomationProject` is the *living* home instead: one git repo per
``(owner_id, project_key, repo)`` that accumulates page objects, components,
fixtures and test data across every run, so each new feature generates less
code than the last (epic #537).

**Repo granularity matters.** ``spec_service.build_case_context`` already
resolves the knowledge base per repo, so a project with two front-ends must not
share one page-object namespace — hence ``repo`` is part of the unique key.

:class:`AutomationFile` is a **queryable mirror of disk, never the source of
truth.** The agentic editor writes real files (the only way Claude can
Read/Edit them) and ``automation_project_service.sync_files_to_db`` reconciles
the rows afterwards. Never the reverse: that keeps the recover-from-disk
property and avoids a two-writer consistency problem.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, timestamp_column

# The kinds of file the project holds. Mirrors the on-disk directory skeleton
# (``pages/``, ``components/``, ``fixtures/``, ``data/``, ``utils/``,
# ``config/``, ``tests/``) — see ``automation_project_service.SCAFFOLD_DIRS``.
FILE_KINDS = ("page", "component", "fixture", "data", "util", "config", "spec")


class AutomationProject(Base):
    """One persistent git-backed automation project, per owner+project+repo."""

    __tablename__ = "automation_projects"
    __table_args__ = (
        UniqueConstraint(
            "owner_id", "project_key", "repo", name="uq_automation_projects_owner_key_repo"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Per-user ownership (#91) — nullable so the shared/auth-disabled namespace
    # ("workspace/shared/...") has a home too, matching workspace_scope.scope_for.
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    project_key: Mapped[str] = mapped_column(String(128), index=True)
    # The repo this asset library belongs to. "" means "the project's only repo".
    repo: Mapped[str] = mapped_column(String(200), default="")
    # Filesystem-safe "<project-slug>/<repo-slug>" (workspace_scope.slug).
    slug: Mapped[str] = mapped_column(String(400), default="")
    # Absolute on-disk root, for observability/queries. The authoritative path is
    # always recomputed by ``automation_project_service.project_dir`` so a moved
    # or re-pointed workspace keeps resolving.
    root_path: Mapped[str] = mapped_column(String(1000), default="")
    # Installed ``@q-agent/playwright-base`` version (or "" when deps aren't
    # installed yet / the registry was unreachable and no fallback resolved).
    base_version: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column()

    files: Mapped[list["AutomationFile"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class AutomationFile(Base):
    """A queryable mirror of one file in the project tree — never authoritative."""

    __tablename__ = "automation_files"
    __table_args__ = (UniqueConstraint("project_id", "path", name="uq_automation_files_project_path"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("automation_projects.id", ondelete="CASCADE"), index=True
    )
    # POSIX-style path relative to the project root, e.g. "pages/LoginPage.ts".
    path: Mapped[str] = mapped_column(String(500))
    kind: Mapped[str] = mapped_column(String(16), default="util")  # see FILE_KINDS
    code: Mapped[str] = mapped_column(Text, default="")
    sha256: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = timestamp_column()

    project: Mapped["AutomationProject"] = relationship(back_populates="files")
