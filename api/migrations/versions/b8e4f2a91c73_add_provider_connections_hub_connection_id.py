"""add provider_connections.hub_connection_id

Marks a local connection row as MIRRORING one EmeHub owns (#514). Nullable and
indexed; NULL means an ordinary local connection with its own credential.

A fresh SSO user owns nothing (everything is per-user, ADR 0009), so they landed
in an empty workspace even though the hub held their connection and tickets.
Mirroring gives them real local rows, which is what every downstream feature —
run creation, review, evidence, publish — addresses by primary key; a read-through
list cannot be selected into a run.

A mirrored row's ``secrets`` stay empty: the hub never releases the PAT, so these
are for scoping and display only, never for a direct provider call.

Revision ID: b8e4f2a91c73
Revises: a7d3c9b41e60
Create Date: 2026-08-06 11:40:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b8e4f2a91c73'
down_revision: Union[str, Sequence[str], None] = 'a7d3c9b41e60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("provider_connections") as batch_op:
        batch_op.add_column(sa.Column("hub_connection_id", sa.String(length=64), nullable=True))
        batch_op.create_index(
            "ix_provider_connections_hub_connection_id", ["hub_connection_id"], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("provider_connections") as batch_op:
        batch_op.drop_index("ix_provider_connections_hub_connection_id")
        batch_op.drop_column("hub_connection_id")
