"""add agent_capture_requests (durable manual-login capture queue)

#625, the sibling of #605's ``agent_authoring_sessions``. The Local-Agent
manual-login capture queue used to be a module-level ``list`` in
``agent_capture_service`` — process-local, so an API restart silently dropped a
queued capture (the agent then polled forever for 204, which reads as "the agent
isn't connected") and a multi-worker deployment lost captures with no restart at
all. Since live authoring requires the ``browser-profile`` this capture creates
(#618), a dropped capture presents as "authoring is broken".

The table holds only **live** work (``queued``/``running``); the row is deleted on
completion. ``dedupe_key`` (``owner_id`` + ``project_key``) is UNIQUE — the "one
live capture per owner+project" guard, now enforced by the database instead of by
``is_capturing`` reading one process's list. It is a derived string rather than a
composite UNIQUE index on the two columns because ``owner_id`` is NULL on
auth-disabled installs and SQL treats NULLs as distinct, so a composite index
would not dedupe there at all.

Revision ID: a4c8e5b19d73
Revises: f3a1d0c7b592
Create Date: 2026-08-23 00:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

import app.db

# revision identifiers, used by Alembic.
revision: str = "a4c8e5b19d73"
down_revision: Union[str, Sequence[str], None] = "f3a1d0c7b592"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "agent_capture_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=True),
        sa.Column("project_key", sa.String(length=200), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=False),
        sa.Column("origin", sa.String(length=500), nullable=False),
        sa.Column("dedupe_key", sa.String(length=280), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", app.db.UTCDateTime(), nullable=False),
        sa.Column("claimed_at", app.db.UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_agent_capture_requests_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # One LIVE capture per owner+project — the multi-worker dedupe guard.
    op.create_index(
        op.f("ix_agent_capture_requests_dedupe_key"),
        "agent_capture_requests",
        ["dedupe_key"],
        unique=True,
    )
    op.create_index(
        op.f("ix_agent_capture_requests_owner_id"),
        "agent_capture_requests",
        ["owner_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_capture_requests_status"),
        "agent_capture_requests",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f("ix_agent_capture_requests_status"), table_name="agent_capture_requests"
    )
    op.drop_index(
        op.f("ix_agent_capture_requests_owner_id"), table_name="agent_capture_requests"
    )
    op.drop_index(
        op.f("ix_agent_capture_requests_dedupe_key"), table_name="agent_capture_requests"
    )
    op.drop_table("agent_capture_requests")
