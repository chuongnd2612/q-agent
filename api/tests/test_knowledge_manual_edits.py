"""`PATCH /projects/{key}/repos/{repo}/knowledge` — the code KB becomes editable (#828).

Until this endpoint existed the code knowledge base was **rebuild-only**: there
was no `PUT`/`PATCH`/`DELETE` on it anywhere, so the only way to fix a wrong
selector was to re-bootstrap and hope. ADR 0016 §5 gates making it editable on
#827's carry-forward, because `apply_build` replaces the blob wholesale.

Two properties carry the slice, and each is written with its **negative control
first** — an assertion that would also pass under the broken behaviour proves
nothing:

1. **Ownership is enforced.** The refusal is the test, not the happy path: an
   endpoint that 200s for everybody would sail through a happy-path-only test.
   Proven with `auth_required=True` and two real bearer tokens, because with the
   suite default `current_user` is `None`, `owned()` is a passthrough and every
   ownership check is a no-op. The control: the *owner's* identical request
   succeeds, so the 404 is about ownership and not about the row being missing —
   and the stored blob is re-read to show the intruder changed nothing.
2. **A manual edit survives a rebuild.** Asserted through `apply_build` with a
   real build payload that contradicts the correction. The control: a *machine*
   entry in the same blob is still shed by the same rebuild, so the test cannot
   pass by simply copying the old blob forward.
"""

from __future__ import annotations

import pytest

from app.db import utcnow
from app.models.knowledge import ProjectKnowledge, compose_key
from app.models.project_config import ProjectConfig
from app.models.user import User
from app.services import auth_service, knowledge_service

PROJECT = "Surency Platform"
REPO = "surency-web"

#: What the bootstrap got wrong — the selector a human is about to correct.
MACHINE_BLOB = {
    "routes": [{"path": "/claims", "description": "Claims list"}],
    "selectors": [
        {"screen": "Login", "element": "submit", "selector": "#wrong-submit"},
        {"screen": "Home", "element": "cta", "selector": "#machine-only"},
    ],
    "domain": "",
    "business_entities": [],
}


@pytest.fixture
def auth_on(monkeypatch, workspace_dir):
    """Identity in play. Applied after ``workspace_dir``, which rebuilds settings."""
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "auth_required", True)
    return config_module.settings


