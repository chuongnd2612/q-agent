"""The QC-voice gate wired into the generation pipeline (#829).

The gate library itself (``app.services.qc_voice_gate``, #823) is covered by
``test_qc_voice_gate.py`` — rule attribution, the allow-list, the fixture
corpus. This file covers the *wiring*, which is where the interesting decisions
live:

* the hook is ``ai_service._case_kwargs_from_raw``, the single funnel all three
  producers pass through (the run pipeline, ``_review_and_expand``,
  ``regenerate_case``), so one hook has to cover all three;
* a rejected case gets **exactly one** retry — bounded cost;
* a case that trips the gate twice is **persisted with its findings**, never
  dropped: this pipeline degrades and shows;
* the project's business glossary is the allow-list, and an absent glossary must
  not break anything.

And the number the whole gate exists for: a full sample run must produce **zero**
findings across every case it generates. If that assertion ever goes red, a
prompt regressed — the fix is the prompt, never a weaker gate.
"""

from __future__ import annotations

from app.models.business import BusinessFact
from app.models.run import Run, RunTicket
from app.models.testcase import TestCase
from app.services import ai_service, project_config_service, qc_voice_gate, sample_run_service

PROJECT_GUID = "11111111-2222-3333-4444-555555555555"

# --- Canned Claude responses, in the real response shape --------------------
#
# Faithful to what the pipeline actually parses (#542): the combined call returns
# {"analysis": {...}, "cases": [...]}, the reviewer returns
# {"verdict", "coverageGaps", "additionalCases"}, and a regenerate returns a bare
# case object.

ANALYSIS = {
    "businessRules": ["Only an internal admin may deactivate an agency"],
    "functionalRequirements": ["Deactivation asks for confirmation"],
    "validationRules": ["The dialog blocks the list until dismissed"],
    "risks": ["An accidental deactivation"],
    "edgeCases": ["Cancel after opening the dialog"],
    "missingInformation": [],
    "suggestedScope": "Cover the confirm and cancel branches.",
}

CLEAN_CASE = {
    "title": "Deactivating an agency asks for confirmation first",
    "objective": "Prove the confirmation step cannot be skipped.",
    "precondition": "Signed in as an internal admin with an active agency in the list.",
    "steps": [
        {"a": "Open the actions menu on an active agency.", "e": "The menu offers Deactivate."},
        {"a": "Choose Deactivate.", "e": "A confirmation dialog opens."},
    ],
    "testData": [{"field": "Agency name", "value": "Northgate Benefits"}],
    "linkedAc": ["AC1"],
    "priority": "High",
    "testType": "Functional",
    "automation": "Playwright",
    "platform": "Web",
}

# Leaks a selector, a route and a status code — three different rules, so a
# finding list can be asserted by NAME rather than merely by length.
LEAKY_CASE = {
    "title": "Deactivate agency via the agencies endpoint",
    "objective": "Prove the deactivation works.",
    "precondition": "Signed in as an internal admin.",
    "steps": [
        {"a": "Click [data-testid=\"deactivate-btn\"].", "e": "A confirmation dialog opens."},
        {"a": "Go to /brokers/agencies and confirm.", "e": "The response status code is 200."},
    ],
    "testData": [{"field": "Agency name", "value": "Northgate Benefits"}],
    "linkedAc": ["AC1"],
    "priority": "High",
    "testType": "Functional",
    "automation": "Playwright",
    "platform": "Web",
}

# Still leaking after the rewrite, but differently — so the persisted findings
# can be shown to come from the SECOND attempt, not the first.
STILL_LEAKY_CASE = {
    **LEAKY_CASE,
    "title": "Deactivate an agency from the list",
    "steps": [
        {"a": "Open the actions menu.", "e": "The menu offers Deactivate."},
        {"a": "Confirm the dialog.", "e": "The agency row shows Inactive."},
    ],
    "precondition": "Signed in as an internal admin; the userId column is populated.",
}

REVIEW_EMPTY = {"verdict": "approve", "coverageGaps": [], "additionalCases": []}


def _make_run(db_session, ticket_external_id: str) -> Run:
    """One `processing` Run with a single RunTicket, ready for the pipeline."""
    run = Run(code="RUN-829", name="QC voice run", status="processing")
    db_session.add(run)
    db_session.flush()
    db_session.add(RunTicket(run_id=run.id, ticket_external_id=ticket_external_id, position=0))
    db_session.commit()
    db_session.refresh(run)
    return run


