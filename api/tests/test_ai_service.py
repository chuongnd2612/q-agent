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
