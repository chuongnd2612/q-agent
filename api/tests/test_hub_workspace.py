"""Mirroring hub resources into a fresh SSO user's workspace (#514).

The bug this covers: a user who signs in through EmeHub gets a newly provisioned
local account, everything here is per-user (``owner_id``, ADR 0009), so they saw
"0 connected providers" and "No tickets found" while the hub held their
connection, projects and every ticket.

Two properties matter most and are asserted repeatedly below: mirroring is
**idempotent** (a second visit must not duplicate anything), and it is
**additive** (a user's own local rows are never clobbered or deleted).
"""

from __future__ import annotations

import re

import httpx
import pytest
import respx

from app.models.project import Project
from app.models.provider_connection import ProviderConnection
from app.models.ticket import Ticket
from app.models.user import User
from app.services import hub_workspace

HUB = "https://hub.example.test/api"


@pytest.fixture
def hub_on(monkeypatch, workspace_dir):
    """Flags on, applied AFTER ``workspace_dir`` — it rebuilds ``settings`` in
    place, so patching before it is silently undone."""
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "hub_sso_enabled", True)
    monkeypatch.setattr(config_module.settings, "hub_data_enabled", True)
    monkeypatch.setattr(config_module.settings, "hub_base_url", HUB)
    return config_module.settings


@pytest.fixture
def sso_user(db_session) -> User:
    """A freshly JIT-provisioned SSO account: owns nothing."""
    user = User(email="duna.nguyen@emesoft.net", password_hash="", hub_user_id="1")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _connections(**over):
    row = {
        "id": 3,
        "kind": "azure_devops",
        "label": "Surency",
        "baseUrl": "https://dev.azure.com/DDKS",
        "config": {"project": "Surency"},
        "capabilities": ["work_item", "repository"],
        "connected": True,
        "hasPat": True,
    }
    row.update(over)
    return [row]


def _tickets(**over):
    item = {
        "id": "hub-202",
        "externalId": "1442",
        "providerKind": "azure_devops",
        "connectionId": 3,
        "title": "Regression Test - Job Orchestration",
        "workItemType": "User Story",
        "status": "In Progress",
        "assignee": "Duna Nguyen",
        "sprint": "Sprint 9",
        "epic": "Orchestration",
        "priority": "High",
        "areaPath": "Surency\\Platform",
        "labels": ["qa"],
        "acCount": 3,
    }
    item.update(over)
    return {"items": [item], "total": 1}


def _projects():
    return [{"id": 3, "key": "surency", "name": "Surency", "shared": False,
             "summary": {"repo": "surency-admin-hub", "branch": "main"}}]


def _mock_hub(tickets=None, connections=None, projects=None):
    """Stub the three hub reads and RETURN the tickets route.

    Returning it matters: respx matches routes in registration order, so a test
    that calls `respx.get(...)` again just adds a second route the first one
    shadows — the hub keeps answering with the original payload and a
    "hub is now empty" scenario silently never happens. Re-mock this route object
    instead.
    """
    respx.get(url__startswith=f"{HUB}/connections").mock(
        return_value=httpx.Response(200, json=connections if connections is not None else _connections())
    )
    tickets_route = respx.get(url__regex=rf"{re.escape(HUB)}/tickets(\?.*)?$").mock(
        return_value=httpx.Response(200, json=tickets if tickets is not None else _tickets())
    )
    respx.get(url__startswith=f"{HUB}/projects").mock(
        return_value=httpx.Response(200, json=projects if projects is not None else _projects())
    )
    return tickets_route


