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
def test_hub_unreachable_refuses_the_run(hub_on):
    """#607 reversal: an unreachable hub used to fall back to the local credential.

    In hub-data mode (`enabled()` requires BOTH sso+data flags) the hub owns which
    Claude account is used, so falling back is the wrong answer — and on a box with
    no local credential it surfaced 20 minutes later, in a background worker, as
    "No Claude credentials configured. Upload your own credentials in Settings":
    advice that was flatly wrong for a hub that had the credential all along.
    Refuse here, while the reason is still known.
    """
    respx.get(RESOLVE).mock(side_effect=httpx.ConnectError("hub is down"))

    with pytest.raises(hub_credentials.HubCredentialRefusedError, match="Could not reach EmeHub"):
        hub_credentials.prepare_run_credential(45, "tok")
    assert _materialized(45) is None


@respx.mock
def test_hub_gateway_error_refuses_the_run(hub_on):
    """Behind nginx + a tunnel, "down" arrives as 502/503/504, not a socket error —
    and it must refuse just like a socket error does (#607)."""
    respx.get(RESOLVE).mock(return_value=httpx.Response(503))

    with pytest.raises(hub_credentials.HubCredentialRefusedError, match="Could not reach EmeHub"):
        hub_credentials.prepare_run_credential(46, "tok")


@respx.mock
def test_hub_401_refuses_the_run_and_says_to_reload(hub_on):
    """An expired 15-minute token says nothing about the credential, but it also
    means we cannot honour "use the hub's credential" — so refuse with the action
    that actually fixes it (reload, which mints a fresh token) rather than running
    on whatever is local (#607)."""
    respx.get(RESOLVE).mock(
        return_value=httpx.Response(401, json={"detail": "Session revoked or expired"})
    )

    with pytest.raises(hub_credentials.HubCredentialRefusedError, match="session token expired"):
        hub_credentials.prepare_run_credential(47, "tok")
    assert _materialized(47) is None


@respx.mock
def test_no_hub_token_refuses_without_calling_the_hub(hub_on):
    """No token means the hub's credential cannot be resolved at all. Still no hub
    call (nothing to authorise it), but no silent local fallback either (#607)."""
    route = respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_hub_payload()))

    with pytest.raises(hub_credentials.HubCredentialRefusedError, match="No EmeHub token"):
        hub_credentials.prepare_run_credential(48, None)
    assert not route.called


@respx.mock
def test_fallback_clears_a_stale_hub_credential(hub_on):
    """A dir left by an earlier attempt must never be picked up silently once the
    hub stops answering."""
    respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_hub_payload()))
    assert hub_credentials.prepare_run_credential(49, "tok") == hub_credentials.SOURCE_HUB
    assert _materialized(49) is not None

    respx.get(RESOLVE).mock(side_effect=httpx.ConnectError("hub is down"))
    with pytest.raises(hub_credentials.HubCredentialRefusedError):
        hub_credentials.prepare_run_credential(49, "tok")
    assert _materialized(49) is None, "the stale dir is still cleared before refusing"


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
    """#607: in hub-data mode a run start with no hub token is refused (409), not
    quietly run on local material. The hub decides which Claude account is used."""
    from tests.test_runs import _patch_pipeline_blocking

    _patch_pipeline_blocking(monkeypatch)
    route = respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_hub_payload()))

    resp = client.post("/runs", json={"scope": "selected", "ticketIds": [seed_ticket.external_id]})

    assert resp.status_code == 409
    assert "No EmeHub token" in resp.json()["detail"]
    assert not route.called


# ------------------------------------------------- picking up a change (#667)
GRANT = f"{HUB}/auth/agent-grant"


def _grant_response(expires_in: int = 14400) -> dict:
    return {
        "grant": "grant-fake-667",
        "audience": "qagent-credential",
        "scope": "claude:resolve",
        "runId": "1",
        "expiresIn": expires_in,
    }


def _age_out(run_id: int) -> None:
    """Backdate the materialised credential past the refresh window.

    The window exists so `claude_cli` (which resolves the environment for EVERY
    Claude call) does not hit the hub once per call; a test that did not age the
    file would silently assert nothing, because the refresh would decline as
    "recent enough".
    """
    import os
    import time

    path = claude_credentials.hub_run_config_dir(run_id) / ".credentials.json"
    old = time.time() - hub_credentials.CREDENTIAL_MAX_AGE.total_seconds() - 60
    os.utime(path, (old, old))


