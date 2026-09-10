"""Migration test for #766 — backfilling ``automation_projects.project_guid``.

Drives Alembic for real: upgrade to the revision *before* the backfill, insert
rows by hand, upgrade to it, then read the column. Written this way rather than
by calling the module's private helpers because the failure mode being guarded
against is SQL that works in the ORM's dialect and not on Alembic's connection
(the same reason ``test_runs_project_guid_migration.py`` exists).

The scenarios mirror what live data actually contains: one repo written to by a
single project (stamped), and one written to by two projects (left NULL, because
``automation_projects`` is keyed on the *provider* project key and so is
legitimately shared).
"""

from __future__ import annotations

import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.config import API_DIR

# The revision the backfill follows, and the one under test.
BEFORE = "f6b3d9c14e27"
UNDER_TEST = "b7c4e1a9d206"


def _alembic_cfg() -> Config:
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "migrations"))
    return cfg


def _temp_db(tmp_path, monkeypatch, filename: str):
    """Point Alembic at a throwaway SQLite file and return its engine.

    ``migrations/env.py`` resolves the URL from ``settings.resolved_database_url``
    and ignores ``sqlalchemy.url`` in alembic.ini entirely, so setting the config
    option is not enough — without this the upgrade runs against the developer's
    real database.
    """
    import app.config as config_module

    url = f"sqlite:///{(tmp_path / filename).as_posix()}"
    monkeypatch.setenv("QAGENT_DATABASE_URL", url)
    monkeypatch.setattr(config_module.settings, "database_url", url)
    return create_engine(url, connect_args={"check_same_thread": False})


