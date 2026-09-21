"""Run the Playwright **planner agent** against the live app (#889, part of #887).

This is the Planner step of ``testCaseMode="live-planner"``. It replaces the
per-step observe→decide→act exploration loop (#877) that preceded it: measured
against playwright.dev with real credentials, that loop stopped at ``repeat``
after three steps without ever reaching ``done`` and cost ~$0.116 *per decide
call*, while the ported ``playwright-test-planner`` agent (#888) completed the
same goal for $0.268 total and produced a plan whose every step carries the
Playwright locator it was actually performed with.

Two artifacts come back from one agentic run, exactly mirroring
:mod:`app.services.live_authoring_service`'s spec + ``discovered.json`` contract:

* ``<feature>.plan.md`` — the human-readable plan the agent already knows how to
  write, kept for the audit trail.
* ``plan.json`` — a **JSON sidecar of the same structure**, which is the only
  thing this module reads. The Markdown is deliberately NOT the source of truth:
  prose drifts, and a Markdown plan-splitter needs regex fallbacks to survive it.

What the plan feeds:

* the ``test-case-generator`` prompt (see
  :func:`app.services.prompts.render_planner_plan`), so the persisted ``TestCase``
  rows keep today's ADO metadata while their ``steps: [{a, e}]`` come from what
  was *observed* rather than guessed from ticket text; and
* the Knowledge Base, via :func:`merge_plan_to_kb`, so the locators land as
  ``verified_at_runtime`` selectors that spec generation later picks up through
  ``render_project_context``'s ``_verified_first()``.

Locators deliberately do NOT travel on ``TestCase.steps``: that schema is
``{a, e}`` only, and both ``TestStep`` (``routers/review.py``) and
``ai_service._case_kwargs_from_raw`` reconstruct steps with just those two keys,
so any extra field is dropped the moment a QC edits the case (#882). The KB is
the channel.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.config import settings
from app.logging import logger
from app.services import (
    agentic_browser,
    agents,
    claude_cli,
    project_config_service,
    settings_store,
)
from app.services.knowledge_service import merge_verified_discovery

#: Sidecar filename the planner agent is told to write next to its Markdown plan.
SIDECAR_NAME = "plan.json"

#: Basename of the Markdown plan (the agent writes it under ``specs/``).
PLAN_MD_NAME = "specs/ticket.plan.md"

#: Provenance stamp for KB entries merged from a planner run.
KB_SOURCE = "planner-agent"

#: What a locator expression is recorded as in the KB's ``strategy`` field. The
#: planner emits Playwright locator *expressions* (``getByRole('link', …)``), not
#: CSS — labelling them honestly keeps a later consumer from pasting one into a
#: ``querySelector``.
LOCATOR_STRATEGY = "playwright"

#: Keeps a plan from blowing the generator prompt out. A ticket that genuinely
#: needs more than this many scenarios is over-scoped for one run.
MAX_SCENARIOS = 12
MAX_STEPS_PER_SCENARIO = 20

_WORKSPACE_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_dirname(value: str) -> str:
    """Sanitise a ticket/run identifier for use as a directory name."""
    return _WORKSPACE_SAFE_RE.sub("-", value).strip("-")[:64] or "ticket"


def _as_text(value: Any) -> str:
    """Coerce a sidecar field to a trimmed string (the agent may emit non-strings)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def normalize_plan(raw: Any) -> dict | None:
    """Coerce the planner's ``plan.json`` into this module's internal contract.

    A sidecar written by a model is only as well-formed as the run that wrote it,
    so every field is defensive: unknown keys are ignored, missing ones default,
    and a payload with no usable scenario at all returns ``None`` (the caller then
    falls back to text-only generation rather than grounding on nothing).

    Args:
        raw: The parsed ``plan.json`` contents.

    Returns:
        ``{"overview", "auth", "scenarios": [{"title", "steps": [{"action",
        "locator", "expect", "expectLocator"}]}], "routes", "selectors"}``, or
        ``None`` when the payload carries no scenario with at least one step.
    """
    if not isinstance(raw, dict):
        return None

    scenarios: list[dict] = []
    for entry in (raw.get("scenarios") or [])[:MAX_SCENARIOS]:
        if not isinstance(entry, dict):
            continue
        steps: list[dict] = []
        for step in (entry.get("steps") or [])[:MAX_STEPS_PER_SCENARIO]:
            if not isinstance(step, dict):
                continue
            action = _as_text(step.get("action") or step.get("a"))
            if not action:
                continue
            steps.append(
                {
                    "action": action,
                    "locator": _as_text(step.get("locator")),
                    "expect": _as_text(step.get("expect") or step.get("e")),
                    "expectLocator": _as_text(step.get("expectLocator")),
                }
            )
        if not steps:
            continue
        scenarios.append({"title": _as_text(entry.get("title")) or "Untitled scenario", "steps": steps})

    if not scenarios:
        return None

    routes = [
        {
            "path": _as_text(r.get("path") or r.get("url")),
            "description": _as_text(r.get("description")) or "observed by the planner agent",
        }
        for r in (raw.get("routes") or [])
        if isinstance(r, dict) and _as_text(r.get("path") or r.get("url"))
    ]

    selectors = [
        {
            "screen": _as_text(s.get("screen")),
            "element": _as_text(s.get("element")) or _as_text(s.get("selector") or s.get("locator")),
            "selector": _as_text(s.get("selector") or s.get("locator")),
            "strategy": _as_text(s.get("strategy")) or LOCATOR_STRATEGY,
        }
        for s in (raw.get("selectors") or [])
        if isinstance(s, dict) and _as_text(s.get("selector") or s.get("locator"))
    ]

    return {
        "overview": _as_text(raw.get("overview")),
        "auth": _as_text(raw.get("auth")),
        "scenarios": scenarios,
        "routes": routes,
        "selectors": selectors,
    }