@respx.mock
def test_a_changed_hub_account_reaches_a_run_already_under_way(hub_on):
    """#667: the whole point — change the account in EmeHub, the run follows.

    The credential used to be resolved once and pinned at run start, so a change
    could only ever affect a NEW run. A background worker had no way to ask the
    hub: agent tokens live 15 minutes and are session-bound. The run now carries a
    credential GRANT, minted from the browser's token while it was fresh, and that
    makes the later call legal.
    """
    respx.post(GRANT).mock(return_value=httpx.Response(201, json=_grant_response()))
    respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_hub_payload()))
    assert hub_credentials.prepare_run_credential(1, "hub-token") == hub_credentials.SOURCE_HUB
    assert FAKE_ACCESS_TOKEN in _materialized(1)

    # The user connects a different Claude account in EmeHub.
    changed = _hub_payload()
    changed["credentials"] = changed["credentials"].replace(FAKE_ACCESS_TOKEN, "sk-ant-oat01-SECOND")
    respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=changed))

    _age_out(1)
    assert hub_credentials.refresh_run_credential(1) is True
    assert "sk-ant-oat01-SECOND" in _materialized(1)


@respx.mock
def test_the_refresh_is_rate_limited(hub_on):
    """#667: `claude_cli` resolves the env per call, so this must not be per call."""
    respx.post(GRANT).mock(return_value=httpx.Response(201, json=_grant_response()))
    respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_hub_payload()))
    hub_credentials.prepare_run_credential(1, "hub-token")

    calls_before = respx.calls.call_count
    # Freshly materialised ⇒ declines without touching the hub.
    assert hub_credentials.refresh_run_credential(1) is False
    assert respx.calls.call_count == calls_before


@respx.mock
def test_a_hub_blip_leaves_the_pinned_credential_alone(hub_on):
    """#667: the credential is a dependency of the work, not the work.

    A generation pass with perfectly good material on disk must not fail because
    the hub had a bad minute.
    """
    respx.post(GRANT).mock(return_value=httpx.Response(201, json=_grant_response()))
    respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_hub_payload()))
    hub_credentials.prepare_run_credential(1, "hub-token")

    respx.get(RESOLVE).mock(return_value=httpx.Response(503, text="down"))
    _age_out(1)
    assert hub_credentials.refresh_run_credential(1) is False
    assert FAKE_ACCESS_TOKEN in _materialized(1), "the run lost credentials it already had"


@respx.mock
def test_an_expired_grant_stops_the_refresh(hub_on):
    """#667: a grant dies with the hub session; expiry must not be papered over."""
    respx.post(GRANT).mock(return_value=httpx.Response(201, json=_grant_response(expires_in=-1)))
    respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_hub_payload()))
    hub_credentials.prepare_run_credential(1, "hub-token")

    _age_out(1)
    calls_before = respx.calls.call_count
    assert hub_credentials.refresh_run_credential(1) is False
    assert respx.calls.call_count == calls_before, "an expired grant must not be sent to the hub"


@respx.mock
def test_a_run_still_starts_when_the_hub_cannot_mint_a_grant(hub_on):
    """#667: the grant is an upgrade, never a gate.

    A hub that refuses to mint one leaves the run on pinned material — exactly the
    behaviour before this existed — rather than failing a run that has a perfectly
    good credential.
    """
    respx.post(GRANT).mock(return_value=httpx.Response(500, text="nope"))
    respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_hub_payload()))

    assert hub_credentials.prepare_run_credential(1, "hub-token") == hub_credentials.SOURCE_HUB
    assert FAKE_ACCESS_TOKEN in _materialized(1)
    _age_out(1)
    assert hub_credentials.refresh_run_credential(1) is False


@respx.mock
def test_the_grant_is_never_logged(hub_on, log_sink):
    """#667: a grant reaches the credential routes — it is a secret like any other."""
    respx.post(GRANT).mock(return_value=httpx.Response(201, json=_grant_response()))
    respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_hub_payload()))
    hub_credentials.prepare_run_credential(1, "hub-token")

    assert not any("grant-fake-667" in line for line in log_sink)
    assert not any(FAKE_ACCESS_TOKEN in line for line in log_sink)


