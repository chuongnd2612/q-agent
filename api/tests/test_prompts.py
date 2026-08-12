"""Tests for prompt builders in ``app.services.prompts`` (#182 relevance ranking)."""

from __future__ import annotations

from app.services.prompts import render_dom_snapshot, render_project_context


def test_render_project_context_ranks_routes_by_relevance():
    """With more routes than the injected cap, the one relevant to the query
    survives instead of being cut by a blind ``[:20]`` slice."""
    routes = [{"path": f"/noise-{i}", "description": "unrelated"} for i in range(25)]
    routes.append({"path": "/invoices/refund", "description": "Refund an invoice"})
    context = {"projectKey": "P", "routes": routes}

    block = render_project_context(context, rank_query="Refund an invoice from the invoices screen")
    assert "/invoices/refund" in block


def test_render_project_context_ranks_selectors_by_relevance():
    selectors = [
        {"screen": "Noise", "element": f"el-{i}", "selector": f"#noise-{i}"} for i in range(35)
    ]
    selectors.append({"screen": "Login", "element": "SubmitButton", "selector": "#login-submit"})
    context = {"projectKey": "P", "selectors": selectors}

    block = render_project_context(context, rank_query="Submit the login form")
    assert "#login-submit" in block


def test_render_project_context_no_query_keeps_prior_blind_slice_order():
    """Empty ``rank_query`` (the default) must not change existing behavior: the
    first N items in KB order are kept, exactly like the old ``[:20]`` slice."""
    routes = [{"path": f"/r{i}", "description": ""} for i in range(25)]
    context = {"projectKey": "P", "routes": routes}

    block = render_project_context(context)
    assert "/r0" in block
    assert "/r19" in block
    assert "/r20" not in block
    assert "/r24" not in block


def test_render_project_context_verified_selector_first_and_tagged():
    """A ``verified_at_runtime`` selector renders BEFORE an unverified one for the
    same screen, is tagged ``✓ runtime-verified``, and surfaces its strategy (#329)."""
    selectors = [
        {"screen": "Login", "element": "Submit", "selector": "#src-inferred"},
        {
            "screen": "Login",
            "element": "Submit",
            "selector": "[data-testid=login-btn]",
            "strategy": "data-testid",
            "verified_at_runtime": "2026-07-15T00:00:00Z",
        },
    ]
    context = {"projectKey": "P", "selectors": selectors}

    block = render_project_context(context)
    line = next(ln for ln in block.splitlines() if "Known selectors" in ln)
    assert line.index("[data-testid=login-btn]") < line.index("#src-inferred")
    assert "✓ runtime-verified (strategy: data-testid)" in line


def test_render_project_context_verified_route_first_and_tagged():
    """A ``verified_at_runtime`` route renders before an unverified one and is tagged (#329)."""
    routes = [
        {"path": "/a", "description": "source-inferred"},
        {"path": "/b", "description": "runtime", "verified_at_runtime": "2026-07-15T00:00:00Z"},
    ]
    context = {"projectKey": "P", "routes": routes}

    block = render_project_context(context)
    line = next(ln for ln in block.splitlines() if "Application routes" in ln)
    assert line.index("/b") < line.index("/a")
    assert "✓ runtime-verified" in line


def test_render_dom_snapshot_lists_identified_elements():
    """The distilled DOM block surfaces real element identifiers and the current page."""
    snapshot = {
        "path": "/login",
        "elements": [
            {"tag": "input", "testId": "email", "type": "email"},
            {"tag": "button", "role": "button", "text": "Sign in"},
            {"tag": "div"},  # anonymous — no identifier
        ],
    }
    block = render_dom_snapshot(snapshot)
    assert "Live DOM captured at failure" in block
    assert "/login" in block
    assert "testid='email'" in block
    assert "text='Sign in'" in block


def test_render_dom_snapshot_empty_is_blank():
    assert render_dom_snapshot(None) == ""
    assert render_dom_snapshot({"elements": []}) == ""


def test_build_fix_prompt_includes_discovered_selector():
    """A DOM snapshot passed to the fixer prompt surfaces its real selectors."""
    from types import SimpleNamespace

    from app.services.spec_service import _build_fix_prompt

    case = SimpleNamespace(
        title="Sign in", precondition=None, steps=[], test_data=[],
        ticket_external_id="TCK-1", code="TC-01",
    )
    snapshot = {"path": "/login", "elements": [{"tag": "input", "testId": "username"}]}
    prompt = _build_fix_prompt(
        case, "test('Sign in', async () => {});", "locator not found",
        dom_snapshot=snapshot,
    )
    assert "Live DOM captured at failure" in prompt
    assert "testid='username'" in prompt


def _auth_policy_case():
    """A minimal TestCase stand-in for the auth-policy prompt assertions (#291).

    ``test_data=[]`` is required: every spec prompt renders it (``_render_test_data``),
    so a stand-in without the attribute makes the builder raise ``AttributeError``
    before any prompt text exists to assert on (part of the #469 red baseline).
    """
    from types import SimpleNamespace

    return SimpleNamespace(
        title="Sign in", precondition=None, steps=[], test_data=[],
        ticket_external_id="TCK-1", code="TC-01",
    )


