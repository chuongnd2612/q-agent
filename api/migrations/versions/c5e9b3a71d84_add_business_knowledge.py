"""add business_source + business_fact + project_config.business_brief

Wave-0 foundation of the Business Knowledge epic (#813, slice #815): the
project-scoped domain grounding that test-case authoring reads *alongside* the
code-derived ``project_knowledge``. Schema only — no service, no router, nothing
a user can see yet.

``business_source``
    One row per link or uploaded document. ``status``/``last_error`` are
    **per source**, which is the point: ``project_knowledge`` has one status for
    a whole row and so cannot express "3 of 40 wiki pages failed".
    Unique on ``(project_guid, owner_id, kind, url)`` — ADR 0009 §3 ownership
    (the owner is part of the key, so a row can exist once per user and once in
    the shared namespace, ``owner_id IS NULL``). ``url`` is nullable and NULL
    for an upload; both SQLite and PostgreSQL treat NULLs as distinct in a
    unique index, so links de-duplicate while a second upload is never rejected.

``business_fact``
    The retrievable unit and the override overlay in one table. ``source_id``
    and ``superseded_by`` are both ``ON DELETE SET NULL``, never CASCADE:
    deleting a source must not silently destroy a pinned human correction.
    ``superseded_by`` is self-referential, declared inside ``create_table`` so
    it is rendered into the CREATE statement (SQLite has no ADD CONSTRAINT).

``project_config.business_brief``
    The project-level digest ``{brief, hash, built_at, status, last_error}``.
    Added **nullable with a backfill to** ``'{}'`` rather than with a
    ``server_default``, matching ``f6b3d9c14e27``'s handling of
    ``runs.link_ticket_ids`` — a JSON server default is the one piece of this
    that does not render identically on SQLite and PostgreSQL.

Runs on PostgreSQL and SQLite: the ``project_config`` alteration goes through
``batch_alter_table`` (SQLite's table-rebuild path), and the two new tables are
plain ``create_table`` calls, which need no batch mode.

Reversible: ``downgrade`` drops the column and both tables, in FK order.

Revision ID: c5e9b3a71d84
Revises: a1f4c7b92e30
Create Date: 2026-09-12 09:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

import app.db

revision: str = "c5e9b3a71d84"
down_revision: Union[str, Sequence[str], None] = "a1f4c7b92e30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "business_source",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_guid", sa.String(length=36), nullable=True),
        sa.Column("project_key", sa.String(length=200), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("url", sa.String(length=500), nullable=True),
        sa.Column("connection_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("last_error", sa.String(length=1000), nullable=False),
        sa.Column("fetched_at", app.db.UTCDateTime(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("doc_count", sa.Integer(), nullable=False),
        sa.Column("excluded", sa.Boolean(), nullable=False),
        sa.Column("raw_path", sa.String(length=600), nullable=False),
        sa.Column("normalized_path", sa.String(length=600), nullable=False),
        sa.Column("created_at", app.db.UTCDateTime(), nullable=False),
        sa.Column("updated_at", app.db.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_business_source_owner_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["provider_connections.id"],
            name=op.f("fk_business_source_connection_id_provider_connections"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_guid",
            "owner_id",
            "kind",
            "url",
            name="uq_business_source_project_kind_url",
        ),
    )
    op.create_index(
        op.f("ix_business_source_project_guid"), "business_source", ["project_guid"], unique=False
    )
    op.create_index(
        op.f("ix_business_source_owner_id"), "business_source", ["owner_id"], unique=False
    )
    op.create_index(op.f("ix_business_source_status"), "business_source", ["status"], unique=False)

    op.create_table(
        "business_fact",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_guid", sa.String(length=36), nullable=True),
        sa.Column("owner_id", sa.Integer(), nullable=True),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("term", sa.String(length=300), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("pinned", sa.Boolean(), nullable=False),
        sa.Column("excluded", sa.Boolean(), nullable=False),
        sa.Column("superseded_by", sa.Integer(), nullable=True),
        sa.Column("rank_text", sa.Text(), nullable=False),
        sa.Column("created_at", app.db.UTCDateTime(), nullable=False),
        sa.Column("updated_at", app.db.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_business_fact_owner_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["business_source.id"],
            name=op.f("fk_business_fact_source_id_business_source"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by"],
            ["business_fact.id"],
            name=op.f("fk_business_fact_superseded_by_business_fact"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_business_fact_project_guid"), "business_fact", ["project_guid"], unique=False
    )
    op.create_index(op.f("ix_business_fact_owner_id"), "business_fact", ["owner_id"], unique=False)
    op.create_index(
        op.f("ix_business_fact_source_id"), "business_fact", ["source_id"], unique=False
    )
    op.create_index(op.f("ix_business_fact_category"), "business_fact", ["category"], unique=False)

    with op.batch_alter_table("project_config") as batch_op:
        batch_op.add_column(sa.Column("business_brief", sa.JSON(), nullable=True))
    # Backfill so no row is left NULL on a column the ORM types as non-optional.
    op.execute("UPDATE project_config SET business_brief = '{}' WHERE business_brief IS NULL")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("project_config") as batch_op:
        batch_op.drop_column("business_brief")

    op.drop_index(op.f("ix_business_fact_category"), table_name="business_fact")
    op.drop_index(op.f("ix_business_fact_source_id"), table_name="business_fact")
    op.drop_index(op.f("ix_business_fact_owner_id"), table_name="business_fact")
    op.drop_index(op.f("ix_business_fact_project_guid"), table_name="business_fact")
    op.drop_table("business_fact")

    op.drop_index(op.f("ix_business_source_status"), table_name="business_source")
    op.drop_index(op.f("ix_business_source_owner_id"), table_name="business_source")
    op.drop_index(op.f("ix_business_source_project_guid"), table_name="business_source")
    op.drop_table("business_source")