def plan_discovery(plan: dict) -> dict[str, list[dict]]:
    """Project a normalized plan onto the KB's runtime-verified discovery shape.

    Every ``locator:`` the planner recorded — on a step and on an ``expect:`` —
    was generated against the live DOM, so each is a runtime-verified fact about
    a real element. They are harvested here *in addition to* whatever the sidecar
    listed under ``selectors``, because the step-bound locators are the ones the
    planner is actually reliable about (it used them) and dropping them would
    throw away the whole point of the handoff.

    Args:
        plan: A plan as returned by :func:`normalize_plan`.

    Returns:
        ``{"routes": [{path, description}], "selectors": [{screen, element,
        selector, strategy}]}`` — deduped, ready for
        :func:`knowledge_service.merge_verified_discovery`.
    """
    routes: list[dict] = []
    seen_paths: set[str] = set()
    for route in plan.get("routes") or []:
        path = route.get("path", "")
        if path and path not in seen_paths:
            seen_paths.add(path)
            routes.append(route)

    selectors: list[dict] = []
    seen_selectors: set[str] = set()

    def _add(screen: str, element: str, selector: str) -> None:
        if not selector or selector in seen_selectors:
            return
        seen_selectors.add(selector)
        selectors.append(
            {
                "screen": screen,
                "element": element or selector,
                "selector": selector,
                "strategy": LOCATOR_STRATEGY,
            }
        )

    for entry in plan.get("selectors") or []:
        _add(entry.get("screen", ""), entry.get("element", ""), entry.get("selector", ""))
    for scenario in plan.get("scenarios") or []:
        screen = scenario.get("title", "")
        for step in scenario.get("steps") or []:
            _add(screen, step.get("action", ""), step.get("locator", ""))
            _add(screen, step.get("expect", ""), step.get("expectLocator", ""))

    return {"routes": routes, "selectors": selectors}


