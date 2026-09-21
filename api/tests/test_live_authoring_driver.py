"""Tests for the `browserDriver` setting (#875): browser-harness vs playwright-cli.

Covers A1 (the setting selects the right methodology/preflight/env-var code path)
and the prompt-level half of A2 (the verified-KB block `_build_prompt` injects).
Since #894 `playwright-cli` also means "use the real Playwright Test Agents", so
the selection returns an agent rather than a skill.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import claude_cli, live_authoring_service, settings_store
from app.services import agents
from app.services.live_authoring_service import (
    LiveAuthoringError,
    _build_prompt,
    methodology_for,
    system_prompt_for,
)


def _case(**overrides):
    """A minimal TestCase stand-in, matching the pattern in test_prompts.py."""
    defaults = dict(
        title="Sign in", precondition=None, steps=[], test_data=[],
        ticket_external_id="TCK-1", code="TC-01",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ------------------------------------------------------------- methodology_for

def test_browser_harness_still_uses_the_skill():
    """The default path must not move."""
    assert methodology_for("browser-harness", heal=False) == ("live-authoring", None)
    assert methodology_for("browser-harness", heal=True) == ("live-authoring", None)


def test_playwright_cli_picks_the_agent_for_the_job():
    """#876 routes heal through the same function, so `heal` decides which agent —
    a generator asked to heal would author from scratch instead of reproducing."""
    assert methodology_for("playwright-cli", heal=False) == (
        None, agents.PLAYWRIGHT_TEST_GENERATOR,
    )
    assert methodology_for("playwright-cli", heal=True) == (
        None, agents.PLAYWRIGHT_TEST_HEALER,
    )


def test_an_unknown_value_falls_back_to_the_skill():
    """An old/typo'd setting must not silently load NO methodology."""
    assert methodology_for("something-else", heal=False) == ("live-authoring", None)


def test_system_prompt_for_sends_the_agent_body_to_the_local_agent():
    """The paired device has no `--agent` support and no `skills/` dir, so the
    dispatch path ships the methodology as text either way."""
    harness = system_prompt_for("browser-harness", heal=False)
    generator = system_prompt_for("playwright-cli", heal=False)
    healer = system_prompt_for("playwright-cli", heal=True)

    assert harness and "browser-harness" in harness
    assert generator and 'attach --cdp "$PW_CLI_CDP_URL"' in generator
    assert healer != generator, "heal must not ship the generator's methodology"
    # It is the agent body, not its frontmatter.
    assert not generator.startswith("---")


# ---------------------------------------------------- claude_cli.playwright_cli_available

def test_playwright_cli_available_false_when_entry_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/node")  # noqa: ARG005
    # The real app/node_modules/playwright/cli.js is not installed in the test
    # environment, so this is the honest "not available" answer.
    assert claude_cli.playwright_cli_available() is False


def test_playwright_cli_available_false_without_node(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)  # noqa: ARG005
    assert claude_cli.playwright_cli_available() is False


def test_playwright_cli_available_true_when_node_and_cli_js_present(monkeypatch, tmp_path):
    from app.config import settings

    cli_js = tmp_path / "playwright" / "cli.js"
    cli_js.parent.mkdir(parents=True)
    cli_js.write_text("", encoding="utf-8")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/node")  # noqa: ARG005
    monkeypatch.setattr(type(settings), "playwright_cli_js", property(lambda self: cli_js))
    assert claude_cli.playwright_cli_available() is True


# ------------------------------------------------------ author_case: driver preflight

def test_author_case_rejects_playwright_cli_when_unavailable(monkeypatch):
    """`browserDriver=playwright-cli` with no installed CLI fails fast, before
    touching the DB/case/run at all — the setting alone picks this code path."""
    monkeypatch.setattr(settings_store, "load_settings", lambda: {"browserDriver": "playwright-cli"})
    monkeypatch.setattr(claude_cli, "playwright_cli_available", lambda: False)
    monkeypatch.setattr(claude_cli, "browser_harness_available", lambda: True)  # must NOT be consulted

    with pytest.raises(LiveAuthoringError, match="playwright-cli"):
        live_authoring_service.author_case(None, None, None, owner_id=None, run_id=None)


def test_author_case_default_driver_still_checks_browser_harness(monkeypatch):
    """No `browserDriver` in settings (pre-#875 installs) keeps the legacy
    browser-harness preflight — the default must not silently become playwright-cli."""
    monkeypatch.setattr(settings_store, "load_settings", lambda: {})
    monkeypatch.setattr(claude_cli, "browser_harness_available", lambda: False)
    monkeypatch.setattr(claude_cli, "playwright_cli_available", lambda: True)  # must NOT be consulted

    with pytest.raises(LiveAuthoringError, match="browser-harness"):
        live_authoring_service.author_case(None, None, None, owner_id=None, run_id=None)


