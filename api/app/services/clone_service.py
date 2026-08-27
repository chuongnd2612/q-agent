"""Clone a shared-namespace project into a member's own scope (ADR 0009 §4, #120).

The admin-managed shared namespace (``owner_id IS NULL``) holds ready-built
projects — config (incl. encrypted test accounts) and an AI-built Project
Knowledge Base — that are expensive to (re)build (``project-bootstrap`` runs a
real Claude pass with a 20-minute budget). Cloning copies those rows and their
on-disk artifacts into the caller's own scope instead of rebuilding, re-
stamping ``owner_id`` while keeping the same project ``key`` (composite-unique
on ``(key, owner_id)`` since ADR 0009 §3).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.knowledge import ProjectKnowledge
from app.models.project import Project
from app.models.project_config import ProjectConfig
from app.models.user import User
from app.services.workspace_scope import scoped_auth_dir, scoped_knowledge_dir, scoped_repos_dir, slug


@dataclass
class CloneResult:
    """Summary of what :func:`clone_shared_project` copied."""

    project_key: str
    projects_cloned: int = 0
    config_cloned: bool = False
    knowledge_cloned: list[str] = field(default_factory=list)
    artifacts_copied: list[str] = field(default_factory=list)


def _shared_projects(db: Session, project_key: str) -> list[Project]:
    """Shared (``owner_id IS NULL``) ``Project`` rows matching ``project_key`` (matched by name)."""
    return db.query(Project).filter(Project.name == project_key, Project.owner_id.is_(None)).all()


def _shared_config(db: Session, project_key: str) -> ProjectConfig | None:
    """The shared ``ProjectConfig`` row for ``project_key``, or ``None``."""
    return (
        db.query(ProjectConfig)
        .filter(ProjectConfig.key == project_key, ProjectConfig.owner_id.is_(None))
        .first()
    )


def _shared_knowledge(db: Session, project_key: str) -> list[ProjectKnowledge]:
    """All shared knowledge rows for ``project_key``: the bare key + every ``<project>::<repo>`` row."""
    prefix = f"{project_key}::"
    return [
        row
        for row in db.query(ProjectKnowledge).filter(ProjectKnowledge.owner_id.is_(None)).all()
        if row.key == project_key or row.key.startswith(prefix)
    ]


def dest_already_has_project(db: Session, project_key: str, dest_owner_id: int | None) -> bool:
    """True if ``dest_owner_id`` already owns a ``Project``/``ProjectConfig``/``ProjectKnowledge``
    row keyed ``project_key`` — the 409 condition for :func:`clone_shared_project`."""
    if db.query(ProjectConfig).filter(
        ProjectConfig.key == project_key, ProjectConfig.owner_id == dest_owner_id
    ).first():
        return True
    if db.query(ProjectKnowledge).filter(
        ProjectKnowledge.key == project_key, ProjectKnowledge.owner_id == dest_owner_id
    ).first():
        return True
    if db.query(Project).filter(
        Project.name == project_key, Project.owner_id == dest_owner_id
    ).first():
        return True
    return False


def _copy_scope_subtree(dir_fn, project_key: str, dest_owner_id: int | None) -> bool:
    """Copy ``<shared-scope>/<kind>/<slug(project_key)>`` to ``<dest-scope>/<kind>/<slug(project_key)>``.

    ``dir_fn`` is one of the ``scoped_*_dir`` resolvers. No-op (returns
    ``False``) when the shared source directory doesn't exist.
    """
    src = dir_fn(None) / slug(project_key)
    if not src.exists():
        return False
    dst = dir_fn(dest_owner_id) / slug(project_key)
    shutil.copytree(src, dst, dirs_exist_ok=True)
    return True


def _rescope_doc_path(doc_path: str, dest_owner_id: int | None) -> str:
    """Rewrite a shared ``doc_path`` to the equivalent path under the dest scope.

    ``doc_path`` (written by ``knowledge_service.write_knowledge_files``) is an
    absolute path under ``scoped_knowledge_dir(None)``. Returned unchanged if
    it isn't rooted there (defensive — blank/legacy rows).
    """
    if not doc_path:
        return doc_path
    shared_root = scoped_knowledge_dir(None)
    try:
        relative = Path(doc_path).resolve().relative_to(shared_root.resolve())
    except ValueError:
        return doc_path
    return str(scoped_knowledge_dir(dest_owner_id) / relative)


def clone_shared_project(db: Session, project_key: str, dest_owner: User | None) -> CloneResult:
    """Clone a shared-namespace project into ``dest_owner``'s own scope.

    Loads the shared (``owner_id IS NULL``) rows for ``project_key`` — the
    ``Project``(s), its ``ProjectConfig``, and every ``ProjectKnowledge`` row
    (bare + per-repo) — and copies them with ``owner_id`` re-stamped to
    ``dest_owner.id`` (``None`` when ``dest_owner`` is ``None``, i.e. auth is
    disabled and the caller already *is* the shared scope — see the 409 case
    below). The Fernet-encrypted ``test_accounts`` ciphertext is copied
    verbatim (the key is process-wide — ADR 0009 §4). Provider-connection
    bindings (``connection_id``, ``work_item_connection_id``,
    ``repository_connection_id``, ``test_case_connection_id``) are dropped rather than copied: those FKs
    point at the admin's own connections, which the destination owner cannot
    see or use.

    On-disk ``knowledge/``, ``repos/`` and ``auth/`` subtrees are copied from
    the shared scope to the destination scope, preserving the
    ``<slug(project_key)>/…`` structure; each cloned ``ProjectKnowledge``'s
    ``doc_path`` is rewritten to the copied destination directory.

    Files are copied *before* any DB row is created, and nothing is committed
    until every row has been added — so a file-copy failure leaves the
    database untouched (nothing to roll back) and a DB failure hasn't left a
    dangling artifact tree behind that a caller might mistake for evidence of
    a partial clone.

    Args:
        db: Active session (commits on success).
        project_key: The shared project's key (== its ``Project.name`` /
            ``ProjectConfig.key`` / ``ProjectKnowledge.project_key``).
        dest_owner: The user cloning the project (``None`` only when auth is
            disabled).

    Returns:
        A :class:`CloneResult` summary of what was copied.

    Raises:
        HTTPException(404): no shared project exists for ``project_key``.
        HTTPException(409): the destination already has a project (any of
            ``Project``/``ProjectConfig``/``ProjectKnowledge``) with this key.
    """
    dest_owner_id = dest_owner.id if dest_owner is not None else None

    projects = _shared_projects(db, project_key)
    config = _shared_config(db, project_key)
    knowledge_rows = _shared_knowledge(db, project_key)
    if not projects and config is None and not knowledge_rows:
        raise HTTPException(status_code=404, detail=f"No shared project '{project_key}'")

    if dest_already_has_project(db, project_key, dest_owner_id):
        raise HTTPException(
            status_code=409, detail=f"You already have a project named '{project_key}'"
        )

    # The point of cloning is to reuse already-built knowledge (ADR 0009 §4).
    # Cloning a project whose knowledge never indexed copies nothing useful and
    # would force the member to rebuild — block it (the catalog also disables the
    # button, this is the server-side backstop).
    if not any(k.status == "indexed" for k in knowledge_rows):
        raise HTTPException(
            status_code=422,
            detail=f"Shared project '{project_key}' has no built knowledge to clone yet.",
        )

    # Copy on-disk artifacts first — a failure here must leave the DB untouched.
    artifacts_copied: list[str] = []
    if _copy_scope_subtree(scoped_knowledge_dir, project_key, dest_owner_id):
        artifacts_copied.append("knowledge")
    if _copy_scope_subtree(scoped_repos_dir, project_key, dest_owner_id):
        artifacts_copied.append("repos")
    if _copy_scope_subtree(scoped_auth_dir, project_key, dest_owner_id):
        artifacts_copied.append("auth")

    result = CloneResult(project_key=project_key, artifacts_copied=artifacts_copied)

    for p in projects:
        db.add(
            Project(
                provider_kind=p.provider_kind,
                external_id=p.external_id,
                name=p.name,
                active=p.active,
                meta=dict(p.meta or {}),
                connection_id=None,
                owner_id=dest_owner_id,
            )
        )
        result.projects_cloned += 1

    if config is not None:
        db.add(
            ProjectConfig(
                key=config.key,
                name=config.name,
                base_url=config.base_url,
                repos=[dict(r) for r in (config.repos or [])],
                local_repo_path=config.local_repo_path,
                repo_url=config.repo_url,
                environments=[dict(e) for e in (config.environments or [])],
                test_accounts=[dict(a) for a in (config.test_accounts or [])],  # ciphertext as-is
                extra=dict(config.extra or {}),
                manual_auth=config.manual_auth,
                work_item_connection_id=None,
                repository_connection_id=None,
                test_case_connection_id=None,
                owner_id=dest_owner_id,
            )
        )
        result.config_cloned = True

    for row in knowledge_rows:
        db.add(
            ProjectKnowledge(
                key=row.key,
                project_key=row.project_key,
                name=row.name,
                provider=row.provider,
                repo=row.repo,
                framework=row.framework,
                status=row.status,
                confidence=row.confidence,
                version=row.version,
                needs_refresh=row.needs_refresh,
                last_indexed=row.last_indexed,
                knowledge=dict(row.knowledge or {}),
                doc_path=_rescope_doc_path(row.doc_path, dest_owner_id),
                last_error=row.last_error,
                owner_id=dest_owner_id,
            )
        )
        result.knowledge_cloned.append(row.key)

    db.commit()
    return result
