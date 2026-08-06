"""Hub data reads — C1 of #497 (`app.services.hub_client`, `app.deps_hub`).

The behaviour under test is mostly about **keeping failure kinds apart**: "the hub
says no", "the hub won't authorise this token" and "the hub isn't answering" lead
to different caller behaviour, and collapsing them is how a broken service comes
to look like an empty result (#491) or a spurious logout (#482).

The hub is stubbed with ``respx`` throughout — no test here touches a real hub.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.services import hub_client

HUB = "https://hub.example.test/api"


@pytest.fixture
def hub_on(monkeypatch):
    """Both flags on and a base URL set — the configured, enabled state."""
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "hub_sso_enabled", True)
    monkeypatch.setattr(config_module.settings, "hub_data_enabled", True)
    monkeypatch.setattr(config_module.settings, "hub_base_url", HUB)
    return config_module.settings


# ---------------------------------------------------------------- the flags
def test_disabled_by_default(workspace_dir):
    import app.config as config_module

    assert config_module.Settings().hub_data_enabled is False


def test_requires_both_flags(hub_on, monkeypatch):
    """Data without identity is a misconfiguration, not a mode."""
    import app.config as config_module

    assert hub_client.enabled() is True

    monkeypatch.setattr(config_module.settings, "hub_sso_enabled", False)
    assert hub_client.enabled() is False


def test_requires_a_base_url(hub_on, monkeypatch):
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "hub_base_url", "")
    assert hub_client.enabled() is False


@respx.mock
def test_disabled_makes_no_network_call(monkeypatch):
    """Off must mean *no outbound request*, not a request we ignore."""
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "hub_data_enabled", False)
    route = respx.get(f"{HUB}/tickets").mock(return_value=httpx.Response(200, json={}))

    with pytest.raises(hub_client.HubDisabledError):
        hub_client.list_tickets("some-token")

    assert not route.called


@respx.mock
def test_missing_token_is_unauthorized_not_a_crash(hub_on):
    route = respx.get(f"{HUB}/tickets").mock(return_value=httpx.Response(200, json={}))

    with pytest.raises(hub_client.HubUnauthorizedError):
        hub_client.list_tickets("")

    assert not route.called


# ---------------------------------------------------------------- happy paths
@respx.mock
def test_list_tickets(hub_on):
    payload = {"items": [{"id": 202, "externalId": "1442", "providerKind": "azure_devops"}], "total": 1}
    respx.get(f"{HUB}/tickets").mock(return_value=httpx.Response(200, json=payload))

    assert hub_client.list_tickets("tok")["total"] == 1


@respx.mock
def test_sends_bearer_token_and_does_not_leak_it(hub_on, caplog):
    captured = {}

    def _record(request):
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"items": [], "total": 0})

    respx.get(f"{HUB}/tickets").mock(side_effect=_record)

    with caplog.at_level("DEBUG"):
        hub_client.list_tickets("super-secret-token")

    assert captured["auth"] == "Bearer super-secret-token"
    # The token must never reach the logs, at any level.
    assert "super-secret-token" not in caplog.text


@respx.mock
def test_connections_carry_haspat_but_never_the_pat(hub_on):
    """Guards the boundary the whole design rests on (#501)."""
    payload = [{"id": 3, "kind": "azure_devops", "hasPat": True, "capabilities": ["work_item"]}]
    respx.get(f"{HUB}/connections").mock(return_value=httpx.Response(200, json=payload))

    row = hub_client.list_connections("tok")[0]

    assert row["hasPat"] is True
    assert not [k for k in row if any(w in k.lower() for w in ("pat", "secret", "password"))
                if k != "hasPat"]


@respx.mock
def test_resolve_claude_credential_passes_through_refreshable_status(hub_on):
    """`refreshable` is the common live value, not an error (#499, §4b)."""
    payload = {"source": "shared", "status": "refreshable", "credentials": "{...}",
               "expiresAt": "2026-08-05T12:00:00Z", "daysLeft": 0}
    respx.get(f"{HUB}/credentials/claude/resolve").mock(return_value=httpx.Response(200, json=payload))

    res = hub_client.resolve_claude_credential("tok")

    assert res["status"] == "refreshable"
    assert res["source"] == "shared"


# ------------------------------------------------------- failure kinds differ
@respx.mock
def test_401_is_unauthorized(hub_on):
    """Token expired or hub session revoked — callers fall back to local."""
    respx.get(f"{HUB}/tickets").mock(
        return_value=httpx.Response(401, json={"detail": "Session revoked or expired"})
    )

    with pytest.raises(hub_client.HubUnauthorizedError):
        hub_client.list_tickets("tok")


@respx.mock
def test_403_is_refused(hub_on):
    """An authoritative decline — distinct from 'unavailable', do not retry."""
    respx.get(f"{HUB}/tickets").mock(return_value=httpx.Response(403, json={"detail": "nope"}))

    with pytest.raises(hub_client.HubRefusedError) as exc:
        hub_client.list_tickets("tok")

    assert exc.value.status_code == 403
    assert "nope" in str(exc.value)


@respx.mock
@pytest.mark.parametrize("status", [502, 503, 504])
def test_gateway_errors_are_unavailable_not_refusals(hub_on, status):
    """Behind nginx + a tunnel, 'hub is down' arrives as a gateway code (#490)."""
    respx.get(f"{HUB}/tickets").mock(return_value=httpx.Response(status))

    with pytest.raises(hub_client.HubUnavailableError):
        hub_client.list_tickets("tok")


@respx.mock
def test_transport_failure_is_unavailable(hub_on):
    respx.get(f"{HUB}/tickets").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(hub_client.HubUnavailableError):
        hub_client.list_tickets("tok")


@respx.mock
def test_timeout_is_unavailable(hub_on):
    respx.get(f"{HUB}/tickets").mock(side_effect=httpx.ReadTimeout("too slow"))

    with pytest.raises(hub_client.HubUnavailableError):
        hub_client.list_tickets("tok")


@respx.mock
def test_html_200_is_unavailable_not_data(hub_on):
    """A wrong base URL (missing the hub's /api prefix) serves index.html at 200.

    Parsing that as data would be worse than failing — this is the #495 mistake
    one layer down.
    """
    respx.get(f"{HUB}/tickets").mock(
        return_value=httpx.Response(200, text="<!doctype html><html>…</html>")
    )

    with pytest.raises(hub_client.HubUnavailableError):
        hub_client.list_tickets("tok")


@respx.mock
def test_404_is_refused(hub_on):
    respx.get(f"{HUB}/tickets/9999").mock(return_value=httpx.Response(404, json={"detail": "gone"}))

    with pytest.raises(hub_client.HubRefusedError) as exc:
        hub_client.get_ticket("9999", "tok")

    assert exc.value.status_code == 404


# ---------------------------------------------------------------- deps_hub
def test_hub_token_dependency_reads_the_header():
    from app.deps_hub import hub_token

    assert hub_token("abc") == "abc"


def test_hub_token_dependency_treats_absent_as_none():
    """No hub token is an ordinary state — it must not raise."""
    from app.deps_hub import hub_token

    assert hub_token(None) is None
    assert hub_token("") is None
    assert hub_token("   ") is None


# ---------------------------------------------------------------- pagination
# The hub defaults to pageSize=25, so a caller that ignores page/pageSize mirrors
# only the first 25 tickets of however many exist — which is what shipped in #514
# before this was parameterised.
@respx.mock
def test_list_tickets_sends_pagination(hub_on):
    captured = {}

    def _record(request):
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"items": [], "total": 0})

    respx.get(url__startswith=f"{HUB}/tickets").mock(side_effect=_record)
    hub_client.list_tickets("tok", page=2, page_size=50)

    assert "page=2" in captured["url"]
    assert "pageSize=50" in captured["url"]


@respx.mock
def test_iter_all_tickets_walks_every_page(hub_on):
    pages = {
        1: {"items": [{"id": f"t{i}"} for i in range(200)], "total": 320},
        2: {"items": [{"id": f"t{i}"} for i in range(200, 320)], "total": 320},
    }

    def _serve(request):
        page = int(dict(request.url.params).get("page", 1))
        return httpx.Response(200, json=pages.get(page, {"items": [], "total": 320}))

    respx.get(url__startswith=f"{HUB}/tickets").mock(side_effect=_serve)

    assert len(hub_client.iter_all_tickets("tok")) == 320


@respx.mock
def test_iter_all_tickets_stops_on_a_short_page(hub_on):
    """A hub whose `total` disagrees with what it serves must still terminate."""
    def _serve(request):
        page = int(dict(request.url.params).get("page", 1))
        if page == 1:
            return httpx.Response(200, json={"items": [{"id": "a"}], "total": 9999})
        return httpx.Response(200, json={"items": [], "total": 9999})

    respx.get(url__startswith=f"{HUB}/tickets").mock(side_effect=_serve)

    assert len(hub_client.iter_all_tickets("tok")) == 1
