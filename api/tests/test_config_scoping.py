"""Tests for per-user scoping of the config/provider domain (#93).

Builds on the #91 ownership foundation: with ``auth_required`` on, each user
only ever sees/uses their own provider connections and project config. The
existing (auth-disabled) suite exercises the ``user=None`` bridge path, which
must stay green — see ``test_bridge_stays_green_without_auth`` below.
"""

from __future__ import annotations

import pytest

from app.models.project_config import ProjectConfig
from app.models.provider_connection import ProviderConnection
from app.services import auth_service


def _make_user(db_session, email, password="password123", role="member"):
    from app.models.user import User

    user = User(
        email=email,
        first_name="Test",
        last_name="User",
        role=role,
        password_hash=auth_service.hash_password(password),
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _auth_headers(user) -> dict:
    token = auth_service.create_access_token(user, sid="test-sid")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def auth_on(monkeypatch):
    """Turn the global auth guard on for the duration of a test."""
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "auth_required", True)
    yield


@pytest.fixture
def two_users(db_session, auth_on):
    """Two authenticated users (A, B) with bearer tokens ready to use."""
    user_a = _make_user(db_session, "a@example.com")
    user_b = _make_user(db_session, "b@example.com")
    return {
        "a": (user_a, _auth_headers(user_a)),
        "b": (user_b, _auth_headers(user_b)),
    }


# --------------------------------------------------------- provider connections
def test_connection_list_is_scoped_to_owner(client, two_users):
    _, headers_a = two_users["a"]
    _, headers_b = two_users["b"]

    client.post("/providers/ado/connections", json={"name": "A's ADO"}, headers=headers_a)

    groups_a = {g["kind"]: g for g in client.get("/providers", headers=headers_a).json()}
    assert groups_a["ado"]["connectionCount"] == 1

    groups_b = {g["kind"]: g for g in client.get("/providers", headers=headers_b).json()}
    assert groups_b["ado"]["connectionCount"] == 0


def test_other_user_cannot_get_update_delete_test_a_connection(client, db_session, two_users):
    user_a, headers_a = two_users["a"]
    _, headers_b = two_users["b"]

    conn = client.post(
        "/providers/ado/connections", json={"name": "A's ADO"}, headers=headers_a
    ).json()
    conn_id = conn["id"]
    assert db_session.get(ProviderConnection, conn_id).owner_id == user_a.id

    # B cannot see, update, test, discover sub-resources of, or delete A's connection.
    assert client.put(
        f"/connections/{conn_id}", json={"name": "hijacked"}, headers=headers_b
    ).status_code == 404
    assert client.post(f"/connections/{conn_id}/test", headers=headers_b).status_code == 404
    assert client.get(f"/connections/{conn_id}/sprints", headers=headers_b).status_code == 404
    assert (
        client.get(f"/connections/{conn_id}/work-item-metadata", headers=headers_b).status_code
        == 404
    )
    assert client.get(f"/connections/{conn_id}/repos", headers=headers_b).status_code == 404
    assert client.delete(f"/connections/{conn_id}", headers=headers_b).status_code == 404

    # The connection is untouched — A can still reach it.
    resp = client.put(f"/connections/{conn_id}", json={"name": "still A's"}, headers=headers_a)
    assert resp.status_code == 200
    assert resp.json()["name"] == "still A's"


def test_owner_can_manage_their_own_connection(client, two_users):
    _, headers_a = two_users["a"]

    conn = client.post(
        "/providers/jira/connections", json={"name": "A's Jira"}, headers=headers_a
    ).json()

    resp = client.put(
        f"/connections/{conn['id']}",
        json={"secrets": {"apiToken": "tok"}},
        headers=headers_a,
    )
    assert resp.status_code == 200
    assert client.delete(f"/connections/{conn['id']}", headers=headers_a).status_code == 204


# --------------------------------------------------------------- project config
def test_other_user_cannot_read_or_write_a_project_config(client, db_session, two_users):
    """B never sees or touches A's config — asserted as a property, not as a 404.

    This used to expect 404 on both calls. That was the *mechanism*, and it was
    also the bug (#583): the lookup matched A's row by key regardless of owner and
    then rejected it, so B was locked out of a key A happened to use — and could
    never create their own, even though `project_config` is unique per
    ``(key, owner_id)`` precisely so two people can each have one.

    What matters is unchanged, and is what this now pins: A's data is neither
    readable nor writable by B.
    """
    user_a, headers_a = two_users["a"]
    user_b, headers_b = two_users["b"]

    resp = client.put(
        "/projects/Surency Platform/config",
        json={"baseUrl": "https://a.example.test"},
        headers=headers_a,
    )
    assert resp.status_code == 200
    row = db_session.query(ProjectConfig).filter_by(
        key="Surency Platform", owner_id=user_a.id
    ).one()
    assert row.owner_id == user_a.id

    # B reads their OWN (absent -> default); A's value must not appear.
    b_read = client.get("/projects/Surency Platform/config", headers=headers_b)
    assert b_read.status_code == 200
    assert b_read.json()["baseUrl"] != "https://a.example.test"

    # B writes their OWN row rather than overwriting A's.
    assert (
        client.put(
            "/projects/Surency Platform/config",
            json={"baseUrl": "https://b.example.test"},
            headers=headers_b,
        ).status_code
        == 200
    )

    # Negative control: A's row untouched, and the two rows are distinct.
    db_session.expire_all()
    a_row = db_session.query(ProjectConfig).filter_by(
        key="Surency Platform", owner_id=user_a.id
    ).one()
    b_row = db_session.query(ProjectConfig).filter_by(
        key="Surency Platform", owner_id=user_b.id
    ).one()
    assert a_row.base_url == "https://a.example.test"
    assert b_row.base_url == "https://b.example.test"

    resp = client.get("/projects/Surency Platform/config", headers=headers_a)
    assert resp.status_code == 200
    assert resp.json()["baseUrl"] == "https://a.example.test"


