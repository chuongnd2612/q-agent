"""The Projects GRID badge for a hub-indexed project (#603).

#598 mirrored the hub's knowledge **blob** when the detail page or the Repos tab
was opened. The grid badge reads ``GET /projects/knowledge``, which was purely
local, so a hub-indexed project still read "not indexed" on first paint and only
became correct after the user had visited it — the same complaint, one screen over.

**The design decision this file pins.** Mirroring the blob on grid load would be
one hub GET per project per repo: 10 projects x 3 repos is 30 hub round trips to
paint a list, on a token that lives 15 minutes. So the badge is served from data
the grid already fetched — EmeHub's ``GET /projects`` returns a ``summary``
carrying ``knowledgeStatus`` / ``knowledgeConfidence`` (its ``ProjectSummaryOut``),
and ``ensure_projects`` already stores that summary in ``Project.meta["hub"]``.

Because "it works, the data is just quietly empty" is this integration's signature
failure, every test here asserts **counts and hub call counts**, not the presence
of a field:

* ``test_first_paint_costs_no_extra_hub_calls`` mocks **only** ``GET /projects``
  (an exact-match route), so a per-repo knowledge fan-out would not merely be
  counted — it would raise as an unmocked request. The call count is asserted at
  the number the projects mirror already spends.
* The fixtures speak the **hub's** dialect: camelCase ``knowledgeStatus`` and
  ``provider: "azure_devops"``. #507 joined on the untranslated value and matched
  zero rows while the tests passed, because they used *our* spelling on both sides.
* ``test_a_hub_project_that_is_not_indexed_gets_no_row`` is the negative control:
  without it, everything else here could pass by emitting a badge unconditionally.
"""

from __future__ import annotations

import re

import httpx
import pytest
import respx

from app.models.knowledge import ProjectKnowledge, compose_key
from app.models.project import Project
from app.models.user import User
from app.services import hub_workspace

HUB = "https://hub.example.test/api"
# Exact match: `/projects/{key}/repos/{repo}/knowledge` must NOT be swallowed by
# this route, so a fan-out fails loudly instead of being absorbed.
PROJECTS_URL = rf"{re.escape(HUB)}/projects$"


@pytest.fixture
def hub_on(monkeypatch, workspace_dir):
    """Flags on, applied AFTER ``workspace_dir`` — it rebuilds settings in place."""
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "hub_sso_enabled", True)
    monkeypatch.setattr(config_module.settings, "hub_data_enabled", True)
    monkeypatch.setattr(config_module.settings, "hub_base_url", HUB)
    monkeypatch.setattr(config_module.settings, "hub_internal_base_url", "")
    return config_module.settings


def _user(db, email="duna@example.com", hub_user_id="1") -> User:
    user = User(email=email, password_hash="x", hub_user_id=hub_user_id)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _hub_summary(**over) -> dict:
    """``ProjectSummaryOut`` exactly as EmeHub serialises it: camelCase, and
    ``azure_devops`` rather than our ``ado``."""
    body = {
        "repo": "surency-admin-hub",
        "repoUrl": "https://dev.azure.com/DDKS/Surency/_git/surency-admin-hub",
        "branch": "main",
        "repoCount": 3,
        "provider": "azure_devops",
        "knowledgeStatus": "indexed",
        "knowledgeConfidence": 82,
        "ticketCount": 12,
    }
    body.update(over)
    return body