def _assert_auth_policy(prompt: str):
    """Every spec prompt must forbid mocking/bypassing auth and any 'Auth note'
    narration, and point at the saved manual-login session instead (#291)."""
    assert "Do NOT mock, stub, intercept, or bypass authentication" in prompt
    assert "/api/sessions/me" in prompt
    assert "VITE_BYPASS_AUTH" in prompt
    assert "saved manual-login session" in prompt
    assert '"Auth note"' in prompt
    # #542: the policy no longer offers "log in inline" as an equal option — the
    # spec consumes the shared authenticated session (doc §17).
    assert "Do NOT re-implement login" in prompt


def test_build_prompt_forbids_mocking_auth_and_narration():
    """Initial generation carries the no-mock-auth / no-narration policy (#291)."""
    from app.services.spec_service import _build_prompt

    _assert_auth_policy(_build_prompt(_auth_policy_case()))


def test_build_fix_prompt_forbids_mocking_auth_and_narration():
    """Self-heal carries the policy too, so it can't reintroduce mocked auth (#291)."""
    from app.services.spec_service import _build_fix_prompt

    prompt = _build_fix_prompt(
        _auth_policy_case(), "test('Sign in', async () => {});", "assertion failed"
    )
    _assert_auth_policy(prompt)


def test_build_chat_edit_prompt_forbids_mocking_auth_and_narration():
    """AI chat-edit carries the policy too (#291)."""
    from app.services.spec_service import _build_chat_edit_prompt

    prompt = _build_chat_edit_prompt(
        _auth_policy_case(), "test('Sign in', async () => {});",
        "add an assertion", context=None,
    )
    _assert_auth_policy(prompt)


# ------------------------------------------------- layered spec contract (#542)


def test_render_base_framework_api_names_the_reusable_surface():
    """The prompt must tell the model what `@q-agent/playwright-base` exports, or it
    reinvents login/waits/assertions instead of reusing them."""
    from app.services.prompts import render_base_framework_api

    block = render_base_framework_api()
    assert "@q-agent/playwright-base" in block
    for export in (
        "test", "expect", "createAuthenticatedTest", "formLoginFlow",
        "expectVisible", "expectUrl", "waitFor", "uniqueSuffix",
    ):
        assert export in block


def test_build_prompt_asks_for_the_layered_shape():
    """Generation must ask for the base-package import, one `test()`, and the real
    two-levels-deep asset paths — the #178 contradiction resolved the other way."""
    from app.services.spec_service import _build_prompt

    prompt = _build_prompt(_auth_policy_case())
    assert "import { test, expect } from '@q-agent/playwright-base';" in prompt
    assert "Never import '@playwright/test' directly" in prompt
    assert "../../pages/Foo" in prompt
    assert "Emit exactly ONE `test(...)` block" in prompt
    # The base-package surface is injected so reuse is possible, not just asked for.
    assert "expectVisible" in prompt


def test_build_prompt_forbids_inventing_page_object_imports():
    """#544 replaces #542's reference-spec proof with the AUTOMATION PLAN as the sole
    import authorization; #545 **inverts the default** on top of that.

    Deliberate wording change (#545): the old bullet told the generator to keep
    locators inline "for any capability the plan marks as still to be created,
    because a later stage authors those files". That later stage now runs BEFORE
    generation, so the sentence was false. A page object is now the default and an
    inline locator is the exception — asserted here and in the matching skill test,
    which must move in the same commit (#178 discipline)."""
    from app.services.spec_service import _build_prompt

    prompt = _build_prompt(_auth_policy_case())
    assert "AUTOMATION PLAN block above is the exhaustive list" in prompt
    assert "Import nothing else from `../../pages/`" in prompt
    assert "those names describe the product repo, not this" in prompt
    # Inverted: page object by default, inline locator as the exception.
    assert "that is the DEFAULT, not an aspiration" in prompt
    assert "An inline locator is the EXCEPTION, not the rule." in prompt
    # The superseded rules are gone from the prompt half.
    assert "REFERENCE SPEC" not in prompt
    assert "A later stage authors those files" not in prompt


def test_build_prompt_injects_the_plan_when_one_exists():
    """A plan's importable list is what makes an asset import legal (#544)."""
    from app.services import automation_planner_service as planner
    from app.services.spec_service import _build_prompt

    plan = planner.normalize(
        {"pages": [{"name": "UserPage", "path": "pages/UserPage.ts", "action": "reuse"}]},
        [{"path": "pages/UserPage.ts", "kind": "page", "exports": ["UserPage"],
          "methods": ["openCreateUser()"]}],
    )
    prompt = _build_prompt(_auth_policy_case(), plan=plan)
    assert "IMPORTABLE" in prompt
    assert "pages/UserPage.ts" in prompt and "openCreateUser()" in prompt


def test_build_fix_prompt_forbids_flattening_the_architecture():
    """Heal rewrites only `spec.code` until #547, so it must be told that re-inlining
    locators/login is a rejection — otherwise it "fixes" specs by undoing the layering."""
    from app.services.spec_service import _build_fix_prompt

    prompt = _build_fix_prompt(
        _auth_policy_case(), "test('Sign in', async () => {});", "assertion failed"
    )
    assert "Preserve the spec's architecture" in prompt
    assert "never swap them back to '@playwright/test'" in prompt
    assert "flattens the layering is REJECTED" in prompt


def test_build_chat_edit_prompt_forbids_flattening_the_architecture():
    """The AI chat editor rewrites the whole spec too — same guard (#542)."""
    from app.services.spec_service import _build_chat_edit_prompt

    prompt = _build_chat_edit_prompt(
        _auth_policy_case(), "test('Sign in', async () => {});",
        "add an assertion", context=None,
    )
    assert "Preserve the spec's architecture" in prompt
