"""add browser_driver to agent_authoring_sessions

Carries Settings' `browserDriver` (#875 — "browser-harness" or "playwright-cli")
into a queued local-agent authoring session, so the agent knows which CLI to
provision and which env vars to set when it claims the job — mirroring the
server-side path (`live_authoring_service.methodology_for`), which resolves the
same setting to pick the matching skill.

NOT NULL with a server default so existing queued rows (created before this
setting existed) get the pre-#875 behavior (browser-harness) rather than an
unset value the agent would have to special-case.

Revision ID: 489677867455
Revises: c7f2b48d1e93
Create Date: 2026-09-20 19:16:03.662114

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '489677867455'
down_revision: Union[str, Sequence[str], None] = 'c7f2b48d1e93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("agent_authoring_sessions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "browser_driver",
                sa.String(length=24),
                nullable=False,
                server_default="browser-harness",
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("agent_authoring_sessions") as batch_op:
        batch_op.drop_column("browser_driver")
