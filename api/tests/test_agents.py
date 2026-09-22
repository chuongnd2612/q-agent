"""Agent definitions load, and `--agent`/`--agents` actually reach the CLI (#888).

The argv assertions here are deliberately paired with a live check (see
``test_the_cli_really_applies_an_inline_agent``): asserting only that the flag was
passed would keep passing if the CLI ignored it, which is exactly the class of
bug #884 was — a prompt describing a wiring that did not exist.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from app.config import settings
from app.services import agents, claude_cli


def test_every_declared_agent_is_present_on_disk():
    """A name in AGENTS with no file is a silent no-op at runtime."""
    for name in agents.AGENTS:
        assert (settings.agents_dir / f"{name}.md").exists(), f"{name}.md missing"


def test_the_image_ships_the_agent_definitions():
    """The check above passes in a checkout and still shipped a broken image (#891).

    `agents/` lives outside `api/`, so `COPY api/ ./` does not carry it — exactly
    like `skills/`, which needs its own COPY. Without one, `load_agent` finds
    nothing in the container and every role degrades to an agent-less call, warning
    to the log and otherwise looking fine. Assert the Dockerfile ships both.
    """
    from app.config import REPO_ROOT

    dockerfile = (REPO_ROOT / "api" / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY agents/" in dockerfile, "agents/ is not copied into the image"
    assert "COPY skills/" in dockerfile, "skills/ is not copied into the image"


def test_load_agent_strips_frontmatter_and_keeps_the_body():
    loaded = agents.load_agent(agents.PLAYWRIGHT_TEST_PLANNER)

    assert loaded is not None
    assert loaded["description"], "description should come from the frontmatter"
    # The frontmatter itself must not leak into the prompt the CLI receives.
    assert not loaded["prompt"].startswith("---")
    assert "name: playwright-test-planner" not in loaded["prompt"]
    assert loaded["prompt"].strip(), "body should survive the split"


def test_a_missing_agent_degrades_instead_of_raising():
    assert agents.load_agent("no-such-agent") is None
    assert agents.agents_json("no-such-agent") is None


def test_agents_json_has_the_shape_the_cli_documents():
    payload = json.loads(agents.agents_json(agents.PLAYWRIGHT_TEST_PLANNER))

    assert set(payload) == {agents.PLAYWRIGHT_TEST_PLANNER}
    assert set(payload[agents.PLAYWRIGHT_TEST_PLANNER]) == {"description", "prompt"}


def test_the_ported_agents_keep_the_locator_handoff():
    """The reason for porting these at all (#887).

    The planner records a `locator:` per step and the generator reuses it instead
    of re-`find`ing. The hand-written skill this replaces had zero of these, which
    is why generation re-discovered every element on every step.
    """
    planner = agents.load_agent(agents.PLAYWRIGHT_TEST_PLANNER)["prompt"]
    generator = agents.load_agent(agents.PLAYWRIGHT_TEST_GENERATOR)["prompt"]

    assert planner.count("locator:") >= 5, "planner must still record step locators"
    assert "locator:" in generator
    assert "straight to the command" in generator, "generator must still reuse, not re-find"


@pytest.mark.parametrize(
    "name", [agents.PLAYWRIGHT_TEST_PLANNER, agents.PLAYWRIGHT_TEST_GENERATOR, agents.PLAYWRIGHT_TEST_HEALER]
)
def test_agents_attach_to_the_prelaunched_browser_never_open_their_own(name):
    """`open` launches a *new*, signed-out browser and wants a `chrome` channel the
    API image does not have — the pre-authenticated Chrome must be attached to
    (#884). Pins the correction so a future edit cannot quietly reintroduce it."""
    prompt = agents.load_agent(name)["prompt"]

    assert 'attach --cdp "$PW_CLI_CDP_URL"' in prompt
    assert '-s="$PW_CLI_SESSION" open ' not in prompt


def test_run_prompt_passes_agent_and_inline_definition(monkeypatch):
    """The flags reach argv, and the definition travels inline rather than by path."""
    captured: dict[str, list[str]] = {}

    class _Proc:
        returncode = 0

        def communicate(self, timeout=None):
            return json.dumps({"result": "ok"}), ""

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(claude_cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(claude_cli, "_resolve_claude_env", lambda: ({}, None))

    claude_cli.run_prompt("hi", agent=agents.PLAYWRIGHT_TEST_PLANNER)

    cmd = captured["cmd"]
    assert "--agent" in cmd and agents.PLAYWRIGHT_TEST_PLANNER in cmd
    payload = json.loads(cmd[cmd.index("--agents") + 1])
    assert agents.PLAYWRIGHT_TEST_PLANNER in payload


def test_an_unknown_agent_leaves_the_call_unflagged(monkeypatch):
    """Degrade, don't fail: the caller's skill/prompt still runs."""
    captured: dict[str, list[str]] = {}

    class _Proc:
        returncode = 0

        def communicate(self, timeout=None):
            return json.dumps({"result": "ok"}), ""

    monkeypatch.setattr(
        claude_cli.subprocess, "Popen", lambda cmd, **kw: (captured.__setitem__("cmd", cmd), _Proc())[1]
    )
    monkeypatch.setattr(claude_cli, "_resolve_claude_env", lambda: ({}, None))

    claude_cli.run_prompt("hi", agent="no-such-agent")

    assert "--agent" not in captured["cmd"]


@pytest.mark.skipif(
    shutil.which(settings.claude_bin) is None or not os.environ.get("QAGENT_LIVE_CLAUDE"),
    reason="needs an authenticated Claude CLI; set QAGENT_LIVE_CLAUDE=1 to run",
)
def test_the_cli_really_applies_an_inline_agent(tmp_path):
    """Negative control for every argv assertion above.

    Those would all still pass if the CLI silently ignored `--agent`. This asks a
    throwaway inline agent to identify itself and checks the answer came back in
    its voice, which can only happen if the definition was actually applied.
    """
    definition = {
        "probe-agent": {
            "description": "probe",
            "prompt": "You are PROBE-AGENT-7. If asked your role, reply exactly: I am PROBE-AGENT-7.",
        }
    }
    proc = subprocess.run(  # noqa: S603
        [
            settings.claude_bin, "-p", "What is your assigned role? Answer in under 10 words.",
            "--agent", "probe-agent", "--agents", json.dumps(definition),
            "--output-format", "json",
        ],
        capture_output=True, text=True, timeout=180, cwd=tmp_path,
    )

    assert proc.returncode == 0, proc.stderr[:300]
    assert "PROBE-AGENT-7" in json.loads(proc.stdout).get("result", "")


# ------------------------------------------------- the ported prose matches THIS codebase

#: Conventions that exist only in the reference spike. Each one, left in an agent's
#: prompt, makes it instruct Claude to do something this pipeline does not support.
_SPIKE_ONLY = {
    ".auth/state.json": "Q-Agent has no .auth/ dir; the runner injects storageState into the CONFIG",
    "auth.setup.ts": "there is no setup project here",
    "tests_generated/": "spec paths come from spec_service.spec_filename, via the prompt",
    "state-save": "the captured manual login is managed outside the agent",
}


@pytest.mark.parametrize("name", sorted(agents.AGENTS))
def test_agents_carry_no_spike_only_conventions(name):
    """#901: the ported agents told Claude to declare `storageState: '.auth/state.json'`.

    Q-Agent's saved manual login is resolved at run time and injected into the
    generated Playwright *config* as an ABSOLUTE path (`playwright_runner`, and see
    `capture_storage_state`). A spec that declares its own relative `storageState`
    overrides that with a file this codebase never writes, so the run loses the
    sign-in it depends on. Every gate was green for this, because nothing else
    asserts what the prompts actually say.
    """
    prompt = agents.load_agent(name)["prompt"]

    for needle, why in _SPIKE_ONLY.items():
        assert needle not in prompt, f"{name} still references {needle!r} — {why}"


@pytest.mark.parametrize("name", sorted(agents.AGENTS))
def test_agents_state_that_auth_is_handled_outside_the_spec(name):
    """The positive half: removing the bad instruction is not enough if the agent is
    left silent and improvises a sign-in instead."""
    prompt = agents.load_agent(name)["prompt"]

    assert "captured" in prompt.lower(), f"{name} should say the browser is already signed in"