# ------------------------------------- posting a ROTATED token back (#682)
#
# The half of the contract that was missing, and the reason a run died with
# "Not logged in · Please run /login" surfaced as a bare HTTP 502.
#
# A Claude OAuth access token lives hours, and the CLI refreshes it *in place*:
# it rewrites `.credentials.json` with a new access token AND a new refresh
# token, which invalidates the previous refresh token. So a rotation that never
# reaches the hub does not merely go missing — it makes the hub's stored copy
# permanently unusable. Then #667's 60-second re-resolve fetched that dead copy
# and wrote it over the live material on disk, and the next Claude call failed.
#
# Two independent defences are asserted below, and both matter: the rotation is
# posted back (so the hub's copy stays alive), AND a staler hub copy can never
# overwrite a fresher token on disk (so a missed post is survivable, not fatal).
REFRESHED = f"{HUB}/credentials/claude/refreshed"

#: The expiry in `_hub_payload`. A "rotated" token is deliberately later.
HUB_EXPIRES_MS = 1754400000000
ROTATED_ACCESS_TOKEN = "sk-ant-oat01-ROTATED-canary-682"


@pytest.fixture(autouse=True)
def _forget_captures():
    """The per-run "already posted this rotation" memo is module state."""
    hub_credentials._LAST_CAPTURED.clear()
    yield
    hub_credentials._LAST_CAPTURED.clear()


def _rotate_on_disk(run_id: int, *, expires_ms: int = HUB_EXPIRES_MS + 3_600_000) -> str:
    """Stand in for the CLI refreshing the token in the run's config dir."""
    material = json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": ROTATED_ACCESS_TOKEN,
                "refreshToken": "rt-fake-682-rotated",
                "expiresAt": expires_ms,
                "scopes": ["user:inference"],
                "subscriptionType": "max",
            }
        }
    )
    path = claude_credentials.hub_run_config_dir(run_id) / ".credentials.json"
    path.write_text(material, encoding="utf-8")
    return material


def _prepared(run_id: int = 1) -> None:
    respx.post(GRANT).mock(return_value=httpx.Response(201, json=_grant_response()))
    respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_hub_payload()))
    assert hub_credentials.prepare_run_credential(run_id, "hub-token") == hub_credentials.SOURCE_HUB


@respx.mock
def test_a_rotated_token_is_posted_back_to_the_hub(hub_on):
    """#682: without this the hub's copy dies the first time a token rotates."""
    put = respx.put(REFRESHED).mock(
        return_value=httpx.Response(200, json={"ok": True, "updated": True})
    )
    _prepared()
    rotated = _rotate_on_disk(1)

    assert hub_credentials.capture_rotated_credential(1) is True

    assert put.called, "the rotation never reached the hub"
    sent = json.loads(put.calls.last.request.content)
    assert sent["credentials"] == rotated
    # It goes with the run's GRANT, which is the only credential a background
    # thread legally holds - an agent token would be 15 minutes dead by now.
    assert put.calls.last.request.headers["authorization"] == "Bearer grant-fake-667"


@respx.mock
def test_a_stale_hub_copy_never_clobbers_a_rotated_token(hub_on):
    """#682, the actual regression: this is what produced the 502.

    The hub still holds the pre-rotation copy - whose refresh token the rotation
    just invalidated - so writing it to disk hands the CLI a dead credential. The
    run had perfectly good material; the "keep it fresh" path is what broke it.
    """
    put = respx.put(REFRESHED).mock(
        return_value=httpx.Response(200, json={"ok": True, "updated": True})
    )
    _prepared()
    _rotate_on_disk(1)  # newer than anything the hub knows about
    _age_out(1)

    assert hub_credentials.refresh_run_credential(1) is False
    assert ROTATED_ACCESS_TOKEN in _materialized(1), "a staler hub copy overwrote a live token"
    assert FAKE_ACCESS_TOKEN not in _materialized(1)
    # And the direction is corrected rather than merely blocked: the hub is the one
    # that is behind, so the rotation is pushed to it.
    assert put.called


@respx.mock
def test_declining_the_write_still_restarts_the_rate_limit(hub_on):
    """Declining is not free: the window is the file's mtime.

    `claude_cli` resolves the environment for EVERY Claude call, so a decline that
    left the mtime alone would put a hub round-trip on every single invocation for
    the rest of the run — the cost #667's window exists to avoid.
    """
    respx.put(REFRESHED).mock(return_value=httpx.Response(200, json={"ok": True, "updated": True}))
    _prepared()
    _rotate_on_disk(1)
    _age_out(1)

    assert hub_credentials.refresh_run_credential(1) is False
    calls_before = respx.calls.call_count
    assert hub_credentials.refresh_run_credential(1) is False
    assert respx.calls.call_count == calls_before, "the hub was asked again immediately"


