"""add test_cases.voice_findings (QC-voice gate findings)

Slice #829 of the Business Knowledge epic (#813). The QC-voice gate
(:mod:`app.services.qc_voice_gate`, #823) now runs over every generated case in
``ai_service._case_kwargs_from_raw``. A case that still trips the gate after its
one allowed regenerate is **persisted anyway** with the findings stamped here —
the pipeline's convention is to degrade and show, not to drop. The Review Center
reads this column to badge the case as "technical wording".

Added **nullable with a backfill to** ``'[]'`` rather than with a
``server_default``, matching ``e2a7d40c9b15`` / ``c5e9b3a71d84``: a JSON server
default is the one piece that does not render identically on SQLite and
PostgreSQL. Every pre-existing case therefore reads as "no findings", which is
the correct meaning for a case generated before the gate existed — not "clean,
verified", but "nothing to show", which is how the UI treats an empty list.

Runs on PostgreSQL and SQLite — the alteration goes through
``batch_alter_table`` (SQLite's table-rebuild path). Reversible.

Revision ID: a1d7c30f95b2
Revises: e2a7d40c9b15
Create Date: 2026-09-13 09:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1d7c30f95b2"
down_revision: Union[str, Sequence[str], None] = "e2a7d40c9b15"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("test_cases") as batch_op:
        batch_op.add_column(sa.Column("voice_findings", sa.JSON(), nullable=True))
    op.execute("UPDATE test_cases SET voice_findings = '[]' WHERE voice_findings IS NULL")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("test_cases") as batch_op:
        batch_op.drop_column("voice_findings")
