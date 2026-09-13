"""add business_fact.revision + updated_by (the correction overlay's versioning)

Slice #827 of the Business Knowledge epic (#813). Two columns, and the reason
they are only two is the decision itself: versioning here is **cheap on
purpose**. A correction carries a revision counter and an author, and the
previous normalized markdown is kept on disk as ``normalized.<hash>.md`` so a
diff is inspectable — but there is **no history table**. A per-revision history
with a diff view and a restore action is a real feature with a real UI, and
nothing has asked for it; ``revision`` is the hook to hang one on if it ever
does (ADR 0016 §5).

``revision`` is added nullable and backfilled to 1 rather than carrying a
``server_default``, matching ``e2a7d40c9b15``: existing ingested rows are at
their first (and only) version, and a re-sync refreshing a row is not a human
revising it, so nothing but a human edit ever moves the counter.

``updated_by`` is a nullable FK to ``users.id`` — an ingested row has no human
author, and the #91 ownership bridge admits an anonymous caller. No ``ON
DELETE`` behaviour is declared for the same reason ``owner_id`` declares none:
users are not deleted in this product, and inventing a cascade for a case that
does not occur is how a correction would quietly disappear if it ever did.

Runs on PostgreSQL and SQLite — the alterations go through
``batch_alter_table`` (SQLite's table-rebuild path). Reversible.

Revision ID: b4d18e3c9a70
Revises: a1d7c30f95b2
Create Date: 2026-09-13 12:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b4d18e3c9a70"
down_revision: Union[str, Sequence[str], None] = "a1d7c30f95b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("business_fact") as batch_op:
        batch_op.add_column(sa.Column("revision", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("updated_by", sa.Integer(), nullable=True))
    # Backfill so no row is left NULL on a column the ORM types as non-optional.
    op.execute("UPDATE business_fact SET revision = 1 WHERE revision IS NULL")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("business_fact") as batch_op:
        batch_op.drop_column("updated_by")
        batch_op.drop_column("revision")
