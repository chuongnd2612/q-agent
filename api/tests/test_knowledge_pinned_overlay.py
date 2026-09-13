"""The code knowledge base's human-correction overlay (#827, ADR 0016 §5).

The code KB has never been human-editable, so nothing in it has ever had to
survive a machine writing over it. #828 makes it editable per entry, and this
file pins the three properties that have to hold *before* that is safe:

1. ``apply_build`` — the highest-risk path in the epic. It assigns
   ``row.knowledge = payload["knowledge"]``, replacing the blob wholesale, so
   without a carry-forward the next ``project-bootstrap`` destroys every manual
   correction **silently**: no error, no log, the correction is simply gone.
2. The three machine-merge paths (``propose_selector_fix``,
   ``merge_discovered_dom``, ``merge_verified_discovery``) leave a pinned entry
   alone — the ``verified_at_runtime`` no-clobber rule, one rung higher up the
   ladder in ADR 0016 §5.

Every assertion carries a **negative control** in the same test: a carry-forward
that copied the whole previous blob forward would pass "the pinned entry
survived" exactly as well as the correct rule does, so each test also proves
that an *unpinned* stale entry is NOT carried, and that a machine merge still
updates an unpinned entry. Without that half the test proves nothing.
"""

from __future__ import annotations

from app.db import utcnow
from app.models.knowledge import ProjectKnowledge
from app.services import knowledge_service

PROJECT = "Surency Platform"

#: What a human pinned. Shaped like a real KB entry — a pinned entry is an
#: ordinary entry carrying ``pinned``, not a second kind of record.
PINNED_SELECTOR = {
    "screen": "Login",
    "element": "submit",
    "selector": "#login-submit-corrected",
    "pinned": True,
    "source": "manual",
}
PINNED_ROUTE = {
    "path": "/claims/adjudicate",
    "description": "Corrected by hand — the bootstrap read the wrong router file.",
    "pinned": True,
    "source": "manual",
}

#: What the machine produced, knowing nothing of the correction above.
REBUILT_KNOWLEDGE = {
    "routes": [{"path": "/home", "description": "Landing"}],
    "selectors": [{"screen": "Home", "element": "cta", "selector": "#cta"}],
    "stack": ["React 19"],
}


def _seed_row(db_session, *, routes=None, selectors=None, rebuild=False) -> ProjectKnowledge:
    """A ``ProjectKnowledge`` row holding ``routes``/``selectors``.

    :param rebuild: When true the row already has a ``last_indexed``, which is
        what ``apply_build`` reads to tell a rebuild from a first index — the
        rebuild is the case a correction has to survive.
    """
    row = ProjectKnowledge(
        key=PROJECT,
        project_key=PROJECT,
        name=PROJECT,
        owner_id=None,
        status="indexed",
        knowledge={"routes": list(routes or []), "selectors": list(selectors or [])},
    )
    if rebuild:
        row.last_indexed = utcnow()
        row.version = "v1"
    db_session.add(row)
    db_session.commit()
    return row


def _selector(row: ProjectKnowledge, value: str) -> dict | None:
    return next(
        (s for s in row.knowledge.get("selectors", []) if s.get("selector") == value), None
    )


def _route(row: ProjectKnowledge, path: str) -> dict | None:
    return next((r for r in row.knowledge.get("routes", []) if r.get("path") == path), None)


# --------------------------------------------------------------------------
# 1. apply_build — the wholesale blob replacement
# --------------------------------------------------------------------------


def test_apply_build_carries_pinned_entries_forward_and_drops_unpinned_ones(db_session):
    """A rebuild keeps the human's corrections and nothing else of the old blob.

    The negative control is the second half: the stale *unpinned* entry must be
    gone. A carry-forward that merged the whole previous blob forward would
    satisfy the first assertion while quietly making every rebuild additive —
    the KB would then never shed an entry the code no longer has.
    """
    stale = {"screen": "Login", "element": "old", "selector": "#stale-generated"}
    row = _seed_row(
        db_session,
        routes=[PINNED_ROUTE, {"path": "/stale", "description": "gone from the code"}],
        selectors=[PINNED_SELECTOR, stale],
        rebuild=True,
    )

    knowledge_service.apply_build(row, {"knowledge": dict(REBUILT_KNOWLEDGE), "confidence": 91})

    # The corrections survived the rebuild, flag and all.
    carried = _selector(row, PINNED_SELECTOR["selector"])
    assert carried is not None, "the pinned selector was destroyed by the rebuild"
    assert carried["pinned"] is True
    assert carried["screen"] == "Login"
    carried_route = _route(row, PINNED_ROUTE["path"])
    assert carried_route is not None, "the pinned route was destroyed by the rebuild"
    assert carried_route["description"] == PINNED_ROUTE["description"]

    # Negative control — the unpinned entries of the OLD blob are not carried.
    assert _selector(row, "#stale-generated") is None
    assert _route(row, "/stale") is None

    # ...and the rebuild's own content landed, with the version bumped.
    assert _selector(row, "#cta") is not None
    assert _route(row, "/home") is not None
    assert row.version == "v2"


