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
``routers/business_knowledge.py``.** That router is #817; it owns the source
*registry* (list/create/patch/delete) and deliberately never fetches one.
Ingestion is a separate concern with a separate failure surface, and keeping it
in a separate file is what let the two slices land in parallel without touching
each other's lines. They share a path prefix on purpose —
one URL space for Business Knowledge — and FastAPI composes two routers under
the same prefix without issue since no path is declared twice.

Both slices having landed, the seams between them are closed (#845): the #585
GUID-or-name bridge and the per-source ownership check are one copy each in
:mod:`app.services.business_source_service`, and both endpoints answer with the
shared :class:`app.schemas.BusinessSourceOut` rather than a local stand-in.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.db import get_db, utcnow
from app.deps_auth import current_user
from app.models.business import BusinessSource
from app.models.user import User
from app.schemas import BusinessSourceOut
from app.services import business_source_service as sources
from app.services.business_ingest import credentials, pipeline, staleness, uploads
from app.services.business_ingest.base import BusinessIngestError, SourceFetchError
from app.services.business_ingest.uploads import MAX_UPLOAD_BYTES, UploadRejectedError

router = APIRouter(prefix="/projects/{project_guid}/business", tags=["business"])


@router.post("/sources/upload", response_model=BusinessSourceOut, status_code=201)
async def upload_business_document(
    project_guid: str,
    file: UploadFile = File(...),
    title: str = Form(default=""),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> BusinessSource:
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
    guid, name = sources.resolve_project(db, project_guid, user)
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
    return row


@router.post("/sources/{source_id}/sync", response_model=BusinessSourceOut, status_code=202)
def sync_business_source(
    project_guid: str,
    source_id: int,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> BusinessSource:
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
    guid, _ = sources.resolve_project(db, project_guid, user)
    row = sources.source_or_404(db, guid, source_id, user)

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
    return row


@router.post("/sources/{source_id}/probe", response_model=BusinessSourceOut)
def probe_business_source(
    project_guid: str,
    source_id: int,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> BusinessSource:
    """Check whether this source's upstream document has moved since the snapshot.

    Synchronous, unlike ``/sync``, and that difference is the design: a probe is
    one cheap request that downloads no document bytes at all (a commit SHA, a
    content-free wiki page tree, an ``ETag``), so making the client poll for it
    would be ceremony. A *fetch* is the thing that can take the adapter's full
    timeout per document.

    **A probe that cannot answer is a 200, not an error.** "GitHub rate-limited
    us", "this page sends no ETag" and "an upload has no address" are all
    recorded on the row as ``probeError``, which is exactly what makes the UI
    fall back to the honest age label ("last fetched 34 days ago") instead of
    claiming a change it cannot see (#830, ADR 0016 §4). The only 4xx here is an
    unreachable source.

    :raises HTTPException: 404 when the source is not the caller's.
    """
    guid, _ = sources.resolve_project(db, project_guid, user)
    row = sources.source_or_404(db, guid, source_id, user)

    credential = None
    if staleness.probe_supported(row.kind):
        try:
            credential = credentials.resolve_credential(db, row)
        except SourceFetchError as exc:
            # Resolved here rather than inside the probe so the adapter contract
            # holds (an adapter never resolves its own secret), and a missing
            # credential lands on the row as a probe failure rather than a 500.
            row.probed_at = utcnow()
            row.probe_error = str(exc)[:1000]
            db.commit()
            db.refresh(row)
            return row
    return staleness.refresh_staleness(db, row, credential)
