"""Ticket filter pickers for a hub-mirrored connection (#655).

Every picker on the Tickets screen was fed by a direct Azure DevOps call using
the connection's PAT — and a hub-mirrored connection has **permanently empty
secrets** by design, so Sprint / Area path / State / Work item type came back
empty while the rows on the same screen plainly carried those values. Proxying to
the hub is not available (both hub endpoints are hub-audience only), so the
fallback is the mirrored rows themselves.

The reported symptom was "the filters don't match", not "the dropdown is empty".
So a populated dropdown proves nothing on its own — the load-bearing test here is
:func:`test_every_offered_value_actually_filters_the_list`, which feeds each
offered value straight back into ``GET /tickets`` and asserts on the **returned
rows**.

The other thing worth protecting is the path *not* being changed: a locally
credentialed connection must keep getting the richer provider answer (sprints
with no ticket yet, the full area-path tree), which is what
:func:`test_credentialed_connection_still_gets_provider_sprints` and its metadata
twin pin.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.logging import logger
from app.models.provider_connection import ProviderConnection
from app.models.ticket import Ticket
from app.models.user import User
from app.services import auth_service, hub_client, hub_workspace

# The hub's dialect, verbatim, where it crosses the boundary: `azure_devops` (not
# our `ado`) and camelCase keys. #507 was caused by joining on the untranslated
# spelling, so the mirror is built here through the real translating code path
# rather than by hand-constructing the local row.
HUB_CONNECTION_ROWS = [
    {
        "id": "7",
        "kind": "azure_devops",
        "label": "Surency ADO",
        "baseUrl": "https://dev.azure.com/surency",
        "connected": True,
        "config": {"orgUrl": "https://dev.azure.com/surency", "project": "Surency"},
    }
]


@pytest.fixture
def hub_user(db_session) -> User:
    user = User(
        email="mirror@example.com",
        password_hash=auth_service.hash_password("password123"),
        first_name="Mira",
        last_name="Roth",
        role="member",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def mirrored_conn(db_session, hub_user, monkeypatch) -> ProviderConnection:
    """A real hub mirror: `hub_connection_id` set, `secrets` empty, kind `ado`."""
    monkeypatch.setattr(hub_client, "list_connections", lambda token: HUB_CONNECTION_ROWS)
    mirrored = hub_workspace.ensure_connections(db_session, hub_user, "hub-token")
    conn = mirrored["7"]
    # The premise of the whole bug, asserted rather than assumed.
    assert conn.kind == "ado"
    assert conn.hub_connection_id == "7"
    assert conn.secrets == {}
    return conn


@pytest.fixture
def log_lines():
    """Captured loguru messages.

    ``caplog`` is silent here: the app logs through loguru, which does not feed
    stdlib logging's handlers, so a caplog assertion would be unprovable in one
    direction and vacuous in the other.
    """
    lines: list[str] = []
    sink_id = logger.add(
        lambda message: lines.append(message.record["message"]), level="INFO"
    )
    try:
        yield lines
    finally:
        logger.remove(sink_id)


def _local_conn(client, kind: str = "ado") -> dict:
    resp = client.post(f"/providers/{kind}/connections", json={"name": "Local ADO"})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _ticket(db, conn_id: int, external_id: str, **kwargs) -> Ticket:
    defaults = dict(
        external_id=external_id,
        connection_id=conn_id,
        provider_kind="ado",
        hub_ticket_id=f"hub-{external_id}",
        title=f"Ticket {external_id}",
        work_item_type="User Story",
        status="Resolved",
        priority="2",
        assignee="Maya Kaur",
        sprint="Sprint 7",
        area_path="Surency\\Admin Hub",
        epic="EPIC-1",
        labels=["regression"],
    )
    defaults.update(kwargs)
    ticket = Ticket(**defaults)
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


@pytest.fixture
def mirrored_tickets(db_session, mirrored_conn) -> ProviderConnection:
    """Two sprints / states / types / areas / priorities, so a filter can discriminate."""
    _ticket(db_session, mirrored_conn.id, "SUR-1446")
    _ticket(db_session, mirrored_conn.id, "SUR-1590")
    _ticket(
        db_session,
        mirrored_conn.id,
        "SUR-1601",
        work_item_type="Bug",
        status="Active",
        priority="1",
        sprint="Sprint 5",
        area_path="Surency\\Data Platform",
        epic="EPIC-2",
    )
    return mirrored_conn


# --------------------------------------------------- the pickers are populated
def test_mirrored_connection_sprint_picker_lists_the_mirrored_sprints(
    client, mirrored_tickets
):
    resp = client.get(f"/connections/{mirrored_tickets.id}/sprints")
    assert resp.status_code == 200, resp.text
    sprints = resp.json()

    assert [s["name"] for s in sprints] == ["Sprint 5", "Sprint 7"]
    # `path` is the value the filter submits, so it must be the row value
    # verbatim — a synthesised `Project\Sprint` would fill the dropdown and then
    # match nothing.
    assert [s["path"] for s in sprints] == ["Sprint 5", "Sprint 7"]


def test_mirrored_connection_metadata_populates_from_the_mirrored_rows(
    client, mirrored_tickets
):
    resp = client.get(f"/connections/{mirrored_tickets.id}/work-item-metadata")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["states"] == ["Active", "Resolved"]
    assert body["workItemTypes"] == ["Bug", "User Story"]
    assert [a["path"] for a in body["areaPaths"]] == [
        "Surency\\Admin Hub",
        "Surency\\Data Platform",
    ]
    # The label is the leaf; the submitted value stays the full path.
    assert [a["name"] for a in body["areaPaths"]] == ["Admin Hub", "Data Platform"]
    assert [e["key"] for e in body["epics"]] == ["EPIC-1", "EPIC-2"]


def test_priorities_are_offered_as_the_rows_spell_them(client, mirrored_tickets):
    """ADO priorities are `1`/`2`, not `High`/`Medium` — the reported mismatch."""
    body = client.get(
        "/tickets/filter-options", params={"connectionId": mirrored_tickets.id}
    ).json()
    assert body["priorities"] == ["1", "2"]


# ------------------------------------------------ THE acceptance criterion:
# selecting a filter actually filters the list (asserted on the ROWS).
def test_every_offered_value_actually_filters_the_list(client, mirrored_tickets):
    """Feed each offered value back into ``GET /tickets`` and check the rows.

    A populated dropdown does not close this bug — the report was "the filters
    don't match". Each assertion below is a *partition*: the filter must return
    the matching rows and must not return the others, so it fails both if the
    value is unusable (zero rows) and if the filter silently no-ops (all rows).
    """
    conn_id = mirrored_tickets.id

    def ids(**params) -> list[str]:
        params["connectionId"] = conn_id
        resp = client.get("/tickets", params=params)
        assert resp.status_code == 200, resp.text
        return sorted(t["externalId"] for t in resp.json()["items"])

    all_ids = ids()
    assert all_ids == ["SUR-1446", "SUR-1590", "SUR-1601"]

    # Sprint — the picker's own `path` values, straight from the endpoint.
    sprints = client.get(f"/connections/{conn_id}/sprints").json()
    by_name = {s["name"]: s["path"] for s in sprints}
    assert ids(sprint=by_name["Sprint 7"]) == ["SUR-1446", "SUR-1590"]
    assert ids(sprint=by_name["Sprint 5"]) == ["SUR-1601"]

    # Area path / State / Work item type — the metadata endpoint's own values.
    meta = client.get(f"/connections/{conn_id}/work-item-metadata").json()
    areas = {a["name"]: a["path"] for a in meta["areaPaths"]}
    assert ids(areaPath=areas["Admin Hub"]) == ["SUR-1446", "SUR-1590"]
    assert ids(areaPath=areas["Data Platform"]) == ["SUR-1601"]

    assert "Resolved" in meta["states"] and "Active" in meta["states"]
    assert ids(states="Resolved") == ["SUR-1446", "SUR-1590"]
    assert ids(states="Active") == ["SUR-1601"]

    assert "User Story" in meta["workItemTypes"] and "Bug" in meta["workItemTypes"]
    assert ids(workItemTypes="User Story") == ["SUR-1446", "SUR-1590"]
    assert ids(workItemTypes="Bug") == ["SUR-1601"]

    # Priority comes from /tickets/filter-options (the same derivation).
    priorities = client.get(
        "/tickets/filter-options", params={"connectionId": conn_id}
    ).json()["priorities"]
    assert priorities == ["1", "2"]
    assert ids(priority="2") == ["SUR-1446", "SUR-1590"]
    assert ids(priority="1") == ["SUR-1601"]


# ------------------------------------------- the credentialed path is unchanged
@respx.mock
def test_credentialed_connection_still_gets_provider_sprints(client, db_session):
    """Richer than the rows: a sprint that exists with no ticket must still show.

    This is the path most at risk of regressing, so it is pinned two ways — the
    provider-only sprint is present, and the ticket-only sprint is absent.
    """
    conn = _local_conn(client)
    client.put(
        f"/connections/{conn['id']}",
        json={
            "config": {"orgUrl": "https://dev.azure.com/myorg", "project": "MyProj"},
            "secrets": {"pat": "secret-pat"},
        },
    )
    # A ticket whose sprint the provider does NOT know about: if the fallback
    # leaked into the credentialed path, this name would appear.
    _ticket(db_session, conn["id"], "SUR-9", sprint="Ticket-Only Sprint")

    respx.get(
        "https://dev.azure.com/myorg/MyProj/_apis/wit/classificationnodes/iterations"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "MyProj",
                "path": "\\MyProj\\Iteration",
                "children": [
                    {
                        "id": 11,
                        "identifier": "it-11",
                        "name": "Empty Sprint",
                        "path": "\\MyProj\\Iteration\\Empty Sprint",
                        "attributes": {
                            "startDate": "2026-01-01T00:00:00Z",
                            "finishDate": "2026-01-14T00:00:00Z",
                        },
                    }
                ],
            },
        )
    )

    sprints = client.get(f"/connections/{conn['id']}/sprints").json()

    assert [s["name"] for s in sprints] == ["Empty Sprint"]
    assert sprints[0]["id"] == "it-11"
    assert sprints[0]["path"] == "MyProj\\Empty Sprint"  # provider iteration path
    assert sprints[0]["startDate"] == "2026-01-01T00:00:00Z"
    assert "Ticket-Only Sprint" not in [s["name"] for s in sprints]


@respx.mock
def test_credentialed_connection_still_gets_provider_metadata(client, db_session):
    conn = _local_conn(client)
    client.put(
        f"/connections/{conn['id']}",
        json={
            "config": {"orgUrl": "https://dev.azure.com/myorg", "project": "MyProj"},
            "secrets": {"pat": "secret-pat"},
        },
    )
    _ticket(db_session, conn["id"], "SUR-10", area_path="MyProj\\FromTicket")

    respx.get(
        "https://dev.azure.com/myorg/MyProj/_apis/wit/classificationnodes/areas"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "MyProj",
                "path": "\\MyProj\\Area",
                "children": [
                    {
                        "id": 21,
                        "identifier": "ar-21",
                        "name": "Web",
                        "path": "\\MyProj\\Area\\Web",
                        # The full tree — the reason this path is richer.
                        "children": [
                            {
                                "id": 22,
                                "identifier": "ar-22",
                                "name": "Checkout",
                                "path": "\\MyProj\\Area\\Web\\Checkout",
                            }
                        ],
                    }
                ],
            },
        )
    )
    respx.get("https://dev.azure.com/myorg/MyProj/_apis/wit/workitemtypes").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {"name": "Task", "states": [{"name": "New"}, {"name": "Done"}]},
                ]
            },
        )
    )

    body = client.get(f"/connections/{conn['id']}/work-item-metadata").json()

    assert [a["path"] for a in body["areaPaths"]] == [
        "MyProj\\Web",
        "MyProj\\Web\\Checkout",
    ]
    assert body["workItemTypes"] == ["Task"]
    assert body["states"] == ["Done", "New"]
    # The ticket's own values did not leak into the provider answer.
    assert "MyProj\\FromTicket" not in [a["path"] for a in body["areaPaths"]]


def test_a_hub_linked_connection_that_is_also_credentialed_uses_the_provider(
    client, db_session, mirrored_conn
):
    """The branch is decided on the credential, not on `hub_connection_id`.

    Give the mirror a local PAT and the provider is attempted again — here the
    call fails (no respx route), and the endpoint's existing resilient `[]` is
    the proof that the provider branch, not the fallback, ran: the fallback
    would have returned the ticket's sprint.
    """
    _ticket(db_session, mirrored_conn.id, "SUR-11", sprint="Sprint 7")
    resp = client.put(
        f"/connections/{mirrored_conn.id}",
        json={
            "config": {"orgUrl": "https://dev.azure.com/surency", "project": "Surency"},
            "secrets": {"pat": "a-real-local-pat"},
        },
    )
    assert resp.status_code == 200, resp.text

    assert client.get(f"/connections/{mirrored_conn.id}/sprints").json() == []


# ---------------------------------------------------- no credential, no tickets
def test_no_credential_and_no_tickets_returns_empty_without_erroring(
    client, mirrored_conn
):
    sprints = client.get(f"/connections/{mirrored_conn.id}/sprints")
    assert sprints.status_code == 200
    assert sprints.json() == []

    meta = client.get(f"/connections/{mirrored_conn.id}/work-item-metadata")
    assert meta.status_code == 200
    assert meta.json() == {
        "areaPaths": [],
        "workItemTypes": [],
        "states": [],
        "epics": [],
    }


# --------------------------------------------------------------- the tell-tale
def test_ticket_sourced_facets_log_a_tell_tale_naming_the_counts(
    client, mirrored_tickets, log_lines
):
    """A partial mirror yields a partial picker — silently, unless it is logged.

    "The picker is missing Sprint 8" has to be diagnosable, so the log names both
    the source (mirrored tickets, not the provider) and the counts.
    """
    client.get(f"/connections/{mirrored_tickets.id}/sprints")
    client.get(f"/connections/{mirrored_tickets.id}/work-item-metadata")

    text = "\n".join(log_lines)
    assert "served from mirrored tickets, not the provider" in text
    assert "Sprint list for connection" in text
    assert "Work-item metadata for connection" in text
    assert "tickets=3" in text
    assert "sprints=2" in text
    assert "area_paths=2" in text
    assert "states=2" in text
    assert "work_item_types=2" in text


def test_the_provider_path_logs_no_tell_tale(client, db_session, log_lines):
    """The tell-tale must mean what it says, so the other branch must not emit it."""
    conn = _local_conn(client)
    client.put(
        f"/connections/{conn['id']}",
        json={
            "config": {"orgUrl": "https://dev.azure.com/myorg", "project": "MyProj"},
            "secrets": {"pat": "secret-pat"},
        },
    )
    client.get(f"/connections/{conn['id']}/sprints")

    assert "served from mirrored tickets" not in "\n".join(log_lines)
