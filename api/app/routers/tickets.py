"""Tickets router.

Endpoints to implement:
  GET  /tickets                     -> TicketPageOut         (query: status, assignee, sprint,
                                                                connection_id, provider_kind,
                                                                priority, epic, q, page, page_size)
  GET  /tickets/filter-options       -> TicketFilterOptionsOut (query: connection_id,
                                                                provider_kind)
  GET  /tickets/{external_id}        -> TicketDetailOut
  POST /tickets/sync                 -> SyncResult           (body: SyncRequest; live adapter pull)
  POST /tickets/delete               -> TicketDeleteResult   (body: TicketDeleteRequest; local bulk delete)
  DELETE /tickets/{external_id}      -> 204                  (local delete of a single ticket)

EmeHub read-through (#500, C3 of #497)
--------------------------------------
``GET /tickets`` can serve the *displayed* ticket fields from the hub when
``QAGENT_HUB_DATA_ENABLED`` (plus SSO) is on **and** the caller supplied a fresh
``X-Hub-Token``. Four properties are load-bearing:

1. **Read-through, not ownership transfer.** Local rows remain the unit of work —
   the hub overlays title/status/assignee/sprint/epic/priority/areaPath/labels/AC
   count onto rows we already have. Ticket *sync* still runs locally, because it
   needs a provider PAT and the hub never hands one out (#497 §4c), so ticket
   ownership cannot move even if we wanted it to.
2. **No phantom rows.** A hub ticket with no local counterpart is reconciled
   against and then ignored: it is never inserted, and never appears in the list.
   Selecting a ticket that has no local row would produce a run that cannot
   generate (no description, no acceptance criteria) — a worse failure than not
   showing it. Local rows the hub does not know about keep their local values, so
   the read-through only ever *freshens* the list, never shrinks it.
3. **Nothing is cached.** The hub offers no webhook, ETag or revision counter, so
   a cache would go stale silently — a smaller copy of the drift the hub exists to
   remove. One hub call per request, or none.
4. **Any hub failure falls back to local, silently.** Down, 401, malformed — the
   local query runs and the user sees tickets. A failed load must never render as
   an empty list (#491), and here it does not render at all.

With the flag off, or with no ``X-Hub-Token``, none of this executes and the
handler is byte-identical to before.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db, utcnow
from app.deps_auth import current_user
from app.deps_hub import hub_token as hub_token_header
from app.logging import logger
from app.models.linked import LinkedTestCase
from app.models.provider_connection import ProviderConnection
from app.models.ticket import Ticket
from app.models.user import User
from app.schemas import (
    LinkedTestCaseOut,
    SyncRequest,
    SyncResult,
    TicketDeleteRequest,
    TicketDeleteResult,
    TicketDetailOut,
    HubQueryRequest,
    HubSyncRequest,
    TicketFilterOptionsOut,
    TicketOut,
    TicketPageOut,
)
from app.services import audit_service, connection_service, hub_client, hub_workspace
from app.services.adapters.base import ProviderError
from app.services.ownership import owned, stamp_owner

router = APIRouter(prefix="/tickets", tags=["tickets"])

# SQLite caps a statement at 999 bound parameters, so reconciliation batches its
# ``external_id IN (...)`` lookups rather than assuming the hub page is small.
_IN_CHUNK = 400


# ------------------------------------------------------------- hub read-through
# The hub names Azure DevOps `azure_devops`; we name it `ado`
# (``provider_connection.py``: ado/jira/github). Reconciling without translating
# means the join key is ("ado", …) on one side and ("azure_devops", …) on the
# other, so nothing ever matches for our most-used provider — and it fails
# *silently*, because a read that matches nothing satisfies every other rule
# (#507). `jira` and `github` are spelled the same on both sides.
_HUB_KIND_ALIASES = {
    "azure_devops": "ado",
    "azure-devops": "ado",
    "azuredevops": "ado",
}


def _hub_key(provider_kind: Any, external_id: Any) -> tuple[str, str] | None:
    """The join key: ``(provider_kind, external_id)``, normalised to OUR vocabulary.

    Both halves are required. Matching on ``external_id`` alone would happily
    join an ADO ``PROJ-1`` to a Jira ``PROJ-1`` — different work items that share
    a naming convention — so a missing kind yields no key rather than a loose one.

    The kind is case-folded **and** translated through :data:`_HUB_KIND_ALIASES`,
    so a hub `azure_devops` and a local `ado` produce the same key. Applied to
    both sides: local kinds pass through unchanged (they are already ours), and
    an unrecognised kind is kept verbatim so it simply fails to match rather than
    joining the wrong rows.
    """
    kind = str(provider_kind or "").strip().lower()
    kind = _HUB_KIND_ALIASES.get(kind, kind)
    ext = str(external_id or "").strip()
    if not kind or not ext:
        return None
    return (kind, ext)


def _index_hub_items(items: list[Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """Hub tickets keyed by ``(providerKind, externalId)``; unkeyable ones dropped."""
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        key = _hub_key(item.get("providerKind"), item.get("externalId"))
        if key is not None:
            indexed[key] = item
    return indexed


def _record_hub_ticket_ids(
    db: Session, user: User | None, overlay: dict[tuple[str, str], dict[str, Any]]
) -> None:
    """Stamp ``hub_ticket_id`` on the caller's rows that the hub also knows.

    **Only ever an UPDATE.** Unmatched hub tickets are dropped on the floor here —
    inserting them would fabricate rows with no description and no acceptance
    criteria, which is exactly the phantom data #500 forbids.

    Scoped to the caller's own tickets and matched on the full
    ``(provider_kind, external_id)`` key, so one user's reconciliation never
    writes another's rows and no ADO id is ever joined to a Jira one. Best-effort:
    a write failure rolls back and the read-through carries on with local ids
    unchanged — recording the mapping is valuable, not essential.
    """
    if not overlay:
        return
    external_ids = sorted({ext for _, ext in overlay})
    changed = 0
    try:
        for start in range(0, len(external_ids), _IN_CHUNK):
            chunk = external_ids[start : start + _IN_CHUNK]
            rows = (
                owned(db.query(Ticket), Ticket, user)
                .filter(Ticket.external_id.in_(chunk))
                .all()
            )
            for row in rows:
                key = _hub_key(row.provider_kind, row.external_id)
                item = overlay.get(key) if key else None
                if item is None:
                    continue
                hub_id = str(item.get("id") or "").strip()
                if hub_id and row.hub_ticket_id != hub_id:
                    row.hub_ticket_id = hub_id
                    changed += 1
        if changed:
            db.commit()
    except Exception as exc:  # noqa: BLE001 - the mapping is an optimisation
        db.rollback()
        logger.warning("hub ticket reconciliation failed: {}", exc)


def _naive_utc(value: datetime | None) -> datetime | None:
    """Drop the tzinfo (converting to UTC first) so datetimes stay comparable.

    Load-bearing: hub-overlaid rows and local-only rows are sorted in the same
    list, and Python raises ``TypeError`` the moment an aware datetime is compared
    to a naive one. Stored ``synced_at`` values are aware on some backends and
    naive on others, so both sides are flattened here rather than assumed.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _parse_hub_timestamp(value: Any) -> datetime | None:
    """Parse the hub's ``syncedAt`` into a naive-UTC datetime, or ``None``."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return _naive_utc(parsed)


def _overlay_ticket(row: Ticket, item: dict[str, Any] | None) -> tuple[TicketOut, datetime]:
    """A ``TicketOut`` for ``row``, with the hub's values layered on where present.

    ``id`` and ``connection_id`` are deliberately **never** overlaid: they are
    local primary/foreign keys, and the hub's ``id``/``connectionId`` live in a
    different namespace entirely. Overlaying them would produce ids that resolve
    to the wrong row — or to nothing — on every follow-up request.
    """
    out = TicketOut.model_validate(row)
    sort_at = _naive_utc(row.synced_at) or datetime.min
    if item is None:
        return out, sort_at

    def take(field: str, current: str) -> str:
        value = item.get(field)
        return value if isinstance(value, str) and value else current

    out.title = take("title", out.title)
    out.status = take("status", out.status)
    out.priority = take("priority", out.priority)
    out.assignee = take("assignee", out.assignee)
    out.sprint = take("sprint", out.sprint)
    out.epic = take("epic", out.epic)
    out.area_path = take("areaPath", out.area_path)
    labels = item.get("labels")
    if isinstance(labels, list):
        out.labels = [str(label) for label in labels]
    ac_count = item.get("acCount")
    if isinstance(ac_count, int) and not isinstance(ac_count, bool):
        out.ac_count = ac_count
    return out, _parse_hub_timestamp(item.get("syncedAt")) or sort_at


def _matches_filters(
    out: TicketOut,
    row: Ticket,
    *,
    status: str | None,
    assignee: str | None,
    sprint: str | None,
    area_path: str | None,
    state_list: list[str],
    type_list: list[str],
    priority: str | None,
    epic: str | None,
    q: str | None,
) -> bool:
    """The local query's WHERE clause, re-expressed over the merged values.

    Applied to the *merged* row rather than the stored one on purpose: filtering
    on a stale local status while displaying the hub's fresh one would produce a
    list that visibly contradicts the filter that produced it.

    ``work_item_type`` has no hub counterpart, so that one filter always reads the
    local value — the honest answer, since the hub simply does not carry the field.
    """
    if status and out.status != status:
        return False
    if assignee and out.assignee != assignee:
        return False
    if sprint and out.sprint != sprint:
        return False
    if area_path and not (out.area_path or "").startswith(area_path):
        return False
    if state_list and out.status not in state_list:
        return False
    if type_list and row.work_item_type not in type_list:
        return False
    if priority and out.priority != priority:
        return False
    if epic and out.epic != epic:
        return False
    if q:
        needle = q.lower()
        if needle not in (out.title or "").lower() and needle not in out.external_id.lower():
            return False
    return True


def _hub_read_through(
    db: Session,
    user: User | None,
    hub_token: str,
    *,
    base_query,
    page: int,
    page_size: int,
    **filters: Any,
) -> TicketPageOut | None:
    """Serve the list with the hub's values overlaid, or ``None`` to use local.

    ``None`` means "the hub could not answer" and is the *only* failure signal —
    every hub exception, a malformed payload and a broken reconciliation all land
    there, and the caller runs the ordinary local query. Nothing here can turn a
    hub outage into an error response or an empty page (#491).
    """
    try:
        payload = hub_client.list_tickets(hub_token)
    except hub_client.HubClientError as exc:
        # Expected, routinely: 15-minute tokens expire and the hub is a remote
        # hop. Info, not error — the user is about to get their tickets anyway.
        logger.info("hub ticket read unavailable, serving local tickets: {}", exc)
        return None
    except Exception as exc:  # noqa: BLE001 - a hub read must never break the list
        logger.warning("unexpected hub ticket read failure, serving local tickets: {}", exc)
        return None

    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        logger.info("hub ticket read returned an unexpected shape; serving local tickets")
        return None

    overlay = _index_hub_items(items)
    _record_hub_ticket_ids(db, user, overlay)

    # A hub page that returns tickets yet matches none of ours is the signature of
    # a vocabulary or key mismatch (#507) — and it is otherwise indistinguishable
    # from the flag being off, because matching nothing satisfies every other
    # rule here. Say so once per read rather than letting it look like success.
    if items and not overlay:
        logger.warning(
            "hub returned {} tickets but none produced a usable join key — "
            "check the providerKind vocabulary",
            len(items),
        )

    # Anchored on local rows (already owner-scoped by the caller), so the hub can
    # freshen what we show but never widen who can see it.
    rows = base_query.all()
    merged: list[tuple[TicketOut, datetime, int]] = []
    for row in rows:
        key = _hub_key(row.provider_kind, row.external_id)
        out, sort_at = _overlay_ticket(row, overlay.get(key) if key else None)
        if _matches_filters(out, row, **filters):
            merged.append((out, sort_at, row.id))

    # Same ordering as the local query: newest sync first (never-synced last, as
    # `nullslast` does), id ascending to break ties so pagination is stable.
    # Two stable passes rather than a negated key — `datetime` has no unary minus,
    # and `.timestamp()` on `datetime.min` overflows on Windows.
    merged.sort(key=lambda entry: entry[2])
    merged.sort(key=lambda entry: entry[1], reverse=True)
    start = (page - 1) * page_size
    return TicketPageOut(
        items=[entry[0] for entry in merged[start : start + page_size]],
        total=len(merged),
        page=page,
        page_size=page_size,
    )


@router.get("", response_model=TicketPageOut)
def list_tickets(
    status: str | None = None,
    assignee: str | None = None,
    sprint: str | None = None,
    # Multi-word query params are camelCase on the wire (matching the rest of the
    # API: request bodies + responses). FastAPI needs an explicit alias to bind
    # snake_case handler args to those camelCase names.
    area_path: str | None = Query(None, alias="areaPath"),
    states: str | None = None,  # comma-separated
    work_item_types: str | None = Query(None, alias="workItemTypes"),  # comma-separated
    q: str | None = None,
    connection_id: int | None = Query(None, alias="connectionId"),
    provider_kind: str | None = Query(None, alias="providerKind"),
    priority: str | None = None,
    epic: str | None = None,
    page: int = 1,
    page_size: int = Query(25, alias="pageSize"),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
    hub_token: str | None = Depends(hub_token_header),
) -> TicketPageOut:
    """Tickets scoped to ``user`` (#93 — private per-user data).

    With ``QAGENT_HUB_DATA_ENABLED`` on and an ``X-Hub-Token`` present, the
    displayed values may come from EmeHub — see the module docstring. Every hub
    failure falls through to the local query below, which is unchanged.
    """
    # A user who signed in through EmeHub owns nothing locally on first visit
    # (everything is per-user, ADR 0009), so mirror the hub's connections and
    # tickets into their workspace first — otherwise this screen is empty while
    # the hub holds all their work (#514). Idempotent, additive, and silent on
    # failure: it never raises, so a hub outage just leaves the local query below
    # to serve whatever already exists.
    hub_workspace.ensure_for_user(db, user, hub_token)

    query = owned(db.query(Ticket), Ticket, user)
    if connection_id:
        query = query.filter(Ticket.connection_id == connection_id)
    if provider_kind:
        query = query.filter(Ticket.provider_kind == provider_kind)

    # Hub read-through. Scoping filters (owner / connection / provider) are already
    # applied above, so the hub path inherits them; the value filters below are
    # re-applied there against the merged values.
    if hub_token and hub_client.enabled():
        hub_page = _hub_read_through(
            db,
            user,
            hub_token,
            base_query=query,
            page=page,
            page_size=page_size,
            status=status,
            assignee=assignee,
            sprint=sprint,
            area_path=area_path,
            state_list=[s for s in (states or "").split(",") if s],
            type_list=[t for t in (work_item_types or "").split(",") if t],
            priority=priority,
            epic=epic,
            q=q,
        )
        if hub_page is not None:
            return hub_page

    if status:
        query = query.filter(Ticket.status == status)
    if assignee:
        query = query.filter(Ticket.assignee == assignee)
    if sprint:
        query = query.filter(Ticket.sprint == sprint)
    if area_path:
        # UNDER semantics: the selected area path and its children. Use
        # startswith(autoescape=True) rather than a raw LIKE: ADO area paths
        # contain backslashes (e.g. "Surency\\Data Platform") and Postgres LIKE
        # treats backslash as its default ESCAPE char, so a raw
        # `LIKE 'Surency\\Data Platform%'` collapses to `SurencyData Platform%`
        # and matches nothing. autoescape emits `ESCAPE '/'` and escapes %/_ in
        # the value, keeping backslashes literal.
        query = query.filter(Ticket.area_path.startswith(area_path, autoescape=True))
    state_list = [s for s in (states or "").split(",") if s]
    if state_list:
        query = query.filter(Ticket.status.in_(state_list))
    type_list = [t for t in (work_item_types or "").split(",") if t]
    if type_list:
        query = query.filter(Ticket.work_item_type.in_(type_list))
    if priority:
        query = query.filter(Ticket.priority == priority)
    if epic:
        query = query.filter(Ticket.epic == epic)
    if q:
        like = f"%{q}%"
        query = query.filter((Ticket.title.ilike(like)) | (Ticket.external_id.ilike(like)))

    total = query.count()
    items = (
        query.order_by(Ticket.synced_at.desc().nullslast(), Ticket.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return TicketPageOut(
        items=[TicketOut.model_validate(t) for t in items],
        total=total,
        page=page,
        page_size=page_size,
    )


def _distinct(values: list[Any]) -> list[str]:
    """Sorted, de-duplicated, blank-free strings — one column's offerable values."""
    seen = {str(value).strip() for value in values if str(value or "").strip()}
    return sorted(seen, key=str.casefold)


# NOTE: declared BEFORE ``GET /{external_id}`` on purpose. FastAPI matches routes
# in declaration order, so the other way round this path would be swallowed as a
# ticket whose external id is the literal string "filter-options".
@router.get("/filter-options", response_model=TicketFilterOptionsOut)
def ticket_filter_options(
    connection_id: int | None = Query(None, alias="connectionId"),
    provider_kind: str | None = Query(None, alias="providerKind"),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> TicketFilterOptionsOut:
    """The query builder's dropdown values, read off the caller's own tickets (#517).

    Owner-scoped through :func:`owned` exactly like ``GET /tickets`` — one user's
    assignees, sprints and area paths must never appear in another's picker,
    which would leak the shape of their work even though no ticket is returned.

    No provider call and no hub call: this is a ``SELECT DISTINCT`` over rows we
    already hold, so it answers whether or not EmeHub is reachable (#491) and
    whether or not the connection has a credential.
    """
    query = owned(db.query(Ticket), Ticket, user)
    if connection_id:
        query = query.filter(Ticket.connection_id == connection_id)
    if provider_kind:
        query = query.filter(Ticket.provider_kind == provider_kind)
    rows = query.all()

    labels: list[Any] = []
    for row in rows:
        if isinstance(row.labels, list):
            labels.extend(row.labels)

    # Hub-managed is asked of the *connection* first: a mirrored connection with
    # no tickets yet is still the hub's to manage, and answering False there
    # would show a Sync button that cannot work until the first row arrives.
    hub_managed = any(row.hub_ticket_id for row in rows)
    if not hub_managed and connection_id:
        # Owner-scoped like everything else here: an unscoped lookup would let a
        # caller probe another user's connection id and learn whether it is
        # hub-backed, and would decide THIS user's Sync button from someone
        # else's row.
        conn = owned(db.query(ProviderConnection), ProviderConnection, user).filter(
            ProviderConnection.id == connection_id
        ).first()
        hub_managed = bool(conn and conn.hub_connection_id)

    return TicketFilterOptionsOut(
        work_item_types=_distinct([row.work_item_type for row in rows]),
        states=_distinct([row.status for row in rows]),
        area_paths=_distinct([row.area_path for row in rows]),
        sprints=_distinct([row.sprint for row in rows]),
        epics=_distinct([row.epic for row in rows]),
        assignees=_distinct([row.assignee for row in rows]),
        priorities=_distinct([row.priority for row in rows]),
        labels=_distinct(labels),
        ticket_count=len(rows),
        hub_managed=hub_managed,
    )


def _is_hub_backed(db: Session, ticket: Ticket) -> bool:
    """True when the ticket hangs off a mirrored hub connection.

    Such a connection holds no PAT and never will (#501), so any adapter call
    through it would fail — the hub owns provider access for these.
    """
    if ticket.hub_ticket_id:
        return True
    if not ticket.connection_id:
        return False
    conn = db.get(ProviderConnection, ticket.connection_id)
    return bool(conn and conn.hub_connection_id)


@router.get("/{external_id}", response_model=TicketDetailOut)
def get_ticket(
    external_id: str,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
    hub_token: str | None = Depends(hub_token_header),
) -> TicketDetailOut:
    """Scoped to ``user`` (#93 — private per-user data)."""
    ticket = owned(db.query(Ticket), Ticket, user).filter(Ticket.external_id == external_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail=f"Ticket '{external_id}' not found")

    # A mirrored ticket carries only list-level fields — description and AC are
    # fetched here, once, because the list shows neither and pulling them for
    # every ticket would be one hub round trip each (#514).
    ticket = hub_workspace.fill_ticket_detail(db, ticket, hub_token)

    # Comments are skipped during bulk sync (N+1). Load them lazily on first view,
    # routed through the ticket's work-item connection. A hub-backed connection
    # holds no PAT, so this can only work for a locally-credentialed one; the
    # mirror above already brought the hub's comments across.
    if not ticket.comments and not _is_hub_backed(db, ticket):
        try:
            connection = connection_service.resolve_work_item_for_ticket(db, ticket)
            adapter = connection_service.adapter_for(db, connection)
            comments = adapter.fetch_comments(external_id)
            if comments:
                ticket.comments = comments
                db.commit()
                db.refresh(ticket)
        except Exception:  # noqa: BLE001 - detail must never fail on comment fetch
            db.rollback()

    return TicketDetailOut.model_validate(ticket)


@router.get("/{external_id}/linked-cases", response_model=list[LinkedTestCaseOut])
def linked_cases(external_id: str, db: Session = Depends(get_db)) -> list[LinkedTestCase]:
    """Test cases created in the provider and linked to this work item."""
    return (
        db.query(LinkedTestCase)
        .filter(LinkedTestCase.ticket_external_id == external_id)
        .order_by(LinkedTestCase.id.desc())
        .all()
    )


@router.get("/{external_id}/provider-test-cases")
def provider_test_cases(external_id: str, db: Session = Depends(get_db)) -> list[dict]:
    """Existing test cases in the provider (e.g. ADO Test Case work items).

    Lets the app show/manage test cases that already live in the provider, and is
    what generation reads to continue the existing numbering/naming convention.
    """
    ticket = db.query(Ticket).filter(Ticket.external_id == external_id).first()
    if ticket is None:
        return []
    try:
        connection = connection_service.resolve_work_item_for_ticket(db, ticket)
        adapter = connection_service.adapter_for(db, connection)
        items = adapter.list_test_cases(external_id)
    except Exception:  # noqa: BLE001 - degrade gracefully (provider/network hiccup)
        return []
    return [
        {"externalId": tc.get("external_id", ""), "title": tc.get("title", ""), "state": tc.get("state", "")}
        for tc in items
    ]


@router.post("/sync", response_model=SyncResult)
def sync_tickets(
    body: SyncRequest, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> SyncResult:
    """Pull tickets from a work-item connection's adapter and upsert Ticket rows.

    Routes by the request's ``connectionId`` (a work-item connection); falls back
    to the first connection of ``providerKind``. Each synced ticket is stamped with
    the connection's id so downstream work-item work routes back to the same origin.
    Both the connection and the synced tickets are scoped to ``user`` (#93 —
    private per-user data): a user only ever syncs via, and into, their own data.
    """
    owner_id = user.id if user else None
    connection = connection_service.get_connection(db, body.connection_id, owner_id=owner_id)
    if connection is None and body.provider_kind:
        connection = connection_service.first_of_kind(db, body.provider_kind, owner_id=owner_id)
    if connection is None:
        raise HTTPException(
            status_code=404,
            detail=f"No work-item connection is configured for '{body.provider_kind or body.connection_id}'",
        )

    try:
        adapter = connection_service.adapter_for(db, connection)
        fetched = adapter.fetch_tickets(
            mode=body.mode,
            sprint=body.sprint,
            sprint_path=body.sprint_path,
            area_path=body.area_path,
            states=body.states,
            work_item_types=body.work_item_types,
            ticket_ids=body.ticket_ids,
            project=body.project,
        )
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    synced: list[Ticket] = []
    for item in fetched:
        external_id = str(item.get("external_id", ""))
        if not external_id:
            continue
        ticket = (
            owned(db.query(Ticket), Ticket, user)
            .filter(Ticket.external_id == external_id, Ticket.provider_kind == connection.kind)
            .first()
        )
        if not ticket:
            ticket = stamp_owner(Ticket(external_id=external_id, provider_kind=connection.kind), user)
            db.add(ticket)
        ticket.connection_id = connection.id  # stamp the work-item origin

        ticket.title = item.get("title", ticket.title if ticket.id else "")
        ticket.work_item_type = item.get("work_item_type", "User Story")
        ticket.status = item.get("status", "")
        ticket.priority = item.get("priority", "Medium")
        ticket.assignee = item.get("assignee", "")
        ticket.sprint = item.get("sprint", "")
        ticket.area_path = item.get("area_path", "")
        ticket.epic = item.get("epic", "")
        ticket.description = item.get("description", "")
        ticket.note = item.get("note", "")
        ticket.labels = item.get("labels", [])
        ticket.acceptance_criteria = item.get("acceptance_criteria", [])
        ticket.acceptance_criteria_html = item.get("acceptance_criteria_html", "")
        ticket.comments = item.get("comments", [])
        ticket.attachments = item.get("attachments", [])
        ticket.linked_prs = item.get("linked_prs", [])
        synced.append(ticket)

    connection.last_sync = utcnow()
    db.commit()
    for ticket in synced:
        db.refresh(ticket)

    audit_service.record(
        category="sync", actor_type="system", action="Synced tickets",
        target=body.sprint or connection.name or connection.kind,
        meta=f"{len(synced)} work items",
    )

    return SyncResult(synced=len(synced), tickets=[TicketOut.model_validate(t) for t in synced])


@router.post("/delete", response_model=TicketDeleteResult)
def delete_tickets(
    body: TicketDeleteRequest,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> TicketDeleteResult:
    """Bulk local-delete the tickets whose external ids are given.

    LOCAL only — this removes only the caller's own ``Ticket`` rows and never
    calls the provider, so a re-sync restores them. Deleting a ``Ticket`` is
    referentially safe: ``RunTicket`` (and test cases, executions, comments,
    linked cases, Claude usage) reference a work item by its ``ticket_external_id``
    string snapshot, not by a foreign key to ``tickets.id`` — so runs keep their
    history intact and no integrity constraint is violated. Ids that don't match
    any owned ticket are silently ignored; ``deleted`` reflects the rows removed.
    """
    ids = [i for i in body.external_ids if i]
    if not ids:
        return TicketDeleteResult(deleted=0)

    tickets = owned(db.query(Ticket), Ticket, user).filter(Ticket.external_id.in_(ids)).all()
    for ticket in tickets:
        db.delete(ticket)
    db.commit()

    if tickets:
        audit_service.record(
            category="sync", action="Removed tickets",
            target=f"{len(tickets)} work items",
            meta=", ".join(t.external_id for t in tickets),
        )
    return TicketDeleteResult(deleted=len(tickets))


@router.delete("/{external_id}", status_code=204)
def delete_ticket(
    external_id: str, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> None:
    """Local-delete a single ticket by its external id (404 if not found).

    LOCAL only — never calls the provider (a re-sync restores it). Scoped to
    ``user`` like the list/detail endpoints. See ``delete_tickets`` for why
    removing a ``Ticket`` row is referentially safe.
    """
    ticket = owned(db.query(Ticket), Ticket, user).filter(Ticket.external_id == external_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail=f"Ticket '{external_id}' not found")
    db.delete(ticket)
    db.commit()
    audit_service.record(
        category="sync", action="Removed ticket", target=external_id,
    )


# --------------------------------------------------------- hub clause queries
# These proxy the caller's clause query to EmeHub (#519). The query is passed
# through untouched: the hub validates it and REFUSES an unrunnable clause with
# the offending index, which `hub_client` surfaces as a message naming the
# condition. Re-validating here would add a second, weaker gate in front of the
# one that matters — and a filter silently dropped returns MORE tickets than
# asked for, the failure a user is least likely to notice.
#
# All three are POST because a clause query is a body, not a query string.
@router.post("/hub/search")
def hub_search_tickets(
    body: HubQueryRequest,
    hub: str | None = Depends(hub_token_header),
    user: User | None = Depends(current_user),  # noqa: ARG001 - auth gate only
) -> dict:
    """Run a clause query against EmeHub's ticket mirror. Reads only.

    Returns ``{"available": false}`` rather than an error when the hub cannot be
    consulted (flag off, no hub session, hub down), so the Tickets screen can fall
    back to filtering local rows instead of showing an error state (#491).
    """
    if not hub_client.enabled() or not hub:
        return {"available": False}
    try:
        result = hub_client.search_tickets(
            hub,
            body.query,
            page=body.page,
            page_size=body.page_size,
            provider_kind=body.provider_kind,
        )
    except hub_client.HubRefusedError as exc:
        # The hub rejected the QUERY itself — a real answer the user must see,
        # not a reason to silently fall back.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except hub_client.HubClientError as exc:
        logger.info("hub ticket search unavailable: {}", exc)
        return {"available": False}
    return {"available": True, **(result if isinstance(result, dict) else {})}


@router.post("/hub/preview")
def hub_preview_query(
    body: HubQueryRequest,
    hub: str | None = Depends(hub_token_header),
    user: User | None = Depends(current_user),  # noqa: ARG001 - auth gate only
) -> dict:
    """What a clause query *would* pull from the provider. Writes nothing.

    Asks the provider through the hub, not the hub's mirror — so it answers "what
    is out there", which is the number worth showing before a sync commits to
    pulling it.
    """
    if not hub_client.enabled() or not hub:
        return {"available": False}
    try:
        result = hub_client.preview_query(
            hub,
            body.query,
            provider_kind=body.provider_kind,
            connection_id=body.connection_id,
            project=body.project,
        )
    except hub_client.HubRefusedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except hub_client.HubClientError as exc:
        logger.info("hub query preview unavailable: {}", exc)
        return {"available": False}
    return {"available": True, **(result if isinstance(result, dict) else {})}


@router.post("/hub/sync")
def hub_sync_tickets(
    body: HubSyncRequest,
    db: Session = Depends(get_db),
    hub: str | None = Depends(hub_token_header),
    user: User | None = Depends(current_user),
) -> dict:
    """Ask EmeHub to pull work items from the provider, then mirror them here.

    **This writes**, on the hub and then locally, so unlike the reads above a
    failure is reported rather than swallowed: a user who pressed Sync is owed an
    answer. The hub performs the provider call with its own PAT (#503), which is
    why this works without a credential ever crossing.

    Mirroring immediately afterwards means the pulled work is usable in a run
    without waiting for the next page load.
    """
    if not hub_client.enabled() or not hub:
        raise HTTPException(status_code=409, detail="EmeHub ticket sync is not available")
    try:
        result = hub_client.sync_tickets(
            hub,
            query=body.query,
            ticket_ids=body.ticket_ids or None,
            provider_kind=body.provider_kind,
            connection_id=body.connection_id,
            project=body.project,
        )
    except hub_client.HubRefusedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except hub_client.HubClientError as exc:
        raise HTTPException(status_code=503, detail=f"EmeHub could not sync: {exc}") from exc

    mirrored = hub_workspace.ensure_for_user(db, user, hub)
    return {"hub": result if isinstance(result, dict) else {}, "mirrored": mirrored}


@router.get("/hub/saved-queries")
def hub_saved_queries(hub: str | None = Depends(hub_token_header)) -> dict:
    """EmeHub's saved ticket queries — built-ins and the user's own.

    Reading these means both apps offer the *same* saved queries, rather than
    Q-Agent keeping a private browser-local copy that silently diverges.
    """
    if not hub_client.enabled() or not hub:
        return {"available": False, "queries": []}
    try:
        queries = hub_client.list_saved_queries(hub)
    except hub_client.HubClientError as exc:
        logger.info("hub saved queries unavailable: {}", exc)
        return {"available": False, "queries": []}
    return {"available": True, "queries": queries if isinstance(queries, list) else []}
