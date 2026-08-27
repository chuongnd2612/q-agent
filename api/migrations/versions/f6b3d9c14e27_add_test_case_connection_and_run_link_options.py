"""add project_config.test_case_connection_id + runs link options

Slice 6 of #726 (ADR 0015 §3 and §5).

**TEST CASE TARGET.** A project binds three connection roles, and only two of them
existed (ADR 0006 §3): the work-item connection tickets come from, and the
repository connection code lives in. The third — where approved cases are created,
linked and published back to — was implicit: every consumer just reused the
ticket source. This adds the column and makes that implicit default *explicit* by
backfilling it from ``work_item_connection_id``, so an existing project keeps the
exact behaviour it had while gaining a binding it can now change.

Rows whose ``work_item_connection_id`` is NULL stay NULL: there is nothing to
default *to*, and ``resolve_test_case_for_project`` falls back through the same
chain the old implicit behaviour used.

**Run link options.** Hiding the ``sync`` stage removes the screen that owned
*link or not*, *which subset of tickets* and *dry run*, so those move into the
Create Run modal and have to be stored on the run. Defaults reproduce today's
behaviour exactly (link on, dry run off, no subset), which is why every existing
row can be backfilled with them and nothing changes for a run created before this.

Revision ID: f6b3d9c14e27
Revises: a1e5d2c47b93
Create Date: 2026-08-27 10:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f6b3d9c14e27"
down_revision: Union[str, Sequence[str], None] = "a1e5d2c47b93"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(conn, table: str) -> set[str]:
    inspector = sa.inspect(conn)
    if not inspector.has_table(table):
        return set()
    return {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()

    project_config_columns = _columns(conn, "project_config")
    if project_config_columns and "test_case_connection_id" not in project_config_columns:
        with op.batch_alter_table("project_config") as batch_op:
            batch_op.add_column(sa.Column("test_case_connection_id", sa.Integer(), nullable=True))
        # Default to the ticket source (ADR 0015 §3). Deliberately not a FK
        # constraint: `provider_connections` rows are deleted freely and the delete
        # path already nulls these bindings by hand (`routers/providers.py`), so a
        # constraint added under SQLite's batch-rebuild would buy nothing and could
        # fail on a database that already holds a dangling id.
        conn.execute(
            sa.text(
                "UPDATE project_config SET test_case_connection_id = work_item_connection_id"
                " WHERE test_case_connection_id IS NULL"
                " AND work_item_connection_id IS NOT NULL"
            )
        )

    run_columns = _columns(conn, "runs")
    if run_columns:
        with op.batch_alter_table("runs") as batch_op:
            if "link_enabled" not in run_columns:
                batch_op.add_column(
                    sa.Column("link_enabled", sa.Boolean(), nullable=True, server_default=sa.true())
                )
            if "link_dry_run" not in run_columns:
                batch_op.add_column(
                    sa.Column(
                        "link_dry_run", sa.Boolean(), nullable=True, server_default=sa.false()
                    )
                )
            if "link_ticket_ids" not in run_columns:
                batch_op.add_column(sa.Column("link_ticket_ids", sa.JSON(), nullable=True))
        # Backfill so no row is left NULL on a column the ORM types as non-optional.
        conn.execute(sa.text("UPDATE runs SET link_enabled = true WHERE link_enabled IS NULL"))
        conn.execute(sa.text("UPDATE runs SET link_dry_run = false WHERE link_dry_run IS NULL"))
        conn.execute(
            sa.text("UPDATE runs SET link_ticket_ids = '[]' WHERE link_ticket_ids IS NULL")
        )


def downgrade() -> None:
    """Downgrade schema."""
    conn = op.get_bind()

    if "test_case_connection_id" in _columns(conn, "project_config"):
        with op.batch_alter_table("project_config") as batch_op:
            batch_op.drop_column("test_case_connection_id")

    run_columns = _columns(conn, "runs")
    if run_columns:
        with op.batch_alter_table("runs") as batch_op:
            for name in ("link_ticket_ids", "link_dry_run", "link_enabled"):
                if name in run_columns:
                    batch_op.drop_column(name)
