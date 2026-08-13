"""Mirroring a hub project's configuration into the caller's own (#590).

Q-Agent showed an empty Settings tab for a hub project — no repos, no
environments — because the project mirror created a bare ``projects`` row and
never copied the configuration hanging off it. The hub had it all along
(``GET /projects/{key}/config`` is agent-readable); we never asked.

Two things carry real risk and are pinned hardest here:

* **Connection ids are the HUB's.** Copying one straight into
  ``work_item_connection_id`` would bind the project to whatever local row holds
  that primary key — a different provider, or another user's connection.
* **Test-account passwords never cross.** Writing an empty list because the hub
  sent none would delete locally-held accounts *and* their passwords.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.models.project import Project
from app.models.project_config import ProjectConfig
from app.models.provider_connection import ProviderConnection
from app.models.user import User
from app.services import hub_workspace, project_config_service

HUB = "https://hub.example.test/api"
CONFIG_URL = f"{HUB}/projects/surency/config"


@pytest.fixture
def hub_on(monkeypatch, workspace_dir):
    """Flags on, applied AFTER ``workspace_dir`` — it rebuilds settings in place."""
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "hub_sso_enabled", True)
    monkeypatch.setattr(config_module.settings, "hub_data_enabled", True)
    monkeypatch.setattr(config_module.settings, "hub_base_url", HUB)
    monkeypatch.setattr(config_module.settings, "hub_internal_base_url", "")
    return config_module.settings


def _user(db, email="duna@example.com") -> User:
    user = User(email=email, password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _hub_project(db, user, *, name="Surency", key="surency", hub_id="3") -> Project:
    project = Project(
        provider_kind="ado", external_id=key, name=name, owner_id=user.id, hub_project_id=hub_id
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _payload(**over) -> dict:
    body = {
        "key": "surency",
        "name": "Surency",
        "baseUrl": "https://app.surency.test",
        "environments": [{"name": "staging", "baseUrl": "https://staging.surency.test"}],
        "repos": [
            {
                "name": "surency-admin-hub",
                "repo_url": "https://dev.azure.com/DDKS/Surency/_git/surency-admin-hub",
                "default_branch": "main",
                "local_repo_path": "",
                "default": True,
            },
            {
                "name": "surency-data",
                "repo_url": "https://dev.azure.com/DDKS/Surency/_git/surency-data",
                "default_branch": "main",
                "local_repo_path": "",
                "default": False,
            },
        ],
        "testAccounts": [],
        "manualAuth": False,
        "workItemConnectionId": 3,
        "repositoryConnectionId": 3,
        "extra": {},
    }
    body.update(over)
    return body


def _mock(payload=None):
    return respx.get(CONFIG_URL).mock(
        return_value=httpx.Response(200, json=payload if payload is not None else _payload())
    )


# ---------------------------------------------------------------- the fix
@respx.mock
def test_repos_and_environments_arrive(hub_on, db_session):
    user = _user(db_session)
    _hub_project(db_session, user)
    _mock()

    assert hub_workspace.ensure_project_config(db_session, user, "Surency", "tok") is True

    cfg = project_config_service.get_config_for_owner(db_session, "Surency", user.id)
    assert cfg is not None
    assert [r["name"] for r in cfg.repos] == ["surency-admin-hub", "surency-data"]
    assert cfg.base_url == "https://app.surency.test"
    assert cfg.environments and cfg.environments[0]["name"] == "staging"


@respx.mock
def test_mirroring_is_idempotent(hub_on, db_session):
    user = _user(db_session)
    _hub_project(db_session, user)
    _mock()

    hub_workspace.ensure_project_config(db_session, user, "Surency", "tok")
    hub_workspace.ensure_project_config(db_session, user, "Surency", "tok")

    rows = db_session.query(ProjectConfig).filter_by(key="Surency", owner_id=user.id).all()
    assert len(rows) == 1
    assert len(rows[0].repos) == 2


@respx.mock
def test_the_config_is_linked_to_the_project_guid(hub_on, db_session):
    user = _user(db_session)
    project = _hub_project(db_session, user)
    _mock()

    hub_workspace.ensure_project_config(db_session, user, "Surency", "tok")

    cfg = project_config_service.get_config_for_owner(db_session, "Surency", user.id)
    assert cfg.project_guid == project.guid


# ------------------------------------------------- connection id translation
@respx.mock
def test_hub_connection_ids_are_translated_to_local_ones(hub_on, db_session):
    """The hub's id 3 must resolve through `hub_connection_id`, not be copied."""
    user = _user(db_session)
    _hub_project(db_session, user)
    # A decoy holding local primary key 3 would be bound if the id were copied.
    for _ in range(2):
        db_session.add(ProviderConnection(owner_id=user.id, kind="jira", name="decoy", secrets={}))
    mirrored = ProviderConnection(
        owner_id=user.id, kind="ado", name="Surency", secrets={}, hub_connection_id="3"
    )
    db_session.add(mirrored)
    db_session.commit()
    db_session.refresh(mirrored)
    _mock()

    hub_workspace.ensure_project_config(db_session, user, "Surency", "tok")

    cfg = project_config_service.get_config_for_owner(db_session, "Surency", user.id)
    assert cfg.work_item_connection_id == mirrored.id
    assert cfg.repository_connection_id == mirrored.id


