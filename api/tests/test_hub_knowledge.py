"""Mirroring a hub project's KNOWLEDGE into the caller's own row (#598).

What was reported: EmeHub showed project *surency* as ``Knowledge: Indexed`` — v1,
"Indexed from surency-admin-hub · main", all five sections populated — while
Q-Agent showed the same project with no knowledge at all. Config was mirrored (so
the hub's **repos** appeared) and knowledge was not, so the project looked
connected and empty. The recurring EmeHub failure shape: the call succeeds,
nothing errors, and the data is quietly incomplete.

Four things carry the real risk here, and each is pinned with a control that
would fail if the behaviour regressed:

* **The fixtures speak the HUB's dialect** — camelCase field names
  (``lastIndexed``, ``needsRefresh``, ``docPath``) and ``provider:
  "azure_devops"``, not our ``ado``. #507 joined on the untranslated value and
  matched zero rows while sixteen tests passed, because the fixture used *our*
  spelling on both sides of the boundary. A fixture that cannot fail is not
  testing the boundary.
* **Mirror, don't read through.** The acceptance test that matters is not "the UI
  shows a badge" but "generation renders the mirrored routes and selectors" —
  asserted through ``project_config_service.context_for_ticket`` ->
  ``prompts.render_project_context``, the path spec generation actually takes.
* **A newer local build is never destroyed**, and a hub ``not_indexed`` row never
  overwrites a local indexed one — both asserted *in both directions*, so the
  no-clobber assertions cannot pass by the mirror simply never firing.
* **``doc_path`` is never copied.** The hub documents it as the agent-host
  directory, "opaque to the hub … which the hub stores and never resolves", so a
  mirrored value points at a path that may not exist here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from app.models.knowledge import ProjectKnowledge, compose_key
from app.models.project import Project
from app.models.ticket import Ticket
from app.models.user import User
from app.services import hub_workspace, project_config_service, prompts

HUB = "https://hub.example.test/api"
REPO = "surency-admin-hub"
REPO_KNOWLEDGE_URL = f"{HUB}/projects/surency/repos/{REPO}/knowledge"
PROJECT_KNOWLEDGE_URL = f"{HUB}/projects/surency/knowledge"
CONFIG_URL = f"{HUB}/projects/surency/config"

HUB_INDEXED_AT = datetime(2026, 8, 19, 9, 30, tzinfo=timezone.utc)


@pytest.fixture
def hub_on(monkeypatch, workspace_dir):
    """Flags on, applied AFTER ``workspace_dir`` — it rebuilds settings in place."""
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "hub_sso_enabled", True)
    monkeypatch.setattr(config_module.settings, "hub_data_enabled", True)
    monkeypatch.setattr(config_module.settings, "hub_base_url", HUB)
    monkeypatch.setattr(config_module.settings, "hub_internal_base_url", "")
    return config_module.settings


def _user(db, email="duna@example.com") -> User:
    user = User(email=email, password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _hub_project(db, user, *, name="Surency", key="surency", hub_id="3") -> Project:
    project = Project(
        provider_kind="ado", external_id=key, name=name, owner_id=user.id, hub_project_id=hub_id
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


# --------------------------------------------------------- the hub's own dialect
def _blob(**over) -> dict:
    body = {
        "branch": "main",
        "stack": ["React", "TypeScript", "Playwright"],
        "architecture": "Admin hub SPA over a .NET API",
        "domain": "Benefits administration: members, plans, claims",
        "locator": "prefer data-testid, then role",
        "base_url": "https://admin.surency.test",
        "routes": [
            {"path": "/members", "description": "Member search"},
            {"path": "/claims/new", "description": "Submit a claim"},
        ],
        "selectors": [
            {"screen": "Members", "element": "search box", "selector": "[data-testid=member-search]"},
            {"screen": "Claims", "element": "submit", "selector": "[data-testid=claim-submit]"},
        ],
        "auth": {"login_flow": "form login", "login_url": "/login"},
        "business_entities": ["Member", "Plan", "Claim"],
        "page_object_names": ["MembersPage", "ClaimsPage"],
        "fixture_names": ["adminSession"],
        "utilities": ["seedMember"],
    }
    body.update(over)
    return body


def _payload(**over) -> dict:
    """A ``KnowledgeOut`` exactly as EmeHub serialises it.

    camelCase (its ``ApiModel`` sets ``alias_generator=to_camel``) and
    ``provider: "azure_devops"`` — the hub's vocabulary, not ours.
    """
    body = {
        "id": 11,
        "key": f"surency::{REPO}",
        "projectKey": "surency",
        "name": "Surency",
        "provider": "azure_devops",
        "repo": REPO,
        "framework": "Playwright",
        "status": "indexed",
        "confidence": 82,
        "version": "v1",
        "needsRefresh": False,
        "lastIndexed": HUB_INDEXED_AT.isoformat(),
        "knowledge": _blob(),
        # The agent-HOST directory. Opaque to the hub, and meaningless here.
        "docPath": "/home/hubagent/workspace/knowledge/surency/surency-admin-hub",
        "lastError": "",
        "shared": False,
        "buildStage": "",
        "buildStep": 0,
        "buildTotalSteps": 5,
        "buildMessage": "",
        "buildStartedAt": None,
        "buildOrphaned": False,
    }
    body.update(over)
    return body


def _config_payload() -> dict:
    return {
        "key": "surency",
        "name": "Surency",
        "baseUrl": "https://admin.surency.test",
        "environments": [],
        "repos": [
            {
                "name": REPO,
                "repo_url": f"https://dev.azure.com/DDKS/Surency/_git/{REPO}",
                "default_branch": "main",
                "local_repo_path": "",
                "default": True,
            }
        ],
        "testAccounts": [],
        "manualAuth": False,
        "workItemConnectionId": None,
        "repositoryConnectionId": None,
        "extra": {},
    }


def _mock_repo(payload=None, url=REPO_KNOWLEDGE_URL):
    return respx.get(url).mock(
        return_value=httpx.Response(200, json=payload if payload is not None else _payload())
    )


def _local_row(db, user, *, repo=REPO, **over) -> ProjectKnowledge:
    row = ProjectKnowledge(
        key=compose_key("Surency", repo),
        project_key="Surency",
        name="Surency",
        provider="ado",
        repo=repo,
        owner_id=user.id,
        **over,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ---------------------------------------------------------------- the fix
@respx.mock
def test_hub_indexed_knowledge_arrives(hub_on, db_session):
    user = _user(db_session)
    _hub_project(db_session, user)
    _mock_repo()

    assert hub_workspace.ensure_knowledge(db_session, user, "Surency", REPO, "tok") is True

    row = db_session.query(ProjectKnowledge).filter_by(key=compose_key("Surency", REPO)).one()
    assert row.status == "indexed"
    assert row.confidence == 82
    assert row.version == "v1"
    assert row.last_indexed == HUB_INDEXED_AT
    assert row.owner_id == user.id
    # All five sections, not just a status badge.
    assert row.knowledge["architecture"].startswith("Admin hub SPA")
    assert row.knowledge["domain"].startswith("Benefits administration")
    assert [r["path"] for r in row.knowledge["routes"]] == ["/members", "/claims/new"]
    assert len(row.knowledge["selectors"]) == 2
    assert row.knowledge["stack"] == ["React", "TypeScript", "Playwright"]


@respx.mock
def test_the_row_is_stamped_with_the_project_guid(hub_on, db_session):
    """The SPA addresses projects by GUID now (#585/#587)."""
    user = _user(db_session)
    project = _hub_project(db_session, user)
    _mock_repo()

    hub_workspace.ensure_knowledge(db_session, user, "Surency", REPO, "tok")

    row = db_session.query(ProjectKnowledge).filter_by(key=compose_key("Surency", REPO)).one()
    assert row.project_guid == project.guid
    assert row.project_key == "Surency"


@respx.mock
def test_the_provider_vocabulary_is_translated(hub_on, db_session):
    """The hub says ``azure_devops``; we say ``ado`` (#507).

    The fixture deliberately sends the hub's spelling, so a mirror that copied the
    value through would store ``azure_devops`` and this assertion would fail.
    """
    user = _user(db_session)
    _hub_project(db_session, user)
    _mock_repo()

    hub_workspace.ensure_knowledge(db_session, user, "Surency", REPO, "tok")

    row = db_session.query(ProjectKnowledge).filter_by(key=compose_key("Surency", REPO)).one()
    assert row.provider == "ado"


@respx.mock
def test_camelcase_hub_fields_are_read(hub_on, db_session):
    """`needsRefresh`/`lastError` are camelCase on the wire; snake_case would read None."""
    user = _user(db_session)
    _hub_project(db_session, user)
    _mock_repo(_payload(needsRefresh=True, lastError="clone timed out", status="stale"))

    hub_workspace.ensure_knowledge(db_session, user, "Surency", REPO, "tok")

    row = db_session.query(ProjectKnowledge).filter_by(key=compose_key("Surency", REPO)).one()
    assert row.needs_refresh is True
    assert row.last_error == "clone timed out"
    assert row.status == "stale"


@respx.mock
def test_mirroring_is_idempotent(hub_on, db_session):
    user = _user(db_session)
    _hub_project(db_session, user)
    _mock_repo()

    hub_workspace.ensure_knowledge(db_session, user, "Surency", REPO, "tok")
    hub_workspace.ensure_knowledge(db_session, user, "Surency", REPO, "tok")

    rows = db_session.query(ProjectKnowledge).filter_by(key=compose_key("Surency", REPO)).all()
    assert len(rows) == 1
    assert rows[0].confidence == 82
    assert rows[0].last_indexed == HUB_INDEXED_AT


@respx.mock
def test_the_project_level_row_is_mirrored_too(hub_on, db_session):
    user = _user(db_session)
    _hub_project(db_session, user)
    route = respx.get(PROJECT_KNOWLEDGE_URL).mock(
        return_value=httpx.Response(200, json=_payload(repo="", key="surency"))
    )

    assert hub_workspace.ensure_knowledge(db_session, user, "Surency", "", "tok") is True

    assert route.called
    row = db_session.query(ProjectKnowledge).filter_by(key="Surency").one()
    assert row.repo == ""
    assert row.status == "indexed"


@respx.mock
def test_a_project_level_fallback_lands_in_the_requested_repo_slot(hub_on, db_session):
    """The hub falls back to its project-level row (``repo: ""``) for a repo with none.

    That payload must still land in the per-repo slot every downstream lookup
    addresses — otherwise the repos listing keeps reading "not indexed".
    """
    user = _user(db_session)
    _hub_project(db_session, user)
    _mock_repo(_payload(repo="", key="surency"))

    hub_workspace.ensure_knowledge(db_session, user, "Surency", REPO, "tok")

    row = db_session.query(ProjectKnowledge).filter_by(key=compose_key("Surency", REPO)).one()
    assert row.repo == REPO


# ------------------------------------------------------------------- doc_path
@respx.mock
def test_the_hubs_doc_path_is_never_copied(hub_on, db_session):
    """It names a directory on the HUB's agent host; it may not exist here."""
    from app.services.workspace_scope import scoped_knowledge_dir

    user = _user(db_session)
    _hub_project(db_session, user)
    _mock_repo()

    hub_workspace.ensure_knowledge(db_session, user, "Surency", REPO, "tok")

    row = db_session.query(ProjectKnowledge).filter_by(key=compose_key("Surency", REPO)).one()
    assert "hubagent" not in row.doc_path
    assert row.doc_path != _payload()["docPath"]
    # Re-rendered locally, under THIS owner's scope, with real artifacts on disk.
    assert str(scoped_knowledge_dir(user.id)) in row.doc_path
    from pathlib import Path

    assert (Path(row.doc_path) / "knowledge.md").exists()
    assert "Member search" in (Path(row.doc_path) / "knowledge.md").read_text(encoding="utf-8")


@respx.mock
def test_doc_path_stays_empty_when_the_local_render_fails(hub_on, db_session, monkeypatch):
    """The mutant this catches: falling back to the hub's ``docPath``.

    Without it, "doc_path is not the hub's" passes for the wrong reason — the local
    re-render happens to overwrite the copied value. Make the re-render fail and the
    only honest answer is an empty path.
    """
    from app.services import knowledge_service

    user = _user(db_session)
    _hub_project(db_session, user)
    _mock_repo()

    def _boom(*_a, **_k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(knowledge_service, "write_knowledge_files", _boom)

    assert hub_workspace.ensure_knowledge(db_session, user, "Surency", REPO, "tok") is True

    row = db_session.query(ProjectKnowledge).filter_by(key=compose_key("Surency", REPO)).one()
    assert row.doc_path == ""
    # The mirror itself still succeeded — artifacts are a convenience, not the row.
    assert row.status == "indexed"
    assert row.knowledge["domain"].startswith("Benefits administration")


# ------------------------------------------------------- the no-clobber rule
@respx.mock
def test_a_newer_local_build_is_not_overwritten(hub_on, db_session):
    user = _user(db_session)
    _hub_project(db_session, user)
    local = _local_row(
        db_session,
        user,
        status="indexed",
        confidence=95,
        last_indexed=HUB_INDEXED_AT + timedelta(hours=2),
        knowledge={"domain": "LOCAL-DOMAIN"},
    )
    _mock_repo()

    assert hub_workspace.ensure_knowledge(db_session, user, "Surency", REPO, "tok") is False

    db_session.refresh(local)
    assert local.confidence == 95
    assert local.knowledge["domain"] == "LOCAL-DOMAIN"


@respx.mock
def test_an_older_local_build_IS_replaced(hub_on, db_session):
    """The negative control for the test above.

    Without it, "the local row survived" would also pass if the mirror never fired
    at all — which is precisely the bug being fixed.
    """
    user = _user(db_session)
    _hub_project(db_session, user)
    local = _local_row(
        db_session,
        user,
        status="indexed",
        confidence=10,
        last_indexed=HUB_INDEXED_AT - timedelta(hours=2),
        knowledge={"domain": "OLD-LOCAL"},
    )
    _mock_repo()

    assert hub_workspace.ensure_knowledge(db_session, user, "Surency", REPO, "tok") is True

    db_session.refresh(local)
    assert local.confidence == 82
    assert local.knowledge["domain"].startswith("Benefits administration")


@respx.mock
def test_a_hub_not_indexed_row_never_overwrites_a_local_indexed_one(hub_on, db_session):
    user = _user(db_session)
    _hub_project(db_session, user)
    local = _local_row(
        db_session,
        user,
        status="indexed",
        confidence=71,
        last_indexed=HUB_INDEXED_AT - timedelta(days=30),
        knowledge={"domain": "LOCAL-DOMAIN"},
    )
    # A hub row that has never been built: no timestamp, empty blob — and old
    # enough that a timestamp comparison alone would let it win.
    _mock_repo(_payload(status="not_indexed", confidence=0, lastIndexed=None, knowledge={}))

    assert hub_workspace.ensure_knowledge(db_session, user, "Surency", REPO, "tok") is False

    db_session.refresh(local)
    assert local.status == "indexed"
    assert local.confidence == 71
    assert local.knowledge["domain"] == "LOCAL-DOMAIN"


@respx.mock
def test_an_equal_timestamp_is_not_a_new_build(hub_on, db_session):
    user = _user(db_session)
    _hub_project(db_session, user)
    local = _local_row(
        db_session, user, status="indexed", confidence=64, last_indexed=HUB_INDEXED_AT
    )
    _mock_repo()

    assert hub_workspace.ensure_knowledge(db_session, user, "Surency", REPO, "tok") is False

    db_session.refresh(local)
    assert local.confidence == 64


@respx.mock
def test_a_build_in_flight_locally_is_not_clobbered(hub_on, db_session):
    """`indexing` means a local build is running; its row is not ours to replace."""
    user = _user(db_session)
    _hub_project(db_session, user)
    local = _local_row(db_session, user, status="indexing", last_indexed=None)
    _mock_repo(_payload(status="not_indexed", lastIndexed=None, knowledge={}))

    assert hub_workspace.ensure_knowledge(db_session, user, "Surency", REPO, "tok") is False

    db_session.refresh(local)
    assert local.status == "indexing"


# --------------------------------------------------------------- guards
@respx.mock
def test_a_hub_404_is_not_an_error_and_leaves_local_state_alone(hub_on, db_session):
    user = _user(db_session)
    _hub_project(db_session, user)
    local = _local_row(db_session, user, status="indexed", knowledge={"domain": "MINE"})
    respx.get(REPO_KNOWLEDGE_URL).mock(
        return_value=httpx.Response(404, json={"detail": "No knowledge base for repo"})
    )

    assert hub_workspace.ensure_knowledge(db_session, user, "Surency", REPO, "tok") is False

    db_session.refresh(local)
    assert local.status == "indexed"
    assert local.knowledge["domain"] == "MINE"


@respx.mock
def test_a_404_creates_nothing_when_there_is_no_local_row(hub_on, db_session):
    user = _user(db_session)
    _hub_project(db_session, user)
    respx.get(REPO_KNOWLEDGE_URL).mock(return_value=httpx.Response(404, json={"detail": "nope"}))

    assert hub_workspace.ensure_knowledge(db_session, user, "Surency", REPO, "tok") is False
    assert db_session.query(ProjectKnowledge).count() == 0


@respx.mock
def test_a_hub_outage_leaves_local_knowledge_alone(hub_on, db_session):
    user = _user(db_session)
    _hub_project(db_session, user)
    local = _local_row(db_session, user, status="indexed", knowledge={"domain": "MINE"})
    respx.get(REPO_KNOWLEDGE_URL).mock(side_effect=httpx.ConnectError("refused"))

    assert hub_workspace.ensure_knowledge(db_session, user, "Surency", REPO, "tok") is False

    db_session.refresh(local)
    assert local.knowledge["domain"] == "MINE"


def test_flag_off_makes_no_hub_call(db_session, monkeypatch, workspace_dir):
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "hub_data_enabled", False)
    user = _user(db_session)
    _hub_project(db_session, user)

    with respx.mock:
        route = _mock_repo()
        assert hub_workspace.ensure_knowledge(db_session, user, "Surency", REPO, "tok") is False

    assert not route.called
    assert db_session.query(ProjectKnowledge).count() == 0


@respx.mock
def test_no_hub_token_makes_no_hub_call(hub_on, db_session):
    user = _user(db_session)
    _hub_project(db_session, user)
    route = _mock_repo()

    assert hub_workspace.ensure_knowledge(db_session, user, "Surency", REPO, None) is False
    assert not route.called


@respx.mock
def test_a_purely_local_project_is_never_mirrored_into(hub_on, db_session):
    """No ``hub_project_id`` -> not the hub's project -> its knowledge isn't ours."""
    user = _user(db_session)
    db_session.add(
        Project(provider_kind="ado", external_id="local", name="Local Only", owner_id=user.id)
    )
    db_session.commit()
    route = _mock_repo(url=f"{HUB}/projects/local/repos/{REPO}/knowledge")

    assert hub_workspace.ensure_knowledge(db_session, user, "Local Only", REPO, "tok") is False
    assert not route.called


@respx.mock
def test_another_users_row_is_never_touched(hub_on, db_session):
    """Rows are owner-scoped (ADR 0009 / #93)."""
    user = _user(db_session)
    stranger = _user(db_session, "stranger@example.com")
    _hub_project(db_session, user)
    theirs = _local_row(
        db_session, stranger, status="indexed", confidence=99, knowledge={"domain": "THEIRS"}
    )
    _mock_repo()

    hub_workspace.ensure_knowledge(db_session, user, "Surency", REPO, "tok")

    db_session.refresh(theirs)
    assert theirs.confidence == 99
    assert theirs.knowledge["domain"] == "THEIRS"
    mine = (
        db_session.query(ProjectKnowledge)
        .filter_by(key=compose_key("Surency", REPO), owner_id=user.id)
        .one()
    )
    assert mine.confidence == 82


# ------------------------------------------------------------- the tell-tale
@respx.mock
def test_a_payload_that_writes_nothing_warns_once(hub_on, db_session, monkeypatch):
    """"The hub had nothing" and "we failed to understand it" look identical otherwise."""
    warnings: list[str] = []

    class _Recorder:
        def warning(self, msg, *args):
            warnings.append(msg.format(*args) if args else msg)

        def info(self, *_a, **_k):
            pass

    monkeypatch.setattr(hub_workspace, "logger", _Recorder())
    user = _user(db_session)
    _hub_project(db_session, user)
    _local_row(
        db_session, user, status="indexed", last_indexed=HUB_INDEXED_AT + timedelta(hours=1)
    )
    _mock_repo()

    written = hub_workspace.ensure_knowledge_for_repos(db_session, user, "Surency", [REPO], "tok")

    assert written == 0
    assert len([w for w in warnings if "no local row was written" in w]) == 1


@respx.mock
def test_an_indexed_payload_with_an_empty_blob_warns(hub_on, db_session, monkeypatch):
    warnings: list[str] = []

    class _Recorder:
        def warning(self, msg, *args):
            warnings.append(msg.format(*args) if args else msg)

        def info(self, *_a, **_k):
            pass

    monkeypatch.setattr(hub_workspace, "logger", _Recorder())
    user = _user(db_session)
    _hub_project(db_session, user)
    _mock_repo(_payload(knowledge={}))

    assert hub_workspace.ensure_knowledge(db_session, user, "Surency", REPO, "tok") is True
    assert any("empty knowledge blob" in w for w in warnings)


@respx.mock
def test_no_warning_when_the_mirror_worked(hub_on, db_session, monkeypatch):
    warnings: list[str] = []

    class _Recorder:
        def warning(self, msg, *args):
            warnings.append(msg.format(*args) if args else msg)

        def info(self, *_a, **_k):
            pass

    monkeypatch.setattr(hub_workspace, "logger", _Recorder())
    user = _user(db_session)
    _hub_project(db_session, user)
    _mock_repo()

    assert hub_workspace.ensure_knowledge_for_repos(db_session, user, "Surency", [REPO], "tok") == 1
    assert warnings == []


# --------------------------------------------- the endpoints the SPA calls
def _authed(client, db_session, monkeypatch) -> dict[str, str]:
    """Headers for a real session. The suite default ``current_user`` is None, and
    the mirror declines without an identity to own the row — so a test that skipped
    this would assert against an unmirrored workspace and fail for the wrong reason.
    """
    import app.config as config_module
    from app.services import auth_service

    monkeypatch.setattr(config_module.settings, "auth_required", True)
    user = _user(db_session)
    token = auth_service.create_access_token(user, sid=f"sid-{user.id}")
    return user, {"X-Hub-Token": "tok", "Authorization": f"Bearer {token}"}


@respx.mock
def test_the_repos_listing_reports_the_mirrored_knowledge(
    hub_on, client, db_session, monkeypatch
):
    """What the user saw: the hub's repos listed, every one of them "not indexed"."""
    user, headers = _authed(client, db_session, monkeypatch)
    _hub_project(db_session, user)
    respx.get(CONFIG_URL).mock(return_value=httpx.Response(200, json=_config_payload()))
    _mock_repo()

    body = client.get("/projects/Surency/repos", headers=headers).json()

    assert [r["name"] for r in body] == [REPO]
    assert body[0]["status"] == "indexed"
    assert body[0]["confidence"] == 82
    assert body[0]["lastIndexed"] is not None


@respx.mock
def test_the_repo_knowledge_endpoint_serves_the_mirrored_row(
    hub_on, client, db_session, monkeypatch
):
    user, headers = _authed(client, db_session, monkeypatch)
    _hub_project(db_session, user)
    _mock_repo()

    resp = client.get(f"/projects/Surency/repos/{REPO}/knowledge", headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "indexed"
    assert [r["path"] for r in body["knowledge"]["routes"]] == ["/members", "/claims/new"]


@respx.mock
def test_the_project_knowledge_endpoint_serves_the_mirrored_row(
    hub_on, client, db_session, monkeypatch
):
    user, headers = _authed(client, db_session, monkeypatch)
    _hub_project(db_session, user)
    respx.get(PROJECT_KNOWLEDGE_URL).mock(
        return_value=httpx.Response(200, json=_payload(repo="", key="surency"))
    )

    resp = client.get("/projects/Surency/knowledge", headers=headers)

    assert resp.status_code == 200
    assert resp.json()["status"] == "indexed"


@respx.mock
def test_the_repo_knowledge_endpoint_still_404s_when_the_hub_has_nothing(
    hub_on, client, db_session, monkeypatch
):
    user, headers = _authed(client, db_session, monkeypatch)
    _hub_project(db_session, user)
    respx.get(REPO_KNOWLEDGE_URL).mock(return_value=httpx.Response(404, json={"detail": "nope"}))

    resp = client.get(f"/projects/Surency/repos/{REPO}/knowledge", headers=headers)

    assert resp.status_code == 404


# ------------------------------------------------- the point of mirroring
@respx.mock
def test_generation_consumes_the_mirrored_knowledge(hub_on, db_session):
    """The acceptance criterion that cannot be faked with a badge.

    Spec generation grounds itself through
    ``project_config_service.context_for_ticket`` -> ``prompts.render_project_context``,
    off the LOCAL row's ``knowledge`` dict. This asserts the mirrored routes and
    selectors reach that prompt block — a read-through would leave this empty while
    the UI looked correct.
    """
    user = _user(db_session)
    _hub_project(db_session, user)
    respx.get(CONFIG_URL).mock(return_value=httpx.Response(200, json=_config_payload()))
    _mock_repo()

    hub_workspace.ensure_project_config(db_session, user, "Surency", "tok")
    hub_workspace.ensure_knowledge_for_repos(db_session, user, "Surency", [REPO], "tok")

    ticket = Ticket(external_id="SUR-1", provider_kind="ado", title="Submit a claim", owner_id=user.id)
    db_session.add(ticket)
    db_session.commit()

    context = project_config_service.context_for_ticket(db_session, ticket)
    assert context["projectKey"] == "Surency"
    assert context["repo"] == REPO
    assert context["domain"].startswith("Benefits administration")

    rendered = prompts.render_project_context(context, rank_query="submit a claim")
    assert "/claims/new" in rendered
    assert "[data-testid=claim-submit]" in rendered
    assert "Benefits administration" in rendered
    assert "prefer data-testid" in rendered
    assert "MembersPage" in rendered
