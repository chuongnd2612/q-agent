"""`GET /ai/credentials/hub` — what Settings shows as the effective credential (#512).

Two things matter here. First, **the credential material must never reach the
browser**: the hub's resolve payload carries the real token, and this endpoint
exists to describe it, not to relay it. Second, the endpoint must **never fail** —
Settings has to render whether or not the hub answers, because the local
credential is a genuine fallback rather than an error condition.
"""

from __future__ import annotations

import httpx
import pytest
import respx

HUB = "https://hub.example.test/api"
RESOLVE = f"{HUB}/credentials/claude/resolve"

# A realistic payload — including the material, which must not survive.
_MATERIAL = '{"claudeAiOauth":{"accessToken":"sk-ant-oat01-SECRET-DO-NOT-LEAK"}}'
_RESOLVED = {
    "source": "shared",
    "status": "refreshable",
    "label": "duna.nguyen@emesoft.net",
    "expiresAt": "2026-08-06T12:00:00Z",
    "daysLeft": 0,
    "scopes": ["user:inference", "user:profile"],
    "subscriptionType": "team",
    "credentials": _MATERIAL,
}


@pytest.fixture
def hub_on(monkeypatch, workspace_dir):
    """Flags on, applied *after* ``workspace_dir``.

    Depending on ``workspace_dir`` explicitly is load-bearing: it rebuilds
    ``settings`` in place (``settings.__dict__.update(...)``), so patches applied
    before it are silently wiped and the test then asserts against a disabled
    integration.
    """
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "hub_sso_enabled", True)
    monkeypatch.setattr(config_module.settings, "hub_data_enabled", True)
    monkeypatch.setattr(config_module.settings, "hub_base_url", HUB)
    return config_module.settings


@respx.mock
def test_returns_sanitised_metadata(hub_on, client):
    respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_RESOLVED))

    body = client.get("/ai/credentials/hub", headers={"X-Hub-Token": "tok"}).json()

    assert body["available"] is True
    assert body["source"] == "shared"
    assert body["label"] == "duna.nguyen@emesoft.net"
    assert body["subscriptionType"] == "team"
    assert body["scopes"] == ["user:inference", "user:profile"]


@respx.mock
def test_credential_material_never_reaches_the_client(hub_on, client):
    """The whole reason this endpoint exists rather than proxying the hub's."""
    respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_RESOLVED))

    res = client.get("/ai/credentials/hub", headers={"X-Hub-Token": "tok"})

    assert "credentials" not in res.json()
    # Belt and braces: the secret must not appear anywhere in the response text,
    # however the payload is nested or re-keyed upstream.
    assert "sk-ant-oat01" not in res.text
    assert "DO-NOT-LEAK" not in res.text


@respx.mock
def test_unknown_upstream_fields_are_not_relayed(hub_on, client):
    """The whitelist is applied by construction, so a new hub field can't leak."""
    respx.get(RESOLVE).mock(
        return_value=httpx.Response(200, json={**_RESOLVED, "refreshToken": "rt-SECRET"})
    )

    res = client.get("/ai/credentials/hub", headers={"X-Hub-Token": "tok"})

    assert "refreshToken" not in res.json()
    assert "rt-SECRET" not in res.text


@respx.mock
def test_refreshable_status_is_passed_through(hub_on, client):
    """`refreshable` is the common live value; the UI must not read it as expired."""
    respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_RESOLVED))

    assert client.get("/ai/credentials/hub", headers={"X-Hub-Token": "tok"}).json()["status"] == "refreshable"


# ------------------------------------------------- never an error, always renders
def test_flag_off_reports_unavailable_without_calling_the_hub(client, monkeypatch):
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "hub_data_enabled", False)

    with respx.mock:
        route = respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_RESOLVED))
        res = client.get("/ai/credentials/hub", headers={"X-Hub-Token": "tok"})

    assert res.status_code == 200
    assert res.json() == {"available": False}
    assert not route.called


@respx.mock
def test_no_hub_token_reports_unavailable(hub_on, client):
    route = respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_RESOLVED))

    res = client.get("/ai/credentials/hub")

    assert res.json() == {"available": False}
    assert not route.called


@respx.mock
@pytest.mark.parametrize("status_code", [401, 403, 502, 503])
def test_hub_failures_report_unavailable_not_an_error(hub_on, client, status_code):
    """Settings must render regardless — a hub failure is not a broken screen."""
    respx.get(RESOLVE).mock(return_value=httpx.Response(status_code))

    res = client.get("/ai/credentials/hub", headers={"X-Hub-Token": "tok"})

    assert res.status_code == 200
    assert res.json() == {"available": False}


@respx.mock
def test_transport_failure_reports_unavailable(hub_on, client):
    respx.get(RESOLVE).mock(side_effect=httpx.ConnectError("refused"))

    res = client.get("/ai/credentials/hub", headers={"X-Hub-Token": "tok"})

    assert res.status_code == 200
    assert res.json() == {"available": False}


@respx.mock
def test_non_dict_payload_reports_unavailable(hub_on, client):
    respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=["not", "a", "dict"]))

    assert client.get("/ai/credentials/hub", headers={"X-Hub-Token": "tok"}).json() == {
        "available": False
    }
