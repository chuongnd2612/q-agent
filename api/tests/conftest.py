"""Unified pytest fixtures for the whole backend test suite.

Every test runs against its own on-disk temp SQLite database. We rebind the
global ``app.db.engine`` / ``SessionLocal`` (and ``app.config.settings``) to the
temp DB/workspace *before* the app is built, so that both request-handling
sessions (``get_db``) and background-thread sessions (``SessionLocal()`` opened by
the AI / automation / execution pipelines) hit the same isolated database.

This conftest merges the fixture surfaces authored by the four backend feature
workstreams: ``client``, ``db_session``, ``seed_ticket``, ``app``, ``app_env``,
``workspace_dir``.
"""

from __future__ import annotations

import time

import contextlib
from collections.abc import Iterator

import pytest

# --- Execution-target default for the suite (#573) --------------------------
#
# ``settings_store.DEFAULTS`` ships ``executionTarget="local-agent"`` — a
# deliberate product decision (#161/d35fe09: a fresh install runs on the user's
# own machine). That default silently made 13 execution/heal tests **vacuous**:
# a test's temp workspace has no ``settings.json``, so ``load_settings()``
# returned ``local-agent``, the endpoints took the agent-dispatch branch, and
# 409'd with "No local agent paired" — never entering the in-process code the
# tests claim to cover.
#
# #469/#573 filed this as an *order-dependent leak* from a test that writes the
# setting without restoring it. It is not: no test writes it (the few that need
# ``local-agent`` monkeypatch ``load_settings``), and every one of the 13
# failures reproduces when its file is run alone. The cause is the default
# itself, and it is fully deterministic.
#
# A leak is in fact structurally impossible here: ``_settings_path()`` derives
# from ``settings.workspace_dir``, which ``workspace_dir`` below repoints at a
# fresh ``tmp_path`` for every test, so the store cannot outlive one test.
#
# The suite pins its *test* default to the in-process runner. Tests that mean to
# exercise agent dispatch opt in via ``local_agent_target`` / ``settings_override``,
# both of which guarantee restore.
TEST_SETTINGS_DEFAULTS: dict = {"executionTarget": "server"}


class FakePopen:
    """Minimal ``subprocess.Popen`` stand-in for ``claude_cli.run_prompt`` tests.

    ``run_prompt`` spawns the CLI via ``subprocess.Popen(...)`` and reads it with
    ``communicate(timeout=...)`` so a run cancel can kill the live process. Tests
    patch ``claude_cli.subprocess.Popen`` with a factory returning this; it
    exposes only the surface ``run_prompt`` touches: ``communicate()`` returning
    ``(stdout, stderr)``, ``returncode``, and no-op ``kill``/``terminate``.
    """

    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    def communicate(self, timeout=None):  # noqa: ANN001 - matches Popen signature
        return self._stdout, self._stderr

    def kill(self):
        pass

    def terminate(self):
        pass



@pytest.fixture(autouse=True)
def _join_automation_workers():
    """Never let a fire-and-forget worker outlive the test that started it.

    `POST /runs/{id}/automation/generate`, `/cases/{id}/spec/regenerate` and
    `/cases/{id}/spec/chat` all return immediately and finish on a daemon thread
    (deliberately — they make several Claude calls and would exceed the fronting
    proxy timeout inline). Nothing waited for them, so a worker from test N was
    still mutating `automation_specs` during test N+1.

    Before #604 that collided on the `test_case_id` UNIQUE index and surfaced as a
    loud `IntegrityError` — the flake reported in #604. #604's `_get_or_create_spec`
    correctly made the loser *adopt* the winner's row instead of crashing, which is
    right for production but turned the cross-test leak SILENT: the late worker now
    overwrites whatever the next test seeded. The symptom moved to
    `test_generate_is_incremental_and_preserves_edits` asserting on an empty `code`,
    intermittently.

    The router already publishes exactly the signal needed: each endpoint adds its
    id to a module-level in-flight set and discards it in a `finally`. So wait on
    those rather than inventing new bookkeeping or making production code
    test-aware.
    """
    yield
    from app.routers import automation

    deadline = time.monotonic() + 30.0
    markers = (
        automation._generating,
        automation._regenerating_cases,
        automation._chatting_cases,
    )
    while any(markers) and time.monotonic() < deadline:
        time.sleep(0.02)
    stragglers = {
        name: sorted(marker)
        for name, marker in (
            ("generating", automation._generating),
            ("regenerating", automation._regenerating_cases),
            ("chatting", automation._chatting_cases),
        )
        if marker
    }
    # Clear so one stuck worker cannot cascade into every later test, but fail
    # loudly: a worker that outlives a 30s wait is a bug worth seeing, not
    # something to swallow.
    for marker in markers:
        marker.clear()
    assert not stragglers, f"background automation worker(s) outlived the test: {stragglers}"

