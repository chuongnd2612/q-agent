"""Authoring test cases for a project with NO CODE YET (#826, ADR 0016 §6).

The cold-start claim is narrow and worth stating precisely, because "works
without code" reads as more than it is: a project with Business Knowledge and no
repository can produce **manual test cases**. Automation generation, execution
and self-heal still need a running application and stay gated — that is correct
and is not what this file tests.

Three things are pinned, each of which can look green while being wrong:

1. A run whose project has **no ``ProjectKnowledge`` row at all** actually
   persists cases. Asserting the pipeline "completed" would not prove that; the
   row count is what does.
2. Those cases carry **no invented routes or selectors**. The QC-voice gate
   (#823/#829) is the oracle, and a negative control proves the assertion has
   teeth rather than passing on any input.
3. The prompt does not *offer* a Knowledge Base that does not exist. A bare
   "Project context (from the Project Knowledge Base …)" header with nothing
   under it is an invitation to fill the silence with a guess, so it is omitted
   entirely and the business block stands alone.
"""

from __future__ import annotations

from app.models.business import BusinessFact
from app.models.knowledge import ProjectKnowledge
from app.models.project_config import ProjectConfig
from app.models.provider_connection import ProviderConnection
from app.models.run import Run, RunTicket
from app.models.testcase import TestCase
from app.models.ticket import Ticket
from app.services import ai_service, project_config_service, qc_voice_gate
from app.services.prompts import build_combined_prompt, render_project_context

GUID = "c01d5747-0000-4000-8000-000000000001"
PROJECT = "Greenfield Benefits"

BRIEF = (
    "Greenfield Benefits lets an employer's HR administrator enrol employees in "
    "a benefit plan and lets an employee claim reimbursement for an eligible "
    "expense."
)

FACTS = [
    {
        "category": "glossary",
        "term": "Plan Sponsor",
        "statement": "The employer who owns the benefit plan.",
        "rank_text": "Plan Sponsor The employer who owns the benefit plan.",
    },
    {
        "category": "rule",
        "term": "Reimbursement window",
        "statement": "An expense can be claimed for up to 90 days after it is incurred.",
        "rank_text": "Reimbursement window claimed 90 days after incurred expense",
    },
]

# Written the way the skill now says to write it for a project with no code:
# business intent, the product's own vocabulary, and not one screen name, route
# or selector that was never supplied.
BUSINESS_ONLY_CASES = [
    {
        "title": "An employee claims reimbursement within the reimbursement window",
        "objective": "Prove an expense incurred inside the window can be claimed.",
        "precondition": "Signed in as an employee enrolled in a benefit plan.",
        "steps": [
            {
                "a": "Start a new reimbursement claim for an expense incurred last week.",
                "e": "The claim is accepted for review.",
            },
            {
                "a": "Submit the claim.",
                "e": "A confirmation that the claim was received is shown.",
            },
        ],
        "testData": [{"field": "Expense amount", "value": "120.00"}],
        "linkedAc": ["AC1"],
        "priority": "High",
        "testType": "Functional",
        "automation": "Manual",
        "platform": "Web",
    },
    {
        "title": "A Plan Sponsor enrols an employee in a benefit plan",
        "objective": "Prove enrolment completes for an eligible employee.",
        "precondition": "Signed in as a Plan Sponsor with an active benefit plan.",
        "steps": [
            {
                "a": "Enrol an eligible employee in the benefit plan.",
                "e": "The employee is shown as enrolled in that plan.",
            },
        ],
        "testData": [{"field": "Employee name", "value": "Dana Whitfield"}],
        "linkedAc": ["AC2"],
        "priority": "Medium",
        "testType": "Functional",
        "automation": "Manual",
        "platform": "Web",
    },
]

# The same intent written by a model that filled the gap with invention — the
# negative control for every "no routes or selectors" assertion below.
INVENTED_CASE = {
    **BUSINESS_ONLY_CASES[0],
    "steps": [
        {
            "a": "Go to /claims/new and click [data-testid=\"submit-claim\"].",
            "e": "The response status code is 201.",
        },
    ],
}

ANALYSIS = {
    "businessRules": ["An expense can be claimed for up to 90 days after it is incurred"],
    "functionalRequirements": ["An employee can submit a reimbursement claim"],
    "validationRules": ["A claim outside the window is refused"],
    "risks": ["A claim submitted on the boundary day"],
    "edgeCases": ["An expense incurred exactly 90 days ago"],
    "missingInformation": ["Which screen the claim is started from is not yet known"],
    "suggestedScope": "Cover enrolment and a claim inside the window.",
}

REVIEW_EMPTY = {"verdict": "approve", "coverageGaps": [], "additionalCases": []}


