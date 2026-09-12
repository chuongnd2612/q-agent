"""Business Knowledge source CRUD — the service layer behind the router (#817).

The registry is deliberately **CRUD only**: a source can be registered, listed,
renamed, excluded and deleted, and registering one never fetches anything —
``status="pending"`` is the state an unsynced source is genuinely in. Ingestion
(fetch → normalize → distil) lives in :mod:`app.services.business_ingest` and is
started explicitly, by the sync endpoint (#818).

Three rules live here rather than in the router, because the ingestion endpoints
(#818) must obey exactly the same ones:

1. **Who the path addresses.** :func:`resolve_project` is the one copy of the
   #585 GUID-or-name bridge, and :func:`source_or_404` the one copy of the
   per-source ownership check. Both Business Knowledge routers call them (#845);
   an identity rule with two implementations drifts, and a drift here is an
   authorisation bug rather than a cosmetic one.
2. **What "the same source" means.** ``uq_business_source_project_kind_url``
   covers ``(project_guid, owner_id, kind, url)``, and ``url`` is NULL for an
   upload — NULLs compare distinct in a unique index on both SQLite and
   PostgreSQL, so the constraint de-duplicates *links* and is inert for uploads.
   See :func:`find_duplicate` for what is done about that.
3. **Deleting a source deletes its bytes.** The row's ``raw_path`` /
   ``normalized_path`` are the only references to files under
   ``scoped_business_dir(owner_id)``; dropping the row without them would leave
   an unreachable snapshot on disk forever.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.logging import logger
from app.models.business import BUSINESS_SOURCE_KINDS, BusinessSource
from app.models.project import Project
from app.models.user import User
from app.services import project_config_service
from app.services.ownership import check_owned_or_404
from app.services.workspace_scope import scoped_business_dir

#: Kinds whose identity *is* their address. An ``upload`` is the exception: it
#: has no URL at all, which is what makes rule (2) above necessary.
LINK_KINDS = tuple(kind for kind in BUSINESS_SOURCE_KINDS if kind != "upload")

#: Schemes a business source may be fetched over. A ``file://`` or ``data:``
#: "URL" parses perfectly well and would hand #818's fetcher a local path, so
#: the allowlist is stated positively rather than as a blocklist.
ALLOWED_URL_SCHEMES = ("http", "https")


def resolve_project(db: Session, project_guid: str, user: User | None) -> tuple[str, str]:
    """The ``(guid, name)`` of the project a Business Knowledge path addresses.

    Accepts a GUID **or** a name, the same #585 bridge every other project route
    carries: the SPA sends GUIDs, but an older deep link or a test fixture may
    send the name, and a row keyed on whichever string arrived would put names
    into ``project_guid``. Resolving here means the column always holds a GUID.

    Owner-scoped: a GUID resolves only to a project the caller may see, so it
    cannot be used to discover another user's project name.

    Lives in the service rather than in a router because **both** Business
    Knowledge routers need it — the registry (``routers/business_knowledge.py``,
    #817) and ingestion (``routers/business_ingest.py``, #818). Those two shipped
    in parallel with a copy each (#845); one identity rule with two
    implementations is the thing that drifts, and a drift here is an
    authorisation bug, not a cosmetic one.

    Args:
        db: Active session.
        project_guid: The path identifier — GUID or name.
        user: The caller, or ``None`` under the #91 ownership bridge.

    Returns:
        The project's GUID and its display name.

    Raises:
        HTTPException: 404 when no project the caller may see matches.
    """
    query = db.query(Project)
    if project_config_service.looks_like_guid(project_guid):
        query = query.filter(Project.guid == project_guid)
    else:
        query = query.filter(Project.name == project_guid)
    if user is not None:
        # Own rows or shared/legacy ones (owner_id NULL), matching
        # `ownership._ownership_mismatch` — never someone else's.
        query = query.filter((Project.owner_id == user.id) | (Project.owner_id.is_(None)))
    project = query.first()
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_guid}' not found")
    return project.guid, project.name


def source_or_404(
    db: Session, guid: str, source_id: int, user: User | None
) -> BusinessSource:
    """The source ``source_id``, proven to belong to ``guid`` and to ``user``.

    Three checks, and all three have to be here rather than at the call sites:

    1. The row exists.
    2. It belongs to the project named in the path — so a source id that exists
       under a *different* project cannot be reached by naming this one.
    3. ``check_owned_or_404`` rejects another user's row. Missing and forbidden
       are indistinguishable (both 404, per ADR 0008/0009): a 403 would confirm
       the row exists to somebody not allowed to know that.

    Shared by both Business Knowledge routers, for the reason given on
    :func:`resolve_project`.

    Args:
        db: Active session.
        guid: The resolved project GUID.
        source_id: The ``BusinessSource`` id, from the path.
        user: The caller, or ``None`` under the #91 ownership bridge.

    Returns:
        The resolved :class:`BusinessSource`.

    Raises:
        HTTPException: 404 in every failing case.
    """
    row = db.get(BusinessSource, source_id)
    if row is None or row.project_guid != guid:
        raise HTTPException(status_code=404, detail="Business source not found")
    check_owned_or_404(row, user, not_found="Business source not found")
    return row


def normalize_url(raw: str) -> str:
    """Validate a source URL and return it stripped, or raise ``ValueError``.

    Args:
        raw: The caller-supplied address.

    Returns:
        The URL with surrounding whitespace removed.

    Raises:
        ValueError: When the URL is blank, carries no ``http``/``https`` scheme,
            or names no host. The message is written to be shown to a user.
    """
    value = (raw or "").strip()
    if not value:
        raise ValueError("A URL is required for this source kind.")
    parsed = urlparse(value)
    if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES:
        raise ValueError("A source URL must start with http:// or https://.")
    if not parsed.netloc:
        raise ValueError("That URL has no host — check it and try again.")
    return value


def visible_sources(db: Session, project_guid: str, user: User | None) -> list[BusinessSource]:
    """Every source of ``project_guid`` that ``user`` may see, newest first.

    Scoped the way :func:`app.services.project_config_service.get_config_visible_to`
    is: the caller's own rows plus the unowned/shared ones (ADR 0009 §3), and
    never another user's. The router still re-states the check per row through
    ``check_owned_or_404`` for the single-row paths.

    Args:
        db: Active session.
        project_guid: The owning project's GUID (ADR 0013 / #585).
        user: The caller, or ``None`` under the #91 ownership bridge.

    Returns:
        The matching rows, ordered newest-created first.
    """
    query = db.query(BusinessSource).filter(BusinessSource.project_guid == project_guid)
    if user is not None:
        query = query.filter(
            (BusinessSource.owner_id == user.id) | (BusinessSource.owner_id.is_(None))
        )
    return query.order_by(BusinessSource.created_at.desc(), BusinessSource.id.desc()).all()


def find_duplicate(
    db: Session,
    project_guid: str,
    owner_id: int | None,
    kind: str,
    url: str | None,
    title: str,
) -> BusinessSource | None:
    """The existing source that ``(kind, url|title)`` would duplicate, if any.

    **Links** de-duplicate on ``url``, which is what the unique constraint
    already enforces; this looks first so the caller can answer with a message
    naming the existing source instead of an ``IntegrityError``.

    **Uploads de-duplicate on the title**, case-insensitively, and that is a
    decision worth stating rather than inheriting. An upload has no address, and
    in this slice it has no ``content_hash`` either (nothing is ingested yet), so
    its filename is the only identity it has. A second upload of
    ``eligibility-rules.md`` into one project is a re-sync of the same document
    far more often than it is a different document that happens to share a name —
    and a re-sync belongs on the existing row, where the snapshot's provenance
    lives, not on a second row that silently doubles the same rules in every
    prompt. When #818 gives a source real bytes the honest key becomes
    ``content_hash`` and this check should move to it; the shape of the answer
    does not change.

    Args:
        db: Active session.
        project_guid: The owning project's GUID.
        owner_id: The prospective owner — part of the key (ADR 0009 §3).
        kind: One of :data:`app.models.business.BUSINESS_SOURCE_KINDS`.
        url: The address, for a link kind; ``None`` for an upload.
        title: The human label, which is the upload's identity.

    Returns:
        The colliding row, or ``None`` when the source is new.
    """
    query = db.query(BusinessSource).filter(
        BusinessSource.project_guid == project_guid,
        BusinessSource.kind == kind,
    )
    # `is_()` rather than `== None`: owner_id is nullable and SQL NULL never
    # equals itself, so the shared-namespace row would otherwise never match.
    query = (
        query.filter(BusinessSource.owner_id.is_(None))
        if owner_id is None
        else query.filter(BusinessSource.owner_id == owner_id)
    )
    if kind == "upload":
        cleaned = (title or "").strip().lower()
        if not cleaned:
            return None
        return next(
            (row for row in query.all() if (row.title or "").strip().lower() == cleaned), None
        )
    return query.filter(BusinessSource.url == url).first()


def _artifact_paths(source: BusinessSource) -> list[Path]:
    """Absolute paths of ``source``'s stored artifacts that are safe to remove.

    ``raw_path`` / ``normalized_path`` are workspace-relative and written by our
    own ingestion — but they are *stored* values, and a stored value is exactly
    the kind of input that should not be joined onto a filesystem root unchecked.
    Any path that escapes ``scoped_business_dir(owner_id)`` once resolved is
    dropped, so a malformed or hostile row can delete nothing outside the owner's
    scope.

    Args:
        source: The row being deleted.

    Returns:
        Existing files inside the owner's business scope, in no particular order.
    """
    root = scoped_business_dir(source.owner_id).resolve()
    out: list[Path] = []
    for relative in (source.raw_path, source.normalized_path):
        if not relative:
            continue
        try:
            candidate = (root / relative).resolve()
            candidate.relative_to(root)
        except (OSError, ValueError):
            logger.warning("business source %s: refusing artifact path %r", source.id, relative)
            continue
        if candidate.is_file():
            out.append(candidate)
    return out


def delete_source(db: Session, source: BusinessSource) -> None:
    """Delete ``source`` and the snapshot files it is the only reference to.

    Artifacts are removed **before** the row, and a failure to unlink one is
    logged rather than raised: a file that cannot be deleted must not leave the
    user holding a row they can never get rid of. Empty parent directories inside
    the owner's business scope are pruned afterwards so a removed project does
    not leave a tree of empty folders behind.

    ``BusinessFact.source_id`` is ``ON DELETE SET NULL`` by design (see
    ``app.models.business``), so distilled facts survive as orphans rather than
    silently taking a pinned human correction down with them — reaping them is
    #827's call to make, with ``origin``/``pinned`` in hand.

    Args:
        db: Active session. Committed by this function.
        source: The row to delete.
    """
    root = scoped_business_dir(source.owner_id).resolve()
    parents: set[Path] = set()
    for path in _artifact_paths(source):
        try:
            path.unlink()
            parents.add(path.parent)
        except OSError as exc:  # pragma: no cover - filesystem-dependent
            logger.warning("business source %s: could not delete %s (%s)", source.id, path, exc)

    for parent in parents:
        try:
            if parent != root and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:  # pragma: no cover - filesystem-dependent
            pass

    db.delete(source)
    db.commit()
