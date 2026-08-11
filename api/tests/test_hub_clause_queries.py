"""Clause queries proxied to EmeHub — search / preview / sync / saved (#519).

The behaviour that matters here is **which failures are swallowed and which are
reported**, because the two kinds of call differ:

* ``search`` and ``preview`` are reads. A hub outage degrades to
  ``{"available": false}`` so the Tickets screen falls back to filtering local
  rows instead of showing an error (#491).
* ``sync`` **writes**. A user who pressed it is owed an answer, so a failure is
  raised rather than swallowed.

And in both cases a **rejected query** is an authoritative answer, never a
fallback: the hub refuses an unrunnable clause instead of dropping it, precisely
so the user can be told which condition is at fault. A dropped filter returns
*more* work items than were asked for — the failure a caller is least likely to
notice.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

HUB = "https://hub.example.test/api"
QUERY = {"clauses": [{"field": "state", "operator": "is", "values": ["Active"]}], "match": "all"}


@pytest.fixture
def hub_on(monkeypatch, workspace_dir):
    """Flags on, applied AFTER ``workspace_dir`` — it rebuilds ``settings`` in
    place, so patches applied before it are silently wiped."""
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "hub_sso_enabled", True)
    monkeypatch.setattr(config_module.settings, "hub_data_enabled", True)
    monkeypatch.setattr(config_module.settings, "hub_base_url", HUB)
    monkeypatch.setattr(config_module.settings, "hub_internal_base_url", "")
    return config_module.settings


HDR = {"X-Hub-Token": "tok"}


# ---------------------------------------------------------------- search
@respx.mock
def test_search_passes_the_query_through_untouched(hub_on, client):
    """The hub validates the clause model; re-encoding it here would put a
    second, weaker validator in front of the one that matters."""
    captured = {}

    def _record(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"items": [], "total": 0, "page": 1, "pageSize": 50})

    respx.post(f"{HUB}/tickets/search").mock(side_effect=_record)

    res = client.post("/tickets/hub/search", json={"query": QUERY}, headers=HDR)

    assert res.status_code == 200
    assert res.json()["available"] is True
    assert captured["body"]["query"] == QUERY


@respx.mock
def test_search_returns_hub_results(hub_on, client):
    respx.post(f"{HUB}/tickets/search").mock(
        return_value=httpx.Response(
            200, json={"items": [{"externalId": "1442"}], "total": 1, "page": 1, "pageSize": 50}
        )
    )

    body = client.post("/tickets/hub/search", json={"query": QUERY}, headers=HDR).json()

    assert body["total"] == 1
    assert body["items"][0]["externalId"] == "1442"


@respx.mock
def test_search_degrades_when_the_hub_is_down(hub_on, client):
    """Not an error: the caller falls back to filtering local rows."""
    respx.post(f"{HUB}/tickets/search").mock(side_effect=httpx.ConnectError("refused"))

    res = client.post("/tickets/hub/search", json={"query": QUERY}, headers=HDR)

    assert res.status_code == 200
    assert res.json() == {"available": False}


def test_search_without_a_hub_token_is_unavailable(hub_on, client):
    assert client.post("/tickets/hub/search", json={"query": QUERY}).json() == {"available": False}


@respx.mock
def test_search_reports_a_rejected_clause_rather_than_falling_back(hub_on, client):
    """422 {problems:[{message, clauseIndex}]} is an ANSWER — the user must see
    which condition the destination cannot run, not a silent local fallback."""
    respx.post(f"{HUB}/tickets/search").mock(
        return_value=httpx.Response(
            422, json={"problems": [{"message": "Jira has no areaPath", "clauseIndex": 1}]}
        )
    )

    res = client.post("/tickets/hub/search", json={"query": QUERY}, headers=HDR)

    assert res.status_code == 422
    assert "areaPath" in res.json()["detail"]
    # 1-based for humans: clauseIndex 1 is the second condition.
    assert "condition 2" in res.json()["detail"]


# ---------------------------------------------------------------- preview
@respx.mock
def test_preview_returns_the_provider_total(hub_on, client):
    """Preview asks the PROVIDER, not the hub's mirror — measured live, it
    returned 51 while the mirror held 0."""
    respx.post(f"{HUB}/tickets/query/preview").mock(
        return_value=httpx.Response(200, json={"total": 51, "sample": [{"externalId": "1742"}]})
    )

    body = client.post(
        "/tickets/hub/preview", json={"query": QUERY, "providerKind": "azure_devops"}, headers=HDR
    ).json()

    assert body["available"] is True
    assert body["total"] == 51


@respx.mock
def test_preview_degrades_when_the_hub_is_down(hub_on, client):
    respx.post(f"{HUB}/tickets/query/preview").mock(return_value=httpx.Response(503))

    assert client.post("/tickets/hub/preview", json={"query": QUERY}, headers=HDR).json() == {
        "available": False
    }


# ---------------------------------------------------------------- sync (writes)
@respx.mock
def test_sync_reports_failure_rather_than_swallowing_it(hub_on, client):
    """Unlike the reads: a user who pressed Sync is owed an answer."""
    respx.post(f"{HUB}/tickets/sync").mock(side_effect=httpx.ConnectError("refused"))

    res = client.post("/tickets/hub/sync", json={"query": QUERY}, headers=HDR)

    assert res.status_code == 503
    assert "EmeHub" in res.json()["detail"]


@respx.mock
def test_sync_surfaces_a_rejected_clause(hub_on, client):
    respx.post(f"{HUB}/tickets/sync").mock(
        return_value=httpx.Response(
            422, json={"problems": [{"message": "GitHub cannot do match=any", "clauseIndex": 0}]}
        )
    )

    res = client.post("/tickets/hub/sync", json={"query": QUERY}, headers=HDR)

    assert res.status_code == 422
    assert "GitHub" in res.json()["detail"]


def test_sync_is_refused_when_the_integration_is_off(client, monkeypatch, workspace_dir):
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "hub_data_enabled", False)

    res = client.post("/tickets/hub/sync", json={"query": QUERY}, headers=HDR)

    assert res.status_code == 409


@respx.mock
def test_sync_mirrors_afterwards_so_the_pull_is_usable(hub_on, client):
    """Pulled work must be selectable into a run without waiting for a page load."""
    respx.post(f"{HUB}/tickets/sync").mock(return_value=httpx.Response(200, json={"synced": 3}))
    respx.get(url__startswith=f"{HUB}/connections").mock(return_value=httpx.Response(200, json=[]))
    respx.get(url__regex=rf"{HUB}/tickets(\?.*)?$").mock(
        return_value=httpx.Response(200, json={"items": [], "total": 0})
    )
    respx.get(url__startswith=f"{HUB}/projects").mock(return_value=httpx.Response(200, json=[]))

    body = client.post("/tickets/hub/sync", json={"query": QUERY}, headers=HDR).json()

    assert body["hub"] == {"synced": 3}
    assert "mirrored" in body


# ---------------------------------------------------------------- saved queries
@respx.mock
def test_saved_queries_come_from_the_hub(hub_on, client):
    """Both apps then offer the SAME saved queries, instead of Q-Agent keeping a
    private browser-local copy that silently diverges."""
    respx.get(f"{HUB}/ticket-queries").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": 1, "name": "Mine — active now", "query": QUERY, "builtIn": True}],
        )
    )

    body = client.get("/tickets/hub/saved-queries", headers=HDR).json()

    assert body["available"] is True
    assert body["queries"][0]["name"] == "Mine — active now"


@respx.mock
def test_saved_queries_degrade_to_empty(hub_on, client):
    respx.get(f"{HUB}/ticket-queries").mock(side_effect=httpx.ConnectError("refused"))

    assert client.get("/tickets/hub/saved-queries", headers=HDR).json() == {
        "available": False,
        "queries": [],
    }


def test_hub_routes_are_not_swallowed_by_the_ticket_detail_route(hub_on, client):
    """`/tickets/{external_id}` must not capture `/tickets/hub/...`."""
    res = client.get("/tickets/hub/saved-queries", headers=HDR)

    assert res.status_code == 200
    assert "queries" in res.json()
