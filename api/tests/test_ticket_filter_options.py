"""``GET /tickets/filter-options`` — the query builder's dropdown source (#517).

The endpoint exists because the two richer sources are both unavailable on the
screen that needs them: EmeHub's ``/work-item-metadata`` is hub-audience only,
and our own calls a provider adapter that a mirrored hub connection cannot reach
(no PAT, by design — #501/#514). So the values are a ``SELECT DISTINCT`` over the
caller's own rows.

Two properties are load-bearing and get most of the tests here:

* **Owner scoping.** A picker is a read of someone's data. Offering another
  user's sprint names or colleagues leaks the shape of their work even though no
  ticket of theirs is ever returned.
* **No provider and no hub call.** It must answer with the connection
  uncredentialled and the hub down, because that is precisely the situation it
  was added for.
"""

from __future__ import annotations

import pytest

from app.models.provider_connection import ProviderConnection
from app.models.ticket import Ticket
from app.models.user import User
from app.services import auth_service


def _user(db, email="options@example.com") -> User:
    user = User(
        email=email,
        password_hash=auth_service.hash_password("password123"),
        first_name="Op",
        last_name="Tions",
        role="member",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _ticket(db, **kwargs) -> Ticket:
    defaults = dict(
        external_id="SUR-1",
        provider_kind="ado",
        title="A ticket",
        work_item_type="User Story",
        status="Ready for QA",
        priority="High",
        assignee="Maya Kaur",
        sprint="Sprint 12",
        area_path="Surency\\Platform",
        epic="EPIC-1",
        labels=["regression"],
    )
    defaults.update(kwargs)
    ticket = Ticket(**defaults)
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


def _get(client, **params):
    resp = client.get("/tickets/filter-options", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ----------------------------------------------------------------- the values
def test_returns_distinct_sorted_values_from_the_callers_rows(client, db_session):
    _ticket(db_session, external_id="SUR-1", assignee="Zoe Ray", sprint="Sprint 12")
    _ticket(
        db_session,
        external_id="SUR-2",
        work_item_type="Bug",
        status="Blocked",
        priority="Low",
        assignee="Ada Lovelace",
        sprint="Sprint 12",
        area_path="Surency\\Data",
        epic="EPIC-2",
        labels=["smoke", "regression"],
    )

    body = _get(client)

    assert body["workItemTypes"] == ["Bug", "User Story"]
    assert body["states"] == ["Blocked", "Ready for QA"]
    assert body["assignees"] == ["Ada Lovelace", "Zoe Ray"]
    # De-duplicated: both rows are in Sprint 12.
    assert body["sprints"] == ["Sprint 12"]
    assert body["areaPaths"] == ["Surency\\Data", "Surency\\Platform"]
    assert body["epics"] == ["EPIC-1", "EPIC-2"]
    assert body["priorities"] == ["High", "Low"]
    assert body["labels"] == ["regression", "smoke"]
    assert body["ticketCount"] == 2


def test_blank_values_are_never_offered(client, db_session):
    """An empty column is "unset", not a filter value that matches nothing."""
    _ticket(db_session, external_id="SUR-3", epic="", sprint="   ", labels=[])

    body = _get(client)

    assert body["epics"] == []
    assert body["sprints"] == []
    assert body["labels"] == []


def test_no_tickets_yields_empty_lists_not_an_error(client):
    body = _get(client)

    assert body["ticketCount"] == 0
    assert body["states"] == []
    assert body["hubManaged"] is False


# ------------------------------------------------------------------- scoping
def test_scoped_to_the_named_connection(client, db_session):
    _ticket(db_session, external_id="SUR-4", connection_id=1, sprint="Sprint A")
    _ticket(db_session, external_id="SUR-5", connection_id=2, sprint="Sprint B")

    assert _get(client, connectionId=1)["sprints"] == ["Sprint A"]
    assert _get(client, connectionId=2)["sprints"] == ["Sprint B"]


def test_scoped_to_the_named_provider_kind(client, db_session):
    _ticket(db_session, external_id="SUR-6", provider_kind="ado", sprint="Sprint A")
    _ticket(db_session, external_id="JIRA-1", provider_kind="jira", sprint="Sprint B")

    assert _get(client, providerKind="jira")["sprints"] == ["Sprint B"]


# ------------------------------------------------------------- owner scoping
def test_one_users_values_never_leak_into_anothers_options(client, db_session, monkeypatch):
    """The whole point of the endpoint being owner-scoped, pinned.

    ``auth_required`` is flipped on so the request carries a real identity —
    with the suite default (``False``) ``current_user`` is ``None`` and
    :func:`owned` is a passthrough, which would make this test pass without
    exercising any scoping at all.
    """
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "auth_required", True)

    alice = _user(db_session, email="alice-options@example.com")
    bob = _user(db_session, email="bob-options@example.com")
    _ticket(
        db_session,
        external_id="ALICE-1",
        owner_id=alice.id,
        assignee="Alice Only",
        sprint="Alice Sprint",
        epic="ALICE-EPIC",
        labels=["alice-label"],
    )
    _ticket(
        db_session,
        external_id="BOB-1",
        owner_id=bob.id,
        assignee="Bob Only",
        sprint="Bob Sprint",
        epic="BOB-EPIC",
        labels=["bob-label"],
    )

    def options_for(user: User) -> dict:
        token = auth_service.create_access_token(user, sid=f"sid-{user.id}")
        resp = client.get(
            "/tickets/filter-options", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    alice_options = options_for(alice)
    bob_options = options_for(bob)

    assert alice_options["assignees"] == ["Alice Only"]
    assert alice_options["sprints"] == ["Alice Sprint"]
    assert alice_options["epics"] == ["ALICE-EPIC"]
    assert alice_options["labels"] == ["alice-label"]
    assert alice_options["ticketCount"] == 1

    assert bob_options["assignees"] == ["Bob Only"]
    assert bob_options["sprints"] == ["Bob Sprint"]
    assert bob_options["epics"] == ["BOB-EPIC"]
    assert bob_options["labels"] == ["bob-label"]
    assert bob_options["ticketCount"] == 1


# --------------------------------------------------------------- hub-managed
def test_hub_managed_is_false_for_ordinary_local_tickets(client, db_session):
    _ticket(db_session, external_id="SUR-7")

    assert _get(client)["hubManaged"] is False


def test_hub_managed_is_true_when_a_row_carries_a_hub_ticket_id(client, db_session):
    _ticket(db_session, external_id="SUR-8", hub_ticket_id="hub-42")

    assert _get(client)["hubManaged"] is True


def test_hub_managed_is_true_for_a_mirrored_connection_with_no_tickets_yet(
    client, db_session
):
    """A mirrored connection has no PAT from the moment it exists, not from the
    moment its first ticket lands — so Sync is unusable before any row arrives."""
    conn = ProviderConnection(
        kind="ado", name="Mirrored", config={}, secrets={}, hub_connection_id="hub-conn-1"
    )
    db_session.add(conn)
    db_session.commit()
    db_session.refresh(conn)

    assert _get(client, connectionId=conn.id)["hubManaged"] is True


def test_hub_managed_is_false_for_a_locally_credentialled_connection(client, db_session):
    conn = ProviderConnection(
        kind="ado", name="Local", config={"project": "P"}, secrets={"pat": "x"}
    )
    db_session.add(conn)
    db_session.commit()
    db_session.refresh(conn)
    _ticket(db_session, external_id="SUR-9", connection_id=conn.id)

    assert _get(client, connectionId=conn.id)["hubManaged"] is False


# ------------------------------------------------------------------- routing
@pytest.mark.parametrize("path", ["/tickets/filter-options"])
def test_the_path_is_not_swallowed_by_the_ticket_detail_route(client, path):
    """``/{external_id}`` would happily claim this path if declared first — the
    symptom being a 404 for a ticket literally called "filter-options"."""
    resp = client.get(path)

    assert resp.status_code == 200
    assert "workItemTypes" in resp.json()


def test_hub_managed_ignores_another_users_connection(client, db_session, monkeypatch):
    """Probing someone else's connection id must not report their hub state.

    An unscoped lookup would leak a boolean about another user's setup, and would
    decide *this* user's Sync button from a row they cannot see.

    ``auth_required`` on, and the request carries a real token — with the suite
    default ``current_user`` is ``None`` and :func:`owned` is a passthrough, so
    this would pass without exercising any scoping.
    """
    import app.config as config_module
    from app.models.provider_connection import ProviderConnection

    monkeypatch.setattr(config_module.settings, "auth_required", True)

    mine = _user(db_session, email="mine-options@example.com")
    stranger = _user(db_session, email="stranger-options@example.com")
    theirs = ProviderConnection(
        owner_id=stranger.id, kind="ado", name="Theirs", hub_connection_id="hub-99", secrets={}
    )
    db_session.add(theirs)
    db_session.commit()

    token = auth_service.create_access_token(mine, sid=f"sid-{mine.id}")
    resp = client.get(
        f"/tickets/filter-options?connectionId={theirs.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["hubManaged"] is False
