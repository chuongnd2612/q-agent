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
from app.models.business import BUSINESS_SOURCE_KINDS, BusinessFact, BusinessSource
from app.models.user import User
from app.schemas import (
    BusinessFactCorrection,
    BusinessFactCreate,
    BusinessFactOut,
    BusinessFactUpdate,
    BusinessSourceCreate,
    BusinessSourceOut,
    BusinessSourceUpdate,
)
from app.services import business_fact_service as facts
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


# --------------------------------------------------------- The fact overlay (#827)
# Ingested content is immutable, so none of these endpoints edits a distilled
# fact in place. They add rows *around* it: a pinned correction that supersedes
# it, an addition the documents never carried, or an exclusion that takes a row
# out of context while leaving it on disk. ADR 0016 §5 is the precedence ladder
# they implement, and `business_fact_service` is where it lives — the router is
# validation and status codes only.


@router.get("/facts", response_model=list[BusinessFactOut])
def list_business_facts(
    project_guid: str,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> list[BusinessFact]:
    """Every fact grounding this project, highest precedence first.

    Superseded and excluded rows are **included**: this is the management view,
    and the Business tab renders a superseded original struck through beside the
    correction that beat it. The prompt view is
    ``business_fact_service.facts_in_context``, which drops both.
    """
    guid, _ = sources.resolve_project(db, project_guid, user)
    return facts.visible_facts(db, guid, user)


@router.post("/facts", response_model=BusinessFactOut, status_code=201)
def create_business_fact(
    project_guid: str,
    body: BusinessFactCreate,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> BusinessFact:
    """Add a fact the documents never stated.

    Not pinned — pinning marks a row as overriding a source, and an addition
    overrides nothing. It survives a re-sync regardless: the merge refuses to
    write over any human-authored row (ADR 0016 §5, layer 2).

    Raises:
        HTTPException: 400 on an unknown category or an empty term/statement.
    """
    guid, _ = sources.resolve_project(db, project_guid, user)
    category = facts.validate_category(body.category)
    term = (body.term or "").strip()
    statement = (body.statement or "").strip()
    if not term or not statement:
        raise HTTPException(
            status_code=400, detail="A fact needs both a term and a statement."
        )
    return facts.add_fact(
        db,
        project_guid=guid,
        owner_id=user.id if user is not None else None,
        category=category,
        term=term,
        statement=statement,
        detail=(body.detail or "").strip(),
        updated_by=user.id if user is not None else None,
        user=user,
    )


@router.post("/facts/{fact_id}/correct", response_model=BusinessFactOut, status_code=201)
def correct_business_fact(
    project_guid: str,
    fact_id: int,
    body: BusinessFactCorrection,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> BusinessFact:
    """Override a fact with a pinned correction — a new row, never an edit.

    201, not 200: a correction is a *created* resource with its own id, and the
    fact it supersedes is still there. That is the point — the original stays
    visible, struck through, so a reader can see where the source document and
    the team disagree instead of the disagreement being silently resolved.

    Raises:
        HTTPException: 400 on an empty statement, 404 when the fact is not the
            caller's to correct, 409 when it has already been superseded (the
            correction belongs on the row that is actually in context).
    """
    guid, _ = sources.resolve_project(db, project_guid, user)
    original = facts.fact_or_404(db, guid, fact_id, user)
    statement = (body.statement or "").strip()
    if not statement:
        raise HTTPException(status_code=400, detail="A correction needs a statement.")
    if original.superseded_by is not None:
        raise HTTPException(
            status_code=409,
            detail="That fact has already been corrected — correct the correction instead.",
        )
    return facts.correct_fact(
        db,
        original,
        statement=statement,
        detail=(body.detail or "").strip(),
        updated_by=user.id if user is not None else None,
    )


@router.patch("/facts/{fact_id}", response_model=BusinessFactOut)
def update_business_fact(
    project_guid: str,
    fact_id: int,
    body: BusinessFactUpdate,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> BusinessFact:
    """Edit a manual fact, or take any fact out of context (and back into it).

    ``excluded`` is not a delete: the row stays, so anything already generated
    from it remains attributable and one click restores it. Editing the
    *content* of an ingested fact is refused with a 400 pointing at the
    correction endpoint.
    """
    guid, _ = sources.resolve_project(db, project_guid, user)
    row = facts.fact_or_404(db, guid, fact_id, user)
    statement = body.statement.strip() if body.statement is not None else None
    if statement is not None and not statement:
        raise HTTPException(status_code=400, detail="A fact statement cannot be empty.")
    return facts.update_fact(
        db,
        row,
        statement=statement,
        detail=body.detail.strip() if body.detail is not None else None,
        excluded=body.excluded,
        updated_by=user.id if user is not None else None,
    )
