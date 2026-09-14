"""API tests for /tickets sync + list/detail, and /projects refresh."""

from __future__ import annotations

import httpx
import respx


def _configure_ado(client) -> int:
    """Create + configure an ADO work-item connection; return its id."""
    conn = client.post("/providers/ado/connections", json={"name": "ADO"}).json()
    client.put(
        f"/connections/{conn['id']}",
        json={
            "config": {"orgUrl": "https://dev.azure.com/myorg", "project": "MyProj"},
            "secrets": {"pat": "secret-pat"},
        },
    )
    return conn["id"]


@respx.mock
def test_sync_tickets_upserts_and_returns_result(client, db_session):
    connection_id = _configure_ado(client)

    respx.post("https://dev.azure.com/myorg/MyProj/_apis/wit/wiql").mock(
        return_value=httpx.Response(200, json={"workItems": [{"id": 101}]})
    )
    respx.get("https://dev.azure.com/myorg/_apis/wit/workitems").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": 101,
                        "fields": {
                            "System.Title": "Login should reject bad password",
                            "System.WorkItemType": "User Story",
                            "System.State": "Ready for QA",
                            "Microsoft.VSTS.Common.Priority": 1,
                            "System.AssignedTo": {"displayName": "Maya Kaur"},
                            "System.IterationPath": "MyProj\\Sprint 12",
                            "System.Description": "<p>Body</p>",
                            "System.Tags": "auth",
                            "Microsoft.VSTS.Common.AcceptanceCriteria": "<p>- AC1</p>",
                        },
                        "relations": [],
                    }
                ]
            },
        )
    )
    respx.get("https://dev.azure.com/myorg/_apis/wit/workItems/101/comments").mock(
        return_value=httpx.Response(200, json={"comments": []})
    )

    resp = client.post(
        "/tickets/sync",
        json={"connectionId": connection_id, "mode": "sprint", "sprint": "Sprint 12"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["synced"] == 1
    assert body["tickets"][0]["externalId"] == "101"
    assert body["tickets"][0]["priority"] == "High"

    # persisted and retrievable via GET /tickets
    list_resp = client.get("/tickets")
    assert list_resp.status_code == 200
    page = list_resp.json()
    assert page["total"] == 1
    assert len(page["items"]) == 1
    assert page["items"][0]["connectionId"] == connection_id

    # The synced ticket is stamped with the work-item connection it came from.
    from app.models.ticket import Ticket

    db_session.expire_all()
    stamped = db_session.query(Ticket).filter(Ticket.external_id == "101").first()
    assert stamped.connection_id == connection_id


@respx.mock
def test_resync_with_partial_payload_keeps_prior_content(client, db_session):
    """A re-sync whose payload omits description/AC must not blank prior content (#864)."""
    connection_id = _configure_ado(client)

    wiql = respx.post("https://dev.azure.com/myorg/MyProj/_apis/wit/wiql").mock(
        return_value=httpx.Response(200, json={"workItems": [{"id": 101}]})
    )
    workitems = respx.get("https://dev.azure.com/myorg/_apis/wit/workitems")
    respx.get("https://dev.azure.com/myorg/_apis/wit/workItems/101/comments").mock(
        return_value=httpx.Response(200, json={"comments": []})
    )

    workitems.mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": 101,
                        "fields": {
                            "System.Title": "Login should reject bad password",
                            "System.State": "Ready for QA",
                            "System.Description": "<p>Body</p>",
                            "Microsoft.VSTS.Common.AcceptanceCriteria": "<p>- AC1</p>",
                        },
                        "relations": [],
                    }
                ]
            },
        )
    )
    resp = client.post(
        "/tickets/sync",
        json={"connectionId": connection_id, "mode": "sprint", "sprint": "Sprint 12"},
    )
    assert resp.status_code == 200

    from app.models.ticket import Ticket

    db_session.expire_all()
    ticket = db_session.query(Ticket).filter(Ticket.external_id == "101").first()
    assert ticket.description == "Body"
    assert ticket.acceptance_criteria

    # Re-sync: same work item, but this fetch's payload has no description/AC —
    # e.g. a provider hiccup or a lighter fetch. Prior content must survive.
    workitems.mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": 101,
                        "fields": {
                            "System.Title": "Login should reject bad password",
                            "System.State": "Ready for QA",
                        },
                        "relations": [],
                    }
                ]
            },
        )
    )
    resp = client.post(
        "/tickets/sync",
        json={"connectionId": connection_id, "mode": "sprint", "sprint": "Sprint 12"},
    )
    assert resp.status_code == 200

    db_session.expire_all()
    ticket = db_session.query(Ticket).filter(Ticket.external_id == "101").first()
    assert ticket.description == "Body"
    assert ticket.acceptance_criteria


