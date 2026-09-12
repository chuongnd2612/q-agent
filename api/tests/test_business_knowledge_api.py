"""Business Knowledge source CRUD (#817) — including the ownership refusal.

The negative control is the point of this file. A happy-path test proves the
endpoint answers; it proves nothing about who it answers *for*, and the
artifact-authorisation bugs this codebase keeps producing (#819/#820 most
recently) are all of that shape. So every single-row endpoint is exercised twice:
once by the owner, once by a second authenticated user who must be refused — and
the refusal is pinned by an **observable effect** (A's row is untouched, A can
still read it), not only by the status code, so the tests cannot pass if the
guard 404s and mutates anyway.

Nothing here asserts ``==`` against a whole response body (#579): additive fields
are correct changes and a whole-body equality would rot instead of being
maintained.
"""

from __future__ import annotations

import pytest

from app.models.business import BusinessSource
from app.models.project import Project
from app.services import auth_service
from app.services.workspace_scope import scoped_business_dir


def _make_user(db_session, email, password="password123", role="member"):
    from app.models.user import User

    user = User(
        email=email,
        first_name="Test",
        last_name="User",
        role=role,
        password_hash=auth_service.hash_password(password),
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _auth_headers(user) -> dict:
    return {"Authorization": f"Bearer {auth_service.create_access_token(user, sid='test-sid')}"}


@pytest.fixture
def auth_on(monkeypatch):
    """Turn the global auth guard on for the duration of a test.

    The suite runs with ``auth_required = False`` (``tests/conftest.py``), which
    makes ``current_user`` resolve to ``None`` and every ownership helper a
    passthrough — the #91 bridge. An ownership test that did not flip this would
    be asserting against the bridge, not against the guard.
    """
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "auth_required", True)
    yield


def _project(db_session, name, owner) -> Project:
    project = Project(
        provider_kind="ado",
        external_id=f"ext-{name}",
        name=name,
        active=True,
        owner_id=owner.id if owner is not None else None,
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)
    return project


@pytest.fixture
def two_projects(db_session, auth_on):
    """Users A and B, each with their own project and bearer token."""
    user_a = _make_user(db_session, "biz-a@example.com")
    user_b = _make_user(db_session, "biz-b@example.com")
    return {
        "a": (user_a, _auth_headers(user_a), _project(db_session, "A Product", user_a)),
        "b": (user_b, _auth_headers(user_b), _project(db_session, "B Product", user_b)),
    }


def _create(client, guid, headers, **body):
    payload = {"kind": "url", "title": "Eligibility rules", "url": "https://wiki.test/elig"}
    payload.update(body)
    return client.post(f"/projects/{guid}/business/sources", json=payload, headers=headers)


# --------------------------------------------------------------------- happy path
def test_create_lists_and_starts_pending(client, db_session, two_projects):
    """A registered source is listed, and it is `pending` — nothing fetched it."""
    _, headers, project = two_projects["a"]

    created = _create(client, project.guid, headers)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["kind"] == "url"
    assert body["url"] == "https://wiki.test/elig"
    assert body["status"] == "pending"
    assert body["fetchedAt"] is None
    assert body["excluded"] is False
    # The GUID is what the row is keyed on (ADR 0013 / #585), and the name rides
    # along denormalized for display.
    assert body["projectGuid"] == project.guid
    assert body["projectKey"] == "A Product"

    listed = client.get(f"/projects/{project.guid}/business/sources", headers=headers)
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [body["id"]]


def test_a_project_name_resolves_to_its_guid(client, db_session, two_projects):
    """The #585 bridge: a name-based deep link must not store a name as the GUID."""
    _, headers, project = two_projects["a"]

    created = _create(client, project.name, headers)
    assert created.status_code == 201
    assert created.json()["projectGuid"] == project.guid
    assert db_session.get(BusinessSource, created.json()["id"]).project_guid == project.guid