# ---------------------------------------------------------------- the fix
@respx.mock
def test_fresh_sso_user_gets_hub_resources(hub_on, db_session, sso_user):
    _mock_hub()

    summary = hub_workspace.ensure_for_user(db_session, sso_user, "tok")

    assert summary == {"connections": 1, "tickets": 1, "projects": 1}
    conn = db_session.query(ProviderConnection).filter_by(owner_id=sso_user.id).one()
    assert conn.hub_connection_id == "3"
    assert conn.kind == "ado"  # translated from the hub's `azure_devops` (#507)
    ticket = db_session.query(Ticket).filter_by(owner_id=sso_user.id).one()
    assert ticket.hub_ticket_id == "hub-202"
    assert ticket.external_id == "1442"
    assert ticket.connection_id == conn.id  # hangs off the mirrored connection
    assert db_session.query(Project).filter_by(owner_id=sso_user.id).count() == 1


@respx.mock
def test_mirrored_connection_carries_no_secrets(hub_on, db_session, sso_user):
    """The PAT never crosses (#501) — a mirrored row must never look credentialed."""
    _mock_hub()

    hub_workspace.ensure_for_user(db_session, sso_user, "tok")

    conn = db_session.query(ProviderConnection).filter_by(owner_id=sso_user.id).one()
    assert conn.secrets == {}
    assert conn.is_hub_backed is True


@respx.mock
def test_mirroring_is_idempotent(hub_on, db_session, sso_user):
    """A second visit must refresh in place, not duplicate."""
    _mock_hub()

    hub_workspace.ensure_for_user(db_session, sso_user, "tok")
    second = hub_workspace.ensure_for_user(db_session, sso_user, "tok")

    assert second["tickets"] == 0  # nothing newly created
    assert db_session.query(ProviderConnection).filter_by(owner_id=sso_user.id).count() == 1
    assert db_session.query(Ticket).filter_by(owner_id=sso_user.id).count() == 1
    assert db_session.query(Project).filter_by(owner_id=sso_user.id).count() == 1


@respx.mock
def test_updates_existing_mirror_in_place(hub_on, db_session, sso_user):
    tickets_route = _mock_hub()
    hub_workspace.ensure_for_user(db_session, sso_user, "tok")

    tickets_route.mock(
        return_value=httpx.Response(200, json=_tickets(title="Renamed at the hub", status="Done"))
    )
    hub_workspace.ensure_for_user(db_session, sso_user, "tok")

    ticket = db_session.query(Ticket).filter_by(owner_id=sso_user.id).one()
    assert ticket.title == "Renamed at the hub"
    assert ticket.status == "Done"


# ---------------------------------------------------------------- additive only
@respx.mock
def test_another_users_rows_are_untouched(hub_on, db_session, sso_user):
    """Mirroring is per-caller: it must not touch anyone else's data."""
    other = User(email="gracie.dong@emesoft.net", password_hash="")
    db_session.add(other)
    db_session.commit()
    db_session.add(Ticket(owner_id=other.id, external_id="OTHER-1", provider_kind="ado", title="Theirs"))
    db_session.commit()
    _mock_hub()

    hub_workspace.ensure_for_user(db_session, sso_user, "tok")

    theirs = db_session.query(Ticket).filter_by(owner_id=other.id).one()
    assert theirs.title == "Theirs"
    assert theirs.hub_ticket_id is None


@respx.mock
def test_local_connection_is_left_alone(hub_on, db_session, sso_user):
    """A connection the user created themselves keeps its credential."""
    local = ProviderConnection(
        owner_id=sso_user.id, kind="jira", name="My own Jira", secrets={"pat": "encrypted"}
    )
    db_session.add(local)
    db_session.commit()
    _mock_hub()

    hub_workspace.ensure_for_user(db_session, sso_user, "tok")

    db_session.refresh(local)
    assert local.secrets == {"pat": "encrypted"}
    assert local.hub_connection_id is None
    assert db_session.query(ProviderConnection).filter_by(owner_id=sso_user.id).count() == 2


