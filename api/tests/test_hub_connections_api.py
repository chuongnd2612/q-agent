"""`GET /hub/connections` — hub-owned provider connections, read-only (C4 of #497).

The point of this endpoint is **visibility, not capability**. The hub never sends
a PAT (`hasPat: true` and nothing more) and `POST /connections/{id}/proxy` is
deliberately unbuilt, so nothing here can be used to make a provider call. These
tests pin that boundary in both directions:

* the data we surface carries no secret, and
* no hub failure — off, unauthorised, refused, unreachable — is allowed to turn
  into an error the Settings screen has to render. The local connection picker
  keeps working in every one of those cases, which is what a user actually needs.

The hub is stubbed with ``respx`` throughout; no test here touches a real hub.
"""

from __future__ import annotations

import httpx
import pytest
import respx

HUB = "https://hub.example.test/api"

# One connection as the live hub actually serves it (measured, #497): `hasPat`
# is present and the PAT itself is not.
HUB_CONNECTION = {
    "id": "conn_ado_1",
    "kind": "ado",
    "label": "Emesoft ADO",
    "baseUrl": "https://dev.azure.com/emesoft",
    "config": {"project": "QAgent"},
    "capabilities": ["work_item"],
    "supportedCapabilities": ["work_item", "repository"],
    "connected": True,
    "hasPat": True,
    "lastSync": "2026-08-01T10:00:00Z",
    "lastTestedAt": "2026-08-02T11:30:00Z",
    "shared": False,
}


@pytest.fixture
def hub_on(monkeypatch):
    """Both flags on and a base URL set — the configured, enabled state."""
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "hub_sso_enabled", True)
    monkeypatch.setattr(config_module.settings, "hub_data_enabled", True)
    monkeypatch.setattr(config_module.settings, "hub_base_url", HUB)
    return config_module.settings


def _get(client, token: str | None = "hub-token-abc"):
    headers = {"X-Hub-Token": token} if token else {}
    return client.get("/hub/connections", headers=headers)


# ------------------------------------------------------------------ the flag
@respx.mock
def test_flag_off_returns_empty_and_makes_no_hub_call(client):
    """Off means *no outbound request*, not a request whose answer we drop."""
    route = respx.get(f"{HUB}/connections").mock(return_value=httpx.Response(200, json=[HUB_CONNECTION]))

    resp = _get(client)

    assert resp.status_code == 200
    assert resp.json() == []
    assert route.call_count == 0


@respx.mock
def test_no_hub_token_returns_empty_and_makes_no_hub_call(client, hub_on):
    """A request without a hub token is ordinary — most requests have none."""
    route = respx.get(f"{HUB}/connections").mock(return_value=httpx.Response(200, json=[HUB_CONNECTION]))

    resp = _get(client, token=None)

    assert resp.status_code == 200
    assert resp.json() == []
    assert route.call_count == 0


# ------------------------------------------------------------- the happy path
@respx.mock
def test_flag_on_lists_hub_connections_read_only(client, hub_on):
    respx.get(f"{HUB}/connections").mock(return_value=httpx.Response(200, json=[HUB_CONNECTION]))

    resp = _get(client)

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    conn = body[0]
    assert conn == {
        "id": "conn_ado_1",
        "kind": "ado",
        "label": "Emesoft ADO",
        "baseUrl": "https://dev.azure.com/emesoft",
        "capabilities": ["work_item"],
        "supportedCapabilities": ["work_item", "repository"],
        "connected": True,
        "hasPat": True,
        "lastSync": "2026-08-01T10:00:00Z",
        "lastTestedAt": "2026-08-02T11:30:00Z",
        "shared": False,
    }