def _user(db, email: str) -> User:
    user = User(email=email, password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _hdr(user: User) -> dict:
    return {
        "Authorization": f"Bearer {auth_service.create_access_token(user, sid=f'sid-{user.id}')}"
    }


def _seed(db, *, owner_id: int | None, blob: dict | None = None) -> ProjectKnowledge:
    """A configured project plus its per-repo knowledge row, owned by ``owner_id``."""
    db.add(
        ProjectConfig(
            key=PROJECT,
            name=PROJECT,
            owner_id=owner_id,
            base_url="https://app.example",
            repos=[{"name": REPO}],
        )
    )
    row = ProjectKnowledge(
        key=compose_key(PROJECT, REPO),
        project_key=PROJECT,
        name=PROJECT,
        repo=REPO,
        owner_id=owner_id,
        status="indexed",
        confidence=80,
        last_indexed=utcnow(),
        knowledge={
            "routes": list((blob or MACHINE_BLOB)["routes"]),
            "selectors": list((blob or MACHINE_BLOB)["selectors"]),
            "domain": (blob or MACHINE_BLOB)["domain"],
            "business_entities": list((blob or MACHINE_BLOB)["business_entities"]),
        },
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _selector(db, value: str) -> dict | None:
    row = db.query(ProjectKnowledge).filter(
        ProjectKnowledge.key == compose_key(PROJECT, REPO)
    ).first()
    db.refresh(row)
    return next((s for s in row.knowledge.get("selectors", []) if s.get("selector") == value), None)


# ----------------------------------------------------------------- the endpoint
def test_editing_a_selector_upserts_it_and_stamps_it_manual_and_pinned(client, db_session):
    _seed(db_session, owner_id=None)

    resp = client.patch(
        f"/projects/{PROJECT}/repos/{REPO}/knowledge",
        json={
            "selectors": [
                {"screen": "Login", "element": "submit", "selector": "#login-submit"}
            ]
        },
    )

    assert resp.status_code == 200
    corrected = _selector(db_session, "#login-submit")
    assert corrected is not None, "the corrected selector was not written"
    assert corrected["origin"] == "manual"
    assert corrected["pinned"] is True
    # Negative control — an upsert, not an append-everything and not a wipe: the
    # machine's other selector is untouched, and the routes section (not
    # mentioned in the PATCH) still has its entry.
    assert _selector(db_session, "#machine-only") is not None
    row = db_session.query(ProjectKnowledge).filter(
        ProjectKnowledge.key == compose_key(PROJECT, REPO)
    ).first()
    assert [r["path"] for r in row.knowledge["routes"]] == ["/claims"]


def test_editing_an_existing_entry_replaces_it_rather_than_duplicating_it(client, db_session):
    """Two entries for one selector would both reach every prompt, contradicting."""
    _seed(db_session, owner_id=None)

    resp = client.patch(
        f"/projects/{PROJECT}/repos/{REPO}/knowledge",
        json={
            "selectors": [
                {"screen": "Login", "element": "submit button", "selector": "#wrong-submit"}
            ]
        },
    )

    assert resp.status_code == 200
    row = db_session.query(ProjectKnowledge).filter(
        ProjectKnowledge.key == compose_key(PROJECT, REPO)
    ).first()
    db_session.refresh(row)
    matches = [s for s in row.knowledge["selectors"] if s["selector"] == "#wrong-submit"]
    assert len(matches) == 1
    assert matches[0]["element"] == "submit button"
    assert matches[0]["pinned"] is True


def test_domain_and_business_entities_are_editable_and_recorded_as_overridden(client, db_session):
    _seed(db_session, owner_id=None)

    resp = client.patch(
        f"/projects/{PROJECT}/repos/{REPO}/knowledge",
        json={
            "domain": "Employee benefits administration.",
            "businessEntities": ["Member", "Claim", "Plan"],
        },
    )

    assert resp.status_code == 200
    body = resp.json()["knowledge"]
    assert body["domain"] == "Employee benefits administration."
    assert body["business_entities"] == ["Member", "Claim", "Plan"]
    assert sorted(body[knowledge_service.PINNED_FIELDS_KEY]) == ["business_entities", "domain"]


def test_a_route_edit_accepts_either_spelling_of_auth_required(client, db_session):
    """The SPA's type is camelCase, the stored blob is snake_case (#828)."""
    _seed(db_session, owner_id=None)

    resp = client.patch(
        f"/projects/{PROJECT}/repos/{REPO}/knowledge",
        json={"routes": [{"path": "/admin", "description": "Admin", "authRequired": True}]},
    )

    assert resp.status_code == 200
    route = next(r for r in resp.json()["knowledge"]["routes"] if r["path"] == "/admin")
    assert route["auth_required"] is True
    assert route["pinned"] is True


def test_an_entry_with_no_identity_key_and_an_empty_patch_are_refused(client, db_session):
    _seed(db_session, owner_id=None)

    orphan = client.patch(
        f"/projects/{PROJECT}/repos/{REPO}/knowledge",
        json={"selectors": [{"screen": "Login", "element": "submit", "selector": "  "}]},
    )
    assert orphan.status_code == 400
    assert "selector" in orphan.json()["detail"]

    empty = client.patch(f"/projects/{PROJECT}/repos/{REPO}/knowledge", json={})
    assert empty.status_code == 400

    # Negative control — neither refusal wrote anything.
    row = db_session.query(ProjectKnowledge).filter(
        ProjectKnowledge.key == compose_key(PROJECT, REPO)
    ).first()
    db_session.refresh(row)
    assert len(row.knowledge["selectors"]) == 2
    assert knowledge_service.PINNED_FIELDS_KEY not in row.knowledge


def test_an_edit_is_refused_while_a_build_is_in_flight(client, db_session):
    """`apply_build` assigns the blob wholesale from a thread that never saw the edit."""
    row = _seed(db_session, owner_id=None)
    row.status = "indexing"
    db_session.commit()

    resp = client.patch(
        f"/projects/{PROJECT}/repos/{REPO}/knowledge",
        json={"domain": "Written mid-build."},
    )

    assert resp.status_code == 409
    assert _selector(db_session, "#wrong-submit") is not None


def test_patching_a_repo_with_no_knowledge_base_is_a_404(client, db_session):
    _seed(db_session, owner_id=None)
    resp = client.patch(
        f"/projects/{PROJECT}/repos/ghost/knowledge", json={"domain": "x"}
    )
    assert resp.status_code == 404


# ------------------------------------------------------------------- ownership
def test_another_user_cannot_patch_someones_knowledge_base(auth_on, client, db_session):
    """404, not 403 — a 403 would confirm the row exists (ADR 0008/0009).

    Runs with ``auth_required=True``: under the suite default ``current_user`` is
    ``None`` and every ownership check is a no-op, so this test would pass while
    exercising nothing.
    """
    alice = _user(db_session, "alice-kb@example.com")
    bob = _user(db_session, "bob-kb@example.com")
    _seed(db_session, owner_id=alice.id)

    intruder = client.patch(
        f"/projects/{PROJECT}/repos/{REPO}/knowledge",
        json={"selectors": [{"screen": "Login", "element": "submit", "selector": "#bobs-edit"}]},
        headers=_hdr(bob),
    )

    assert intruder.status_code == 404
    # The refusal actually refused: Bob's selector is nowhere in Alice's blob.
    assert _selector(db_session, "#bobs-edit") is None

    # Negative control — the SAME request from the owner succeeds, so the 404 is
    # about ownership, not about a row that was never reachable in the first place.
    owner = client.patch(
        f"/projects/{PROJECT}/repos/{REPO}/knowledge",
        json={"selectors": [{"screen": "Login", "element": "submit", "selector": "#alices-edit"}]},
        headers=_hdr(alice),
    )
    assert owner.status_code == 200
    assert _selector(db_session, "#alices-edit")["pinned"] is True


# --------------------------------------------------- the edit survives a rebuild
def test_an_edited_entry_keeps_its_pinned_stamp_through_a_rebuild(client, db_session):
    """The producing side of #827's carry-forward (ADR 0016 §5).

    A rebuild assigns `row.knowledge` wholesale, so without the carry-forward the
    correction is destroyed **silently** — no error, no log. The negative control
    is the machine-only selector: it is shed by the same rebuild, which is what
    proves the blob was not merely copied forward wholesale.
    """
    _seed(db_session, owner_id=None)
    edit = client.patch(
        f"/projects/{PROJECT}/repos/{REPO}/knowledge",
        json={
            "selectors": [{"screen": "Login", "element": "submit", "selector": "#login-submit"}],
            "domain": "Employee benefits administration.",
        },
    )
    assert edit.status_code == 200

    row = db_session.query(ProjectKnowledge).filter(
        ProjectKnowledge.key == compose_key(PROJECT, REPO)
    ).first()
    db_session.refresh(row)
    # A fresh build that knows nothing of the correction and contradicts it.
    knowledge_service.apply_build(
        row,
        {
            "knowledge": {
                "routes": [{"path": "/claims", "description": "Claims list"}],
                "selectors": [
                    {"screen": "Login", "element": "submit", "selector": "#wrong-submit"}
                ],
                "domain": "Whatever the parser inferred this time.",
                "business_entities": [],
            },
            "confidence": 72,
        },
    )
    db_session.commit()
    db_session.refresh(row)

    survived = _selector(db_session, "#login-submit")
    assert survived is not None, "the pinned selector was destroyed by the rebuild"
    assert survived["pinned"] is True
    assert survived["origin"] == "manual"
    assert row.knowledge["domain"] == "Employee benefits administration."
    # Negative control — the rebuild really did replace the blob: the machine-only
    # selector the new build no longer reports is gone, and the version advanced.
    assert _selector(db_session, "#machine-only") is None
    assert row.version == "v2"


def test_correcting_a_selectors_value_replaces_the_wrong_one(client, db_session):
    """The primary use case: "#wrong-submit should be #login-submit".

    The identity key IS the value being fixed, so without `replaces` the fix
    would *add* the right selector and leave the wrong one standing in every
    prompt — two contradicting entries, which is the failure the correction was
    meant to end.
    """
    _seed(db_session, owner_id=None)

    resp = client.patch(
        f"/projects/{PROJECT}/repos/{REPO}/knowledge",
        json={
            "selectors": [
                {
                    "screen": "Login",
                    "element": "submit",
                    "selector": "#login-submit",
                    "replaces": "#wrong-submit",
                }
            ]
        },
    )

    assert resp.status_code == 200
    assert _selector(db_session, "#login-submit")["pinned"] is True
    assert _selector(db_session, "#wrong-submit") is None, "the wrong selector survived the fix"
    # Negative control — a replace, not a purge: the unrelated machine entry stays,
    # and the corrected entry kept the position (and any machine keys) of the one
    # it superseded rather than being appended at the end.
    row = db_session.query(ProjectKnowledge).filter(
        ProjectKnowledge.key == compose_key(PROJECT, REPO)
    ).first()
    db_session.refresh(row)
    assert [s["selector"] for s in row.knowledge["selectors"]] == [
        "#login-submit",
        "#machine-only",
    ]


def test_replaces_naming_an_unknown_entry_is_a_plain_upsert(client, db_session):
    """A stale `replaces` (someone else rebuilt meanwhile) adds, never drops."""
    _seed(db_session, owner_id=None)

    resp = client.patch(
        f"/projects/{PROJECT}/repos/{REPO}/knowledge",
        json={"routes": [{"path": "/admin", "description": "Admin", "replaces": "/gone"}]},
    )

    assert resp.status_code == 200
    paths = [r["path"] for r in resp.json()["knowledge"]["routes"]]
    assert paths == ["/claims", "/admin"]
