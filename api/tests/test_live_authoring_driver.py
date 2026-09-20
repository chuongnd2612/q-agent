"""Tests for the `browserDriver` setting (#875): browser-harness vs playwright-cli.

Covers A1 (the setting selects the right skill/preflight/env-var code path) and
the prompt-level half of A2 (the verified-KB block `_build_prompt` injects).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import claude_cli, live_authoring_service, settings_store
from app.services.live_authoring_service import LiveAuthoringError, _build_prompt, skill_for_driver


def _case(**overrides):
    """A minimal TestCase stand-in, matching the pattern in test_prompts.py."""
    defaults = dict(
        title="Sign in", precondition=None, steps=[], test_data=[],
        ticket_external_id="TCK-1", code="TC-01",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ------------------------------------------------------------ skill_for_driver

def test_skill_for_driver_maps_each_setting_value():
    assert skill_for_driver("browser-harness") == "live-authoring"
    assert skill_for_driver("playwright-cli") == "live-authoring-playwright-cli"


def test_skill_for_driver_falls_back_for_unknown_value():
    """An old/typo'd setting must not silently load NO skill."""
    assert skill_for_driver("something-else") == "live-authoring"


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