def merge_plan_to_kb(plan: dict | None, context: dict, *, owner_id: int | None) -> int:
    """Merge a plan's observed routes/locators into the Knowledge Base.

    Stamped ``verified_at_runtime`` with ``source="planner-agent"``, so later spec
    generation prefers them over source-inferred entries (ADR 0010 §6) through
    ``render_project_context``'s ``_verified_first()``.

    Args:
        plan: A normalized plan, or ``None`` (nothing to merge).
        context: The resolved project context (for ``projectKey``/``repo``).
        owner_id: Owning user id, scoping the knowledge row (ADR 0009).

    Returns:
        The number of KB entries merged or upgraded (0 on nothing to do, or on a
        failure — KB enrichment is additive and never fails generation).
    """
    if not plan or not context.get("projectKey"):
        return 0
    discovery = plan_discovery(plan)
    if not discovery["routes"] and not discovery["selectors"]:
        return 0
    try:
        return merge_verified_discovery(
            context.get("projectKey") or "",
            context.get("repo") or "",
            discovery,
            owner_id=owner_id,
            source=KB_SOURCE,
        )
    except Exception as exc:  # noqa: BLE001 - KB enrichment is additive/best-effort
        logger.warning("Planner KB merge skipped: {}", exc)
        return 0


def build_planner_prompt(target: dict[str, str], base_url: str, context: dict) -> str:
    """Build the planner agent's task prompt.

    The agent definition (``agents/playwright-test-planner.md``) already carries
    the methodology — how to drive ``playwright-cli``, how to record a ``locator:``
    per step and per ``expect:``. This prompt supplies only the target (the
    ticket's own text, since no test case exists yet), the credentials, and the
    **two** deliverables — the Markdown plan the agent writes by default, plus the
    JSON sidecar this service actually reads.

    Args:
        target: ``{"ticket", "screen", "goal"}`` derived from the ticket.
        base_url: The project's resolved base URL.
        context: The resolved project context (for test accounts).

    Returns:
        The task prompt.
    """
    accounts = context.get("testAccounts") or []
    cred_lines = "\n".join(
        f"- role={a.get('role', '')} username={a.get('username', '')} password={a.get('password', '')}"
        for a in accounts
    ) or "(no test accounts in context — reuse the browser's existing session)"
    return (
        "Plan the manual test scenarios for the work item below by exploring the REAL, "
        "running application. Every step you write must be one you performed, and every "
        "expected result one you saw.\n\n"
        f"## Work item\n"
        f"Ticket: {target.get('ticket', '')}\n"
        f"Screen / feature: {target.get('screen', '')}\n"
        f"Goal: {target.get('goal', '')}\n\n"
        f"## Application\n"
        f"Base URL: {base_url}\n"
        f"Test accounts:\n{cred_lines}\n\n"
        f"## Deliverables — write BOTH files into the current working directory\n"
        f"1. `{PLAN_MD_NAME}` — the Markdown test plan in the structure your agent "
        f"definition specifies (numbered steps, a `locator:` per step and per `expect:` "
        f"that targets a different element).\n"
        f"2. `{SIDECAR_NAME}` — the SAME plan as JSON. This is the machine-readable "
        "handoff and the only file downstream tooling reads, so it must be complete "
        "even where the Markdown summarises. Exact shape:\n"
        "```json\n"
        "{\n"
        '  "overview": "one paragraph on what this feature does",\n'
        '  "auth": "storage state | none - this scenario tests signing in",\n'
        '  "scenarios": [\n'
        "    {\n"
        '      "title": "Scenario title",\n'
        '      "steps": [\n'
        "        {\n"
        '          "action": "the user-level step you performed",\n'
        '          "locator": "getByRole(\'link\', { name: \'Docs\' })",\n'
        '          "expect": "the observable outcome you saw",\n'
        '          "expectLocator": "getByRole(\'heading\', { name: \'Installation\' })"\n'
        "        }\n"
        "      ]\n"
        "    }\n"
        "  ],\n"
        '  "routes": [{"path": "/docs/intro", "description": "Docs landing page"}],\n'
        '  "selectors": [{"screen": "Docs", "element": "Docs nav link", '
        '"selector": "getByRole(\'link\', { name: \'Docs\' })"}]\n'
        "}\n"
        "```\n"
        "Rules for the sidecar: `locator`/`expectLocator` are omitted (or empty) when "
        "there is no single element to point at — never guessed. `routes` lists the "
        "paths you actually reached; `selectors` the elements you confirmed live. "
        "Write valid JSON only — no comments, no trailing commas.\n"
    )


