"""Business Knowledge reaches the test-case prompts (#825, ADR 0016 §7).

Three things are pinned here, because each of them can look green while being
wrong:

1. ``build_context`` loads the brief and the facts by **project GUID**, not by
   repo — the property the cold-start case (#826) depends on, so it is asserted
   against a project that has no ``ProjectKnowledge`` row at all.
2. The character budget actually **bites**: an oversized corpus loses its
   lowest-ranked facts while the brief and every pinned fact survive.
3. Ranking actually **ranks**: two different tickets keep two different sets of
   facts. A ranking assertion that holds for any query is testing that the
   function returned something, not that it ranked.
"""

from __future__ import annotations

from app.models.business import BusinessFact
from app.models.project_config import ProjectConfig
from app.models.provider_connection import ProviderConnection
from app.models.ticket import Ticket
from app.services import project_config_service
from app.services.prompts import (
    BUSINESS_FACT_CHARS,
    build_case_regenerate_prompt,
    build_combined_prompt,
    build_review_prompt,
    render_business_context,
)

GUID = "11111111-2222-3333-4444-555555555555"


def _seed_project(db_session, *, brief: str = "", facts: list[dict] | None = None) -> Ticket:
    """A ticket whose project has Business Knowledge and NO repo/knowledge row.

    The absence of a ``ProjectKnowledge`` row is the point: business context must
    load off the project GUID alone (#826).
    """
    conn = ProviderConnection(
        kind="ado", name="ADO", connected=True, config={"project": "Surency Platform"}, secrets={}
    )
    db_session.add(conn)
    db_session.flush()
    db_session.add(
        ProjectConfig(
            key="Surency Platform",
            name="Surency Platform",
            project_guid=GUID,
            business_brief={"brief": brief, "status": "ready"} if brief else {},
        )
    )
    for fact in facts or []:
        db_session.add(BusinessFact(project_guid=GUID, **fact))
    ticket = Ticket(
        external_id="SUR-1", provider_kind="ado", title="t", connection_id=conn.id
    )
    db_session.add(ticket)
    db_session.commit()
    return ticket


# ------------------------------------------------------------- build_context
def test_build_context_loads_business_knowledge_by_project_guid(db_session):
    """Brief + facts arrive with no repo and no ProjectKnowledge row in play."""
    ticket = _seed_project(
        db_session,
        brief="Surency administers employer benefit plans.",
        facts=[
            {"category": "glossary", "term": "Sponsor", "statement": "The employer.",
             "rank_text": "Sponsor The employer."},
            {"category": "rule", "term": "Eligibility", "statement": "90 days of service.",
             "rank_text": "Eligibility 90 days of service."},
        ],
    )

    ctx = project_config_service.build_context(db_session, ticket)

    assert ctx["projectKey"] == "Surency Platform"
    # No repo anywhere — the cold-start shape (#826).
    assert not ctx.get("routes")
    assert ctx["businessBrief"] == "Surency administers employer benefit plans."
    assert {f["term"] for f in ctx["businessFacts"]} == {"Sponsor", "Eligibility"}


def test_build_context_drops_excluded_and_superseded_facts(db_session):
    """An excluded fact is out of context, and an overridden one yields to its overlay."""
    ticket = _seed_project(
        db_session,
        facts=[
            {"category": "rule", "term": "Old", "statement": "Wrong: 30 days."},
            {"category": "rule", "term": "Gone", "statement": "Retired.", "excluded": True},
        ],
    )
    stale = (
        db_session.query(BusinessFact).filter(BusinessFact.term == "Old").one()
    )
    db_session.add(
        BusinessFact(
            project_guid=GUID, category="rule", term="Corrected",
            statement="Right: 90 days.", origin="manual", pinned=True,
            superseded_by=stale.id,
        )
    )
    db_session.commit()

    terms = {
        f["term"] for f in project_config_service.build_context(db_session, ticket)["businessFacts"]
    }
    assert terms == {"Corrected"}
    # Negative control: the overlay is what removed "Old" — it is not simply absent.
    assert "Gone" not in terms and "Old" not in terms


def test_build_context_without_business_knowledge_is_empty_not_missing(db_session):
    """A project with no Business Knowledge still carries the keys (#826 reads them)."""
    ticket = _seed_project(db_session)
    ctx = project_config_service.build_context(db_session, ticket)
    assert ctx["businessBrief"] == ""
    assert ctx["businessFacts"] == []


