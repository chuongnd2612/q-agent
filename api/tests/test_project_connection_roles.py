"""Slice 6 of #726 (ADR 0015 §3 and §5) — the three connection roles, the run's
link options, and provider deriving from the project.

Four things are pinned here, each for a reason:

1. **TEST CASE TARGET resolution.** The role is new, but the *behaviour* it
   formalises is not — every consumer already used the ticket source. So the
   interesting assertions are the fallback (an unset target must still resolve,
   to the ticket source) and the override (an explicit target must actually win).
2. **The migration's default.** The migration is what makes an existing project
   keep behaving identically, so it is driven through real Alembic rather than by
   calling helpers: the failure mode is SQL that works in the ORM's dialect and
   not in Alembic's connection, and only a real upgrade catches that.
3. **Link options as tightening constraints.** ``create-link`` has three inputs
   that can each only narrow the previous one. A status code proves nothing here
   — every branch returns 200 — so each assertion pins what
   ``start_create_link`` was actually *called with*.
4. **provider_kind written from the project.** The column stays (ADR 0015 §3
   rejects deleting it) but its source of writes changes, so the test is that a
   project's TICKET SOURCE decides it, including re-stamping an existing row.
"""

from __future__ import annotations

import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.config import API_DIR
from app.models.project import Project
from app.models.project_config import ProjectConfig
from app.models.provider_connection import ProviderConnection
from app.models.run import Run
from app.models.testcase import TestCase
from app.models.ticket import Ticket
from app.services import connection_service
from app.services.adapters.base import ProviderAdapter

# The revision this migration follows, and the one under test.
BEFORE = "a1e5d2c47b93"
UNDER_TEST = "f6b3d9c14e27"


# ------------------------------------------------------- TEST CASE TARGET
def test_test_case_target_falls_back_to_the_ticket_source(db_session):
    """An unset target is a working default, not a gap."""
    source = ProviderConnection(kind="ado", name="Board")
    other = ProviderConnection(kind="jira", name="Somewhere else")
    db_session.add_all([source, other])
    db_session.flush()
    db_session.add(
        ProjectConfig(key="P", name="P", work_item_connection_id=source.id)
    )
    db_session.commit()

    assert connection_service.resolve_test_case_for_project(db_session, "P").id == source.id


def test_explicit_test_case_target_wins_over_the_ticket_source(db_session):
    source = ProviderConnection(kind="ado", name="Board")
    plans = ProviderConnection(kind="ado", name="Test Plans")
    db_session.add_all([source, plans])
    db_session.flush()
    db_session.add(
        ProjectConfig(
            key="P",
            name="P",
            work_item_connection_id=source.id,
            test_case_connection_id=plans.id,
        )
    )
    db_session.commit()

    assert connection_service.resolve_test_case_for_project(db_session, "P").id == plans.id


def test_project_config_saves_the_test_case_binding(client, db_session):
    source = ProviderConnection(kind="ado", name="Board")
    plans = ProviderConnection(kind="ado", name="Test Plans")
    db_session.add_all([source, plans])
    db_session.commit()

    resp = client.put(
        "/projects/Surency Platform/config",
        json={"workItemConnectionId": source.id, "testCaseConnectionId": plans.id},
    )
    assert resp.status_code == 200
    # Only the fields this test is about — never `== ` against the whole body,
    # which rots on correct additive changes (#579).
    assert resp.json()["testCaseConnectionId"] == plans.id

    row = db_session.query(ProjectConfig).filter(ProjectConfig.key == "Surency Platform").first()
    assert row.test_case_connection_id == plans.id
    # ...and the observable effect: resolution now routes there.
    assert (
        connection_service.resolve_test_case_for_project(db_session, "Surency Platform").id
        == plans.id
    )


def test_deleting_a_connection_clears_the_test_case_binding(client, db_session):
    """A dangling binding would resolve to nothing and fail at the Link stage."""
    source = ProviderConnection(kind="ado", name="Board")
    plans = ProviderConnection(kind="ado", name="Test Plans")
    db_session.add_all([source, plans])
    db_session.flush()
    db_session.add(
        ProjectConfig(
            key="P", name="P", work_item_connection_id=source.id, test_case_connection_id=plans.id
        )
    )
    db_session.commit()
    plans_id = plans.id

    assert client.delete(f"/connections/{plans_id}").status_code == 204

    db_session.expire_all()
    row = db_session.query(ProjectConfig).filter(ProjectConfig.key == "P").first()
    assert row.test_case_connection_id is None
    # Negative control: the *other* binding must survive, or the delete is
    # over-clearing and this test would pass for the wrong reason.
    assert row.work_item_connection_id == source.id


# ------------------------------------------------------------- migration
def _alembic_cfg() -> Config:
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "migrations"))
    return cfg