def _record_run_json(monkeypatch, responses: list):
    """Stub ``ai_service.run_json`` with a scripted response list.

    Returns the list every call's prompt is appended to, so a test can assert
    *how many* Claude calls happened and *what* was in them — which is the only
    way to prove the retry fired rather than assuming it from the outcome.
    """
    prompts: list[str] = []
    queue = list(responses)

    def _fake(prompt, *_args, **_kwargs):
        prompts.append(prompt)
        assert queue, f"run_json called more times than scripted (call {len(prompts)})"
        return queue.pop(0)

    monkeypatch.setattr(ai_service, "run_json", _fake)
    return prompts


def _cases(db_session, run_id: int) -> list[TestCase]:
    return (
        db_session.query(TestCase)
        .filter(TestCase.run_id == run_id)
        .order_by(TestCase.code)
        .all()
    )


# ---------------------------------------------------------------- clean path


def test_a_clean_case_is_persisted_with_no_findings_and_no_retry(
    db_session, seed_ticket, monkeypatch
):
    run = _make_run(db_session, seed_ticket.external_id)
    prompts = _record_run_json(
        monkeypatch, [{"analysis": ANALYSIS, "cases": [CLEAN_CASE]}, REVIEW_EMPTY]
    )

    ai_service.run_generation_pipeline(run.id, blocking=True)

    cases = _cases(db_session, run.id)
    assert len(cases) == 1
    assert cases[0].voice_findings == []
    # Exactly two calls: generate + review. No retry was issued.
    assert len(prompts) == 2


# ---------------------------------------------------------------- the retry


def test_a_leaking_case_is_retried_once_and_the_clean_rewrite_is_kept(
    db_session, seed_ticket, monkeypatch
):
    """The first response leaks; the second does not. The rewrite is what lands."""
    run = _make_run(db_session, seed_ticket.external_id)
    prompts = _record_run_json(
        monkeypatch,
        [
            {"analysis": ANALYSIS, "cases": [LEAKY_CASE]},
            CLEAN_CASE,  # the retry's answer
            REVIEW_EMPTY,
        ],
    )

    ai_service.run_generation_pipeline(run.id, blocking=True)

    cases = _cases(db_session, run.id)
    assert len(cases) == 1
    # The retry actually fired: three calls, and the middle one carried the
    # findings as its reason.
    assert len(prompts) == 3
    assert "these phrases leaked technical detail" in prompts[1]
    assert 'data-testid="deactivate-btn"' in prompts[1]
    # And the REWRITE is what was persisted, not the leaky original.
    assert cases[0].title == CLEAN_CASE["title"]
    assert cases[0].voice_findings == []


def test_a_case_that_leaks_twice_is_persisted_with_its_findings_not_dropped(
    db_session, seed_ticket, monkeypatch
):
    """Degrade and show: a twice-failing case still becomes a row a QC can fix."""
    run = _make_run(db_session, seed_ticket.external_id)
    prompts = _record_run_json(
        monkeypatch,
        [
            {"analysis": ANALYSIS, "cases": [LEAKY_CASE]},
            STILL_LEAKY_CASE,  # the retry still leaks
            REVIEW_EMPTY,
        ],
    )

    ai_service.run_generation_pipeline(run.id, blocking=True)

    cases = _cases(db_session, run.id)
    # Persisted, not dropped — this is the whole point.
    assert len(cases) == 1
    case = cases[0]
    assert case.voice_findings, "a twice-failing case must carry its findings"
    # The findings are the SECOND attempt's, not the first's: the second attempt
    # leaked `userId` (code_identifier) and no longer leaks a selector.
    rules = {f["rule"] for f in case.voice_findings}
    matches = {f["match"] for f in case.voice_findings}
    assert "code_identifier" in rules
    assert "css_xpath_selector" not in rules
    assert "userId" in matches
    # ...and the rewrite is what was kept.
    assert case.title == STILL_LEAKY_CASE["title"]
    # Exactly ONE retry — bounded cost. generate + retry + review, nothing more.
    assert len(prompts) == 3


