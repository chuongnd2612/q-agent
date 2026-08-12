"""Claude -> Playwright TypeScript spec generation.

Prompts the real Claude CLI with a test case's title, precondition, and steps
and asks it to emit a single runnable Playwright + TypeScript spec file. Per
ADR 0001 there is no simulated fallback: failures propagate as ``ClaudeError``.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.logging import logger
from app.models.run import RunTicket
from app.models.testcase import TestCase
from app.models.ticket import Ticket
from app.services import claude_cli, project_config_service
from app.services.prompts import (
    render_base_framework_api,
    render_dom_snapshot,
    render_project_context,
)
from app.services.skills import AUTOMATION_GENERATOR
from app.services.workspace_scope import scoped_specs_dir

_FENCE_RE = re.compile(r"```(?:ts|typescript)?\s*(.*?)```", re.DOTALL)

# Few-shot reference specs are truncated to keep the prompt small (context-bloat
# guard); at most 1-2 examples are ever injected (see select_examples).
_EXAMPLE_MAX_CHARS = 6000

_SYSTEM_PROMPT = (
    "You are a senior QA automation engineer. You write clean, runnable "
    "Playwright + TypeScript test specs on top of the shared "
    "@q-agent/playwright-base framework, in a layered automation project where "
    "low-level detail lives in shared files and the spec reads as business "
    "steps. Respond with "
    "ONLY the TypeScript source code for a single spec file, wrapped in a "
    "```typescript fenced code block. Do not include any prose before or "
    "after the code block."
)

# The layered spec contract (#542, reversing #178; import rule superseded by #544)
# — the prompt half of the pair that MUST stay in step with
# `skills/automation-generator/SKILL.md`. Editing one alone recreates the
# system-vs-user dissonance #178 closed for.
#
# Two hard constraints from the machinery, not preferences:
#  * Specs live at `tests/<TICKET>/<TICKET>-<CASE>.spec.ts` — TWO levels below the
#    project root — so a project file is `../../pages/Foo`, never `../pages/Foo`.
#  * `automation_gate.list_ok_in_project` requires rc==0 AND the candidate's
#    `test()` titles in the `--list` output, so an import that does not resolve is
#    a hard rejection, and a `test.describe` with no `test()` inside is one too.
#
# #542 gated asset imports on *observable evidence*: a file was importable only if
# a REFERENCE SPEC already imported it. #544 replaces that with the AUTOMATION PLAN
# block, which enumerates the exact importable paths — computed server-side from
# `automation_project_service.inventory()`, i.e. from the real tree, never from the
# model's claim. The plan block is injected by `automation_planner_service.render_plan`
# and is the ONLY authorization; with no plan (legacy path, planning failed) the
# block is absent and the rule below reduces to "import nothing but the base
# package", which is the safe pre-#544 behaviour.
_SPEC_ARCHITECTURE = (
    "Spec architecture — layered (doc §12). This spec file is written into the "
    "project's persistent automation project at "
    "`tests/<TICKET-ID>/<TICKET-ID>-<CASE>.spec.ts`, i.e. TWO directory levels "
    "below the project root: a shared project file is therefore imported as "
    "`../../pages/Foo`, `../../fixtures/app.fixture`, `../../data/users` — never "
    "`../pages/Foo`.\n"
    "- Import `test`, `expect` and any assertion helper from "
    "'@q-agent/playwright-base'. Never import '@playwright/test' directly.\n"
    "- Emit exactly ONE `test(...)` block for this case. A file containing only a "
    "`test.describe(...)` with no `test()` inside it is rejected. Write the test "
    "title as a plain quoted string — never a template literal with `${...}` in "
    "it.\n"
    "- The body reads as business steps plus web-first assertions, not as a "
    "recording of browser mechanics.\n"
    "- Do NOT write an inline login flow (see the authentication policy below).\n"
    "- Locators and low-level UI mechanics belong in a shared page object (doc "
    "§14) — that is the DEFAULT, not an aspiration: the page objects this case "
    "needs have already been authored or extended for it. The AUTOMATION PLAN "
    "block above is the exhaustive list of the ones you may import, and those "
    "paths were verified against this project's real tree. "
    "Import nothing else from `../../pages/`, `../../components/`, "
    "`../../fixtures/`, `../../data/` or `../../utils/`: an import that does not "
    "resolve fails collection and the spec is rejected outright. Never invent "
    "`import { LoginPage } from '../../pages/LoginPage'` in the hope that it "
    "exists, and never treat a page object / fixture NAME listed in the Knowledge "
    "Base as proof of a file — those names describe the product repo, not this "
    "automation project.\n"
    "- An inline locator is the EXCEPTION, not the rule. Take it only for a step "
    "whose asset the plan names but does not list as importable (its file is not "
    "on disk, so importing it would fail collection), or when there is no plan "
    "block at all. Choose such a locator by the project's locator priority and "
    "keep the body a thin, readable sequence of steps."
)

# Heal / edit guard (#542). A fixer told merely to "make it pass" will happily
# flatten the layering — re-inline a page object's locators, drop the base-package
# import, or paste the login flow back in — and the spec would still pass the
# gate. State the rejection explicitly instead.
#
# #547 removed the trailing "note the suspected file in one brief comment"
# instruction, which was a stopgap for the era when this fixer was the ONLY thing
# that could edit anything: a comment was all it could do about a defect it could
# not reach. The heal loop now repairs the page object for real
# (`page_object_healer_service.heal_library`) BEFORE this fixer is asked for
# anything, so by the time the fixer runs the library has either been repaired or
# been found sound. Leaving the instruction in would invite a cosmetic comment
# about a file that was already fixed, and — worse — keep implying the spec is the
# only writable file.
_ARCHITECTURE_GUARD = (
    "Preserve the spec's architecture — fix the defect, not the design. Keep the "
    "imports from '@q-agent/playwright-base' (never swap them back to "
    "'@playwright/test') and keep every import of a shared project file "
    "(`../../pages/…`, `../../fixtures/…`, `../../data/…`). Do NOT inline a page "
    "object's locators, a fixture's setup, or a login flow back into the spec to "
    "route around it, and do NOT delete an import to make the file "
    "self-contained: a fix that flattens the layering is REJECTED even if it "
    "would pass. Repairing an imported page object is a different stage's job and "
    "not yours: fix the spec's own use of it."
)

# Robustness rules shared by generation and self-heal prompts (#178 promotes
# these from the fix-only prompt into generation too, so the FIRST spec is
# already flaky-resistant rather than relying on a heal cycle to fix it).
_ROBUSTNESS_RULES = (
    "Prefer robust locators (getByRole/getByLabel/getByTestId) over brittle raw "
    "CSS/XPath selectors. Use web-first assertions (expect(locator).toBeVisible(), "
    "toHaveText(...), etc.) that rely on Playwright's built-in auto-waiting. Never "
    "use page.waitForTimeout(...) or any other arbitrary hard-coded wait."
)

# Auth policy shared by generation, self-heal, and chat-edit prompts (#291, #542).
# Authentication is handled OUTSIDE the spec — by the run's saved manual-login
# session (storageState + sessionStorage replay, installed by the base package's
# `test` fixture and the project's Playwright config) and the real test-account
# credentials injected in the project context. So the spec must never mock/bypass
# auth, and must never narrate its auth strategy (the old "Auth note" boilerplate).
#
# #542 tightens "log in inline OR rely on the session" into "rely on the session,
# with authentication cases as the one exception". Re-inlining the same login
# preamble into every generated spec is the single largest source of generated bulk
# and flake today (doc §17), and it is what the base package's session fixture
# exists to delete.
_AUTH_POLICY = (
    "Authentication is already handled for you, OUTSIDE the spec (doc §17): the run "
    "installs its saved manual-login session (storageState + sessionStorage replay) "
    "through the `test` fixture from '@q-agent/playwright-base' and the project's "
    "Playwright config, so the spec STARTS authenticated. Do NOT re-implement login: "
    "no `goto('/login')` + fill username + fill password + click preamble at the top "
    "of the spec. Navigate straight to the route the case starts on and test the app "
    "as a real authenticated user. The ONE exception is a case whose subject IS "
    "authentication (login, logout, session expiry, permissions) — then drive the "
    "real login form with the real test-account credentials from the project "
    "context, preferably via `formLoginFlow`/`performFormLogin` from the base "
    "package rather than hand-rolled fills. Do NOT mock, "
    "stub, intercept, or bypass authentication — never route-mock identity/session "
    "endpoints (e.g. GET /api/sessions/me, /api/sessions/permissions), never assume "
    "environment flags such as VITE_BYPASS_AUTH, and never fabricate a storageState. "
    "Do NOT "
    "emit meta-commentary or an \"Auth note\" explaining auth strategy, mocking "
    "decisions, or environment assumptions; keep comments to brief step annotations."
)


def _render_examples(examples: list[dict] | None) -> str:
    """Render up to 2 proven passing specs as a reference block for the prompt.

    Args:
        examples: ``[{"filename", "code"}]`` from ``spec_examples.select_examples``
            — real specs that already passed against THIS project + repo. May be
            None/empty.

    Returns:
        A clearly-labelled reference section (each example truncated to
        ``_EXAMPLE_MAX_CHARS``), or "" when there are no usable examples so the
        prompt is unchanged in the no-grounding case.
    """
    if not examples:
        return ""
    blocks: list[str] = []
    for ex in examples[:2]:
        code = (ex.get("code", "") or "")[:_EXAMPLE_MAX_CHARS].strip()
        if not code:
            continue
        filename = ex.get("filename", "") or "spec.ts"
        blocks.append(f"// {filename}\n{code}")
    if not blocks:
        return ""
    return (
        "REFERENCE SPECS — real, already-passing specs from THIS project. Match "
        "their conventions exactly (fixtures, helpers, import structure, assertion "
        "style). Do NOT copy their test logic:\n\n"
        + "\n\n".join(blocks)
        + "\n\n"
    )


def build_case_context(db: Session, case: TestCase, env: str = "") -> dict[str, Any]:
    """Resolve the Project Knowledge Base + config for a test case's project.

    Looks up the case's ticket to find its provider, then resolves the full
    project context (base URL, decrypted test-account credentials, routes,
    selectors, auth flow, reusable assets). Returns an empty-ish dict when no
    project resolves — generation still works, just without grounding.
    """
    ticket = db.query(Ticket).filter(Ticket.external_id == case.ticket_external_id).first()
    if ticket is None:
        return {}
    # Resolve the work item's chosen target repo so the spec is generated against
    # that repo's knowledge base (falls back to the project default when empty).
    run_ticket = (
        db.query(RunTicket)
        .filter(
            RunTicket.run_id == case.run_id,
            RunTicket.ticket_external_id == case.ticket_external_id,
        )
        .first()
    )
    repo = run_ticket.repo if run_ticket else ""
    return project_config_service.context_for_ticket(db, ticket, env=env, repo=repo)


def _case_rank_query(case: TestCase) -> str:
    """Build the relevance-ranking query text for a case: title + step text.

    Passed as ``render_project_context``'s ``rank_query`` (#182) so the KB's
    routes/selectors are ranked by relevance to what THIS case actually needs
    before being truncated, instead of an arbitrary blind slice.
    """
    parts = [case.title or ""]
    for step in case.steps or []:
        parts.append(step.get("a", ""))
        parts.append(step.get("e", ""))
    return " ".join(parts)


def _render_test_data(case: TestCase) -> str:
    """Render the reviewer-provided test data as a prompt block, or '' if none.

    Args:
        case: The TestCase whose ``test_data`` (a list of ``{field, value}`` dicts,
            editable by QA in the Review screen) supplies the concrete inputs the
            spec should exercise.

    Returns:
        A prompt paragraph (trailing blank line) listing each field/value so the
        model fills forms and asserts against QA's exact values instead of
        inventing them — or an empty string when the case has no usable test data.
    """
    rows = [
        row
        for row in (case.test_data or [])
        if isinstance(row, dict) and (row.get("field") or row.get("value"))
    ]
    if not rows:
        return ""
    lines = "\n".join(f"  - {row.get('field', '')}: {row.get('value', '')}" for row in rows)
    return (
        "Test Data (reviewer-provided — use these EXACT values wherever the steps "
        "reference them: enter them into inputs and assert against them; do not "
        f"substitute invented values):\n{lines}\n\n"
    )


def _build_prompt(
    case: TestCase,
    context: dict[str, Any] | None = None,
    examples: list[dict] | None = None,
    reviewer_comment: str | None = None,
    plan: dict[str, Any] | None = None,
) -> str:
    """Render the Claude prompt for a single test case.

    Args:
        case: The approved, non-Manual TestCase to generate a spec for.
        context: Resolved project context (base URL, real credentials, selectors,
            routes, auth) used to emit a runnable spec with no placeholders.
        examples: Optional few-shot reference specs (proven, already-passing) shown
            so the model matches this project's conventions.
        reviewer_comment: Optional free-text note from a human reviewer steering
            this regeneration. When present it is injected as a high-priority
            guidance block right after the grounding — the caller's gate still
            enforces quality, so the note cannot license placeholders or weaker
            assertions.
        plan: The ticket's normalized Automation Plan (#544). Rendered as the
            AUTOMATION PLAN block, which is the **only** authorization for an asset
            import — see :data:`_SPEC_ARCHITECTURE`. ``None`` (legacy path, or
            planning failed) omits the block, leaving the base-package import as
            the only legal one.

    Returns:
        A prompt string describing the case's title, precondition, and steps,
        instructing Claude to produce a Playwright TS spec.
    """
    steps_lines = "\n".join(
        f"  {i + 1}. Action: {step.get('a', '')} | Expected: {step.get('e', '')}"
        for i, step in enumerate(case.steps or [])
    )
    # include_secrets=True: the user chose to bake literal credentials/URLs into
    # generated specs so they run unmodified.
    project_block = render_project_context(
        context, include_secrets=True, rank_query=_case_rank_query(case)
    )
    if project_block:
        grounding = (
            f"{project_block}\n\n"
            "Use the real values above DIRECTLY in the spec: navigate to the real "
            "base URL / routes and use the real selectors and locator strategy. The "
            "real credentials are there for an authentication-subject case only — the "
            "spec does not log in (see the authentication policy below). Only fall "
            "back to a clearly-marked "
            "// TODO placeholder for a value that is genuinely absent from the "
            "context above.\n\n"
        )
    else:
        grounding = (
            "If a concrete URL or selector isn't known, use reasonable placeholders "
            "and TODO comments rather than inventing unrelated behavior.\n\n"
        )
    reviewer_block = (
        (
            "Reviewer guidance — a human reviewer requested this regeneration with "
            "these instructions. Prioritise them, but do NOT use placeholders/invented "
            "values or weaken assertions to satisfy them:\n"
            f"{reviewer_comment[:2000]}\n\n"
        )
        if reviewer_comment
        else ""
    )
    # Local import: `automation_planner_service` imports `prompts`, and importing it
    # at module scope would make spec_service <-> planner a cycle.
    from app.services.automation_planner_service import render_plan

    plan_block = render_plan(plan)
    return (
        f"Generate a Playwright TypeScript test spec for this manual test case.\n\n"
        f"{grounding}"
        f"{reviewer_block}"
        f"{render_base_framework_api()}\n\n"
        + (f"{plan_block}\n\n" if plan_block else "")
        + f"{_SPEC_ARCHITECTURE}\n\n"
        f"{_render_examples(examples)}"
        f"Test Case ID: {case.code}\n"
        f"Title: {case.title}\n"
        f"Precondition: {case.precondition or 'None'}\n"
        f"Steps:\n{steps_lines or '  (none provided)'}\n\n"
        f"{_render_test_data(case)}"
        f"Use `import {{ test, expect }} from '@q-agent/playwright-base';` and a single "
        f"`test('{case.code} — {case.title}', async ({{ page }}) => {{ ... }})` block, "
        f"tagged with the Test Case ID ({case.code}) so results trace back to this case, "
        f"that encodes the precondition and each step as business actions/assertions. "
        f"{_ROBUSTNESS_RULES} {_AUTH_POLICY}"
    )


def _build_fix_prompt(
    case: TestCase,
    current_code: str,
    error_message: str,
    run_output: str = "",
    context: dict[str, Any] | None = None,
    examples: list[dict] | None = None,
    dom_snapshot: dict[str, Any] | None = None,
) -> str:
    """Render a Claude prompt asking it to FIX a spec that failed when executed.

    Args:
        case: The TestCase the spec belongs to (provides the intended behavior).
        current_code: The spec source that just ran and failed — Claude edits this.
        error_message: The failure/assertion error Playwright reported.
        run_output: Optional tail of Playwright stdout/stderr for extra signal.
        context: Resolved project context (base URL, credentials, selectors, …)
            so fixes use the real, grounded values rather than guesses.
        examples: Optional few-shot reference specs (proven, already-passing) so the
            fix keeps this project's conventions.
        dom_snapshot: Optional distilled live-DOM captured when the spec failed
            (``qagent-dom-distilled``) — the page's real interactable elements, so
            the fixer can pick grounded locators even when the KB has no selectors
            (the ``blocked`` case).

    Returns:
        A prompt instructing Claude to return the complete corrected spec file.
    """
    steps_lines = "\n".join(
        f"  {i + 1}. Action: {step.get('a', '')} | Expected: {step.get('e', '')}"
        for i, step in enumerate(case.steps or [])
    )
    project_block = render_project_context(
        context, include_secrets=True, rank_query=_case_rank_query(case)
    )
    grounding = f"{project_block}\n\n" if project_block else ""
    dom_block_text = render_dom_snapshot(dom_snapshot)
    dom_block = f"{dom_block_text}\n\n" if dom_block_text else ""
    output_block = f"\n\nPlaywright output (tail):\n{run_output.strip()[-2000:]}" if run_output.strip() else ""
    return (
        "The following Playwright test FAILED when executed. Fix it so it passes.\n\n"
        f"{grounding}"
        f"{dom_block}"
        f"{render_base_framework_api()}\n\n"
        f"{_render_examples(examples)}"
        f"Test case being automated:\n"
        f"Title: {case.title}\n"
        f"Precondition: {case.precondition or 'None'}\n"
        f"Steps:\n{steps_lines or '  (none provided)'}\n\n"
        f"{_render_test_data(case)}"
        "Current spec (this is exactly what ran and FAILED):\n"
        f"```typescript\n{current_code.strip()}\n```\n\n"
        f"Failure / error:\n{error_message.strip() or '(no error message captured)'}"
        f"{output_block}\n\n"
        "Return the COMPLETE corrected spec file (full source, not a diff). Keep the "
        f"same `test('{case.title}', ...)` title. Address the specific failure above: "
        "fix broken selectors, missing awaits, wrong routes/URLs, timing, or "
        "assertions. Prefer robust locators (getByRole/getByLabel/getByText), "
        "web-first assertions (expect(locator).toBeVisible(), etc.), and explicit "
        "waits over arbitrary timeouts. Use the real grounded values above where "
        "given. Do not invent unrelated behavior or weaken the test just to pass. "
        f"{_ARCHITECTURE_GUARD} {_AUTH_POLICY}"
    )


def generate_fixed_spec_code(
    case: TestCase,
    current_code: str,
    error_message: str,
    run_output: str = "",
    context: dict[str, Any] | None = None,
    examples: list[dict] | None = None,
    dom_snapshot: dict[str, Any] | None = None,
) -> str:
    """Ask Claude to repair a failing spec, given its code and the failure.

    Args:
        case: The TestCase the spec automates.
        current_code: The spec source that failed.
        error_message: The Playwright failure/assertion message.
        run_output: Optional tail of the Playwright process output.
        context: Resolved project context for grounded fixes.
        examples: Optional few-shot reference specs (proven, already-passing).
        dom_snapshot: Optional distilled live-DOM captured at the failure so the
            fixer can pick real locators (see ``_build_fix_prompt``).

    Returns:
        The corrected TypeScript spec source code.

    Raises:
        claude_cli.ClaudeError: if the CLI is unavailable or errors.
    """
    # Observability (#heal-slow): make it visible whether a heal fix is actually
    # progressing and whether it's grounded on the live DOM — the heal is slow
    # because it regenerates the WHOLE spec (~15k tokens), not because it hangs.
    dom_elements = len(dom_snapshot.get("elements") or []) if dom_snapshot else 0
    dom_path = (dom_snapshot or {}).get("path") or ""
    label = f"Heal: {case.ticket_external_id} {case.code}"
    logger.info(
        "{} — generating fix (full-spec regenerate): DOM grounding = {}",
        label,
        f"{dom_elements} element(s), path={dom_path}" if dom_elements else "none (no live DOM captured)",
    )
    started = time.monotonic()
    raw = claude_cli.run_prompt(
        _build_fix_prompt(
            case, current_code, error_message, run_output, context, examples, dom_snapshot
        ),
        system=_SYSTEM_PROMPT,
        skill=AUTOMATION_GENERATOR,
        label=label,
        # The fixer is a targeted DOM-grounded edit — run it on the fast heal
        # model (#398), not the heavy global model used for fresh generation.
        model=settings.heal_fix_model,
    )
    code = _extract_code(raw)
    logger.info(
        "{} — fix received in {:.1f}s ({} chars)",
        label,
        time.monotonic() - started,
        len(code),
    )
    return code


def _render_references(references: list[tuple[str, str]] | None) -> str:
    """Render `@spec` mentions the reviewer embedded — the referenced specs' code,
    shown as read-only context (Claude edits only the current spec)."""
    if not references:
        return ""
    blocks = "\n\n".join(
        f"// {name}\n```typescript\n{(code or '').strip()}\n```" for name, code in references
    )
    return (
        "Referenced specs (context the reviewer @-mentioned — do NOT edit these, only the "
        f"current spec below; reuse their patterns/selectors where relevant):\n{blocks}\n\n"
    )


def _build_chat_edit_prompt(
    case: TestCase,
    current_code: str,
    instruction: str,
    context: dict[str, Any] | None,
    references: list[tuple[str, str]] | None = None,
) -> str:
    """Render a prompt asking Claude to edit an existing spec per a reviewer instruction.

    Feeds the current spec, the natural-language instruction, grounded project
    context, any @-mentioned reference specs, and the shared robustness rules. Asks
    for a short prose explanation followed by the COMPLETE edited spec in a
    ```typescript fence — the caller splits the two (:func:`generate_chat_edit`).
    """
    project_block = render_project_context(
        context, include_secrets=True, rank_query=_case_rank_query(case)
    )
    grounding = f"{project_block}\n\n" if project_block else ""
    return (
        "You are editing an existing Playwright test spec based on a reviewer's instruction.\n\n"
        f"{grounding}"
        f"{render_base_framework_api()}\n\n"
        f"{_render_references(references)}"
        f"{_render_test_data(case)}"
        f"{_ROBUSTNESS_RULES} {_ARCHITECTURE_GUARD} {_AUTH_POLICY}\n\n"
        f"Reviewer instruction:\n{instruction.strip()}\n\n"
        "Current spec:\n"
        f"```typescript\n{current_code.strip()}\n```\n\n"
        "First, in 1-3 short sentences, explain what you changed and why (plain prose — no "
        "code, no markdown headings, no bullet list). Then output the COMPLETE edited spec "
        "file in a single ```typescript fenced block. Apply the instruction faithfully; keep "
        "the same test intent and all existing assertions unless the instruction says "
        "otherwise; use the real grounded routes/selectors above; never weaken assertions or "
        "introduce placeholders/invented references."
    )