@respx.mock
def test_an_unmapped_connection_binds_to_nothing(hub_on, db_session):
    """No binding beats a wrong one — the #507 lesson."""
    user = _user(db_session)
    _hub_project(db_session, user)
    _mock(_payload(workItemConnectionId=999, repositoryConnectionId=999))

    hub_workspace.ensure_project_config(db_session, user, "Surency", "tok")

    cfg = project_config_service.get_config_for_owner(db_session, "Surency", user.id)
    assert cfg.work_item_connection_id is None
    assert cfg.repository_connection_id is None


@respx.mock
def test_another_users_mirrored_connection_is_never_bound(hub_on, db_session):
    user = _user(db_session)
    stranger = _user(db_session, "stranger@example.com")
    _hub_project(db_session, user)
    db_session.add(
        ProviderConnection(
            owner_id=stranger.id, kind="ado", name="Theirs", secrets={}, hub_connection_id="3"
        )
    )
    db_session.commit()
    _mock()

    hub_workspace.ensure_project_config(db_session, user, "Surency", "tok")

    cfg = project_config_service.get_config_for_owner(db_session, "Surency", user.id)
    assert cfg.work_item_connection_id is None


# ---------------------------------------------------------- test accounts
@respx.mock
def test_an_empty_hub_account_list_does_not_wipe_local_ones(hub_on, db_session):
    """The hub never sends passwords, so an empty list must mean "nothing to say"."""
    user = _user(db_session)
    _hub_project(db_session, user)
    db_session.add(
        ProjectConfig(
            key="Surency",
            name="Surency",
            owner_id=user.id,
            test_accounts=[{"role": "admin", "username": "qa@example.com", "password": "enc"}],
        )
    )
    db_session.commit()
    _mock()

    hub_workspace.ensure_project_config(db_session, user, "Surency", "tok")

    cfg = project_config_service.get_config_for_owner(db_session, "Surency", user.id)
    assert len(cfg.test_accounts) == 1
    assert cfg.test_accounts[0]["username"] == "qa@example.com"


# ---------------------------------------------------------------- guards
@respx.mock
def test_a_local_project_is_never_overwritten(hub_on, db_session):
    """No hub id -> not the hub's project -> its config is not ours to replace."""
    user = _user(db_session)
    local = Project(provider_kind="ado", external_id="local", name="Local Only", owner_id=user.id)
    db_session.add(local)
    db_session.add(
        ProjectConfig(key="Local Only", name="Local Only", owner_id=user.id, base_url="https://mine")
    )
    db_session.commit()
    route = _mock()

    assert hub_workspace.ensure_project_config(db_session, user, "Local Only", "tok") is False

    assert not route.called
    cfg = project_config_service.get_config_for_owner(db_session, "Local Only", user.id)
    assert cfg.base_url == "https://mine"


@respx.mock
def test_a_hub_failure_leaves_the_local_config_alone(hub_on, db_session):
    user = _user(db_session)
    _hub_project(db_session, user)
    respx.get(CONFIG_URL).mock(side_effect=httpx.ConnectError("refused"))

    assert hub_workspace.ensure_project_config(db_session, user, "Surency", "tok") is False


def test_flag_off_makes_no_hub_call(db_session, monkeypatch, workspace_dir):
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "hub_data_enabled", False)
    user = _user(db_session)
    _hub_project(db_session, user)

    with respx.mock:
        route = respx.get(CONFIG_URL).mock(return_value=httpx.Response(200, json=_payload()))
        assert hub_workspace.ensure_project_config(db_session, user, "Surency", "tok") is False

    assert not route.called


def test_no_hub_token_makes_no_hub_call(hub_on, db_session):
    user = _user(db_session)
    _hub_project(db_session, user)

    assert hub_workspace.ensure_project_config(db_session, user, "Surency", None) is False


# ---------------------------------------------------------------- end to end
@respx.mock
def test_the_config_endpoint_serves_the_mirrored_repos(hub_on, client, db_session, monkeypatch):
    """What the user actually reported: an empty Settings tab.

    ``auth_required`` is flipped on and a real token sent. With the suite default
    ``current_user`` is ``None``, and the mirror declines without an identity —
    correctly, since it has no owner to write the config for — so the test would
    assert against an unmirrored config and fail for the wrong reason.
    """
    import app.config as config_module
    from app.services import auth_service

    monkeypatch.setattr(config_module.settings, "auth_required", True)
    user = _user(db_session)
    _hub_project(db_session, user)
    _mock()

    token = auth_service.create_access_token(user, sid=f"sid-{user.id}")
    body = client.get(
        "/projects/Surency/config",
        headers={"X-Hub-Token": "tok", "Authorization": f"Bearer {token}"},
    ).json()

    assert [r["name"] for r in body["repos"]] == ["surency-admin-hub", "surency-data"]


@respx.mock
def test_the_repos_endpoint_serves_them_too(hub_on, client, db_session, monkeypatch):
    """The Repos tab reads a different route; both had to be wired."""
    import app.config as config_module
    from app.services import auth_service

    monkeypatch.setattr(config_module.settings, "auth_required", True)
    user = _user(db_session)
    _hub_project(db_session, user)
    _mock()

    token = auth_service.create_access_token(user, sid=f"sid-{user.id}")
    body = client.get(
        "/projects/Surency/repos",
        headers={"X-Hub-Token": "tok", "Authorization": f"Bearer {token}"},
    ).json()

    assert [r["name"] for r in body] == ["surency-admin-hub", "surency-data"]
