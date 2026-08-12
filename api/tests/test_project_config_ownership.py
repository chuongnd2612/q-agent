"""Project config is per-user, even when two users name a project the same (#583).

`project_config` is keyed by project **name** and unique per ``(key, owner_id)``,
so two people with a "Surency" project is the ordinary case, not an exotic one.
The lookup used by the routes matched the first row for a key *regardless of
owner* and then rejected it, so the second user got a permanent 404 and could
never create their own row.

Every test here runs with ``auth_required=True`` and a real bearer token. With the
suite default ``current_user`` is ``None``, ``owned()`` is a passthrough and the
ownership checks are no-ops — a test written without it passes while exercising
nothing.
"""

from __future__ import annotations

import pytest

from app.models.project_config import ProjectConfig
from app.models.user import User
from app.services import auth_service, project_config_service

KEY = "Surency"


@pytest.fixture
def auth_on(monkeypatch, workspace_dir):
    """Identity in play. Applied after ``workspace_dir``, which rebuilds settings."""
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "auth_required", True)
    return config_module.settings


def _user(db, email: str) -> User:
    user = User(email=email, password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _hdr(user: User) -> dict:
    return {"Authorization": f"Bearer {auth_service.create_access_token(user, sid=f'sid-{user.id}')}"}


def _config(db, key: str, owner_id: int | None, base_url: str) -> ProjectConfig:
    row = ProjectConfig(key=key, name=key, owner_id=owner_id, base_url=base_url)
    db.add(row)
    db.commit()
    return row


# ------------------------------------------------------- the reported failure
def test_second_user_gets_their_own_config_not_a_404(auth_on, client, db_session):
    """The bug: another user's same-keyed row 404'd this user out of their own."""
    alice = _user(db_session, "alice-cfg@example.com")
    bob = _user(db_session, "bob-cfg@example.com")
    _config(db_session, KEY, alice.id, "https://alice.example")

    res = client.get(f"/projects/{KEY}/config", headers=_hdr(bob))

    assert res.status_code == 200
    # Bob sees a default, NOT Alice's row.
    assert res.json()["baseUrl"] != "https://alice.example"


def test_second_user_gets_repos_not_a_404(auth_on, client, db_session):
    alice = _user(db_session, "alice-repos@example.com")
    bob = _user(db_session, "bob-repos@example.com")
    _config(db_session, KEY, alice.id, "https://alice.example")

    assert client.get(f"/projects/{KEY}/repos", headers=_hdr(bob)).status_code == 200


# ------------------------------------------------------------------ isolation
def test_each_user_reads_their_own_config(auth_on, client, db_session):
    alice = _user(db_session, "alice-own@example.com")
    bob = _user(db_session, "bob-own@example.com")
    _config(db_session, KEY, alice.id, "https://alice.example")
    _config(db_session, KEY, bob.id, "https://bob.example")

    assert client.get(f"/projects/{KEY}/config", headers=_hdr(alice)).json()["baseUrl"] == (
        "https://alice.example"
    )
    assert client.get(f"/projects/{KEY}/config", headers=_hdr(bob)).json()["baseUrl"] == (
        "https://bob.example"
    )


def test_saving_never_writes_into_another_users_row(auth_on, client, db_session):
    """The dangerous half. Fixing the read alone would let this save land in
    Alice's row, turning a 404 into cross-user corruption."""
    alice = _user(db_session, "alice-write@example.com")
    bob = _user(db_session, "bob-write@example.com")
    _config(db_session, KEY, alice.id, "https://alice.example")

    res = client.put(
        f"/projects/{KEY}/config", json={"baseUrl": "https://bob.example"}, headers=_hdr(bob)
    )

    assert res.status_code == 200
    db_session.expire_all()
    alice_row = project_config_service.get_config_for_owner(db_session, KEY, alice.id)
    bob_row = project_config_service.get_config_for_owner(db_session, KEY, bob.id)
    # The negative control: Alice's row must be untouched, and Bob's must exist.
    assert alice_row is not None and alice_row.base_url == "https://alice.example"
    assert bob_row is not None and bob_row.base_url == "https://bob.example"


def test_a_users_own_row_wins_over_a_shared_one(auth_on, client, db_session):
    alice = _user(db_session, "alice-pref@example.com")
    _config(db_session, KEY, None, "https://shared.example")
    _config(db_session, KEY, alice.id, "https://alice.example")

    assert client.get(f"/projects/{KEY}/config", headers=_hdr(alice)).json()["baseUrl"] == (
        "https://alice.example"
    )


# --------------------------------------------------------------- shared rows
def test_a_shared_config_stays_visible(auth_on, client, db_session):
    """`owner_id IS NULL` is deliberately everyone's (ADR 0009 / `_ownership_mismatch`)."""
    alice = _user(db_session, "alice-shared@example.com")
    _config(db_session, KEY, None, "https://shared.example")

    assert client.get(f"/projects/{KEY}/config", headers=_hdr(alice)).json()["baseUrl"] == (
        "https://shared.example"
    )


def test_editing_a_shared_config_keeps_it_shared(auth_on, client, db_session):
    """Editing the shared row must not silently fork it into a private copy —
    that would strand every other user on the old values."""
    alice = _user(db_session, "alice-editshared@example.com")
    _config(db_session, KEY, None, "https://shared.example")

    res = client.put(
        f"/projects/{KEY}/config", json={"baseUrl": "https://shared-v2.example"}, headers=_hdr(alice)
    )

    assert res.status_code == 200
    db_session.expire_all()
    shared = project_config_service.get_config_for_owner(db_session, KEY, None)
    assert shared is not None and shared.base_url == "https://shared-v2.example"
    assert project_config_service.get_config_for_owner(db_session, KEY, alice.id) is None


# ------------------------------------------------------------- no config yet
def test_a_project_with_no_config_returns_a_default(auth_on, client, db_session):
    alice = _user(db_session, "alice-empty@example.com")

    res = client.get("/projects/NeverConfigured/config", headers=_hdr(alice))

    assert res.status_code == 200
    assert res.json()["key"] == "NeverConfigured"


# ------------------------------------------------------------------- service
def test_visible_to_prefers_own_then_shared_then_none(auth_on, db_session):
    alice = _user(db_session, "alice-svc@example.com")
    bob = _user(db_session, "bob-svc@example.com")

    assert project_config_service.get_config_visible_to(db_session, KEY, alice) is None

    _config(db_session, KEY, bob.id, "https://bob.example")
    # Bob's row must never be visible to Alice.
    assert project_config_service.get_config_visible_to(db_session, KEY, alice) is None

    _config(db_session, KEY, None, "https://shared.example")
    row = project_config_service.get_config_visible_to(db_session, KEY, alice)
    assert row is not None and row.base_url == "https://shared.example"

    _config(db_session, KEY, alice.id, "https://alice.example")
    row = project_config_service.get_config_visible_to(db_session, KEY, alice)
    assert row is not None and row.base_url == "https://alice.example"