def _temp_db(tmp_path, monkeypatch, filename: str):
    """Point Alembic at a throwaway SQLite file and return its engine.

    ``migrations/env.py`` resolves the URL from ``settings.resolved_database_url``
    and ignores ``sqlalchemy.url`` in alembic.ini entirely — without this the
    upgrade runs against the developer's real database.
    """
    import app.config as config_module

    url = f"sqlite:///{(tmp_path / filename).as_posix()}"
    monkeypatch.setenv("QAGENT_DATABASE_URL", url)
    monkeypatch.setattr(config_module.settings, "database_url", url)
    return create_engine(url, connect_args={"check_same_thread": False})


def _seed_config(conn, key: str, work_item_connection_id: int | None) -> None:
    conn.execute(
        text(
            "INSERT INTO project_config (key, name, project_guid, work_item_connection_id,"
            " base_url, repos, local_repo_path, repo_url, environments, test_accounts,"
            " extra, manual_auth, created_at, updated_at)"
            " VALUES (:k, :k, :g, :c, '', '[]', '', '', '[]', '[]', '{}', 0,"
            " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        {"k": key, "g": str(uuid.uuid4()), "c": work_item_connection_id},
    )


def test_migration_defaults_the_test_case_target_to_the_ticket_source(tmp_path, monkeypatch):
    """The whole point of the backfill: an existing project keeps behaving the same.

    A project with no ticket source has nothing to default *to* and must stay
    NULL — the negative control that stops this passing on a blanket UPDATE.
    """
    engine = _temp_db(tmp_path, monkeypatch, "roles.db")
    cfg = _alembic_cfg()
    command.upgrade(cfg, BEFORE)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO provider_connections (kind, name, config, secrets, connected,"
                " created_at, updated_at)"
                " VALUES ('ado', 'Board', '{}', '{}', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection_id = conn.execute(
            text("SELECT id FROM provider_connections WHERE name = 'Board'")
        ).scalar()
        _seed_config(conn, "Bound", connection_id)
        _seed_config(conn, "Unbound", None)

    command.upgrade(cfg, UNDER_TEST)

    with engine.begin() as conn:
        targets = {
            row.key: row.test_case_connection_id
            for row in conn.execute(
                text("SELECT key, test_case_connection_id FROM project_config")
            ).fetchall()
        }
    assert targets == {"Bound": connection_id, "Unbound": None}


def test_migration_backfills_run_link_options_to_todays_behaviour(tmp_path, monkeypatch):
    """A run created before #732 must behave exactly as it did: link on, dry off."""
    engine = _temp_db(tmp_path, monkeypatch, "runlinks.db")
    cfg = _alembic_cfg()
    command.upgrade(cfg, BEFORE)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (code, name, scope, scope_label, framework, browser, env,"
                " workers, retry_policy, status, created_at, cancel_requested)"
                " VALUES ('RUN-77', 'r', 'selected', 'Selected tickets', 'Playwright',"
                " 'chromium', 'Staging', 4, 2, 'done', CURRENT_TIMESTAMP, 0)"
            )
        )

    command.upgrade(cfg, UNDER_TEST)

    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT link_enabled, link_dry_run, link_ticket_ids FROM runs WHERE code='RUN-77'")
        ).fetchone()
    assert bool(row.link_enabled) is True
    assert bool(row.link_dry_run) is False
    assert row.link_ticket_ids == "[]"


def test_migration_downgrade_drops_the_new_columns(tmp_path, monkeypatch):
    engine = _temp_db(tmp_path, monkeypatch, "rolesdown.db")
    cfg = _alembic_cfg()
    command.upgrade(cfg, UNDER_TEST)

    def columns(table: str) -> set[str]:
        with engine.begin() as conn:
            return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()}

    assert "test_case_connection_id" in columns("project_config")
    assert {"link_enabled", "link_dry_run", "link_ticket_ids"} <= columns("runs")

    command.downgrade(cfg, BEFORE)

    assert "test_case_connection_id" not in columns("project_config")
    assert not ({"link_enabled", "link_dry_run", "link_ticket_ids"} & columns("runs"))


# --------------------------------------------------------- run link options
def _approved_run(db_session, **link_options) -> Run:
    """A run with one approved case — the minimum ``create-link`` accepts."""
    run = Run(code=f"RUN-{uuid.uuid4().hex[:6]}", name="r", status="review", **link_options)
    db_session.add(run)
    db_session.flush()
    db_session.add(
        TestCase(
            run_id=run.id,
            ticket_external_id="SUR-1",
            code="TC-1",
            title="t",
            approval="approved",
        )
    )
    db_session.commit()
    db_session.refresh(run)
    return run


def _capture_start(monkeypatch) -> dict:
    """Record what ``create-link`` hands the worker.

    All three branches return 200, so the status code proves nothing — the
    argument tuple is the only thing that says which decision actually took
    effect (CLAUDE.md: assert which branch ran).
    """
    from app.services import link_service

    seen: dict = {}
    original = link_service.start_create_link

    def fake(run_id, link, ticket_ids, dry_run=False):
        seen.update(run_id=run_id, link=link, ticket_ids=list(ticket_ids), dry_run=dry_run)

    monkeypatch.setattr(link_service, "start_create_link", fake)
    # Restore by re-setting the attribute, never `monkeypatch.undo()` — undo also
    # un-redirects the workspace fixture's SessionLocal (#641).
    seen["_original"] = original
    return seen