def test_project_config_manual_auth_endpoints_scoped_to_owner(client, two_users):
    """B's manual-auth calls act on B's own namespace, never A's or the shared one.

    Also previously a 404 assertion (#583). The risk when that loosened was
    specific and worth pinning: these handlers derive an ``owner_id`` for the
    on-disk auth namespace, and a caller with no config would have fallen to
    ``None`` — the **shared/admin** namespace — so B's capture could have written
    where every user reads. It now falls to the caller's own id.
    """
    _, headers_a = two_users["a"]
    _, headers_b = two_users["b"]

    client.put(
        "/projects/Surency Platform/config",
        json={"baseUrl": "https://a.example.test"},
        headers=headers_a,
    )

    # B sees their own (empty) state, not A's.
    b_state = client.get("/projects/Surency Platform/auth", headers=headers_b)
    assert b_state.status_code == 200
    assert b_state.json()["exists"] is False

    # A is unaffected by anything B does.
    assert client.delete("/projects/Surency Platform/auth", headers=headers_b).status_code in (
        200,
        204,
    )
    assert client.get("/projects/Surency Platform/auth", headers=headers_a).status_code == 200


def test_project_list_is_scoped_to_owner(client, db_session, two_users):
    from app.models.project import Project

    user_a, _ = two_users["a"]
    _, headers_b = two_users["b"]

    db_session.add(
        Project(provider_kind="ado", external_id="p1", name="A's Project", owner_id=user_a.id)
    )
    db_session.commit()

    resp = client.get("/projects", headers=headers_b)
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------- tickets
def test_ticket_sync_and_list_are_scoped_to_owner(client, db_session, two_users):
    import httpx
    import respx

    user_a, headers_a = two_users["a"]
    _, headers_b = two_users["b"]

    conn = client.post(
        "/providers/ado/connections", json={"name": "A's ADO"}, headers=headers_a
    ).json()
    client.put(
        f"/connections/{conn['id']}",
        json={
            "config": {"orgUrl": "https://dev.azure.com/myorg", "project": "MyProj"},
            "secrets": {"pat": "secret-pat"},
        },
        headers=headers_a,
    )

    with respx.mock:
        respx.post("https://dev.azure.com/myorg/MyProj/_apis/wit/wiql").mock(
            return_value=httpx.Response(200, json={"workItems": [{"id": 101}]})
        )
        respx.get("https://dev.azure.com/myorg/_apis/wit/workitems").mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": 101,
                            "fields": {
                                "System.Title": "A's ticket",
                                "System.WorkItemType": "User Story",
                                "System.State": "Ready for QA",
                                "Microsoft.VSTS.Common.Priority": 1,
                            },
                            "relations": [],
                        }
                    ]
                },
            )
        )
        respx.get("https://dev.azure.com/myorg/_apis/wit/workItems/101/comments").mock(
            return_value=httpx.Response(200, json={"comments": []})
        )
        resp = client.post(
            "/tickets/sync",
            json={"connectionId": conn["id"], "mode": "sprint", "sprint": "Sprint 12"},
            headers=headers_a,
        )
    assert resp.status_code == 200
    assert resp.json()["synced"] == 1

    from app.models.ticket import Ticket

    ticket = db_session.query(Ticket).filter_by(external_id="101").one()
    assert ticket.owner_id == user_a.id

    # B's connection-less sync request 404s (no work-item connection of B's own).
    assert (
        client.post(
            "/tickets/sync", json={"connectionId": conn["id"], "mode": "sprint"}, headers=headers_b
        ).status_code
        == 404
    )

    # B's ticket list doesn't include A's synced ticket.
    page_b = client.get("/tickets", headers=headers_b).json()
    assert page_b["total"] == 0

    # A's own list/detail views still see it.
    page_a = client.get("/tickets", headers=headers_a).json()
    assert page_a["total"] == 1
    assert client.get("/tickets/101", headers=headers_a).status_code == 200
    assert client.get("/tickets/101", headers=headers_b).status_code == 404


# ------------------------------------------------------- bridge stays green
def test_bridge_stays_green_without_auth(client, db_session):
    """With auth disabled (the suite default), scoping helpers stay no-ops."""
    conn = client.post("/providers/ado/connections", json={"name": "unauthed"}).json()
    assert db_session.get(ProviderConnection, conn["id"]).owner_id is None

    resp = client.put(
        "/projects/Unauthed Project/config", json={"baseUrl": "https://x.test"}
    )
    assert resp.status_code == 200

    row = db_session.query(ProjectConfig).filter_by(key="Unauthed Project").one()
    assert row.owner_id is None
    assert client.get("/projects/Unauthed Project/config").status_code == 200
