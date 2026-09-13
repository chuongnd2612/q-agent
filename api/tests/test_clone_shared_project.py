"""Tests for cloning a shared-namespace project + admin shared management (#120, ADR 0009 §2/§4).

Covers ``app.services.clone_service`` end-to-end via the ``/shared/projects``
router: a seeded shared project (``owner_id=None``) — config with an
encrypted test account, a knowledge row, and on-disk knowledge files — clones
into an authenticated member's own scope (rows re-stamped, secrets decrypt
identically, files copied under ``users/<id>/…``). Also covers the 404/409
clone semantics and that shared-namespace writes are admin-only.

Business Knowledge (#831, ADR 0016) rides the same path and is tested the same
way plus one more: the clone's rows must be *new rows*, not shared references,
so the Business Knowledge tests here also mutate the clone and assert the
admin's original did not move.
"""

from __future__ import annotations

from app import crypto
from app.models.business import BusinessFact, BusinessSource
from app.models.knowledge import ProjectKnowledge
from app.models.project import Project
from app.models.project_config import ProjectConfig
from app.models.user import User
from app.services import auth_service, knowledge_service
from app.services.workspace_scope import scoped_business_dir, scoped_knowledge_dir, slug

PROJECT_KEY = "Surency Platform"


def _make_user(db_session, email, password="password123", role="member"):
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
    token = auth_service.create_access_token(user, sid="test-sid")
    return {"Authorization": f"Bearer {token}"}


def _auth_on(monkeypatch):
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "auth_required", True)


def _seed_shared_project(db_session, key: str = PROJECT_KEY) -> dict:
    """Seed a shared (``owner_id=None``) project: Project + ProjectConfig (with an
    encrypted test account) + ProjectKnowledge row + on-disk knowledge files."""
    db_session.add(
        Project(provider_kind="ado", external_id="shared-1", name=key, active=True, owner_id=None)
    )
    config = ProjectConfig(
        key=key,
        name=key,
        base_url="https://shared.surency.test",
        test_accounts=[
            {
                "role": "Internal Admin",
                "username": "qa@surency.test",
                "password": crypto.encrypt("s3cret!"),
                "notes": "seeded",
            }
        ],
        owner_id=None,
    )
    db_session.add(config)
    knowledge = ProjectKnowledge(
        key=key,
        project_key=key,
        name=key,
        provider="Azure DevOps",
        status="indexed",
        confidence=90,
        knowledge={"stack": ["React"]},
        owner_id=None,
    )
    db_session.add(knowledge)
    db_session.commit()

    out_dir = scoped_knowledge_dir(None) / slug(key)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "knowledge.json").write_text("{}", encoding="utf-8")
    (out_dir / "knowledge.md").write_text("# Surency Platform", encoding="utf-8")
    knowledge.doc_path = str(out_dir)
    db_session.commit()

    return {"config": config, "knowledge": knowledge}


