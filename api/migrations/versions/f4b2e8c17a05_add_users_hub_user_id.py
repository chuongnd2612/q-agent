"""add users.hub_user_id

Maps a local Q-Agent account to its EmeHub user (#478, docs/HUB-INTEGRATION.md
§3.1). Nullable, unique, indexed — NULL for local-only accounts.

A hub agent token's ``sub`` claim is a **hub** user id and never equals a local
``users.id``, so hub tokens resolve through this column instead. That keeps every
local id untouched, which is the point: nearly every table carries
``owner_id -> users.id`` and the per-user workspace is a path built from it
(ADR 0009), so re-pointing ``owner_id`` at hub ids would be a migration across
every scoped table. **No data migration runs here** — existing rows keep
``hub_user_id`` NULL and behave exactly as before.

The unique constraint is what makes the JIT-provisioning path in B2 (#479) safe:
two logins with the same hub ``sub`` cannot create duplicate local accounts.
Uniqueness on a nullable column ignores NULLs on both SQLite and Postgres, so
any number of local-only users coexist.

Revision ID: f4b2e8c17a05
Revises: e1c9a4f70d52
Create Date: 2026-08-05 14:10:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f4b2e8c17a05'
down_revision: Union[str, Sequence[str], None] = 'e1c9a4f70d52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("hub_user_id", sa.String(length=64), nullable=True))
        batch_op.create_index("ix_users_hub_user_id", ["hub_user_id"], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_index("ix_users_hub_user_id")
        batch_op.drop_column("hub_user_id")
