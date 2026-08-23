"""add agent_authoring_sessions (durable live-authoring queue)

#605. The agent-driven live-authoring queue used to be a module-level ``list``
in ``agent_authoring_service`` — process-local, so any API restart lost every
queued session while the ``AutomationSpec`` row stayed committed at
``status="running"`` with empty ``code``, hanging the spec forever. This table
makes the queue durable (and correct under more than one API worker).

The table holds only **live** work (``queued``/``running``); rows are deleted on
finalize / stop / eviction. ``case_id`` is UNIQUE — the #419 "one live authoring
session per case" guard, now enforced by the database instead of by a check
inside a single process's lock.

Revision ID: f3a1d0c7b592
Revises: e7b25c1f9a04
Create Date: 2026-08-23 00:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

import app.db

# revision identifiers, used by Alembic.
revision: str = "f3a1d0c7b592"
down_revision: Union[str, Sequence[str], None] = "e7b25c1f9a04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "agent_authoring_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=True),
        sa.Column("case_id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("project_key", sa.String(length=200), nullable=False),
        sa.Column("repo", sa.String(length=200), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=False),
        sa.Column("origin", sa.String(length=500), nullable=False),
        sa.Column("spec_filename", sa.String(length=500), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("task_prompt", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("max_budget_usd", sa.Float(), nullable=False),
        sa.Column("log_verbosity", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", app.db.UTCDateTime(), nullable=False),
        sa.Column("claimed_at", app.db.UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_agent_authoring_sessions_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["test_cases.id"],
            name=op.f("fk_agent_authoring_sessions_case_id_test_cases"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_agent_authoring_sessions_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_agent_authoring_sessions_session_id"),
        "agent_authoring_sessions",
        ["session_id"],
        unique=True,
    )
    # One LIVE session per case — the #419 double-authoring guard.
    op.create_index(
        op.f("ix_agent_authoring_sessions_case_id"),
        "agent_authoring_sessions",
        ["case_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_agent_authoring_sessions_owner_id"),
        "agent_authoring_sessions",
        ["owner_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_authoring_sessions_run_id"),
        "agent_authoring_sessions",
        ["run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_authoring_sessions_status"),
        "agent_authoring_sessions",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f("ix_agent_authoring_sessions_status"), table_name="agent_authoring_sessions"
    )
    op.drop_index(
        op.f("ix_agent_authoring_sessions_run_id"), table_name="agent_authoring_sessions"
    )
    op.drop_index(
        op.f("ix_agent_authoring_sessions_owner_id"), table_name="agent_authoring_sessions"
    )
    op.drop_index(
        op.f("ix_agent_authoring_sessions_case_id"), table_name="agent_authoring_sessions"
    )
    op.drop_index(
        op.f("ix_agent_authoring_sessions_session_id"), table_name="agent_authoring_sessions"
    )
    op.drop_table("agent_authoring_sessions")
