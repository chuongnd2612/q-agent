"""Tests for Project Config: encrypted secrets, masking, and context injection."""

from __future__ import annotations

from app import crypto
from app.models.project_config import ProjectConfig
from app.models.provider_connection import ProviderConnection
from app.models.ticket import Ticket
from app.services import project_config_service


def test_save_config_masks_password_and_encrypts_at_rest(client, db_session):
    resp = client.put(
        "/projects/Surency Platform/config",
        json={
            "baseUrl": "https://staging.surency.test",
            "localRepoPath": "/tmp/does-not-exist",
            "testAccounts": [
                {"role": "Internal Admin", "username": "qa@surency.test",
                 "password": "s3cret!", "notes": "primary"}
            ],
            "environments": [{"name": "Staging", "baseUrl": "https://staging.surency.test", "notes": ""}],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    # Password is never returned; only a boolean flag.
    account = data["testAccounts"][0]
    assert account["hasPassword"] is True
    assert "password" not in account
    assert data["baseUrl"] == "https://staging.surency.test"

    # Stored ciphertext is encrypted, not plaintext.
    row = db_session.query(ProjectConfig).filter_by(key="Surency Platform").one()
    stored = row.test_accounts[0]["password"]
    assert stored != "s3cret!"
    assert crypto.is_encrypted(stored)
    assert crypto.decrypt(stored) == "s3cret!"


def test_blank_password_preserves_stored_secret(client, db_session):
    client.put(
        "/projects/P/config",
        json={"testAccounts": [{"role": "Admin", "username": "a@b.c", "password": "orig", "notes": ""}]},
    )
    # Re-save with a blank password (UI submitting the masked form).
    client.put(
        "/projects/P/config",
        json={"testAccounts": [{"role": "Admin", "username": "a@b.c", "password": "", "notes": "edited"}]},
    )
    row = db_session.query(ProjectConfig).filter_by(key="P").one()
    assert crypto.decrypt(row.test_accounts[0]["password"]) == "orig"
    assert row.test_accounts[0]["notes"] == "edited"


def test_get_config_defaults_when_absent(client):
    data = client.get("/projects/Nope/config").json()
    assert data["key"] == "Nope"
    assert data["testAccounts"] == []
    assert data["baseUrl"] == ""
    assert data["manualAuth"] is False


def test_manual_auth_round_trips_via_config(client, db_session):
    resp = client.put("/projects/P/config", json={"manualAuth": True})
    assert resp.status_code == 200
    assert resp.json()["manualAuth"] is True

    row = db_session.query(ProjectConfig).filter_by(key="P").one()
    assert row.manual_auth is True

    # And it survives a read-back / can be toggled off.
    assert client.get("/projects/P/config").json()["manualAuth"] is True
    assert client.put("/projects/P/config", json={"manualAuth": False}).json()["manualAuth"] is False


def test_auth_state_reflects_session_file_and_delete_removes_it(client, app_env):
    key = "Surency Platform"
    # No session yet.
    state = client.get(f"/projects/{key}/auth").json()
    assert state["exists"] is False
    assert state["capturedAt"] is None

    # Create the saved session file where the service expects it.
    path = project_config_service.auth_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"cookies": []}', encoding="utf-8")

    state = client.get(f"/projects/{key}/auth").json()
    assert state["exists"] is True
    assert state["capturedAt"] is not None

    # DELETE removes it and returns the now-empty state.
    deleted = client.delete(f"/projects/{key}/auth").json()
    assert deleted["exists"] is False
    assert not path.exists()


def test_capture_auth_requires_base_url(client, app_env):
    resp = client.post("/projects/NoUrl/auth/capture")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Set a base URL for the project first."


def test_capture_auth_runs_in_background_and_saves_session(client, app_env, monkeypatch):
    import time

    from app.services import playwright_runner

    key = "Surency Platform"
    client.put(f"/projects/{key}/config", json={"baseUrl": "https://staging.surency.test"})

    # Stand in for the real headed-browser capture: write a dummy storageState.
    def _fake_capture(base_url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text('{"cookies": []}', encoding="utf-8")
        return True

    monkeypatch.setattr(playwright_runner, "capture_storage_state", _fake_capture)

    resp = client.post(f"/projects/{key}/auth/capture")
    assert resp.status_code == 200
    assert resp.json()["capturing"] is True

    # Poll until the background thread finishes.
    for _ in range(100):
        if not playwright_runner.is_capturing(key):
            break
        time.sleep(0.05)

    state = client.get(f"/projects/{key}/auth").json()
    assert state["capturing"] is False
    assert state["exists"] is True


def test_build_context_resolves_via_connection_and_decrypts(db_session):
    conn = ProviderConnection(kind="ado", name="ADO", connected=True,
                              config={"project": "Surency Platform"}, secrets={})
    db_session.add(conn)
    db_session.flush()
    db_session.add(
        ProjectConfig(
            key="Surency Platform", name="Surency Platform",
            base_url="https://app.test",
            environments=[{"name": "Staging", "base_url": "https://staging.test", "notes": ""}],
            test_accounts=[{"role": "Admin", "username": "u", "password": crypto.encrypt("pw"), "notes": ""}],
        )
    )
    ticket = Ticket(external_id="SUR-1", provider_kind="ado", title="t", connection_id=conn.id)
    db_session.add(ticket)
    db_session.commit()

    ctx = project_config_service.build_context(db_session, ticket, env="Staging")
    assert ctx["projectKey"] == "Surency Platform"
    # Per-environment URL wins when the run env matches.
    assert ctx["baseUrl"] == "https://staging.test"
    # Passwords are decrypted for the generator (never over the API).
    assert ctx["testAccounts"][0]["password"] == "pw"


def test_spec_prompt_bakes_real_values_when_context_present(db_session, monkeypatch):
    from app.services import spec_service
    from app.models.testcase import TestCase

    context = {
        "projectKey": "P",
        "baseUrl": "https://app.test",
        "testAccounts": [{"role": "Admin", "username": "qa@app.test", "password": "pw123"}],
        "routes": [{"path": "/groups", "description": "Groups list"}],
    }
    case = TestCase(
        run_id=1, ticket_external_id="SUR-1", code="TC-01",
        title="Open groups", precondition="Logged in as Admin",
        steps=[{"a": "Go to groups", "e": "List shows"}],
    )
    prompt = spec_service._build_prompt(case, context)
    assert "https://app.test" in prompt
    assert "qa@app.test" in prompt
    assert "pw123" in prompt  # literal credentials, per the product decision
    assert "/groups" in prompt
    # The old "use placeholders" instruction is gone when context is present.
    assert "reasonable placeholders" not in prompt


# ------------------------------------------------- project-key resolution (#663)
def _ado_connection(db_session, project: str, name: str = "surency"):
    """A work-item connection naming its provider project, as ADO reports it."""
    from app.models.provider_connection import ProviderConnection

    conn = ProviderConnection(
        kind="ado",
        name=name,
        config={"project": project, "baseUrl": "https://dev.azure.com/DDKS"},
    )
    db_session.add(conn)
    db_session.commit()
    db_session.refresh(conn)
    return conn


def test_the_connection_id_link_decides_which_project_a_ticket_belongs_to(db_session):
    """#663: resolve by id, because the provider's name cannot tell them apart.

    Reproduces the reported install: TWO Q-Agent projects wired to two different
    connections that both point at the SAME Azure DevOps project, so both report
    `config.project == "Surency"`. No comparison of that string — case-sensitive or
    not — can decide which project a ticket belongs to. The id link the user
    configured can.
    """
    from app.models.project_config import ProjectConfig
    from app.services import project_config_service

    conn_a = _ado_connection(db_session, "Surency", name="surency")
    conn_b = _ado_connection(db_session, "Surency", name="surency 2")
    db_session.add(
        ProjectConfig(
            key="surency",
            base_url="https://one.example.com",
            work_item_connection_id=conn_a.id,
        )
    )
    db_session.add(
        ProjectConfig(
            key="surency 3",
            base_url="https://three.example.com",
            work_item_connection_id=conn_b.id,
        )
    )
    db_session.commit()

    assert project_config_service.resolve_project_key(db_session, conn_a) == "surency"
    assert project_config_service.resolve_project_key(db_session, conn_b) == "surency 3"


def test_two_projects_claiming_one_connection_is_refused(db_session):
    """#663: a data error must not be resolved by picking a winner."""
    from app.models.project_config import ProjectConfig
    from app.services import project_config_service

    conn = _ado_connection(db_session, "Surency")
    db_session.add(ProjectConfig(key="a", work_item_connection_id=conn.id))
    db_session.add(ProjectConfig(key="b", work_item_connection_id=conn.id))
    db_session.commit()

    assert project_config_service.resolve_project_key(db_session, conn) is None


def test_a_provider_project_matches_its_key_case_insensitively(db_session):
    """#663: ADO says "Surency", the config key is "surency" — same project.

    The comparison was exact, so this never matched and resolution silently fell
    through to the "only one project exists" fallback instead.
    """
    from app.models.project_config import ProjectConfig
    from app.services import project_config_service

    db_session.add(ProjectConfig(key="surency", base_url="https://hub-dev.surency.com/"))
    db_session.commit()

    conn = _ado_connection(db_session, "Surency")
    assert project_config_service.resolve_project_key(db_session, conn) == "surency"


def test_adding_a_second_project_does_not_break_the_first(db_session):
    """#663: the regression this fixes, reproduced.

    With one project the sole-project fallback masked the failed exact match. Add a
    second and the fallback returns None, so `projectKey` comes back empty, the
    case context has no base URL, and generation fails with "No base URL in the
    project context" — months after the run last worked, and with no visible
    connection to the change that caused it.
    """
    from app.models.project_config import ProjectConfig
    from app.services import project_config_service

    db_session.add(ProjectConfig(key="surency", base_url="https://hub-dev.surency.com/"))
    db_session.commit()
    conn = _ado_connection(db_session, "Surency")
    assert project_config_service.resolve_project_key(db_session, conn) == "surency"

    # The user configures a SECOND project. The first must keep resolving.
    db_session.add(ProjectConfig(key="surency 3", base_url="https://hub-dev.surency.com"))
    db_session.commit()
    assert project_config_service.resolve_project_key(db_session, conn) == "surency"


def test_an_exact_key_wins_over_a_case_variant(db_session):
    """#663: never "correct" a key that is already right."""
    from app.models.project_config import ProjectConfig
    from app.services import project_config_service

    db_session.add(ProjectConfig(key="Surency", base_url="https://exact.example.com"))
    db_session.add(ProjectConfig(key="surency", base_url="https://lower.example.com"))
    db_session.commit()

    conn = _ado_connection(db_session, "Surency")
    assert project_config_service.resolve_project_key(db_session, conn) == "Surency"


def test_keys_differing_only_by_case_are_refused_not_guessed(db_session):
    """#663: two candidates, no way to tell which the provider meant.

    Guessing would be a coin flip that writes into the wrong project's config, so
    resolution declines — and the caller's error names the real problem instead of
    silently using someone else's base URL.
    """
    from app.models.project_config import ProjectConfig
    from app.services import project_config_service

    db_session.add(ProjectConfig(key="surency", base_url="https://one.example.com"))
    db_session.add(ProjectConfig(key="SURENCY", base_url="https://two.example.com"))
    db_session.commit()

    conn = _ado_connection(db_session, "Surency")
    assert project_config_service.resolve_project_key(db_session, conn) is None