def _insert_automation_project(conn, owner_id: int | None, key: str, repo: str) -> int:
    conn.execute(
        text(
            "INSERT INTO automation_projects (owner_id, project_key, repo, slug, root_path,"
            " base_version, project_guid, created_at, updated_at)"
            " VALUES (:o, :k, :r, :s, '', '', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        {"o": owner_id, "k": key, "r": repo, "s": f"{key}/{repo or 'default'}"},
    )
    return conn.execute(
        text("SELECT id FROM automation_projects WHERE repo = :r"), {"r": repo}
    ).scalar()


def _write_spec_from_a_run(conn, automation_project_id: int, code: str, guid: str | None) -> None:
    """A run belonging to ``guid`` that generated one spec into ``automation_project_id``."""
    conn.execute(
        text(
            "INSERT INTO runs (code, name, scope, scope_label, framework, browser, env,"
            " workers, retry_policy, status, project_guid, created_at, cancel_requested)"
            " VALUES (:c, 'r', 'selected', 'Selected tickets', 'Playwright', 'chromium',"
            " 'Staging', 4, 2, 'done', :g, CURRENT_TIMESTAMP, 0)"
        ),
        {"c": code, "g": guid},
    )
    run_id = conn.execute(text("SELECT id FROM runs WHERE code = :c"), {"c": code}).scalar()
    conn.execute(
        text(
            "INSERT INTO test_cases (run_id, ticket_external_id, code, title, objective,"
            " precondition, steps, test_data, linked_ac, priority, test_type, automation,"
            " platform, duration, approval, source, edited, created_at)"
            " VALUES (:r, 'T-1', 'TC-01', 't', '', '', '[]', '[]', '[]', 'Medium',"
            " 'Functional', 'Playwright', 'Web', '-', 'approved', 'ai', 0, CURRENT_TIMESTAMP)"
        ),
        {"r": run_id},
    )
    case_id = conn.execute(
        text("SELECT id FROM test_cases WHERE run_id = :r"), {"r": run_id}
    ).scalar()
    conn.execute(
        text(
            "INSERT INTO automation_specs (test_case_id, filename, language, framework, code,"
            " path, heal_report, status, block_reason, gate_report, project_id, created_at)"
            " VALUES (:t, 's.spec.ts', 'TypeScript', 'Playwright', '', '', '', 'draft', '',"
            " '', :p, CURRENT_TIMESTAMP)"
        ),
        {"t": case_id, "p": automation_project_id},
    )


def _guids_by_repo(engine) -> dict[str, str | None]:
    with engine.begin() as conn:
        return {
            row.repo: row.project_guid
            for row in conn.execute(
                text("SELECT repo, project_guid FROM automation_projects")
            ).fetchall()
        }


def test_backfill_stamps_a_row_written_by_exactly_one_project(tmp_path, monkeypatch):
    """Two runs of the same project still resolve to one guid — DISTINCT, not a row count."""
    engine = _temp_db(tmp_path, monkeypatch, "single.db")
    cfg = _alembic_cfg()
    command.upgrade(cfg, BEFORE)
    guid = str(uuid.uuid4())
    with engine.begin() as conn:
        project_id = _insert_automation_project(conn, 2, "surency", "surency-admin-hub")
        _write_spec_from_a_run(conn, project_id, "RUN-1", guid)
        _write_spec_from_a_run(conn, project_id, "RUN-2", guid)

    command.upgrade(cfg, UNDER_TEST)

    assert _guids_by_repo(engine) == {"surency-admin-hub": guid}


def test_backfill_leaves_a_repo_shared_by_two_projects_null(tmp_path, monkeypatch):
    """The live case: one provider repo, runs from two q-agent projects.

    A scalar column cannot represent that, and choosing a winner would hide the
    repo from the loser — so it stays unclaimed and ``projects_for_guid`` finds
    it through the join instead.
    """
    engine = _temp_db(tmp_path, monkeypatch, "shared.db")
    cfg = _alembic_cfg()
    command.upgrade(cfg, BEFORE)
    with engine.begin() as conn:
        project_id = _insert_automation_project(conn, 3, "surency", "surency-admin-hub")
        _write_spec_from_a_run(conn, project_id, "RUN-1", str(uuid.uuid4()))
        _write_spec_from_a_run(conn, project_id, "RUN-2", str(uuid.uuid4()))

    command.upgrade(cfg, UNDER_TEST)

    assert _guids_by_repo(engine) == {"surency-admin-hub": None}


def test_backfill_leaves_an_untouched_scaffold_null(tmp_path, monkeypatch):
    """No specs, and a spec whose run has no project, both resolve to nothing."""
    engine = _temp_db(tmp_path, monkeypatch, "bare.db")
    cfg = _alembic_cfg()
    command.upgrade(cfg, BEFORE)
    with engine.begin() as conn:
        _insert_automation_project(conn, 2, "surency", "empty")
        unassigned = _insert_automation_project(conn, 2, "surency", "unassigned")
        _write_spec_from_a_run(conn, unassigned, "RUN-1", None)

    command.upgrade(cfg, UNDER_TEST)

    assert _guids_by_repo(engine) == {"empty": None, "unassigned": None}


def test_backfill_resolves_a_row_whose_key_is_not_its_projects_name(tmp_path, monkeypatch):
    """``project_key`` is the PROVIDER key — a name match would be wrong, not merely weak.

    A project named ``demo`` whose automation row is keyed ``surency``: the join
    resolves it to ``demo``, which no name-matching backfill could have done.
    """
    engine = _temp_db(tmp_path, monkeypatch, "provider.db")
    cfg = _alembic_cfg()
    command.upgrade(cfg, BEFORE)
    demo_guid = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO projects (guid, provider_kind, external_id, name, active, meta,"
                " created_at, owner_id)"
                " VALUES (:g, 'ado', 'ext-surency', 'surency', 1, '{}', CURRENT_TIMESTAMP, 2)"
            ),
            {"g": str(uuid.uuid4())},
        )
        conn.execute(
            text(
                "INSERT INTO projects (guid, provider_kind, external_id, name, active, meta,"
                " created_at, owner_id)"
                " VALUES (:g, 'ado', 'ext-demo', 'demo', 1, '{}', CURRENT_TIMESTAMP, 3)"
            ),
            {"g": demo_guid},
        )
        project_id = _insert_automation_project(conn, 3, "surency", "surency-admin-hub")
        _write_spec_from_a_run(conn, project_id, "RUN-1", demo_guid)

    command.upgrade(cfg, UNDER_TEST)

    assert _guids_by_repo(engine) == {"surency-admin-hub": demo_guid}


def test_downgrade_clears_the_column(tmp_path, monkeypatch):
    engine = _temp_db(tmp_path, monkeypatch, "down.db")
    cfg = _alembic_cfg()
    command.upgrade(cfg, BEFORE)
    guid = str(uuid.uuid4())
    with engine.begin() as conn:
        project_id = _insert_automation_project(conn, 2, "surency", "web")
        _write_spec_from_a_run(conn, project_id, "RUN-1", guid)

    command.upgrade(cfg, UNDER_TEST)
    assert _guids_by_repo(engine) == {"web": guid}

    command.downgrade(cfg, BEFORE)
    assert _guids_by_repo(engine) == {"web": None}
