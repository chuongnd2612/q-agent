"""Setup readiness checklist (#642).

The point of this endpoint is that a new account is told what it still needs
*before* it clicks, instead of discovering it as a failure afterwards (#640). Two
properties carry that weight and are pinned here:

* **Owner scoping.** Another user's paired device must never make this account
  look ready — the same trap #583 hit with project config. With the suite's
  default ``auth_required=False`` the caller is ``None`` and every ownership check
  is a no-op, so these tests run with the guard ON and real bearer tokens.
* **Relevance.** ``required`` follows the settings in force. A server-target user
  nagged about a Local Agent that blocks nothing for them learns to ignore the
  checklist, which is worse than not having one.
"""

from __future__ import annotations

import pytest

from app.models.agent_device import AgentDevice
from app.models.project_config import ProjectConfig
from app.services import auth_service


def _make_user(db_session, email):
    from app.models.user import User

    user = User(
        email=email,
        first_name="Test",
        last_name="User",
        role="member",
        password_hash=auth_service.hash_password("password123"),
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _headers(user) -> dict:
    return {"Authorization": f"Bearer {auth_service.create_access_token(user, sid='test-sid')}"}


@pytest.fixture
def auth_on(monkeypatch):
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "auth_required", True)
    yield


def _pair_device(db_session, owner_id, name="PC-1"):
    device = AgentDevice(name=name, owner_id=owner_id, token_hash=f"hash-{owner_id}-{name}")
    db_session.add(device)
    db_session.commit()
    return device


def _item(payload, key):
    return next(i for i in payload["items"] if i["key"] == key)


def test_a_paired_device_belongs_to_its_owner_only(client, db_session, auth_on, local_agent_target):
    """#642: readiness must be per-user, or it lies to everyone but one account.

    A shows as having an agent; B, who paired nothing, must not — even though a
    device row exists in the same database.
    """
    user_a = _make_user(db_session, "a@example.com")
    user_b = _make_user(db_session, "b@example.com")
    _pair_device(db_session, user_a.id)

    a = client.get("/readiness", headers=_headers(user_a)).json()
    b = client.get("/readiness", headers=_headers(user_b)).json()

    assert _item(a, "localAgent")["ready"] is True
    assert _item(b, "localAgent")["ready"] is False
    # And it is a real blocker for B, because the target is the local agent.
    assert _item(b, "localAgent")["required"] is True
    assert b["ready"] is False


def test_the_agent_is_not_required_when_runs_execute_on_the_server(client, db_session, auth_on):
    """#642: an unmet item that blocks nothing must not be reported as blocking.

    The suite's default target is ``server`` (conftest), so this is that case: the
    item is still reported (unmet, so the UI can offer it) but `required` is false
    and it does not drag `ready` down.
    """
    user = _make_user(db_session, "server@example.com")

    payload = client.get("/readiness", headers=_headers(user)).json()
    agent = _item(payload, "localAgent")
    assert agent["ready"] is False
    assert agent["required"] is False, "a server-target user was told to pair an agent"
    assert agent["detail"] == "", "an irrelevant item should not explain itself as a blocker"


def test_live_authoring_makes_the_agent_and_a_base_url_required(client, db_session, auth_on):
    """#642: the items live-harness actually needs become required together.

    These are exactly the two prerequisites whose silent failure #641 had to make
    readable after the fact — `_enqueue_agent_authoring` raises for both.
    """
    from tests.conftest import settings_override

    user = _make_user(db_session, "live@example.com")

    with settings_override(authoringMode="live-harness"):
        payload = client.get("/readiness", headers=_headers(user)).json()

    assert _item(payload, "localAgent")["required"] is True
    assert _item(payload, "projectBaseUrl")["required"] is True
    assert "live authoring" in _item(payload, "localAgent")["detail"]