def test_apply_build_lets_the_rebuild_win_where_it_agrees_with_a_pinned_entry(db_session):
    """A pinned entry replaces the rebuilt one it collides with, never duplicates it.

    Two entries with the same selector would both reach every prompt, and the
    machine's one would sit there contradicting the human's.
    """
    row = _seed_row(db_session, selectors=[PINNED_SELECTOR], rebuild=True)
    payload = {
        "knowledge": {
            "routes": [],
            "selectors": [
                {
                    "screen": "Login",
                    "element": "submit",
                    "selector": PINNED_SELECTOR["selector"],
                    "source": "bootstrap",
                }
            ],
        },
        "confidence": 80,
    }

    knowledge_service.apply_build(row, payload)

    matches = [
        s for s in row.knowledge["selectors"] if s.get("selector") == PINNED_SELECTOR["selector"]
    ]
    assert len(matches) == 1
    assert matches[0]["pinned"] is True
    assert matches[0]["source"] == "manual"


def test_apply_build_on_a_first_index_has_nothing_to_carry(db_session):
    """A first index is not a rebuild — there is no previous blob to preserve."""
    row = _seed_row(db_session, selectors=[PINNED_SELECTOR], rebuild=False)

    knowledge_service.apply_build(row, {"knowledge": dict(REBUILT_KNOWLEDGE), "confidence": 70})

    assert row.version == "v1"
    assert _selector(row, PINNED_SELECTOR["selector"]) is None


# --------------------------------------------------------------------------
# 2. The machine-merge paths
# --------------------------------------------------------------------------


def test_propose_selector_fix_never_rewrites_a_pinned_selector(db_session):
    """A self-heal must not undo a human's correction (ADR 0016 §5: 1 beats 4)."""
    pinned = {**PINNED_SELECTOR, "selector": "#human-choice"}
    unpinned = {"screen": "Login", "element": "submit", "selector": "#machine-choice"}
    _seed_row(db_session, selectors=[pinned, unpinned])

    assert knowledge_service.propose_selector_fix(
        PROJECT, "", "#human-choice", "#healed", None
    ) is False

    # Negative control — the same call against the UNPINNED entry does land, so
    # this test cannot pass because the whole function stopped working.
    assert knowledge_service.propose_selector_fix(
        PROJECT, "", "#machine-choice", "#healed", None
    ) is True

    db_session.expire_all()
    row = db_session.query(ProjectKnowledge).filter(ProjectKnowledge.key == PROJECT).first()
    assert _selector(row, "#human-choice") is not None
    assert _selector(row, "#healed") is not None


def test_merge_verified_discovery_never_upgrades_a_pinned_entry(db_session):
    """``pinned`` joins ``verified_at_runtime`` in the no-clobber condition.

    A runtime observation outranks a source parse (ADR 0010 §6) but not a human
    (ADR 0016 §5). The unpinned half of the same call proves the rule
    discriminates rather than simply skipping everything.
    """
    pinned = {
        "screen": "Login",
        "element": "submit",
        "selector": "#pinned-sel",
        "strategy": "css",
        "pinned": True,
    }
    unpinned = {"screen": "Login", "element": "email", "selector": "#unpinned-sel"}
    _seed_row(
        db_session,
        routes=[{"path": "/pinned", "description": "by hand", "pinned": True}],
        selectors=[pinned, unpinned],
    )

    merged = knowledge_service.merge_verified_discovery(
        PROJECT,
        "",
        {
            "routes": [{"path": "/pinned", "description": "observed"}],
            "selectors": [
                {"screen": "Observed", "element": "submit", "selector": "#pinned-sel"},
                {"screen": "Observed", "element": "email", "selector": "#unpinned-sel"},
            ],
        },
        owner_id=None,
    )

    db_session.expire_all()
    row = db_session.query(ProjectKnowledge).filter(ProjectKnowledge.key == PROJECT).first()

    kept = _selector(row, "#pinned-sel")
    assert kept["screen"] == "Login"
    assert "verified_at_runtime" not in kept
    assert _route(row, "/pinned")["description"] == "by hand"

    # Negative control — the unpinned entry WAS upgraded by the very same call.
    upgraded = _selector(row, "#unpinned-sel")
    assert upgraded["screen"] == "Observed"
    assert upgraded.get("verified_at_runtime")
    assert merged == 1


def test_merge_discovered_dom_leaves_a_pinned_entry_alone(db_session):
    """A DOM discovery colliding with a pinned entry adds nothing and rewrites nothing."""
    _seed_row(
        db_session,
        routes=[{"path": "/pinned", "description": "by hand", "pinned": True}],
        selectors=[{**PINNED_SELECTOR, "selector": "#pinned-sel"}],
    )

    added = knowledge_service.merge_discovered_dom(
        PROJECT, "", {"route": "/pinned", "selectors": ["#pinned-sel", "#brand-new"]}, None
    )

    db_session.expire_all()
    row = db_session.query(ProjectKnowledge).filter(ProjectKnowledge.key == PROJECT).first()
    assert _route(row, "/pinned")["description"] == "by hand"
    assert _selector(row, "#pinned-sel")["pinned"] is True
    # Negative control — a genuinely new selector is still added.
    assert added == 1
    assert _selector(row, "#brand-new") is not None
