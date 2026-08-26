"""Tests for #727 / ADR 0015 slice 1 — a run's project is a stamped column.

Three things are pinned here, because each of them is something the old
*derive-on-read* resolution could not do:

1. ``POST /runs`` stamps ``Run.project_guid`` at creation, and refuses a run
   whose tickets span two projects.
2. ``?project=`` filters runs, tickets and reports — including the explicit
   ``unassigned`` bucket, so a row whose project cannot be resolved is visible
   somewhere rather than nowhere.
3. The migration backfills existing rows by walking the run's first ticket
   **once**, at upgrade time (see ``test_runs_project_guid_migration.py``).
"""

from __future__ import annotations

import pytest

from app.models.project import Project
from app.models.project_config import ProjectConfig
from app.models.provider_connection import ProviderConnection
from app.models.report import Report
from app.models.run import Run, RunTicket
from app.models.ticket import Ticket
from app.routers import runs as runs_router
from app.services import ai_service
from app.services.skills import TEST_CASE_GENERATOR, TEST_CASE_REVIEWER


# ------------------------------------------------------------------ helpers
def _patch_pipeline_blocking(monkeypatch):
    """Keep POST /runs off the AI pipeline entirely — this file tests stamping."""

    def canned(*_args, **kwargs):
        skill = kwargs.get("skill")
        if skill == TEST_CASE_REVIEWER:
            return {"verdict": "approve", "coverageGaps": [], "additionalCases": []}
        if skill == TEST_CASE_GENERATOR:
            return {"analysis": {}, "cases": []}
        return {}

    monkeypatch.setattr(ai_service, "run_json", canned)
    monkeypatch.setattr(runs_router, "run_generation_pipeline", lambda run_id, blocking=False: None)


def _make_project(db_session, name: str) -> tuple[Project, ProviderConnection]:
    """A project with its own work-item connection and config, wired by id.

    The id link (``work_item_connection_id``) is what resolution reads — see
    ``resolve_project_key``, which stopped trusting name comparison in #663.
    """
    connection = ProviderConnection(kind="ado", name=f"{name}-conn", config={"project": name})
    db_session.add(connection)
    db_session.flush()
    project = Project(provider_kind="ado", external_id=f"ext-{name}", name=name, active=True)
    db_session.add(project)
    db_session.flush()
    db_session.add(
        ProjectConfig(
            key=name,
            name=name,
            project_guid=project.guid,
            work_item_connection_id=connection.id,
        )
    )
    db_session.commit()
    db_session.refresh(project)
    db_session.refresh(connection)
    return project, connection


def _make_ticket(
    db_session,
    external_id: str,
    connection: ProviderConnection | None,
    project: Project | None,
    kind: str = "ado",
) -> Ticket:
    ticket = Ticket(
        external_id=external_id,
        provider_kind=kind,
        title=f"Work item {external_id}",
        connection_id=connection.id if connection else None,
        project_id=project.id if project else None,
    )
    db_session.add(ticket)
    db_session.commit()
    db_session.refresh(ticket)
    return ticket


@pytest.fixture
def two_projects(db_session):
    return _make_project(db_session, "Alpha"), _make_project(db_session, "Beta")


# ---------------------------------------------------------------- stamping
def test_create_run_stamps_project_guid(client, db_session, monkeypatch, two_projects):
    (alpha, alpha_conn), _ = two_projects
    ticket = _make_ticket(db_session, "ALP-1", alpha_conn, alpha)
    _patch_pipeline_blocking(monkeypatch)

    body = client.post("/runs", json={"ticketIds": [ticket.external_id]}).json()

    assert body["projectGuid"] == alpha.guid
    # Stamped in the row, not computed for the response.
    assert db_session.get(Run, body["id"]).project_guid == alpha.guid


def test_create_run_rejects_tickets_from_two_projects(
    client, db_session, monkeypatch, two_projects
):
    """The cheap server-side invariant (ADR 0015 section 9).

    Slice 6 scopes the picker so this is unreachable through the UI, but a mixed
    run would silently corrupt every project-scoped count, and the API is public.
    """
    (alpha, alpha_conn), (_beta, beta_conn) = two_projects
    a = _make_ticket(db_session, "ALP-2", alpha_conn, alpha)
    b = _make_ticket(db_session, "BET-1", beta_conn, None)
    _patch_pipeline_blocking(monkeypatch)

    resp = client.post("/runs", json={"ticketIds": [a.external_id, b.external_id]})

    assert resp.status_code == 400
    assert "multiple projects" in resp.json()["detail"]
    # And nothing was created — the refusal happens before the insert.
    assert db_session.query(Run).count() == 0


def test_create_run_with_unresolvable_ticket_is_allowed_and_unstamped(
    client, db_session, monkeypatch, two_projects
):
    """A ticket that resolves to no project must not block run creation.

    An install whose project is only *indexed* has no configured connection to
    resolve through. The run is stamped NULL and lands in the unassigned bucket.

    The ticket is a **jira** row with no jira connection: ADR 0006 resolution
    falls back to first-of-kind, so an unconnected *ado* ticket would still land
    on one of the two ado connections above. Only a kind nothing is configured
    for is genuinely unresolvable.
    """
    _patch_pipeline_blocking(monkeypatch)
    orphan = _make_ticket(db_session, "ORP-1", None, None, kind="jira")

    body = client.post("/runs", json={"ticketIds": [orphan.external_id]}).json()

    assert body["projectGuid"] is None