@respx.mock
def test_the_hub_token_is_forwarded_as_a_bearer(client, hub_on):
    captured: dict[str, str] = {}

    def _record(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(200, json=[])

    respx.get(f"{HUB}/connections").mock(side_effect=_record)

    assert _get(client).status_code == 200
    assert captured["auth"] == "Bearer hub-token-abc"


# ------------------------------------------------------------- no PAT, ever
@respx.mock
def test_a_pat_is_never_surfaced_even_if_the_hub_sent_one(client, hub_on):
    """Defence in depth: the hub does not send a PAT, and if it ever did we drop it.

    The response model is a fixed subset, so an unexpected secret-shaped field
    cannot leak through to the browser by accident.
    """
    leaky = {**HUB_CONNECTION, "pat": "ghp_supersecret", "token": "t0ps3cret", "secrets": {"pat": "x"}}
    respx.get(f"{HUB}/connections").mock(return_value=httpx.Response(200, json=[leaky]))

    resp = _get(client)

    assert resp.status_code == 200
    serialized = resp.text
    assert "ghp_supersecret" not in serialized
    assert "t0ps3cret" not in serialized
    assert set(resp.json()[0]) == {
        "id", "kind", "label", "baseUrl", "capabilities", "supportedCapabilities",
        "connected", "hasPat", "lastSync", "lastTestedAt", "shared",
    }


@respx.mock
def test_no_pat_is_requested_of_the_hub(client, hub_on):
    """We ask for `/connections` and nothing else — no PAT-bearing endpoint.

    ``respx.mock`` asserts on the routes actually called, so a stray request to
    a proxy/secret endpoint would fail this test rather than pass silently.
    """
    listing = respx.get(f"{HUB}/connections").mock(return_value=httpx.Response(200, json=[HUB_CONNECTION]))

    assert _get(client).status_code == 200

    assert listing.call_count == 1
    called = {call.request.url.path for call in respx.calls}
    assert called == {"/api/connections"}
    assert not any("proxy" in path or "pat" in path or "secret" in path for path in called)


# -------------------------------------------------- failures degrade silently
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"detail": "Session revoked or expired"}),
        httpx.Response(403, json={"detail": "nope"}),
        httpx.Response(404, json={"detail": "gone"}),
        httpx.Response(502, text="bad gateway"),
        httpx.Response(503, text="unavailable"),
        httpx.Response(200, text="<html>not json</html>"),
    ],
)
@respx.mock
def test_hub_failures_show_nothing_extra_and_never_error(client, hub_on, response):
    """Every hub failure kind degrades to "no hub connections", not a 5xx.

    The Settings screen's job is the local picker; a hub problem must not put an
    error over it.
    """
    respx.get(f"{HUB}/connections").mock(return_value=response)

    resp = _get(client)

    assert resp.status_code == 200
    assert resp.json() == []


@respx.mock
def test_hub_unreachable_shows_nothing_extra(client, hub_on):
    respx.get(f"{HUB}/connections").mock(side_effect=httpx.ConnectError("no route to host"))

    resp = _get(client)

    assert resp.status_code == 200
    assert resp.json() == []


@respx.mock
def test_hub_unreachable_leaves_the_local_picker_untouched(client, hub_on):
    """The load-bearing guarantee: hub down → `/providers` is exactly as before."""
    respx.get(f"{HUB}/connections").mock(side_effect=httpx.ConnectError("no route to host"))

    before = client.get("/providers")
    assert _get(client).json() == []
    after = client.get("/providers", headers={"X-Hub-Token": "hub-token-abc"})

    assert before.status_code == after.status_code == 200
    assert before.json() == after.json()


# --------------------------------------------------------- defensive mapping
@respx.mock
def test_malformed_entries_are_dropped_not_rendered_half_formed(client, hub_on):
    respx.get(f"{HUB}/connections").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"label": "no id or kind"},
                {"id": "c2", "kind": "github"},  # minimal but valid
                "not-an-object",
            ],
        )
    )

    body = _get(client).json()

    assert [c["id"] for c in body] == ["c2"]
    assert body[0] == {
        "id": "c2",
        "kind": "github",
        "label": "github",
        "baseUrl": "",
        "capabilities": [],
        "supportedCapabilities": [],
        "connected": False,
        "hasPat": False,
        "lastSync": None,
        "lastTestedAt": None,
        "shared": False,
    }


@respx.mock
def test_a_non_list_payload_is_not_trusted(client, hub_on):
    respx.get(f"{HUB}/connections").mock(return_value=httpx.Response(200, json={"items": []}))

    assert _get(client).json() == []
