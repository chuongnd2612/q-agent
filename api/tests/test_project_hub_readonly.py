"""Projects addressed by GUID, and project settings read-only under the hub flag (#587).

Two things are pinned here, and they are the same idea seen from both ends:

* **The SPA addresses a project by its GUID**, so the routes have to answer to one
  — while a name-based bookmark still resolves rather than 404ing (the G1 bridge,
  removed in G4). Names collide across users and change on rename; #583 was the
  second user with a "Surency" being locked out of their own config permanently.
* **With ``QAGENT_HUB_DATA_ENABLED`` on, EmeHub owns project configuration**, so
  the write is refused by the API and not merely hidden by the UI. A read-only
  screen over a writable endpoint is the #512 defect in a new place: the screen
  says one thing and the API does another, and anything that isn't the screen
  writes into a store EmeHub is about to overwrite.

The refusal is gated on exactly the flag the SPA reads from ``/health``
(``settings.hub_data_enabled``), so the two surfaces cannot disagree.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.models.knowledge import ProjectKnowledge
from app.models.project import Project
from app.models.project_config import ProjectConfig
from app.models.user import User
from app.services import auth_service, hub_workspace

HUB = "https://hub.example.test/api"


@pytest.fixture
def auth_on(monkeypatch, workspace_dir):
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "auth_required", True)
    return config_module.settings


@pytest.fixture
def hub_owns_projects(monkeypatch, workspace_dir):
    """``QAGENT_HUB_DATA_ENABLED`` on.

    Applied AFTER ``workspace_dir``, which rebuilds ``settings`` in place — patch
    before it and the patch is silently undone (learned in ``test_hub_workspace``).
    """
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "hub_sso_enabled", True)
    monkeypatch.setattr(config_module.settings, "hub_data_enabled", True)
    monkeypatch.setattr(config_module.settings, "hub_base_url", HUB)
    return config_module.settings


def _user(db, email: str) -> User:
    user = User(email=email, password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _project(db, name: str, owner_id: int | None) -> Project:
    project = Project(provider_kind="ado", external_id=name, name=name, owner_id=owner_id)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _config(db, key: str, owner_id: int | None, base_url: str) -> ProjectConfig:
    row = ProjectConfig(key=key, name=key, owner_id=owner_id, base_url=base_url)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _hdr(user: User) -> dict:
    return {"Authorization": f"Bearer {auth_service.create_access_token(user, sid=f'sid-{user.id}')}"}


# ------------------------------------------------------- addressing by GUID
def test_config_is_written_by_guid(auth_on, client, db_session):
    """What the SPA now sends: the identifier in the URL is the GUID."""
    alice = _user(db_session, "alice-put-guid@example.com")
    project = _project(db_session, "Surency", alice.id)
    _config(db_session, "Surency", alice.id, "https://before.example")

    res = client.put(
        f"/projects/{project.guid}/config",
        json={"baseUrl": "https://after.example"},
        headers=_hdr(alice),
    )

    assert res.status_code == 200
    # The write landed on the row keyed by the NAME — the bridge translated it,
    # rather than creating a second config keyed by the GUID.
    rows = db_session.query(ProjectConfig).filter_by(owner_id=alice.id).all()
    assert [r.key for r in rows] == ["Surency"]
    assert rows[0].base_url == "https://after.example"


def test_config_is_still_written_by_name(auth_on, client, db_session):
    """A pre-#587 deep link or script must keep working until G4 removes the bridge."""
    alice = _user(db_session, "alice-put-name@example.com")
    _project(db_session, "Surency", alice.id)
    _config(db_session, "Surency", alice.id, "https://before.example")

    res = client.put(
        "/projects/Surency/config",
        json={"baseUrl": "https://by-name.example"},
        headers=_hdr(alice),
    )

    assert res.status_code == 200
    row = db_session.query(ProjectConfig).filter_by(owner_id=alice.id).one()
    assert row.base_url == "https://by-name.example"


def test_two_users_with_the_same_project_name_each_reach_their_own(auth_on, client, db_session):
    """#583, from the user's side: the second "Surency" owner is not locked out."""
    alice = _user(db_session, "alice-collide@example.com")
    bob = _user(db_session, "bob-collide@example.com")
    alices = _project(db_session, "Surency", alice.id)
    bobs = _project(db_session, "Surency", bob.id)
    _config(db_session, "Surency", alice.id, "https://alice.example")
    _config(db_session, "Surency", bob.id, "https://bob.example")

    alice_body = client.get(f"/projects/{alices.guid}/config", headers=_hdr(alice)).json()
    bob_body = client.get(f"/projects/{bobs.guid}/config", headers=_hdr(bob)).json()

    assert alice_body["baseUrl"] == "https://alice.example"
    assert bob_body["baseUrl"] == "https://bob.example"


def test_knowledge_is_reachable_by_guid(auth_on, client, db_session):
    """The knowledge routes take the same identifier the SPA now sends everywhere."""
    alice = _user(db_session, "alice-kb-guid@example.com")
    project = _project(db_session, "Surency", alice.id)
    db_session.add(
        ProjectKnowledge(key="Surency", project_key="Surency", name="Surency", owner_id=alice.id)
    )
    db_session.commit()

    res = client.get(f"/projects/{project.guid}/knowledge", headers=_hdr(alice))

    assert res.status_code == 200
    assert res.json()["key"] == "Surency"


