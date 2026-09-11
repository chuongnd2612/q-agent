"""make executions addressable without a run

#796 (foundation for #795) — an Execution is currently pinned to a Run by a NOT
NULL FK, which is what makes "run the suite" reachable only from inside a run.
A project's Automation tab browses the specs accumulated in its automation repo
and must be able to execute a selection of them, where there is no Run, no
RunTicket and no TestCase behind any result.

Three columns and one relaxed constraint:

``executions.owner_id``
    Ownership is currently *derived* by joining Run
    (``routers/agent.py`` scopes a job claim with ``Run.owner_id == user.id``),
    which cannot work for a run-less row. Denormalized onto the execution so
    every scope check has a direct column to read. **Nullable**, mirroring
    ``runs.owner_id``, which is still nullable under the #91/#98 ownership
    bridge — a NOT NULL column here would contradict the rows it is backfilled
    from.

``executions.automation_project_id``
    Which automation repo the execution ran out of. NULL for a run-scoped
    execution, whose specs are resolved through its cases instead.

``execution_results.spec_path``
    A project-scoped result's only identity. Run-scoped results keep using
    ``ticket_external_id``/``case_code`` and leave this empty, so the column is
    additive and no existing matching logic changes.

``executions.run_id`` becomes nullable
    SQLite cannot drop NOT NULL in place, so this runs under
    ``batch_alter_table`` (table recreate). That is also why ``run_id``'s index
    is recreated explicitly below rather than assumed to survive.

The backfill reads ``runs.owner_id`` rather than guessing: every execution that
exists today has a run, so its owner is known exactly.

Revision ID: a1f4c7b92e30
Revises: b7c4e1a9d206
Create Date: 2026-09-11 14:45:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1f4c7b92e30"
down_revision: Union[str, Sequence[str], None] = "b7c4e1a9d206"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("executions") as batch_op:
        batch_op.add_column(sa.Column("owner_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("automation_project_id", sa.Integer(), nullable=True))
        batch_op.alter_column("run_id", existing_type=sa.Integer(), nullable=True)
        # Named explicitly, matching the convention of every other FK here
        # (``op.f("fk_<table>_<column>_<referent>")``). Declared in the batch so
        # they are rendered into the recreated table rather than left to a
        # follow-up ALTER, which SQLite has no syntax for.
        batch_op.create_foreign_key(
            op.f("fk_executions_owner_id_users"), "users", ["owner_id"], ["id"]
        )
        batch_op.create_foreign_key(
            op.f("fk_executions_automation_project_id_automation_projects"),
            "automation_projects",
            ["automation_project_id"],
            ["id"],
        )
    op.create_index("ix_executions_owner_id", "executions", ["owner_id"])
    op.create_index(
        "ix_executions_automation_project_id", "executions", ["automation_project_id"]
    )

    with op.batch_alter_table("execution_results") as batch_op:
        batch_op.add_column(
            sa.Column(
                "spec_path",
                sa.String(length=600),
                nullable=False,
                server_default="",
            )
        )

    # Every pre-existing execution has a run, so its owner is knowable exactly.
    op.execute(
        "UPDATE executions SET owner_id = "
        "(SELECT owner_id FROM runs WHERE runs.id = executions.run_id) "
        "WHERE owner_id IS NULL"
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("execution_results") as batch_op:
        batch_op.drop_column("spec_path")

    # Re-pinning run_id to NOT NULL would fail on any project-scoped execution,
    # which by definition has no run. Drop those rows first: they are unreachable
    # under the old schema anyway, and their results/evidence cascade.
    op.execute("DELETE FROM executions WHERE run_id IS NULL")
    op.drop_index("ix_executions_automation_project_id", table_name="executions")
    op.drop_index("ix_executions_owner_id", table_name="executions")
    with op.batch_alter_table("executions") as batch_op:
        batch_op.alter_column("run_id", existing_type=sa.Integer(), nullable=False)
        batch_op.drop_column("automation_project_id")
        batch_op.drop_column("owner_id")