def generate_chat_edit(
    db, run, case: TestCase, current_code: str, instruction: str,
    references: list[tuple[str, str]] | None = None,
) -> tuple[str, str]:
    """Ask Claude to edit a spec per a NL instruction; return ``(explanation, new_code)``.

    Backend for the Automation screen's AI chat panel. Resolves the run's grounded
    project context, prompts Claude for a prose explanation + the complete edited
    spec, and splits the response: ``new_code`` via :func:`_extract_code`, the
    ``explanation`` = the text before the code fence. ``references`` are ``(filename,
    code)`` pairs the reviewer ``@``-mentioned, embedded as read-only context.

    Raises:
        claude_cli.ClaudeError: if the CLI is unavailable or errors.
    """
    context = build_case_context(db, case, env=run.env)
    raw = claude_cli.run_prompt(
        _build_chat_edit_prompt(case, current_code, instruction, context, references),
        system=_SYSTEM_PROMPT,
        skill=AUTOMATION_GENERATOR,
        label=f"Chat edit: {case.ticket_external_id} {case.code}",
    )
    new_code = _extract_code(raw)
    match = _FENCE_RE.search(raw)
    explanation = (raw[: match.start()] if match else "").strip()
    return (explanation or "Updated the spec as requested.", new_code)


def _extract_code(raw: str) -> str:
    """Pull TypeScript source out of Claude's response.

    Args:
        raw: The raw text returned by the Claude CLI, expected to contain a
            fenced ```typescript code block.

    Returns:
        The extracted source code, or the raw text stripped if no fence is
        present (defensive — Claude is instructed to always fence).
    """
    match = _FENCE_RE.search(raw)
    return (match.group(1) if match else raw).strip() + "\n"


