"""add projects.guid + project_guid on config/knowledge/automation

G1 of #585 — give a project a stable identity that is not its display name.

Everything hung off the **name**: routes were ``/projects/{name}/…`` and
``project_config`` / ``project_knowledge`` / ``automation_projects`` all keyed by
that string. Two users with the same project name collided (#583) and a rename
orphaned everything the project owned.

There was also no canonical project *entity*: measured on the live database, only
2 of 6 ``project_config`` rows and 4 of 5 ``project_knowledge`` rows had a
matching ``projects`` row at all. So this migration does two things:

1. Adds ``projects.guid`` (UUID, unique, indexed) and fills it for every row.
2. Adds a nullable ``project_guid`` to the three dependent tables and backfills it
   by matching ``(name, owner_id)`` — **creating a ``projects`` row** for any
   config/knowledge that never had one, so the reference can exist.

``project_guid`` stays nullable and the legacy key columns stay in place: G1 ships
a bridge on purpose, because renaming identity across 288 call sites, 21 routes
and 25 frontend sites in one commit is how a refactor this size breaks quietly.
G4 drops both once nothing reads them.

Owner-matched deliberately. A config owned by user A and one owned by user B may
share a name — that is the collision this exists to fix — so each is linked to its
own project, and a synthesised project inherits the config's owner.

Revision ID: c3f81a6d47e2
Revises: d9a4c7e12b58
Create Date: 2026-08-13 09:20:00.000000

"""

from __future__ import annotations

import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c3f81a6d47e2'
down_revision: Union[str, Sequence[str], None] = 'd9a4c7e12b58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()

    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(sa.Column("guid", sa.String(length=36), nullable=True))

    # Fill every existing project. Done row-by-row rather than with a SQL UUID
    # function because those differ across SQLite and Postgres, and this runs on
    # both.
    rows = conn.execute(sa.text("SELECT id FROM projects")).fetchall()
    for (project_id,) in rows:
        conn.execute(
            sa.text("UPDATE projects SET guid = :guid WHERE id = :id"),
            {"guid": str(uuid.uuid4()), "id": project_id},
        )

    with op.batch_alter_table("projects") as batch_op:
        batch_op.create_index("ix_projects_guid", ["guid"], unique=True)

    for table, key_column in (
        ("project_config", "key"),
        ("project_knowledge", "project_key"),
        ("automation_projects", "project_key"),
    ):
        if not _has_table(conn, table):
            continue
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(sa.Column("project_guid", sa.String(length=36), nullable=True))
            batch_op.create_index(f"ix_{table}_project_guid", ["project_guid"], unique=False)
        _backfill(conn, table, key_column)


def _has_table(conn, table: str) -> bool:
    return sa.inspect(conn).has_table(table)


def _backfill(conn, table: str, key_column: str) -> None:
    """Point each row at a project guid, synthesising the project when absent."""
    has_owner = any(
        col["name"] == "owner_id" for col in sa.inspect(conn).get_columns(table)
    )
    owner_expr = "owner_id" if has_owner else "NULL AS owner_id"
    rows = conn.execute(
        sa.text(f"SELECT id, {key_column} AS k, {owner_expr} FROM {table}")
    ).fetchall()

    for row in rows:
        name, owner_id = row.k, row.owner_id
        if not name:
            continue
        # Owner-matched: two users may legitimately share a project name (#583),
        # so each links to their own project rather than to whichever row is first.
        if owner_id is None:
            found = conn.execute(
                sa.text("SELECT guid FROM projects WHERE name = :n AND owner_id IS NULL"),
                {"n": name},
            ).fetchone()
        else:
            found = conn.execute(
                sa.text("SELECT guid FROM projects WHERE name = :n AND owner_id = :o"),
                {"n": name, "o": owner_id},
            ).fetchone()

        if found is None:
            # No project row ever existed for this name — the common case for
            # config/knowledge created before `projects` was populated. Synthesise
            # one so the reference has a target, inheriting the row's owner.
            guid = str(uuid.uuid4())
            conn.execute(
                sa.text(
                    "INSERT INTO projects (guid, provider_kind, external_id, name, active,"
                    " meta, created_at, owner_id)"
                    " VALUES (:guid, '', :name, :name, :active, '{}', CURRENT_TIMESTAMP, :owner)"
                ),
                {"guid": guid, "name": name, "active": False, "owner": owner_id},
            )
        else:
            guid = found.guid

        conn.execute(
            sa.text(f"UPDATE {table} SET project_guid = :guid WHERE id = :id"),
            {"guid": guid, "id": row.id},
        )


def downgrade() -> None:
    """Downgrade schema.

    Drops the columns only. Projects synthesised during backfill are left in
    place: they represent config/knowledge that genuinely existed, and deleting
    them on a downgrade would destroy data the upgrade merely described.
    """
    conn = op.get_bind()
    for table in ("project_config", "project_knowledge", "automation_projects"):
        if not _has_table(conn, table):
            continue
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_index(f"ix_{table}_project_guid")
            batch_op.drop_column("project_guid")

    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_index("ix_projects_guid")
        batch_op.drop_column("guid")
