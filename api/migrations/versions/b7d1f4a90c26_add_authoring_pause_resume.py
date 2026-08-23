"""add pause/resume columns to agent_authoring_sessions

#619 — "stop at the mid of the authoring process, feed more input, then
continue". A paused live-authoring session is held open ON THE DEVICE: Chrome
stays up (so the user can click their way to the awkward screen), the temp
workdir stays on disk, and `CLAUDE_CONFIG_DIR` — which holds the transcript
`claude --resume` needs — stays with it. None of that state may live in a
process (#605/#625), so everything the pause protocol waits on is a column here:

* ``pause_requested`` — the user's Pause, delivered to the device on the progress
  channel it already posts on once per Claude step (no new poller).
* ``paused_at`` — drives the pause EXPIRY that tears the browser + workdir down
  when a pause is forgotten, and replaces ``claimed_at`` as the staleness clock
  while paused (a long pause is not an abandoned claim).
* ``claude_session_id`` — Claude CLI's own session id, read off the
  ``--output-format stream-json`` envelope. Empty ⇒ Continue must fall back to a
  fresh guided pass instead of ``--resume``.
* ``guidance`` / ``guidance_history`` — JSON arrays of what the user typed in the
  spec chat: the undelivered turn, and everything ever accepted (a fallback pass
  has no Claude memory, so it must carry the whole accumulated intent).
* ``cost_usd_so_far`` / ``resume_count`` — the cost ceiling is a SESSION budget
  spanning resumes, so each resume is handed the REMAINDER, not the full ceiling.

Revision ID: b7d1f4a90c26
Revises: a4c8e5b19d73
Create Date: 2026-08-23 00:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

import app.db

# revision identifiers, used by Alembic.
revision: str = "b7d1f4a90c26"
down_revision: Union[str, Sequence[str], None] = "a4c8e5b19d73"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_COLUMNS = (
    ("pause_requested", sa.Boolean(), False, "0"),
    ("paused_at", app.db.UTCDateTime(), True, None),
    ("claude_session_id", sa.String(length=120), False, "''"),
    ("guidance", sa.Text(), False, "''"),
    ("guidance_history", sa.Text(), False, "''"),
    ("cost_usd_so_far", sa.Float(), False, "0"),
    ("resume_count", sa.Integer(), False, "0"),
)


def upgrade() -> None:
    """Upgrade schema."""
    existing = {
        c["name"] for c in sa.inspect(op.get_bind()).get_columns("agent_authoring_sessions")
    }
    for name, type_, nullable, default in _COLUMNS:
        if name in existing:
            continue
        op.add_column(
            "agent_authoring_sessions",
            sa.Column(name, type_, nullable=nullable, server_default=default),
        )


def downgrade() -> None:
    """Downgrade schema."""
    existing = {
        c["name"] for c in sa.inspect(op.get_bind()).get_columns("agent_authoring_sessions")
    }
    for name, *_rest in reversed(_COLUMNS):
        if name in existing:
            op.drop_column("agent_authoring_sessions", name)