@respx.mock
def test_sync_tickets_unknown_provider_404(client):
    resp = client.post("/tickets/sync", json={"providerKind": "ado", "mode": "sprint"})
    assert resp.status_code == 404


@respx.mock
def test_get_ticket_detail(client):
    connection_id = _configure_ado(client)
    respx.post("https://dev.azure.com/myorg/MyProj/_apis/wit/wiql").mock(
        return_value=httpx.Response(200, json={"workItems": [{"id": 202}]})
    )
    respx.get("https://dev.azure.com/myorg/_apis/wit/workitems").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": 202,
                        "fields": {
                            "System.Title": "Signup form validation",
                            "System.WorkItemType": "User Story",
                            "System.State": "In Progress",
                            "Microsoft.VSTS.Common.Priority": 2,
                            "System.AssignedTo": {"displayName": "Maya Kaur"},
                            "System.IterationPath": "MyProj\\Sprint 12",
                            "System.Description": "<p>Detail body</p>",
                            "System.Tags": "",
                            "Microsoft.VSTS.Common.AcceptanceCriteria": "<p>- AC1</p><p>- AC2</p>",
                        },
                        "relations": [],
                    }
                ]
            },
        )
    )
    respx.get("https://dev.azure.com/myorg/_apis/wit/workItems/202/comments").mock(
        return_value=httpx.Response(
            200, json={"comments": [{"createdBy": {"displayName": "Bob"}, "text": "LGTM"}]}
        )
    )

    client.post(
        "/tickets/sync",
        json={"connectionId": connection_id, "mode": "sprint", "sprint": "Sprint 12"},
    )

    detail_resp = client.get("/tickets/202")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["externalId"] == "202"
    assert detail["description"] == "Detail body"
    assert detail["acceptanceCriteria"] == ["AC1", "AC2"]
    assert detail["comments"][0]["text"] == "LGTM"


@respx.mock
def test_get_ticket_detail_ac_html_and_linked_pr(client):
    """A synced ADO ticket exposes the original AC as HTML, and a PR artifact
    relation resolves to a real numeric ``num`` plus a clickable web ``url`` (#225)."""
    connection_id = _configure_ado(client)
    respx.post("https://dev.azure.com/myorg/MyProj/_apis/wit/wiql").mock(
        return_value=httpx.Response(200, json={"workItems": [{"id": 303}]})
    )
    respx.get("https://dev.azure.com/myorg/_apis/wit/workitems").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": 303,
                        "fields": {
                            "System.Title": "Rich AC ticket",
                            "System.WorkItemType": "User Story",
                            "System.State": "Ready for QA",
                            "Microsoft.VSTS.Common.Priority": 2,
                            "System.AssignedTo": {"displayName": "Maya Kaur"},
                            "System.IterationPath": "MyProj\\Sprint 12",
                            "System.Description": "<p>Body</p>",
                            "System.Tags": "",
                            # A single rich blob that does not split into >=2 clean criteria.
                            "Microsoft.VSTS.Common.AcceptanceCriteria": (
                                "<div><b>Given</b> a user, <b>then</b> allow login</div>"
                            ),
                        },
                        "relations": [
                            {
                                "rel": "ArtifactLink",
                                "url": "vstfs:///Git/PullRequestId/proj-guid%2Frepo-guid%2F42",
                                "attributes": {"name": "Pull Request"},
                            }
                        ],
                    }
                ]
            },
        )
    )
    respx.get("https://dev.azure.com/myorg/_apis/wit/workItems/303/comments").mock(
        return_value=httpx.Response(200, json={"comments": []})
    )

    client.post(
        "/tickets/sync",
        json={"connectionId": connection_id, "mode": "sprint", "sprint": "Sprint 12"},
    )

    detail = client.get("/tickets/303").json()
    assert "Given" in detail["acceptanceCriteriaHtml"]
    assert len(detail["linkedPrs"]) == 1
    pr = detail["linkedPrs"][0]
    assert pr["num"] == "42"
    assert pr["title"] == "PR !42"
    assert pr["url"] == "https://dev.azure.com/myorg/proj-guid/_git/repo-guid/pullrequest/42"