def generate_spec_code(
    case: TestCase,
    context: dict[str, Any] | None = None,
    examples: list[dict] | None = None,
    reviewer_comment: str | None = None,
    plan: dict[str, Any] | None = None,
) -> str:
    """Ask Claude to generate Playwright TypeScript source for a test case.

    Args:
        case: The TestCase to generate automation for.
        context: Resolved project context (base URL, credentials, selectors, …)
            so the generated spec runs with little to no manual modification.
        examples: Optional few-shot reference specs (proven, already-passing) shown
            so the generated spec matches this project's conventions.
        reviewer_comment: Optional free-text reviewer note steering a per-case
            regeneration; forwarded into the prompt as guidance (gate unchanged).
        plan: The ticket's Automation Plan (#544) — the reuse/extend/create
            decisions, and the only authorization for an asset import. Produced once
            per ticket by ``automation_planner_service.plan_for_ticket`` and passed
            in by ``routers/automation._generate_one``; ``None`` on the legacy path.

    Returns:
        The generated TypeScript spec source code.

    Raises:
        claude_cli.ClaudeError: if the CLI is unavailable or errors.
    """
    raw = claude_cli.run_prompt(
        _build_prompt(case, context, examples, reviewer_comment, plan),
        system=_SYSTEM_PROMPT,
        skill=AUTOMATION_GENERATOR,
        include_template=True,
        label=f"Spec: {case.ticket_external_id} {case.code}",
    )
    return _extract_code(raw)


