"""Where a Business Knowledge snapshot lives on disk, and how it is hashed (#818).

Layout, under the per-owner scope resolved by
:func:`app.services.workspace_scope.scoped_business_dir` (ADR 0009)::

    workspace/<scope>/business/<project-slug>/<source_id>/raw/<doc-path>
    workspace/<scope>/business/<project-slug>/<source_id>/normalized/<doc-path>.md

**Raw is kept as well as normalized**, and that is a decision with teeth: a
re-normalize must never require a re-fetch (which for #821/#822 means a
re-authenticate), and the raw bytes are what let a future parser improvement be
applied retroactively to every document already ingested. Keyed on the source's
primary key rather than its title so a rename never orphans a snapshot.

``BusinessSource.raw_path`` / ``normalized_path`` store the **directory**, not a
file, relative to ``scoped_business_dir(owner_id)`` — a wiki source holds many
documents and a single-page source is just the one-document case of that.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.services import workspace_scope
from app.services.business_ingest.base import FetchedDoc

__all__ = ["PersistedSnapshot", "source_root", "persist_snapshot", "content_hash_for"]


@dataclass(frozen=True)
class PersistedSnapshot:
    """What :func:`persist_snapshot` wrote, in the shape the row needs.

    :param raw_path: Workspace-scope-relative directory holding the raw bytes.
    :param normalized_path: Same, for the normalized markdown.
    :param content_hash: SHA-256 over the normalized documents (see
        :func:`content_hash_for`).
    :param byte_size: Total raw bytes stored.
    :param doc_count: How many documents landed.
    """

    raw_path: str
    normalized_path: str
    content_hash: str
    byte_size: int
    doc_count: int


def _safe_relative(path: str) -> str:
    """Reduce an adapter-supplied document path to a safe relative POSIX path.

    Adapter output is upstream-controlled (a wiki page path, a repository file
    path), so it is treated as hostile: drive letters, leading separators and
    ``..`` segments are removed rather than trusted. An empty result becomes
    ``"document"`` so a snapshot is never written to the source directory root.

    :param path: The adapter's :attr:`~...base.FetchedDoc.path`.
    :returns: A relative POSIX path with no ``..`` segment.
    """
    parts = [
        segment
        for segment in str(path).replace("\\", "/").split("/")
        if segment not in ("", ".", "..") and ":" not in segment
    ]
    return "/".join(parts) or "document"


def source_root(project_key: str, source_id: int, owner_id: int | None) -> Path:
    """Absolute directory holding one source's snapshot.

    :param project_key: The project name; slugged for the directory segment.
    :param source_id: ``BusinessSource.id`` — stable across a title change.
    :param owner_id: Row owner; ``None`` resolves the shared namespace.
    """
    return (
        workspace_scope.scoped_business_dir(owner_id)
        / workspace_scope.slug(project_key)
        / str(source_id)
    )


def content_hash_for(normalized: dict[str, str]) -> str:
    """SHA-256 over the normalized documents of a source.

    Computed from the **normalized** text, not the raw bytes, so that a
    cosmetic upstream change (a re-rendered timestamp in an HTML comment, a
    different charset declaration) does not read as a content change — the hash
    is the staleness signal (#830) and a noisy one would be useless.

    Path-ordered and path-inclusive, so the digest is stable across two
    identical fetches regardless of the order the adapter returned documents in,
    and changes if a document is renamed, added or removed.

    :param normalized: ``{doc path: markdown}``.
    :returns: Lower-case hex digest.
    """
    digest = hashlib.sha256()
    for path in sorted(normalized):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(normalized[path].encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def persist_snapshot(
    *,
    project_key: str,
    source_id: int,
    owner_id: int | None,
    documents: list[tuple[FetchedDoc, str]],
) -> PersistedSnapshot:
    """Write a source's raw bytes and normalized markdown, replacing any prior snapshot.

    Called only once the fetch has already succeeded, so replacing in place is
    safe: a *failed* sync never reaches here and therefore never destroys the
    snapshot an existing test case is attributed to.

    :param project_key: Project name, for the directory segment.
    :param source_id: ``BusinessSource.id``.
    :param owner_id: Row owner (``None`` = shared namespace).
    :param documents: ``(doc, normalized markdown)`` pairs — successful
        documents only; callers filter out the failures first.
    :returns: A :class:`PersistedSnapshot` to copy onto the row.
    """
    root = source_root(project_key, source_id, owner_id)
    raw_dir = root / "raw"
    normalized_dir = root / "normalized"
    for directory in (raw_dir, normalized_dir):
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True, exist_ok=True)

    normalized_by_path: dict[str, str] = {}
    byte_size = 0
    for doc, markdown in documents:
        relative = _safe_relative(doc.path)
        raw_file = raw_dir / relative
        raw_file.parent.mkdir(parents=True, exist_ok=True)
        raw_file.write_bytes(doc.raw_bytes)
        byte_size += len(doc.raw_bytes)

        normalized_file = normalized_dir / f"{relative}.md"
        normalized_file.parent.mkdir(parents=True, exist_ok=True)
        normalized_file.write_text(markdown, encoding="utf-8")
        normalized_by_path[relative] = markdown

    scope_root = workspace_scope.scoped_business_dir(owner_id)
    return PersistedSnapshot(
        raw_path=raw_dir.relative_to(scope_root).as_posix(),
        normalized_path=normalized_dir.relative_to(scope_root).as_posix(),
        content_hash=content_hash_for(normalized_by_path),
        byte_size=byte_size,
        doc_count=len(normalized_by_path),
    )