def test_get_ticket_detail_not_found(client):
    resp = client.get("/tickets/does-not-exist")
    assert resp.status_code == 404


def test_list_tickets_filters_by_status(client, db_session):
    from app.models.ticket import Ticket

    db_session.add(
        Ticket(external_id="1", provider_kind="ado", title="A", status="Done", priority="Low")
    )
    db_session.add(
        Ticket(
            external_id="2",
            provider_kind="ado",
            title="B",
            status="Ready for QA",
            priority="High",
        )
    )
    db_session.commit()

    resp = client.get("/tickets", params={"status": "Ready for QA"})
    assert resp.status_code == 200
    page = resp.json()
    results = page["items"]
    assert page["total"] == 1
    assert len(results) == 1
    assert results[0]["externalId"] == "2"


def test_list_tickets_pagination(client, db_session):
    from app.models.ticket import Ticket

    for i in range(5):
        db_session.add(
            Ticket(external_id=str(i), provider_kind="ado", title=f"T{i}", status="Done", priority="Low")
        )
    db_session.commit()

    resp = client.get("/tickets", params={"page": 1, "pageSize": 2})
    assert resp.status_code == 200
    page = resp.json()
    assert page["total"] == 5
    assert page["page"] == 1
    assert page["pageSize"] == 2
    assert len(page["items"]) == 2

    resp2 = client.get("/tickets", params={"page": 3, "pageSize": 2})
    page2 = resp2.json()
    assert page2["total"] == 5
    assert len(page2["items"]) == 1

    # Pages don't overlap.
    ids_page1 = {t["id"] for t in page["items"]}
    ids_page2 = {t["id"] for t in page2["items"]}
    assert ids_page1.isdisjoint(ids_page2)


def test_list_tickets_scoped_by_connection(client, db_session):
    from app.models.ticket import Ticket

    db_session.add(
        Ticket(external_id="1", provider_kind="ado", connection_id=1, title="A", status="Done", priority="Low")
    )
    db_session.add(
        Ticket(external_id="2", provider_kind="ado", connection_id=2, title="B", status="Done", priority="Low")
    )
    db_session.commit()

    resp = client.get("/tickets", params={"connectionId": 1})
    assert resp.status_code == 200
    page = resp.json()
    assert page["total"] == 1
    assert page["items"][0]["externalId"] == "1"


def test_list_tickets_filters_by_priority(client, db_session):
    from app.models.ticket import Ticket

    db_session.add(Ticket(external_id="1", provider_kind="ado", title="A", status="Done", priority="High"))
    db_session.add(Ticket(external_id="2", provider_kind="ado", title="B", status="Done", priority="Low"))
    db_session.commit()

    resp = client.get("/tickets", params={"priority": "High"})
    assert resp.status_code == 200
    page = resp.json()
    assert page["total"] == 1
    assert page["items"][0]["externalId"] == "1"


def test_list_tickets_filters_by_epic(client, db_session):
    from app.models.ticket import Ticket

    db_session.add(
        Ticket(external_id="1", provider_kind="ado", title="A", status="Done", priority="Low", epic="Checkout")
    )
    db_session.add(
        Ticket(external_id="2", provider_kind="ado", title="B", status="Done", priority="Low", epic="Billing")
    )
    db_session.commit()

    resp = client.get("/tickets", params={"epic": "Checkout"})
    assert resp.status_code == 200
    page = resp.json()
    assert page["total"] == 1
    assert page["items"][0]["externalId"] == "1"
    assert page["items"][0]["epic"] == "Checkout"


