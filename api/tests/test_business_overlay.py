"""Correcting, adding to and excluding Business Knowledge (#827, ADR 0016 §5).

The overlay is three affordances and one invariant.

**The invariant** is that ingested content is never edited in place: a correction
is a *new* row (``origin="manual"``, ``pinned=True``) that supersedes the
ingested one, so the disagreement with the source document stays visible instead
of being hidden. Every test here pins that by checking the superseded row is
still present, not merely that the correction exists.

**The rule with teeth** is the one in :func:`distil.merge_facts`: a re-sync must
not overwrite a human. #824 shipped the ``pinned`` half of it; this slice adds
the other half of ADR 0016 §5's ladder — a **human addition** (``origin="manual"``
and *not* pinned, layer 2) also outranks an ingested fact (layer 3), and was
being clobbered by any re-sync that happened to distil the same term.

Every no-clobber assertion carries its negative control in the same test: a
merge that skipped everything would pass the "the human's fact survived" half on
its own, so the ingested row is checked to be refreshed by the very same call.
"""

from __future__ import annotations

from app.models.business import BusinessFact, BusinessSource
from app.models.project import Project
from app.services import business_fact_service as facts_service
from app.services.business_ingest import distil

PROJECT_GUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PROJECT_NAME = "Acme Ops"

#: What a later re-sync distils out of the corpus, unaware of any correction.
DISTILLED = [
    {
        "category": "rule",
        "term": "refund window",
        "statement": "A refund may be requested within 30 days of the order date.",
        "detail": "Measured from the order date.",
    },
    {
        "category": "glossary",
        "term": "premium member",
        "statement": "A premium member is a customer whose refunds skip the review queue.",
        "detail": "",
    },
]


def _seed_project(db_session) -> Project:
    project = Project(
        guid=PROJECT_GUID,
        provider_kind="ado",
        external_id="ext-overlay",
        name=PROJECT_NAME,
        active=True,
    )
    db_session.add(project)
    db_session.commit()
    return project


def _seed_source(db_session, *, title: str = "Operations handbook") -> BusinessSource:
    row = BusinessSource(
        project_guid=PROJECT_GUID,
        project_key=PROJECT_NAME,
        owner_id=None,
        kind="upload",
        title=title,
        status="synced",
    )
    db_session.add(row)
    db_session.commit()
    return row


def _seed_ingested(db_session, source_id: int | None = None) -> dict[str, BusinessFact]:
    """The two ingested facts a re-sync would later collide with."""
    rows = {}
    for fact in DISTILLED:
        row = BusinessFact(
            project_guid=PROJECT_GUID,
            owner_id=None,
            source_id=source_id,
            category=fact["category"],
            term=fact["term"],
            statement=fact["statement"],
            detail=fact["detail"],
            origin="ingested",
            rank_text=f"{fact['term']} {fact['statement']}",
        )
        db_session.add(row)
        rows[fact["term"]] = row
    db_session.commit()
    return rows


def _fact(db_session, fact_id: int) -> BusinessFact:
    db_session.expire_all()
    return db_session.get(BusinessFact, fact_id)


# --------------------------------------------------------------------------
# The no-clobber ladder, at the merge
# --------------------------------------------------------------------------


