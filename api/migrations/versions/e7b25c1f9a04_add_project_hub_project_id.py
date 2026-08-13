"""add projects.hub_project_id

#587 — record which EmeHub project a mirrored row corresponds to.

With ``QAGENT_HUB_DATA_ENABLED`` on, EmeHub owns project configuration and
Q-Agent shows it read-only. A read-only screen that cannot say *where* to make
the change is only half the answer, so the settings tab deep-links to the hub's
project screen — which addresses projects by **numeric id**
(``<hub web origin>/app/projects/{id}``), not by key.

Nullable on purpose and left NULL by this migration: only the mirror
(``hub_workspace.ensure_projects``) knows a project's hub id, and it re-runs on
every hub-backed read, so existing rows fill themselves in on the next mirror
rather than being guessed at here. A project with no hub id gets a generic
"manage in EmeHub" hint instead of a broken link.

Follows the existing ``provider_connections.hub_connection_id`` /
``tickets.hub_ticket_id`` convention: String(64), indexed, nullable.

Revision ID: e7b25c1f9a04
Revises: c3f81a6d47e2
Create Date: 2026-08-13 12:10:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e7b25c1f9a04"
down_revision: Union[str, Sequence[str], None] = "c3f81a6d47e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(sa.Column("hub_project_id", sa.String(length=64), nullable=True))
    op.create_index("ix_projects_hub_project_id", "projects", ["hub_project_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_projects_hub_project_id", table_name="projects")
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_column("hub_project_id")
