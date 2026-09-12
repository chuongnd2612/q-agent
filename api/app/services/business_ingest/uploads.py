"""Uploaded-file ingestion — the second credential-free source (#818).

An upload skips ``fetch`` (its bytes arrived with the request) and joins the
shared pipeline at :func:`~app.services.business_ingest.pipeline.ingest_documents`,
so it is normalized, hashed and persisted by exactly the same code as a wiki
page. That is the point of the split: "how do the bytes arrive" is the *only*
thing a source gets to differ in.

v1 accepts ``.md`` and ``.txt`` only. PDF and DOCX need ``pypdf`` /
``python-docx`` and carry a conversion-fidelity problem of their own; they are a
separate issue (#832), not a silent "we tried our best" path here.

Limits are per file (10 MB) and per project (200 files). They are enforced in
the service, not the router, so every caller inherits them — the router
(#817) supplies the multipart plumbing and nothing else.
"""

from __future__ import annotations

from app.models.business import BusinessSource
from app.services.business_ingest.base import BusinessIngestError, FetchedDoc
from app.services.business_ingest.pipeline import ingest_documents

__all__ = [
    "UploadRejectedError",
    "ALLOWED_EXTENSIONS",
    "MAX_UPLOAD_BYTES",
    "MAX_FILES_PER_PROJECT",
    "validate_upload",
    "ingest_upload",
]


class UploadRejectedError(BusinessIngestError):
    """An upload was refused before any row was created.

    Distinct from a sync failure: there is no source row to hang the message on,
    so the caller returns it to the user directly (a 400), rather than creating a
    row that exists only to be in ``error``.
    """


#: Accepted file extensions in v1, and the media type each maps to.
ALLOWED_EXTENSIONS = {".md": "text/markdown", ".txt": "text/plain"}
#: Per-file ceiling. A business document that exceeds this is an export, not a
#: page, and splitting it is the better answer than ingesting it whole.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
#: Per-project ceiling on uploaded files.
MAX_FILES_PER_PROJECT = 200


def _basename(filename: str) -> str:
    """The bare filename from a client-supplied path, which may be a full path."""
    return str(filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()


def validate_upload(filename: str, data: bytes) -> tuple[str, str]:
    """Check one uploaded file against the v1 limits.

    :param filename: The client-supplied name; any directory part is dropped.
    :param data: The file's bytes.
    :returns: ``(basename, media type)`` — the media type drives normalization.
    :raises UploadRejectedError: for a missing name, an unsupported extension,
        an empty file, or one over :data:`MAX_UPLOAD_BYTES`.
    """
    name = _basename(filename)
    if not name:
        raise UploadRejectedError("the upload has no filename")
    extension = name[name.rfind(".") :].lower() if "." in name else ""
    media_type = ALLOWED_EXTENSIONS.get(extension)
    if media_type is None:
        accepted = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise UploadRejectedError(
            f"{name} is not a supported document — this version accepts {accepted} only"
        )
    if not data:
        raise UploadRejectedError(f"{name} is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        limit_mb = MAX_UPLOAD_BYTES // (1024 * 1024)
        raise UploadRejectedError(f"{name} is larger than {limit_mb} MB")
    return name, media_type


def _existing_upload(db, project_guid: str | None, owner_id: int | None, name: str):
    """The row a re-upload of ``name`` should replace, if there is one.

    ``BusinessSource.url`` is NULL for an upload, so the table's unique
    constraint deliberately does not de-duplicate uploads (NULLs compare
    distinct in both SQLite and PostgreSQL). De-duplication for this kind is
    therefore a service-layer decision, and it is made on the filename: the same
    file uploaded again is the same document, so it **replaces** — which keeps
    the row id, and therefore the on-disk snapshot directory and every fact
    already distilled from it, while producing a new content hash.
    """
    return (
        db.query(BusinessSource)
        .filter(
            BusinessSource.project_guid == project_guid,
            BusinessSource.owner_id == owner_id,
            BusinessSource.kind == "upload",
            BusinessSource.title == name,
        )
        .first()
    )


def upload_count(db, project_guid: str | None, owner_id: int | None) -> int:
    """How many uploaded sources this project already holds for this owner."""
    return (
        db.query(BusinessSource)
        .filter(
            BusinessSource.project_guid == project_guid,
            BusinessSource.owner_id == owner_id,
            BusinessSource.kind == "upload",
        )
        .count()
    )


def ingest_upload(
    db,
    *,
    project_guid: str | None,
    project_key: str,
    owner_id: int | None,
    filename: str,
    data: bytes,
) -> BusinessSource:
    """Ingest one uploaded document, creating or replacing its source row.

    Synchronous on purpose: there is no network call, so the work is a decode
    and a write, and making the caller poll for that would be ceremony. The
    thread-and-guard convention in
    :mod:`~app.services.business_ingest.pipeline` is for sources that fetch.

    :param project_guid: Owning project's GUID (ADR 0013 / #585).
    :param project_key: Project name, for display and for the on-disk directory.
    :param owner_id: Row owner; ``None`` writes the shared namespace.
    :param filename: The client-supplied filename.
    :param data: The file's bytes.
    :returns: The created or replaced ``BusinessSource``, already ``synced``
        (or ``error`` if the file held nothing readable).
    :raises UploadRejectedError: when the file or the project's capacity fails
        the v1 limits — raised *before* any row is written.
    """
    name, media_type = validate_upload(filename, data)

    source = _existing_upload(db, project_guid, owner_id, name)
    if source is None:
        if upload_count(db, project_guid, owner_id) >= MAX_FILES_PER_PROJECT:
            raise UploadRejectedError(
                f"this project already holds {MAX_FILES_PER_PROJECT} uploaded documents — "
                "remove one before adding another"
            )
        source = BusinessSource(
            project_guid=project_guid,
            project_key=project_key,
            owner_id=owner_id,
            kind="upload",
            title=name,
            url=None,
            status="syncing",
        )
        db.add(source)
    else:
        source.status = "syncing"
        source.last_error = ""
        source.project_key = project_key
    db.commit()
    db.refresh(source)

    doc = FetchedDoc(path=name, title=name, raw_bytes=data, content_type=media_type)
    return ingest_documents(db, source, [doc])