# -------------------------------------------------------------------- _build_prompt

def test_build_prompt_default_driver_text_is_byte_identical_to_pre_875():
    """browserDriver='browser-harness' (the default) must not change the prompt
    text existing tests/fixtures already match on (e.g. `"browser-harness" in
    prompt` in test_live_harness_incremental.py)."""
    prompt = _build_prompt(_case(), {}, "TCK-1-TC-01.spec.ts", "discovered.json", "https://app.test")
    assert "browser-harness (it is already wired to a signed-in Chrome via BU_CDP_URL" in prompt


def test_build_prompt_playwright_cli_driver_names_the_right_env_vars():
    prompt = _build_prompt(
        _case(), {}, "TCK-1-TC-01.spec.ts", "discovered.json", "https://app.test",
        browser_driver="playwright-cli",
    )
    assert "playwright-cli (it is already wired" in prompt
    assert "PW_CLI_CDP_URL" in prompt
    assert "browser-harness" not in prompt


def test_build_prompt_injects_verified_kb_block_when_present():
    """A2: routes/selectors the KB already confirmed live are surfaced as their
    own 'use these directly' block, not buried in the raw Known routes/selectors
    dump (which includes unverified entries too)."""
    context = {
        "routes": [{"path": "/claims/new", "description": "New claim form"}],
        "selectors": [
            {"screen": "New claim", "element": "Amount", "selector": "#amount"},
            {
                "screen": "Sign in", "element": "Submit", "selector": "[data-testid=\"submit\"]",
                "verified_at_runtime": "2026-01-01T00:00:00Z", "strategy": "data-testid",
            },
        ],
    }
    prompt = _build_prompt(_case(), context, "TCK-1-TC-01.spec.ts", "discovered.json", "https://app.test")

    assert "Already-verified locators (Knowledge Base)" in prompt
    assert "use them directly instead of rediscovering" in prompt
    assert "data-testid=\\\"submit\\\"" in prompt or '"submit"' in prompt


def test_build_prompt_omits_verified_kb_block_when_nothing_verified():
    context = {"routes": [{"path": "/claims/new", "description": "New claim form"}]}
    prompt = _build_prompt(_case(), context, "TCK-1-TC-01.spec.ts", "discovered.json", "https://app.test")
    assert "Already-verified locators" not in prompt


# -------------------------------------------- the call site actually passes it

def _stub_author_case_preconditions(monkeypatch, workspace_dir, driver: str):
    """Mock only author_case's preconditions, so run_agentic's kwargs stay real."""
    from app.services import project_config_service, spec_service

    monkeypatch.setattr(settings_store, "load_settings", lambda: {"browserDriver": driver})
    monkeypatch.setattr(claude_cli, "playwright_cli_available", lambda: True)
    monkeypatch.setattr(claude_cli, "browser_harness_available", lambda: True)
    monkeypatch.setattr(
        spec_service, "build_case_context",
        lambda *a, **k: {"baseUrl": "https://example.test", "projectKey": "P", "repo": ""},
    )
    monkeypatch.setattr(live_authoring_service, "_launch_browser", lambda *a, **k: SimpleNamespace(
        stdin=None, wait=lambda timeout=None: 0,
    ))
    monkeypatch.setattr(live_authoring_service, "_wait_cdp", lambda *a, **k: True)
    profile = project_config_service.auth_path("P", None).parent / "browser-profile"
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "Default").mkdir(exist_ok=True)


@pytest.mark.parametrize(
    "driver,heal,expect_agent,expect_skill",
    [
        ("playwright-cli", None, agents.PLAYWRIGHT_TEST_GENERATOR, None),
        ("playwright-cli", {"code": "x", "error": "boom"}, agents.PLAYWRIGHT_TEST_HEALER, None),
        ("browser-harness", None, None, "live-authoring"),
    ],
)
def test_author_case_passes_the_right_methodology_to_run_agentic(
    monkeypatch, workspace_dir, db_session, driver, heal, expect_agent, expect_skill
):
    """Pins the selection where it is actually consumed.

    `methodology_for` returning the right pair proves nothing on its own — the call
    site could drop it. A generation run that silently used the healer (or a heal
    that used the generator) would still return a spec and look green.
    """
    captured: dict = {}
    _stub_author_case_preconditions(monkeypatch, workspace_dir, driver)
    monkeypatch.setattr(
        claude_cli, "run_agentic",
        lambda prompt, **kw: (captured.update(kw), "done")[1],
    )

    live_authoring_service.author_case(
        db_session, _case(), SimpleNamespace(code="RUN-1", env="dev", owner_id=None),
        owner_id=None, run_id=None, heal=heal,
    )

    assert captured.get("agent") == expect_agent
    assert captured.get("skill") == expect_skill
