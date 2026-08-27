"""Provider-connection resolution — split by capability (ADR 0006 revision 2).

Credentials are resolved along two independent paths:

- **Work-item** work (ticket fetch, sync, comment publish, work-item linking,
  project-key resolution) routes by the **ticket's** work-item connection
  (``ticket.connection_id`` → first work-item-capable connection of the
  ticket's kind).
- **Repository** work (repo clone, knowledge build, repo discovery) routes by the
  **project's** ``repository_connection_id`` → first repository-capable
  connection (which may be an Azure DevOps connection — ADO carries both
  capabilities).

Nullable FKs plus first-of-capability fallbacks keep un-bound paths working: a
legacy row or an un-configured project degrades to the first matching connection
rather than crashing.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app import crypto
from app.models.project_config import ProjectConfig
from app.models.provider import Provider
from app.models.provider_connection import (
    PROVIDER_CAPABILITIES,
    PROVIDER_DISPLAY_NAMES,
    REPOSITORY,
    WORK_ITEM,
    ProviderConnection,
    categories_for,
)
from app.models.ticket import Ticket
from app.services.adapters import get_adapter
from app.services.adapters.base import ProviderAdapter, ProviderError

__all__ = [
    "PROVIDER_CAPABILITIES",
    "REPOSITORY",
    "WORK_ITEM",
    "adapter_for",
    "backfill_from_providers",
    "categories_for",
    "connections_with_capability",
    "first_of_kind",
    "get_connection",
    "resolve_repository_for_project",
    "resolve_test_case_for_project",
    "resolve_work_item_for_ticket",
]


# ------------------------------------------------------------------ helpers
def get_connection(
    db: Session, connection_id: int | None, owner_id: int | None = None
) -> ProviderConnection | None:
    """Return a connection by id, or None (also for a None/0 id).

    ``owner_id`` (#93 — private per-user data) restricts the result to a
    connection owned by that user; a connection with no owner (legacy/un-stamped)
    is still returned, mirroring :func:`app.services.ownership.get_owned_or_404`'s
    bridge semantics. ``None`` (the default) applies no ownership filtering.
    """
    if not connection_id:
        return None
    conn = db.get(ProviderConnection, connection_id)
    if conn is None:
        return None
    if owner_id is not None and conn.owner_id is not None and conn.owner_id != owner_id:
        return None
    return conn


def first_of_kind(db: Session, kind: str, owner_id: int | None = None) -> ProviderConnection | None:
    """First connection of a provider kind (ordered by id), or None.

    ``owner_id`` (#93) restricts the search to that user's own connections.
    """
    query = db.query(ProviderConnection).filter(ProviderConnection.kind == kind)
    if owner_id is not None:
        query = query.filter(ProviderConnection.owner_id == owner_id)
    return query.order_by(ProviderConnection.id).first()


def connections_with_capability(
    db: Session, capability: str, owner_id: int | None = None
) -> list[ProviderConnection]:
    """All connections whose kind's capabilities include ``capability``.

    A kind may carry more than one capability (e.g. ``ado`` is both
    ``work_item`` and ``repository``-eligible), so a connection can appear in
    both capability lists. ``owner_id`` (#93) restricts the result to that
    user's own connections.
    """
    kinds = [k for k, caps in PROVIDER_CAPABILITIES.items() if capability in caps]
    if not kinds:
        return []
    query = db.query(ProviderConnection).filter(ProviderConnection.kind.in_(kinds))
    if owner_id is not None:
        query = query.filter(ProviderConnection.owner_id == owner_id)
    return query.order_by(ProviderConnection.id).all()


def _first_with_capability(
    db: Session, capability: str, owner_id: int | None = None
) -> ProviderConnection | None:
    conns = connections_with_capability(db, capability, owner_id=owner_id)
    return conns[0] if conns else None


def adapter_for(db: Session, connection: ProviderConnection) -> ProviderAdapter:  # noqa: ARG001
    """Build a live adapter for a connection (decrypt secrets + ``get_adapter``)."""
    decrypted = {k: crypto.decrypt(v) for k, v in (connection.secrets or {}).items()}
    return get_adapter(connection.kind, connection.config or {}, decrypted)


# ----------------------------------------------------------- work-item routing
def resolve_work_item_for_ticket(db: Session, ticket: Ticket) -> ProviderConnection:
    """Resolve the work-item connection a ticket's work should route through.

    Order: the ticket's stamped ``connection_id`` → the first work-item connection
    of the ticket's ``provider_kind`` → :class:`ProviderError`. Scoped to the
    ticket's own ``owner_id`` (#93 — private per-user data): an owned ticket only
    ever routes through that same user's connections. A ticket with no owner
    (legacy/un-stamped, or auth disabled) applies no ownership filtering.
    """
    conn = get_connection(db, ticket.connection_id, owner_id=ticket.owner_id)
    if conn is not None:
        return conn
    conn = first_of_kind(db, ticket.provider_kind, owner_id=ticket.owner_id)
    if conn is not None and WORK_ITEM in categories_for(conn.kind):
        return conn
    raise ProviderError(
        f"Work-item provider '{ticket.provider_kind}' is not configured"
    )


# ---------------------------------------------------------- repository routing
def resolve_repository_for_project(
    db: Session, project_key: str | None, owner_id: int | None = None
) -> ProviderConnection:
    """Resolve the repository connection a project's code work routes through.

    Order: the project's bound ``repository_connection_id`` (which may be an
    Azure DevOps connection — ADO is repository-capable) → the first
    repository-capable connection → :class:`ProviderError`. ``owner_id`` (#93)
    restricts both steps to that user's own connections.
    """
    if project_key:
        # Prefer the config owned by the resolving scope (shared builds pass
        # ``owner_id=None`` → the shared config; per-user builds → their own),
        # so a same-keyed row another owner holds can't leak the wrong binding.
        cfg = (
            db.query(ProjectConfig)
            .filter(ProjectConfig.key == project_key, ProjectConfig.owner_id == owner_id)
            .first()
        ) or db.query(ProjectConfig).filter(ProjectConfig.key == project_key).first()
        if cfg is not None:
            conn = get_connection(db, cfg.repository_connection_id, owner_id=owner_id)
            if conn is not None:
                return conn
    conn = _first_with_capability(db, REPOSITORY, owner_id=owner_id)
    if conn is not None:
        return conn
    raise ProviderError("Repository provider is not configured")


# ------------------------------------------------------- test-case routing
def resolve_test_case_for_project(
    db: Session, project_key: str | None, owner_id: int | None = None
) -> ProviderConnection:
    """Resolve the connection approved test cases are created/linked in.

    The project's third connection role (ADR 0015 §3, ``TEST CASE TARGET``).
    Order: the project's ``test_case_connection_id`` → its
    ``work_item_connection_id`` (the ticket source, which is what every consumer
    implicitly used before the role existed) → the first work-item-capable
    connection → :class:`ProviderError`.

    The middle step is the whole point: an unset target is not an error, it means
    "the same place the tickets came from", so a project that never opens the
    Connection tab behaves exactly as it did.
    """
    if project_key:
        cfg = (
            db.query(ProjectConfig)
            .filter(ProjectConfig.key == project_key, ProjectConfig.owner_id == owner_id)
            .first()
        ) or db.query(ProjectConfig).filter(ProjectConfig.key == project_key).first()
        if cfg is not None:
            for candidate in (cfg.test_case_connection_id, cfg.work_item_connection_id):
                conn = get_connection(db, candidate, owner_id=owner_id)
                if conn is not None:
                    return conn
    conn = _first_with_capability(db, WORK_ITEM, owner_id=owner_id)
    if conn is not None:
        return conn
    raise ProviderError("Test-case target provider is not configured")


# ---------------------------------------------------------------- backfill
def backfill_from_providers(db: Session) -> None:
    """One-time migration from legacy ``providers`` to ``provider_connections``.

    Best-effort (mirrors ``_backfill_audit``): copies each legacy Provider into a
    ProviderConnection when the new table is empty, then binds every ProjectConfig
    with null bindings to the first connection of each capability and stamps each
    un-stamped ticket's ``connection_id`` from the matching work-item connection.
    Idempotent — the copy is guarded by an emptiness check and only null bindings
    are filled.
    """
    if db.query(ProviderConnection).count() == 0:
        for p in db.query(Provider).all():
            db.add(
                ProviderConnection(
                    kind=p.kind,
                    name=p.name or PROVIDER_DISPLAY_NAMES.get(p.kind, p.kind.upper()),
                    connected=bool(p.connected),
                    config=p.config or {},
                    secrets=p.secrets or {},
                    last_sync=p.last_sync,
                )
            )
        db.commit()

    work_item = _first_with_capability(db, WORK_ITEM)
    repository = _first_with_capability(db, REPOSITORY)
    for cfg in db.query(ProjectConfig).all():
        if cfg.work_item_connection_id is None and work_item is not None:
            cfg.work_item_connection_id = work_item.id
        if cfg.repository_connection_id is None and repository is not None:
            cfg.repository_connection_id = repository.id
        # TEST CASE TARGET defaults to the ticket source (ADR 0015 §3), mirroring
        # migration f6b3d9c14e27 so a fresh `create_all` database and a migrated
        # one agree.
        if cfg.test_case_connection_id is None and cfg.work_item_connection_id is not None:
            cfg.test_case_connection_id = cfg.work_item_connection_id

    for ticket in db.query(Ticket).filter(Ticket.connection_id.is_(None)).all():
        conn = first_of_kind(db, ticket.provider_kind)
        if conn is not None and WORK_ITEM in categories_for(conn.kind):
            ticket.connection_id = conn.id
    db.commit()
