"""Distilling a business corpus into a brief and structured facts (#824).

Two things carry this file.

**The stub must stay faithful.** Every test drives ``distil.run_json`` through a
canned response in *exactly* the shape the skill is asked for
(:data:`CANNED_DISTILLATION` — a brief plus ``{category, term, statement,
detail}`` facts). A stub missing a field is how assertions in this repo have
silently never run before (#542), so the shape is asserted against the model's
own :data:`BUSINESS_FACT_CATEGORIES` rather than hard-coded twice.

**The no-clobber rule is asserted in BOTH directions.** A merge that never
overwrites anything passes a "the pinned fact survived" test exactly as well as
the correct rule does. So the pinned row is checked to survive a colliding
re-sync *and* the unpinned row is checked to be updated by the very same call.

Conventions from ``CLAUDE.md`` that apply here: no ``==`` against a whole
response body, no ``monkeypatch.undo()``, and every outcome is pinned on an
observable effect (the row, the fact table, the on-disk corpus) rather than on a
status string alone.
"""

from __future__ import annotations

import pytest

from app.models.business import (
    BUSINESS_FACT_CATEGORIES,
    BusinessFact,
    BusinessSource,
)
from app.models.project import Project
from app.models.project_config import ProjectConfig
from app.services import skills
from app.services.business_ingest import distil, pipeline
from app.services.business_ingest.base import FetchedDoc

PROJECT_GUID = "11111111-2222-3333-4444-555555555555"
PROJECT_NAME = "Acme Ops"

#: A realistic handbook page — the thing a QC would actually upload.
HANDBOOK_MD = """# Refund eligibility

A customer may request a refund within 30 days of the order date. Orders placed
with store credit are refunded to store credit, never to a card.

A premium member skips the review queue: their refund is approved automatically
unless the order is flagged for fraud, in which case it is routed to the risk
team and the customer is notified by email.
"""

#: The canned Claude response. Shaped exactly like the skill's documented output
#: — the same keys, the same fact fields, and categories drawn from the model's
#: own tuple so a category rename cannot leave this stub quietly stale (#542).
CANNED_DISTILLATION = {
    "brief": (
        "Acme Ops is the back-office console the support team uses to review and settle "
        "customer refunds. A support agent works a queue of refund requests; a premium "
        "member's request skips that queue and settles automatically unless it is flagged "
        "for fraud, in which case the risk team picks it up. Refunds follow the tender "
        "they were paid with: an order placed with store credit is refunded to store "
        "credit and never to a card."
    ),
    "facts": [
        {
            "category": "glossary",
            "term": "premium member",
            "statement": "A premium member is a customer whose refunds skip the review queue.",
            "detail": "Stated in the refund eligibility page of the operations handbook.",
        },
        {
            "category": "rule",
            "term": "refund window",
            "statement": "A refund may be requested within 30 days of the order date.",
            "detail": "Measured from the order date, not the delivery date.",
        },
        {
            "category": "constraint",
            "term": "store credit refunds",
            "statement": "An order paid with store credit is refunded to store credit, never to a card.",
            "detail": "",
        },
    ],
}


def _seed_project(db_session) -> Project:
    project = Project(
        guid=PROJECT_GUID,
        provider_kind="ado",
        external_id="ext-acme-ops",
        name=PROJECT_NAME,
        active=True,
    )
    db_session.add(project)
    db_session.commit()
    return project


def _seed_source(db_session, *, title: str = "Operations handbook", markdown: str = HANDBOOK_MD,
                 path: str = "handbook.md") -> BusinessSource:
    """A registered source with a REAL ingested snapshot on disk.

    Driven through ``pipeline.ingest_documents`` rather than by writing the row's
    fields directly, so the corpus these tests read is the corpus ingestion
    actually produces — the same reason the Claude stub is kept faithful.
    """
    source = BusinessSource(
        project_guid=PROJECT_GUID,
        project_key=PROJECT_NAME,
        owner_id=None,
        kind="upload",
        title=title,
        status="pending",
    )
    db_session.add(source)
    db_session.commit()
    pipeline.ingest_documents(
        db_session,
        source,
        [FetchedDoc(path=path, title=title, raw_bytes=markdown.encode("utf-8"),
                    content_type="text/markdown")],
    )
    assert source.status == "synced", source.last_error
    return source


