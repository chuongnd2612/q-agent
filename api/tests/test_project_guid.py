"""Projects are identified by a GUID, not by their name (G1 of #585).

Two properties this pins, both of which the name-as-identity model got wrong:

* **Names collide across users** — two people may each have a "Surency", and a
  GUID must resolve to *their own*, never the other's (the #583 failure).
* **A rename must not move identity** — the GUID is stable, so everything hanging
  off it stays attached.

G1 is a bridge: routes still key off the name internally, and
``resolve_project_identifier`` is the single place that understands both. These tests
are written against that seam so they keep meaning when G2–G4 move the storage.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.project import Project
from app.models.project_config import ProjectConfig
from app.models.user import User
from app.services import auth_service, project_config_service


@pytest.fixture
def auth_on(monkeypatch, workspace_dir):
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "auth_required", True)
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


def _hdr(user: User) -> dict:
    return {"Authorization": f"Bearer {auth_service.create_access_token(user, sid=f'sid-{user.id}')}"}


# ---------------------------------------------------------------- generation
def test_every_project_gets_a_guid(db_session):
    project = _project(db_session, "Surency", None)

    assert project.guid
    uuid.UUID(project.guid)  # raises if it isn't a real UUID


def test_guids_are_unique_per_project(db_session):
    a = _project(db_session, "Alpha", None)
    b = _project(db_session, "Beta", None)

    assert a.guid != b.guid


def test_two_users_may_share_a_name_with_distinct_guids(db_session):
    alice = _user(db_session, "alice-guid@example.com")
    bob = _user(db_session, "bob-guid@example.com")

    a = _project(db_session, "Surency", alice.id)
    b = _project(db_session, "Surency", bob.id)

    assert a.guid != b.guid


# ---------------------------------------------------------------- resolution
def test_a_guid_resolves_to_its_project_name(auth_on, db_session):
    alice = _user(db_session, "alice-res@example.com")
    project = _project(db_session, "Surency", alice.id)

    assert project_config_service.resolve_project_identifier(db_session, project.guid, alice) == "Surency"


def test_a_name_passes_through_unchanged(auth_on, db_session):
    """Every existing name-based caller must keep working during the bridge."""
    alice = _user(db_session, "alice-name@example.com")

    assert project_config_service.resolve_project_identifier(db_session, "Surency", alice) == "Surency"


def test_an_unknown_guid_is_returned_unchanged(auth_on, db_session):
    """Not an error: it simply isn't a project, so the caller 404s as before."""
    alice = _user(db_session, "alice-unknown@example.com")
    stranger_guid = str(uuid.uuid4())

    assert (
        project_config_service.resolve_project_identifier(db_session, stranger_guid, alice)
        == stranger_guid
    )


def test_a_guid_never_resolves_another_users_project(auth_on, db_session):
    """Otherwise a GUID would be an oracle for other people's project names."""
    alice = _user(db_session, "alice-iso@example.com")
    bob = _user(db_session, "bob-iso@example.com")
    bobs = _project(db_session, "Bob Secret Project", bob.id)

    resolved = project_config_service.resolve_project_identifier(db_session, bobs.guid, alice)

    assert resolved == bobs.guid  # unchanged — Alice learns nothing
    assert resolved != "Bob Secret Project"


def test_a_shared_project_resolves_for_anyone(auth_on, db_session):
    """`owner_id IS NULL` is deliberately everyone's (ADR 0009)."""
    alice = _user(db_session, "alice-shared-guid@example.com")
    shared = _project(db_session, "Shared Surency", None)

    assert project_config_service.resolve_project_identifier(db_session, shared.guid, alice) == (
        "Shared Surency"
    )


# ---------------------------------------------------------------- end to end
def test_config_is_reachable_by_guid(auth_on, client, db_session):
    """The point of G1: a GUID URL works before the rest of the refactor lands."""
    alice = _user(db_session, "alice-e2e@example.com")
    project = _project(db_session, "Surency", alice.id)
    db_session.add(
        ProjectConfig(key="Surency", name="Surency", owner_id=alice.id, base_url="https://a.example")
    )
    db_session.commit()

    res = client.get(f"/projects/{project.guid}/config", headers=_hdr(alice))

    assert res.status_code == 200
    assert res.json()["baseUrl"] == "https://a.example"


def test_config_by_guid_is_owner_isolated(auth_on, client, db_session):
    alice = _user(db_session, "alice-e2e-iso@example.com")
    bob = _user(db_session, "bob-e2e-iso@example.com")
    bobs = _project(db_session, "Surency", bob.id)
    db_session.add(
        ProjectConfig(key="Surency", name="Surency", owner_id=bob.id, base_url="https://bob.example")
    )
    db_session.commit()

    res = client.get(f"/projects/{bobs.guid}/config", headers=_hdr(alice))

    # Alice may not read Bob's config through his GUID.
    assert res.status_code == 200
    assert res.json()["baseUrl"] != "https://bob.example"


def test_the_project_list_exposes_the_guid(auth_on, client, db_session):
    """The SPA needs it to address projects by GUID (G3)."""
    alice = _user(db_session, "alice-list@example.com")
    project = _project(db_session, "Surency", alice.id)

    body = client.get("/projects", headers=_hdr(alice)).json()

    assert body[0]["guid"] == project.guid


def test_renaming_a_project_keeps_its_guid(auth_on, db_session):
    """Identity must survive the rename that used to orphan everything."""
    alice = _user(db_session, "alice-rename@example.com")
    project = _project(db_session, "Old Name", alice.id)
    original = project.guid

    project.name = "New Name"
    db_session.commit()
    db_session.refresh(project)

    assert project.guid == original
    assert project_config_service.resolve_project_identifier(db_session, original, alice) == "New Name"