def test_create_link_stores_and_honours_the_runs_link_options(client, db_session, monkeypatch):
    run = _approved_run(db_session, link_enabled=False, link_dry_run=True)
    seen = _capture_start(monkeypatch)

    resp = client.post(f"/runs/{run.id}/testcases/create-link", json={"link": True})
    assert resp.status_code == 200

    # The run's options can only TIGHTEN: a request asking to link is refused the
    # link, and a run created as a dry run stays one.
    assert seen["link"] is False
    assert seen["dry_run"] is True


def test_create_link_uses_the_runs_ticket_subset_as_the_fallback(client, db_session, monkeypatch):
    run = _approved_run(db_session, link_ticket_ids=["SUR-1"])
    seen = _capture_start(monkeypatch)

    assert client.post(f"/runs/{run.id}/testcases/create-link", json={}).status_code == 200
    assert seen["ticket_ids"] == ["SUR-1"]

    # A caller that names tickets is being specific about *this* pass and wins —
    # the two subsets are not intersected, which could yield an empty one neither
    # of them asked for.
    seen.clear()
    monkeypatch.setattr(
        __import__("app.services.link_service", fromlist=["x"]),
        "start_create_link",
        lambda run_id, link, ticket_ids, dry_run=False: seen.update(ticket_ids=list(ticket_ids)),
    )
    client.post(f"/runs/{run.id}/testcases/create-link", json={"ticketIds": ["SUR-9"]})
    assert seen["ticket_ids"] == ["SUR-9"]


def test_create_run_stores_the_link_options(client, db_session):
    db_session.add(Ticket(external_id="SUR-1", provider_kind="ado", title="t"))
    db_session.add(Ticket(external_id="SUR-2", provider_kind="ado", title="t2"))
    db_session.commit()

    resp = client.post(
        "/runs",
        json={
            "scope": "selected",
            "ticketIds": ["SUR-1", "SUR-2"],
            "link": False,
            "dryRun": True,
            # "SUR-9" is not in the run: a subset naming a ticket the run does not
            # contain is a typo, not a smaller selection, and carrying it would make
            # the Link stage skip everything.
            "linkTicketIds": ["SUR-1", "SUR-9"],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["linkEnabled"] is False
    assert body["linkDryRun"] is True
    assert body["linkTicketIds"] == ["SUR-1"]

    row = db_session.get(Run, body["id"])
    assert row.link_ticket_ids == ["SUR-1"]


# ----------------------------------------- provider_kind written from the project
class _FakeAdapter(ProviderAdapter):
    """Returns one work item, whatever it is asked for."""

    kind = "jira"

    def __init__(self, external_id: str = "SUR-1") -> None:
        self._external_id = external_id

    def test_connection(self):  # pragma: no cover - not exercised here
        return True, "ok", {}

    def fetch_tickets(self, **kwargs):
        return [{"external_id": self._external_id, "title": "From the project's source"}]

    def list_projects(self):  # pragma: no cover - not exercised here
        return []

    def publish_comment(self, *args, **kwargs):  # pragma: no cover - not exercised here
        return {}


def test_sync_stamps_provider_kind_from_the_projects_ticket_source(
    client, db_session, monkeypatch
):
    """`provider_kind` is a denormalised cache of the connection's kind now.

    The ticket already belongs to the project and is stamped ``ado`` from when the
    project's source *was* ADO. Repointing the source to Jira and re-syncing must
    UPDATE that row, not duplicate it: matching on ``(external_id, provider_kind)``
    is exactly what would insert a second copy once the kind is a cache.
    """
    jira = ProviderConnection(kind="jira", name="Jira board")
    db_session.add(jira)
    db_session.flush()
    guid = str(uuid.uuid4())
    project = Project(
        guid=guid, provider_kind="jira", external_id="P", name="Claims Portal", active=True
    )
    db_session.add(project)
    db_session.flush()
    db_session.add(
        ProjectConfig(
            key="Claims Portal",
            name="Claims Portal",
            project_guid=guid,
            work_item_connection_id=jira.id,
        )
    )
    db_session.add(
        Ticket(external_id="SUR-1", provider_kind="ado", title="stale", project_id=project.id)
    )
    db_session.commit()

    monkeypatch.setattr(
        connection_service, "adapter_for", lambda db, connection: _FakeAdapter("SUR-1")
    )

    # No connectionId and no providerKind — the *project* is the only thing that
    # can decide, which is the behaviour under test.
    resp = client.post("/tickets/sync", json={"projectGuid": guid})
    assert resp.status_code == 200, resp.text
    assert resp.json()["synced"] == 1

    db_session.expire_all()
    rows = db_session.query(Ticket).filter(Ticket.external_id == "SUR-1").all()
    # One row, not two — the negative control that catches a duplicating match.
    assert len(rows) == 1
    row = rows[0]
    assert row.provider_kind == "jira"  # re-stamped from the project's source
    assert row.connection_id == jira.id
    assert row.project_id == project.id