def _seed_shared_business(db_session, project_guid: str, key: str = PROJECT_KEY) -> dict:
    """Seed the shared namespace's Business Knowledge for ``project_guid``.

    Two sources — an upload (``url IS NULL``, the case
    ``uq_business_source_project_kind_url`` cannot de-duplicate) and a link —
    each with a real snapshot on disk, plus an ingested fact and the pinned
    manual correction that supersedes it.
    """
    upload = BusinessSource(
        project_guid=project_guid,
        project_key=key,
        owner_id=None,
        kind="upload",
        title="eligibility-rules.md",
        url=None,
        connection_id=None,
        status="synced",
        content_hash="abc123",
        byte_size=42,
        doc_count=1,
        secrets={"token": "admin-pat"},
    )
    link = BusinessSource(
        project_guid=project_guid,
        project_key=key,
        owner_id=None,
        kind="url",
        title="Claims adjudication",
        url="https://wiki.surency.test/claims",
        status="synced",
        content_hash="def456",
        byte_size=12,
        doc_count=1,
    )
    db_session.add_all([upload, link])
    db_session.commit()

    for source, body in ((upload, "# Eligibility"), (link, "# Claims")):
        root = scoped_business_dir(None) / slug(key) / str(source.id)
        (root / "raw").mkdir(parents=True, exist_ok=True)
        (root / "normalized").mkdir(parents=True, exist_ok=True)
        (root / "raw" / "doc").write_text(body, encoding="utf-8")
        (root / "normalized" / "doc.md").write_text(body, encoding="utf-8")
        source.raw_path = f"{slug(key)}/{source.id}/raw"
        source.normalized_path = f"{slug(key)}/{source.id}/normalized"

    ingested = BusinessFact(
        project_guid=project_guid,
        owner_id=None,
        source_id=upload.id,
        category="rule",
        term="Suspended member",
        statement="A suspended member may not file a new claim.",
        origin="ingested",
        rank_text="suspended member claim",
    )
    db_session.add(ingested)
    db_session.commit()
    correction = BusinessFact(
        project_guid=project_guid,
        owner_id=None,
        source_id=None,
        category="rule",
        term="Suspended member",
        statement="A suspended member may not file a new claim, but may appeal.",
        origin="manual",
        pinned=True,
        superseded_by=ingested.id,
    )
    db_session.add(correction)
    db_session.commit()
    return {"upload": upload, "link": link, "ingested": ingested, "correction": correction}


def _shared_guid(db_session, key: str = PROJECT_KEY) -> str:
    """The GUID the shared project's Business Knowledge rows are keyed on."""
    return db_session.query(Project).filter_by(name=key, owner_id=None).one().guid


# ------------------------------------------------------ business knowledge (#831)
def test_clone_carries_business_sources_facts_and_artifacts(client, db_session, monkeypatch):
    """Sources, facts and their snapshots arrive under the CLONING user's scope."""
    _auth_on(monkeypatch)
    _seed_shared_project(db_session)
    guid = _shared_guid(db_session)
    seeded = _seed_shared_business(db_session, guid)
    user = _make_user(db_session, "bk-member@example.com")

    resp = client.post(f"/shared/projects/{PROJECT_KEY}/clone", headers=_auth_headers(user))
    assert resp.status_code == 200
    body = resp.json()
    assert sorted(body["businessSourcesCloned"]) == ["Claims adjudication", "eligibility-rules.md"]
    assert body["businessFactsCloned"] == 2
    assert "business" in body["artifactsCopied"]

    cloned_sources = (
        db_session.query(BusinessSource)
        .filter_by(owner_id=user.id)
        .order_by(BusinessSource.id)
        .all()
    )
    assert [s.title for s in cloned_sources] == ["eligibility-rules.md", "Claims adjudication"]
    # Same project identity, different owner — ADR 0009 §3/§4 verbatim.
    assert {s.project_guid for s in cloned_sources} == {guid}
    # New rows, not the admin's.
    assert not {s.id for s in cloned_sources} & {seeded["upload"].id, seeded["link"].id}

    cloned_upload = next(s for s in cloned_sources if s.kind == "upload")
    # The admin's per-source credential does not travel.
    assert cloned_upload.secrets == {}
    assert cloned_upload.connection_id is None
    # ... but the snapshot does, so the clone needs no re-fetch.
    assert cloned_upload.content_hash == "abc123"
    assert cloned_upload.status == "synced"

    # Artifacts landed under the caller's own scope, at the CLONE's source id.
    for source in cloned_sources:
        dest = scoped_business_dir(user.id) / slug(PROJECT_KEY) / str(source.id)
        assert (dest / "normalized" / "doc.md").read_text(encoding="utf-8").startswith("#")
        assert (dest / "raw" / "doc").exists()
        assert source.raw_path == f"{slug(PROJECT_KEY)}/{source.id}/raw"
        assert source.normalized_path == f"{slug(PROJECT_KEY)}/{source.id}/normalized"
        # The stored path resolves inside the caller's scope, and nowhere else.
        resolved = (scoped_business_dir(user.id) / source.normalized_path).resolve()
        resolved.relative_to(scoped_business_dir(user.id).resolve())

    cloned_facts = (
        db_session.query(BusinessFact).filter_by(owner_id=user.id).order_by(BusinessFact.id).all()
    )
    assert len(cloned_facts) == 2
    ingested, correction = cloned_facts
    assert ingested.statement.endswith("file a new claim.")
    assert correction.pinned is True
    # The fact's source link was remapped onto the CLONE's source, and the
    # correction still supersedes the clone's ingested row.
    assert ingested.source_id == cloned_upload.id
    assert correction.superseded_by == ingested.id

    # Negative: nothing of the admin's is reachable from the clone.
    shared_source_ids = {seeded["upload"].id, seeded["link"].id}
    shared_fact_ids = {seeded["ingested"].id, seeded["correction"].id}
    assert not {f.source_id for f in cloned_facts} & shared_source_ids
    assert not {f.superseded_by for f in cloned_facts if f.superseded_by} & shared_fact_ids
    assert not (
        scoped_business_dir(user.id) / slug(PROJECT_KEY) / str(seeded["upload"].id)
    ).exists()


