"""Migration test for #727 — backfilling ``runs.project_guid``.

The point of slice 1 is that the project stops being derived on read, so the
rows that already exist have to be walked **once**, in the migration. This drives
Alembic for real: upgrade to the revision *before* the new one, insert a
pre-stamping database by hand, then upgrade to head and read the column.

Written this way deliberately rather than by calling the module's private
helpers — the failure mode being guarded against is SQL that works in the ORM's
dialect and not in Alembic's connection, which only a real upgrade catches.
"""

from __future__ import annotations

import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.config import API_DIR

# The revision this migration follows, and the one under test.
BEFORE = "d5c9a71e3f48"
UNDER_TEST = "a1e5d2c47b93"


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


def _seed_pre_stamping_db(conn, *, connections: int) -> dict[str, str]:
    """A database as it looks before stamping: runs, tickets, configs, projects.

    ``connections`` chooses which resolution leg gets exercised: 2 means the
    ticket's ``connection_id`` has to decide (the id link), 1 leaves the
    sole-project fallback as the only way through.
    """
    guids: dict[str, str] = {}
    for index in range(connections):
        name = ["Alpha", "Beta"][index]
        guid = str(uuid.uuid4())
        guids[name] = guid
        conn.execute(
            text(
                "INSERT INTO provider_connections (kind, name, config, secrets, connected,"
                " created_at, updated_at)"
                " VALUES ('ado', :n, '{}', '{}', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"n": f"{name}-conn"},
        )
        connection_id = conn.execute(
            text("SELECT id FROM provider_connections WHERE name = :n"),
            {"n": f"{name}-conn"},
        ).scalar()
        conn.execute(
            text(
                "INSERT INTO projects (guid, provider_kind, external_id, name, active, meta,"
                " created_at) VALUES (:g, 'ado', :e, :n, 1, '{}', CURRENT_TIMESTAMP)"
            ),
            {"g": guid, "e": f"ext-{name}", "n": name},
        )
        conn.execute(
            text(
                "INSERT INTO project_config (key, name, project_guid, work_item_connection_id,"
                " base_url, repos, local_repo_path, repo_url, environments, test_accounts,"
                " extra, manual_auth, created_at, updated_at)"
                " VALUES (:k, :k, :g, :c, '', '[]', '', '', '[]', '[]', '{}', 0,"
                " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"k": name, "g": guid, "c": connection_id},
        )
        conn.execute(
            text(
                "INSERT INTO tickets (external_id, provider_kind, connection_id, title,"
                " work_item_type, status, priority, assignee, sprint, area_path, epic,"
                " description, note, labels, acceptance_criteria, comments, attachments,"
                " linked_prs, synced_at)"
                " VALUES (:x, 'ado', :c, 'T', 'User Story', 'Ready for QA', 'Medium', '', '',"
                " '', '', '', '', '[]', '[]', '[]', '[]', '[]', CURRENT_TIMESTAMP)"
            ),
            {"x": f"{name.upper()[:3]}-1", "c": connection_id},
        )
        conn.execute(
            text(
                "INSERT INTO runs (code, name, scope, scope_label, framework, browser, env,"
                " workers, retry_policy, status, created_at, cancel_requested)"
                " VALUES (:c, 'r', 'selected', 'Selected tickets', 'Playwright', 'chromium',"
                " 'Staging', 4, 2, 'done', CURRENT_TIMESTAMP, 0)"
            ),
            {"c": f"RUN-{90 + index}"},
        )
        run_id = conn.execute(
            text("SELECT id FROM runs WHERE code = :c"), {"c": f"RUN-{90 + index}"}
        ).scalar()
        conn.execute(
            text(
                "INSERT INTO run_tickets (run_id, ticket_external_id, position, gen_status,"
                " repo, analysis, analysis_error)"
                " VALUES (:r, :x, 0, 'done', '', '{}', '')"
            ),
            {"r": run_id, "x": f"{name.upper()[:3]}-1"},
        )
    return guids


def _project_guids_by_code(engine) -> dict[str, str | None]:
    with engine.begin() as conn:
        return {
            row.code: row.project_guid
            for row in conn.execute(text("SELECT code, project_guid FROM runs")).fetchall()
        }


def test_migration_backfills_each_run_from_its_own_ticket_connection(tmp_path, monkeypatch):
    """Two projects: each run must get *its* project, not the first one found.

    This is the assertion that would fail if the backfill took a shortcut like
    "there is a project, use it" — the exact class of bug the derive-on-read
    resolution had.
    """
    engine = _temp_db(tmp_path, monkeypatch, "pre.db")
    cfg = _alembic_cfg()
    command.upgrade(cfg, BEFORE)
    with engine.begin() as conn:
        guids = _seed_pre_stamping_db(conn, connections=2)

    command.upgrade(cfg, UNDER_TEST)

    assert _project_guids_by_code(engine) == {
        "RUN-90": guids["Alpha"],
        "RUN-91": guids["Beta"],
    }


def test_migration_leaves_a_run_with_no_tickets_null(tmp_path, monkeypatch):
    """NULL is a real outcome, not a bug — the API exposes it as 'unassigned'."""
    engine = _temp_db(tmp_path, monkeypatch, "bare.db")
    cfg = _alembic_cfg()
    command.upgrade(cfg, BEFORE)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (code, name, scope, scope_label, framework, browser, env,"
                " workers, retry_policy, status, created_at, cancel_requested)"
                " VALUES ('RUN-95', 'r', 'selected', 'Selected tickets', 'Playwright',"
                " 'chromium', 'Staging', 4, 2, 'done', CURRENT_TIMESTAMP, 0)"
            )
        )

    command.upgrade(cfg, UNDER_TEST)

    assert _project_guids_by_code(engine) == {"RUN-95": None}


def test_migration_downgrade_drops_the_column(tmp_path, monkeypatch):
    engine = _temp_db(tmp_path, monkeypatch, "down.db")
    cfg = _alembic_cfg()
    command.upgrade(cfg, UNDER_TEST)

    def has_column() -> bool:
        with engine.begin() as conn:
            rows = conn.execute(text("PRAGMA table_info(runs)")).fetchall()
        return any(row[1] == "project_guid" for row in rows)

    assert has_column()
    command.downgrade(cfg, BEFORE)
    assert not has_column()
