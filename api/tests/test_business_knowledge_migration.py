"""Migration test for #815 — the Business Knowledge schema.

Drives Alembic for real against an isolated temp SQLite file (the pattern in
``test_runs_project_guid_migration.py``): upgrade to the revision *before* the
new one, then to it, and inspect the result. Written this way rather than by
calling ``Base.metadata.create_all`` because the failure mode being guarded
against is DDL that the ORM renders happily and Alembic's connection does not.

What is asserted, and why each assertion can fail:

- the two tables and the ``project_config`` column exist after the upgrade;
- **the ORM and the migration agree on every column name** — the thing that
  actually breaks downstream slices, and which nothing else in the suite checks;
- the composite unique constraint rejects a duplicate *link* but not a second
  *upload* (``url IS NULL``) — the one non-obvious consequence of putting a
  nullable column in a unique key, and the reason uploads work at all;
- ``owner_id`` is part of that key, so two users may hold the same link
  (ADR 0009 §3);
- deleting a source sets its facts' ``source_id`` to NULL instead of deleting
  them, so a pinned human correction survives — with a negative control (the
  fact row itself is still there) so the test cannot pass by the row vanishing;
- the downgrade removes all three, i.e. the revision is reversible.
"""

from __future__ import annotations

import json
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.config import API_DIR

# The revision this migration follows, and the one under test.
BEFORE = "a1f4c7b92e30"
UNDER_TEST = "c5e9b3a71d84"

_SOURCE_INSERT = (
    "INSERT INTO business_source (project_guid, project_key, owner_id, kind, title, url,"
    " status, last_error, content_hash, byte_size, doc_count, excluded, raw_path,"
    " normalized_path, created_at, updated_at)"
    " VALUES (:g, 'Alpha', :o, :k, 't', :u, 'pending', '', '', 0, 0, 0, '', '',"
    " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
)

_FACT_INSERT = (
    "INSERT INTO business_fact (project_guid, owner_id, source_id, category, term, statement,"
    " detail, origin, pinned, excluded, rank_text, created_at, updated_at)"
    " VALUES (:g, :o, :s, 'rule', 'Premium', 'A premium member skips the queue.', '',"
    " :origin, :pinned, 0, 'premium member queue', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
)


def _alembic_cfg() -> Config:
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "migrations"))
    return cfg


def _temp_db(tmp_path, monkeypatch, filename: str):
    """Point Alembic at a throwaway SQLite file and return its engine.

    ``migrations/env.py`` resolves the URL from ``settings.resolved_database_url``
    and ignores ``sqlalchemy.url`` in alembic.ini entirely — so setting the
    config option is not enough, and without this the upgrade runs against the
    developer's real database (a Postgres URL in ``api/.env``).
    """
    import app.config as config_module

    url = f"sqlite:///{(tmp_path / filename).as_posix()}"
    monkeypatch.setenv("QAGENT_DATABASE_URL", url)
    monkeypatch.setattr(config_module.settings, "database_url", url)
    return create_engine(url, connect_args={"check_same_thread": False})


def _upgraded(tmp_path, monkeypatch, filename: str):
    """A temp database migrated all the way to the revision under test."""
    engine = _temp_db(tmp_path, monkeypatch, filename)
    command.upgrade(_alembic_cfg(), UNDER_TEST)
    return engine


def _columns(engine, table: str) -> set[str]:
    return {col["name"] for col in inspect(engine).get_columns(table)}


def test_upgrade_creates_both_tables_and_the_project_config_column(tmp_path, monkeypatch):
    engine = _temp_db(tmp_path, monkeypatch, "up.db")
    cfg = _alembic_cfg()
    command.upgrade(cfg, BEFORE)

    insp = inspect(engine)
    assert not insp.has_table("business_source")
    assert not insp.has_table("business_fact")
    assert "business_brief" not in _columns(engine, "project_config")

    command.upgrade(cfg, UNDER_TEST)

    insp = inspect(engine)
    assert insp.has_table("business_source")
    assert insp.has_table("business_fact")
    assert "business_brief" in _columns(engine, "project_config")