_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def spec_filename(ticket_external_id: str, case_code: str) -> str:
    """Build the on-disk spec filename for a case, from the **full** ticket id.

    This used to strip the ticket to its last segment (``"SUR-1428"`` ->
    ``"1428"``), which made ``SUR-1428/TC-01`` and ``OPS-1428/TC-01`` produce the
    *same* filename. In the old per-run throwaway dirs that was the "Minor —
    filename collision" of ``docs/ARCHITECTURE-REVIEW.md:289-291``. In the
    persistent ``tests/`` tree of #537 — which accumulates across every ticket
    forever — one ticket's spec would silently overwrite another's and
    ``execution_service.match_result`` would misattribute results, so #540 emits
    the full id instead. ``match_result`` still accepts the old short form so
    in-flight legacy runs keep matching (see :func:`legacy_spec_filename`).

    Args:
        ticket_external_id: e.g. "SUR-1428".
        case_code: e.g. "TC-01".

    Returns:
        A filename like "SUR-1428-TC-01.spec.ts". Characters that are not
        filesystem-safe are collapsed to ``-``; an empty ticket/case degrades to
        ``"unknown"`` rather than producing a hidden or empty filename.
    """
    ticket = _UNSAFE_NAME_RE.sub("-", (ticket_external_id or "").strip()).strip("-") or "unknown"
    case = _UNSAFE_NAME_RE.sub("-", (case_code or "").strip()).strip("-") or "unknown"
    return f"{ticket}-{case}.spec.ts"


