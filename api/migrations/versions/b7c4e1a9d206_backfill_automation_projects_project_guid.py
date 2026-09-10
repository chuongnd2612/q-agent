"""backfill automation_projects.project_guid from the specs its runs wrote

Slice A of #766. ``AutomationProject.project_guid`` was added and indexed by
``c3f81a6d47e2`` and then written by nothing since, so every row created after
that migration is NULL. The project-scoped Automation tab keys on the column,
so the rows that already exist have to be resolved **once**, here.

Data-only: the column and its index already exist, so there is no schema change.

**The resolution is a join, not a name match.** ``automation_projects`` is keyed
on ``(owner_id, provider project_key, repo)`` — ``project_key`` is the
*provider's* project key, not the q-agent project's name. Verified on live data:
a q-agent project named ``demo`` owns an automation row keyed ``surency``, so
matching ``project_config.key`` or ``projects.name`` resolves to NULL for it and
the project shows "no automation repo" despite having a completed run. Ground
truth is what the runs actually wrote::

    automation_specs.project_id = automation_projects.id
      JOIN test_cases  ON test_cases.id = automation_specs.test_case_id
      JOIN runs        ON runs.id       = test_cases.run_id
     WHERE runs.project_guid IS NOT NULL

A row is stamped **only when that join yields exactly one distinct
``runs.project_guid``**. Zero means nothing has been written into the repo yet
(a bare scaffold), and more than one means the repo is genuinely **shared**:
because the key does not mention the q-agent project, two projects whose runs
target the same provider project and repo legitimately write into one row. Live
data has such a row today. Picking "the latest" would attach it to one project
and silently hide it from the other, so an ambiguous row stays NULL and is found
instead by the second leg of ``automation_project_service.projects_for_guid``,
which runs this same join per request and therefore returns the row for *every*
project that wrote into it.

Written against Alembic's own connection with bound parameters compared using
``=`` rather than ``IS`` so it runs identically on SQLite and Postgres.

Revision ID: b7c4e1a9d206
Revises: f6b3d9c14e27
Create Date: 2026-09-11 10:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7c4e1a9d206"
down_revision: Union[str, Sequence[str], None] = "f6b3d9c14e27"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Every table the join needs. A database migrated only part-way through the
#: project refactor skips the backfill rather than failing the upgrade.
_REQUIRED_TABLES = ("automation_projects", "automation_specs", "test_cases", "runs")


def upgrade() -> None:
    """Stamp each unambiguous ``automation_projects`` row that has no GUID yet."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not all(inspector.has_table(table) for table in _REQUIRED_TABLES):
        return
    ids = [
        row.id
        for row in conn.execute(
            sa.text("SELECT id FROM automation_projects WHERE project_guid IS NULL")
        ).fetchall()
    ]
    for project_id in ids:
        guid = _sole_project_guid(conn, project_id)
        if guid:
            conn.execute(
                sa.text("UPDATE automation_projects SET project_guid = :g WHERE id = :i"),
                {"g": guid, "i": project_id},
            )


def downgrade() -> None:
    """Clear the column — the backfill is the whole of this revision."""
    conn = op.get_bind()
    if not sa.inspect(conn).has_table("automation_projects"):
        return
    conn.execute(sa.text("UPDATE automation_projects SET project_guid = NULL"))


def _sole_project_guid(conn, project_id: int) -> str | None:
    """The one q-agent project whose runs wrote into ``project_id``, if there is one.

    Args:
        conn: Alembic's bound connection.
        project_id: An ``automation_projects.id``.

    Returns:
        The single distinct ``runs.project_guid`` reached through the
        spec -> test case -> run join, or ``None`` when the join yields none (an
        untouched scaffold) **or** more than one (a repo shared by several
        projects). Both are expected outcomes, not errors: NULL keeps the row
        unclaimed rather than forcing it onto one project.
    """
    guids = conn.execute(
        sa.text(
            "SELECT DISTINCT r.project_guid AS guid"
            "  FROM automation_specs s"
            "  JOIN test_cases tc ON tc.id = s.test_case_id"
            "  JOIN runs r ON r.id = tc.run_id"
            " WHERE s.project_id = :p AND r.project_guid IS NOT NULL"
        ),
        {"p": project_id},
    ).fetchall()
    return guids[0].guid if len(guids) == 1 else None