def test_existing_project_config_rows_are_backfilled_not_left_null(tmp_path, monkeypatch):
    """The ORM types ``business_brief`` as a non-optional dict, so no row may be NULL."""
    engine = _temp_db(tmp_path, monkeypatch, "backfill.db")
    cfg = _alembic_cfg()
    command.upgrade(cfg, BEFORE)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO project_config (key, name, base_url, repos, local_repo_path,"
                " repo_url, environments, test_accounts, extra, manual_auth, created_at,"
                " updated_at) VALUES ('Alpha', 'Alpha', '', '[]', '', '', '[]', '[]', '{}', 0,"
                " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

    command.upgrade(cfg, UNDER_TEST)

    with engine.begin() as conn:
        briefs = [
            row.business_brief
            for row in conn.execute(text("SELECT business_brief FROM project_config")).fetchall()
        ]
    assert briefs and all(brief is not None for brief in briefs)
    assert [json.loads(brief) for brief in briefs] == [{}]


@pytest.mark.parametrize(
    ("model", "table"),
    [("BusinessSource", "business_source"), ("BusinessFact", "business_fact")],
)
def test_orm_and_migration_agree_on_columns(tmp_path, monkeypatch, model, table):
    """Schema drift between the model and the migration is silent until runtime.

    Downstream slices build against the ORM; the running database is built by
    Alembic. Comparing the two column sets here is what keeps a column added in
    one place from being missing in the other.
    """
    import app.models.business as business_models

    engine = _upgraded(tmp_path, monkeypatch, f"orm-{table}.db")
    orm_columns = {col.name for col in getattr(business_models, model).__table__.columns}

    assert orm_columns == _columns(engine, table)


def test_duplicate_link_is_rejected_but_a_second_upload_is_not(tmp_path, monkeypatch):
    """The nullable ``url`` in the unique key is what lets uploads coexist.

    NULLs compare as distinct in a unique index, so ``(project, owner, "upload",
    NULL)`` never collides — while two identical wiki links for the same owner do.
    """
    engine = _upgraded(tmp_path, monkeypatch, "unique.db")
    guid = str(uuid.uuid4())

    with engine.begin() as conn:
        conn.execute(
            text(_SOURCE_INSERT),
            {"g": guid, "o": 1, "k": "ado_wiki", "u": "https://wiki/Home"},
        )
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(_SOURCE_INSERT),
                {"g": guid, "o": 1, "k": "ado_wiki", "u": "https://wiki/Home"},
            )

    with engine.begin() as conn:
        for _ in range(2):
            conn.execute(text(_SOURCE_INSERT), {"g": guid, "o": 1, "k": "upload", "u": None})
        uploads = conn.execute(
            text("SELECT COUNT(*) FROM business_source WHERE kind = 'upload'")
        ).scalar()
    assert uploads == 2


def test_the_constraint_is_inert_in_the_shared_namespace(tmp_path, monkeypatch):
    """Documented, not accidental: ``owner_id IS NULL`` disables the unique key.

    A NULL anywhere in the tuple makes the row distinct on both SQLite and
    PostgreSQL, so the shared namespace (ADR 0009 §3) gets no database-level
    de-duplication — exactly as ``uq_project_knowledge_key_owner`` already
    behaves. A shared-tier writer must de-duplicate in the service layer; this
    test exists so that is a known property rather than a surprise.
    """
    engine = _upgraded(tmp_path, monkeypatch, "shared.db")
    guid = str(uuid.uuid4())

    with engine.begin() as conn:
        for _ in range(2):
            conn.execute(
                text(_SOURCE_INSERT),
                {"g": guid, "o": None, "k": "ado_wiki", "u": "https://wiki/Home"},
            )
        count = conn.execute(text("SELECT COUNT(*) FROM business_source")).scalar()
    assert count == 2


def test_the_same_link_may_exist_once_per_owner(tmp_path, monkeypatch):
    """ADR 0009 §3: the owner is part of the key, so two users do not collide."""
    engine = _upgraded(tmp_path, monkeypatch, "owners.db")
    guid = str(uuid.uuid4())

    with engine.begin() as conn:
        for owner in (None, 1, 2):
            conn.execute(
                text(_SOURCE_INSERT),
                {"g": guid, "o": owner, "k": "url", "u": "https://docs/spec"},
            )
        owners = {
            row.owner_id
            for row in conn.execute(text("SELECT owner_id FROM business_source")).fetchall()
        }
    assert owners == {None, 1, 2}


def test_deleting_a_source_orphans_its_facts_instead_of_destroying_them(tmp_path, monkeypatch):
    """``ON DELETE SET NULL``, not CASCADE — a pinned correction must survive.

    SQLite enforces FK actions only with ``PRAGMA foreign_keys=ON``, which is
    off by default on a raw connection, so it is enabled explicitly here; the
    application engine has it on via the pragma listener in ``app.db``.
    """
    engine = _upgraded(tmp_path, monkeypatch, "cascade.db")
    guid = str(uuid.uuid4())

    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.execute(
            text(_SOURCE_INSERT), {"g": guid, "o": None, "k": "url", "u": "https://docs/rules"}
        )
        source_id = conn.execute(text("SELECT id FROM business_source")).scalar()
        conn.execute(
            text(_FACT_INSERT),
            {"g": guid, "o": None, "s": source_id, "origin": "ingested", "pinned": 0},
        )
        conn.execute(
            text(_FACT_INSERT),
            {"g": guid, "o": None, "s": source_id, "origin": "manual", "pinned": 1},
        )
        conn.execute(text("DELETE FROM business_source WHERE id = :i"), {"i": source_id})

        rows = conn.execute(
            text("SELECT origin, pinned, source_id FROM business_fact ORDER BY id")
        ).fetchall()

    # Negative control: the rows are still present (the test cannot pass by the
    # facts having been cascaded away), and only the link is gone.
    assert [(row.origin, row.pinned) for row in rows] == [("ingested", 0), ("manual", 1)]
    assert [row.source_id for row in rows] == [None, None]


def test_downgrade_removes_both_tables_and_the_column(tmp_path, monkeypatch):
    engine = _upgraded(tmp_path, monkeypatch, "down.db")
    cfg = _alembic_cfg()

    command.downgrade(cfg, BEFORE)

    insp = inspect(engine)
    assert not insp.has_table("business_source")
    assert not insp.has_table("business_fact")
    assert "business_brief" not in _columns(engine, "project_config")