# ---------------------------------------------------------------- degradation
@respx.mock
def test_hub_unavailable_leaves_the_workspace_alone(hub_on, db_session, sso_user):
    respx.get(f"{HUB}/connections").mock(side_effect=httpx.ConnectError("refused"))

    summary = hub_workspace.ensure_for_user(db_session, sso_user, "tok")

    assert summary == {"connections": 0, "tickets": 0, "projects": 0}
    assert db_session.query(Ticket).filter_by(owner_id=sso_user.id).count() == 0


@respx.mock
def test_hub_401_is_not_an_error(hub_on, db_session, sso_user):
    respx.get(f"{HUB}/connections").mock(return_value=httpx.Response(401))

    assert hub_workspace.ensure_for_user(db_session, sso_user, "tok") == {
        "connections": 0, "tickets": 0, "projects": 0
    }


def test_flag_off_does_nothing(db_session, sso_user, monkeypatch, workspace_dir):
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "hub_data_enabled", False)

    with respx.mock:
        route = respx.get(f"{HUB}/connections").mock(return_value=httpx.Response(200, json=[]))
        hub_workspace.ensure_for_user(db_session, sso_user, "tok")

    assert not route.called
    assert db_session.query(Ticket).filter_by(owner_id=sso_user.id).count() == 0


def test_no_hub_token_does_nothing(hub_on, db_session, sso_user):
    assert hub_workspace.ensure_for_user(db_session, sso_user, None) == {
        "connections": 0, "tickets": 0, "projects": 0
    }


def test_anonymous_caller_does_nothing(hub_on, db_session):
    assert hub_workspace.ensure_for_user(db_session, None, "tok") == {
        "connections": 0, "tickets": 0, "projects": 0
    }


# ---------------------------------------------------------------- detail fill
@respx.mock
def test_detail_fetch_fills_description_and_ac(hub_on, db_session, sso_user):
    _mock_hub()
    hub_workspace.ensure_for_user(db_session, sso_user, "tok")
    ticket = db_session.query(Ticket).filter_by(owner_id=sso_user.id).one()
    assert ticket.description == ""

    respx.get(f"{HUB}/tickets/1442").mock(return_value=httpx.Response(200, json={
        "externalId": "1442", "description": "The full description",
        "acceptanceCriteria": ["AC one", "AC two"], "acceptanceCriteriaHtml": "<ul></ul>",
        "comments": [{"body": "hi"}],
    }))

    filled = hub_workspace.fill_ticket_detail(db_session, ticket, "tok")

    assert filled.description == "The full description"
    assert filled.acceptance_criteria == ["AC one", "AC two"]
    assert filled.comments == [{"body": "hi"}]


@respx.mock
def test_detail_fetch_never_clobbers_local_edits(hub_on, db_session, sso_user):
    _mock_hub()
    hub_workspace.ensure_for_user(db_session, sso_user, "tok")
    ticket = db_session.query(Ticket).filter_by(owner_id=sso_user.id).one()
    ticket.description = "Edited here"
    ticket.acceptance_criteria = ["mine"]
    db_session.commit()

    route = respx.get(f"{HUB}/tickets/1442").mock(
        return_value=httpx.Response(200, json={"description": "from hub", "acceptanceCriteria": ["hub"]})
    )
    filled = hub_workspace.fill_ticket_detail(db_session, ticket, "tok")

    assert filled.description == "Edited here"
    assert filled.acceptance_criteria == ["mine"]
    assert not route.called  # already complete — no pointless round trip


@respx.mock
def test_detail_fetch_survives_a_hub_failure(hub_on, db_session, sso_user):
    _mock_hub()
    hub_workspace.ensure_for_user(db_session, sso_user, "tok")
    ticket = db_session.query(Ticket).filter_by(owner_id=sso_user.id).one()
    respx.get(f"{HUB}/tickets/1442").mock(side_effect=httpx.ConnectError("refused"))

    assert hub_workspace.fill_ticket_detail(db_session, ticket, "tok").external_id == "1442"