# -------------------------------------------------------- render_business_context
def _bulk_facts(topic: str, count: int, *, chars: int = 400) -> list[dict]:
    """``count`` facts about ``topic``, each ~``chars`` long, so a corpus overflows."""
    return [
        {
            "category": "rule",
            "term": f"{topic}-{i}",
            "statement": f"{topic} rule {i}: " + f"{topic} handling detail. " * (chars // 24),
        }
        for i in range(count)
    ]


def test_business_block_budget_drops_lowest_ranked_and_keeps_brief_and_pinned():
    """An oversized corpus is cut to the budget; the brief and pinned facts survive."""
    facts = _bulk_facts("refund", 6) + _bulk_facts("payroll", 40)
    facts.append(
        {"category": "rule", "term": "PinnedTruth", "statement": "x" * 3000, "pinned": True}
    )
    context = {"projectKey": "P", "businessBrief": "The product settles claims.", "businessFacts": facts}

    block = render_business_context(context, rank_query="Refund a claim")

    # The budget bit: not every fact made it.
    assert "payroll-39" not in block
    assert sum(1 for line in block.splitlines() if line.startswith("- [")) < len(facts)
    # The brief is never dropped, and neither is a pinned correction — even though
    # it alone is half the fact budget.
    assert "The product settles claims." in block
    assert "PinnedTruth" in block
    # The highest-ranked facts are the ones that survived.
    assert "refund-0" in block
    # Negative control: without the budget everything would fit, so the assertion
    # above is about the budget and not about the corpus being small.
    unbudgeted = render_business_context(
        context, rank_query="Refund a claim", char_budget=10**7
    )
    assert "payroll-39" in unbudgeted


def test_business_block_ranking_changes_which_facts_survive_per_ticket():
    """Two different queries keep two different sets — the test ranking is real."""
    facts = _bulk_facts("refund", 20) + _bulk_facts("payroll", 20)
    context = {"projectKey": "P", "businessFacts": facts}

    refund_block = render_business_context(context, rank_query="Refund an overpaid claim")
    payroll_block = render_business_context(context, rank_query="Payroll deduction schedule")

    assert "refund-0" in refund_block and "refund-0" not in payroll_block
    assert "payroll-0" in payroll_block and "payroll-0" not in refund_block
    # Both blocks are budget-bound, so "different survivors" means something.
    assert len(refund_block) <= BUSINESS_FACT_CHARS * 2
    assert len(payroll_block) <= BUSINESS_FACT_CHARS * 2


def test_business_block_is_headed_as_intent_and_demotes_the_code_kb():
    """The heading tells the model which block is intent and which is implementation."""
    block = render_business_context({"projectKey": "P", "businessBrief": "B"})
    assert "PRIMARY source" in block
    assert "Project Knowledge Base" in block and "automation detail" in block


def test_business_block_absent_when_the_project_has_none():
    assert render_business_context({"projectKey": "P"}) == ""
    assert render_business_context(None) == ""


# --------------------------------------------------------------- the prompts
def _ticket() -> Ticket:
    return Ticket(
        external_id="SUR-9",
        provider_kind="ado",
        title="Refund an invoice",
        description="A finance user refunds an invoice from the invoices screen.",
        acceptance_criteria=["The refund is recorded against the invoice"],
    )


def _context_with_buried_route() -> dict:
    """25 irrelevant routes, then the relevant one — a blind ``[:20]`` loses it."""
    routes = [{"path": f"/noise-{i}", "description": "unrelated"} for i in range(25)]
    routes.append({"path": "/invoices/refund", "description": "Refund an invoice"})
    return {
        "projectKey": "P",
        "routes": routes,
        "businessBrief": "Invoicing for benefit plans.",
        "businessFacts": [
            {"category": "rule", "term": "Refund window", "statement": "Refunds within 60 days."}
        ],
    }


def test_case_prompts_rank_the_project_block_by_the_ticket():
    """All three test-case prompts now pass a rank_query (the latent bug, #825)."""
    ticket = _ticket()
    context = _context_with_buried_route()

    for prompt in (
        build_combined_prompt(ticket, context=context),
        build_review_prompt(ticket, {}, [], context=context),
        build_case_regenerate_prompt(ticket, {}, {"title": "c"}, context=context),
    ):
        assert "/invoices/refund" in prompt
        # It only survives because it was ranked: it is past the blind 20-item cut.
        assert "/noise-24" not in prompt


def test_case_prompts_carry_the_business_block():
    ticket = _ticket()
    context = _context_with_buried_route()

    for prompt in (
        build_combined_prompt(ticket, context=context),
        build_review_prompt(ticket, {}, [], context=context),
        build_case_regenerate_prompt(ticket, {}, {"title": "c"}, context=context),
    ):
        assert "Invoicing for benefit plans." in prompt
        assert "Refund window" in prompt
        # Business intent leads; the code KB follows it.
        assert prompt.index("PRIMARY source") < prompt.index("Project context (from the")


def test_automation_review_prompt_is_deliberately_left_alone():
    """v1 stops at the three test-case prompts; automation/spec/heal are #833."""
    from app.services.prompts import build_automation_review_prompt

    prompt = build_automation_review_prompt("const x = 1;", _ticket(), _context_with_buried_route())
    assert "PRIMARY source" not in prompt