def legacy_spec_filename(ticket_external_id: str, case_code: str) -> str:
    """The pre-#540 short-ticket filename, e.g. ``"1428-TC-01.spec.ts"``.

    Kept only so ``execution_service.match_result`` can still attribute results
    from runs whose specs were written before #540. Never used for new writes.
    """
    return f"{(ticket_external_id or '').rsplit('-', 1)[-1]}-{case_code}.spec.ts"


def write_spec_file(
    run_code: str,
    ticket_external_id: str,
    case_code: str,
    code: str,
    owner_id: int | None = None,
) -> Path:
    """Write generated spec source to <scoped specs dir>/{run_code}/{filename}.

    Args:
        run_code: The owning Run's human code, e.g. "RUN-205".
        ticket_external_id: The ticket the case belongs to, e.g. "SUR-1428".
        case_code: The test case's code, e.g. "TC-01".
        code: The TypeScript source to write.
        owner_id: The owning Run's ``owner_id`` (ADR 0009 §1) — resolves the
            per-owner specs tree via ``scoped_specs_dir``; ``None`` (no owner,
            e.g. auth disabled) resolves to the shared namespace.

    Returns:
        The absolute path the file was written to.
    """
    run_dir = scoped_specs_dir(owner_id) / run_code
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / spec_filename(ticket_external_id, case_code)
    path.write_text(code, encoding="utf-8")
    return path


