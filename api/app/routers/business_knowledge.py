"""Business Knowledge sources, keyed on the project GUID (#817, epic #813).

``ProjectKnowledge`` answers *how the product is built*; these rows answer *what
it is supposed to do* — the question a QC actually writes a test case from. ADR
0016 makes them a **peer** source rather than an enrichment of the code KB, and
this router is its read/write surface.

Why a separate module, for the same reasons ``automation_projects.py`` is one:

* Not ``routers/projects.py`` — every path there is keyed on ``{key}``, the
  project **name**. Business Knowledge is keyed on ``project_guid`` (ADR 0013 /
  #585), and mixing two identifiers in one path space is a footgun.
* File-disjointness is what lets #818 (ingestion) and #823 (the QC voice gate)
  land in parallel with this slice.

**This router registers sources; it never fetches one.** Every created row reads
``status="pending"`` until something starts an ingestion — which is the sync
endpoint in :mod:`app.routers.business_ingest` (#818), deliberately still an
explicit act rather than a side effect of ``POST /sources``: registering a
document should not make its 201 depend on a remote host being up, and #821/#822
add kinds whose fetch needs a connection that may be chosen after the row exists.
The SPA fires that sync itself right after a successful create, so a link the
user just added starts fetching without a second click (#845).

The two helpers this router shares with the ingestion one — the #585
GUID-or-name bridge and the per-source ownership check — live in
:mod:`app.services.business_source_service`, not here: two copies of an identity
rule drift, and a drift there is an authorisation bug (#845).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps_auth import current_user
from app.models.business import BUSINESS_SOURCE_KINDS, BusinessSource
from app.models.user import User
from app.schemas import BusinessSourceCreate, BusinessSourceOut, BusinessSourceUpdate
from app.services import business_source_service as sources
from app.services.ownership import stamp_owner

router = APIRouter(prefix="/projects/{project_guid}/business", tags=["business"])


@router.get("/sources", response_model=list[BusinessSourceOut])
def list_business_sources(
    project_guid: str,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> list[BusinessSource]:
    """Every source grounding this project, newest first, with its own status.

    Scoped to ``user`` (#93): another user's sources are not listed, and there is
    no "all sources" view to fall back to.
    """
    guid, _ = sources.resolve_project(db, project_guid, user)
    return sources.visible_sources(db, guid, user)


@router.post("/sources", response_model=BusinessSourceOut, status_code=201)
def create_business_source(
    project_guid: str,
    body: BusinessSourceCreate,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> BusinessSource:
    """Register a document that grounds this project's test authoring.

    Validation is deliberately front-loaded rather than deferred to the first
    fetch: a source that can never be fetched should be refused with a message
    the user can act on *now*, not sit in the list reading ``error`` after an
    ingestion round trip it was never going to survive.

    * ``kind`` must be one of :data:`BUSINESS_SOURCE_KINDS`. ``notion`` is
      absent on purpose (deferred to v2, #832).
    * Every kind but ``upload`` requires a parseable ``http``/``https`` URL.
    * An ``upload`` carries no URL at all; one supplied is dropped rather than
      stored, so the row's ``url IS NULL`` invariant holds whatever the client
      sends.
    * A duplicate is a 409 naming the existing source — see
      :func:`business_source_service.find_duplicate` for what "duplicate" means
      for an upload, which the unique constraint cannot express.

    Raises:
        HTTPException: 400 on a bad ``kind`` or URL, 409 on a duplicate, 404
            when the project is not the caller's to add to.
    """
    guid, name = sources.resolve_project(db, project_guid, user)

    kind = (body.kind or "").strip()
    if kind not in BUSINESS_SOURCE_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown source kind '{kind}'. Expected one of: "
            + ", ".join(BUSINESS_SOURCE_KINDS),
        )

    url: str | None = None
    if kind != "upload":
        try:
            url = sources.normalize_url(body.url or "")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # An upload's title is its identity (see `find_duplicate`); a link falls back
    # to its URL so the list never renders a blank row.
    title = (body.title or "").strip() or (url or "")
    if not title:
        raise HTTPException(status_code=400, detail="A title is required for an uploaded document.")

    owner_id = user.id if user is not None else None
    existing = sources.find_duplicate(db, guid, owner_id, kind, url, title)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"This project already has that source: '{existing.title}'.",
        )

    row = stamp_owner(
        BusinessSource(
            project_guid=guid,
            project_key=name,
            kind=kind,
            title=title[:500],
            url=url,
            connection_id=body.connection_id,
            status="pending",
        ),
        user,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.patch("/sources/{source_id}", response_model=BusinessSourceOut)
def update_business_source(
    project_guid: str,
    source_id: int,
    body: BusinessSourceUpdate,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> BusinessSource:
    """Rename a source, or take it out of context.

    ``excluded`` is **not** a soft delete: the snapshot and its provenance stay,
    so an artifact already generated from this source remains attributable while
    the source stops feeding new ones (epic #813).
    """
    guid, _ = sources.resolve_project(db, project_guid, user)
    row = sources.source_or_404(db, guid, source_id, user)

    if body.title is not None:
        title = body.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="A source title cannot be empty.")
        row.title = title[:500]
    if body.excluded is not None:
        row.excluded = body.excluded

    db.commit()
    db.refresh(row)
    return row


@router.delete("/sources/{source_id}", status_code=204)
def delete_business_source(
    project_guid: str,
    source_id: int,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> None:
    """Delete a source **and** the snapshot files under the owner's scope.

    Forgetting the artifacts would leave bytes on disk that nothing references
    and no endpoint can reach — see
    :func:`business_source_service.delete_source`, which also explains why the
    distilled facts deliberately survive.
    """
    guid, _ = sources.resolve_project(db, project_guid, user)
    row = sources.source_or_404(db, guid, source_id, user)
    sources.delete_source(db, row)