# ---------------------------------------------------------------- #522 pruning
# Mirroring was create-or-update only, so a ticket deleted at the hub lived on in
# Q-Agent forever. Pruning is guarded three ways, because a careless prune is far
# worse than a stale row.
@respx.mock
def test_tickets_deleted_at_the_hub_are_pruned(hub_on, db_session, sso_user):
    tickets_route = _mock_hub()
    hub_workspace.ensure_for_user(db_session, sso_user, "tok")
    assert db_session.query(Ticket).filter_by(owner_id=sso_user.id).count() == 1

    # The hub is emptied — a COMPLETE read returning zero.
    tickets_route.mock(return_value=httpx.Response(200, json={"items": [], "total": 0}))
    hub_workspace.ensure_for_user(db_session, sso_user, "tok")

    assert db_session.query(Ticket).filter_by(owner_id=sso_user.id).count() == 0


@respx.mock
def test_locally_created_tickets_are_never_pruned(hub_on, db_session, sso_user):
    """Only rows carrying `hub_ticket_id` were ever ours to remove."""
    db_session.add(
        Ticket(owner_id=sso_user.id, external_id="MINE-1", provider_kind="ado", title="Mine")
    )
    db_session.commit()
    tickets_route = _mock_hub()
    hub_workspace.ensure_for_user(db_session, sso_user, "tok")

    tickets_route.mock(return_value=httpx.Response(200, json={"items": [], "total": 0}))
    hub_workspace.ensure_for_user(db_session, sso_user, "tok")

    remaining = db_session.query(Ticket).filter_by(owner_id=sso_user.id).all()
    assert [t.external_id for t in remaining] == ["MINE-1"]


@respx.mock
def test_a_failed_hub_read_prunes_nothing(hub_on, db_session, sso_user):
    """A transient hub error must never be read as 'the hub is empty now'."""
    tickets_route = _mock_hub()
    hub_workspace.ensure_for_user(db_session, sso_user, "tok")

    tickets_route.mock(side_effect=httpx.ConnectError("refused"))
    hub_workspace.ensure_for_user(db_session, sso_user, "tok")

    assert db_session.query(Ticket).filter_by(owner_id=sso_user.id).count() == 1


@respx.mock
def test_a_partial_read_prunes_nothing(hub_on, db_session, sso_user):
    """A page-capped or malformed walk looks like a short one — it must not delete."""
    tickets_route = _mock_hub()
    hub_workspace.ensure_for_user(db_session, sso_user, "tok")

    def _serve(request):
        page = int(dict(request.url.params).get("page", 1))
        if page == 1:
            return httpx.Response(200, json={"items": [_tickets()["items"][0]] * 200, "total": 400})
        return httpx.Response(200, json={"items": "not-a-list", "total": 400})

    tickets_route.mock(side_effect=_serve)
    hub_workspace.ensure_for_user(db_session, sso_user, "tok")

    assert db_session.query(Ticket).filter_by(owner_id=sso_user.id).count() >= 1


@respx.mock
def test_a_ticket_referenced_by_a_run_is_kept(hub_on, db_session, sso_user):
    """No FK protects this: run_tickets.ticket_external_id is a plain string, so
    deleting would silently orphan run history rather than fail."""
    from app.models.run import Run, RunTicket

    tickets_route = _mock_hub()
    hub_workspace.ensure_for_user(db_session, sso_user, "tok")
    run = Run(code="RUN-901", name="Regression sweep", owner_id=sso_user.id)
    db_session.add(run)
    db_session.commit()
    db_session.add(RunTicket(run_id=run.id, ticket_external_id="1442"))
    db_session.commit()

    tickets_route.mock(return_value=httpx.Response(200, json={"items": [], "total": 0}))
    hub_workspace.ensure_for_user(db_session, sso_user, "tok")

    kept = db_session.query(Ticket).filter_by(owner_id=sso_user.id).one()
    assert kept.external_id == "1442"
