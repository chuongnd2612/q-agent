"""add runs.project_guid (stamped, backfilled from the run's first ticket)

Slice 1 of #726 / ADR 0015 (#727). A run's project used to be *derived on read*:
``_resolve_run_project_key()`` walked the run's first ``run_ticket`` to a ticket,
the ticket to its work-item connection, and the connection to a project. ADR 0013
recorded the two problems with that. It is simply wrong for a run whose tickets
span projects — the first ticket decides for all of them. And it makes
project-scoped listing impossible: you cannot put a per-row Python derivation in
a WHERE clause, so ``GET /runs?project=…`` had nothing to filter on.

So the value becomes a column, stamped at creation. This migration adds it and
runs that same walk **once**, here, for the rows that already exist.

The backfill mirrors ``project_config_service.resolve_project_key`` in SQL:

1. the ticket's ``connection_id`` matched against
   ``project_config.work_item_connection_id`` — the id link the user actually
   configured, and the only unambiguous step (two connections can report the same
   provider project name; their ids differ);
2. failing that, the sole project the run's owner can see.

Anything unresolved stays NULL, which the API surfaces as the explicit
``?project=unassigned`` bucket rather than hiding. The column is nullable for the
same reason ``owner_id`` still is: enforcing non-null belongs to a cleanup pass,
after a real database has been observed to have no NULLs left.

Revision ID: a1e5d2c47b93
Revises: d5c9a71e3f48
Create Date: 2026-08-27 09:40:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a1e5d2c47b93'
down_revision: Union[str, Sequence[str], None] = 'd5c9a71e3f48'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    with op.batch_alter_table("runs") as batch_op:
        batch_op.add_column(sa.Column("project_guid", sa.String(length=36), nullable=True))
        batch_op.create_index("ix_runs_project_guid", ["project_guid"], unique=False)
    _backfill(conn)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_index("ix_runs_project_guid")
        batch_op.drop_column("project_guid")


def _backfill(conn) -> None:
    if not sa.inspect(conn).has_table("project_config"):
        return
    runs = conn.execute(sa.text("SELECT id, owner_id FROM runs")).fetchall()
    for run in runs:
        guid = _guid_for_run(conn, run.id, run.owner_id)
        if guid:
            conn.execute(
                sa.text("UPDATE runs SET project_guid = :g WHERE id = :i"),
                {"g": guid, "i": run.id},
            )


def _guid_for_run(conn, run_id: int, owner_id: int | None) -> str | None:
    ticket = conn.execute(
        sa.text(
            "SELECT t.connection_id AS connection_id"
            "  FROM run_tickets rt"
            "  JOIN tickets t ON t.external_id = rt.ticket_external_id"
            " WHERE rt.run_id = :r"
            " ORDER BY rt.position, rt.id"
            " LIMIT 1"
        ),
        {"r": run_id},
    ).fetchone()
    if ticket is None:
        return None

    # (1) The configured id link. More than one config claiming a connection is a
    # data error, not something to pick a winner from — leave it unresolved.
    if ticket.connection_id is not None:
        rows = conn.execute(
            sa.text(
                "SELECT project_guid, key FROM project_config"
                " WHERE work_item_connection_id = :c"
            ),
            {"c": ticket.connection_id},
        ).fetchall()
        if len(rows) == 1:
            return _guid_of(conn, rows[0], owner_id)

    # (2) The sole-project fallback, scoped to rows this run's owner can see.
    rows = conn.execute(
        sa.text(
            "SELECT project_guid, key FROM project_config"
            " WHERE (owner_id = :o OR owner_id IS NULL)"
        ),
        {"o": owner_id},
    ).fetchall()
    if len(rows) == 1:
        return _guid_of(conn, rows[0], owner_id)
    return None


def _guid_of(conn, config_row, owner_id: int | None) -> str | None:
    """The project GUID a config row points at — its own link, else by name.

    ``project_config.project_guid`` was added by the #585 G1 bridge and is
    populated for rows that existed then, but a config created since could still
    be NULL, so the name lookup stays as a second leg.
    """
    if config_row.project_guid:
        return config_row.project_guid
    if not config_row.key:
        return None
    found = conn.execute(
        sa.text(
            "SELECT guid FROM projects"
            " WHERE lower(name) = lower(:n) AND (owner_id = :o OR owner_id IS NULL)"
            " ORDER BY CASE WHEN owner_id IS NULL THEN 1 ELSE 0 END, id"
            " LIMIT 1"
        ),
        {"n": config_row.key, "o": owner_id},
    ).fetchone()
    return found.guid if found else None
