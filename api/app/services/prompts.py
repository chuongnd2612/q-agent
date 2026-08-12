"""Prompt builders for the Claude CLI calls used by the AI pipeline.

Each builder returns a single prompt string that instructs Claude to respond
with strict JSON matching the shape the caller (``app.services.ai_service``)
will parse. Keeping the prompts here (rather than inline) makes the expected
JSON contracts easy to find and change in one place.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.models.ticket import Ticket
from app.services.spec_examples import _keywords

ANALYSIS_JSON_SHAPE = """{
  "businessRules": string[],
  "functionalRequirements": string[],
  "validationRules": string[],
  "risks": string[],
  "edgeCases": string[],
  "missingInformation": string[],
  "suggestedScope": string,
  "suggestedRepo": string
}"""

CASES_JSON_SHAPE = """[
  {
    "title": string,
    "objective": string,
    "precondition": string,
    "testData": [ { "field": string, "value": string } ],
    "steps": [ { "a": string, "e": string } ],
    "linkedAc": string[],
    "priority": "High" | "Medium" | "Low",
    "testType": string,
    "automation": "Playwright" | "Selenium" | "Cypress" | "Manual",
    "platform": string
  }
]"""

CASE_JSON_SHAPE = """{
  "title": string,
  "objective": string,
  "precondition": string,
  "testData": [ { "field": string, "value": string } ],
  "steps": [ { "a": string, "e": string } ],
  "linkedAc": string[],
  "priority": "High" | "Medium" | "Low",
  "testType": string,
  "automation": "Playwright" | "Selenium" | "Cypress" | "Manual",
  "platform": string
}"""

REVIEW_JSON_SHAPE = """{
  "verdict": "approve" | "approve-with-changes" | "reject",
  "coverageGaps": string[],
  "additionalCases": [
    {
      "title": string,
      "objective": string,
      "precondition": string,
      "testData": [ { "field": string, "value": string } ],
      "steps": [ { "a": string, "e": string } ],
      "linkedAc": string[],
      "priority": "High" | "Medium" | "Low",
      "testType": string,
      "automation": "Playwright" | "Selenium" | "Cypress" | "Manual",
      "platform": string
    }
  ]
}"""

AUTOMATION_REVIEW_JSON_SHAPE = """{
  "verdict": "approve" | "approve-with-changes" | "reject",
  "findings": [
    { "severity": "Critical" | "Major" | "Minor" | "Nit", "message": string }
  ]
}"""


def _ticket_context(ticket: Ticket) -> str:
    """Render the ticket fields Claude needs: title, description, acceptance criteria."""
    ac = "\n".join(f"- {item}" for item in (ticket.acceptance_criteria or [])) or "(none provided)"
    return (
        f"Ticket {ticket.external_id}: {ticket.title}\n\n"
        f"Description:\n{ticket.description or '(none provided)'}\n\n"
        f"Acceptance Criteria:\n{ac}"
    )


def _rank_by_relevance(
    items: list[dict], text_fn: Callable[[dict], str], query_keywords: set[str], limit: int
) -> list[dict]:
    """Order KB items by keyword overlap with a query, then truncate to ``limit``.

    Replaces a blind ``items[:limit]`` slice (#182) so a project with more items
    than the cap doesn't always lose whichever ones happen to sort last — the
    ones most relevant to the case being generated win instead. Ties (including
    the "no query" case, where every score is 0) keep their original order, so
    behavior is unchanged when ``query_keywords`` is empty.

    Args:
        items: The raw KB list (routes or selectors, dict entries).
        text_fn: Extracts the text of one item to score against the query.
        query_keywords: Keyword set to score against (see ``spec_examples._keywords``).
        limit: Max items to keep.

    Returns:
        The top ``limit`` items, most relevant first.
    """
    scored = [
        (len(query_keywords & _keywords(text_fn(item))), idx, item)
        for idx, item in enumerate(items)
    ]
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [item for _, _, item in scored[:limit]]


def _verified_first(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable-sort KB entries so runtime-verified ones come first.

    Entries stamped with a truthy ``verified_at_runtime`` (selectors/routes the
    DOM exploration agent confirmed live, per ADR 0010 §6) are preferred over
    source-inferred ones, so downstream generation reaches for them first. The
    sort is stable, so the upstream relevance order (see ``_rank_by_relevance``)
    is preserved within the verified and unverified groups alike.

    Args:
        items: KB entries (route or selector dicts) already relevance-ranked.

    Returns:
        The same entries, verified-at-runtime first, order otherwise unchanged.
    """
    return sorted(
        items,
        key=lambda it: 0 if isinstance(it, dict) and it.get("verified_at_runtime") else 1,
    )


def render_project_context(
    context: dict[str, Any] | None, *, include_secrets: bool = False, rank_query: str = ""
) -> str:
    """Render the resolved Project Knowledge Base + config for a prompt.

    This is the shared project grounding that lets downstream skills reuse real
    domain terminology, routes, selectors, auth, and (for automation) concrete
    URLs and credentials instead of inventing placeholders.

    Args:
        context: Output of ``project_config_service.build_context`` (or None).
        include_secrets: When True, test-account passwords are included verbatim
            (used ONLY by automation generation, per the "literal values" choice).
            When False, only roles/usernames are shown.
        rank_query: Optional free text (typically the target case's title + steps)
            used to relevance-rank ``routes``/``selectors`` before truncating to the
            injected cap (#182), instead of always keeping the first N. Empty
            (default) preserves the prior blind-slice behavior.

    Returns:
        A markdown block, or an empty string when there is no project context.
    """
    if not context or not context.get("projectKey"):
        return ""

    query_keywords = _keywords(rank_query)

    lines = ["Project context (from the Project Knowledge Base — reuse this, do not invent):"]
    if context.get("baseUrl"):
        lines.append(f"- Base URL: {context['baseUrl']}")
    if context.get("domain"):
        lines.append(f"- Business domain: {context['domain']}")
    if context.get("architecture"):
        lines.append(f"- Architecture: {context['architecture']}")
    if context.get("businessEntities"):
        lines.append(f"- Business entities: {', '.join(context['businessEntities'])}")
    if context.get("locator"):
        lines.append(f"- Locator strategy: {context['locator']}")

    routes = context.get("routes") or []
    if routes:
        ranked_routes = _verified_first(_rank_by_relevance(
            routes, lambda r: f"{r.get('path', '')} {r.get('description', '')}", query_keywords, 20
        ))
        rendered = "; ".join(
            f"{r.get('path', '')} ({r.get('description', '')})"
            + (" ✓ runtime-verified" if r.get("verified_at_runtime") else "")
            for r in ranked_routes
        )
        lines.append(
            "- Application routes (prefer ✓ runtime-verified — confirmed live "
            f"over source-inferred): {rendered}"
        )

    selectors = context.get("selectors") or []
    if selectors:
        ranked_selectors = _verified_first(_rank_by_relevance(
            selectors,
            lambda s: f"{s.get('screen', '')} {s.get('element', '')} {s.get('selector', '')}",
            query_keywords,
            30,
        ))
        rendered = "; ".join(
            f"{s.get('screen', '')}:{s.get('element', '')}=`{s.get('selector', '')}`"
            + (
                f" ✓ runtime-verified (strategy: {s.get('strategy', 'css')})"
                if s.get("verified_at_runtime")
                else ""
            )
            for s in ranked_selectors
        )
        lines.append(
            "- Known selectors (prefer ✓ runtime-verified — confirmed live "
            f"over source-inferred): {rendered}"
        )

    auth = context.get("auth") or {}
    if auth.get("login_flow") or auth.get("login_url"):
        lines.append(
            f"- Auth: {auth.get('login_flow', '')} "
            f"(login URL: {auth.get('login_url', '—')}, "
            f"storageState: {auth.get('storage_state', '—')})"
        )

    # NAMES ONLY, and labelled as such (#542): these come from indexing the
    # customer's own repo, not from the automation project this spec is generated
    # into. #178 died because they read like importable modules — a spec that turns
    # one of these names into `import … from '../../pages/X'` fails collection.
    #
    # #544 supersedes the *importability* question entirely: the AUTOMATION PLAN
    # block (built from `automation_project_service.inventory()`) is now the single
    # source of truth for what can be imported, so the label points there rather
    # than leaving the reader to infer it.
    for label, key in (("Page objects", "pageObjectNames"), ("Fixtures", "fixtureNames"),
                       ("Utilities", "utilities")):
        vals = context.get(key) or []
        if vals:
            lines.append(
                f"- {label} that exist in the product repo (names only — reuse the "
                f"naming/vocabulary; these are NOT importable modules, and only the "
                f"AUTOMATION PLAN block says what is): {', '.join(vals)}"
            )

    accounts = context.get("testAccounts") or []
    if accounts:
        if include_secrets:
            rendered = "; ".join(
                f"{a.get('role', 'account')}: username=`{a.get('username', '')}` "
                f"password=`{a.get('password', '')}`"
                for a in accounts
            )
            lines.append(f"- Test accounts (use these real credentials directly): {rendered}")
        else:
            rendered = "; ".join(
                f"{a.get('role', 'account')} ({a.get('username', '')})" for a in accounts
            )
            lines.append(f"- Test-account roles available: {rendered}")

    return "\n".join(lines)


def render_base_framework_api() -> str:
    """Render the public surface of ``@q-agent/playwright-base`` for a spec prompt (#542).

    Layer 2 of the architecture in
    ``docs/QAgent_Playwright_Automation_Architecture_Update.md`` is a real npm
    package (published by #539) that every automation project depends on. The
    model can only *reuse* it if it knows what is in it — otherwise it reinvents
    an inline login flow and its own wait helpers, which is exactly the generated
    bulk this slice removes (doc §17).

    Kept deliberately as a hand-maintained list of **names only**, not signatures:
    it must stay small enough to sit in every generation prompt. The authoritative
    surface is ``playwright-base/src/index.ts``; when that changes, change this.

    Returns:
        A markdown block naming the exports a generated spec is expected to use.
    """
    return "\n".join(
        [
            "Base framework — `@q-agent/playwright-base` (Layer 2; already a "
            "dependency of this automation project). Reuse these instead of "
            "reimplementing them, and do NOT import `@playwright/test` directly:",
            "- `import { test, expect } from '@q-agent/playwright-base';` — `test` is "
            "Playwright's `test` extended with always-on evidence capture (distilled "
            "DOM, console, network) and replay of the run's saved session. It is a "
            "drop-in replacement: `test('…', async ({ page }) => { … })` still works.",
            "- Auth (doc §17): `createAuthenticatedTest({ login, isAuthenticated?, "
            "entryUrl? })` (adds an `authenticatedPage` fixture), `formLoginFlow({ "
            "loginUrl, credentials, fields })`, `performFormLogin`, `ensureLoggedIn`, "
            "`hasStorageState`, `saveAuthState`, `applySessionStorage`.",
            "- Web-first assertion helpers: `expectVisible`, `expectHidden`, "
            "`expectText`, `expectContainsText`, `expectValue`, `expectChecked`, "
            "`expectEnabled`, `expectDisabled`, `expectCount`, `expectAttribute`, "
            "`expectClass`, `expectUrl`, `expectTitle`, `expectRowVisible`, "
            "`expectAllVisible`, `expectEventuallyGone` (plus plain `expect`).",
            "- Waiting/retry — never a hard sleep: `waitFor`, `retry`, `withTimeout` "
            "(`sleep` exists for setup only; never use it to wait for the UI).",
            "- Dynamic test data: `uniqueId`, `uniqueSuffix`, `randomEmail`, "
            "`randomString`, `randomInt`, `randomDigits`, `randomPick`, `isoDate`, "
            "`today`, `addDays`, `daysFromNow`, `formatDate`, `timestampSlug`.",
            "- Files / API / logging / config: `uploadFiles`, `downloadTo`, `readJson`, "
            "`writeJson`, `createApiClient`, `logger`, `createLogger`, `env`, "
            "`envBool`, `envInt`, `resolveUrl`.",
        ]
    )


def render_dom_snapshot(dom_snapshot: dict[str, Any] | None, *, max_elements: int = 60) -> str:
    """Render a distilled live-DOM snapshot (captured at run/failure time) for a heal prompt.

    Gives the fixer the page's REAL interactable elements so it can pick grounded
    locators instead of guessing — especially valuable when the KB has no selectors
    (the ``blocked`` case). Each line lists the stable identifiers Playwright
    locators care about (test id, role, name, text, …).

    Args:
        dom_snapshot: The parsed ``qagent-dom-distilled`` payload — ``{path, url,
            elements: [{tag, role?, testId?, id?, name?, text?, placeholder?, type?}]}``
            — or None.
        max_elements: Cap on rendered elements to bound prompt size. Elements
            carrying an explicit identifier are preferred over anonymous ones.

    Returns:
        A markdown block, or "" when there is no usable DOM snapshot.
    """
    if not dom_snapshot:
        return ""
    elements = dom_snapshot.get("elements") or []
    if not elements:
        return ""

    def _has_identifier(el: dict) -> bool:
        return bool(
            el.get("testId") or el.get("role") or el.get("name")
            or el.get("text") or el.get("id") or el.get("placeholder")
        )

    # Prefer elements with a stable identifier; keep source order within the group.
    identified = [e for e in elements if isinstance(e, dict) and _has_identifier(e)]
    ranked = (identified or [e for e in elements if isinstance(e, dict)])[:max_elements]

    def _fmt(el: dict) -> str:
        parts = [el.get("tag", "")]
        for key, label in (
            ("testId", "testid"), ("role", "role"), ("name", "name"),
            ("type", "type"), ("id", "id"), ("placeholder", "placeholder"),
        ):
            if el.get(key):
                parts.append(f"{label}={el[key]!r}")
        if el.get("text"):
            parts.append(f"text={el['text']!r}")
        return "  - " + " ".join(p for p in parts if p)

    lines = ["Live DOM captured at failure — real interactable elements on the page "
             "(prefer these over guesses):"]
    loc = dom_snapshot.get("path") or dom_snapshot.get("url") or ""
    if loc:
        lines.append(f"- Current page: {loc}")
    lines.extend(_fmt(e) for e in ranked)
    if len(elements) > len(ranked):
        lines.append(f"  … ({len(elements) - len(ranked)} more elements omitted)")
    # These are REAL observed values — use them verbatim. Templated placeholders
    # (${BASE_URL}, ${EMPLOYER_ID}, …) are treated as invented refs by the gate and
    # will be rejected, so concrete literals are required, not env-var templates.
    lines.append(
        "Ground the fix in these exact values: use the 'Current page' path above as a "
        "literal string in page.goto(...) and use the exact test ids / selectors listed. "
        "Do NOT use ${...} template variables or placeholder URLs — write concrete literals."
    )
    return "\n".join(lines)


def _repo_section(context: dict[str, Any] | None) -> str:
    """Render the 'pick the target repo' instruction block, or "" when the
    project has no repositories. Shared by the analysis and combined prompts."""
    repo_options = (context or {}).get("repoOptions") or []
    if not repo_options:
        return ""
    repo_lines = "\n".join(
        f"- {opt.get('name', '')}" + (f" (hint: {opt['hint']})" if opt.get("hint") else "")
        for opt in repo_options
        if opt.get("name")
    )
    return (
        "The project has these repositories. Decide which single one this work "
        "item most likely targets and set \"suggestedRepo\" to that repo NAME "
        "exactly as written below (or \"\" if you are unsure):\n"
        f"{repo_lines}\n\n"
    )


def build_combined_prompt(
    ticket: Ticket, max_cases: int = 8, context: dict[str, Any] | None = None
) -> str:
    """One-call prompt that both analyzes the work item AND writes the baseline
    happy-path cases (#174), returning ``{"analysis": {...}, "cases": [...]}``.

    Merges the analysis and generation stages into a single Claude call to cut
    per-ticket CLI/overhead cost. The caller composes BOTH the requirement-analyst
    and test-case-generator skills as the system prompt so neither stage loses its
    methodology; this prompt carries the explicit output contract for both.
    """
    project_block = render_project_context(context)
    project_section = f"{project_block}\n\n" if project_block else ""
    repo_section = _repo_section(context)
    return (
        "You are a senior QA analyst and engineer. In a SINGLE response, do two "
        "things for the work item below.\n\n"
        f"{project_section}"
        f"{repo_section}"
        f"{_ticket_context(ticket)}\n\n"
        "STEP 1 — Analyze. Identify: business rules implied by the requirements, "
        "functional requirements, validation rules (input constraints, formats, "
        "boundaries), risks, edge cases worth testing, any missing information to "
        "clarify with the author, a one-sentence suggested test scope, and the "
        "single most likely target repository name (suggestedRepo).\n\n"
        "STEP 2 — Generate happy-path cases from that analysis. Write a "
        "lightweight, review-friendly set of ADO-style manual test cases covering "
        "ONLY the primary successful flow (happy path) — aim for one successful "
        "scenario per acceptance criterion, merging near-duplicate journeys. Do "
        "NOT generate negative, invalid-input, boundary, permission, empty-state "
        "or error-handling cases: those are added later in a separate review "
        f"stage. Generate AT MOST {max_cases} cases.\n\n"
        "Each case must have: a clear title; a one-line objective; a precondition; "
        "any test data as testData [{field, value}]; steps where each has an "
        "action (a) and expected result (e); linkedAc (the acceptance criteria it "
        "covers); a priority (High/Medium/Low); a testType (typically "
        "'Functional'); an automation type (Playwright/Selenium/Cypress/Manual); "
        "and a platform (e.g. Web). DEFAULT automation to 'Playwright' for web UI "
        "flows a browser can drive; use 'Manual' only when a case genuinely cannot "
        "be automated reliably.\n\n"
        "Respond with ONLY a single JSON object of this exact shape:\n"
        "{\n"
        f"  \"analysis\": {ANALYSIS_JSON_SHAPE},\n"
        f"  \"cases\": {CASES_JSON_SHAPE}\n"
        "}"
    )


def build_review_prompt(
    ticket: Ticket,
    analysis: dict,
    existing_cases: list[dict],
    max_cases: int = 8,
    context: dict[str, Any] | None = None,
) -> str:
    """Prompt for the second stage: review the happy-path set and fill coverage gaps.

    The ``test-case-generator`` stage intentionally produces only the primary
    successful flow per acceptance criterion (see :func:`build_combined_prompt`).
    This stage asks the reviewer to audit that set against the requirement
    analysis and then GENERATE the deferred coverage — negative, invalid-input,
    boundary, permission, empty-state and error-handling cases — that fill the
    gaps, without duplicating the existing happy-path cases.

    Returns a JSON object with ``verdict``, ``coverageGaps`` (ACs/business rules
    not yet covered) and ``additionalCases`` (new cases in the standard case
    shape). ``max_cases`` caps how many additional cases to add.
    """
    project_block = render_project_context(context)
    project_section = f"{project_block}\n\n" if project_block else ""
    return (
        "You are a senior QA reviewer. The happy-path test cases below were "
        "generated to cover the PRIMARY successful flow of each acceptance "
        "criterion only; edge, negative, boundary, permission, empty-state and "
        "error-handling coverage was deliberately deferred to you.\n\n"
        "Review the existing cases against the ticket and the prior requirement "
        "analysis, then:\n"
        "1. List the concrete coverage gaps (acceptance criteria, business rules, "
        "validation rules, risks and edge cases from the analysis that the "
        "happy-path set does not yet exercise).\n"
        f"2. GENERATE AT MOST {max_cases} additional test cases that fill those "
        "gaps — negative, invalid-input, boundary, permission and error-handling "
        "scenarios. Do NOT duplicate or restate the existing happy-path cases.\n"
        "3. Give an overall verdict.\n\n"
        f"{project_section}"
        f"{_ticket_context(ticket)}\n\n"
        f"Prior analysis (JSON):\n{analysis}\n\n"
        f"Existing happy-path cases (JSON):\n{existing_cases}\n\n"
        "Each additional case uses the same fields as the existing cases: title, "
        "objective, precondition, testData ([{field, value}]), steps ([{a, e}]), "
        "linkedAc (the acceptance criteria it covers), priority (High/Medium/Low), "
        "testType (e.g. Negative, Boundary, Security, Permission), automation "
        "(Playwright/Selenium/Cypress/Manual), and platform (e.g. Web).\n\n"
        f"Respond with ONLY a JSON object of this exact shape:\n{REVIEW_JSON_SHAPE}"
    )


def build_case_regenerate_prompt(
    ticket: Ticket, analysis: dict, existing_case: dict, context: dict[str, Any] | None = None
) -> str:
    """Prompt asking Claude to regenerate a single test case, keeping its intent/code.

    ``context`` is the resolved Project Knowledge Base — passed so the rewrite
    reuses real routes, roles and selectors instead of inventing them (#183),
    matching :func:`build_combined_prompt`.

    Returns a JSON object matching :data:`CASE_JSON_SHAPE`.
    """
    project_block = render_project_context(context)
    project_section = f"{project_block}\n\n" if project_block else ""
    return (
        "You are a senior QA engineer. Rewrite/improve the single test case below "
        "for the given ticket, using the prior requirement analysis for context. "
        "Keep it focused on the same testing intent and scope, but improve its "
        "clarity and correctness.\n\n"
        f"{project_section}"
        f"{_ticket_context(ticket)}\n\n"
        f"Prior analysis (JSON):\n{analysis}\n\n"
        f"Existing test case (JSON):\n{existing_case}\n\n"
        "Return an improved version of this single test case with the same "
        "fields: title, objective, precondition, testData ([{field, value}]), "
        "steps ([{a, e}]), linkedAc, priority, testType, automation, platform.\n\n"
        f"Respond with ONLY a JSON object of this exact shape:\n{CASE_JSON_SHAPE}"
    )


def build_automation_review_prompt(
    code: str, case: Any, context: dict[str, Any] | None = None
) -> str:
    """Prompt for ``automation-reviewer``: statically review a gate-passed spec.

    Runs AFTER the deterministic placeholder / flaky-pattern gate (see
    ``app.services.placeholder_gate``) has already passed a spec — this stage's
    job is what regex heuristics can't catch: correctness against the case's
    expected results, reuse discipline, and subtler flakiness/locator-quality
    issues.

    Args:
        code: The generated Playwright/TypeScript spec source to review.
        case: The source ``TestCase`` (title/precondition/steps) the spec
            automates, used to check the spec actually covers it.
        context: Resolved project context (Knowledge Base) for convention checks.

    Returns:
        A prompt instructing Claude to respond with a JSON object matching
        :data:`AUTOMATION_REVIEW_JSON_SHAPE`. A ``Critical`` finding is treated
        like a gate rejection by the caller (see ``app.routers.automation``).
    """
    project_block = render_project_context(context)
    project_section = f"{project_block}\n\n" if project_block else ""
    steps_lines = "\n".join(
        f"  {i + 1}. Action: {step.get('a', '')} | Expected: {step.get('e', '')}"
        for i, step in enumerate(getattr(case, "steps", None) or [])
    )
    return (
        "You are a senior QA automation reviewer. Statically review the "
        "Playwright + TypeScript spec below against the source test case and "
        "the Project Knowledge Base. Focus on correctness (does it assert every "
        "Expected Result?), flakiness risk (hard waits, non-web-first "
        "assertions, races), locator quality, and reuse discipline.\n\n"
        f"{project_section}"
        f"Test case:\nTitle: {getattr(case, 'title', '')}\n"
        f"Precondition: {getattr(case, 'precondition', '') or 'None'}\n"
        f"Steps:\n{steps_lines or '  (none provided)'}\n\n"
        f"Generated spec:\n```typescript\n{(code or '').strip()}\n```\n\n"
        "Rate each finding: Critical (the spec doesn't test the intended "
        "behavior, or will fail/pass incorrectly) / Major (real flakiness risk "
        "or brittle locators) / Minor (convention deviation) / Nit (polish).\n\n"
        f"Respond with ONLY a JSON object of this exact shape:\n{AUTOMATION_REVIEW_JSON_SHAPE}"
    )