def test_a_human_addition_survives_a_resync_that_would_overwrite_it(db_session):
    """ADR 0016 §5: a human addition (layer 2) outranks an ingested fact (layer 3).

    An addition is deliberately **not** pinned — pinning is reserved for a
    correction that overrides a specific source statement — so the ``pinned``
    guard #824 shipped does not cover it, and a re-sync that distilled the same
    term silently rewrote the user's own sentence.

    The negative control is the second half: the ingested row IS refreshed by
    the same call, so the test cannot pass because the merge stopped merging.
    """
    _seed_project(db_session)
    ingested = _seed_ingested(db_session)
    ingested["premium member"].statement = "stale"
    addition = BusinessFact(
        project_guid=PROJECT_GUID,
        owner_id=None,
        category="rule",
        term="refund window",
        statement="Refunds are 45 days for members of the loyalty tier.",
        detail="Not stated anywhere in the handbook — added by the QC lead.",
        origin="manual",
        pinned=False,
    )
    db_session.add(addition)
    # The ingested row for that same term is gone (the addition replaced it in
    # the user's mind); the collision is with the human's row alone.
    db_session.delete(ingested["refund window"])
    db_session.commit()
    addition_id, refreshed_id = addition.id, ingested["premium member"].id

    distil.merge_facts(db_session, PROJECT_GUID, None, DISTILLED)

    kept = _fact(db_session, addition_id)
    assert kept.statement == "Refunds are 45 days for members of the loyalty tier."
    assert kept.origin == "manual"
    # Negative control — the ingested row was refreshed by the very same call.
    assert _fact(db_session, refreshed_id).statement == DISTILLED[1]["statement"]


def test_a_correction_and_the_fact_it_supersedes_both_survive_a_resync(db_session):
    """The overlay is two rows, and a re-sync must leave both standing.

    The superseded row is checked explicitly: the correction winning by
    *deleting* the ingested fact would pass a "the correction survived" test
    while destroying the visible disagreement the overlay exists to show.
    """
    _seed_project(db_session)
    source = _seed_source(db_session)
    ingested = _seed_ingested(db_session, source.id)
    original = ingested["refund window"]

    correction = facts_service.correct_fact(
        db_session,
        original,
        statement="A refund may be requested within 45 days of the order date.",
        detail="Extended by the 2026 policy update; the handbook is out of date.",
        updated_by=None,
    )
    correction_id, original_id = correction.id, original.id

    distil.merge_facts(db_session, PROJECT_GUID, None, DISTILLED, source_id=source.id)

    kept = _fact(db_session, correction_id)
    assert kept.pinned is True
    assert kept.origin == "manual"
    assert kept.statement.endswith("45 days of the order date.")

    superseded = _fact(db_session, original_id)
    assert superseded is not None, "the ingested fact must stay, shown struck through"
    assert superseded.superseded_by == correction_id


# --------------------------------------------------------------------------
# The three affordances
# --------------------------------------------------------------------------


def test_correcting_a_fact_writes_an_overlay_rather_than_mutating_the_original(db_session):
    _seed_project(db_session)
    original = _seed_ingested(db_session)["refund window"]
    before = original.statement

    correction = facts_service.correct_fact(
        db_session, original, statement="45 days.", detail="", updated_by=7
    )

    assert correction.id != original.id
    assert correction.origin == "manual"
    assert correction.pinned is True
    assert correction.category == original.category
    assert correction.term == original.term
    assert correction.updated_by == 7
    assert correction.revision == 1
    # The ingested row is untouched apart from the pointer that marks it superseded.
    assert original.statement == before
    assert original.superseded_by == correction.id
    assert original.origin == "ingested"


def test_correcting_an_already_corrected_fact_moves_the_pointer(db_session):
    """A second correction supersedes the first, not the ingested original.

    Otherwise two pinned rows would both claim the same term and both reach the
    prompt, which is the one thing the overlay must not do.
    """
    _seed_project(db_session)
    original = _seed_ingested(db_session)["refund window"]
    first = facts_service.correct_fact(db_session, original, statement="45 days.", detail="")
    second = facts_service.correct_fact(db_session, first, statement="60 days.", detail="")

    assert _fact(db_session, first.id).superseded_by == second.id
    assert _fact(db_session, second.id).superseded_by is None
    assert _fact(db_session, original.id).superseded_by == first.id

    in_context = facts_service.facts_in_context(db_session, PROJECT_GUID, None)
    statements = [row.statement for row in in_context]
    assert "60 days." in statements
    assert "45 days." not in statements