def plan_ticket(run, ticket, context: dict, *, owner_id: int | None, target: dict[str, str]) -> dict | None:
    """Run the planner agent against the live app for one ticket (#889).

    Launches the project's dedicated, pre-authenticated Chrome (shared plumbing in
    :mod:`app.services.agentic_browser`, the same setup
    :func:`live_authoring_service.author_case` uses) and runs the ported
    ``playwright-test-planner`` agent over it, then reads back the JSON sidecar.

    Best-effort by construction: a missing ``playwright-cli``, a browser that never
    came up, a Claude failure, or a sidecar that cannot be parsed all log a warning
    and return ``None`` — which the caller treats as "no live grounding", falling
    back to today's text-only generation. Nothing here may fail a run.

    Args:
        run: The run this ticket belongs to (workspace + label attribution).
        ticket: The work item being planned for.
        context: The resolved project context (must already carry a ``baseUrl``).
        owner_id: Owning user id — resolves the project's browser profile.
        target: ``{"ticket", "screen", "goal"}`` the plan should pursue.

    Returns:
        A normalized plan (see :func:`normalize_plan`), or ``None``.
    """
    base_url = (context.get("baseUrl") or "").strip()
    if not base_url:
        return None
    if not claude_cli.playwright_cli_available():
        logger.warning(
            "Planner agent skipped for {}: playwright-cli not available on the API host",
            ticket.external_id,
        )
        return None
    if agents.agents_json(agents.PLAYWRIGHT_TEST_PLANNER) is None:
        logger.warning("Planner agent definition missing — falling back to text-only")
        return None

    workspace = (
        settings.workspace_dir / "planning" / _safe_dirname(run.code) / _safe_dirname(ticket.external_id)
    )
    workspace.mkdir(parents=True, exist_ok=True)
    sidecar_path = workspace / SIDECAR_NAME
    try:  # a stale sidecar from a previous attempt must never be read as this run's plan
        sidecar_path.unlink()
    except FileNotFoundError:
        pass

    profile_dir = (
        project_config_service.auth_path(context.get("projectKey") or "", owner_id).parent
        / "browser-profile"
    )
    name = agentic_browser.session_name("plan", run.code, ticket.external_id)

    try:
        with agentic_browser.browser_session(
            base_url, profile_dir, name, browser_driver="playwright-cli"
        ) as extra_env:
            claude_cli.run_agentic(
                build_planner_prompt(target, base_url, context),
                workspace_dir=workspace,
                agent=agents.PLAYWRIGHT_TEST_PLANNER,
                label=f"Plan test scenarios: {ticket.external_id}",
                extra_env=extra_env,
                max_budget_usd=settings_store.authoring_cost_budget_usd(),
            )
    except Exception as exc:  # noqa: BLE001 - live planning is best-effort grounding
        logger.warning(
            "Planner agent run failed for {}: {} — falling back to text-only",
            ticket.external_id,
            exc,
        )
        return None

    return read_plan_sidecar(sidecar_path)


def read_plan_sidecar(path: Path) -> dict | None:
    """Read + normalize the planner's JSON sidecar, or ``None`` if unusable.

    Args:
        path: Absolute path to ``plan.json``.

    Returns:
        A normalized plan, or ``None`` when the file is absent, not JSON, or
        carries no usable scenario.
    """
    if not path.exists():
        logger.warning("Planner agent wrote no {} — falling back to text-only", path.name)
        return None
    try:
        plan = normalize_plan(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001 - a bad sidecar must not fail generation
        logger.warning("Planner sidecar could not be parsed: {}", exc)
        return None
    if plan is None:
        logger.warning("Planner sidecar carried no usable scenario — falling back to text-only")
    return plan
