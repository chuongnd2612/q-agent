"""add agent_planning_sessions

The queue that lets `testCaseMode="live-planner"` run on the paired Local Agent
instead of the API host (#900) — the last live-browser action that ignored
`executionTarget="local-agent"`.

Durable (a table, not process memory) because the planner's caller waits inline
for the result: with more than one API worker an in-memory queue would let the
device's claim land on a worker other than the one waiting, and every run would
time out while the device planned happily.

Revision ID: d1a9f37c5b84
Revises: 489677867455
Create Date: 2026-09-22 10:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

import app.db

# revision identifiers, used by Alembic.
revision: str = "d1a9f37c5b84"
down_revision: Union[str, Sequence[str], None] = "489677867455"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "agent_planning_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=True),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("project_key", sa.String(length=200), nullable=False),
        sa.Column("repo", sa.String(length=200), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=False),
        sa.Column("origin", sa.String(length=500), nullable=False),
        sa.Column("run_code", sa.String(length=64), nullable=False),
        sa.Column("ticket", sa.String(length=120), nullable=False),
        sa.Column("sidecar_filename", sa.String(length=200), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("task_prompt", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("max_budget_usd", sa.Float(), nullable=False),
        sa.Column("log_verbosity", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", app.db.UTCDateTime(), nullable=False),
        sa.Column("claimed_at", app.db.UTCDateTime(), nullable=True),
        sa.Column("finished_at", app.db.UTCDateTime(), nullable=True),
        sa.Column("plan_json", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_agent_planning_sessions_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_agent_planning_sessions_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_agent_planning_sessions_session_id"),
        "agent_planning_sessions",
        ["session_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_agent_planning_sessions_owner_id"),
        "agent_planning_sessions",
        ["owner_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_planning_sessions_run_id"),
        "agent_planning_sessions",
        ["run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_planning_sessions_status"),
        "agent_planning_sessions",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_agent_planning_sessions_status"), table_name="agent_planning_sessions")
    op.drop_index(op.f("ix_agent_planning_sessions_run_id"), table_name="agent_planning_sessions")
    op.drop_index(op.f("ix_agent_planning_sessions_owner_id"), table_name="agent_planning_sessions")
    op.drop_index(
        op.f("ix_agent_planning_sessions_session_id"), table_name="agent_planning_sessions"
    )
    op.drop_table("agent_planning_sessions")
