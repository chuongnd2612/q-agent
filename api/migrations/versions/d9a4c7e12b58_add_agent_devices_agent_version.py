"""add agent_devices.agent_version

Version-skew guard for the layered automation architecture (#541, epic #537).
``AgentDevice`` recorded no version, so the server could not tell whether a
claiming device understands the ``project`` bundle in the ``/agent/jobs/next``
payload. An old agent would flatten the nested tree into its workdir and every
import would fail collection — a silent mass failure across the whole run.

The column is NOT NULL with a ``''`` server default rather than nullable: an
empty string is the unambiguous "never reported" marker, and
``agent_project_bundle.version_ok('')`` is False, so every pre-upgrade device is
treated as **below minimum** without a backfill.

Runs on PostgreSQL and SQLite: the alteration goes through
``batch_alter_table`` (ADR 0009 precedent) so SQLite uses its table-rebuild
path for the NOT NULL addition.

Revision ID: d9a4c7e12b58
Revises: c3f81a9d4b27
Create Date: 2026-08-12 00:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd9a4c7e12b58'
down_revision: Union[str, Sequence[str], None] = 'c3f81a9d4b27'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('agent_devices') as batch_op:
        batch_op.add_column(
            sa.Column('agent_version', sa.String(length=32), nullable=False, server_default='')
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('agent_devices') as batch_op:
        batch_op.drop_column('agent_version')