def test_editing_a_manual_fact_bumps_its_revision(db_session):
    """Versioning is cheap on purpose: a counter and an author, no history table."""
    _seed_project(db_session)
    row = facts_service.add_fact(
        db_session,
        project_guid=PROJECT_GUID,
        owner_id=None,
        category="rule",
        term="loyalty tier",
        statement="Loyalty members get 45 days.",
        detail="",
        updated_by=3,
    )
    assert row.revision == 1

    facts_service.update_fact(db_session, row, statement="Loyalty members get 60 days.", updated_by=4)

    assert row.revision == 2
    assert row.updated_by == 4
    assert row.statement == "Loyalty members get 60 days."

    # Excluding is not an edit of the content, so it does not bump the revision.
    facts_service.update_fact(db_session, row, excluded=True, updated_by=4)
    assert row.revision == 2
    assert row.excluded is True


def test_an_excluded_fact_leaves_context_but_stays_on_the_row(db_session):
    """Excluding is "out of context, still on disk, one click to restore"."""
    _seed_project(db_session)
    rows = _seed_ingested(db_session)
    target = rows["premium member"]

    facts_service.update_fact(db_session, target, excluded=True)

    in_context = facts_service.facts_in_context(db_session, PROJECT_GUID, None)
    assert target.id not in {row.id for row in in_context}
    # Negative control — the row is still there, and the other fact still is too.
    assert _fact(db_session, target.id) is not None
    assert rows["refund window"].id in {row.id for row in in_context}

    facts_service.update_fact(db_session, target, excluded=False)
    assert target.id in {
        row.id for row in facts_service.facts_in_context(db_session, PROJECT_GUID, None)
    }


def test_facts_in_context_orders_by_the_precedence_ladder(db_session):
    """Human pinned corrections, then human additions, then ingested facts."""
    _seed_project(db_session)
    _seed_ingested(db_session)
    facts_service.add_fact(
        db_session,
        project_guid=PROJECT_GUID,
        owner_id=None,
        category="rule",
        term="loyalty tier",
        statement="An addition.",
        detail="",
    )
    original = (
        db_session.query(BusinessFact).filter(BusinessFact.term == "refund window").first()
    )
    facts_service.correct_fact(db_session, original, statement="A correction.", detail="")

    ordered = facts_service.facts_in_context(db_session, PROJECT_GUID, None)
    origins = [(row.origin, row.pinned) for row in ordered]
    assert origins[0] == ("manual", True)
    assert origins[1] == ("manual", False)
    assert all(row.origin == "ingested" for row in ordered[2:])
    assert ordered  # the ingested tail is non-empty, so the slice above means something


# --------------------------------------------------------------------------
# The HTTP surface (#828 builds on these endpoints)
# --------------------------------------------------------------------------


def _seed_owned(db_session, owner_id: int | None) -> BusinessFact:
    row = BusinessFact(
        project_guid=PROJECT_GUID,
        owner_id=owner_id,
        category="rule",
        term="refund window",
        statement="A refund may be requested within 30 days of the order date.",
        origin="ingested",
    )
    db_session.add(row)
    db_session.commit()
    return row


def test_the_endpoints_add_correct_and_exclude(client, db_session):
    """One pass over the three affordances, asserting the fields each is about.

    No ``==`` against a whole response body (CLAUDE.md, #579): a correct
    additive change to ``BusinessFactOut`` must not fail this test.
    """
    _seed_project(db_session)
    ingested = _seed_owned(db_session, None)

    added = client.post(
        f"/projects/{PROJECT_GUID}/business/facts",
        json={
            "category": "constraint",
            "term": "loyalty tier",
            "statement": "Loyalty members get 60 days.",
            "detail": "Agreed with the product owner; not in any document.",
        },
    )
    assert added.status_code == 201, added.text
    assert added.json()["origin"] == "manual"
    assert added.json()["pinned"] is False
    assert added.json()["revision"] == 1

    corrected = client.post(
        f"/projects/{PROJECT_GUID}/business/facts/{ingested.id}/correct",
        json={"statement": "45 days.", "detail": "The handbook is out of date."},
    )
    assert corrected.status_code == 201, corrected.text
    correction = corrected.json()
    assert correction["pinned"] is True
    assert correction["term"] == "refund window"
    assert correction["id"] != ingested.id

    listed = client.get(f"/projects/{PROJECT_GUID}/business/facts")
    assert listed.status_code == 200
    rows = {row["id"]: row for row in listed.json()}
    # The superseded original is listed, so the UI can strike it through.
    assert rows[ingested.id]["supersededBy"] == correction["id"]
    # ...and precedence order puts the pinned correction first.
    assert listed.json()[0]["id"] == correction["id"]

    excluded = client.patch(
        f"/projects/{PROJECT_GUID}/business/facts/{added.json()['id']}",
        json={"excluded": True},
    )
    assert excluded.status_code == 200
    assert excluded.json()["excluded"] is True
    # Negative control — excluding is not deleting.
    assert db_session.get(BusinessFact, added.json()["id"]) is not None