def _resolve_list_bin() -> str | None:
    """Path to the locally-installed Playwright binary, or None if not installed.

    We deliberately do NOT fall back to ``npx`` here (unlike execution): a bare
    ``npx playwright`` could trigger a network fetch/install and hang, and this
    parse check must never block generation. Absent a local install we skip.
    """
    nm = settings.playwright_node_modules
    for candidate in (nm / ".bin" / "playwright.cmd", nm / ".bin" / "playwright"):
        if candidate.exists():
            return str(candidate)
    return None


def playwright_list_ok(code: str, owner_id: int | None = None) -> bool:
    """**Legacy** best-effort ``playwright test --list`` parse gate for one spec.

    Superseded by ``automation_gate.list_ok_in_project`` (#540), which lists the
    whole persistent automation project. This function is kept **only** for
    ``project_id IS NULL`` specs — every spec that existed before #540 — and must
    keep behaving exactly as it always has for the lifetime of those runs. Do not
    extend it; new work belongs in :mod:`app.services.automation_gate`.

    Because it collects the spec **alone in an empty temp dir**, any import of a
    shared page object or of ``@q-agent/playwright-base`` fails collection here.
    That is the blocker the project-aware gate dissolves, and the reason this
    path must never be used for a project-backed spec.

    Writes ``code`` to a throwaway spec in a temp dir under the caller's scoped
    specs workspace and runs ``playwright test --list`` against it.

    Args:
        code: The generated Playwright/TypeScript spec source to parse-check.
        owner_id: The owning Run's ``owner_id`` — resolves the scoped specs
            dir the throwaway gate dir is created under (ADR 0009 §1); ``None``
            resolves to the shared namespace.

    Returns:
        ``False`` only when Playwright ran but FAILED to parse/collect the spec (a
        definitive syntax/collection error) — the caller then treats the spec like
        a gate rejection and keeps any previous good spec. Returns ``True`` when the
        spec lists cleanly OR when the check cannot run at all (no local Playwright
        install, timeout, OS error): the check is an optimization and must never
        block generation when it is simply unavailable.
    """
    bin_path = _resolve_list_bin()
    if bin_path is None:
        return True  # skip: nothing to parse with
    try:
        specs_dir = scoped_specs_dir(owner_id)
        specs_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(specs_dir)) as tmp:
            tmp_dir = Path(tmp)
            (tmp_dir / "playwright.config.ts").write_text(
                "import { defineConfig } from '@playwright/test';\n"
                "export default defineConfig({ testDir: '.' });\n",
                encoding="utf-8",
            )
            (tmp_dir / "_gate.spec.ts").write_text(code, encoding="utf-8")
            nm = str(settings.playwright_node_modules)
            env = os.environ.copy()
            env["NODE_PATH"] = nm + (
                os.pathsep + env["NODE_PATH"] if env.get("NODE_PATH") else ""
            )
            proc = subprocess.run(  # noqa: S603
                [bin_path, "test", "--list", "_gate.spec.ts"],
                cwd=str(tmp_dir),
                capture_output=True,
                text=True,
                timeout=60,
                shell=True,  # noqa: S602 - .cmd resolution on Windows
                env=env,
            )
            return proc.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("playwright_list_ok skipped ({}): {}", type(exc).__name__, exc)
        return True  # skip on any inability to run
