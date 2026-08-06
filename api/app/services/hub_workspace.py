"""Mirror EmeHub-owned resources into the caller's own workspace (#514).

A user who signs in through EmeHub for the first time gets a freshly provisioned
local account, and everything in Q-Agent is per-user (``owner_id``, ADR 0009) — so
they landed in an empty workspace while the hub held their connection, projects
and every ticket.

**Why mirror rather than read through.** Every downstream feature — run creation,
review, evidence, publish — addresses local rows by primary key. A read-through
list can be *browsed* but not *selected into a run*, which is the only thing a
user actually wants to do with a ticket here. The read-through added in #500
overlays hub values onto local rows and is complementary: it keeps an existing row
fresh, while this creates the row in the first place.

**What a mirrored connection is.** A real ``ProviderConnection`` with
``hub_connection_id`` set and ``secrets`` empty — empty permanently, because the
hub never releases the PAT (``GET /connections`` returns ``hasPat`` only). It
exists so per-connection scoping, the connection picker and the ticket filters
keep working unchanged. It must never be used for a direct provider call; provider
work for these belongs to the hub, which holds the credential.

Everything here is **idempotent** and **additive**: re-running updates in place,
never duplicates, and never deletes or overwrites a row the user owns locally. A
user who already has their own connections is unaffected.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging import logger
from app.models.project import Project
from app.models.provider_connection import ProviderConnection
from app.models.run import RunTicket
from app.models.ticket import Ticket
from app.models.user import User
from app.services import hub_client

# The hub spells Azure DevOps `azure_devops`; we spell it `ado` (#507). Shared
# with the ticket read-through's join key so both sides agree.
_KIND_ALIASES = {"azure_devops": "ado", "azure-devops": "ado", "azuredevops": "ado"}


def _local_kind(hub_kind: Any) -> str:
    kind = str(hub_kind or "").strip().lower()
    return _KIND_ALIASES.get(kind, kind)


def _str(value: Any, default: str = "") -> str:
    return str(value).strip() if value not in (None, "") else default


def ensure_connections(db: Session, user: User, hub_token: str) -> dict[str, ProviderConnection]:
    """Mirror the hub's connections for ``user``; return them keyed by hub id.

    Idempotent: an existing mirror is refreshed in place. Connections the user
    created locally are matched by nothing and left completely alone — we only
    ever touch rows carrying the matching ``hub_connection_id``.
    """
    rows = hub_client.list_connections(hub_token)
    if not isinstance(rows, list):
        return {}

    mirrored: dict[str, ProviderConnection] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        hub_id = _str(row.get("id"))
        kind = _local_kind(row.get("kind"))
        if not hub_id or not kind:
            continue

        existing = db.scalar(
            select(ProviderConnection).where(
                ProviderConnection.hub_connection_id == hub_id,
                ProviderConnection.owner_id == user.id,
            )
        )
        conn = existing or ProviderConnection(
            owner_id=user.id,
            hub_connection_id=hub_id,
            # Empty, and staying that way: the PAT never crosses (#501).
            secrets={},
        )
        conn.kind = kind
        conn.name = _str(row.get("label")) or _str(row.get("baseUrl")) or f"EmeHub {kind}"
        # `connected` reflects the hub's own view. It does NOT mean Q-Agent can
        # call the provider — it cannot, and must not try.
        conn.connected = bool(row.get("connected"))
        config = row.get("config")
        conn.config = dict(config) if isinstance(config, dict) else {}
        if row.get("baseUrl"):
            conn.config.setdefault("baseUrl", row["baseUrl"])
        if existing is None:
            db.add(conn)
        mirrored[hub_id] = conn

    db.commit()
    for conn in mirrored.values():
        db.refresh(conn)
    return mirrored


def ensure_tickets(
    db: Session, user: User, hub_token: str, connections: dict[str, ProviderConnection]
) -> int:
    """Mirror the hub's tickets for ``user``. Returns how many rows were created.

    Matched on ``hub_ticket_id`` — the hub's own id — rather than
    ``(kind, external_id)``, because this owns the row's existence and needs an
    unambiguous identity. Existing rows are refreshed in place.

    Only list-level fields are written. ``description`` and ``acceptanceCriteria``
    are deliberately left to the detail fetch: pulling them for every ticket would
    mean one hub round trip per ticket (200 here) to populate fields the list never
    shows.
    """
    # Every page, not just the first: the hub defaults to 25 per page, so a
    # single call mirrored 25 of 200 tickets and the workspace looked
    # arbitrarily truncated. `complete` says whether the walk was exhaustive —
    # only then may we prune (#522).
    items, complete = hub_client.iter_all_tickets(hub_token)
    if not items and not complete:
        return 0

    existing_rows = db.scalars(
        select(Ticket).where(Ticket.owner_id == user.id, Ticket.hub_ticket_id.is_not(None))
    ).all()
    by_hub_id = {row.hub_ticket_id: row for row in existing_rows}

    created = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        hub_id = _str(item.get("id"))
        external_id = _str(item.get("externalId"))
        if not hub_id or not external_id:
            continue

        row = by_hub_id.get(hub_id)
        if row is None:
            row = Ticket(owner_id=user.id, hub_ticket_id=hub_id)
            db.add(row)
            created += 1

        row.external_id = external_id
        row.provider_kind = _local_kind(item.get("providerKind"))
        row.title = _str(item.get("title"))[:500]
        row.work_item_type = _str(item.get("workItemType"), "User Story")[:32]
        row.status = _str(item.get("status"), "Ready for QA")[:32]
        row.priority = _str(item.get("priority"), "Medium")[:16]
        row.assignee = _str(item.get("assignee"))[:120]
        row.sprint = _str(item.get("sprint"))[:120]
        row.area_path = _str(item.get("areaPath"))[:300]
        row.epic = _str(item.get("epic"))[:300]
        labels = item.get("labels")
        row.labels = [str(x) for x in labels] if isinstance(labels, list) else []

        conn = connections.get(_str(item.get("connectionId")))
        if conn is not None:
            row.connection_id = conn.id

    if complete:
        _prune_vanished(db, user, seen_hub_ids={_str(i.get("id")) for i in items if isinstance(i, dict)})

    db.commit()
    return created


def _prune_vanished(db: Session, user: User, seen_hub_ids: set[str]) -> int:
    """Delete the caller's mirrored tickets the hub no longer has. Returns the count.

    Only ever called after an **exhaustive** hub read: a partial or failed read
    would otherwise look like "the hub is empty now" and wipe the workspace. That
    is why :func:`hub_client.iter_all_tickets` reports completeness.

    Two rows are deliberately spared:

    * anything without ``hub_ticket_id`` — a ticket the user created or synced
      locally was never ours to remove;
    * anything a run references. ``runs.run_tickets.ticket_external_id`` is a
      plain string with **no foreign key**, so deleting would not fail loudly — it
      would silently orphan run history, leaving a run pointing at a ticket that
      no longer exists.
    """
    mirrored = db.scalars(
        select(Ticket).where(Ticket.owner_id == user.id, Ticket.hub_ticket_id.is_not(None))
    ).all()
    vanished = [row for row in mirrored if row.hub_ticket_id not in seen_hub_ids]
    if not vanished:
        return 0

    referenced = {
        ext
        for (ext,) in db.execute(
            select(RunTicket.ticket_external_id).where(
                RunTicket.ticket_external_id.in_([row.external_id for row in vanished])
            )
        ).all()
    }

    removed = 0
    kept = 0
    for row in vanished:
        if row.external_id in referenced:
            kept += 1
            continue
        db.delete(row)
        removed += 1

    if removed or kept:
        logger.info(
            "pruned {} mirrored tickets the hub no longer has for user {} ({} kept, referenced by a run)",
            removed, user.id, kept,
        )
    return removed


def ensure_projects(
    db: Session, user: User, hub_token: str, connections: dict[str, ProviderConnection]
) -> int:
    """Mirror the hub's projects for ``user``. Returns how many rows were created.

    Matched on ``(provider_kind, external_id)`` scoped to the owner, so a project
    the user already had locally is refreshed rather than duplicated.

    Mirrored projects are created **inactive**: activating a project is a
    deliberate user choice here, and silently activating several because the hub
    happens to know about them would change what runs target.
    """
    rows = hub_client.list_projects(hub_token)
    if not isinstance(rows, list):
        return 0

    created = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = _str(row.get("key"))
        name = _str(row.get("name")) or key
        if not key:
            continue

        conn = next(iter(connections.values()), None)
        kind = conn.kind if conn is not None else ""

        existing = db.scalar(
            select(Project).where(
                Project.external_id == key,
                Project.owner_id == user.id,
            )
        )
        project = existing or Project(
            owner_id=user.id, external_id=key, provider_kind=kind, active=False
        )
        project.name = name[:200]
        if conn is not None:
            project.connection_id = conn.id
            if not project.provider_kind:
                project.provider_kind = conn.kind
        summary = row.get("summary")
        if isinstance(summary, dict):
            project.meta = {**(project.meta or {}), "hub": summary}
        if existing is None:
            db.add(project)
            created += 1

    db.commit()
    return created


def ensure_for_user(db: Session, user: User | None, hub_token: str | None) -> dict[str, int]:
    """Mirror the hub's connections and tickets for ``user``. Never raises.

    Called from read paths, so a hub outage must degrade to "whatever is already
    local" rather than breaking the screen — the #491 rule. Returns a small
    summary for logging; callers ignore it.
    """
    if user is None or not hub_token or not hub_client.enabled():
        return {"connections": 0, "tickets": 0, "projects": 0}

    try:
        connections = ensure_connections(db, user, hub_token)
        created = ensure_tickets(db, user, hub_token, connections)
        projects = ensure_projects(db, user, hub_token, connections)
    except hub_client.HubClientError as exc:
        # Routine: 15-minute tokens expire, the hub is a remote hop.
        logger.info("hub workspace mirror skipped ({}), serving local data", exc)
        db.rollback()
        return {"connections": 0, "tickets": 0, "projects": 0}
    except Exception as exc:  # noqa: BLE001 - a mirror must never break a read
        logger.warning("hub workspace mirror failed unexpectedly: {}", exc)
        db.rollback()
        return {"connections": 0, "tickets": 0, "projects": 0}

    if created or projects:
        logger.info(
            "mirrored {} hub tickets and {} projects for user {}", created, projects, user.id
        )
    return {"connections": len(connections), "tickets": created, "projects": projects}


def fill_ticket_detail(db: Session, ticket: Ticket, hub_token: str | None) -> Ticket:
    """Populate a mirrored ticket's description/AC/comments from the hub, once.

    Deferred to here rather than done during the list mirror: the list shows none
    of these, and fetching them eagerly would cost one hub round trip per ticket.

    Never raises, and only fills what is empty — a user's local edits to a
    mirrored ticket are never clobbered.
    """
    if not ticket.hub_ticket_id or not hub_token or not hub_client.enabled():
        return ticket
    if ticket.description and ticket.acceptance_criteria:
        return ticket

    try:
        detail = hub_client.get_ticket(ticket.external_id, hub_token)
    except hub_client.HubClientError as exc:
        logger.info("hub ticket detail unavailable for {}: {}", ticket.external_id, exc)
        return ticket
    if not isinstance(detail, dict):
        return ticket

    if not ticket.description:
        ticket.description = _str(detail.get("description"))
    if not ticket.acceptance_criteria:
        criteria = detail.get("acceptanceCriteria")
        if isinstance(criteria, list):
            ticket.acceptance_criteria = [str(c) for c in criteria]
        elif criteria:
            ticket.acceptance_criteria = [str(criteria)]
    if not ticket.acceptance_criteria_html:
        ticket.acceptance_criteria_html = _str(detail.get("acceptanceCriteriaHtml"))
    if not ticket.comments:
        comments = detail.get("comments")
        ticket.comments = comments if isinstance(comments, list) else []

    db.commit()
    db.refresh(ticket)
    return ticket
