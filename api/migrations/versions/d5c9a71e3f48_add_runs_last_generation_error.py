"""add runs.last_generation_error

#641 — make a failed automation-generation pass survive the moment it happened.

``_run_generation`` catches each case's exception, logs it, and publishes an
``automation.progress`` event with ``"Error: {exc}"`` over the run WebSocket.
Nothing persists it. The pass then flips the run to ``automation`` exactly as a
successful one does, so afterwards the UI cannot distinguish "generation was
attempted and every case failed" from "there was nothing to generate" — it shows
the generic *"No automation yet"* either way.

That is not a cosmetic gap. The two most common failures are missing
prerequisites raised by ``_enqueue_agent_authoring`` under the shipped defaults
(``authoringMode=live-harness`` + ``executionTarget=local-agent``, #161):

    ValueError("No base URL in the project context — configure it before live authoring.")
    ValueError("No local agent paired — start your local agent to author live.")

Both are one-click fixable and both were invisible unless the user happened to be
watching the screen at that second.

Nullable, and left NULL here: the column records the outcome of the NEXT pass, so
there is nothing to backfill — a run that has never failed simply has no error.
JSON in a Text column rather than a JSON column, matching the existing
``gate_report`` / ``plan_report`` / ``heal_report`` convention on
``automation_specs``.

Revision ID: d5c9a71e3f48
Revises: b7d1f4a90c26
Create Date: 2026-08-24 03:20:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d5c9a71e3f48"
down_revision: Union[str, Sequence[str], None] = "b7d1f4a90c26"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("runs") as batch_op:
        batch_op.add_column(sa.Column("last_generation_error", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_column("last_generation_error")