@pytest.fixture
def workspace_dir(tmp_path, monkeypatch) -> Iterator:
    """Point the app's workspace (DB + specs + evidence) at a temp directory and
    rebind the engine/session/settings singletons accordingly."""
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("QAGENT_WORKSPACE_DIR", str(ws))
    monkeypatch.setenv("QAGENT_DATABASE_URL", f"sqlite:///{(ws / 'test.db').as_posix()}")

    import app.config as config_module

    config_module.get_settings.cache_clear()
    fresh = config_module.get_settings()
    # Mutate the singleton in place so modules that did `from app.config import
    # settings` at import time see the temp workspace too.
    config_module.settings.__dict__.update(fresh.__dict__)
    # The app enforces auth by default (#79); the suite exercises handlers
    # without auth plumbing, so disable enforcement here. test_auth opts back in
    # per-test via its own `auth_on` fixture.
    config_module.settings.auth_required = False
    config_module.settings.ensure_dirs()
    settings = config_module.settings

    import app.db as db_module

    monkeypatch.setattr(db_module, "settings", settings)
    new_engine = db_module.create_engine(
        settings.resolved_database_url, connect_args={"check_same_thread": False}, echo=False
    )
    monkeypatch.setattr(db_module, "engine", new_engine)
    new_session_local = db_module.sessionmaker(
        bind=new_engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    monkeypatch.setattr(db_module, "SessionLocal", new_session_local)

    db_module.init_db()

    # Pin the persisted settings store to the suite's test defaults (#573) so
    # endpoints resolve the in-process execution path rather than silently
    # dispatching to a (nonexistent) paired local agent. Written after
    # ensure_dirs so the workspace exists.
    from app.services import settings_store

    settings_store.save_settings(dict(TEST_SETTINGS_DEFAULTS))

    yield ws


@contextlib.contextmanager
def settings_override(**values):
    """Temporarily change persisted workspace settings, restoring on exit (#573).

    The only sanctioned way for a test to mutate the settings store. A bare
    ``settings_store.save_settings(...)`` is forbidden in tests: it leaves the
    store changed for the rest of the test, so whatever runs next silently takes
    a different code path (exactly the failure mode that made 13 tests vacuous).

    Restores the *complete* prior file (including its absence), not just the keys
    passed in.
    """
    from app.services import settings_store

    path = settings_store._settings_path()
    before = path.read_text(encoding="utf-8") if path.exists() else None
    try:
        settings_store.save_settings(dict(values))
        yield
    finally:
        if before is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(before, encoding="utf-8")


@pytest.fixture
def local_agent_target(workspace_dir):
    """Persist ``executionTarget="local-agent"`` for one test, restored after.

    For tests that genuinely mean to exercise the agent-dispatch branch. Pair it
    with a paired ``AgentDevice`` row, or the endpoint 409s.
    """
    with settings_override(executionTarget="local-agent"):
        yield


# Alias kept for the workstream that named the base fixture `app_env`.
@pytest.fixture
def app_env(workspace_dir) -> Iterator[dict]:
    import app.config as config_module

    yield {"settings": config_module.settings, "workspace": workspace_dir}


@pytest.fixture
def db_session(workspace_dir):
    """A session bound to the isolated temp DB."""
    import app.db as db_module

    session = db_module.SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def app(workspace_dir):
    """A fresh FastAPI app wired to the isolated temp DB."""
    from app.main import create_app

    return create_app()


@pytest.fixture
def client(app, db_session):
    """TestClient wired to the isolated DB. `get_db` is overridden to the shared
    session so request writes and test assertions see one consistent DB."""
    from app.db import get_db
    from fastapi.testclient import TestClient

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def shared_claude_credential(db_session):
    """Seed a shared Claude credential row (#95) so ``claude_cli.run_prompt``
    resolves an effective ``CLAUDE_CONFIG_DIR`` instead of raising "no
    credentials configured" — for tests that exercise the real ``run_prompt``
    body with only ``subprocess.Popen`` mocked out."""
    from app.services import claude_credentials

    claude_credentials.upsert_shared(db_session, '{"token": "test-token"}')
    return db_session


@pytest.fixture
def seed_ticket(db_session):
    """Seed one Ticket row directly in the temp DB (used by AI/review tests)."""
    from app.models.ticket import Ticket

    ticket = Ticket(
        external_id="SUR-1428",
        provider_kind="ado",
        title="Add password reset flow",
        work_item_type="User Story",
        status="Ready for QA",
        priority="High",
        assignee="Maya Kaur",
        sprint="Sprint 12",
        description="As a user I want to reset my password via email link.",
        acceptance_criteria=[
            "Given a valid email, a reset link is sent",
            "Reset link expires after 30 minutes",
        ],
    )
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket
