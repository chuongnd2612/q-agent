"""Tests for the AI analysis + test-case generation pipeline (app.services.ai_service)."""

from __future__ import annotations

from app.models.run import Run, RunTicket
from app.models.testcase import TestCase
from app.services import ai_service
from app.services.claude_cli import ClaudeError
from app.services.skills import TEST_CASE_REVIEWER

CANNED_ANALYSIS = {
    "businessRules": ["Reset link must be single-use"],
    "functionalRequirements": ["Send reset email on request"],
    "validationRules": ["Email must be a valid format"],
    "risks": ["Link reuse after password change"],
    "edgeCases": ["Expired link clicked"],
    "missingInformation": ["What happens after 3 failed resets?"],
    "suggestedScope": "Cover reset request, link expiry, and reset completion.",
}

CANNED_CASES = [
    {
        "title": "Request reset link with valid email",
        "precondition": "User has a registered account",
        "steps": [{"a": "Submit valid email", "e": "Reset email is sent"}],
        "priority": "High",
        "testType": "Functional",
        "automation": "Playwright",
        "platform": "Web",
    },
    {
        "title": "Reset link expires after 30 minutes",
        "precondition": "A reset link was requested 31 minutes ago",
        "steps": [{"a": "Click expired link", "e": "Error shown, link rejected"}],
        "priority": "Medium",
        "testType": "Negative",
        "automation": "Playwright",
        "platform": "Web",
    },
]

# The second-stage reviewer (#173) audits the happy-path set and returns the
# deferred edge/negative coverage to append.
CANNED_REVIEW = {
    "verdict": "approve-with-changes",
    "coverageGaps": ["No test for reset request against an unregistered email"],
    "additionalCases": [
        {
            "title": "Reset request for unregistered email shows a neutral message",
            "precondition": "Email is not registered",
            "steps": [{"a": "Submit an unknown email", "e": "Neutral confirmation; no account leak"}],
            "priority": "Medium",
            "testType": "Negative",
            "automation": "Playwright",
            "platform": "Web",
        }
    ],
}
# A no-op review (used where the test asserts on the generation cap only).
CANNED_REVIEW_EMPTY = {"verdict": "approve", "coverageGaps": [], "additionalCases": []}


def _make_run(db_session, ticket_external_id: str) -> Run:
    run = Run(code="RUN-200", name="Test run", status="processing")
    db_session.add(run)
    db_session.flush()
    db_session.add(RunTicket(run_id=run.id, ticket_external_id=ticket_external_id, position=0))
    db_session.commit()
    db_session.refresh(run)
    return run


def test_pipeline_creates_analysis_and_cases(db_session, seed_ticket, monkeypatch):
    run = _make_run(db_session, seed_ticket.external_id)

    responses = iter([{"analysis": CANNED_ANALYSIS, "cases": CANNED_CASES}, CANNED_REVIEW])
    monkeypatch.setattr(ai_service, "run_json", lambda *a, **k: next(responses))

    ai_service.run_generation_pipeline(run.id, blocking=True)

    db_session.refresh(run)
    assert run.status == "review"

    run_ticket = db_session.query(RunTicket).filter(RunTicket.run_id == run.id).first()
    assert run_ticket.gen_status == "done"
    assert run_ticket.analysis["suggestedScope"].startswith("Cover reset")
    # Reviewer verdict + coverage gaps recorded alongside the analysis.
    assert run_ticket.analysis["review"]["verdict"] == "approve-with-changes"

    cases = db_session.query(TestCase).filter(TestCase.run_id == run.id).order_by(TestCase.code).all()
    assert len(cases) == 3  # 2 happy-path + 1 reviewer-added
    assert [c.code for c in cases] == ["TC-01", "TC-02", "TC-03"]
    # Happy-path cases are source "ai"; the reviewer's addition is "ai-review".
    assert [c.source for c in cases] == ["ai", "ai", "ai-review"]
    assert all(c.approval == "pending" for c in cases)


def test_pipeline_caps_and_continues_numbering(db_session, seed_ticket, monkeypatch):
    """Respects maxCasesPerTicket and continues codes from existing provider cases."""
    from app.services import settings_store

    run = _make_run(db_session, seed_ticket.external_id)
    monkeypatch.setattr(settings_store, "load_settings", lambda: {"maxCasesPerTicket": 2})
    # Existing provider test cases up to TC-05 -> new codes start at TC-06.
    monkeypatch.setattr(ai_service, "provider_case_offset", lambda db, ticket: 5)

    many = [dict(CANNED_CASES[0]) for _ in range(6)]  # Claude returns 6; cap is 2
    responses = iter([{"analysis": CANNED_ANALYSIS, "cases": many}, CANNED_REVIEW_EMPTY])
    monkeypatch.setattr(ai_service, "run_json", lambda *a, **k: next(responses))

    ai_service.run_generation_pipeline(run.id, blocking=True)

    cases = db_session.query(TestCase).filter(TestCase.run_id == run.id).order_by(TestCase.code).all()
    assert len(cases) == 2  # capped (reviewer added none here)
    assert [c.code for c in cases] == ["TC-06", "TC-07"]  # continued from offset


def test_pipeline_persists_new_contract_fields(db_session, seed_ticket, monkeypatch):
    """objective / testData / linkedAc from the generator are persisted (#177)."""
    run = _make_run(db_session, seed_ticket.external_id)
    rich = [
        {
            "title": "Sign in with valid credentials",
            "objective": "Prove a registered user can sign in",
            "precondition": "User account exists",
            "testData": [{"field": "email", "value": "a@b.com"}],
            "steps": [{"a": "Submit valid credentials", "e": "Dashboard loads"}],
            "linkedAc": ["AC-1", "AC-2"],
            "priority": "High",
            "testType": "Functional",
            "automation": "Playwright",
            "platform": "Web",
        }
    ]
    responses = iter([{"analysis": CANNED_ANALYSIS, "cases": rich}, CANNED_REVIEW_EMPTY])
    monkeypatch.setattr(ai_service, "run_json", lambda *a, **k: next(responses))

    ai_service.run_generation_pipeline(run.id, blocking=True)

    case = db_session.query(TestCase).filter(TestCase.run_id == run.id).one()
    assert case.objective == "Prove a registered user can sign in"
    assert case.test_data == [{"field": "email", "value": "a@b.com"}]
    assert case.linked_ac == ["AC-1", "AC-2"]


def test_pipeline_surfaces_claude_error(db_session, seed_ticket, monkeypatch):
    run = _make_run(db_session, seed_ticket.external_id)

    def _boom(*a, **k):
        raise ClaudeError("CLI not authenticated")

    monkeypatch.setattr(ai_service, "run_json", _boom)

    ai_service.run_generation_pipeline(run.id, blocking=True)

    run_ticket = db_session.query(RunTicket).filter(RunTicket.run_id == run.id).first()
    assert run_ticket.gen_status == "error"
    assert "CLI not authenticated" in run_ticket.analysis_error

    # This test used to assert `run.status == "review"`, which is the bug in #758:
    # every ticket failed, so the run had produced nothing and yet presented as
    # "In review · needs you". `failed_stage="processing"` is what ADR 0005's
    # retry dispatch resumes by re-running generation.
    db_session.refresh(run)
    assert run.status == "failed"
    assert run.failed_stage == "processing"

    assert db_session.query(TestCase).filter(TestCase.run_id == run.id).count() == 0


def test_pipeline_partial_failure_still_reaches_review(db_session, seed_ticket, monkeypatch):
    """One ticket errors, one succeeds — the run is genuinely reviewable (#758).

    The counterpart to the test above, and the reason the decision is "ALL
    tickets failed" rather than "any ticket failed": there is real output to
    review, so failing the whole run would throw it away.
    """
    from app.models.ticket import Ticket

    second = Ticket(
        external_id="SUR-2000",
        provider_kind="ado",
        title="Second work item",
        acceptance_criteria=["Given something, then something"],
    )
    db_session.add(second)
    db_session.commit()

    run = _make_run(db_session, seed_ticket.external_id)
    db_session.add(RunTicket(run_id=run.id, ticket_external_id=second.external_id, position=1))
    db_session.commit()

    def _boom_for_second(*_args, **kwargs):
        # `run_json` is called twice per ticket (generate, then review) and both
        # calls carry the ticket in their `label`, so that is the discriminator —
        # not call order, which would break the moment the pipeline's call count
        # per ticket changes.
        label = str(kwargs.get("label", ""))
        if second.external_id in label:
            raise ClaudeError("rate limited")
        if kwargs.get("skill") == TEST_CASE_REVIEWER:
            return CANNED_REVIEW_EMPTY
        return {"analysis": CANNED_ANALYSIS, "cases": CANNED_CASES}

    monkeypatch.setattr(ai_service, "run_json", _boom_for_second)

    ai_service.run_generation_pipeline(run.id, blocking=True)

    statuses = {
        rt.ticket_external_id: rt.gen_status
        for rt in db_session.query(RunTicket).filter(RunTicket.run_id == run.id).all()
    }
    assert statuses[second.external_id] == "error"
    assert statuses[seed_ticket.external_id] == "done"

    db_session.refresh(run)
    assert run.status == "review"
    # And the surviving ticket's cases were kept, which is the whole point.
    assert db_session.query(TestCase).filter(TestCase.run_id == run.id).count() > 0


def test_pipeline_missing_ticket_sets_error(db_session):
    run = _make_run(db_session, "SUR-9999")

    ai_service.run_generation_pipeline(run.id, blocking=True)

    run_ticket = db_session.query(RunTicket).filter(RunTicket.run_id == run.id).first()
    assert run_ticket.gen_status == "error"
    assert "not found" in run_ticket.analysis_error


def test_regenerate_case_updates_fields_and_sets_edited(db_session, seed_ticket, monkeypatch):
    run = _make_run(db_session, seed_ticket.external_id)
    case = TestCase(
        run_id=run.id,
        ticket_external_id=seed_ticket.external_id,
        code="TC-01",
        title="Old title",
        precondition="Old precondition",
        steps=[{"a": "old action", "e": "old expected"}],
        priority="Low",
        test_type="Functional",
        automation="Playwright",
        platform="Web",
        source="ai",
    )
    db_session.add(case)
    db_session.commit()
    db_session.refresh(case)

    improved = {
        "title": "Improved title",
        "precondition": "Improved precondition",
        "steps": [{"a": "new action", "e": "new expected"}],
        "priority": "High",
        "testType": "Functional",
        "automation": "Playwright",
        "platform": "Web",
    }
    monkeypatch.setattr(ai_service, "run_json", lambda *a, **k: improved)

    updated = ai_service.regenerate_case(db_session, case)

    assert updated.code == "TC-01"  # code preserved
    assert updated.title == "Improved title"
    assert updated.priority == "High"
    assert updated.edited is True


def test_resolve_worker_count_defaults_and_clamps(workspace_dir, monkeypatch):
    """SQLite defaults to sequential; explicit settings are honored + clamped (#179).

    ``workspace_dir`` rebinds the engine to the temp SQLite DB so the dialect
    default is deterministic regardless of the ambient dev database.
    """
    from app.services import settings_store

    monkeypatch.setattr(settings_store, "load_settings", lambda: {})
    assert ai_service._resolve_worker_count() == 1  # sqlite → sequential
    monkeypatch.setattr(settings_store, "load_settings", lambda: {"aiPipelineWorkers": 3})
    assert ai_service._resolve_worker_count() == 3
    monkeypatch.setattr(settings_store, "load_settings", lambda: {"aiPipelineWorkers": 99})
    assert ai_service._resolve_worker_count() == 4  # clamped to 4
    monkeypatch.setattr(settings_store, "load_settings", lambda: {"aiPipelineWorkers": "oops"})
    assert ai_service._resolve_worker_count() == 1  # bad value → sqlite fallback


def test_pipeline_parallel_processes_all_tickets(db_session, seed_ticket, monkeypatch):
    """With aiPipelineWorkers>1 every ticket is processed in its own session (#179)."""
    from app.models.ticket import Ticket
    from app.services import settings_store
    from app.services.skills import TEST_CASE_GENERATOR, TEST_CASE_REVIEWER

    for ext in ("SUR-2001", "SUR-2002"):
        db_session.add(
            Ticket(
                external_id=ext,
                provider_kind="ado",
                title=f"Ticket {ext}",
                work_item_type="User Story",
                status="Ready for QA",
                description="desc",
                acceptance_criteria=["AC1"],
            )
        )
    run = Run(code="RUN-301", name="Parallel run", status="processing")
    db_session.add(run)
    db_session.flush()
    externals = [seed_ticket.external_id, "SUR-2001", "SUR-2002"]
    for i, ext in enumerate(externals):
        db_session.add(RunTicket(run_id=run.id, ticket_external_id=ext, position=i))
    db_session.commit()
    db_session.refresh(run)

    def _dispatch(*_a, **kwargs):  # thread-safe: no shared iterator
        skill = kwargs.get("skill")
        if skill == TEST_CASE_REVIEWER:
            return {"verdict": "approve", "coverageGaps": [], "additionalCases": []}
        if skill == TEST_CASE_GENERATOR:
            return {"analysis": CANNED_ANALYSIS, "cases": CANNED_CASES}
        return CANNED_ANALYSIS

    monkeypatch.setattr(ai_service, "run_json", _dispatch)
    monkeypatch.setattr(
        settings_store, "load_settings", lambda: {"aiPipelineWorkers": 3, "maxCasesPerTicket": 8}
    )

    ai_service.run_generation_pipeline(run.id, blocking=True)

    db_session.expire_all()
    rts = db_session.query(RunTicket).filter(RunTicket.run_id == run.id).all()
    assert {rt.gen_status for rt in rts} == {"done"}
    for ext in externals:
        n = (
            db_session.query(TestCase)
            .filter(TestCase.run_id == run.id, TestCase.ticket_external_id == ext)
            .count()
        )
        assert n == len(CANNED_CASES)
    db_session.refresh(run)
    assert run.status == "review"


#: A planner-agent plan as ``planner_agent_service.normalize_plan`` returns it.
PLANNER_PLAN = {
    "overview": "Password reset flow on the account portal.",
    "auth": "storage state (tests_generated/auth.setup.ts)",
    "scenarios": [
        {
            "title": "Request a reset link",
            "steps": [
                {
                    "action": 'Click the "Forgot password" link on the sign-in page.',
                    "locator": "getByRole('link', { name: 'Forgot password' })",
                    "expect": "The reset request form is shown.",
                    "expectLocator": "getByRole('heading', { name: 'Reset your password' })",
                },
                {
                    "action": "Enter a registered email address and submit.",
                    "locator": "getByLabel('Email')",
                    "expect": "A confirmation banner says the reset email was sent.",
                    "expectLocator": "",
                },
            ],
        }
    ],
    "routes": [{"path": "/reset", "description": "Password reset request"}],
    "selectors": [],
}


def _planner_env(monkeypatch, *, base_url: str = "https://app.test") -> dict:
    """Put the pipeline in live-planner mode with (or without) a resolvable base_url."""
    from app.services import settings_store

    monkeypatch.setattr(
        settings_store, "load_settings",
        lambda: {"testCaseMode": "live-planner", "maxCasesPerTicket": 8},
    )
    context = {"projectKey": "surency", "repo": "web"}
    if base_url:
        context["baseUrl"] = base_url
    monkeypatch.setattr(
        ai_service.project_config_service, "context_for_ticket", lambda *a, **k: context
    )
    return context


def _steps_from_plan_block(prompt: str) -> list[dict]:
    """Stand in for the generator: build case steps out of the plan block in the prompt.

    A real ``test-case-generator`` call is what turns the plan into ``{a, e}``
    steps; this fake does the same mechanically, so that a regression which drops
    the plan from the prompt makes the persisted rows go generic and the
    assertions below fail — rather than passing on canned text that never
    depended on the plan at all.
    """
    from app.services.prompts import PLANNER_PLAN_HEADER

    if PLANNER_PLAN_HEADER not in prompt:
        return [{"a": "Generic step invented from ticket text", "e": "Something happens"}]
    steps: list[dict] = []
    action = ""
    for line in prompt.splitlines():
        stripped = line.strip()
        if stripped.startswith("- expect: "):
            if action:
                steps.append({"a": action, "e": stripped[len("- expect: "):]})
                action = ""
        elif stripped.startswith("- locator:"):
            continue
        elif stripped[:1].isdigit() and "." in stripped:
            action = stripped.split(".", 1)[1].strip()
    return steps