@respx.mock
def test_a_newer_hub_credential_still_wins(hub_on):
    """#682 must not break #667: a genuinely newer hub token still lands.

    The guard is "not older", not "never" - otherwise changing the Claude account
    in EmeHub could no longer reach a run under way, which is the whole point of
    the refresh.
    """
    _prepared()
    _rotate_on_disk(1, expires_ms=HUB_EXPIRES_MS - 3_600_000)  # disk is the STALE one

    newer = _hub_payload()
    newer["credentials"] = newer["credentials"].replace(FAKE_ACCESS_TOKEN, "sk-ant-oat01-NEWER")
    respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=newer))
    _age_out(1)

    assert hub_credentials.refresh_run_credential(1) is True
    assert "sk-ant-oat01-NEWER" in _materialized(1)


@respx.mock
def test_a_logged_out_file_is_never_posted_back(hub_on):
    """A failed refresh leaves a token with no expiry - posting it would be the
    one write that could kill the hub's credential from our side."""
    put = respx.put(REFRESHED).mock(
        return_value=httpx.Response(200, json={"ok": True, "updated": True})
    )
    _prepared()
    path = claude_credentials.hub_run_config_dir(1) / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": ""}}), encoding="utf-8")

    assert hub_credentials.capture_rotated_credential(1) is False
    assert not put.called


@respx.mock
def test_the_same_rotation_is_posted_only_once(hub_on):
    """`claude_cli` captures after EVERY call; a generation pass makes many."""
    put = respx.put(REFRESHED).mock(
        return_value=httpx.Response(200, json={"ok": True, "updated": True})
    )
    _prepared()
    _rotate_on_disk(1)

    assert hub_credentials.capture_rotated_credential(1) is True
    assert hub_credentials.capture_rotated_credential(1) is False
    assert hub_credentials.capture_rotated_credential(1) is False
    assert put.call_count == 1

    # A second, genuinely new rotation does go.
    _rotate_on_disk(1, expires_ms=HUB_EXPIRES_MS + 7_200_000)
    assert hub_credentials.capture_rotated_credential(1) is True
    assert put.call_count == 2


@respx.mock
def test_a_run_with_no_grant_posts_nothing(hub_on):
    """No grant means no legal way to write, and no half-authenticated attempt."""
    put = respx.put(REFRESHED).mock(
        return_value=httpx.Response(200, json={"ok": True, "updated": True})
    )
    respx.post(GRANT).mock(return_value=httpx.Response(500, text="nope"))
    respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_hub_payload()))
    hub_credentials.prepare_run_credential(1, "hub-token")
    _rotate_on_disk(1)

    assert hub_credentials.capture_rotated_credential(1) is False
    assert not put.called


@respx.mock
def test_a_run_that_never_used_the_hub_posts_nothing(hub_on):
    """Local material is not the hub's to store, and there is nothing to keep alive."""
    put = respx.put(REFRESHED).mock(
        return_value=httpx.Response(200, json={"ok": True, "updated": True})
    )

    assert hub_credentials.capture_rotated_credential(4242) is False
    assert not put.called


@respx.mock
def test_a_hub_that_refuses_the_rotation_does_not_break_the_run(hub_on):
    """The credential is a dependency of the work, not the work itself."""
    respx.put(REFRESHED).mock(return_value=httpx.Response(500, text="nope"))
    _prepared()
    _rotate_on_disk(1)

    assert hub_credentials.capture_rotated_credential(1) is True  # handled, not raised
    assert ROTATED_ACCESS_TOKEN in _materialized(1), "the live token must stay on disk"


@respx.mock
def test_the_rotated_material_is_never_logged(hub_on, log_sink):
    """Same rule as every other credential path - the token is a secret."""
    respx.put(REFRESHED).mock(return_value=httpx.Response(200, json={"ok": True, "updated": True}))
    _prepared()
    _rotate_on_disk(1)
    hub_credentials.capture_rotated_credential(1)

    assert not any(ROTATED_ACCESS_TOKEN in line for line in log_sink)
    assert not any("rt-fake-682-rotated" in line for line in log_sink)
