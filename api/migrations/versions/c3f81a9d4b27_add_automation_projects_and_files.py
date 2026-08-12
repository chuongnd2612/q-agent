"""add automation_projects + automation_files + automation_specs.project_id/plan_report

Foundation of the layered Playwright automation architecture (#538, epic #537):
the persistent, git-backed per-project automation project that accumulates page
objects, fixtures and test data across runs.

- ``automation_projects`` — one row per ``(owner_id, project_key, repo)``
  (repo granularity is deliberate: a project with two front-ends must not share
  one page-object namespace), unique-constrained on that triple.
- ``automation_files`` — a queryable mirror of the on-disk tree, unique on
  ``(project_id, path)``. Never the source of truth; reconciled from disk by
  ``automation_project_service.sync_files_to_db``.
- ``automation_specs`` gains two **nullable** columns: ``project_id`` (FK) and
  ``plan_report`` (Text, mirroring the ``gate_report``/``heal_report``
  convention). Nullable means every existing spec keeps working as
  ``project_id IS NULL`` — **no backfill, no history rewrite.**

Runs on PostgreSQL and SQLite: the ``automation_specs`` alterations go through
``batch_alter_table`` (ADR 0009 precedent) so SQLite's table-rebuild path is
used for the added FK.

Revision ID: c3f81a9d4b27
Revises: b8e4f2a91c73
Create Date: 2026-08-12 00:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

import app.db

# revision identifiers, used by Alembic.
revision: str = 'c3f81a9d4b27'
down_revision: Union[str, Sequence[str], None] = 'b8e4f2a91c73'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'automation_projects',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('owner_id', sa.Integer(), nullable=True),
        sa.Column('project_key', sa.String(length=128), nullable=False),
        sa.Column('repo', sa.String(length=200), nullable=False),
        sa.Column('slug', sa.String(length=400), nullable=False),
        sa.Column('root_path', sa.String(length=1000), nullable=False),
        sa.Column('base_version', sa.String(length=32), nullable=False),
        sa.Column('created_at', app.db.UTCDateTime(), nullable=False),
        sa.Column('updated_at', app.db.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ['owner_id'], ['users.id'], name=op.f('fk_automation_projects_owner_id_users')
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'owner_id', 'project_key', 'repo', name='uq_automation_projects_owner_key_repo'
        ),
    )
    op.create_index(
        op.f('ix_automation_projects_owner_id'), 'automation_projects', ['owner_id'], unique=False
    )
    op.create_index(
        op.f('ix_automation_projects_project_key'),
        'automation_projects',
        ['project_key'],
        unique=False,
    )

    op.create_table(
        'automation_files',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('path', sa.String(length=500), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('code', sa.Text(), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('updated_at', app.db.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ['project_id'],
            ['automation_projects.id'],
            name=op.f('fk_automation_files_project_id_automation_projects'),
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('project_id', 'path', name='uq_automation_files_project_path'),
    )
    op.create_index(
        op.f('ix_automation_files_project_id'), 'automation_files', ['project_id'], unique=False
    )

    # Both columns nullable, no backfill: existing specs stay project-less.
    with op.batch_alter_table('automation_specs') as batch_op:
        batch_op.add_column(sa.Column('project_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('plan_report', sa.Text(), nullable=True))
        batch_op.create_foreign_key(
            op.f('fk_automation_specs_project_id_automation_projects'),
            'automation_projects',
            ['project_id'],
            ['id'],
            ondelete='SET NULL',
        )
        batch_op.create_index(
            op.f('ix_automation_specs_project_id'), ['project_id'], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('automation_specs') as batch_op:
        batch_op.drop_index(op.f('ix_automation_specs_project_id'))
        batch_op.drop_constraint(
            op.f('fk_automation_specs_project_id_automation_projects'), type_='foreignkey'
        )
        batch_op.drop_column('plan_report')
        batch_op.drop_column('project_id')

    op.drop_index(op.f('ix_automation_files_project_id'), table_name='automation_files')
    op.drop_table('automation_files')
    op.drop_index(op.f('ix_automation_projects_project_key'), table_name='automation_projects')
    op.drop_index(op.f('ix_automation_projects_owner_id'), table_name='automation_projects')
    op.drop_table('automation_projects')
