"""add business_source.secrets (per-source wiki token)

Slice #822 of the Business Knowledge epic (#813). One column, holding encrypted
secret values for a single source, in exactly the shape
``provider_connections.secrets`` already uses — same JSON-of-encrypted-strings,
same :mod:`app.crypto` helpers. No new secret-storage mechanism is introduced.

Why a source needs its own credential at all, rather than borrowing the
project's Azure DevOps connection: a **hub-backed** connection holds no PAT and
never will (EmeHub returns ``hasPat`` only and never releases the token, #501),
and ``hub_client`` has no wiki endpoint to route around it with. Even a locally
held ADO PAT is usually scoped to work items (``vso.work``) rather than wikis
(``vso.wiki``), so it 401s on ``/_apis/wiki/wikis``. A per-source, wiki-scoped
token is therefore the path that always works.

Added **nullable with a backfill to** ``'{}'`` rather than with a
``server_default``, matching ``c5e9b3a71d84``'s handling of
``project_config.business_brief``: a JSON server default is the one piece that
does not render identically on SQLite and PostgreSQL.

Runs on PostgreSQL and SQLite — the alteration goes through
``batch_alter_table`` (SQLite's table-rebuild path). Reversible: ``downgrade``
drops the column, which also destroys the stored tokens, as a credential column
should.

Revision ID: e2a7d40c9b15
Revises: c5e9b3a71d84
Create Date: 2026-09-12 12:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e2a7d40c9b15"
down_revision: Union[str, Sequence[str], None] = "c5e9b3a71d84"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("business_source") as batch_op:
        batch_op.add_column(sa.Column("secrets", sa.JSON(), nullable=True))
    # Backfill so no row is left NULL on a column the ORM types as non-optional.
    op.execute("UPDATE business_source SET secrets = '{}' WHERE secrets IS NULL")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("business_source") as batch_op:
        batch_op.drop_column("secrets")