# --------------------------------------------- testCaseMode="live-planner" (#877 → #889)
def test_live_planner_mode_is_off_by_default(db_session, seed_ticket, monkeypatch):
    """`testCaseMode` defaults to "text" — the planner agent never runs unless opted in."""
    run = _make_run(db_session, seed_ticket.external_id)

    def _boom(*a, **k):
        raise AssertionError("plan_ticket() must not run under the default testCaseMode")

    monkeypatch.setattr(ai_service.planner_agent_service, "plan_ticket", _boom)
    responses = iter([{"analysis": CANNED_ANALYSIS, "cases": CANNED_CASES}, CANNED_REVIEW_EMPTY])
    monkeypatch.setattr(ai_service, "run_json", lambda *a, **k: next(responses))

    ai_service.run_generation_pipeline(run.id, blocking=True)

    run_ticket = db_session.query(RunTicket).filter(RunTicket.run_id == run.id).first()
    assert run_ticket.gen_status == "done"


def test_live_planner_cases_carry_the_planner_steps(db_session, seed_ticket, monkeypatch):
    """live-planner + a base_url plans first, and the OBSERVED steps reach the rows (#889)."""
    from app.services.prompts import PLANNER_PLAN_HEADER

    run = _make_run(db_session, seed_ticket.external_id)
    _planner_env(monkeypatch)

    plan_calls: list[dict] = []

    def _fake_plan(run_arg, ticket_arg, context, *, owner_id, target):
        plan_calls.append({"target": target, "context": context, "owner_id": owner_id})
        return PLANNER_PLAN

    monkeypatch.setattr(ai_service.planner_agent_service, "plan_ticket", _fake_plan)
    monkeypatch.setattr(ai_service.planner_agent_service, "merge_plan_to_kb", lambda *a, **k: 3)

    prompts_seen: list[str] = []

    def _fake_run_json(prompt, **k):
        prompts_seen.append(prompt)
        if k.get("skill") == TEST_CASE_REVIEWER:
            return CANNED_REVIEW_EMPTY
        cases = [dict(CANNED_CASES[0], steps=_steps_from_plan_block(prompt))]
        return {"analysis": CANNED_ANALYSIS, "cases": cases}

    monkeypatch.setattr(ai_service, "run_json", _fake_run_json)

    ai_service.run_generation_pipeline(run.id, blocking=True)

    # The planner agent ran exactly once, before either authoring call, targeting
    # the ticket's own text (no case exists yet to derive a target from).
    assert len(plan_calls) == 1
    assert plan_calls[0]["target"]["ticket"] == seed_ticket.external_id
    assert plan_calls[0]["target"]["screen"] == seed_ticket.title

    # Both authoring prompts are grounded in the plan.
    assert len(prompts_seen) == 2
    for prompt in prompts_seen:
        assert PLANNER_PLAN_HEADER in prompt
        assert "getByRole('link', { name: 'Forgot password' })" in prompt

    # Observable effect: the persisted rows carry the PLANNER's steps, not generic text.
    case = (
        db_session.query(TestCase)
        .filter(TestCase.run_id == run.id, TestCase.source == "ai")
        .one()
    )
    assert [s["a"] for s in case.steps] == [
        'Click the "Forgot password" link on the sign-in page.',
        "Enter a registered email address and submit.",
    ]
    assert case.steps[0]["e"] == "The reset request form is shown."
    # And no locator leaks onto a step — TestCase.steps is {a, e} only (#882).
    assert all(set(s) == {"a", "e"} for s in case.steps)


def test_live_planner_merges_plan_locators_into_the_kb(db_session, seed_ticket, monkeypatch):
    """The plan's step-bound locators reach the KB as runtime-verified entries (#889)."""
    from app.services import planner_agent_service

    run = _make_run(db_session, seed_ticket.external_id)
    _planner_env(monkeypatch)
    monkeypatch.setattr(
        ai_service.planner_agent_service, "plan_ticket", lambda *a, **k: PLANNER_PLAN
    )

    merged: list[dict] = []

    def _fake_merge(project_key, repo, discovered, *, owner_id=None, source="exploration"):
        merged.append(
            {"project_key": project_key, "repo": repo, "discovered": discovered, "source": source}
        )
        return len(discovered["routes"]) + len(discovered["selectors"])

    monkeypatch.setattr(planner_agent_service, "merge_verified_discovery", _fake_merge)

    responses = iter([{"analysis": CANNED_ANALYSIS, "cases": CANNED_CASES}, CANNED_REVIEW_EMPTY])
    monkeypatch.setattr(ai_service, "run_json", lambda *a, **k: next(responses))

    ai_service.run_generation_pipeline(run.id, blocking=True)

    assert len(merged) == 1
    assert merged[0]["project_key"] == "surency"
    assert merged[0]["repo"] == "web"
    assert merged[0]["source"] == planner_agent_service.KB_SOURCE
    selectors = {s["selector"] for s in merged[0]["discovered"]["selectors"]}
    assert "getByRole('link', { name: 'Forgot password' })" in selectors
    assert "getByRole('heading', { name: 'Reset your password' })" in selectors  # expect locator
    assert "getByLabel('Email')" in selectors
    assert [r["path"] for r in merged[0]["discovered"]["routes"]] == ["/reset"]


