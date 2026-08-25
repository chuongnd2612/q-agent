"""Post-run actions resolve the Claude credential from the hub, not from disk (#689).

Q-Agent cannot configure a Claude credential of its own once it is connected to the
hub — the hub is the only source (#607). So an action taken *after* a run has
finished must resolve from the hub exactly as the run's own start did, and this file
is about why inheriting the pinned material was never good enough:

* An access token lives **hours**, and publishing happens whenever the person gets
  round to it. By then the material written at run start is routinely expired.
* The run's **grant expires** (240 minutes), and once it has, the background
  re-resolve in ``refresh_run_credential`` cannot ask the hub at all — so the run is
  stuck on material that only gets older. That is exactly what surfaced as
  ``Not logged in · Please run /login`` → **HTTP 502** on "Prepare comments".

A request has the one thing a worker does not: the browser's freshly-minted hub
token. These tests pin that it is *used*, that a refusal fails the **action** and not
the long-finished run, and that the flag-off path is untouched.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.services import claude_credentials, hub_credentials

HUB = "https://hub.example.test/api"
RESOLVE = f"{HUB}/credentials/claude/resolve"
GRANT = f"{HUB}/auth/agent-grant"

RENEWED_ACCESS_TOKEN = "sk-ant-oat01-RENEWED-canary-689"


def _payload(token: str = RENEWED_ACCESS_TOKEN, status: str = "refreshable") -> dict:
    return {
        "source": "own",
        "status": status,
        "expiresAt": "2026-08-26T12:00:00Z",
        "daysLeft": 0,
        "scopes": ["user:inference"],
        "subscriptionType": "max",
        "credentials": json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": token,
                    "refreshToken": "rt-fake-689",
                    "expiresAt": 1790000000000,
                    "scopes": ["user:inference"],
                    "subscriptionType": "max",
                }
            }
        ),
    }


@pytest.fixture
def hub_on(workspace_dir, monkeypatch):
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "hub_sso_enabled", True)
    monkeypatch.setattr(config_module.settings, "hub_data_enabled", True)
    monkeypatch.setattr(config_module.settings, "hub_base_url", HUB)
    return config_module.settings


def _materialized(run_id: int) -> str | None:
    config_dir = claude_credentials.hub_run_config_dir(run_id)
    if config_dir is None:
        return None
    return (config_dir / ".credentials.json").read_text(encoding="utf-8")


def _pin_dead_material(run_id: int) -> None:
    """What a run looks like hours later: expired material, and no live grant.

    The grant file is deliberately absent — that is the state that makes the
    background re-resolve powerless, and therefore the state a later action has to
    be able to recover from on its own.
    """
    claude_credentials.materialize_raw(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-oat01-DEAD",
                    "refreshToken": "rt-consumed",
                    "expiresAt": 1000,
                }
            }
        ),
        claude_credentials.hub_run_key(run_id),
    )
    hub_credentials.discard_grant(run_id)


# ---------------------------------------------------------------------------
# The fix
# ---------------------------------------------------------------------------


@respx.mock
def test_a_later_action_replaces_expired_pinned_material(hub_on):
    """The regression, at the service level.

    Dead material on disk, no grant, and a request that carries a fresh hub token:
    the action must come back with a live credential rather than running the CLI on
    a token that cannot authenticate.
    """
    respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_payload()))
    respx.post(GRANT).mock(
        return_value=httpx.Response(201, json={"grant": "grant-689", "expiresIn": 14400})
    )
    _pin_dead_material(30)
    assert "sk-ant-oat01-DEAD" in _materialized(30)

    hub_credentials.ensure_run_credential(30, "fresh-browser-token")

    assert RENEWED_ACCESS_TOKEN in _materialized(30)
    assert "sk-ant-oat01-DEAD" not in _materialized(30)


@respx.mock
def test_the_action_re_mints_the_grant_for_the_worker_it_starts(hub_on):
    """Most of these actions hand off to a background thread.

    Renewing only the material would leave that worker on an expired grant, i.e.
    unable to follow an account change or to post a rotation back — the run would be
    healthy for one action and stuck again for the next.
    """
    respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_payload()))
    minted = respx.post(GRANT).mock(
        return_value=httpx.Response(201, json={"grant": "grant-689", "expiresIn": 14400})
    )
    _pin_dead_material(30)

    hub_credentials.ensure_run_credential(30, "fresh-browser-token")

    assert minted.called
    assert hub_credentials._load_grant(30) == "grant-689"


@respx.mock
def test_no_hub_token_refuses_rather_than_running_on_stale_material(hub_on):
    """In hub-data mode there is nothing else to fall back to.

    Running the CLI on the pinned copy is what produced an opaque 502; refusing says
    what to do while the reason is still known.
    """
    route = respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_payload()))
    _pin_dead_material(30)

    with pytest.raises(hub_credentials.HubCredentialRefusedError):
        hub_credentials.ensure_run_credential(30, None)

    assert not route.called, "no token is not something to ask the hub about"


@respx.mock
def test_the_hub_being_authoritative_about_no_credential_refuses_the_action(hub_on):
    """A 404 is an answer, and quietly using local material is the one wrong move."""
    respx.get(RESOLVE).mock(return_value=httpx.Response(404, json={"detail": "none"}))
    _pin_dead_material(30)

    with pytest.raises(hub_credentials.HubCredentialRefusedError):
        hub_credentials.ensure_run_credential(30, "fresh-browser-token")


@respx.mock
def test_with_the_hub_off_an_action_touches_nothing(hub_on, monkeypatch):
    """Flag off ⇒ byte-identical to before this existed."""
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "hub_data_enabled", False)
    monkeypatch.setattr(config_module.settings, "hub_sso_enabled", False)
    route = respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_payload()))

    assert hub_credentials.ensure_run_credential(30, "token") == hub_credentials.SOURCE_LOCAL
    assert not route.called


@respx.mock
def test_the_renewed_material_is_never_logged(hub_on, caplog):
    """Same rule as every other credential path."""
    from app.logging import logger

    records: list[str] = []
    sink = logger.add(records.append, level="TRACE")
    try:
        respx.get(RESOLVE).mock(return_value=httpx.Response(200, json=_payload()))
        respx.post(GRANT).mock(
            return_value=httpx.Response(201, json={"grant": "grant-689", "expiresIn": 14400})
        )
        hub_credentials.ensure_run_credential(30, "fresh-browser-token")
    finally:
        logger.remove(sink)

    assert not any(RENEWED_ACCESS_TOKEN in line for line in records)
    assert not any("grant-689" in line for line in records)


# ---------------------------------------------------------------------------
# The endpoint contract: a refusal fails the ACTION, never the finished run
# ---------------------------------------------------------------------------


@respx.mock
def test_a_refusal_is_a_409_on_the_action(hub_on):
    """`use_hub_credential` is what every wired endpoint calls.

    Run start fails the *run* on a refusal, which is right there and wrong here: the
    run finished hours ago, and rewriting its status to describe a credential problem
    in a later click would make the history lie.
    """
    from fastapi import HTTPException

    from app.deps_hub import use_hub_credential

    respx.get(RESOLVE).mock(return_value=httpx.Response(404, json={"detail": "none"}))

    with pytest.raises(HTTPException) as excinfo:
        use_hub_credential(30, "fresh-browser-token")

    assert excinfo.value.status_code == 409
    assert "EmeHub" in str(excinfo.value.detail)


@respx.mock
def test_every_claude_driven_run_action_asks_for_the_credential(hub_on):
    """A list is easy to add an endpoint to and forget.

    So this asserts the wiring itself: each endpoint that goes on to drive Claude for
    an existing run must call `use_hub_credential`. If a new one is added without it,
    this fails rather than the user discovering it as a 502 months later.
    """
    import inspect

    from app.routers import automation, comments, evidence, review

    expected = {
        comments: ["prepare_comments"],
        automation: [
            "generate_automation",
            "regenerate_case_spec",
            "chat_edit_spec",
            "heal_case_spec",
        ],
        review: ["regenerate_single_case"],
        evidence: ["auto_annotate_evidence"],
    }
    for module, names in expected.items():
        for name in names:
            source = inspect.getsource(getattr(module, name))
            assert "use_hub_credential(" in source, f"{module.__name__}.{name}"
            assert "hub_token" in source, f"{module.__name__}.{name} takes no hub token"
