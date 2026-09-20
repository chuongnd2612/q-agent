"""Tests for self-heal -> Knowledge Base selector feedback (#182).

Unit-tests the diffing/wiring helpers directly (``_selector_literals`` and
``_propose_healed_selector_to_kb``) rather than driving the full background
heal loop, since that loop's failure/success wiring is exercised elsewhere by
``tests/test_automation.py``'s ``test_heal_*`` tests.
"""

from __future__ import annotations

import threading
import time

from app.services import knowledge_service, playwright_runner


def test_selector_literals_extracts_locator_and_testid():
    code = (
        "await page.locator('#old-btn').click();\n"
        "await page.getByTestId(\"submit\").click();\n"
    )
    assert playwright_runner._selector_literals(code) == {"#old-btn", "submit"}


def test_selector_literals_empty_for_no_selectors():
    assert playwright_runner._selector_literals("await page.goto('/login');\n") == set()


def test_propose_healed_selector_to_kb_calls_knowledge_service_on_single_swap(monkeypatch):
    calls = []
    monkeypatch.setattr(
        knowledge_service,
        "propose_selector_fix",
        lambda *a, **k: calls.append(a) or True,
    )
    before = "await page.locator('#old-btn').click();\n"
    after = "await page.locator('#new-btn').click();\n"

    playwright_runner._propose_healed_selector_to_kb("Surency Platform", "org/web", before, after, None)

    assert calls == [("Surency Platform", "org/web", "#old-btn", "#new-btn", None)]


def test_propose_healed_selector_to_kb_skips_ambiguous_diff(monkeypatch):
    """More than one selector changed at once -> too ambiguous to propose; skipped."""
    calls = []
    monkeypatch.setattr(
        knowledge_service, "propose_selector_fix", lambda *a, **k: calls.append(a) or True
    )
    before = "await page.locator('#a').click();\nawait page.locator('#b').click();\n"
    after = "await page.locator('#c').click();\n"

    playwright_runner._propose_healed_selector_to_kb("Surency Platform", "", before, after, None)

    assert calls == []


def test_propose_healed_selector_to_kb_noop_when_no_diff(monkeypatch):
    calls = []
    monkeypatch.setattr(
        knowledge_service, "propose_selector_fix", lambda *a, **k: calls.append(a) or True
    )
    same = "await page.locator('#a').click();\n"

    playwright_runner._propose_healed_selector_to_kb("Surency Platform", "", same, same, None)

    assert calls == []


def test_propose_healed_selector_to_kb_noop_without_project_key(monkeypatch):
    calls = []
    monkeypatch.setattr(
        knowledge_service, "propose_selector_fix", lambda *a, **k: calls.append(a) or True
    )

    playwright_runner._propose_healed_selector_to_kb(None, "", "before", "after", None)

    assert calls == []


def test_propose_healed_selector_to_kb_never_raises_on_kb_failure(monkeypatch):
    """Best-effort: even if knowledge_service explodes, the heal loop must not crash."""

    def boom(*a, **k):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(knowledge_service, "propose_selector_fix", boom)
    before = "await page.locator('#old-btn').click();\n"
    after = "await page.locator('#new-btn').click();\n"

    # Must not raise.
    playwright_runner._propose_healed_selector_to_kb("Surency Platform", "", before, after, None)


# --- Heal->KB DOM enrichment wiring (#249) -----------------------------------


def test_merge_discovered_dom_to_kb_passes_route_and_spec_selectors(monkeypatch):
    """The route comes from the captured DOM; selectors from the passing spec's literals."""
    calls = []
    monkeypatch.setattr(
        knowledge_service, "merge_discovered_dom", lambda *a, **k: calls.append(a) or 2
    )
    passing_code = "await page.goto('/login');\nawait page.locator('#login-submit').click();\n"
    dom = {"path": "/login", "elements": []}

    playwright_runner._merge_discovered_dom_to_kb("P", "org/web", passing_code, dom, None)

    assert calls == [("P", "org/web", {"route": "/login", "selectors": ["#login-submit"]}, None)]


def test_merge_discovered_dom_to_kb_noop_without_project_or_dom(monkeypatch):
    calls = []
    monkeypatch.setattr(
        knowledge_service, "merge_discovered_dom", lambda *a, **k: calls.append(a) or 0
    )

    playwright_runner._merge_discovered_dom_to_kb(None, "", "code", {"path": "/x"}, None)
    playwright_runner._merge_discovered_dom_to_kb("P", "", "code", None, None)

    assert calls == []


def test_merge_discovered_dom_to_kb_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(knowledge_service, "merge_discovered_dom", boom)
    # Must not raise.
    playwright_runner._merge_discovered_dom_to_kb(
        "P", "", "await page.locator('#a').click();\n", {"path": "/a"}, None
    )


# ---------------------------------------------------------------------------
# heal_run_busy / start_heal serialization (#876) — two cases in the same run
# share the run's spec dir/report.json, so heals must never run concurrently,
# regardless of whether one of them takes the new live-rehearsal path.
# ---------------------------------------------------------------------------


def test_start_heal_rejects_a_second_case_while_the_run_is_busy(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def fake_heal_spec(case_id):
        started.set()
        release.wait(timeout=5)

    monkeypatch.setattr(playwright_runner, "heal_spec", fake_heal_spec)
    playwright_runner._healing.clear()
    try:
        assert playwright_runner.start_heal(case_id=1, run_id=100) is True
        assert started.wait(timeout=5), "heal_spec never ran on the background thread"
        assert playwright_runner.heal_run_busy(100) is True

        # A second case in the SAME run is rejected while the first is healing —
        # true regardless of which fix mechanism (live rehearsal or static) the
        # first case's heal is using.
        assert playwright_runner.start_heal(case_id=2, run_id=100) is False
        # A case in a DIFFERENT run is unaffected.
        assert playwright_runner.start_heal(case_id=3, run_id=101) is True
        # Re-starting the SAME case that is already healing is idempotently True.
        assert playwright_runner.start_heal(case_id=1, run_id=100) is True
    finally:
        release.set()
        for _ in range(50):
            if not playwright_runner._healing:
                break
            time.sleep(0.05)
        playwright_runner._healing.clear()