def test_a_failed_retry_call_keeps_the_original_case_and_its_findings(
    db_session, seed_ticket, monkeypatch
):
    """A Claude error on the retry must not fail the run or lose the case."""
    run = _make_run(db_session, seed_ticket.external_id)
    queue = [{"analysis": ANALYSIS, "cases": [LEAKY_CASE]}, "boom", REVIEW_EMPTY]
    calls: list[str] = []

    def _fake(prompt, *_args, **_kwargs):
        calls.append(prompt)
        nxt = queue.pop(0)
        if nxt == "boom":
            raise RuntimeError("claude exploded")
        return nxt

    monkeypatch.setattr(ai_service, "run_json", _fake)

    ai_service.run_generation_pipeline(run.id, blocking=True)

    cases = _cases(db_session, run.id)
    assert len(cases) == 1
    assert cases[0].title == LEAKY_CASE["title"]  # the original survived
    assert {f["rule"] for f in cases[0].voice_findings} >= {"css_xpath_selector"}
    run_ticket = db_session.query(RunTicket).filter(RunTicket.run_id == run.id).first()
    assert run_ticket.gen_status == "done"  # the run was NOT failed on voice


# ------------------------------------------------------------ the allow-list


def test_glossary_terms_are_collected_for_the_ticket_project(
    db_session, seed_ticket, monkeypatch
):
    monkeypatch.setattr(
        project_config_service, "project_guid_for_ticket", lambda *_a, **_k: PROJECT_GUID
    )
    db_session.add_all(
        [
            BusinessFact(project_guid=PROJECT_GUID, category="glossary", term="FSA_Card"),
            BusinessFact(project_guid=PROJECT_GUID, category="glossary", term="eClaims"),
            # Excluded and non-glossary rows must not contribute vocabulary.
            BusinessFact(
                project_guid=PROJECT_GUID, category="glossary", term="deadTerm", excluded=True
            ),
            BusinessFact(project_guid=PROJECT_GUID, category="rule", term="ruleSubject"),
            # A different project's glossary must not leak in.
            BusinessFact(project_guid="99999999-0000-0000-0000-000000000000",
                         category="glossary", term="otherProjectTerm"),
        ]
    )
    db_session.commit()

    terms = ai_service.glossary_terms_for_ticket(db_session, seed_ticket)

    assert set(terms) == {"FSA_Card", "eClaims"}


def test_a_glossary_term_is_not_flagged_and_triggers_no_retry(
    db_session, seed_ticket, monkeypatch
):
    """`FSA_Card` is domain vocabulary, not leaked code — and must not cost a retry."""
    monkeypatch.setattr(
        project_config_service, "project_guid_for_ticket", lambda *_a, **_k: PROJECT_GUID
    )
    db_session.add(BusinessFact(project_guid=PROJECT_GUID, category="glossary", term="FSA_Card"))
    db_session.commit()

    domain_case = {
        **CLEAN_CASE,
        "title": "An expired FSA_Card is rejected at checkout",
        "steps": [{"a": "Pay with an expired FSA_Card.", "e": "The payment is declined."}],
    }
    # Negative control: without the glossary the SAME case is rejected, so this
    # test cannot pass for the wrong reason.
    assert qc_voice_gate.check_case(domain_case)["outcome"] == "reject"

    run = _make_run(db_session, seed_ticket.external_id)
    prompts = _record_run_json(
        monkeypatch, [{"analysis": ANALYSIS, "cases": [domain_case]}, REVIEW_EMPTY]
    )

    ai_service.run_generation_pipeline(run.id, blocking=True)

    cases = _cases(db_session, run.id)
    assert cases[0].voice_findings == []
    assert len(prompts) == 2  # no retry was spent on the project's own vocabulary


def test_the_gate_still_runs_when_the_project_has_no_glossary(
    db_session, seed_ticket, monkeypatch
):
    """No project, no facts — the gate must still catch a leak, not silently skip."""
    assert ai_service.glossary_terms_for_ticket(db_session, seed_ticket) == []

    run = _make_run(db_session, seed_ticket.external_id)
    _record_run_json(
        monkeypatch,
        [{"analysis": ANALYSIS, "cases": [LEAKY_CASE]}, STILL_LEAKY_CASE, REVIEW_EMPTY],
    )

    ai_service.run_generation_pipeline(run.id, blocking=True)

    assert _cases(db_session, run.id)[0].voice_findings


# --------------------------------------------- the other two funnel entrances