def _stub_claude(monkeypatch, response=None, *, calls: list | None = None):
    """Replace ``distil.run_json`` with the canned distillation.

    Patched on ``distil`` (where it is bound by the ``from ... import run_json``)
    and never undone by hand — ``monkeypatch`` unwinds it, and
    ``monkeypatch.undo()`` in an api test would un-redirect the temp DB too.
    """
    payload = CANNED_DISTILLATION if response is None else response

    def _fake(prompt, **kwargs):
        if calls is not None:
            calls.append((prompt, kwargs))
        return payload

    monkeypatch.setattr(distil, "run_json", _fake)


def _brief_of(db_session) -> dict:
    row = (
        db_session.query(ProjectConfig)
        .filter(ProjectConfig.project_guid == PROJECT_GUID)
        .first()
    )
    return dict(row.business_brief or {}) if row is not None else {}


def _facts_of(db_session) -> dict[tuple[str, str], BusinessFact]:
    rows = (
        db_session.query(BusinessFact)
        .filter(BusinessFact.project_guid == PROJECT_GUID)
        .all()
    )
    return {(r.category, r.term.lower()): r for r in rows}


# ---------------------------------------------------------------------------
# The stub itself — the guard against #542
# ---------------------------------------------------------------------------


def test_canned_response_matches_the_shape_the_code_parses():
    """The stub is the real shape, not a convenient subset.

    Every fact carries all four stored fields, and every category is one the
    model actually allows — so a category renamed in ``app.models.business``
    fails here rather than silently making every merge assertion vacuous.
    """
    assert set(CANNED_DISTILLATION) == {"brief", "facts"}
    for fact in CANNED_DISTILLATION["facts"]:
        assert set(fact) == {"category", "term", "statement", "detail"}
        assert fact["category"] in BUSINESS_FACT_CATEGORIES


def test_the_skill_is_registered_and_loadable():
    assert skills.BUSINESS_ANALYST in skills.SKILLS
    text = skills.load_skill(skills.BUSINESS_ANALYST)
    assert text and "Business Analyst" in text
    # The budget the code enforces is the budget the skill states.
    assert str(distil.BRIEF_TOKEN_BUDGET) in text


# ---------------------------------------------------------------------------
# Distillation
# ---------------------------------------------------------------------------


def test_distil_writes_the_brief_and_the_facts(db_session, monkeypatch):
    _seed_project(db_session)
    source = _seed_source(db_session)
    calls: list = []
    _stub_claude(monkeypatch, calls=calls)

    summary = distil.distil_project(
        db_session, PROJECT_GUID, None, project_name=PROJECT_NAME
    )

    assert summary["status"] == "ready"
    assert summary["documents"] == 1
    assert summary["facts_merged"] == len(CANNED_DISTILLATION["facts"])

    brief = _brief_of(db_session)
    assert brief["status"] == "ready"
    assert brief["last_error"] == ""
    assert brief["brief"] == CANNED_DISTILLATION["brief"]
    assert brief["hash"] and brief["built_at"]

    facts = _facts_of(db_session)
    window = facts[("rule", "refund window")]
    assert "30 days" in window.statement
    assert window.origin == "ingested"
    assert window.pinned is False
    # Attributable: one source in the corpus, so every fact points at it.
    assert window.source_id == source.id
    # The searchable projection is materialized on the row (there is no vector
    # store anywhere in this codebase).
    assert "refund window" in window.rank_text and "30 days" in window.rank_text

    # The corpus really did reach the prompt — this is the one stage that reads
    # raw documents, and nothing downstream ever will.
    prompt, kwargs = calls[0]
    assert "store credit" in prompt
    assert kwargs["skill"] == skills.BUSINESS_ANALYST


def test_the_brief_is_clamped_to_the_token_budget(db_session, monkeypatch):
    """The ceiling is enforced in code, not merely requested in the skill.

    The brief is a prompt-budget guarantee every later stage depends on, and a
    model that ignores a stated limit must not be able to blow it.
    """
    _seed_project(db_session)
    _seed_source(db_session)
    _stub_claude(monkeypatch, {"brief": "word " * 20_000, "facts": []})

    distil.distil_project(db_session, PROJECT_GUID, None, project_name=PROJECT_NAME)

    brief = _brief_of(db_session)["brief"]
    assert len(brief) <= distil.BRIEF_CHAR_BUDGET + 1  # + the ellipsis
    assert brief.endswith("…")


