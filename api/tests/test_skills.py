"""Tests for dedicated-skill injection into Claude CLI actions."""

from __future__ import annotations

import json
import re
from pathlib import Path

from conftest import FakePopen

from app.services import claude_cli, skills

# ---------------------------------------------------------------------------
# Guardrail (#184): skills and prompts drifting apart unnoticed was the root
# cause of the biggest quality gaps in the architecture review — two reviewer
# skills (test-case-reviewer, automation-reviewer) were fully authored with
# zero call sites. This catches a newly-orphaned skill in CI.
# ---------------------------------------------------------------------------

# api/app — every service/router source file lives under here.
_APP_DIR = Path(skills.__file__).resolve().parent.parent
_SKILLS_MODULE_FILE = Path(skills.__file__).resolve()

# Skills registered in SKILLS that are intentionally not (yet) called from
# app/ — e.g. designed for direct/manual invocation, or awaiting a future
# wiring issue. Keep this list short and each entry commented with why;
# anything else orphaned fails the guardrail below.
ALLOWLISTED_ORPHAN_SKILLS = {
    # Aggregates the whole pipeline into a stakeholder-facing QA report.
    # report_service.build_report() only calls EXECUTION_ANALYZER today —
    # wiring report-generator in is a separate, not-yet-filed enhancement.
    skills.REPORT_GENERATOR,
}


def _constant_name_for_skill(skill_value: str) -> str | None:
    """The module-level UPPER_CASE constant in app.services.skills whose value
    is ``skill_value`` (e.g. "test-case-reviewer" -> "TEST_CASE_REVIEWER").

    Callers reference skills by this Python identifier (``skill=AUTOMATION_REVIEWER``),
    never by the raw string, so finding call sites means searching for the name.
    """
    for name, value in vars(skills).items():
        if name.isupper() and value == skill_value:
            return name
    return None


def _has_call_site(skill_value: str) -> bool:
    """Whether any file under ``api/app`` (other than ``skills.py`` itself)
    references this skill's constant identifier."""
    const_name = _constant_name_for_skill(skill_value)
    if const_name is None:
        return False
    pattern = re.compile(rf"\b{re.escape(const_name)}\b")
    for py_file in _APP_DIR.rglob("*.py"):
        if py_file.resolve() == _SKILLS_MODULE_FILE:
            continue
        if pattern.search(py_file.read_text(encoding="utf-8")):
            return True
    return False


def test_every_registered_skill_has_a_call_site_or_is_allowlisted():
    """Every skill in ``skills.SKILLS`` must be either used somewhere under
    ``api/app`` or explicitly allowlisted as intentionally manual/unwired —
    a newly-orphaned skill (registered but never called) fails this test."""
    orphaned = sorted(
        s for s in skills.SKILLS
        if s not in ALLOWLISTED_ORPHAN_SKILLS and not _has_call_site(s)
    )
    assert orphaned == [], (
        f"Orphaned skill(s) with no call site under api/app and not allowlisted: "
        f"{orphaned}. Wire it into a service/router, or add it to "
        "ALLOWLISTED_ORPHAN_SKILLS with a reason."
    )


def test_allowlisted_orphan_skills_are_a_subset_of_registered_skills():
    """The allowlist can only reference real, registered skills (catches typos
    and stale entries left behind after a skill IS wired up)."""
    assert ALLOWLISTED_ORPHAN_SKILLS <= skills.SKILLS


def test_has_call_site_detects_a_real_wired_skill():
    assert _has_call_site(skills.TEST_CASE_REVIEWER) is True


def test_has_call_site_false_for_unregistered_skill_name():
    assert _has_call_site("not-a-real-skill") is False


def test_load_skill_returns_methodology():
    text = skills.load_skill(skills.REQUIREMENT_ANALYST)
    assert text and "Requirement Analyst" in text
    assert "dedicated Q-Agent skill" in text  # our injected preamble


def test_load_skill_missing_returns_none():
    assert skills.load_skill("does-not-exist-skill") is None


def test_run_prompt_injects_skill_as_system(monkeypatch, shared_claude_credential):
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakePopen(returncode=0, stdout=json.dumps({"result": "ok"}), stderr="")

    monkeypatch.setattr(claude_cli.subprocess, "Popen", fake_popen)

    out = claude_cli.run_prompt("hello", skill=skills.TEST_CASE_GENERATOR)
    assert out == "ok"
    cmd = captured["cmd"]
    assert "--append-system-prompt" in cmd
    injected = cmd[cmd.index("--append-system-prompt") + 1]
    assert "Test Case Generator" in injected


# ---------------------------------------------------------------------------
# Guardrail (#542, reversing #178): the SKILL and the generation prompt must
# describe ONE achievable spec shape. #178 was closed because the skill demanded
# reuse the prompt forbade; this asserts they no longer disagree — in either
# direction.
# ---------------------------------------------------------------------------


def _generation_prompt() -> str:
    from types import SimpleNamespace

    from app.services.spec_service import _build_prompt

    return _build_prompt(
        SimpleNamespace(
            title="Pay an invoice", precondition=None, steps=[], test_data=[],
            ticket_external_id="ADO-1", code="TC-01",
        )
    )


def test_automation_generator_skill_and_prompt_agree_on_the_layered_shape():
    skill = skills.load_skill(skills.AUTOMATION_GENERATOR, include_template=True)
    prompt = _generation_prompt()
    assert skill
    for text in (skill, prompt):
        assert "@q-agent/playwright-base" in text
        # the real spec depth: tests/<TICKET>/<spec>.spec.ts -> ../../pages
        assert "../../pages" in text
    # The old standalone mandate is gone from both halves.
    assert "no other imports" not in skill
    assert "import { test, expect } from '@playwright/test';" not in skill


def test_automation_generator_skill_forbids_inventing_page_object_imports():
    """Page objects arrive in #544/#545. Until then the skill must permit an asset
    import only where a reference spec proves the file exists."""
    skill = skills.load_skill(skills.AUTOMATION_GENERATOR, include_template=True)
    assert skill
    assert "REFERENCE SPEC" in skill.upper()
    assert "Never invent" in skill


def test_live_authoring_skill_matches_the_same_contract():
    skill = (
        Path(skills.settings.skills_dir) / "live-authoring" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "@q-agent/playwright-base" in skill
    assert "No inline login" in skill
    # the discovery sidecar survives the rewrite
    assert "discovery sidecar" in skill and '"selectors"' in skill


def test_run_prompt_merges_skill_and_explicit_system(monkeypatch, shared_claude_credential):
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakePopen(returncode=0, stdout=json.dumps({"result": "x"}), stderr="")

    monkeypatch.setattr(claude_cli.subprocess, "Popen", fake_popen)

    claude_cli.run_prompt("p", system="EXTRA_SYS", skill=skills.AUTOMATION_GENERATOR)
    injected = captured["cmd"][captured["cmd"].index("--append-system-prompt") + 1]
    assert "Automation Generator" in injected and "EXTRA_SYS" in injected