def test_cloned_business_knowledge_is_independent_of_the_original(client, db_session, monkeypatch):
    """Mutating the clone — rows and files — must not touch the admin's copy."""
    _auth_on(monkeypatch)
    _seed_shared_project(db_session)
    guid = _shared_guid(db_session)
    seeded = _seed_shared_business(db_session, guid)
    shared_upload_id = seeded["upload"].id
    user = _make_user(db_session, "bk-independent@example.com")

    resp = client.post(f"/shared/projects/{PROJECT_KEY}/clone", headers=_auth_headers(user))
    assert resp.status_code == 200

    cloned_upload = (
        db_session.query(BusinessSource).filter_by(owner_id=user.id, kind="upload").one()
    )
    cloned_fact = db_session.query(BusinessFact).filter_by(owner_id=user.id, origin="ingested").one()
    cloned_upload.title = "renamed-by-member.md"
    cloned_upload.excluded = True
    cloned_fact.statement = "Rewritten by the member."
    db_session.commit()
    (
        scoped_business_dir(user.id)
        / slug(PROJECT_KEY)
        / str(cloned_upload.id)
        / "normalized"
        / "doc.md"
    ).write_text("# Member edit", encoding="utf-8")

    db_session.refresh(seeded["upload"])
    db_session.refresh(seeded["ingested"])
    assert seeded["upload"].title == "eligibility-rules.md"
    assert seeded["upload"].excluded is False
    assert seeded["ingested"].statement.endswith("file a new claim.")
    shared_doc = (
        scoped_business_dir(None)
        / slug(PROJECT_KEY)
        / str(shared_upload_id)
        / "normalized"
        / "doc.md"
    )
    assert shared_doc.read_text(encoding="utf-8") == "# Eligibility"
    # And the admin's rows are still the only ones in the shared namespace.
    assert db_session.query(BusinessSource).filter(BusinessSource.owner_id.is_(None)).count() == 2


def test_clone_does_not_duplicate_a_source_the_member_already_has(client, db_session, monkeypatch):
    """An upload has no URL, so the unique constraint cannot de-duplicate it (#815) —
    the clone consults ``find_duplicate`` instead."""
    _auth_on(monkeypatch)
    _seed_shared_project(db_session)
    guid = _shared_guid(db_session)
    _seed_shared_business(db_session, guid)
    user = _make_user(db_session, "bk-dupe@example.com")
    db_session.add(
        BusinessSource(
            project_guid=guid,
            project_key=PROJECT_KEY,
            owner_id=user.id,
            kind="upload",
            title="Eligibility-Rules.MD",  # same document, different casing
            url=None,
            status="synced",
        )
    )
    db_session.commit()

    resp = client.post(f"/shared/projects/{PROJECT_KEY}/clone", headers=_auth_headers(user))
    assert resp.status_code == 200
    assert resp.json()["businessSourcesCloned"] == ["Claims adjudication"]

    uploads = db_session.query(BusinessSource).filter_by(owner_id=user.id, kind="upload").all()
    assert [u.title for u in uploads] == ["Eligibility-Rules.MD"]
    # The link still cloned, and the member's pre-existing row was left alone.
    assert db_session.query(BusinessSource).filter_by(owner_id=user.id, kind="url").count() == 1
    # The fact whose source was skipped keeps no link to the admin's row.
    ingested = db_session.query(BusinessFact).filter_by(owner_id=user.id, origin="ingested").one()
    assert ingested.source_id is None


def test_clone_copies_the_business_brief_onto_the_members_config(client, db_session, monkeypatch):
    """``ProjectConfig.business_brief`` (#824) follows the clone, and so does the GUID."""
    _auth_on(monkeypatch)
    seeded = _seed_shared_project(db_session)
    guid = _shared_guid(db_session)
    seeded["config"].project_guid = guid
    seeded["config"].business_brief = {
        "brief": "Members may appeal.",
        "hash": "h1",
        "status": "built",
    }
    db_session.commit()
    user = _make_user(db_session, "bk-brief@example.com")

    resp = client.post(f"/shared/projects/{PROJECT_KEY}/clone", headers=_auth_headers(user))
    assert resp.status_code == 200

    cloned = db_session.query(ProjectConfig).filter_by(key=PROJECT_KEY, owner_id=user.id).one()
    assert cloned.business_brief["brief"] == "Members may appeal."
    assert cloned.project_guid == guid
    # A separate dict, not the admin's — editing one must not edit the other.
    cloned.business_brief = {**cloned.business_brief, "brief": "Member edit."}
    db_session.commit()
    db_session.refresh(seeded["config"])
    assert seeded["config"].business_brief["brief"] == "Members may appeal."


def test_clone_without_business_knowledge_reports_nothing(client, db_session, monkeypatch):
    """A project with no Business Knowledge clones exactly as before (#831 is additive)."""
    _auth_on(monkeypatch)
    _seed_shared_project(db_session)
    user = _make_user(db_session, "bk-none@example.com")

    body = client.post(f"/shared/projects/{PROJECT_KEY}/clone", headers=_auth_headers(user)).json()
    assert body["businessSourcesCloned"] == []
    assert body["businessFactsCloned"] == 0
    assert "business" not in body["artifactsCopied"]