def test_live_planner_falls_back_to_text_without_base_url(db_session, seed_ticket, monkeypatch):
    """NEGATIVE CONTROL: no base_url → the planner agent is never invoked (#877, #889)."""
    from app.services.prompts import PLANNER_PLAN_HEADER

    run = _make_run(db_session, seed_ticket.external_id)
    _planner_env(monkeypatch, base_url="")

    def _boom(*a, **k):
        raise AssertionError("plan_ticket() must not run without a resolvable base_url")

    monkeypatch.setattr(ai_service.planner_agent_service, "plan_ticket", _boom)

    prompts_seen: list[str] = []

    def _fake_run_json(prompt, **k):
        prompts_seen.append(prompt)
        if k.get("skill") == TEST_CASE_REVIEWER:
            return CANNED_REVIEW_EMPTY
        return {"analysis": CANNED_ANALYSIS, "cases": CANNED_CASES}

    monkeypatch.setattr(ai_service, "run_json", _fake_run_json)

    ai_service.run_generation_pipeline(run.id, blocking=True)

    run_ticket = db_session.query(RunTicket).filter(RunTicket.run_id == run.id).first()
    assert run_ticket.gen_status == "done"
    assert prompts_seen and all(PLANNER_PLAN_HEADER not in p for p in prompts_seen)
    # ...and the text-only output is exactly today's: the canned cases, unchanged.
    titles = [
        c.title
        for c in db_session.query(TestCase)
        .filter(TestCase.run_id == run.id)
        .order_by(TestCase.code)
        .all()
    ]
    assert titles == [c["title"] for c in CANNED_CASES]


def test_live_planner_agent_failure_falls_back_to_text_only(db_session, seed_ticket, monkeypatch):
    """A crashed planner run is best-effort — generation still completes text-only (#889)."""
    from app.services.prompts import PLANNER_PLAN_HEADER

    run = _make_run(db_session, seed_ticket.external_id)
    _planner_env(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("Chrome CDP endpoint did not come up")

    monkeypatch.setattr(ai_service.planner_agent_service, "plan_ticket", _boom)

    prompts_seen: list[str] = []

    def _fake_run_json(prompt, **k):
        prompts_seen.append(prompt)
        if k.get("skill") == TEST_CASE_REVIEWER:
            return CANNED_REVIEW_EMPTY
        return {"analysis": CANNED_ANALYSIS, "cases": CANNED_CASES}

    monkeypatch.setattr(ai_service, "run_json", _fake_run_json)

    ai_service.run_generation_pipeline(run.id, blocking=True)

    run_ticket = db_session.query(RunTicket).filter(RunTicket.run_id == run.id).first()
    assert run_ticket.gen_status == "done"
    assert prompts_seen and all(PLANNER_PLAN_HEADER not in p for p in prompts_seen)


def test_exploration_target_derives_from_ticket_text():
    """No case exists yet for a proactive Planner target — it's derived off the ticket."""
    from app.models.ticket import Ticket

    ticket = Ticket(
        external_id="SUR-1428",
        provider_kind="ado",
        title="Add password reset flow",
        acceptance_criteria=["Given a valid email, a reset link is sent", "Reset link expires after 30 minutes"],
    )
    target = ai_service._exploration_target_for_ticket(ticket)
    assert target["ticket"] == "SUR-1428"
    assert target["screen"] == "Add password reset flow"
    assert "Add password reset flow" in target["goal"]
    assert "Given a valid email" in target["goal"]


def test_next_case_code_increments(db_session, seed_ticket):
    run = _make_run(db_session, seed_ticket.external_id)
    db_session.add(
        TestCase(
            run_id=run.id,
            ticket_external_id=seed_ticket.external_id,
            code="TC-01",
            title="First",
        )
    )
    db_session.commit()

    assert ai_service.next_case_code(db_session, run.id, seed_ticket.external_id) == "TC-02"