def test_unusable_facts_are_dropped_rather_than_stored(db_session, monkeypatch):
    """A bad category or a missing term is dropped; the good fact still lands.

    The negative control matters as much as the drop: a filter that rejected
    everything would pass a drop-only assertion.
    """
    _seed_project(db_session)
    _seed_source(db_session)
    _stub_claude(monkeypatch, {
        "brief": "A short brief.",
        "facts": [
            {"category": "rule", "term": "refund window",
             "statement": "A refund may be requested within 30 days.", "detail": ""},
            {"category": "not-a-category", "term": "x", "statement": "y", "detail": ""},
            {"category": "rule", "term": "", "statement": "no term at all", "detail": ""},
            {"category": "rule", "term": "no statement", "statement": "", "detail": "d"},
        ],
    })

    summary = distil.distil_project(
        db_session, PROJECT_GUID, None, project_name=PROJECT_NAME
    )

    assert summary["facts_merged"] == 1
    assert set(_facts_of(db_session)) == {("rule", "refund window")}


def test_an_excluded_source_is_out_of_the_corpus_but_keeps_its_snapshot(
    db_session, monkeypatch
):
    """Excluding a source stops it grounding new artifacts; it is not a delete."""
    _seed_project(db_session)
    kept = _seed_source(db_session, title="Handbook", path="handbook.md")
    dropped = _seed_source(
        db_session,
        title="Deprecated policy",
        markdown="# Deprecated\n\nRefunds used to take 90 days and required a manager.\n",
        path="deprecated.md",
    )
    dropped.excluded = True
    db_session.commit()

    corpus = distil.collect_corpus(db_session, PROJECT_GUID, None)

    assert [doc.source_id for doc in corpus] == [kept.id]
    assert "90 days" not in "".join(doc.markdown for doc in corpus)
    # Still attributable: the snapshot and its provenance survive exclusion.
    assert dropped.normalized_path and dropped.content_hash


def test_an_empty_corpus_is_ready_not_error(db_session, monkeypatch):
    """A project with nothing ingested is the cold-start case, not a failure."""
    _seed_project(db_session)
    calls: list = []
    _stub_claude(monkeypatch, calls=calls)

    summary = distil.distil_project(
        db_session, PROJECT_GUID, None, project_name=PROJECT_NAME
    )

    assert summary["status"] == "ready"
    assert summary["documents"] == 0
    brief = _brief_of(db_session)
    assert brief["status"] == "ready" and brief["brief"] == ""
    # Claude is not called at all for an empty corpus.
    assert calls == []


def test_a_claude_failure_keeps_the_previous_brief(db_session, monkeypatch):
    """A failed rebuild must not cost the project the grounding it already had.

    Same contract as a failed re-sync leaving the previous snapshot on disk.
    """
    _seed_project(db_session)
    _seed_source(db_session)
    _stub_claude(monkeypatch)
    distil.distil_project(db_session, PROJECT_GUID, None, project_name=PROJECT_NAME)
    good_brief = _brief_of(db_session)["brief"]

    def _boom(prompt, **kwargs):
        raise RuntimeError("claude exited 1: credential rejected")

    monkeypatch.setattr(distil, "run_json", _boom)
    summary = distil.distil_project(
        db_session, PROJECT_GUID, None, project_name=PROJECT_NAME
    )

    assert summary["status"] == "error"
    brief = _brief_of(db_session)
    assert brief["status"] == "error"
    assert "credential rejected" in brief["last_error"]
    assert brief["brief"] == good_brief  # the previous grounding survives
    # And so do the facts already distilled.
    assert ("rule", "refund window") in _facts_of(db_session)


# ---------------------------------------------------------------------------
# The no-clobber merge — asserted in BOTH directions
# ---------------------------------------------------------------------------


def test_a_pinned_fact_survives_a_colliding_resync_and_an_unpinned_one_is_updated(
    db_session, monkeypatch
):
    """The rule from ``knowledge_service.merge_verified_discovery``, with ``pinned``.

    Both directions in ONE re-sync, deliberately: a merge that never overwrote
    anything would pass the pinned half on its own, so the unpinned half is what
    proves the rule discriminates rather than simply always skipping.
    """
    _seed_project(db_session)
    _seed_source(db_session)
    _stub_claude(monkeypatch)
    distil.distil_project(db_session, PROJECT_GUID, None, project_name=PROJECT_NAME)

    facts = _facts_of(db_session)
    corrected = facts[("rule", "refund window")]
    corrected_id = corrected.id
    # A human correction, the way #827 will write one.
    corrected.statement = "A refund may be requested within 45 days of the order date."
    corrected.detail = "Extended to 45 days by the 2026 policy update."
    corrected.pinned = True
    untouched = facts[("glossary", "premium member")]
    untouched_id = untouched.id
    db_session.commit()

    merged = distil.merge_facts(
        db_session, PROJECT_GUID, None, CANNED_DISTILLATION["facts"]
    )

    after = _facts_of(db_session)
    pinned_row = after[("rule", "refund window")]
    unpinned_row = after[("glossary", "premium member")]

    # Direction 1 — the pinned correction is untouched, and still the same row.
    assert pinned_row.id == corrected_id
    assert pinned_row.statement.endswith("45 days of the order date.")
    assert pinned_row.detail == "Extended to 45 days by the 2026 policy update."
    assert pinned_row.pinned is True

    # Direction 2 — the unpinned row IS refreshed, in place.
    assert unpinned_row.id == untouched_id
    assert unpinned_row.statement == CANNED_DISTILLATION["facts"][0]["statement"]

    # The skipped one is not counted as merged.
    assert merged == len(CANNED_DISTILLATION["facts"]) - 1
    # And nothing was duplicated: the collision upgraded rather than appended.
    assert len(after) == len(CANNED_DISTILLATION["facts"])