# ------------------------------------------------------------ ?project= runs
def test_list_runs_filters_by_project(client, db_session, two_projects):
    (alpha, _), (beta, _) = two_projects
    db_session.add_all(
        [
            Run(code="RUN-901", name="a", status="done", project_guid=alpha.guid),
            Run(code="RUN-902", name="b", status="done", project_guid=beta.guid),
            Run(code="RUN-903", name="legacy", status="done", project_guid=None),
        ]
    )
    db_session.commit()

    def codes(query: str) -> list[str]:
        return sorted(r["code"] for r in client.get(f"/runs{query}").json())

    assert codes(f"?project={alpha.guid}") == ["RUN-901"]
    assert codes(f"?project={beta.guid}") == ["RUN-902"]
    # The unassigned bucket exists precisely so RUN-903 is reachable.
    assert codes("?project=unassigned") == ["RUN-903"]
    # Negative control: without the param nothing is filtered, so the assertions
    # above cannot be the filter silently matching everything.
    assert codes("") == ["RUN-901", "RUN-902", "RUN-903"]


def test_list_runs_unknown_project_returns_nothing(client, db_session, two_projects):
    (alpha, _), _ = two_projects
    db_session.add(Run(code="RUN-904", name="a", status="done", project_guid=alpha.guid))
    db_session.commit()

    assert client.get("/runs?project=00000000-0000-0000-0000-000000000000").json() == []


# --------------------------------------------------------- ?project= tickets
def test_list_tickets_filters_by_project_including_unstamped(client, db_session, two_projects):
    """Both legs of the criterion, plus the fallback the issue calls out.

    ``Ticket.project_id`` is NULL for everything synced before project stamping.
    Those rows are claimed by their connection instead, so containment does not
    make them disappear.
    """
    (alpha, alpha_conn), (beta, beta_conn) = two_projects
    _make_ticket(db_session, "ALP-10", alpha_conn, alpha)  # stamped
    _make_ticket(db_session, "ALP-11", alpha_conn, None)  # unstamped, claimed
    _make_ticket(db_session, "BET-10", beta_conn, beta)
    _make_ticket(db_session, "ORP-10", None, None)  # claimed by nobody

    def ids(query: str) -> list[str]:
        return sorted(t["externalId"] for t in client.get(f"/tickets{query}").json()["items"])

    assert ids(f"?project={alpha.guid}") == ["ALP-10", "ALP-11"]
    assert ids(f"?project={beta.guid}") == ["BET-10"]
    assert ids("?project=unassigned") == ["ORP-10"]
    assert ids("") == ["ALP-10", "ALP-11", "BET-10", "ORP-10"]


def test_list_tickets_unknown_project_returns_empty_page_not_everything(
    client, db_session, two_projects
):
    """A mistyped GUID must not degrade into an unfiltered list."""
    (alpha, alpha_conn), _ = two_projects
    _make_ticket(db_session, "ALP-12", alpha_conn, alpha)

    page = client.get("/tickets?project=00000000-0000-0000-0000-000000000000").json()
    assert page["items"] == []
    assert page["total"] == 0


# --------------------------------------------------------- ?project= reports
def test_list_reports_filters_by_project(client, db_session, two_projects):
    (alpha, _), (beta, _) = two_projects
    alpha_run = Run(code="RUN-905", name="a", status="done", project_guid=alpha.guid)
    beta_run = Run(code="RUN-906", name="b", status="done", project_guid=beta.guid)
    db_session.add_all([alpha_run, beta_run])
    db_session.flush()
    db_session.add_all(
        [
            Report(run_id=alpha_run.id, overall_result="passed", pass_rate=100.0),
            Report(run_id=beta_run.id, overall_result="failed", pass_rate=0.0),
        ]
    )
    db_session.commit()

    def run_ids(query: str) -> list[int]:
        return sorted(r["runId"] for r in client.get(f"/reports{query}").json())

    assert run_ids(f"?project={alpha.guid}") == [alpha_run.id]
    assert run_ids(f"?project={beta.guid}") == [beta_run.id]
    assert run_ids("") == sorted([alpha_run.id, beta_run.id])


# ---------------------------------------------------------------- read path
def test_run_project_key_reads_the_stamped_column(db_session, two_projects):
    """``_resolve_run_project_key`` must stop walking tickets once stamped.

    The run below has NO run_tickets at all, so the first-ticket walk cannot
    resolve anything — if a key comes back, it came from the column.
    """
    (alpha, _), _ = two_projects
    run = Run(code="RUN-907", name="a", status="done", project_guid=alpha.guid)
    db_session.add(run)
    db_session.commit()

    assert runs_router._resolve_run_project_key(db_session, run) == "Alpha"


def test_run_project_key_falls_back_to_first_ticket_when_unstamped(db_session, two_projects):
    (alpha, alpha_conn), _ = two_projects
    ticket = _make_ticket(db_session, "ALP-13", alpha_conn, alpha)
    run = Run(code="RUN-908", name="a", status="done", project_guid=None)
    db_session.add(run)
    db_session.flush()
    db_session.add(RunTicket(run_id=run.id, ticket_external_id=ticket.external_id, position=0))
    db_session.commit()

    assert runs_router._resolve_run_project_key(db_session, run) == "Alpha"
