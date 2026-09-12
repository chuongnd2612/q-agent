"""The Azure DevOps wiki credential surface — where the constraint becomes visible (#822).

Three routes, and all three exist because of one finding: **a project's existing
Azure DevOps connection usually cannot read its wiki.** A hub-backed connection
holds no PAT and never will (#501), and a locally held one was provisioned for
work items (``vso.work``), not wikis (``vso.wiki``). Left implicit, that shows up
as a source stuck on ``error`` with a 401 behind it and a user re-doing a
connection that was never broken.

So the constraint is made a surface instead of a surprise:

* ``GET  …/sources/{id}/ado-credential`` — *before* anything is fetched, say
  where this source's token would come from, and say **hub** in so many words
  when the answer is "it cannot come from your connection".
* ``PUT  …/sources/{id}/ado-credential`` — store a wiki-scoped PAT, but only
  after :func:`…ado_wiki.preflight` has proved it can actually list the wiki. A
  token that cannot read wikis is rejected here, at the field the user is
  looking at, rather than accepted and discovered later.
* ``DELETE …/sources/{id}/ado-credential`` — remove it.

**Why a new module rather than routes on ``business_knowledge.py`` /
``business_ingest.py``.** Those two are being refactored in parallel (#845), and
this is the same file-disjointness argument that let #817 and #818 land at once.
It shares their ``/projects/{project_guid}/business`` prefix — one URL space,
three concerns — and declares no path either of them declares.

A token is accepted, encrypted and never echoed: no response here contains one,
and the stored value is read back only by
:func:`app.services.business_ingest.credentials.resolve_credential`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps_auth import current_user
from app.models.business import BusinessSource
from app.models.project import Project
from app.models.user import User
from app.services import project_config_service
from app.services.business_ingest import credentials
from app.services.business_ingest.adapters import ado_wiki
from app.services.business_ingest.base import SourceFetchError
from app.services.ownership import check_owned_or_404

router = APIRouter(prefix="/projects/{project_guid}/business", tags=["business"])

#: Shown for ``origin="hub"``. The one thing the user must not conclude is "my
#: connection is broken", so the copy says what the connection *is* doing.
_ORIGIN_MESSAGES = {
    "source": "This source has its own Azure DevOps token.",
    "connection": "This source uses the Azure DevOps connection's token. If the "
    "sync fails with a scope error, add a wiki-scoped token here instead.",
    "hub": credentials.HUB_BACKED_MESSAGE,
    "missing": credentials.NO_CREDENTIAL_MESSAGE,
    "none": "This source kind needs no credential.",
}


class AdoCredentialOut(BaseModel):
    """Where a source's Azure DevOps token comes from — never the token itself.

    Declared locally rather than in ``app/schemas.py`` for the same reason
    ``business_ingest.IngestedSourceOut`` is: that file is being refactored in
    parallel (#845) and a second edit to it would be a merge conflict over a
    model only this router uses.
    """

    sourceId: int
    #: ``source`` | ``connection`` | ``hub`` | ``missing`` | ``none`` — the
    #: branch, exposed deliberately so the SPA and the tests can assert on which
    #: path was taken rather than inferring it from prose.
    origin: str
    hasToken: bool
    #: ``True`` only when a sync can actually be attempted.
    canSync: bool
    message: str


class AdoCredentialIn(BaseModel):
    """A wiki-scoped personal access token, submitted once and never returned."""

    pat: str = Field(min_length=1)


class AdoPreflightIn(BaseModel):
    """A wiki address plus the token to test it with, before either is stored."""

    url: str = Field(min_length=1)
    pat: str = Field(min_length=1)


class AdoPreflightOut(BaseModel):
    """What the token proved it could see."""

    ok: bool
    project: str
    wiki: str
    wikis: list[str]


def _resolve_project(db: Session, project_guid: str, user: User | None) -> str:
    """The GUID of the project the path addresses, accepting a GUID or a name.

    The same #585 bridge every other project route carries, owner-scoped so a
    GUID resolves only to a project the caller may see.

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
    return project.guid


def _source_or_404(db: Session, guid: str, source_id: int, user: User | None) -> BusinessSource:
    """The source, proven to belong to this project and this caller.

    Missing and forbidden are indistinguishable — both 404, per ADR 0008/0009.
    """
    row = db.get(BusinessSource, source_id)
    if row is None or row.project_guid != guid:
        raise HTTPException(status_code=404, detail="Business source not found")
    check_owned_or_404(row, user, not_found="Business source not found")
    return row


def _state(db: Session, source: BusinessSource) -> AdoCredentialOut:
    """Project a source's credential situation, without decrypting anything into it."""
    origin = credentials.credential_origin(db, source)
    return AdoCredentialOut(
        sourceId=source.id,
        origin=origin,
        hasToken=credentials.has_source_token(source),
        canSync=origin in ("source", "connection", "none"),
        message=_ORIGIN_MESSAGES.get(origin, credentials.NO_CREDENTIAL_MESSAGE),
    )


@router.get("/sources/{source_id}/ado-credential", response_model=AdoCredentialOut)
def get_ado_credential(
    project_guid: str,
    source_id: int,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> AdoCredentialOut:
    """Where this source's Azure DevOps token would come from, before any sync.

    This is the route that makes the hub constraint legible: a source pointed at
    a hub-backed connection answers ``origin="hub"``, ``canSync=false`` and the
    sentence that names the fix — while the connection itself stays untouched and
    perfectly healthy for the work items it was made for.
    """
    guid = _resolve_project(db, project_guid, user)
    return _state(db, _source_or_404(db, guid, source_id, user))


@router.put("/sources/{source_id}/ado-credential", response_model=AdoCredentialOut)
def set_ado_credential(
    project_guid: str,
    source_id: int,
    body: AdoCredentialIn,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> AdoCredentialOut:
    """Store a wiki-scoped PAT for this source — after proving it can read the wiki.

    Preflight **then** store, never the other way round: a token that cannot list
    the project's wikis is refused with the reason (wrong scope, expired, no wiki
    there) and nothing is written, so the row never carries a credential that was
    known-bad the moment it was saved.

    :raises HTTPException: 400 with the adapter's own message when preflight
        fails, 404 when the source is not the caller's.
    """
    guid = _resolve_project(db, project_guid, user)
    row = _source_or_404(db, guid, source_id, user)
    try:
        ado_wiki.preflight(row.url or "", body.pat)
    except SourceFetchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    credentials.set_source_token(db, row, body.pat)
    return _state(db, row)


@router.delete("/sources/{source_id}/ado-credential", response_model=AdoCredentialOut)
def delete_ado_credential(
    project_guid: str,
    source_id: int,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> AdoCredentialOut:
    """Remove this source's stored token.

    Answers with the resulting state rather than 204, so the caller immediately
    sees what the source falls back to — which, for a hub-backed connection, is
    "nothing", said in words.
    """
    guid = _resolve_project(db, project_guid, user)
    row = _source_or_404(db, guid, source_id, user)
    credentials.clear_source_token(db, row)
    return _state(db, row)


@router.post("/ado/preflight", response_model=AdoPreflightOut)
def preflight_ado_wiki(
    project_guid: str,
    body: AdoPreflightIn,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> AdoPreflightOut:
    """Test a wiki URL and token **before** the source is created.

    The issue's "preflight on source creation, not at first sync" — the user
    learns about a scope problem while adding the source, not hours later in a
    failed sync. Stores nothing at all, so it is safe to call on every keystroke
    of the Add-source dialog's Test button.

    :raises HTTPException: 400 carrying the specific refusal — wrong scope,
        rejected token, no wiki in that project, no such wiki, rate limit.
    """
    _resolve_project(db, project_guid, user)
    try:
        result = ado_wiki.preflight(body.url, body.pat)
    except SourceFetchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return AdoPreflightOut(ok=True, **result)