def test_a_base_url_on_a_visible_project_satisfies_the_item(client, db_session, auth_on):
    """#642: a shared (owner-less) config row counts — the user really can use it.

    Filtering strictly by owner here would report a blocker the user cannot act on
    and does not actually have.
    """
    from tests.conftest import settings_override

    user = _make_user(db_session, "base@example.com")
    db_session.add(ProjectConfig(key="shared-proj", owner_id=None, base_url="https://app.example.com"))
    db_session.commit()

    with settings_override(authoringMode="live-harness"):
        payload = client.get("/readiness", headers=_headers(user)).json()

    assert _item(payload, "projectBaseUrl")["ready"] is True


def test_captured_login_is_vacuously_ready_when_nothing_asks_for_one(client, db_session, auth_on):
    """#642: an unmeetable item is worse than no item.

    No project requests a manual login, so there is nothing to capture. Reporting
    "not ready" would leave a permanent red mark the user cannot ever clear.
    """
    from tests.conftest import settings_override

    user = _make_user(db_session, "nocapture@example.com")
    db_session.add(ProjectConfig(key="p", owner_id=user.id, base_url="https://app.example.com"))
    db_session.commit()

    with settings_override(authoringMode="live-harness"):
        payload = client.get("/readiness", headers=_headers(user)).json()

    captured = _item(payload, "capturedLogin")
    assert captured["ready"] is True
    assert captured["required"] is False


def test_a_manual_auth_project_without_a_capture_blocks(client, db_session, auth_on):
    """#642: the negative control for the vacuous case above."""
    from tests.conftest import settings_override

    user = _make_user(db_session, "manual@example.com")
    db_session.add(
        ProjectConfig(
            key="p", owner_id=user.id, base_url="https://app.example.com", manual_auth=True
        )
    )
    db_session.commit()

    with settings_override(authoringMode="live-harness"):
        payload = client.get("/readiness", headers=_headers(user)).json()

    captured = _item(payload, "capturedLogin")
    assert captured["ready"] is False
    assert captured["required"] is True
    assert payload["ready"] is False


def test_the_hub_owned_credential_is_never_reported_missing(client, db_session, auth_on, monkeypatch):
    """#651: don't assert a blocker on a setting whose authority we cannot see.

    Under hub management EmeHub owns the Claude credential and Q-Agent materialises
    it PER RUN from a browser-minted hub token — it is never in the local store, so
    `resolve_effective_config_dir` legitimately finds nothing. Reporting that as a
    blocker put "No Claude credential resolves for this account" on the Automation
    screen while the same screen showed live plan usage from the credential that
    plainly did resolve.

    That is the exact failure #642 set out to avoid (an alarm on something that
    doesn't matter), landing on the first row of the list — which teaches users to
    scroll past the rows that do matter.
    """
    import app.config as config_module

    user = _make_user(db_session, "hubcred@example.com")
    monkeypatch.setattr(config_module.settings, "hub_data_enabled", True)

    payload = client.get("/readiness", headers=_headers(user)).json()
    item = _item(payload, "claudeCredential")

    assert item["managed"] is True, "the hub-owned credential must be marked managed"
    assert item["required"] is False, "a state we did not verify must not block"
    assert item["detail"] == "", "do not claim a credential is missing when we didn't look"
    assert item["fix"] == "hub", "the fix lives in EmeHub, not Q-Agent's Settings"
    # And it is not a blocker. Asserting the aggregate `ready` here would test the
    # wrong thing: this account also has no provider connection, so `ready` is
    # legitimately false for a reason that has nothing to do with the credential.
    blocking = [i["key"] for i in payload["items"] if i["required"] and not i["ready"]]
    assert "claudeCredential" not in blocking
    assert payload["hubManaged"] is True


def test_without_hub_management_a_missing_credential_still_blocks(client, db_session, auth_on):
    """#651 negative control: the local check keeps its teeth when it IS the authority."""
    user = _make_user(db_session, "localcred@example.com")

    payload = client.get("/readiness", headers=_headers(user)).json()
    item = _item(payload, "claudeCredential")

    assert item.get("managed") is False
    assert item["required"] is True
    assert item["ready"] is False
    assert "No Claude credential" in item["detail"]
    assert payload["ready"] is False