def test_a_resync_leaves_an_excluded_unpinned_fact_excluded(db_session, monkeypatch):
    """``excluded`` is a human decision too, and an upgrade-in-place preserves it."""
    _seed_project(db_session)
    _seed_source(db_session)
    _stub_claude(monkeypatch)
    distil.distil_project(db_session, PROJECT_GUID, None, project_name=PROJECT_NAME)

    row = _facts_of(db_session)[("constraint", "store credit refunds")]
    row.excluded = True
    row.statement = "stale"
    db_session.commit()

    distil.merge_facts(db_session, PROJECT_GUID, None, CANNED_DISTILLATION["facts"])

    refreshed = _facts_of(db_session)[("constraint", "store credit refunds")]
    assert refreshed.excluded is True  # the human's decision stands
    assert refreshed.statement != "stale"  # ...but the content was refreshed


def test_facts_of_another_owner_are_never_merged_into(db_session, monkeypatch):
    """Ownership is part of the merge lookup (ADR 0009 §3).

    Without this, one user's re-sync would overwrite another user's identically
    termed fact — an authorisation bug wearing a merge's clothes.
    """
    _seed_project(db_session)
    other = BusinessFact(
        project_guid=PROJECT_GUID,
        owner_id=42,
        category="rule",
        term="refund window",
        statement="Another user's fact.",
        origin="ingested",
    )
    db_session.add(other)
    db_session.commit()

    distil.merge_facts(db_session, PROJECT_GUID, None, CANNED_DISTILLATION["facts"])

    db_session.refresh(other)
    assert other.statement == "Another user's fact."
    assert (
        db_session.query(BusinessFact)
        .filter(BusinessFact.project_guid == PROJECT_GUID, BusinessFact.owner_id.is_(None))
        .count()
        == len(CANNED_DISTILLATION["facts"])
    )


# ---------------------------------------------------------------------------
# The background guard
# ---------------------------------------------------------------------------


def test_the_in_process_guard_refuses_a_second_run(db_session):
    """Same guard shape as ``knowledge_service._building`` / ``pipeline._syncing``."""
    key = distil._guard_key(PROJECT_GUID, None)
    distil._distilling.add(key)
    try:
        assert distil.is_distilling(PROJECT_GUID, None) is True
        assert distil.start_distil(PROJECT_GUID, None) is False
    finally:
        distil._distilling.discard(key)
    assert distil.is_distilling(PROJECT_GUID, None) is False
    # The guard is per owner: one user's run must not lock another's out.
    assert distil._guard_key(PROJECT_GUID, 7) != key


def test_the_corpus_hash_tracks_the_content(db_session, monkeypatch):
    """The staleness signal (#830): same corpus, same digest; changed, different."""
    _seed_project(db_session)
    source = _seed_source(db_session)
    first = distil.corpus_hash(distil.collect_corpus(db_session, PROJECT_GUID, None))
    assert first == distil.corpus_hash(
        distil.collect_corpus(db_session, PROJECT_GUID, None)
    )

    pipeline.ingest_documents(
        db_session,
        source,
        [FetchedDoc(path="handbook.md", title="Operations handbook",
                    raw_bytes=(HANDBOOK_MD + "\nDigital goods are never refundable.\n").encode(),
                    content_type="text/markdown")],
    )
    assert distil.corpus_hash(distil.collect_corpus(db_session, PROJECT_GUID, None)) != first


@pytest.mark.parametrize("category", BUSINESS_FACT_CATEGORIES)
def test_every_model_category_is_accepted_by_the_cleaner(category):
    """The cleaner's allowlist is the model's tuple — not a second copy of it."""
    cleaned = distil._clean_facts(
        [{"category": category, "term": "t", "statement": "s", "detail": ""}]
    )
    assert cleaned and cleaned[0]["category"] == category
