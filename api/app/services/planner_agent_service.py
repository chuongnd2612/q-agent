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

from uuid import uuid4

from app.config import settings
from app.logging import logger
from app.services import (
    agent_capture_service,
    agent_planning_service,
    agentic_browser,
    agents,
    audit_service,
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

    **Where it runs (#900).** With ``executionTarget="local-agent"`` the planning
    is dispatched to the paired device instead (:func:`_plan_on_device`), because
    the API container usually cannot reach the app under test and — worse — the
    manual login was captured into the *device's* browser profile, so planning
    here would explore whatever an anonymous visitor sees and write test cases
    describing the login screen. Every device failure still degrades to ``None``,
    so a dispatch problem costs a run its grounding, never the run.

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
    if settings_store.load_settings().get("executionTarget", "server") == "local-agent":
        return _plan_on_device(
            run, ticket, context, owner_id=owner_id, target=target, base_url=base_url
        )
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


def system_prompt() -> str:
    """The planner agent definition's body, as raw text, for the device path.

    The paired device runs its own CLI, which has no ``--agent`` support and no
    ``agents/`` directory, so the methodology travels as a *system prompt string*
    instead (the wire shape live authoring already uses — see
    :func:`live_authoring_service.system_prompt_for`, #894/#901). An agent
    definition is just a prompt, so sending its body delivers the same
    methodology. Returns ``""`` when the definition cannot be loaded, which the
    caller treats as "cannot plan on the device".
    """
    definition = agents.load_agent(agents.PLAYWRIGHT_TEST_PLANNER)
    return definition["prompt"] if definition else ""


def _plan_on_device(
    run, ticket, context: dict, *, owner_id: int | None, target: dict[str, str], base_url: str
) -> dict | None:
    """Plan on the paired Local Agent and wait, bounded, for the result (#900).

    Mirrors the three existing device dispatches (capture, exploration, live
    authoring) in wire shape — enqueue a session, the device claims it over
    ``/agent/planning/next`` and posts back to ``/finalize`` — with one
    deliberate difference: the planner's output is consumed *inline* by the very
    next generation prompt, so this blocks on the session row instead of
    returning and letting something poll later. That is safe because the caller
    is already a background worker thread, and because every failure below
    returns ``None``, i.e. "generate from text", exactly as a missing sidecar
    already did.

    Args:
        run, ticket, context, owner_id, target: as :func:`plan_ticket`.
        base_url: The already-validated, non-empty app URL.

    Returns:
        A normalized plan, or ``None`` when no device is paired, nothing claimed
        the session, the device overran the deadline, or what it posted back
        carried no usable scenario. Each of those logs a warning AND records an
        audit row, because the symptom of a silent failure here is not an error
        but a run that quietly planned blind.
    """
    if not agent_planning_service.has_paired_device(owner_id):
        return _device_planning_unavailable(
            run,
            ticket,
            "No Local Agent paired — planned from ticket text instead",
            "Execution target is Local Agent but no device is paired",
        )
    methodology = system_prompt()
    if not methodology:
        return _device_planning_unavailable(
            run,
            ticket,
            "Planner agent definition missing — planned from ticket text instead",
            "agents/playwright-test-planner.md could not be loaded",
        )

    session_id = uuid4().hex
    stored = settings_store.load_settings()
    agent_planning_service.request_planning(
        session_id,
        owner_id=owner_id,
        run_id=getattr(run, "id", None),
        project_key=context.get("projectKey") or "",
        repo=context.get("repo", "") or "",
        base_url=base_url,
        origin=agent_capture_service.origin_of(base_url),
        run_code=run.code,
        ticket=ticket.external_id,
        sidecar_filename=SIDECAR_NAME,
        system_prompt=methodology,
        task_prompt=build_planner_prompt(target, base_url, context),
        model=stored.get("claudeModel") or settings.claude_model,
        max_budget_usd=settings_store.authoring_cost_budget_usd(),
        log_verbosity=stored.get("authoringLogVerbosity", "concise"),
    )
    logger.info(
        "Dispatched live planning for {} to the paired device (session={})",
        ticket.external_id,
        session_id,
    )

    result = agent_planning_service.await_result(session_id)
    if result is None:
        return _device_planning_unavailable(
            run,
            ticket,
            "Local Agent did not return a plan in time — planned from ticket text instead",
            f"planning session {session_id} hit its deadline",
        )
    if result.get("status") != "done":
        return _device_planning_unavailable(
            run,
            ticket,
            "Local Agent could not plan live — planned from ticket text instead",
            (result.get("summary") or "the device reported a failure")[:400],
        )
    return parse_plan_text(result.get("plan_json") or "")


def _device_planning_unavailable(run, ticket, action: str, why: str) -> None:
    """Log + audit a device-planning fallback, then return ``None``.

    One helper for every giving-up path so the run timeline always says which
    one happened. Without this the fallback is invisible: generation simply
    continues from ticket text and looks like a normal text-mode run.
    """
    logger.warning("Live planning on the device unavailable for {}: {}", ticket.external_id, why)
    audit_service.record(
        category="automation",
        actor_type="ai",
        action=action,
        target=ticket.external_id,
        status="warning",
        meta=why,
        run_code=getattr(run, "code", None),
    )
    return None


def parse_plan_text(raw: str) -> dict | None:
    """Parse + normalize a plan sidecar's raw TEXT, or ``None`` if unusable.

    The one place sidecar text becomes a plan, whether it was written to disk by
    a server-side run or posted back by a device. Keeping it here is what lets
    the device ship raw text over the wire: the caps, the key aliases and the
    "no usable scenario" rule cannot fork onto a device that releases on its own
    cadence.

    Args:
        raw: The sidecar's contents.

    Returns:
        A normalized plan, or ``None`` when the text is empty, not JSON, or
        carries no usable scenario.
    """
    if not (raw or "").strip():
        logger.warning("Planner produced an empty plan sidecar — falling back to text-only")
        return None
    try:
        plan = normalize_plan(json.loads(raw))
    except Exception as exc:  # noqa: BLE001 - a bad sidecar must not fail generation
        logger.warning("Planner sidecar could not be parsed: {}", exc)
        return None
    if plan is None:
        logger.warning("Planner sidecar carried no usable scenario — falling back to text-only")
    return plan


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
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Planner sidecar could not be read: {}", exc)
        return None
    return parse_plan_text(raw)