def test_knowledge_by_guid_stays_owner_isolated(auth_on, client, db_session):
    """A GUID must not become a way around ownership on the knowledge routes."""
    alice = _user(db_session, "alice-kb-iso@example.com")
    bob = _user(db_session, "bob-kb-iso@example.com")
    bobs = _project(db_session, "Surency", bob.id)
    db_session.add(
        ProjectKnowledge(key="Surency", project_key="Surency", name="Surency", owner_id=bob.id)
    )
    db_session.commit()

    res = client.get(f"/projects/{bobs.guid}/knowledge", headers=_hdr(alice))

    assert res.status_code == 404


# --------------------------------------------- read-only under the hub flag
def test_config_put_is_refused_when_the_hub_owns_projects(
    auth_on, hub_owns_projects, client, db_session
):
    """The heart of it: the API refuses, so the read-only UI is not a UI-only promise."""
    alice = _user(db_session, "alice-refused@example.com")
    project = _project(db_session, "Surency", alice.id)
    _config(db_session, "Surency", alice.id, "https://before.example")

    res = client.put(
        f"/projects/{project.guid}/config",
        json={"baseUrl": "https://sneaky.example"},
        headers=_hdr(alice),
    )

    assert res.status_code == 409
    # Naming EmeHub is the point — "read-only" alone doesn't say where to go.
    assert "EmeHub" in res.json()["detail"]
    # And the refusal is real: nothing was written on the way to the error.
    assert (
        db_session.query(ProjectConfig).filter_by(owner_id=alice.id).one().base_url
        == "https://before.example"
    )


def test_config_put_by_name_is_refused_too(auth_on, hub_owns_projects, client, db_session):
    """The refusal is the endpoint's, not the GUID path's — the bridge is no bypass."""
    alice = _user(db_session, "alice-refused-name@example.com")
    _project(db_session, "Surency", alice.id)
    _config(db_session, "Surency", alice.id, "https://before.example")

    res = client.put(
        "/projects/Surency/config",
        json={"baseUrl": "https://sneaky.example"},
        headers=_hdr(alice),
    )

    assert res.status_code == 409


def test_config_is_still_readable_when_the_hub_owns_projects(
    auth_on, hub_owns_projects, client, db_session
):
    """Read-only means read-only, not gone. The screen still renders (#491)."""
    alice = _user(db_session, "alice-readable@example.com")
    project = _project(db_session, "Surency", alice.id)
    _config(db_session, "Surency", alice.id, "https://alice.example")

    res = client.get(f"/projects/{project.guid}/config", headers=_hdr(alice))

    assert res.status_code == 200
    assert res.json()["baseUrl"] == "https://alice.example"


def test_config_put_is_accepted_with_the_flag_off(auth_on, client, db_session):
    """The negative control: without the flag this endpoint behaves exactly as before.

    Without it, `test_config_put_is_refused_…` would still pass if the endpoint
    were broken for everyone.
    """
    alice = _user(db_session, "alice-flag-off@example.com")
    project = _project(db_session, "Surency", alice.id)
    _config(db_session, "Surency", alice.id, "https://before.example")

    res = client.put(
        f"/projects/{project.guid}/config",
        json={"baseUrl": "https://after.example"},
        headers=_hdr(alice),
    )

    assert res.status_code == 200
    assert (
        db_session.query(ProjectConfig).filter_by(owner_id=alice.id).one().base_url
        == "https://after.example"
    )


# ------------------------------------------------------- the hub deep link
def _mock_hub_projects(projects):
    respx.get(url__startswith=f"{HUB}/connections").mock(return_value=httpx.Response(200, json=[]))
    respx.get(url__startswith=f"{HUB}/tickets").mock(return_value=httpx.Response(200, json=[]))
    respx.get(url__startswith=f"{HUB}/projects").mock(
        return_value=httpx.Response(200, json=projects)
    )


@respx.mock
def test_the_mirror_records_the_hub_project_id(hub_owns_projects, db_session):
    """The hub deep-links by NUMERIC id, so that is what has to be kept."""
    alice = _user(db_session, "alice-mirror@example.com")
    _mock_hub_projects([{"id": 3, "key": "surency", "name": "Surency"}])

    hub_workspace.ensure_for_user(db_session, alice, "tok")

    project = db_session.query(Project).filter_by(owner_id=alice.id).one()
    assert project.hub_project_id == "3"


@respx.mock
def test_a_re_mirror_without_an_id_does_not_blank_the_link(hub_owns_projects, db_session):
    """Otherwise one odd hub response silently turns the deep link into the generic hint."""
    alice = _user(db_session, "alice-remirror@example.com")
    _mock_hub_projects([{"id": 3, "key": "surency", "name": "Surency"}])
    hub_workspace.ensure_for_user(db_session, alice, "tok")

    respx.get(url__startswith=f"{HUB}/projects").mock(
        return_value=httpx.Response(200, json=[{"key": "surency", "name": "Surency"}])
    )
    hub_workspace.ensure_for_user(db_session, alice, "tok")

    project = db_session.query(Project).filter_by(owner_id=alice.id).one()
    assert project.hub_project_id == "3"


def test_the_project_list_exposes_the_hub_project_id(auth_on, client, db_session):
    """The SPA builds `<hub web origin>/app/projects/{id}` from this field."""
    alice = _user(db_session, "alice-list-hubid@example.com")
    project = _project(db_session, "Surency", alice.id)
    project.hub_project_id = "3"
    db_session.commit()

    body = client.get("/projects", headers=_hdr(alice)).json()

    assert body[0]["hubProjectId"] == "3"


def test_a_locally_discovered_project_has_no_hub_project_id(auth_on, client, db_session):
    """`None`, not a guessed id: the UI must show a generic hint, never a broken link."""
    alice = _user(db_session, "alice-list-nohubid@example.com")
    _project(db_session, "Local Only", alice.id)

    body = client.get("/projects", headers=_hdr(alice)).json()

    assert body[0]["hubProjectId"] is None