def test_editing_an_ingested_fact_is_refused_with_the_correction_route(client, db_session):
    """Ingested content is immutable; the 400 says what to do instead."""
    _seed_project(db_session)
    ingested = _seed_owned(db_session, None)
    before = ingested.statement

    resp = client.patch(
        f"/projects/{PROJECT_GUID}/business/facts/{ingested.id}",
        json={"statement": "rewritten in place"},
    )

    assert resp.status_code == 400
    assert "correct it instead" in resp.json()["detail"]
    assert _fact(db_session, ingested.id).statement == before


def test_correcting_an_already_superseded_fact_is_refused(client, db_session):
    """A second correction belongs on the row that is actually in context."""
    _seed_project(db_session)
    ingested = _seed_owned(db_session, None)
    first = client.post(
        f"/projects/{PROJECT_GUID}/business/facts/{ingested.id}/correct",
        json={"statement": "45 days."},
    )
    assert first.status_code == 201

    again = client.post(
        f"/projects/{PROJECT_GUID}/business/facts/{ingested.id}/correct",
        json={"statement": "60 days."},
    )
    assert again.status_code == 409

    # Negative control — the correction itself can still be corrected.
    onward = client.post(
        f"/projects/{PROJECT_GUID}/business/facts/{first.json()['id']}/correct",
        json={"statement": "60 days."},
    )
    assert onward.status_code == 201


def test_an_unknown_category_and_an_empty_statement_are_refused(client, db_session):
    _seed_project(db_session)
    bad_category = client.post(
        f"/projects/{PROJECT_GUID}/business/facts",
        json={"category": "vibes", "term": "x", "statement": "y"},
    )
    assert bad_category.status_code == 400
    assert "vibes" in bad_category.json()["detail"]

    blank = client.post(
        f"/projects/{PROJECT_GUID}/business/facts",
        json={"category": "rule", "term": "x", "statement": "   "},
    )
    assert blank.status_code == 400


def test_another_users_fact_is_not_reachable(client, db_session):
    """404, not 403 — a 403 would confirm the row exists (ADR 0008/0009)."""
    from app.models.user import User
    from app.services import auth_service

    _seed_project(db_session)
    owner = User(
        email="overlay-owner@example.com",
        first_name="O",
        last_name="W",
        role="member",
        password_hash=auth_service.hash_password("password123"),
        is_active=True,
    )
    db_session.add(owner)
    db_session.commit()
    theirs = _seed_owned(db_session, owner.id)

    intruder = User(
        email="overlay-intruder@example.com",
        first_name="I",
        last_name="N",
        role="member",
        password_hash=auth_service.hash_password("password123"),
        is_active=True,
    )
    db_session.add(intruder)
    db_session.commit()
    headers = {
        "Authorization": f"Bearer {auth_service.create_access_token(intruder, sid='sid')}"
    }

    resp = client.patch(
        f"/projects/{PROJECT_GUID}/business/facts/{theirs.id}",
        json={"excluded": True},
        headers=headers,
    )
    assert resp.status_code == 404
    assert _fact(db_session, theirs.id).excluded is False
