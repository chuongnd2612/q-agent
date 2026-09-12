"""The HTTP surface that *triggers* Business Knowledge ingestion (#818, epic #813).

:mod:`app.services.business_ingest` is the pipeline; this module is the two
doors into it, and nothing else:

* ``POST /projects/{project_guid}/business/sources/upload`` — multipart, one
  ``.md``/``.txt`` file, following the precedent in
  :mod:`app.routers.agent` (``push_job_evidence``). Synchronous: an upload has
  already arrived, so the work is a decode and a write and making the client
  poll for that would be ceremony.
* ``POST /projects/{project_guid}/business/sources/{source_id}/sync`` — fetch a
  link-backed source. Asynchronous (202 + a daemon thread), because a fetch can
  take the adapter's full 20-second timeout per document, and the UI polls the
  row's ``status`` exactly as it polls a knowledge build.

**Why this is its own module rather than routes added to
``routers/business_knowledge.py``.** That router is #817, landing in parallel on
its own branch; it owns the source *registry* (list/create/patch/delete) and
deliberately never fetches one. Ingestion is a separate concern with a separate
failure surface, and keeping it in a separate file is what lets the two slices
merge without touching each other's lines. They share a path prefix on purpose —
one URL space for Business Knowledge — and FastAPI composes two routers under
the same prefix without issue since no path is declared twice.

Once #817 has landed, two follow-ups are worth doing and neither blocks this
slice: fold :func:`_resolve_project` into the one copy in that module (it is
the same #585 GUID-or-name bridge), and have its response model replace the
local :class:`IngestedSourceOut`.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps_auth import current_user
from app.models.business import BusinessSource
from app.models.project import Project
from app.models.user import User
from app.services import project_config_service
from app.services.business_ingest import pipeline, uploads
from app.services.business_ingest.base import BusinessIngestError
from app.services.business_ingest.uploads import MAX_UPLOAD_BYTES, UploadRejectedError
from app.services.ownership import check_owned_or_404

router = APIRouter(prefix="/projects/{project_guid}/business", tags=["business"])


class IngestedSourceOut(BaseModel):
    """The ingestion-relevant projection of a :class:`BusinessSource`.

    Deliberately declared here rather than in ``app/schemas.py``: #817 is adding
    a fuller ``BusinessSourceOut`` to that file in parallel, and a second edit to
    the same region would be a merge conflict over a model that is going to be
    deleted in favour of that one anyway.

    ``lastError`` is populated on a **synced** source too. That is not a
    contradiction: a multi-document source whose readable pages landed is genuinely
    usable, and the "N documents could not be read" detail is how the drop is kept
    visible instead of silent.
    """

    id: int
    kind: str
    title: str
    url: str | None = None
    status: str
    lastError: str = ""
    contentHash: str = ""
    docCount: int = 0
    byteSize: int = 0
    fetchedAt: datetime | None = None

    @classmethod
    def of(cls, row: BusinessSource) -> "IngestedSourceOut":
        """Project a row, without exposing its on-disk paths to the client."""
        return cls(
            id=row.id,
            kind=row.kind,
            title=row.title,
            url=row.url,
            status=row.status,
            lastError=row.last_error or "",
            contentHash=row.content_hash or "",
            docCount=row.doc_count or 0,
            byteSize=row.byte_size or 0,
            fetchedAt=row.fetched_at,
        )


def _resolve_project(db: Session, project_guid: str, user: User | None) -> tuple[str, str]:
    """The ``(guid, name)`` of the project the path addresses.

    Accepts a GUID **or** a name — the #585 bridge every other project route
    carries — so the column always ends up holding a GUID whichever the client
    sent. Owner-scoped: a GUID resolves only to a project the caller may see.

    :raises HTTPException: 404 when no project the caller may see matches.
    """
    query = db.query(Project)
    if project_config_service.looks_like_guid(project_guid):
        query = query.filter(Project.guid == project_guid)
    else:
        query = query.filter(Project.name == project_guid)
    if user is not None:
        query = query.filter((Project.owner_id == user.id) | (Project.owner_id.is_(None)))
    project = query.first()
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_guid}' not found")
    return project.guid, project.name


def _source_or_404(db: Session, guid: str, source_id: int, user: User | None) -> BusinessSource:
    """The source ``source_id``, proven to belong to ``guid`` and to ``user``.

    Missing and forbidden are indistinguishable — both 404, per ADR 0008/0009.
    """
    row = db.get(BusinessSource, source_id)
    if row is None or row.project_guid != guid:
        raise HTTPException(status_code=404, detail="Business source not found")
    check_owned_or_404(row, user, not_found="Business source not found")
    return row


@router.post("/sources/upload", response_model=IngestedSourceOut, status_code=201)
async def upload_business_document(
    project_guid: str,
    file: UploadFile = File(...),
    title: str = Form(default=""),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> IngestedSourceOut:
    """Ingest one uploaded document, synchronously, creating or replacing its row.

    Re-uploading the same filename **replaces** the snapshot and keeps the row
    id, so the on-disk directory and everything already distilled from it survive
    while the content hash changes — which is the same staleness signal a link
    gets (#830).

    The v1 limits (``.md``/``.txt`` only, 10 MB per file, 200 files per project)
    are enforced in :mod:`app.services.business_ingest.uploads`, not here, so
    every caller inherits them; this handler only turns their rejection into a
    400 the user can read.

    :raises HTTPException: 400 for a rejected file, 404 for a project that is
        not the caller's.
    """
    guid, name = _resolve_project(db, project_guid, user)
    data = await file.read()
    # Read-then-check rather than a Content-Length check: the declared length is
    # client-controlled, and Starlette has already spooled the part to a temp
    # file, so this bounds memory no worse than the framework already did.
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"{file.filename} is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
        )
    try:
        row = uploads.ingest_upload(
            db,
            project_guid=guid,
            project_key=name,
            owner_id=user.id if user is not None else None,
            filename=(title.strip() or file.filename or ""),
            data=data,
        )
    except UploadRejectedError as exc:
        # Refused before any row was written, so there is nothing to hang the
        # message on — it goes straight back to the user.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BusinessIngestError as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return IngestedSourceOut.of(row)


@router.post("/sources/{source_id}/sync", response_model=IngestedSourceOut, status_code=202)
def sync_business_source(
    project_guid: str,
    source_id: int,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> IngestedSourceOut:
    """Start (or re-start) the ingestion of a link-backed source.

    202 and a daemon thread, mirroring ``knowledge_service.start_build``: the
    fetch may take the adapter's full timeout, and there is no job scheduler in
    this codebase to hand it to. The client polls the row.

    An ingestion **failure** is not an error response — it is the row's next
    state, which is the only form the user ever sees it in. The 4xx cases here
    are the ones where there is nothing to start at all: an upload (which has no
    address to re-fetch from) and a sync already in flight.

    :raises HTTPException: 400 for an upload, 404 for an unreachable source,
        409 when this source is already syncing.
    """
    guid, _ = _resolve_project(db, project_guid, user)
    row = _source_or_404(db, guid, source_id, user)

    if row.kind == "upload":
        raise HTTPException(
            status_code=400,
            detail="An uploaded file has no address to re-sync from — upload the new "
            "version to replace it.",
        )
    if pipeline.is_syncing(row.id):
        raise HTTPException(status_code=409, detail="This source is already syncing.")

    row.status = "syncing"
    row.last_error = ""
    db.commit()
    db.refresh(row)
    # Credential-free in this slice: `url` needs no secret. #821/#822 resolve one
    # from `row.connection_id` here, on the request thread that has the context
    # to resolve it, and pass it in — the adapter never resolves its own secret.
    pipeline.start_sync(row.id)
    return IngestedSourceOut.of(row)