def test_patch_renames_and_excludes_without_deleting(client, db_session, two_projects):
    """`excluded` takes a source out of context; the row and its provenance stay."""
    _, headers, project = two_projects["a"]
    source_id = _create(client, project.guid, headers).json()["id"]

    resp = client.patch(
        f"/projects/{project.guid}/business/sources/{source_id}",
        json={"title": "Eligibility rules (2026)", "excluded": True},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "Eligibility rules (2026)"
    assert resp.json()["excluded"] is True

    row = db_session.get(BusinessSource, source_id)
    db_session.refresh(row)
    assert row.excluded is True
    assert row.url == "https://wiki.test/elig", "excluding must not clear provenance"

    # An omitted field is left alone, not reset to its default.
    resp = client.patch(
        f"/projects/{project.guid}/business/sources/{source_id}",
        json={"title": "Renamed again"},
        headers=headers,
    )
    assert resp.json()["excluded"] is True


def test_delete_removes_the_row_and_its_artifacts(client, db_session, two_projects):
    """Deleting a source must not leave unreachable bytes under the owner's scope.

    Proved with a **negative control on the filesystem** as well: a second
    source's snapshot sitting in the same directory has to survive, so the test
    cannot pass if the delete wipes the scope wholesale.
    """
    user_a, headers, project = two_projects["a"]
    doomed_id = _create(client, project.guid, headers).json()["id"]
    kept_id = _create(
        client, project.guid, headers, title="Claims flow", url="https://wiki.test/claims"
    ).json()["id"]

    business_dir = scoped_business_dir(user_a.id)
    (business_dir / "a-product").mkdir(parents=True, exist_ok=True)
    doomed_raw = business_dir / "a-product" / "elig.raw.md"
    doomed_norm = business_dir / "a-product" / "elig.md"
    kept_norm = business_dir / "a-product" / "claims.md"
    for path in (doomed_raw, doomed_norm, kept_norm):
        path.write_text("# snapshot", encoding="utf-8")

    doomed = db_session.get(BusinessSource, doomed_id)
    doomed.raw_path = "a-product/elig.raw.md"
    doomed.normalized_path = "a-product/elig.md"
    kept = db_session.get(BusinessSource, kept_id)
    kept.normalized_path = "a-product/claims.md"
    db_session.commit()

    resp = client.delete(f"/projects/{project.guid}/business/sources/{doomed_id}", headers=headers)
    assert resp.status_code == 204

    assert db_session.get(BusinessSource, doomed_id) is None
    assert db_session.get(BusinessSource, kept_id) is not None, "the delete over-reached"
    assert not doomed_raw.exists()
    assert not doomed_norm.exists()
    assert kept_norm.exists(), "another source's snapshot was deleted"


def test_artifact_path_escaping_the_scope_is_refused(client, db_session, two_projects, tmp_path):
    """A stored path that climbs out of the business scope deletes nothing.

    ``raw_path`` is written by our own code today, but it is a *stored* value, and
    the delete joins it onto a filesystem root. Pinning the refusal means a future
    ingestion bug cannot turn into an arbitrary unlink.
    """
    user_a, headers, project = two_projects["a"]
    source_id = _create(client, project.guid, headers).json()["id"]

    outsider = scoped_business_dir(user_a.id).parent / "not-business.md"
    outsider.parent.mkdir(parents=True, exist_ok=True)
    outsider.write_text("untouchable", encoding="utf-8")

    row = db_session.get(BusinessSource, source_id)
    row.raw_path = "../not-business.md"
    db_session.commit()

    assert (
        client.delete(
            f"/projects/{project.guid}/business/sources/{source_id}", headers=headers
        ).status_code
        == 204
    )
    assert outsider.exists(), "a ../ path escaped the owner's business scope"


# --------------------------------------------------------------------- validation
def test_unknown_kind_and_unparseable_url_are_refused(client, two_projects):
    """Refused at registration, with a message, rather than at first fetch."""
    _, headers, project = two_projects["a"]

    bad_kind = _create(client, project.guid, headers, kind="notion")
    assert bad_kind.status_code == 400
    # `notion` is deferred to v2 (#832) — the message names the kinds that work.
    assert "github_md" in bad_kind.json()["detail"]

    for url in ("", "not a url", "ftp://wiki.test/x", "https:///nohost"):
        resp = _create(client, project.guid, headers, url=url)
        assert resp.status_code == 400, f"{url!r} was accepted"


def test_upload_needs_no_url_and_never_stores_one(client, db_session, two_projects):
    """An upload's `url` stays NULL — the invariant the unique key is built on."""
    _, headers, project = two_projects["a"]

    resp = _create(
        client, project.guid, headers, kind="upload", title="rules.md", url="https://ignored.test"
    )
    assert resp.status_code == 201
    assert resp.json()["url"] is None
    assert db_session.get(BusinessSource, resp.json()["id"]).url is None


def test_duplicate_link_is_refused_and_so_is_a_duplicate_upload(client, two_projects):
    """The unique key de-duplicates links only; uploads are de-duplicated here.

    ``url`` is NULL for an upload and NULLs compare distinct in a unique index, so
    ``uq_business_source_project_kind_url`` is inert for uploads by construction.
    The service layer makes the call instead: an upload de-duplicates on its
    title, case-insensitively, because in this slice the filename is the only
    identity it has (see ``business_source_service.find_duplicate``).
    """
    _, headers, project = two_projects["a"]

    assert _create(client, project.guid, headers).status_code == 201
    dup_link = _create(client, project.guid, headers, title="A different label")
    assert dup_link.status_code == 409

    assert _create(client, project.guid, headers, kind="upload", title="rules.md").status_code == 201
    dup_upload = _create(client, project.guid, headers, kind="upload", title="  RULES.MD  ")
    assert dup_upload.status_code == 409
    assert "rules.md" in dup_upload.json()["detail"]

    # A genuinely different upload still lands — the check is a de-duplication,
    # not a one-upload-per-project rule.
    assert (
        _create(client, project.guid, headers, kind="upload", title="claims.md").status_code == 201
    )


def test_the_same_link_lands_once_per_user(client, db_session, two_projects):
    """Ownership is part of the key (ADR 0009 §3): B's copy is not A's duplicate."""
    _, headers_a, project_a = two_projects["a"]
    _, headers_b, project_b = two_projects["b"]

    assert _create(client, project_a.guid, headers_a).status_code == 201
    assert _create(client, project_b.guid, headers_b).status_code == 201


# ---------------------------------------------------------------- negative control
def test_another_user_cannot_see_patch_or_delete_a_source(client, db_session, two_projects):
    """B is refused on every single-row path, and A's row is provably untouched."""
    user_a, headers_a, project_a = two_projects["a"]
    _, headers_b, _ = two_projects["b"]

    source_id = _create(client, project_a.guid, headers_a).json()["id"]
    assert db_session.get(BusinessSource, source_id).owner_id == user_a.id

    # B cannot even name A's project.
    assert (
        client.get(f"/projects/{project_a.guid}/business/sources", headers=headers_b).status_code
        == 404
    )
    assert _create(client, project_a.guid, headers_b).status_code == 404
    assert (
        client.patch(
            f"/projects/{project_a.guid}/business/sources/{source_id}",
            json={"title": "hijacked", "excluded": True},
            headers=headers_b,
        ).status_code
        == 404
    )
    assert (
        client.delete(
            f"/projects/{project_a.guid}/business/sources/{source_id}", headers=headers_b
        ).status_code
        == 404
    )

    # The refusal is real, not a 404 thrown after the mutation: the row is
    # unchanged and A can still read it.
    row = db_session.get(BusinessSource, source_id)
    db_session.refresh(row)
    assert row.title == "Eligibility rules"
    assert row.excluded is False
    still_there = client.get(f"/projects/{project_a.guid}/business/sources", headers=headers_a)
    assert [r["id"] for r in still_there.json()] == [source_id]


def test_a_source_cannot_be_reached_through_another_project_of_the_same_owner(
    client, db_session, two_projects
):
    """Naming a different project in the path does not hop to someone else's row.

    Both projects here belong to **A**, so ownership alone cannot refuse this —
    only the `project_guid` check in `source_or_404` can, which is exactly what
    this pins.
    """
    user_a, headers_a, project_a = two_projects["a"]
    other = _project(db_session, "A Second Product", user_a)

    source_id = _create(client, project_a.guid, headers_a).json()["id"]

    assert (
        client.patch(
            f"/projects/{other.guid}/business/sources/{source_id}",
            json={"title": "wrong project"},
            headers=headers_a,
        ).status_code
        == 404
    )
    assert (
        client.delete(
            f"/projects/{other.guid}/business/sources/{source_id}", headers=headers_a
        ).status_code
        == 404
    )
    assert db_session.get(BusinessSource, source_id) is not None


def test_the_list_is_scoped_even_when_a_project_is_shared(client, db_session, auth_on):
    """An unowned (shared) project still does not expose another user's sources.

    The project row is reachable by both users — `owner_id IS NULL` is everyone's,
    per `ownership._ownership_mismatch` — so the refusal has to come from the
    per-row scoping in `visible_sources`, not from the project lookup.
    """
    user_a = _make_user(db_session, "shared-a@example.com")
    user_b = _make_user(db_session, "shared-b@example.com")
    project = _project(db_session, "Shared Product", None)

    headers_a = _auth_headers(user_a)
    headers_b = _auth_headers(user_b)

    a_source = _create(client, project.guid, headers_a).json()["id"]
    b_source = _create(
        client, project.guid, headers_b, title="B's doc", url="https://wiki.test/b"
    ).json()["id"]

    assert [r["id"] for r in client.get(
        f"/projects/{project.guid}/business/sources", headers=headers_a
    ).json()] == [a_source]
    assert [r["id"] for r in client.get(
        f"/projects/{project.guid}/business/sources", headers=headers_b
    ).json()] == [b_source]


def test_an_unknown_project_is_a_404_not_an_empty_list(client, two_projects):
    """An empty list would read as "this project has no sources", which is a lie."""
    _, headers, _ = two_projects["a"]
    assert (
        client.get(
            "/projects/00000000-0000-4000-8000-000000000000/business/sources", headers=headers
        ).status_code
        == 404
    )