def _mirrored_project(db, user, *, name="Surency", key="surency", hub_id="3", summary=...) -> Project:
    """A project as the #514 mirror leaves it: ``hub_project_id`` set and the hub's
    own ``summary`` parked under ``meta["hub"]``."""
    meta = {} if summary is None else {"hub": _hub_summary() if summary is ... else summary}
    project = Project(
        provider_kind="ado",
        external_id=key,
        name=name,
        owner_id=user.id,
        hub_project_id=hub_id,
        meta=meta,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _local_row(db, user, *, repo="surency-admin-hub", project="Surency", **over) -> ProjectKnowledge:
    row = ProjectKnowledge(
        key=compose_key(project, repo),
        project_key=project,
        name=project,
        provider="ado",
        repo=repo,
        owner_id=user.id,
        **over,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _authed(db, monkeypatch, **kw) -> tuple[User, dict[str, str]]:
    import app.config as config_module
    from app.services import auth_service

    monkeypatch.setattr(config_module.settings, "auth_required", True)
    user = _user(db, **kw)
    token = auth_service.create_access_token(user, sid=f"sid-{user.id}")
    return user, {"Authorization": f"Bearer {token}", "X-Hub-Token": "tok"}


# ------------------------------------------------------------------ the fix
def test_the_status_comes_out_of_the_mirrored_summary(hub_on, db_session):
    user = _user(db_session)
    _mirrored_project(db_session, user)

    rows = hub_workspace.hub_knowledge_status_rows(db_session, user, [])

    assert len(rows) == 1
    assert rows[0]["status"] == "indexed"
    assert rows[0]["confidence"] == 82
    # Keyed the way the grid groups its rows: by the project's name.
    assert rows[0]["project_key"] == "Surency"
    assert rows[0]["key"] == compose_key("Surency")
    assert rows[0]["source"] == "hub"


def test_the_provider_vocabulary_is_translated(hub_on, db_session):
    """The hub says ``azure_devops``; we say ``ado`` (#507)."""
    user = _user(db_session)
    _mirrored_project(db_session, user)

    rows = hub_workspace.hub_knowledge_status_rows(db_session, user, [])

    assert rows[0]["provider"] == "ado"


def test_snake_case_from_the_hub_is_read_too(hub_on, db_session):
    user = _user(db_session)
    _mirrored_project(
        db_session,
        user,
        summary={"knowledge_status": "indexed", "knowledge_confidence": 71, "provider": "github"},
    )

    rows = hub_workspace.hub_knowledge_status_rows(db_session, user, [])

    assert (rows[0]["status"], rows[0]["confidence"], rows[0]["provider"]) == ("indexed", 71, "github")


def test_a_non_indexed_hub_status_is_passed_through(hub_on, db_session):
    """``stale`` must not be flattened into ``indexed`` — nor dropped."""
    user = _user(db_session)
    _mirrored_project(db_session, user, summary=_hub_summary(knowledgeStatus="stale"))

    rows = hub_workspace.hub_knowledge_status_rows(db_session, user, [])

    assert [r["status"] for r in rows] == ["stale"]


# --------------------------------------------------- the negative controls
def test_a_hub_project_that_is_not_indexed_gets_no_row(hub_on, db_session):
    """Without this, every assertion above could pass by always emitting a badge."""
    user = _user(db_session)
    _mirrored_project(db_session, user, summary=_hub_summary(knowledgeStatus="not_indexed"))

    assert hub_workspace.hub_knowledge_status_rows(db_session, user, []) == []


def test_a_local_row_is_never_shadowed(hub_on, db_session):
    """A project with any local knowledge row is skipped: the grid keeps showing the
    real per-repo breakdown rather than the hub's collapsed status."""
    user = _user(db_session)
    _mirrored_project(db_session, user)
    local = _local_row(db_session, user, status="indexed", confidence=91)

    assert hub_workspace.hub_knowledge_status_rows(db_session, user, [local]) == []


def test_a_build_in_flight_locally_is_not_shadowed(hub_on, db_session):
    user = _user(db_session)
    _mirrored_project(db_session, user)
    local = _local_row(db_session, user, status="indexing")

    assert hub_workspace.hub_knowledge_status_rows(db_session, user, [local]) == []


def test_a_purely_local_project_contributes_nothing(hub_on, db_session):
    """No ``hub_project_id`` means the row never came from the hub."""
    user = _user(db_session)
    project = Project(
        provider_kind="ado", external_id="local", name="LocalOnly", owner_id=user.id, meta={}
    )
    db_session.add(project)
    db_session.commit()

    assert hub_workspace.hub_knowledge_status_rows(db_session, user, []) == []


def test_a_project_with_no_mirrored_summary_contributes_nothing(hub_on, db_session):
    """Hub unreachable during the projects mirror -> no summary -> local behaviour."""
    user = _user(db_session)
    _mirrored_project(db_session, user, summary=None)

    assert hub_workspace.hub_knowledge_status_rows(db_session, user, []) == []


def test_flag_off_contributes_nothing(db_session, monkeypatch, workspace_dir):
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "hub_sso_enabled", False)
    monkeypatch.setattr(config_module.settings, "hub_data_enabled", False)
    user = _user(db_session)
    _mirrored_project(db_session, user)

    assert hub_workspace.hub_knowledge_status_rows(db_session, user, []) == []


def test_anonymous_contributes_nothing(hub_on, db_session):
    user = _user(db_session)
    _mirrored_project(db_session, user)

    assert hub_workspace.hub_knowledge_status_rows(db_session, None, []) == []


def test_another_users_hub_project_never_leaks(hub_on, db_session):
    mine = _user(db_session, "mine@example.com", hub_user_id="1")
    theirs = _user(db_session, "theirs@example.com", hub_user_id="2")
    _mirrored_project(db_session, theirs, name="TheirProject", key="theirs")

    assert hub_workspace.hub_knowledge_status_rows(db_session, mine, []) == []


# ------------------------------------------------------------- the tell-tale
def test_an_unreadable_summary_warns(hub_on, db_session, monkeypatch):
    """A mirrored summary with no status at all is the silent-partial-success shape:
    nothing errors, and the grid shows "not indexed" as if the hub had nothing."""
    warnings: list[str] = []
    monkeypatch.setattr(
        hub_workspace.logger, "warning", lambda msg, *a, **k: warnings.append(msg.format(*a))
    )
    user = _user(db_session)
    _mirrored_project(db_session, user, summary={"repo": "surency-admin-hub", "branch": "main"})

    assert hub_workspace.hub_knowledge_status_rows(db_session, user, []) == []
    assert len(warnings) == 1
    assert "knowledgeStatus" in warnings[0]


def test_no_warning_when_the_summary_was_readable(hub_on, db_session, monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr(
        hub_workspace.logger, "warning", lambda msg, *a, **k: warnings.append(msg.format(*a))
    )
    user = _user(db_session)
    _mirrored_project(db_session, user)

    assert len(hub_workspace.hub_knowledge_status_rows(db_session, user, [])) == 1
    assert warnings == []


# ------------------------------------------------------- the endpoint + cost
@respx.mock
def test_the_grid_endpoint_reports_the_hub_status(hub_on, client, db_session, monkeypatch):
    user, headers = _authed(db_session, monkeypatch)
    _mirrored_project(db_session, user)

    body = client.get("/projects/knowledge", headers=headers).json()

    assert len(body) == 1
    assert body[0]["projectKey"] == "Surency"
    assert body[0]["status"] == "indexed"
    assert body[0]["confidence"] == 82
    assert body[0]["source"] == "hub"
    # A badge, not a knowledge base: no blob is invented here.
    assert body[0]["knowledge"] == {}
    assert body[0]["repo"] == ""
    # And nothing was persisted — the hub row is a projection, not a mirror.
    assert db_session.query(ProjectKnowledge).count() == 0
    assert respx.calls.call_count == 0


@respx.mock
def test_first_paint_costs_no_extra_hub_calls(hub_on, client, db_session, monkeypatch):
    """The acceptance criterion, asserted as a COST and not as a rendered badge.

    Only ``GET /projects`` (exact match) is mocked, so a per-repo knowledge
    fan-out raises as an unmocked request rather than quietly inflating a count.
    Three calls total is what the #514 projects mirror already spends
    (connections + tickets + projects) — the badge adds none.
    """
    user, headers = _authed(db_session, monkeypatch)
    respx.get(url__startswith=f"{HUB}/connections").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(url__regex=rf"{re.escape(HUB)}/tickets(\?.*)?$").mock(
        return_value=httpx.Response(200, json={"items": [], "total": 0})
    )
    respx.get(url__regex=PROJECTS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[{"id": 3, "key": "surency", "name": "Surency", "summary": _hub_summary()}],
        )
    )

    # Exactly what the grid does on first paint, in order.
    projects = client.get("/projects", headers=headers).json()
    knowledge = client.get("/projects/knowledge", headers=headers).json()

    assert [p["name"] for p in projects] == ["Surency"]
    after_projects = respx.calls.call_count
    assert after_projects == 3, [c.request.url.path for c in respx.calls]
    # The badge is right on FIRST paint, before the detail page is ever opened...
    assert [(k["projectKey"], k["status"]) for k in knowledge] == [("Surency", "indexed")]
    # ...and it cost nothing.
    assert respx.calls.call_count == after_projects


@respx.mock
def test_the_grid_endpoint_is_unchanged_for_a_local_project(
    hub_on, client, db_session, monkeypatch
):
    user, headers = _authed(db_session, monkeypatch)
    _local_row(db_session, user, project="LocalOnly", repo="app", status="indexed", confidence=64)

    body = client.get("/projects/knowledge", headers=headers).json()

    assert len(body) == 1
    assert body[0]["projectKey"] == "LocalOnly"
    assert body[0]["status"] == "indexed"
    assert body[0]["confidence"] == 64
    assert body[0]["source"] == "local"
    assert respx.calls.call_count == 0