def test_review_expansion_cases_go_through_the_gate_too(db_session, seed_ticket, monkeypatch):
    """`_review_and_expand` shares the funnel, so its cases are gated as well."""
    run = _make_run(db_session, seed_ticket.external_id)
    review = {
        "verdict": "approve-with-changes",
        "coverageGaps": ["No negative case"],
        "additionalCases": [LEAKY_CASE],
    }
    prompts = _record_run_json(
        monkeypatch,
        [
            {"analysis": ANALYSIS, "cases": [CLEAN_CASE]},
            review,
            STILL_LEAKY_CASE,  # the reviewer case's one retry
        ],
    )

    ai_service.run_generation_pipeline(run.id, blocking=True)

    cases = _cases(db_session, run.id)
    assert len(cases) == 2
    by_source = {c.source: c for c in cases}
    assert by_source["ai"].voice_findings == []
    assert by_source["ai-review"].voice_findings, "reviewer cases must be gated too"
    # generate + review + exactly one retry for the reviewer case.
    assert len(prompts) == 3


def test_regenerate_case_stamps_and_clears_voice_findings(db_session, seed_ticket, monkeypatch):
    """A manual regenerate is the third funnel entrance — and the retry action."""
    run = _make_run(db_session, seed_ticket.external_id)
    case = TestCase(
        run_id=run.id,
        ticket_external_id=seed_ticket.external_id,
        code="TC-01",
        source="ai",
        title="Old title",
        precondition="Old precondition",
        steps=[{"a": "old", "e": "old"}],
        voice_findings=[{"field": "title", "rule": "api_path", "match": "/brokers"}],
    )
    db_session.add(case)
    db_session.commit()

    # A clean regenerate clears the stamp.
    monkeypatch.setattr(ai_service, "run_json", lambda *a, **k: CLEAN_CASE)
    ai_service.regenerate_case(db_session, case)
    assert case.title == CLEAN_CASE["title"]
    assert case.voice_findings == []

    # A regenerate that leaks twice re-stamps it rather than dropping the case.
    responses = iter([LEAKY_CASE, STILL_LEAKY_CASE])
    monkeypatch.setattr(ai_service, "run_json", lambda *a, **k: next(responses))
    ai_service.regenerate_case(db_session, case)
    assert case.title == STILL_LEAKY_CASE["title"]
    assert {f["rule"] for f in case.voice_findings} == {"code_identifier"}


def test_voice_findings_are_on_the_case_response(client, db_session, seed_ticket, monkeypatch):
    """The Review Center badge needs the findings on the wire, camelCased."""
    run = _make_run(db_session, seed_ticket.external_id)
    _record_run_json(
        monkeypatch,
        [{"analysis": ANALYSIS, "cases": [LEAKY_CASE]}, STILL_LEAKY_CASE, REVIEW_EMPTY],
    )
    ai_service.run_generation_pipeline(run.id, blocking=True)

    body = client.get(f"/runs/{run.id}/cases").json()

    assert len(body) == 1
    findings = body[0]["voiceFindings"]
    assert [set(f) for f in findings] == [{"field", "rule", "match"} for _ in findings]
    assert {f["rule"] for f in findings} == {"code_identifier"}


# ------------------------------------------------- the number, not a reading


def test_the_sample_run_generates_zero_voice_findings(db_session):
    """The definition of done: a full sample run leaks NOTHING.

    This is the assertion the gate exists for. If it goes red, the generated
    corpus regressed — fix the wording (or the prompt behind it), never the
    gate.
    """
    run = sample_run_service.ensure_sample_run(db_session, None)
    cases = _cases(db_session, run.id)
    assert cases, "the sample run must actually produce cases"

    leaks: list[str] = []
    for case in cases:
        report = qc_voice_gate.check_case(
            {
                "title": case.title,
                "objective": case.objective,
                "precondition": case.precondition,
                "steps": case.steps,
                "testData": case.test_data,
            }
        )
        for finding in report["findings"]:
            leaks.append(
                f"{case.ticket_external_id} {case.code} {finding['field']}: "
                f"{finding['rule']} -> {finding['match']!r}"
            )
        # The seeder inserts directly, so nothing should have been stamped.
        assert case.voice_findings == []

    assert leaks == [], "QC-voice leaks in the sample run:\n" + "\n".join(leaks)
