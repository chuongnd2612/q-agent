"""add the staleness probe columns and a test case's grounded-in snapshot list

Slice #830 of the Business Knowledge epic (#813). ADR 0016 §4 argues the
snapshot model from **attributability**: a generated test case must be traceable
to the exact document version behind it, and an upstream change must surface as
a *stale* badge rather than silently shifting the ground under an approved
artifact. Neither half of that was storable before this revision.

``business_source`` gains four columns rather than one ``stale`` boolean,
because the honest answer has three parts — what the upstream version was when
we fetched, when we last asked, and what the asking said. A bare flag cannot
tell "upstream is unchanged" apart from "nobody has ever looked", and the UI
would then have to pick one of those to claim. ``upstream_rev`` empty means the
source has no cheap probe at all (an upload has no address; a page that carries
neither ``ETag`` nor ``Last-Modified`` cannot be asked), in which case staleness
is time-based and is *labelled* as time-based.

``test_case.grounded_in`` is a JSON list of document-version copies, not a join
to ``business_source``. Deliberately denormalized: the question it answers is
asked months later, about a source that may since have been re-synced, excluded
or deleted, and a foreign key would answer it with today's version — which is
precisely the silent retroactive shift the snapshot model exists to prevent.

Every column is nullable + backfilled rather than carrying a ``server_default``,
matching ``e2a7d40c9b15`` and ``b4d18e3c9a70``. Runs on PostgreSQL and SQLite
(``batch_alter_table`` takes SQLite's table-rebuild path). Reversible.

Revision ID: c7f2b48d1e93
Revises: b4d18e3c9a70
Create Date: 2026-09-13 14:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

import app.db

revision: str = "c7f2b48d1e93"
down_revision: Union[str, Sequence[str], None] = "b4d18e3c9a70"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("business_source") as batch_op:
        batch_op.add_column(sa.Column("upstream_rev", sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column("probed_at", app.db.UTCDateTime(), nullable=True))
        batch_op.add_column(sa.Column("stale", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("probe_error", sa.String(length=1000), nullable=True))
    op.execute("UPDATE business_source SET upstream_rev = '' WHERE upstream_rev IS NULL")
    op.execute("UPDATE business_source SET stale = 0 WHERE stale IS NULL")
    op.execute("UPDATE business_source SET probe_error = '' WHERE probe_error IS NULL")

    with op.batch_alter_table("test_cases") as batch_op:
        batch_op.add_column(sa.Column("grounded_in", sa.JSON(), nullable=True))
    # An existing case genuinely has no recorded grounding — an empty list says
    # "not recorded", which is the true statement. Inventing today's sources for
    # a case generated before this shipped would be the one failure mode the
    # whole attribution story exists to rule out.
    op.execute("UPDATE test_cases SET grounded_in = '[]' WHERE grounded_in IS NULL")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("test_cases") as batch_op:
        batch_op.drop_column("grounded_in")
    with op.batch_alter_table("business_source") as batch_op:
        batch_op.drop_column("probe_error")
        batch_op.drop_column("stale")
        batch_op.drop_column("probed_at")
        batch_op.drop_column("upstream_rev")