def _seed_cold_start_project(db_session) -> Ticket:
    """A ticket whose project has Business Knowledge and NO code anywhere.

    No ``ProjectKnowledge`` row and no repo is the whole point: business context
    hangs off ``project_guid`` (#825), which is what makes a project that has
    never been built groundable at all.
    """
    conn = ProviderConnection(
        kind="ado", name="ADO", connected=True, config={"project": PROJECT}, secrets={}
    )
    db_session.add(conn)
    db_session.flush()
    db_session.add(
        ProjectConfig(
            key=PROJECT,
            name=PROJECT,
            project_guid=GUID,
            business_brief={"brief": BRIEF, "status": "ready"},
        )
    )
    for fact in FACTS:
        db_session.add(BusinessFact(project_guid=GUID, **fact))
    ticket = Ticket(
        external_id="SUR-9001",
        provider_kind="ado",
        title="Employee reimbursement claim",
        connection_id=conn.id,
        description="An employee can claim reimbursement for an eligible expense.",
        acceptance_criteria=[
            "An expense incurred within the reimbursement window can be claimed",
            "A Plan Sponsor can enrol an eligible employee",
        ],
    )
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


def _make_run(db_session, ticket_external_id: str) -> Run:
    run = Run(code="RUN-826", name="Cold start run", status="processing")
    db_session.add(run)
    db_session.flush()
    db_session.add(RunTicket(run_id=run.id, ticket_external_id=ticket_external_id, position=0))
    db_session.commit()
    db_session.refresh(run)
    return run


def _script_run_json(monkeypatch, responses: list) -> list[str]:
    """Stub ``ai_service.run_json`` and record every prompt it was handed."""
    prompts: list[str] = []
    queue = list(responses)

    def _fake(prompt, *_args, **_kwargs):
        prompts.append(prompt)
        assert queue, f"run_json called more times than scripted (call {len(prompts)})"
        return queue.pop(0)

    monkeypatch.setattr(ai_service, "run_json", _fake)
    return prompts


# ------------------------------------------------------------- the context


def test_business_knowledge_grounds_a_project_with_no_knowledge_row(db_session):
    """Brief + facts arrive; routes and selectors do not, because there are none."""
    ticket = _seed_cold_start_project(db_session)
    assert db_session.query(ProjectKnowledge).count() == 0, "the cold-start premise"

    ctx = project_config_service.build_context(db_session, ticket)

    assert ctx["projectKey"] == PROJECT
    assert ctx["businessBrief"] == BRIEF
    assert {f["term"] for f in ctx["businessFacts"]} == {"Plan Sponsor", "Reimbursement window"}
    # Nothing about how it is built, because nothing has been built.
    assert not ctx.get("routes")
    assert not ctx.get("selectors")


def test_the_prompt_does_not_offer_a_knowledge_base_that_does_not_exist(db_session):
    """An empty KB block is an invitation to guess; it is omitted, not left bare."""
    ticket = _seed_cold_start_project(db_session)
    ctx = project_config_service.build_context(db_session, ticket)

    assert render_project_context(ctx) == ""

    prompt = build_combined_prompt(ticket, context=ctx)
    assert BRIEF in prompt, "the business brief must be the grounding that remains"
    assert "Reimbursement window" in prompt
    assert "Project context (from the" not in prompt

    # Negative control: the block IS rendered the moment there is something real
    # in it, so this is not asserting that the renderer is simply broken.
    assert "Project context (from the" in render_project_context(
        {**ctx, "routes": [{"path": "/claims", "description": "Claims list"}]}
    )


# --------------------------------------------------------- the run, end to end


def test_a_run_generates_cases_for_a_project_with_no_knowledge_row(
    db_session, monkeypatch
):
    """The cold-start claim itself: cases are produced, and they invent nothing.

    Both halves matter. Cases-but-invented is the failure ADR 0002 forbids;
    no-cases is the refusal #826 exists to remove.
    """
    ticket = _seed_cold_start_project(db_session)
    assert db_session.query(ProjectKnowledge).count() == 0
    run = _make_run(db_session, ticket.external_id)

    prompts = _script_run_json(
        monkeypatch,
        [{"analysis": ANALYSIS, "cases": BUSINESS_ONLY_CASES}, REVIEW_EMPTY],
    )

    ai_service.run_generation_pipeline(run.id, blocking=True)

    cases = db_session.query(TestCase).filter(TestCase.run_id == run.id).all()
    assert len(cases) == len(BUSINESS_ONLY_CASES), "a repo-less project produced no cases"
    assert {c.title for c in cases} == {c["title"] for c in BUSINESS_ONLY_CASES}

    # The QC-voice gate (#823/#829) is the oracle for "no invented routes or
    # selectors": every persisted case came through it clean, so no retry was
    # spent either — generate + review and nothing more.
    for case in cases:
        assert case.voice_findings == [], f"{case.title} leaked technical detail"
    assert len(prompts) == 2

    # The pass is not vacuous: the same gate rejects the invented version.
    control = qc_voice_gate.check_case(INVENTED_CASE)
    assert control["outcome"] == "reject"
    assert {f["rule"] for f in control["findings"]} >= {"css_xpath_selector", "api_path"}

    # And the run really was grounded on business knowledge alone.
    assert BRIEF in prompts[0]
    assert "Project context (from the" not in prompts[0]

    run_ticket = db_session.query(RunTicket).filter(RunTicket.run_id == run.id).first()
    assert run_ticket.gen_status == "done"