# --------------------------------------------------------------------- clone
def test_clone_copies_rows_and_files_and_decrypts_secrets(client, db_session, monkeypatch):
    _auth_on(monkeypatch)
    _seed_shared_project(db_session)
    user = _make_user(db_session, "member@example.com")
    headers = _auth_headers(user)

    resp = client.post(f"/shared/projects/{PROJECT_KEY}/clone", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["projectKey"] == PROJECT_KEY
    assert body["projectsCloned"] == 1
    assert body["configCloned"] is True
    assert body["knowledgeCloned"] == [PROJECT_KEY]
    assert set(body["artifactsCopied"]) == {"knowledge"}

    # Rows are owned by the caller now.
    project = db_session.query(Project).filter_by(name=PROJECT_KEY, owner_id=user.id).one()
    assert project.provider_kind == "ado"

    config = db_session.query(ProjectConfig).filter_by(key=PROJECT_KEY, owner_id=user.id).one()
    assert config.base_url == "https://shared.surency.test"
    assert crypto.decrypt(config.test_accounts[0]["password"]) == "s3cret!"
    # The shared source row is untouched.
    shared_config = db_session.query(ProjectConfig).filter_by(key=PROJECT_KEY, owner_id=None).one()
    assert crypto.decrypt(shared_config.test_accounts[0]["password"]) == "s3cret!"

    knowledge = db_session.query(ProjectKnowledge).filter_by(key=PROJECT_KEY, owner_id=user.id).one()
    assert knowledge.confidence == 90
    assert f"users/{user.id}" in knowledge.doc_path.replace("\\", "/")

    # Files landed under the caller's own scope.
    dest_dir = scoped_knowledge_dir(user.id) / slug(PROJECT_KEY)
    assert (dest_dir / "knowledge.json").exists()
    assert (dest_dir / "knowledge.md").exists()
    # The shared source files are untouched.
    assert (scoped_knowledge_dir(None) / slug(PROJECT_KEY) / "knowledge.json").exists()


def test_clone_second_time_conflicts_409(client, db_session, monkeypatch):
    _auth_on(monkeypatch)
    _seed_shared_project(db_session)
    user = _make_user(db_session, "twice@example.com")
    headers = _auth_headers(user)

    first = client.post(f"/shared/projects/{PROJECT_KEY}/clone", headers=headers)
    assert first.status_code == 200

    second = client.post(f"/shared/projects/{PROJECT_KEY}/clone", headers=headers)
    assert second.status_code == 409


def test_clone_unbuilt_shared_project_422(client, db_session, monkeypatch):
    """A shared project with no indexed knowledge has nothing to reuse — block it."""
    _auth_on(monkeypatch)
    key = "Unbuilt Project"
    db_session.add(
        Project(provider_kind="ado", external_id="shared-nb", name=key, active=True, owner_id=None)
    )
    db_session.add(ProjectConfig(key=key, name=key, base_url="https://x.test", owner_id=None))
    db_session.add(
        ProjectKnowledge(key=key, project_key=key, name=key, status="not_indexed", owner_id=None)
    )
    db_session.commit()

    user = _make_user(db_session, "unbuilt@example.com")
    resp = client.post(f"/shared/projects/{key}/clone", headers=_auth_headers(user))
    assert resp.status_code == 422


def test_clone_missing_shared_project_404(client, db_session, monkeypatch):
    _auth_on(monkeypatch)
    user = _make_user(db_session, "ghost@example.com")
    headers = _auth_headers(user)

    resp = client.post("/shared/projects/Ghost Project/clone", headers=headers)
    assert resp.status_code == 404


def test_clone_does_not_expose_admins_connection_bindings(client, db_session, monkeypatch):
    """Provider-connection FKs are not copied — the destination owner can't see them."""
    _auth_on(monkeypatch)
    seeded = _seed_shared_project(db_session)
    seeded["config"].work_item_connection_id = 999
    seeded["config"].repository_connection_id = 998
    db_session.commit()

    user = _make_user(db_session, "conn@example.com")
    headers = _auth_headers(user)
    resp = client.post(f"/shared/projects/{PROJECT_KEY}/clone", headers=headers)
    assert resp.status_code == 200

    cloned = db_session.query(ProjectConfig).filter_by(key=PROJECT_KEY, owner_id=user.id).one()
    assert cloned.work_item_connection_id is None
    assert cloned.repository_connection_id is None


# --------------------------------------------------------- admin shared management
def test_non_admin_cannot_write_shared_namespace(client, db_session, monkeypatch):
    _auth_on(monkeypatch)
    member = _make_user(db_session, "notadmin@example.com", role="member")
    headers = _auth_headers(member)

    resp = client.post(f"/shared/projects/{PROJECT_KEY}", json={"baseUrl": "https://x.test"}, headers=headers)
    assert resp.status_code == 403

    resp = client.post(f"/shared/projects/{PROJECT_KEY}/knowledge/build", json={}, headers=headers)
    assert resp.status_code == 403


def test_admin_can_create_shared_project_shell_and_build_knowledge(client, db_session, monkeypatch):
    _auth_on(monkeypatch)
    admin = _make_user(db_session, "admin@example.com", role="admin")
    headers = _auth_headers(admin)

    resp = client.post(
        f"/shared/projects/{PROJECT_KEY}",
        json={
            "name": PROJECT_KEY,
            "providerKind": "ado",
            "externalId": "shared-2",
            "baseUrl": "https://shared.surency.test",
        },
        headers=headers,
    )
    assert resp.status_code == 201
    assert resp.json()["baseUrl"] == "https://shared.surency.test"

    project = db_session.query(Project).filter_by(name=PROJECT_KEY, owner_id=None).one()
    assert project.provider_kind == "ado"
    config = db_session.query(ProjectConfig).filter_by(key=PROJECT_KEY, owner_id=None).one()
    assert config.base_url == "https://shared.surency.test"

    from tests.test_knowledge import _wait_idle

    monkeypatch.setattr(knowledge_service, "run_json", lambda *a, **k: {"confidence": 77, "stack": ["React"]})
    from app.services import repo_service

    monkeypatch.setattr(repo_service, "resolve_repo_path", lambda *a, **k: None)

    build_resp = client.post(
        f"/shared/projects/{PROJECT_KEY}/knowledge/build",
        json={"name": PROJECT_KEY, "provider": "ado"},
        headers=headers,
    )
    assert build_resp.status_code == 200
    _wait_idle(PROJECT_KEY)

    knowledge = db_session.query(ProjectKnowledge).filter_by(key=PROJECT_KEY, owner_id=None).one()
    assert knowledge.status == "indexed"
    assert knowledge.confidence == 77


def test_admin_configures_repo_and_config_round_trips(client, db_session, monkeypatch):
    """Admin can attach a repo + repository connection; catalog + config GET reflect it."""
    _auth_on(monkeypatch)
    admin = _make_user(db_session, "repoadmin@example.com", role="admin")
    headers = _auth_headers(admin)
    key = "Repo Project"

    resp = client.post(
        f"/shared/projects/{key}",
        json={
            "name": key,
            "baseUrl": "https://repo.test",
            "repositoryConnectionId": 42,
            "repos": [
                {"name": "web", "repoUrl": "https://git.test/web.git", "defaultBranch": "main"}
            ],
        },
        headers=headers,
    )
    assert resp.status_code == 201

    # The shared config GET returns the repo + binding (passwords masked shape).
    cfg = client.get(f"/shared/projects/{key}/config", headers=headers).json()
    assert cfg["baseUrl"] == "https://repo.test"
    assert cfg["repositoryConnectionId"] == 42
    assert [r["name"] for r in cfg["repos"]] == ["web"]

    # The catalog also surfaces the repo so per-repo build buttons can render.
    entry = next(e for e in client.get("/shared/projects", headers=headers).json() if e["key"] == key)
    assert [r["name"] for r in entry["repos"]] == ["web"]
    assert entry["repositoryConnectionId"] == 42


def test_shared_auth_routes_are_admin_only(client, db_session, monkeypatch):
    _auth_on(monkeypatch)
    member = _make_user(db_session, "authmember@example.com", role="member")
    headers = _auth_headers(member)
    assert client.get(f"/shared/projects/{PROJECT_KEY}/config", headers=headers).status_code == 403
    assert client.get(f"/shared/projects/{PROJECT_KEY}/auth", headers=headers).status_code == 403


# ------------------------------------------------------------------------ catalog
def test_shared_catalog_lists_project_and_reflects_clone_state(client, db_session, monkeypatch):
    _auth_on(monkeypatch)
    _seed_shared_project(db_session)
    user = _make_user(db_session, "catalog@example.com")
    headers = _auth_headers(user)

    before = client.get("/shared/projects", headers=headers)
    assert before.status_code == 200
    entry = next(e for e in before.json() if e["key"] == PROJECT_KEY)
    assert entry["hasConfig"] is True
    assert entry["knowledge"][0]["confidence"] == 90
    assert entry["alreadyCloned"] is False

    client.post(f"/shared/projects/{PROJECT_KEY}/clone", headers=headers)

    after = client.get("/shared/projects", headers=headers)
    entry_after = next(e for e in after.json() if e["key"] == PROJECT_KEY)
    assert entry_after["alreadyCloned"] is True