def test_list_tickets_filters_by_area_path_with_backslash(client, db_session):
    """Area-path filter matches the selected node AND its children (UNDER
    semantics), and must handle the backslash in ADO area paths.

    Note: the real-world bug this guards was Postgres-specific — Postgres LIKE
    treats backslash as its default ESCAPE char, so a raw
    ``LIKE 'Surency\\Data Platform%'`` matched nothing. The fix uses
    ``startswith(autoescape=True)`` (ESCAPE '/'). This test runs on SQLite
    (which doesn't special-case backslash) so it locks in the UNDER semantics
    and that the fix keeps normal matching intact; the Postgres escape was
    verified directly against the live database.
    """
    from app.models.ticket import Ticket

    db_session.add(
        Ticket(external_id="1", provider_kind="ado", title="Exact", status="Done", priority="Low", area_path="Surency\\Data Platform")
    )
    db_session.add(
        Ticket(external_id="2", provider_kind="ado", title="Child", status="Done", priority="Low", area_path="Surency\\Data Platform\\Ingestion")
    )
    db_session.add(
        Ticket(external_id="3", provider_kind="ado", title="Other", status="Done", priority="Low", area_path="Surency\\Admin Hub")
    )
    db_session.commit()

    resp = client.get("/tickets", params={"areaPath": "Surency\\Data Platform"})
    assert resp.status_code == 200
    page = resp.json()
    ids = {t["externalId"] for t in page["items"]}
    assert ids == {"1", "2"}  # the node itself + its child, not the sibling


def test_delete_ticket_removes_row(client, db_session):
    """DELETE /tickets/{external_id} removes the row locally and returns 204."""
    from app.models.ticket import Ticket

    db_session.add(Ticket(external_id="1", provider_kind="ado", title="A", status="Done", priority="Low"))
    db_session.commit()

    resp = client.delete("/tickets/1")
    assert resp.status_code == 204

    db_session.expire_all()
    assert db_session.query(Ticket).filter(Ticket.external_id == "1").first() is None


def test_delete_ticket_not_found_404(client):
    """Deleting a non-existent ticket 404s, matching GET detail's convention."""
    resp = client.delete("/tickets/does-not-exist")
    assert resp.status_code == 404


def test_bulk_delete_tickets_returns_count(client, db_session):
    """POST /tickets/delete removes all matching rows and returns the count;
    unknown ids are ignored (no error)."""
    from app.models.ticket import Ticket

    for i in range(3):
        db_session.add(
            Ticket(external_id=str(i), provider_kind="ado", title=f"T{i}", status="Done", priority="Low")
        )
    db_session.commit()

    resp = client.post("/tickets/delete", json={"externalIds": ["0", "1", "nope"]})
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 2

    db_session.expire_all()
    remaining = {t.external_id for t in db_session.query(Ticket).all()}
    assert remaining == {"2"}


def test_bulk_delete_tickets_empty_is_noop(client):
    """An empty id list deletes nothing and returns deleted=0."""
    resp = client.post("/tickets/delete", json={"externalIds": []})
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 0


@respx.mock
def test_projects_refresh_upserts_from_connected_providers(client):
    connection_id = _configure_ado(client)

    respx.get("https://dev.azure.com/myorg/_apis/projects", params={"api-version": "7.1"}).mock(
        return_value=httpx.Response(200, json={"count": 1, "value": [{"id": "p1", "name": "MyProj"}]})
    )

    # Mark the connection connected via the real test-connection endpoint first.
    resp_test = client.post(f"/connections/{connection_id}/test")
    assert resp_test.status_code == 200

    resp = client.post("/projects/refresh")
    assert resp.status_code == 200
    projects = resp.json()
    assert len(projects) == 1
    assert projects[0]["externalId"] == "p1"
    assert projects[0]["name"] == "MyProj"

    list_resp = client.get("/projects")
    assert len(list_resp.json()) == 1
