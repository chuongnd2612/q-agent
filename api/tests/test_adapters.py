"""Unit tests for provider adapters: normalization + connectivity, HTTP mocked via respx."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

from app.services.adapters.azure_devops import AzureDevOpsAdapter
from app.services.adapters.base import ProviderError
from app.services.adapters.github import GitHubAdapter
from app.services.adapters.jira import JiraAdapter


# ---------------------------------------------------------------- Azure DevOps
@respx.mock
def test_ado_test_connection_ok():
    adapter = AzureDevOpsAdapter(
        config={"orgUrl": "https://dev.azure.com/myorg", "project": "MyProj"},
        secrets={"pat": "secret-pat"},
    )
    respx.get("https://dev.azure.com/myorg/_apis/projects", params={"api-version": "7.1"}).mock(
        return_value=httpx.Response(200, json={"count": 2, "value": []})
    )
    result = adapter.test_connection()
    assert result["ok"] is True
    assert "2 projects" in result["message"]


def test_ado_test_connection_missing_pat():
    adapter = AzureDevOpsAdapter(config={"orgUrl": "https://dev.azure.com/myorg"}, secrets={})
    result = adapter.test_connection()
    assert result["ok"] is False
    assert "PAT" in result["message"]


@respx.mock
def test_ado_fetch_tickets_normalizes_fields():
    adapter = AzureDevOpsAdapter(
        config={"orgUrl": "https://dev.azure.com/myorg", "project": "MyProj"},
        secrets={"pat": "secret-pat"},
    )
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
                            "System.Description": "<p>As a user I want...</p>",
                            "System.Tags": "auth; security",
                            "Microsoft.VSTS.Common.AcceptanceCriteria": "<p>- AC1</p><p>- AC2</p>",
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

    tickets = adapter.fetch_tickets(mode="sprint", sprint="Sprint 12")
    assert len(tickets) == 1
    t = tickets[0]
    assert t["external_id"] == "101"
    assert t["title"] == "Login should reject bad password"
    assert t["priority"] == "High"
    assert t["assignee"] == "Maya Kaur"
    assert t["sprint"] == "Sprint 12"
    assert t["labels"] == ["auth", "security"]
    assert t["acceptance_criteria"] == ["AC1", "AC2"]


@respx.mock
def test_ado_fetch_tickets_retries_without_iteration_when_sprint_path_invalid():
    """A WIQL 400 from a non-existent sprint path retries without the filter."""
    import json as _json

    adapter = AzureDevOpsAdapter(
        config={"orgUrl": "https://dev.azure.com/myorg", "project": "MyProj"},
        secrets={"pat": "secret-pat"},
    )
    wiql = respx.post("https://dev.azure.com/myorg/MyProj/_apis/wit/wiql").mock(
        side_effect=[
            httpx.Response(
                400,
                json={"message": "VS402371: The iteration path 'MyProj\\Nope' does not exist."},
            ),
            httpx.Response(200, json={"workItems": [{"id": 7}]}),
        ]
    )
    respx.get("https://dev.azure.com/myorg/_apis/wit/workitems").mock(
        return_value=httpx.Response(
            200, json={"value": [{"id": 7, "fields": {"System.Title": "T"}, "relations": []}]}
        )
    )
    respx.get("https://dev.azure.com/myorg/_apis/wit/workItems/7/comments").mock(
        return_value=httpx.Response(200, json={"comments": []})
    )

    tickets = adapter.fetch_tickets(mode="sprint", sprint="Nope")
    assert len(tickets) == 1 and tickets[0]["external_id"] == "7"
    assert wiql.call_count == 2
    first = _json.loads(wiql.calls[0].request.content)["query"]
    second = _json.loads(wiql.calls[1].request.content)["query"]
    assert "IterationPath" in first and "IterationPath" not in second


@respx.mock
def test_ado_list_sprints_converts_iteration_paths():
    adapter = AzureDevOpsAdapter(
        config={"orgUrl": "https://dev.azure.com/myorg", "project": "MyProj"},
        secrets={"pat": "secret-pat"},
    )
    respx.get(
        "https://dev.azure.com/myorg/MyProj/_apis/wit/classificationnodes/iterations"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "MyProj",
                "path": "\\MyProj\\Iteration",
                "children": [
                    {
                        "name": "Sprint 1",
                        "identifier": "a1",
                        "path": "\\MyProj\\Iteration\\Sprint 1",
                        "attributes": {"startDate": "2026-01-01T00:00:00Z"},
                    },
                    {
                        "name": "Release 1",
                        "identifier": "r1",
                        "path": "\\MyProj\\Iteration\\Release 1",
                        "children": [
                            {
                                "name": "Sprint 2",
                                "identifier": "a2",
                                "path": "\\MyProj\\Iteration\\Release 1\\Sprint 2",
                            }
                        ],
                    },
                ],
            },
        )
    )
    sprints = {s["name"]: s["path"] for s in adapter.list_sprints()}
    assert sprints["Sprint 1"] == "MyProj\\Sprint 1"  # IterationPath form (no \Iteration)
    assert sprints["Release 1"] == "MyProj\\Release 1"
    assert sprints["Sprint 2"] == "MyProj\\Release 1\\Sprint 2"  # nested preserved


@respx.mock
def test_ado_fetch_tickets_raises_provider_error_on_non_sprint_wiql_400():
    adapter = AzureDevOpsAdapter(
        config={"orgUrl": "https://dev.azure.com/myorg", "project": "MyProj"},
        secrets={"pat": "secret-pat"},
    )
    respx.post("https://dev.azure.com/myorg/MyProj/_apis/wit/wiql").mock(
        return_value=httpx.Response(400, json={"message": "Bad query"})
    )
    with pytest.raises(ProviderError):
        adapter.fetch_tickets(mode="assigned")


@respx.mock
def test_ado_list_test_cases_returns_titles():
    adapter = AzureDevOpsAdapter(
        config={"orgUrl": "https://dev.azure.com/myorg", "project": "MyProj"},
        secrets={"pat": "secret-pat"},
    )
    respx.post("https://dev.azure.com/myorg/MyProj/_apis/wit/wiql").mock(
        return_value=httpx.Response(200, json={"workItems": [{"id": 501}, {"id": 502}]})
    )
    respx.get("https://dev.azure.com/myorg/_apis/wit/workitems").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {"id": 501, "fields": {"System.Title": "TC-08: Login", "System.State": "Design"}},
                    {"id": 502, "fields": {"System.Title": "TC-09: Logout", "System.State": "Ready"}},
                ]
            },
        )
    )
    cases = adapter.list_test_cases("SUR-1428")
    assert [c["external_id"] for c in cases] == ["501", "502"]
    assert cases[0]["title"] == "TC-08: Login"


@respx.mock
def test_ado_work_item_metadata():
    adapter = AzureDevOpsAdapter(
        config={"orgUrl": "https://dev.azure.com/myorg", "project": "MyProj"},
        secrets={"pat": "secret-pat"},
    )
    respx.get("https://dev.azure.com/myorg/MyProj/_apis/wit/classificationnodes/areas").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "MyProj",
                "path": "\\MyProj\\Area",
                "children": [
                    {"name": "Brokers", "identifier": "a1", "path": "\\MyProj\\Area\\Brokers"}
                ],
            },
        )
    )
    respx.get("https://dev.azure.com/myorg/MyProj/_apis/wit/workitemtypes").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {"name": "Bug", "states": [{"name": "New"}, {"name": "Active"}]},
                    {"name": "User Story", "states": [{"name": "New"}, {"name": "Ready for QA"}]},
                ]
            },
        )
    )
    meta = adapter.list_work_item_metadata()
    assert meta["area_paths"][0]["path"] == "MyProj\\Brokers"
    assert set(meta["work_item_types"]) == {"Bug", "User Story"}
    assert "Ready for QA" in meta["states"] and "Active" in meta["states"]
    assert meta["epics"] == []


@respx.mock
def test_ado_publish_comment_uses_basic_auth():
    adapter = AzureDevOpsAdapter(
        config={"orgUrl": "https://dev.azure.com/myorg", "project": "MyProj"},
        secrets={"pat": "secret-pat"},
    )
    route = respx.post(
        "https://dev.azure.com/myorg/_apis/wit/workItems/101/comments"
    ).mock(return_value=httpx.Response(200, json={"id": 555}))

    comment_id = adapter.publish_comment("101", "All tests passed")
    assert comment_id == "555"
    sent = route.calls[0].request
    expected_token = base64.b64encode(b":secret-pat").decode()
    assert sent.headers["Authorization"] == f"Basic {expected_token}"


# ---------------------------------------------------------------- Jira
@respx.mock
def test_jira_test_connection_ok():
    adapter = JiraAdapter(
        config={"baseUrl": "https://myorg.atlassian.net", "project": "SUR"},
        secrets={"email": "qa@myorg.com", "apiToken": "tok"},
    )
    respx.get("https://myorg.atlassian.net/rest/api/3/myself").mock(
        return_value=httpx.Response(200, json={"displayName": "Maya Kaur", "accountId": "abc"})
    )
    result = adapter.test_connection()
    assert result["ok"] is True
    assert "Maya Kaur" in result["message"]


def test_jira_test_connection_missing_credentials():
    adapter = JiraAdapter(config={"baseUrl": "https://myorg.atlassian.net"}, secrets={})
    result = adapter.test_connection()
    assert result["ok"] is False


@respx.mock
def test_jira_fetch_tickets_normalizes_adf_description():
    adapter = JiraAdapter(
        config={"baseUrl": "https://myorg.atlassian.net", "project": "SUR"},
        secrets={"email": "qa@myorg.com", "apiToken": "tok"},
    )
    respx.post("https://myorg.atlassian.net/rest/api/3/search/jql").mock(
        return_value=httpx.Response(
            200,
            json={
                "issues": [
                    {
                        "key": "SUR-1428",
                        "fields": {
                            "summary": "Cart total miscalculates tax",
                            "status": {"name": "Ready for QA"},
                            "priority": {"name": "High"},
                            "assignee": {"displayName": "Maya Kaur"},
                            "issuetype": {"name": "Bug"},
                            "labels": ["billing"],
                            "description": {
                                "type": "doc",
                                "version": 1,
                                "content": [
                                    {
                                        "type": "paragraph",
                                        "content": [{"type": "text", "text": "Tax should be 8%."}],
                                    }
                                ],
                            },
                            "comment": {"comments": []},
                            "attachment": [],
                        },
                    }
                ]
            },
        )
    )

    tickets = adapter.fetch_tickets(mode="sprint", sprint="Sprint 12")
    assert len(tickets) == 1
    t = tickets[0]
    assert t["external_id"] == "SUR-1428"
    assert t["priority"] == "High"
    assert "Tax should be 8%." in t["description"]
    assert t["labels"] == ["billing"]


@respx.mock
def test_jira_fetch_tickets_normalizes_epic_from_parent():
    adapter = JiraAdapter(
        config={"baseUrl": "https://myorg.atlassian.net", "project": "SUR"},
        secrets={"email": "qa@myorg.com", "apiToken": "tok"},
    )
    respx.post("https://myorg.atlassian.net/rest/api/3/search/jql").mock(
        return_value=httpx.Response(
            200,
            json={
                "issues": [
                    {
                        "key": "SUR-1428",
                        "fields": {
                            "summary": "Cart total miscalculates tax",
                            "status": {"name": "Ready for QA"},
                            "priority": {"name": "High"},
                            "issuetype": {"name": "Bug"},
                            "labels": [],
                            "comment": {"comments": []},
                            "attachment": [],
                            "parent": {"fields": {"summary": "Checkout epic"}},
                        },
                    }
                ]
            },
        )
    )

    tickets = adapter.fetch_tickets(mode="sprint", sprint="Sprint 12")
    assert tickets[0]["epic"] == "Checkout epic"


def test_jira_fetch_tickets_epic_defaults_empty_without_parent():
    adapter = JiraAdapter(
        config={"baseUrl": "https://myorg.atlassian.net", "project": "SUR"},
        secrets={"email": "qa@myorg.com", "apiToken": "tok"},
    )
    ticket = adapter._normalize(
        {
            "key": "SUR-1",
            "fields": {
                "summary": "No epic",
                "status": {"name": "Done"},
                "priority": {"name": "Low"},
                "issuetype": {"name": "Task"},
            },
        }
    )
    assert ticket["epic"] == ""


@respx.mock
def test_jira_work_item_metadata_includes_epics():
    adapter = JiraAdapter(
        config={"baseUrl": "https://myorg.atlassian.net", "project": "SUR"},
        secrets={"email": "qa@myorg.com", "apiToken": "tok"},
    )
    respx.get("https://myorg.atlassian.net/rest/api/3/issuetype").mock(
        return_value=httpx.Response(200, json=[{"name": "Bug"}])
    )
    respx.get("https://myorg.atlassian.net/rest/api/3/status").mock(
        return_value=httpx.Response(200, json=[{"name": "Done"}])
    )
    respx.post("https://myorg.atlassian.net/rest/api/3/search/jql").mock(
        return_value=httpx.Response(
            200,
            json={"issues": [{"key": "SUR-100", "fields": {"summary": "Checkout epic"}}]},
        )
    )
    meta = adapter.list_work_item_metadata()
    assert meta["epics"] == [{"key": "SUR-100", "name": "Checkout epic"}]


@respx.mock
def test_jira_work_item_metadata_epics_empty_on_error():
    adapter = JiraAdapter(
        config={"baseUrl": "https://myorg.atlassian.net", "project": "SUR"},
        secrets={"email": "qa@myorg.com", "apiToken": "tok"},
    )
    respx.get("https://myorg.atlassian.net/rest/api/3/issuetype").mock(
        side_effect=httpx.ConnectError("boom")
    )
    meta = adapter.list_work_item_metadata()
    assert meta["epics"] == []


# ---------------------------------------------------------------- GitHub
@respx.mock
def test_github_test_connection_ok():
    adapter = GitHubAdapter(config={"org": "acme", "repo": "webapp"}, secrets={"pat": "ghp_xxx"})
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, json={"login": "duna"})
    )
    result = adapter.test_connection()
    assert result["ok"] is True
    assert "duna" in result["message"]


def test_github_test_connection_missing_pat():
    adapter = GitHubAdapter(config={"org": "acme", "repo": "webapp"}, secrets={})
    result = adapter.test_connection()
    assert result["ok"] is False


@respx.mock
def test_github_fetch_tickets_normalizes_issue():
    adapter = GitHubAdapter(config={"org": "acme", "repo": "webapp"}, secrets={"pat": "ghp_xxx"})
    respx.get("https://api.github.com/repos/acme/webapp/issues").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "number": 42,
                    "title": "Checkout button disabled on Safari",
                    "state": "open",
                    "labels": [{"name": "priority:high"}],
                    "assignee": {"login": "maya"},
                    "body": "Steps to reproduce...",
                    "comments": 0,
                }
            ],
        )
    )

    tickets = adapter.fetch_tickets(mode="sprint")
    assert len(tickets) == 1
    t = tickets[0]
    assert t["external_id"] == "42"
    assert t["status"] == "In Progress"
    assert t["priority"] == "High"
    assert t["assignee"] == "maya"


@respx.mock
def test_github_publish_comment():
    adapter = GitHubAdapter(config={"org": "acme", "repo": "webapp"}, secrets={"pat": "ghp_xxx"})
    respx.post("https://api.github.com/repos/acme/webapp/issues/42/comments").mock(
        return_value=httpx.Response(201, json={"id": 999})
    )
    comment_id = adapter.publish_comment("42", "Automation passed.")
    assert comment_id == "999"


def test_adapter_raises_provider_error_without_config():
    adapter = GitHubAdapter(config={}, secrets={})
    with pytest.raises(ProviderError):
        adapter.fetch_tickets(mode="sprint")


@respx.mock
def test_github_list_repos_org_account():
    adapter = GitHubAdapter(config={"org": "acme"}, secrets={"pat": "ghp_xxx"})
    respx.get("https://api.github.com/orgs/acme/repos").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"name": "webapp", "clone_url": "https://github.com/acme/webapp.git",
                 "html_url": "https://github.com/acme/webapp", "default_branch": "main"},
            ],
        )
    )
    repos = adapter.list_repos()
    assert [r["name"] for r in repos] == ["webapp"]
    assert repos[0]["clone_url"] == "https://github.com/acme/webapp.git"


@respx.mock
def test_github_list_repos_user_account_falls_back_to_user_repos():
    """A personal account 404s on /orgs; discovery falls back to /user/repos
    (which includes private repos) filtered to the configured owner."""
    adapter = GitHubAdapter(config={"org": "Gift-Card-Market", "repo": "GiftcardMarketplace"},
                            secrets={"pat": "ghp_xxx"})
    respx.get("https://api.github.com/orgs/Gift-Card-Market/repos").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    respx.get("https://api.github.com/user/repos").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"name": "GiftcardMarketplace", "clone_url": "https://github.com/Gift-Card-Market/GiftcardMarketplace.git",
                 "html_url": "https://github.com/Gift-Card-Market/GiftcardMarketplace", "default_branch": "main",
                 "owner": {"login": "Gift-Card-Market"}},
                {"name": "unrelated", "clone_url": "https://github.com/someone-else/unrelated.git",
                 "html_url": "https://github.com/someone-else/unrelated", "default_branch": "main",
                 "owner": {"login": "someone-else"}},
            ],
        )
    )
    repos = adapter.list_repos()
    assert [r["name"] for r in repos] == ["GiftcardMarketplace"]


@respx.mock
def test_github_list_repos_falls_back_to_single_repo():
    """When neither org nor user listing yields the owner's repos, fall back to
    the single configured repo (e.g. a repo-scoped PAT)."""
    adapter = GitHubAdapter(config={"org": "Gift-Card-Market", "repo": "GiftcardMarketplace"},
                            secrets={"pat": "ghp_xxx"})
    respx.get("https://api.github.com/orgs/Gift-Card-Market/repos").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    respx.get("https://api.github.com/user/repos").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get("https://api.github.com/repos/Gift-Card-Market/GiftcardMarketplace").mock(
        return_value=httpx.Response(
            200,
            json={"name": "GiftcardMarketplace",
                  "clone_url": "https://github.com/Gift-Card-Market/GiftcardMarketplace.git",
                  "html_url": "https://github.com/Gift-Card-Market/GiftcardMarketplace",
                  "default_branch": "main"},
        )
    )
    repos = adapter.list_repos()
    assert [r["name"] for r in repos] == ["GiftcardMarketplace"]


# --------------------------------------------- ADO comment attachments (#696)
#
# Uploading and *linking* are two calls, and both are needed: the upload parks the
# bytes and returns a URL, and the relation is what puts the file under the work
# item's Attachments — and what makes that URL readable by anyone who can see the
# ticket. An uploaded-but-unlinked attachment is a URL nobody can find, which is a
# failure that looks like success from the caller's side.

_ORG = "https://dev.azure.com/myorg"
_ATTACH = f"{_ORG}/MyProj/_apis/wit/attachments"
_COMMENTS = f"{_ORG}/_apis/wit/workItems/1377/comments"
_WORKITEM = f"{_ORG}/_apis/wit/workitems/1377"


def _ado() -> AzureDevOpsAdapter:
    return AzureDevOpsAdapter(
        config={"orgUrl": _ORG, "project": "MyProj"},
        secrets={"pat": "secret-pat"},
    )


@respx.mock
def test_ado_uploads_links_and_references_each_attachment(tmp_path):
    shot = tmp_path / "TC-01-screenshot.png"
    shot.write_bytes(b"PNG-BYTES")

    upload = respx.post(_ATTACH).mock(
        return_value=httpx.Response(201, json={"id": "abc", "url": f"{_ATTACH}/abc"})
    )
    link = respx.patch(_WORKITEM).mock(return_value=httpx.Response(200, json={"id": 1377}))
    comment = respx.post(_COMMENTS).mock(return_value=httpx.Response(200, json={"id": 55}))

    external_id = _ado().publish_comment("1377", "1/1 passed", attachments=[str(shot)])

    assert external_id == "55"
    # The bytes went up, under the case-prefixed name the manifest promised.
    assert upload.called
    assert upload.calls.last.request.content == b"PNG-BYTES"
    assert upload.calls.last.request.url.params["fileName"] == "TC-01-screenshot.png"
    # ...and were RELATED to the work item, or nobody could find them.
    assert link.called
    relation = json.loads(link.calls.last.request.content)[0]
    assert relation["value"]["rel"] == "AttachedFile"
    assert relation["value"]["url"] == f"{_ATTACH}/abc"
    # ...and the comment points at them.
    body = json.loads(comment.calls.last.request.content)["text"]
    assert "1/1 passed" in body, "the prepared body was replaced rather than extended"
    assert f'href="{_ATTACH}/abc"' in body


@respx.mock
def test_ado_uploads_binary_as_octet_stream(tmp_path):
    """The client's default is `application/json`; ADO answers 400 to binary
    announced as JSON, so the header has to be overridden per call."""
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"PNG")
    upload = respx.post(_ATTACH).mock(
        return_value=httpx.Response(201, json={"url": f"{_ATTACH}/abc"})
    )
    respx.patch(_WORKITEM).mock(return_value=httpx.Response(200, json={}))
    respx.post(_COMMENTS).mock(return_value=httpx.Response(200, json={"id": 1}))

    _ado().publish_comment("1377", "body", attachments=[str(shot)])

    assert upload.calls.last.request.headers["content-type"] == "application/octet-stream"


@respx.mock
def test_ado_still_posts_the_comment_when_an_attachment_fails(tmp_path):
    """A comment that reaches the ticket without one screenshot is worth far more
    than no comment at all — but the body has to say which file is missing, so nobody
    goes looking for evidence that never arrived."""
    good, bad = tmp_path / "good.png", tmp_path / "bad.png"
    good.write_bytes(b"OK")
    bad.write_bytes(b"NO")

    def upload_response(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("fileName") == "bad.png":
            return httpx.Response(500, text="nope")
        return httpx.Response(201, json={"url": f"{_ATTACH}/ok"})

    respx.post(_ATTACH).mock(side_effect=upload_response)
    respx.patch(_WORKITEM).mock(return_value=httpx.Response(200, json={}))
    comment = respx.post(_COMMENTS).mock(return_value=httpx.Response(200, json={"id": 9}))

    external_id = _ado().publish_comment("1377", "body", attachments=[str(good), str(bad)])

    assert external_id == "9", "one bad attachment lost the whole comment"
    body = json.loads(comment.calls.last.request.content)["text"]
    assert "good.png" in body
    assert "Could not attach: bad.png" in body


@respx.mock
def test_ado_with_no_attachments_uploads_nothing_but_still_renders_html(tmp_path):
    """No attachments, no upload — but the body is still converted (#703).

    A draft is Markdown, an ADO comment renders HTML, and posting the draft verbatim
    is why `**PASSED**` reached work items as literal asterisks.
    """
    respx.post(_COMMENTS).mock(return_value=httpx.Response(200, json={"id": 3}))
    upload = respx.post(_ATTACH).mock(return_value=httpx.Response(201, json={}))

    _ado().publish_comment("1377", "**Status:** PASSED")

    assert not upload.called
    body = json.loads(respx.calls.last.request.content)["text"]
    assert "<b>Status:</b> PASSED" in body
    assert "**" not in body, "markdown reached the work item"


def test_ado_declares_the_attachment_capability():
    """`publish_service` branches on this; a provider that cannot attach must not be
    handed files, and must say so in the comment instead."""
    assert _ado().supports_attachments() is True
    assert GitHubAdapter(config={}, secrets={}).supports_attachments() is False
    assert JiraAdapter(config={}, secrets={}).supports_attachments() is False
