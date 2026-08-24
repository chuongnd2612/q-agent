"""Ticket-derived filter facets — one ``SELECT DISTINCT`` shared by every picker.

Why this lives in a service rather than in ``routers/tickets.py``: two different
router modules need the *same* derivation, and #655 was caused by them not
sharing it.

* ``GET /tickets/filter-options`` (``routers/tickets.py``) serves the query
  builder from these rows and always has.
* ``GET /connections/{id}/sprints`` and ``GET
  /connections/{id}/work-item-metadata`` (``routers/providers.py``) normally call
  the provider with the connection's PAT — but a **hub-mirrored** connection has
  permanently empty secrets by design (the hub never releases the PAT), so that
  call can never succeed, and every picker on the Tickets screen came back empty
  while the rows on the same screen plainly carried Sprint / State / Area path /
  Work item type. Proxying to the hub is not available either: both endpoints are
  hub-audience only (an agent token gets 401 "Token is not valid for this
  audience"). Those handlers therefore fall back to *this* derivation.

The tradeoff, stated so it is diagnosable rather than mysterious (#507/#514/#598
are all the same shape — the call succeeds and the data is quietly incomplete):
facets read off tickets are only as complete as the mirror. A sprint with no
mirrored ticket is not offered. Callers that take this path log a tell-tale
naming the counts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models.ticket import Ticket
from app.models.user import User
from app.services.ownership import owned


def distinct_values(values: list[Any]) -> list[str]:
    """Sorted, de-duplicated, blank-free strings — one column's offerable values."""
    seen = {str(value).strip() for value in values if str(value or "").strip()}
    return sorted(seen, key=str.casefold)


@dataclass
class TicketFacets:
    """Every offerable value present on a scoped set of ticket rows."""

    work_item_types: list[str] = field(default_factory=list)
    states: list[str] = field(default_factory=list)
    area_paths: list[str] = field(default_factory=list)
    sprints: list[str] = field(default_factory=list)
    epics: list[str] = field(default_factory=list)
    assignees: list[str] = field(default_factory=list)
    priorities: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    ticket_count: int = 0
    #: True when any row in the set carries a hub ticket id.
    any_hub_ticket: bool = False

    def counts_summary(self) -> str:
        """Compact ``k=n`` census for the tell-tale log line."""
        return (
            f"tickets={self.ticket_count} sprints={len(self.sprints)} "
            f"area_paths={len(self.area_paths)} states={len(self.states)} "
            f"work_item_types={len(self.work_item_types)} epics={len(self.epics)} "
            f"priorities={len(self.priorities)} assignees={len(self.assignees)}"
        )


def derive(
    db: Session,
    user: User | None,
    *,
    connection_id: int | None = None,
    provider_kind: str | None = None,
) -> TicketFacets:
    """Distinct filter values off the caller's OWN rows, optionally connection-scoped.

    Owner-scoped through :func:`owned` exactly like ``GET /tickets`` — one user's
    assignees, sprints and area paths must never appear in another's picker, which
    would leak the shape of their work even though no ticket is returned (#517).

    No provider call and no hub call: it answers whether or not EmeHub is
    reachable (#491) and whether or not the connection has a credential (#655).
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

    return TicketFacets(
        work_item_types=distinct_values([row.work_item_type for row in rows]),
        states=distinct_values([row.status for row in rows]),
        area_paths=distinct_values([row.area_path for row in rows]),
        sprints=distinct_values([row.sprint for row in rows]),
        epics=distinct_values([row.epic for row in rows]),
        assignees=distinct_values([row.assignee for row in rows]),
        priorities=distinct_values([row.priority for row in rows]),
        labels=distinct_values(labels),
        ticket_count=len(rows),
        any_hub_ticket=any(row.hub_ticket_id for row in rows),
    )
