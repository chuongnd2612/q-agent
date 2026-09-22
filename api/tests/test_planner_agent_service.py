"""Tests for the planner-agent run in live-planner mode (#889).

Covers the three things the slice actually claims: the sidecar — not the
Markdown — is the source of truth, the run really goes through the
``playwright-test-planner`` AGENT wired to the shared browser session, and every
failure mode degrades to ``None`` (text-only) rather than failing a run.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.services import agents, planner_agent_service


def _plan_payload() -> dict:
    return {
        "overview": "Docs site.",
        "auth": "none",
        "scenarios": [
            {
                "title": "Reach the installation docs",
                "steps": [
                    {
                        "action": 'Click the "Docs" link.',
                        "locator": "getByRole('link', { name: 'Docs' })",
                        "expect": "The Installation page renders.",
                        "expectLocator": "getByRole('heading', { name: 'Installation' })",
                    }
                ],
            }
        ],
        "routes": [{"path": "/docs/intro", "description": "Docs landing"}],
        "selectors": [
            {"screen": "Docs", "element": "Search box", "selector": "getByPlaceholder('Search')"}
        ],
    }


# ------------------------------------------------------------------ normalize_plan
def test_normalize_plan_keeps_steps_locators_routes_and_selectors():
    plan = planner_agent_service.normalize_plan(_plan_payload())
    assert plan is not None
    step = plan["scenarios"][0]["steps"][0]
    assert step["action"] == 'Click the "Docs" link.'
    assert step["locator"] == "getByRole('link', { name: 'Docs' })"
    assert step["expectLocator"] == "getByRole('heading', { name: 'Installation' })"
    assert plan["routes"] == [{"path": "/docs/intro", "description": "Docs landing"}]
    assert plan["selectors"][0]["strategy"] == planner_agent_service.LOCATOR_STRATEGY


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "not a dict",
        {},
        {"scenarios": []},
        {"scenarios": [{"title": "Empty", "steps": []}]},
        {"scenarios": [{"title": "No action", "steps": [{"expect": "something"}]}]},
    ],
)
def test_normalize_plan_returns_none_for_unusable_payloads(raw):
    """No usable scenario → None, i.e. the caller grounds on nothing rather than noise."""
    assert planner_agent_service.normalize_plan(raw) is None


def test_normalize_plan_caps_scenarios_and_steps():
    raw = {
        "scenarios": [
            {"title": f"S{i}", "steps": [{"action": f"a{j}"} for j in range(40)]}
            for i in range(30)
        ]
    }
    plan = planner_agent_service.normalize_plan(raw)
    assert len(plan["scenarios"]) == planner_agent_service.MAX_SCENARIOS
    assert len(plan["scenarios"][0]["steps"]) == planner_agent_service.MAX_STEPS_PER_SCENARIO


# ------------------------------------------------------------------ plan_discovery
def test_plan_discovery_harvests_step_and_expect_locators():
    """Step-bound locators are the point of the handoff — they must reach the KB."""
    plan = planner_agent_service.normalize_plan(_plan_payload())
    discovery = planner_agent_service.plan_discovery(plan)
    selectors = {s["selector"] for s in discovery["selectors"]}
    assert selectors == {
        "getByPlaceholder('Search')",
        "getByRole('link', { name: 'Docs' })",
        "getByRole('heading', { name: 'Installation' })",
    }
    assert all(s["strategy"] == planner_agent_service.LOCATOR_STRATEGY for s in discovery["selectors"])
    assert [r["path"] for r in discovery["routes"]] == ["/docs/intro"]


def test_plan_discovery_dedupes_repeated_locators():
    raw = _plan_payload()
    raw["scenarios"].append(dict(raw["scenarios"][0], title="Again"))
    plan = planner_agent_service.normalize_plan(raw)
    discovery = planner_agent_service.plan_discovery(plan)
    assert len(discovery["selectors"]) == 3


# ------------------------------------------------------------------ merge_plan_to_kb
def test_merge_plan_to_kb_stamps_the_planner_source(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(
        planner_agent_service,
        "merge_verified_discovery",
        lambda pk, repo, discovered, *, owner_id=None, source="exploration": calls.append(
            {"pk": pk, "repo": repo, "discovered": discovered, "owner_id": owner_id, "source": source}
        )
        or 2,
    )
    plan = planner_agent_service.normalize_plan(_plan_payload())
    merged = planner_agent_service.merge_plan_to_kb(
        plan, {"projectKey": "surency", "repo": "web"}, owner_id=7
    )
    assert merged == 2
    assert calls[0]["source"] == planner_agent_service.KB_SOURCE
    assert calls[0]["owner_id"] == 7


def test_merge_plan_to_kb_skips_without_project_key(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("must not write to the KB without a project key")

    monkeypatch.setattr(planner_agent_service, "merge_verified_discovery", _boom)
    plan = planner_agent_service.normalize_plan(_plan_payload())
    assert planner_agent_service.merge_plan_to_kb(plan, {}, owner_id=None) == 0
    assert planner_agent_service.merge_plan_to_kb(None, {"projectKey": "p"}, owner_id=None) == 0


def test_merge_plan_to_kb_swallows_a_kb_failure(monkeypatch):
    """KB enrichment is additive — a write failure must never propagate."""

    def _raise(*a, **k):
        raise RuntimeError("knowledge row locked")

    monkeypatch.setattr(planner_agent_service, "merge_verified_discovery", _raise)
    plan = planner_agent_service.normalize_plan(_plan_payload())
    assert planner_agent_service.merge_plan_to_kb(plan, {"projectKey": "p"}, owner_id=None) == 0


# ------------------------------------------------------------------ plan_ticket
@pytest.fixture
def planner_run(monkeypatch, tmp_path):
    """Stub out the browser + CLI so ``plan_ticket`` can be driven in-process.

    Records what the agentic call was given, so a test can pin the BRANCH that
    ran (the real ``playwright-test-planner`` agent, against the shared browser
    session's env) and not merely that something returned a plan.

    ``executionTarget`` is pinned to ``server`` here (via the sanctioned
    ``settings_override``, not a bare ``save_settings``) because this file tests
    the IN-PROCESS path. Without it these tests read ``settings_store.DEFAULTS``
    — which ships ``executionTarget="local-agent"`` deliberately (#161) — since
    the temp workspace holds no ``settings.json``, and every one of them would
    silently take the #900 device-dispatch branch instead. That is exactly the
    #573 failure mode: green-looking tests that never enter the code they claim
    to cover.
    """
    from app.config import settings

    from tests.conftest import settings_override

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    monkeypatch.setattr(planner_agent_service.claude_cli, "playwright_cli_available", lambda: True)

    launched: list[dict] = []

    @contextmanager
    def _fake_session(base_url, profile_dir, name, *, browser_driver="playwright-cli"):
        launched.append(
            {"base_url": base_url, "profile_dir": profile_dir, "name": name, "driver": browser_driver}
        )
        yield {
            "PW_CLI_CDP_URL": "http://127.0.0.1:9999",
            "PW_CLI_SESSION": name,
            "PLAYWRIGHT_CLI_JS": "/cli.js",
        }

    monkeypatch.setattr(planner_agent_service.agentic_browser, "browser_session", _fake_session)

    calls: list[dict] = []

    def _fake_agentic(prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        payload = _fake_agentic.payload
        if payload is not None:
            (kwargs["workspace_dir"] / planner_agent_service.SIDECAR_NAME).write_text(
                payload, encoding="utf-8"
            )
        return "done"

    _fake_agentic.payload = json.dumps(_plan_payload())
    monkeypatch.setattr(planner_agent_service.claude_cli, "run_agentic", _fake_agentic)
    with settings_override(executionTarget="server"):
        yield SimpleNamespace(launched=launched, calls=calls, agentic=_fake_agentic)


_RUN = SimpleNamespace(code="RUN-1")
_TICKET = SimpleNamespace(external_id="SUR-1428", title="Add password reset flow")
_CONTEXT = {"projectKey": "surency", "repo": "web", "baseUrl": "https://app.test"}
_TARGET = {"ticket": "SUR-1428", "screen": "Add password reset flow", "goal": "Reach the reset screen"}


def test_plan_ticket_runs_the_planner_agent_over_the_shared_browser_session(planner_run):
    plan = planner_agent_service.plan_ticket(
        _RUN, _TICKET, _CONTEXT, owner_id=1, target=_TARGET
    )

    # The branch that ran: the real agent, not a skill, over a playwright-cli session.
    assert len(planner_run.calls) == 1
    call = planner_run.calls[0]
    assert call["agent"] == agents.PLAYWRIGHT_TEST_PLANNER
    assert call.get("skill") is None
    assert set(call["extra_env"]) == {"PW_CLI_CDP_URL", "PW_CLI_SESSION", "PLAYWRIGHT_CLI_JS"}
    assert planner_run.launched[0]["base_url"] == "https://app.test"
    assert planner_run.launched[0]["driver"] == "playwright-cli"

    # The prompt asks for BOTH artifacts, and the ticket's own text is the target.
    assert planner_agent_service.SIDECAR_NAME in call["prompt"]
    assert planner_agent_service.PLAN_MD_NAME in call["prompt"]
    assert "Reach the reset screen" in call["prompt"]

    # And the sidecar — not the Markdown — is what came back.
    assert plan["scenarios"][0]["steps"][0]["locator"] == "getByRole('link', { name: 'Docs' })"


def test_plan_ticket_returns_none_without_a_base_url(planner_run):
    def _boom(*a, **k):
        raise AssertionError("no browser may be launched without a base_url")

    planner_run.agentic.payload = None
    assert (
        planner_agent_service.plan_ticket(
            _RUN, _TICKET, {"projectKey": "surency"}, owner_id=1, target=_TARGET
        )
        is None
    )
    assert planner_run.calls == []


def test_plan_ticket_returns_none_without_playwright_cli(planner_run, monkeypatch):
    monkeypatch.setattr(planner_agent_service.claude_cli, "playwright_cli_available", lambda: False)
    assert (
        planner_agent_service.plan_ticket(_RUN, _TICKET, _CONTEXT, owner_id=1, target=_TARGET) is None
    )
    assert planner_run.calls == []


def test_plan_ticket_returns_none_when_the_agentic_run_raises(planner_run, monkeypatch):
    def _raise(*a, **k):
        raise RuntimeError("Claude CLI exited 1")

    monkeypatch.setattr(planner_agent_service.claude_cli, "run_agentic", _raise)
    assert (
        planner_agent_service.plan_ticket(_RUN, _TICKET, _CONTEXT, owner_id=1, target=_TARGET) is None
    )


def test_plan_ticket_returns_none_when_no_sidecar_was_written(planner_run):
    """The Markdown plan alone is NOT enough — the JSON sidecar is the contract."""
    planner_run.agentic.payload = None
    assert (
        planner_agent_service.plan_ticket(_RUN, _TICKET, _CONTEXT, owner_id=1, target=_TARGET) is None
    )
    assert len(planner_run.calls) == 1  # it really did run; only the artifact was missing


def test_plan_ticket_returns_none_on_a_malformed_sidecar(planner_run):
    planner_run.agentic.payload = "{ not json,,, "
    assert (
        planner_agent_service.plan_ticket(_RUN, _TICKET, _CONTEXT, owner_id=1, target=_TARGET) is None
    )


def test_plan_ticket_ignores_a_stale_sidecar_from_a_previous_attempt(planner_run):
    """A failed rerun must not silently resurrect the last run's plan."""
    planner_agent_service.plan_ticket(_RUN, _TICKET, _CONTEXT, owner_id=1, target=_TARGET)
    planner_run.agentic.payload = None
    assert (
        planner_agent_service.plan_ticket(_RUN, _TICKET, _CONTEXT, owner_id=1, target=_TARGET) is None
    )
