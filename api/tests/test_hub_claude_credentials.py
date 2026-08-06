"""Claude credentials resolved from EmeHub at run start — C2 of #497 / #499.

Three things are actually being defended here:

1. **The asymmetric fallback.** "The hub is down" and "this 15-minute token is
   done" are *not* answers about the credential, so the run proceeds on the local
   one. "The hub says there is no usable credential" *is* an answer, so the run
   refuses rather than quietly using a possibly-stale local credential.
2. **``refreshable`` is usable.** It is the common live value (a Claude access
   token expires within hours), so treating it as expired would refuse almost
   every real credential.
3. **The material never leaks.** Not into logs at any level, not into the run
   response, not into the exception message.

The hub is stubbed with ``respx`` throughout — nothing here touches a real hub,
and the "credential material" below is a fabricated string, not a token.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.services import claude_credentials, hub_credentials

HUB = "https://hub.example.test/api"
RESOLVE = f"{HUB}/credentials/claude/resolve"

# A marker string that must never appear in logs or responses. Fabricated — this
# is not, and must never be, real credential material.
FAKE_ACCESS_TOKEN = "sk-ant-oat01-NOT-A-REAL-TOKEN-canary-499"


def _hub_payload(status: str = "refreshable", *, source: str = "shared") -> dict:
    return {
        "source": source,
        "status": status,
        "expiresAt": "2026-08-05T12:00:00Z",
        "daysLeft": 0,
        "scopes": ["user:inference"],
        "subscriptionType": "max",
        "credentials": json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": FAKE_ACCESS_TOKEN,
                    "refreshToken": "rt-fake-499",
                    "expiresAt": 1754400000000,
                    "scopes": ["user:inference"],
                    "subscriptionType": "max",
                }
            }
        ),
    }


@pytest.fixture
def hub_on(workspace_dir, monkeypatch):
    """Both flags on and a base URL set — the configured, enabled state."""
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "hub_sso_enabled", True)
    monkeypatch.setattr(config_module.settings, "hub_data_enabled", True)
    monkeypatch.setattr(config_module.settings, "hub_base_url", HUB)
    return config_module.settings


@pytest.fixture
def log_sink():
    """Capture every loguru record, at every level.

    ``caplog`` alone does not see loguru output, so a "never logged" assertion
    against it would pass vacuously. This attaches a real sink instead.
    """
    from app.logging import logger

    records: list[str] = []
    sink_id = logger.add(records.append, level="TRACE")
    try:
        yield records
    finally:
        logger.remove(sink_id)


def _materialized(run_id: int) -> str | None:
    config_dir = claude_credentials.hub_run_config_dir(run_id)
    if config_dir is None:
        return None
    return (config_dir / ".credentials.json").read_text(encoding="utf-8")


# ------------------------------------------------------------- the happy path
@respx.mock
def test_hub_credential_is_materialized_at_run_start(hub_on):
    respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_hub_payload()))

    source = hub_credentials.prepare_run_credential(41, "fresh-hub-token")

    assert source == hub_credentials.SOURCE_HUB
    written = _materialized(41)
    assert written is not None
    # The CLI reads `.credentials.json`; the OAuth object must survive intact.
    assert json.loads(written)["claudeAiOauth"]["accessToken"] == FAKE_ACCESS_TOKEN


@respx.mock
def test_refreshable_is_usable_not_expired(hub_on):
    """`refreshable` is the common live value — the access token is past its
    expiry but a refresh token exists, and the CLI refreshes in place."""
    respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_hub_payload("refreshable")))

    assert hub_credentials.prepare_run_credential(42, "tok") == hub_credentials.SOURCE_HUB
    assert _materialized(42) is not None


@respx.mock
def test_bare_oauth_object_is_wrapped_for_the_cli(hub_on):
    """The hub may send the OAuth object itself; the CLI needs it under
    `claudeAiOauth`."""
    payload = _hub_payload()
    payload["credentials"] = {"accessToken": FAKE_ACCESS_TOKEN, "expiresAt": 1754400000000}
    respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=payload))

    assert hub_credentials.prepare_run_credential(43, "tok") == hub_credentials.SOURCE_HUB
    assert json.loads(_materialized(43))["claudeAiOauth"]["accessToken"] == FAKE_ACCESS_TOKEN


@respx.mock
def test_unknown_status_is_treated_as_usable(hub_on):
    """A status we've never seen must not refuse a run the hub was willing to
    serve material for — only the explicitly dead ones refuse."""
    respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_hub_payload("brand_new")))

    assert hub_credentials.prepare_run_credential(44, "tok") == hub_credentials.SOURCE_HUB


# ------------------------------------------------------ fall back to the local
@respx.mock
def test_hub_unreachable_falls_back_to_local(hub_on):
    respx.get(RESOLVE).mock(side_effect=httpx.ConnectError("hub is down"))

    assert hub_credentials.prepare_run_credential(45, "tok") == hub_credentials.SOURCE_LOCAL
    assert _materialized(45) is None


@respx.mock
def test_hub_gateway_error_falls_back_to_local(hub_on):
    """Behind nginx + a tunnel, "down" arrives as 502/503/504, not a socket error."""
    respx.get(RESOLVE).mock(return_value=httpx.Response(503))

    assert hub_credentials.prepare_run_credential(46, "tok") == hub_credentials.SOURCE_LOCAL


@respx.mock
def test_hub_401_falls_back_to_local(hub_on):
    """An expired 15-minute token / revoked hub session says nothing about the
    credential — the run proceeds locally."""
    respx.get(RESOLVE).mock(
        return_value=httpx.Response(401, json={"detail": "Session revoked or expired"})
    )

    assert hub_credentials.prepare_run_credential(47, "tok") == hub_credentials.SOURCE_LOCAL
    assert _materialized(47) is None


@respx.mock
def test_no_hub_token_falls_back_to_local_without_calling_the_hub(hub_on):
    route = respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_hub_payload()))

    assert hub_credentials.prepare_run_credential(48, None) == hub_credentials.SOURCE_LOCAL
    assert not route.called


@respx.mock
def test_fallback_clears_a_stale_hub_credential(hub_on):
    """A dir left by an earlier attempt must never be picked up silently once the
    hub stops answering."""
    respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_hub_payload()))
    assert hub_credentials.prepare_run_credential(49, "tok") == hub_credentials.SOURCE_HUB
    assert _materialized(49) is not None

    respx.get(RESOLVE).mock(side_effect=httpx.ConnectError("hub is down"))
    assert hub_credentials.prepare_run_credential(49, "tok") == hub_credentials.SOURCE_LOCAL
    assert _materialized(49) is None


# ------------------------------------------------------------- refuse the run
@respx.mock
def test_authoritative_no_credential_refuses_the_run(hub_on):
    """404 is an answer, not an outage: refuse rather than fall back."""
    respx.get(RESOLVE).mock(
        return_value=httpx.Response(404, json={"detail": "No Claude credential"})
    )

    with pytest.raises(hub_credentials.HubCredentialRefusedError):
        hub_credentials.prepare_run_credential(50, "tok")


@respx.mock
def test_status_none_refuses_the_run(hub_on):
    payload = _hub_payload("none", source="none")
    payload["credentials"] = None
    respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=payload))

    with pytest.raises(hub_credentials.HubCredentialRefusedError):
        hub_credentials.prepare_run_credential(51, "tok")
    assert _materialized(51) is None


@respx.mock
def test_status_expired_refuses_the_run(hub_on):
    """A stale credential is the one thing the run must refuse on."""
    respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_hub_payload("expired")))

    with pytest.raises(hub_credentials.HubCredentialRefusedError):
        hub_credentials.prepare_run_credential(52, "tok")


@respx.mock
def test_403_refuses_the_run(hub_on):
    respx.get(RESOLVE).mock(return_value=httpx.Response(403, json={"detail": "Not yours"}))

    with pytest.raises(hub_credentials.HubCredentialRefusedError):
        hub_credentials.prepare_run_credential(53, "tok")


# ------------------------------------------------------------------- flag off
@respx.mock
def test_flag_off_makes_no_hub_call_and_uses_local(workspace_dir, monkeypatch):
    """Off means *no outbound request*, and behaviour identical to before."""
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "hub_sso_enabled", True)
    monkeypatch.setattr(config_module.settings, "hub_data_enabled", False)
    monkeypatch.setattr(config_module.settings, "hub_base_url", HUB)
    route = respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_hub_payload()))

    assert hub_credentials.prepare_run_credential(54, "tok") == hub_credentials.SOURCE_LOCAL
    assert not route.called
    assert _materialized(54) is None


@respx.mock
def test_flag_off_leaves_the_cli_env_on_the_local_credential(
    workspace_dir, shared_claude_credential, monkeypatch
):
    """With the flag off, a materialized hub dir (e.g. from before the flag was
    turned off) must be ignored entirely — byte-identical to today."""
    import app.config as config_module
    from app.services import claude_cli, run_context

    claude_credentials.materialize_raw(json.dumps({"claudeAiOauth": {"accessToken": "x"}}), claude_credentials.hub_run_key(55))
    monkeypatch.setattr(config_module.settings, "hub_data_enabled", False)

    run_context.set_run(55)
    try:
        env, _owner = claude_cli._resolve_claude_env()
    finally:
        run_context.clear()

    assert claude_credentials.hub_run_key(55) not in env["CLAUDE_CONFIG_DIR"]


# ------------------------------------------- the run actually uses the hub dir
@respx.mock
def test_cli_env_points_at_the_hub_dir_without_calling_the_hub(
    hub_on, shared_claude_credential
):
    """The whole point of resolving at run start: the worker thread reads a file.

    The respx mock is asserted *uncalled* during the CLI-env resolution, which is
    what "no hub call on a background thread mid-run" means concretely.
    """
    from app.services import claude_cli, run_context

    respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_hub_payload()))
    assert hub_credentials.prepare_run_credential(56, "tok") == hub_credentials.SOURCE_HUB

    respx.reset()
    route = respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_hub_payload()))

    run_context.set_run(56)
    try:
        env, _owner = claude_cli._resolve_claude_env()
    finally:
        run_context.clear()

    assert env["CLAUDE_CONFIG_DIR"].endswith(claude_credentials.hub_run_key(56))
    assert not route.called


@respx.mock
def test_a_run_without_a_hub_credential_still_uses_the_local_one(
    hub_on, shared_claude_credential
):
    from app.services import claude_cli, run_context

    run_context.set_run(57)
    try:
        env, _owner = claude_cli._resolve_claude_env()
    finally:
        run_context.clear()

    assert claude_credentials.hub_run_key(57) not in env["CLAUDE_CONFIG_DIR"]


# ------------------------------------------------------- the material is quiet
@respx.mock
def test_credential_material_is_never_logged(hub_on, log_sink, caplog):
    """Not at any level, and not in the refusal message either."""
    respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_hub_payload()))

    with caplog.at_level("DEBUG"):
        assert hub_credentials.prepare_run_credential(58, "hub-token-canary") == "hub"

    logged = "\n".join(log_sink) + caplog.text
    assert FAKE_ACCESS_TOKEN not in logged
    assert "rt-fake-499" not in logged
    # The hub token that bought the credential must not leak either.
    assert "hub-token-canary" not in logged
    # ...but the *source* must be answerable from the logs.
    assert "hub" in logged


@respx.mock
def test_refusal_message_carries_no_material(hub_on):
    payload = _hub_payload("expired")
    respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=payload))

    with pytest.raises(hub_credentials.HubCredentialRefusedError) as exc:
        hub_credentials.prepare_run_credential(59, "tok")

    assert FAKE_ACCESS_TOKEN not in str(exc.value)


# ------------------------------------------------------------ endpoint wiring
@respx.mock
def test_create_run_refuses_when_the_hub_says_no(hub_on, client, seed_ticket):
    respx.get(RESOLVE).mock(return_value=httpx.Response(404, json={"detail": "none"}))

    resp = client.post(
        "/runs",
        json={"scope": "selected", "ticketIds": [seed_ticket.external_id]},
        headers={"X-Hub-Token": "tok"},
    )

    assert resp.status_code == 409
    assert "EmeHub" in resp.json()["detail"]
    assert FAKE_ACCESS_TOKEN not in resp.text


@respx.mock
def test_create_run_materializes_the_hub_credential(hub_on, client, seed_ticket, monkeypatch):
    from tests.test_runs import _patch_pipeline_blocking

    _patch_pipeline_blocking(monkeypatch)
    respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_hub_payload()))

    resp = client.post(
        "/runs",
        json={"scope": "selected", "ticketIds": [seed_ticket.external_id]},
        headers={"X-Hub-Token": "tok"},
    )

    assert resp.status_code == 200
    run_id = resp.json()["id"]
    assert _materialized(run_id) is not None
    # The material must never come back to the SPA.
    assert FAKE_ACCESS_TOKEN not in resp.text


@respx.mock
def test_create_run_without_a_hub_token_is_unchanged(hub_on, client, seed_ticket, monkeypatch):
    """A request with no hub token is ordinary — the run proceeds locally."""
    from tests.test_runs import _patch_pipeline_blocking

    _patch_pipeline_blocking(monkeypatch)
    route = respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_hub_payload()))

    resp = client.post("/runs", json={"scope": "selected", "ticketIds": [seed_ticket.external_id]})

    assert resp.status_code == 200
    assert not route.called
