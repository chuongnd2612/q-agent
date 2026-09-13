"""Clone a shared-namespace project into a member's own scope (ADR 0009 §4, #120).

The admin-managed shared namespace (``owner_id IS NULL``) holds ready-built
projects — config (incl. encrypted test accounts), an AI-built Project
Knowledge Base, and the project's **Business Knowledge** (ADR 0016, #831) — that
are expensive to (re)build (``project-bootstrap`` runs a real Claude pass with a
20-minute budget; a business source costs a fetch, a normalize and a distil
pass). Cloning copies those rows and their on-disk artifacts into the caller's
own scope instead of rebuilding, re-stamping ``owner_id`` while keeping the same
project ``key`` (composite-unique on ``(key, owner_id)`` since ADR 0009 §3) and
the same ``project_guid`` (the Business Knowledge tables' half of the same
composite key — ADR 0016 §3).

**Hub mirroring is deliberately not implemented here.** A cloned project's
Business Knowledge exists only in Q-Agent: nothing is pushed to EmeHub, and a
document curated in the hub is not pulled in. Doing so needs a hub-side
endpoint that does not exist — the hub's project payload carries config,
connections and knowledge and has no business-document surface at all, nor a
way to authorise one member reading another's snapshot. The ask is written
down, in the shape ``docs/HUB-REQUESTS-project-config.md`` set:
**``docs/HUB-REQUESTS-business-knowledge.md``**. Until it is answered this
clone is Q-Agent-local by design rather than by omission.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.logging import logger
from app.models.business import BusinessFact, BusinessSource
from app.models.knowledge import ProjectKnowledge
from app.models.project import Project
from app.models.project_config import ProjectConfig
from app.models.user import User
from app.services import business_source_service
from app.services.workspace_scope import (
    scoped_auth_dir,
    scoped_business_dir,
    scoped_knowledge_dir,
    scoped_repos_dir,
    slug,
)


@dataclass
class CloneResult:
    """Summary of what :func:`clone_shared_project` copied."""

    project_key: str
    projects_cloned: int = 0
    config_cloned: bool = False
    knowledge_cloned: list[str] = field(default_factory=list)
    artifacts_copied: list[str] = field(default_factory=list)
    #: Titles of the ``BusinessSource`` rows copied (#831).
    business_sources_cloned: list[str] = field(default_factory=list)
    #: How many ``BusinessFact`` rows were copied.
    business_facts_cloned: int = 0


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


def _shared_project_guids(projects: list[Project], config: ProjectConfig | None) -> list[str]:
    """The project GUIDs the shared rows for this key are addressed by (#585).

    Business Knowledge is keyed on ``project_guid``, not on the project name, so
    the clone has to translate the key it was given into the identity those rows
    carry. Both places a GUID can live are read — the shared ``Project`` row(s)
    and the shared ``ProjectConfig`` — because the G1 bridge leaves
    ``ProjectConfig.project_guid`` nullable and a config created before it was
    stamped still points at a real project.

    Args:
        projects: The shared ``Project`` rows for the key.
        config: The shared ``ProjectConfig``, if there is one.

    Returns:
        Distinct GUIDs, in a stable order (never ``None``).
    """
    guids = [p.guid for p in projects if p.guid]
    if config is not None and config.project_guid:
        guids.append(config.project_guid)
    return list(dict.fromkeys(guids))


def _shared_business_sources(db: Session, guids: list[str]) -> list[BusinessSource]:
    """Shared (``owner_id IS NULL``) ``BusinessSource`` rows for ``guids`` (#831)."""
    if not guids:
        return []
    return (
        db.query(BusinessSource)
        .filter(BusinessSource.project_guid.in_(guids), BusinessSource.owner_id.is_(None))
        .order_by(BusinessSource.id)
        .all()
    )


def _shared_business_facts(db: Session, guids: list[str]) -> list[BusinessFact]:
    """Shared ``BusinessFact`` rows for ``guids`` — including source-less manual ones (#831)."""
    if not guids:
        return []
    return (
        db.query(BusinessFact)
        .filter(BusinessFact.project_guid.in_(guids), BusinessFact.owner_id.is_(None))
        .order_by(BusinessFact.id)
        .all()
    )


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


def _rescope_business_path(relative_path: str, source_id: int, new_source_id: int) -> str:
    """Rewrite one snapshot path for the cloned source's id (#831).

    ``BusinessSource.raw_path`` / ``normalized_path`` are *scope-relative*
    directories shaped ``<project-slug>/<source_id>/{raw,normalized}``
    (``business_ingest.storage``), so re-scoping them is not a prefix swap like
    :func:`_rescope_doc_path` — the owner is implied by the scope root, but the
    **source id** is embedded and the clone is a new row with a new id.

    Only the id segment is replaced, and only when it is where the layout says
    it is; anything else is returned unchanged (defensive — a blank or legacy
    value must not be turned into a path that points somewhere real).

    Args:
        relative_path: The stored scope-relative path.
        source_id: The shared row's id, as it appears in the path.
        new_source_id: The cloned row's id.

    Returns:
        The path the cloned row should carry.
    """
    if not relative_path:
        return relative_path
    parts = relative_path.replace("\\", "/").split("/")
    if len(parts) < 2 or parts[1] != str(source_id):
        return relative_path
    parts[1] = str(new_source_id)
    return "/".join(parts)


def _copy_business_snapshot(
    project_key: str, source_id: int, new_source_id: int, dest_owner_id: int | None
) -> bool:
    """Copy one shared source's snapshot directory into the destination scope.

    ``<shared>/business/<slug>/<source_id>/`` → ``<dest>/business/<slug>/<new_source_id>/``,
    which carries both the raw bytes and the normalized markdown in one pass.
    No-op (returns ``False``) when the shared source never landed a snapshot —
    a ``pending`` or ``error`` source is a legitimate row with no files behind it.
    """
    src = scoped_business_dir(None) / slug(project_key) / str(source_id)
    if not src.exists():
        return False
    dst = scoped_business_dir(dest_owner_id) / slug(project_key) / str(new_source_id)
    shutil.copytree(src, dst, dirs_exist_ok=True)
    return True


def _clone_business_knowledge(
    db: Session,
    project_key: str,
    sources: list[BusinessSource],
    facts: list[BusinessFact],
    dest_owner_id: int | None,
    result: CloneResult,
) -> bool:
    """Copy the shared Business Knowledge rows and their snapshots (#831, ADR 0016).

    Mirrors how ``ProjectKnowledge`` is cloned — new rows, ``owner_id``
    re-stamped, the project identity (here ``project_guid``) unchanged — with
    two differences the model forces:

    * **Ids are part of the artifact layout**, so the rows must be flushed to
      get their ids before the files can be copied, and each cloned row's
      ``raw_path``/``normalized_path`` is rewritten onto the new id. The flush
      is not a commit: a file-copy failure still leaves an uncommitted session,
      exactly like the ProjectKnowledge path.
    * **Nothing here de-duplicates itself.** ``uq_business_source_project_kind_url``
      covers ``(project_guid, owner_id, kind, url)`` and ``url`` is NULL for an
      upload — NULLs compare distinct — so a second clone of an upload would be
      accepted by the database. :func:`business_source_service.find_duplicate`
      is consulted per source instead, which is the same rule the registry
      applies when a member adds one by hand.

    ``connection_id`` is dropped for the same reason the config's provider
    bindings are: it points at the admin's own connection, which the destination
    owner cannot see. ``secrets`` (a wiki-scoped PAT, #822) is **not** copied —
    ADR 0009 §4 copies the shared namespace's secrets because they are
    project credentials the clone needs to be runnable, but a per-source wiki
    token is the admin's own credential and a re-sync by the member should ask
    for theirs. The snapshot is already copied, so the clone is complete
    without it; only a *re-sync* needs the token.

    ``superseded_by`` is remapped onto the cloned fact rows, so a human
    correction that overrides an ingested fact still overrides the *clone's*
    copy of it rather than dangling at the admin's row.

    Args:
        db: Active session — rows are added and flushed, never committed here.
        project_key: The project name, for the on-disk slug.
        sources: The shared ``BusinessSource`` rows to copy.
        facts: The shared ``BusinessFact`` rows to copy.
        dest_owner_id: The cloning user's id (``None`` when auth is disabled).
        result: Mutated with what was copied.

    Returns:
        True if any snapshot files were copied (for ``artifacts_copied``).
    """
    source_id_map: dict[int, int] = {}
    for row in sources:
        if business_source_service.find_duplicate(
            db, row.project_guid or "", dest_owner_id, row.kind, row.url, row.title
        ):
            logger.info(
                "clone %s: business source %r already exists for owner %s — skipped",
                project_key,
                row.title,
                dest_owner_id,
            )
            continue
        clone = BusinessSource(
            project_guid=row.project_guid,
            project_key=row.project_key or project_key,
            owner_id=dest_owner_id,
            kind=row.kind,
            title=row.title,
            url=row.url,
            connection_id=None,
            status=row.status,
            last_error=row.last_error,
            fetched_at=row.fetched_at,
            content_hash=row.content_hash,
            byte_size=row.byte_size,
            doc_count=row.doc_count,
            excluded=row.excluded,
            secrets={},
        )
        db.add(clone)
        db.flush()
        source_id_map[row.id] = clone.id
        result.business_sources_cloned.append(row.title)

    artifacts = False
    for old_id, new_id in source_id_map.items():
        if _copy_business_snapshot(project_key, old_id, new_id, dest_owner_id):
            artifacts = True

    for row in sources:
        new_id = source_id_map.get(row.id)
        if new_id is None:
            continue
        clone = db.get(BusinessSource, new_id)
        clone.raw_path = _rescope_business_path(row.raw_path, row.id, new_id)
        clone.normalized_path = _rescope_business_path(row.normalized_path, row.id, new_id)

    fact_id_map: dict[int, int] = {}
    for fact in facts:
        clone_fact = BusinessFact(
            project_guid=fact.project_guid,
            owner_id=dest_owner_id,
            # A fact whose source was skipped as a duplicate keeps no source
            # link rather than pointing at the admin's row (the column is
            # ON DELETE SET NULL, so NULL is already its "no source" state).
            source_id=source_id_map.get(fact.source_id) if fact.source_id else None,
            category=fact.category,
            term=fact.term,
            statement=fact.statement,
            detail=fact.detail,
            origin=fact.origin,
            pinned=fact.pinned,
            excluded=fact.excluded,
            rank_text=fact.rank_text,
        )
        db.add(clone_fact)
        db.flush()
        fact_id_map[fact.id] = clone_fact.id
        result.business_facts_cloned += 1

    for fact in facts:
        if not fact.superseded_by:
            continue
        clone_id = fact_id_map.get(fact.id)
        if clone_id is None:
            continue
        db.get(BusinessFact, clone_id).superseded_by = fact_id_map.get(fact.superseded_by)

    return artifacts


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

    The project's **Business Knowledge** (ADR 0016, #831) travels with it:
    every shared ``BusinessSource`` and ``BusinessFact`` for the project's GUID
    is copied with ``owner_id`` re-stamped, and each source's snapshot
    directory under ``business/`` is copied into the destination scope — so the
    clone is grounded in the same documents without re-fetching them. Sources
    the destination already has are skipped
    (:func:`business_source_service.find_duplicate`), and per-source
    ``secrets``/``connection_id`` are dropped — see
    :func:`_clone_business_knowledge`. **Hub mirroring is not implemented**;
    see the module docstring.

    On-disk ``knowledge/``, ``repos/``, ``auth/`` and ``business/`` subtrees are
    copied from the shared scope to the destination scope, preserving the
    ``<slug(project_key)>/…`` structure; each cloned ``ProjectKnowledge``'s
    ``doc_path`` is rewritten to the copied destination directory.

    Files are copied *before* any DB row is created, and nothing is committed
    until every row has been added — so a file-copy failure leaves the
    database untouched (nothing to roll back) and a DB failure hasn't left a
    dangling artifact tree behind that a caller might mistake for evidence of
    a partial clone. Business Knowledge is the one case that has to flush
    first, because the cloned source's **id** is part of its snapshot path;
    the guarantee is unchanged, since a flush is not a commit.

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
    business_guids = _shared_project_guids(projects, config)
    business_sources = _shared_business_sources(db, business_guids)
    business_facts = _shared_business_facts(db, business_guids)
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
                # The Business Knowledge digest (#824) lives on the config, so it
                # follows the clone the same way the rest of the row does —
                # a cloned project's prompts are grounded without a re-distil.
                business_brief=dict(config.business_brief or {}),
                # Identity, not a binding: the clone addresses the same project
                # (#585), which is also how its Business Knowledge rows are keyed.
                project_guid=config.project_guid,
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

    if _clone_business_knowledge(
        db, project_key, business_sources, business_facts, dest_owner_id, result
    ):
        result.artifacts_copied.append("business")

    db.commit()
    return result
