"""add tickets.hub_ticket_id

Maps a local Q-Agent ticket to its EmeHub work item (#500, docs/HUB-INTEGRATION.md
§5). Nullable, indexed, **not** unique — NULL for every ticket the hub has never
been asked about.

The same mapping trick as ``users.hub_user_id`` (``f4b2e8c17a05``), and for the
same reason: it makes the two stores **reconcilable without moving ownership**.
Q-Agent keeps its own ``tickets`` table and its own sync — sync needs a provider
PAT and the hub never hands one out (#497 §4c) — so the value here is that a
later cutover becomes a join rather than a re-import. This column is the durable
part of the slice; the read-through above it can be reverted without touching it.

Not unique, unlike ``users.hub_user_id``: tickets are per-user private data
(``owner_id``), so one hub work item legitimately maps to one row *per owner*.
A unique index would make the second user's reconciliation fail.

**No data migration runs here.** Existing rows keep ``hub_ticket_id`` NULL and
behave exactly as before; the column is only ever written by a hub read, which
is itself behind ``QAGENT_HUB_DATA_ENABLED`` (off by default).

Revision ID: a7d3c9b41e60
Revises: f4b2e8c17a05
Create Date: 2026-08-06 09:20:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a7d3c9b41e60'
down_revision: Union[str, Sequence[str], None] = 'f4b2e8c17a05'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.add_column(sa.Column("hub_ticket_id", sa.String(length=64), nullable=True))
        batch_op.create_index("ix_tickets_hub_ticket_id", ["hub_ticket_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.drop_index("ix_tickets_hub_ticket_id")
        batch_op.drop_column("hub_ticket_id")
