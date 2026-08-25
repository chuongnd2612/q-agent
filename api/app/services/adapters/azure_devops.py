"""Azure DevOps provider adapter.

Talks to the real Azure DevOps REST API via ``httpx`` (ADR 0001 — no mock
fallback). Authenticates with basic auth using an empty username and a PAT
(Azure DevOps convention).

Config fields (non-secret): ``orgUrl`` (e.g. "https://dev.azure.com/myorg"),
``project`` (default ADO project name).
Secret fields: ``pat`` (Personal Access Token).
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import httpx

from app.logging import logger
from app.services.adapters import register
from app.services.adapters.base import NormalizedTicket, ProviderAdapter, ProviderError

API_VERSION = "7.1"

# QA-relevant work item types across the common ADO process templates (Agile,
# Scrum, Basic). Unknown values in a WIQL IN() list simply don't match — they do
# not cause a 400 — so listing a superset is safe.
_WORK_ITEM_TYPES = (
    "User Story",
    "Product Backlog Item",
    "Bug",
    "Task",
    "Feature",
    "Issue",
)


def _wiql_literal(value: str) -> str:
    """Escape a value for use inside a single-quoted WIQL string literal."""
    return value.replace("'", "''")


def _xml_escape(value: str) -> str:
    return (
        (value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _steps_xml(steps: list[dict[str, Any]]) -> str:
    """Serialize steps into the Azure DevOps TCM Steps XML format."""
    if not steps:
        return '<steps id="0" last="1"></steps>'
    parts = []
    for i, st in enumerate(steps, start=2):
        action = _xml_escape(st.get("a", ""))
        expected = _xml_escape(st.get("e", ""))
        parts.append(
            f'<step id="{i}" type="ActionStep">'
            f'<parameterizedString isformatted="true">{action}</parameterizedString>'
            f'<parameterizedString isformatted="true">{expected}</parameterizedString>'
            f"<description/></step>"
        )
    return f'<steps id="0" last="{len(steps) + 1}">{"".join(parts)}</steps>'


def _json_bytes(value: Any) -> bytes:
    """Serialize a JSON-patch body to bytes (so the json-patch content-type header sticks)."""
    return json.dumps(value).encode("utf-8")


class _WiqlError(RuntimeError):
    """A 400 from the WIQL endpoint, carrying ADO's validation message."""


def _classification_path_to_iteration(node_path: str) -> str:
    """Convert a classification-node path to a System.IterationPath/AreaPath value.

    ``\\Surency\\Iteration\\Release 1\\Sprint 3`` -> ``Surency\\Release 1\\Sprint 3``
    (strip the leading separator and the structural ``Iteration``/``Area`` segment).
    """
    parts = [p for p in node_path.split("\\") if p]
    if len(parts) >= 2 and parts[1] in ("Iteration", "Area"):
        parts = [parts[0]] + parts[2:]
    return "\\".join(parts)


def _strip_html(html: str) -> str:
    """Best-effort HTML -> plain text for ADO rich-text fields."""
    if not html:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return text.strip()


def _split_ac(text: str) -> list[str]:
    """Split acceptance-criteria text into a list of criteria lines."""
    if not text:
        return []
    lines = [ln.strip("-• \t") for ln in text.splitlines()]
    return [ln for ln in lines if ln]


class AzureDevOpsAdapter(ProviderAdapter):
    kind = "ado"

    def __init__(self, config: dict, secrets: dict) -> None:
        super().__init__(config, secrets)
        self.org_url = (self.config.get("orgUrl") or self.config.get("org_url") or "").rstrip("/")
        self.project = self.config.get("project") or ""
        self.pat = self.secrets.get("pat") or ""

    def _client(self) -> httpx.Client:
        if not self.org_url:
            raise ProviderError("Azure DevOps orgUrl is not configured")
        if not self.pat:
            raise ProviderError("Azure DevOps PAT is not configured")
        token = base64.b64encode(f":{self.pat}".encode("utf-8")).decode("utf-8")
        return httpx.Client(
            base_url=self.org_url,
            headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
            timeout=30.0,
        )

    # -- Connectivity -----------------------------------------------------
    def test_connection(self) -> dict[str, Any]:
        try:
            with self._client() as client:
                resp = client.get(f"/_apis/projects?api-version={API_VERSION}")
                resp.raise_for_status()
                data = resp.json()
                return {
                    "ok": True,
                    "message": f"Connected to Azure DevOps ({data.get('count', 0)} projects visible)",
                    "detail": {"count": data.get("count", 0)},
                }
        except ProviderError as exc:
            return {"ok": False, "message": str(exc), "detail": {}}
        except httpx.HTTPStatusError as exc:
            return {
                "ok": False,
                "message": f"Azure DevOps returned {exc.response.status_code}",
                "detail": {"status_code": exc.response.status_code},
            }
        except httpx.HTTPError as exc:
            return {"ok": False, "message": f"Azure DevOps connection failed: {exc}", "detail": {}}

    # -- Read ---------------------------------------------------------------
    def list_projects(self) -> list[dict[str, Any]]:
        with self._client() as client:
            resp = client.get(f"/_apis/projects?api-version={API_VERSION}")
            resp.raise_for_status()
            data = resp.json()
            return [
                {"external_id": p["id"], "name": p["name"], "state": p.get("state", "")}
                for p in data.get("value", [])
            ]

    def list_sprints(self) -> list[dict[str, Any]]:
        """Enumerate the project's iterations via classification nodes.

        Uses the project-scoped iteration tree (no team required) and converts each
        node's classification path (``\\Project\\Iteration\\Sprint``) into the
        ``System.IterationPath`` form (``Project\\Sprint``) that WIQL expects.
        """
        project = self.project
        if not project:
            raise ProviderError("Azure DevOps project is not configured")
        with self._client() as client:
            resp = client.get(
                f"/{quote(project)}/_apis/wit/classificationnodes/iterations",
                params={"$depth": 10, "api-version": API_VERSION},
            )
            resp.raise_for_status()
            root = resp.json()

        sprints: list[dict[str, Any]] = []

        def walk(node: dict[str, Any]) -> None:
            for child in node.get("children", []) or []:
                attrs = child.get("attributes") or {}
                sprints.append(
                    {
                        "id": str(child.get("identifier") or child.get("id", "")),
                        "name": child.get("name", ""),
                        "path": _classification_path_to_iteration(child.get("path", "")),
                        "start_date": attrs.get("startDate"),
                        "finish_date": attrs.get("finishDate"),
                    }
                )
                walk(child)

        walk(root)
        return sprints

    def list_repos(self) -> list[dict[str, Any]]:
        """List the Git repositories in the configured ADO project."""
        project = self.project
        if not project:
            raise ProviderError("Azure DevOps project is not configured")
        with self._client() as client:
            resp = client.get(
                f"/{quote(project)}/_apis/git/repositories",
                params={"api-version": API_VERSION},
            )
            resp.raise_for_status()
            data = resp.json()
        repos: list[dict[str, Any]] = []
        for r in data.get("value", []):
            default_branch = (r.get("defaultBranch") or "").removeprefix("refs/heads/")
            repos.append(
                {
                    "name": r.get("name", ""),
                    "clone_url": r.get("remoteUrl", ""),
                    "web_url": r.get("webUrl", "") or r.get("remoteUrl", ""),
                    "default_branch": default_branch,
                }
            )
        return repos

    def list_work_item_metadata(self) -> dict[str, Any]:
        """Area paths (classification nodes), work item types + their states."""
        project = self.project
        if not project:
            return {"area_paths": [], "work_item_types": [], "states": [], "epics": []}
        area_paths: list[dict[str, Any]] = []
        types: list[str] = []
        states: set[str] = set()
        with self._client() as client:
            areas = client.get(
                f"/{quote(project)}/_apis/wit/classificationnodes/areas",
                params={"$depth": 10, "api-version": API_VERSION},
            )
            if areas.status_code < 400:

                def walk(node: dict[str, Any]) -> None:
                    for child in node.get("children", []) or []:
                        area_paths.append(
                            {
                                "id": str(child.get("identifier") or child.get("id", "")),
                                "name": child.get("name", ""),
                                "path": _classification_path_to_iteration(child.get("path", "")),
                            }
                        )
                        walk(child)

                walk(areas.json())

            wits = client.get(
                f"/{quote(project)}/_apis/wit/workitemtypes",
                params={"api-version": API_VERSION},
            )
            if wits.status_code < 400:
                for wit in wits.json().get("value", []):
                    name = wit.get("name", "")
                    if name:
                        types.append(name)
                    for st in wit.get("states", []) or []:
                        if st.get("name"):
                            states.add(st["name"])
        return {
            "area_paths": area_paths,
            "work_item_types": types,
            "states": sorted(states),
            "epics": [],
        }

    # Max work items pulled in a single sync — keeps sync responsive on large sprints.
    MAX_SYNC_ITEMS = 200

    def fetch_tickets(
        self,
        *,
        mode: str = "sprint",
        sprint: str | None = None,
        sprint_path: str | None = None,
        area_path: str | None = None,
        states: list[str] | None = None,
        work_item_types: list[str] | None = None,
        ticket_ids: list[str] | None = None,
        include_comments: bool = False,
        project: str | None = None,
    ) -> list[NormalizedTicket]:
        project = project or self.project
        if not project:
            raise ProviderError("Azure DevOps project is not configured")

        with self._client() as client:
            ids = self._query_work_item_ids(
                client,
                project,
                mode=mode,
                sprint=sprint,
                sprint_path=sprint_path,
                area_path=area_path,
                states=states,
                work_item_types=work_item_types,
                ticket_ids=ticket_ids,
            )
            if not ids:
                return []
            if len(ids) > self.MAX_SYNC_ITEMS:
                logger.warning(
                    "ADO sync capped at {} of {} work items", self.MAX_SYNC_ITEMS, len(ids)
                )
                ids = ids[: self.MAX_SYNC_ITEMS]
            items = self._get_work_items(client, ids)
            return [self._normalize(client, item, include_comments=include_comments) for item in items]

    def fetch_comments(self, ticket_external_id: str) -> list[dict[str, Any]]:
        try:
            wi_id = int(ticket_external_id)
        except (TypeError, ValueError):
            return []
        with self._client() as client:
            return self._fetch_comments(client, wi_id)

    def _query_work_item_ids(
        self,
        client: httpx.Client,
        project: str,
        *,
        mode: str,
        sprint: str | None,
        sprint_path: str | None,
        area_path: str | None = None,
        states: list[str] | None = None,
        work_item_types: list[str] | None = None,
        ticket_ids: list[str] | None,
    ) -> list[int]:
        if mode == "selected" and ticket_ids:
            return [int(tid) for tid in ticket_ids if str(tid).isdigit()]

        # Selected work-item types (or the default QA-relevant superset).
        type_list = work_item_types or list(_WORK_ITEM_TYPES)
        types = ", ".join(f"'{_wiql_literal(t)}'" for t in type_list)
        base_conditions = [
            f"[System.TeamProject] = '{_wiql_literal(project)}'",
            f"[System.WorkItemType] IN ({types})",
        ]
        # State filter: selected states, else exclude Removed.
        if states:
            state_list = ", ".join(f"'{_wiql_literal(s)}'" for s in states)
            base_conditions.append(f"[System.State] IN ({state_list})")
        else:
            base_conditions.append("[System.State] <> 'Removed'")
        # Area path filter (applies in every mode).
        if area_path:
            base_conditions.append(f"[System.AreaPath] UNDER '{_wiql_literal(area_path)}'")

        conditions = list(base_conditions)
        # Prefer the native iteration path from list_sprints; fall back to project\name.
        iteration = sprint_path or (f"{project}\\{sprint}" if sprint else None)
        if mode == "sprint" and iteration:
            conditions.append(f"[System.IterationPath] UNDER '{_wiql_literal(iteration)}'")
        elif mode == "assigned":
            conditions.append("[System.AssignedTo] = @Me")

        try:
            return self._run_wiql(client, project, conditions)
        except _WiqlError as exc:
            # The most common WIQL 400 is an iteration/area path that does not
            # exist in this project (e.g. a placeholder sprint name). Retry once
            # without the iteration filter so sync still returns the project's
            # work items; otherwise surface ADO's own error message.
            if mode == "sprint" and iteration:
                logger.warning(
                    "ADO WIQL rejected iteration filter ({}); retrying without sprint scope", exc
                )
                return self._run_wiql(client, project, base_conditions)
            raise ProviderError(f"Azure DevOps WIQL query failed: {exc}") from exc

    def _run_wiql(self, client: httpx.Client, project: str, conditions: list[str]) -> list[int]:
        wiql = {
            "query": (
                "SELECT [System.Id] FROM WorkItems WHERE "
                + " AND ".join(conditions)
                + " ORDER BY [System.ChangedDate] DESC"
            )
        }
        resp = client.post(
            f"/{quote(project)}/_apis/wit/wiql?api-version={API_VERSION}",
            json=wiql,
        )
        if resp.status_code == 400:
            try:
                message = resp.json().get("message") or resp.text
            except ValueError:
                message = resp.text
            raise _WiqlError(message.strip())
        resp.raise_for_status()
        data = resp.json()
        return [wi["id"] for wi in data.get("workItems", [])]

    def _get_work_items(self, client: httpx.Client, ids: list[int]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for i in range(0, len(ids), 200):
            batch = ids[i : i + 200]
            id_str = ",".join(str(i) for i in batch)
            resp = client.get(
                "/_apis/wit/workitems",
                params={
                    "ids": id_str,
                    "$expand": "relations",
                    "api-version": API_VERSION,
                },
            )
            resp.raise_for_status()
            items.extend(resp.json().get("value", []))
        return items

    def _normalize(
        self, client: httpx.Client, item: dict[str, Any], *, include_comments: bool = False
    ) -> NormalizedTicket:
        fields = item.get("fields", {})
        wi_id = item["id"]

        labels = [t.strip() for t in (fields.get("System.Tags") or "").split(";") if t.strip()]
        ac_html = fields.get("Microsoft.VSTS.Common.AcceptanceCriteria", "")
        comments = self._fetch_comments(client, wi_id) if include_comments else []
        attachments, linked_prs = self._parse_relations(item.get("relations", []))

        assigned_to = fields.get("System.AssignedTo") or {}
        if isinstance(assigned_to, dict):
            assignee = assigned_to.get("displayName", "")
        else:
            assignee = str(assigned_to)

        return NormalizedTicket(
            external_id=str(wi_id),
            provider_kind=self.kind,
            title=fields.get("System.Title", ""),
            work_item_type=fields.get("System.WorkItemType", "User Story"),
            status=fields.get("System.State", ""),
            priority=self._map_priority(fields.get("Microsoft.VSTS.Common.Priority")),
            assignee=assignee,
            sprint=(fields.get("System.IterationPath") or "").split("\\")[-1],
            area_path=fields.get("System.AreaPath") or "",
            description=_strip_html(fields.get("System.Description", "")),
            note="",
            labels=labels,
            acceptance_criteria=_split_ac(_strip_html(ac_html)),
            acceptance_criteria_html=ac_html or "",
            comments=comments,
            attachments=attachments,
            linked_prs=linked_prs,
        )

    @staticmethod
    def _map_priority(value: Any) -> str:
        try:
            n = int(value)
        except (TypeError, ValueError):
            return "Medium"
        if n <= 1:
            return "High"
        if n == 2:
            return "Medium"
        return "Low"

    def _fetch_comments(self, client: httpx.Client, wi_id: int) -> list[dict[str, Any]]:
        try:
            resp = client.get(
                f"/_apis/wit/workItems/{wi_id}/comments",
                params={"api-version": "7.1-preview.3"},
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            return []
        data = resp.json()
        result = []
        for c in data.get("comments", []):
            result.append(
                {
                    "who": c.get("createdBy", {}).get("displayName", ""),
                    "when": c.get("createdDate", ""),
                    "text": _strip_html(c.get("text", "")),
                }
            )
        return result

    def _parse_relations(self, relations: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        attachments: list[dict[str, Any]] = []
        linked_prs: list[dict[str, Any]] = []
        for rel in relations:
            rel_type = rel.get("rel", "")
            url = rel.get("url", "")
            attrs = rel.get("attributes", {})
            if rel_type == "AttachedFile":
                attachments.append({"name": attrs.get("name", url.rsplit("/", 1)[-1]), "size": ""})
            elif "PullRequest" in rel_type or "ArtifactLink" in rel_type and "PullRequest" in url:
                linked_prs.append(self._parse_pr_artifact(url))
        return attachments, linked_prs

    def _parse_pr_artifact(self, url: str) -> dict[str, Any]:
        """Turn an ADO pull-request artifact link into a friendly, clickable PR entry.

        The relation ``url`` is a vstfs artifact of the shape
        ``vstfs:///Git/PullRequestId/{projectId}%2F{repoId}%2F{prId}``. We take the
        segment after ``PullRequestId/``, URL-decode it and split on ``/`` to recover
        ``[projectId, repoId, prId]`` — ``prId`` is the real PR number. From those we
        build the PR's web URL (GUIDs resolve fine in ADO web URLs). This is purely
        string parsing — no extra per-PR API call during bulk sync.

        :param url: the relation's vstfs artifact url.
        :returns: ``{repo, num, title, status, url}``. ``repo`` stays empty (the repo
            name isn't in the artifact) and ``status`` is unknown without a lookup.
            Falls back to the old raw-segment behavior if the artifact doesn't match
            the expected shape (never crashes on unexpected input).
        """
        marker = "PullRequestId/"
        idx = url.find(marker)
        if idx != -1:
            parts = unquote(url[idx + len(marker):]).split("/")
            if len(parts) == 3 and all(parts):
                project_id, repo_id, pr_id = parts
                return {
                    "repo": "",
                    "num": pr_id,
                    "title": f"PR !{pr_id}",
                    "status": "",
                    "url": f"{self.org_url}/{project_id}/_git/{repo_id}/pullrequest/{pr_id}",
                }
        return {"repo": "", "num": url.rsplit("/", 1)[-1], "title": "", "status": "", "url": ""}

    # -- Write ------------------------------------------------------------
    def supports_attachments(self) -> bool:
        """Azure DevOps can take real file attachments (#696)."""
        return True

    def publish_comment(
        self,
        ticket_external_id: str,
        body: str,
        *,
        attachments: list[str] | None = None,
    ) -> str:
        """Post a comment, with each path in ``attachments`` uploaded to the work item.

        Uploading and *linking* are two calls, and both are needed (#696): the upload
        parks the bytes and returns a URL, and the relation is what makes the file show
        up under the work item's Attachments — and what makes the URL readable by
        anyone who can see the ticket. An uploaded-but-unlinked attachment is a URL
        nobody can find.

        Attachment failures degrade rather than abort. A comment that reaches the
        ticket without one screenshot is worth far more than no comment at all, and
        the body says which files failed so nobody goes looking for evidence that
        never arrived.
        """
        uploaded: list[tuple[str, str]] = []
        failed: list[str] = []
        for path in attachments or []:
            file = Path(path)
            try:
                url = self._upload_attachment(file)
                self._link_attachment(ticket_external_id, url, file.name)
                uploaded.append((file.name, url))
            except Exception as exc:  # noqa: BLE001 - see the docstring
                logger.warning("ADO: could not attach {}: {}", file.name, exc)
                failed.append(file.name)

        text = body
        if uploaded:
            links = "".join(f'<li><a href="{url}">{name}</a></li>' for name, url in uploaded)
            text = f"{body}<br/><b>Attached evidence</b><ul>{links}</ul>"
        if failed:
            text = f"{text}<br/><i>Could not attach: {', '.join(failed)}</i>"

        with self._client() as client:
            resp = client.post(
                f"/_apis/wit/workItems/{ticket_external_id}/comments",
                params={"api-version": "7.1-preview.3"},
                json={"text": text},
            )
            resp.raise_for_status()
            return str(resp.json().get("id", ""))

    def _upload_attachment(self, file: Path) -> str:
        """Upload one file and return its attachment URL.

        Project-scoped: an org-level attachment cannot be related to a work item in a
        project the caller did not name.
        """
        data = file.read_bytes()
        project = self.project
        prefix = f"/{project}" if project else ""
        with self._client() as client:
            resp = client.post(
                f"{prefix}/_apis/wit/attachments",
                params={"fileName": file.name, "api-version": API_VERSION},
                content=data,
                # Overrides the client's JSON default — the body is bytes, and ADO
                # answers 400 to a binary payload announced as JSON.
                headers={"Content-Type": "application/octet-stream"},
            )
            resp.raise_for_status()
            url = str(resp.json().get("url") or "")
        if not url:
            raise ProviderError(f"Azure DevOps returned no attachment URL for {file.name}")
        return url

    def _link_attachment(self, ticket_external_id: str, url: str, name: str) -> None:
        """Relate an uploaded attachment to the work item."""
        with self._client() as client:
            resp = client.patch(
                f"/_apis/wit/workitems/{ticket_external_id}",
                params={"api-version": API_VERSION},
                headers={"Content-Type": "application/json-patch+json"},
                json=[
                    {
                        "op": "add",
                        "path": "/relations/-",
                        "value": {
                            "rel": "AttachedFile",
                            "url": url,
                            "attributes": {"comment": f"Q-Agent evidence: {name}"},
                        },
                    }
                ],
            )
            resp.raise_for_status()

    def update_status(self, ticket_external_id: str, target_status: str) -> None:
        with self._client() as client:
            resp = client.patch(
                f"/_apis/wit/workitems/{ticket_external_id}?api-version={API_VERSION}",
                headers={"Content-Type": "application/json-patch+json"},
                json=[{"op": "add", "path": "/fields/System.State", "value": target_status}],
            )
            resp.raise_for_status()

    def list_test_cases(self, ticket_external_id: str | None = None) -> list[dict[str, Any]]:
        """List the project's 'Test Case' work items (id + title + state)."""
        if not self.project:
            return []
        with self._client() as client:
            try:
                ids = self._run_wiql(
                    client,
                    self.project,
                    [
                        f"[System.TeamProject] = '{_wiql_literal(self.project)}'",
                        "[System.WorkItemType] = 'Test Case'",
                        "[System.State] <> 'Removed'",
                    ],
                )
            except _WiqlError:
                return []
            if not ids:
                return []
            items = self._get_work_items(client, ids[: self.MAX_SYNC_ITEMS])
            return [
                {
                    "external_id": str(it["id"]),
                    "title": it.get("fields", {}).get("System.Title", ""),
                    "state": it.get("fields", {}).get("System.State", ""),
                }
                for it in items
            ]

    def create_test_case(
        self,
        ticket_external_id: str,
        *,
        title: str,
        precondition: str = "",
        steps: list[dict[str, Any]] | None = None,
        priority: str = "Medium",
        link: bool = True,
    ) -> dict[str, Any]:
        """Create an ADO 'Test Case' work item (with TCM steps) and relate it to the ticket."""
        if not self.project:
            raise ProviderError("Azure DevOps project is not configured")
        prio = {"High": 1, "Medium": 2, "Low": 3}.get(priority, 2)
        patch: list[dict[str, Any]] = [
            {"op": "add", "path": "/fields/System.Title", "value": title},
            {"op": "add", "path": "/fields/Microsoft.VSTS.Common.Priority", "value": prio},
            {"op": "add", "path": "/fields/Microsoft.VSTS.TCM.Steps", "value": _steps_xml(steps or [])},
        ]
        if precondition:
            patch.append(
                {"op": "add", "path": "/fields/System.Description", "value": _xml_escape(precondition)}
            )
        with self._client() as client:
            resp = client.post(
                f"/{quote(self.project)}/_apis/wit/workitems/$Test%20Case",
                params={"api-version": API_VERSION},
                headers={"Content-Type": "application/json-patch+json"},
                content=_json_bytes(patch),
            )
            if resp.status_code >= 400:
                raise ProviderError(f"ADO create test case failed ({resp.status_code}): {resp.text[:300]}")
            created = resp.json()
            tc_id = created["id"]
            web_url = (created.get("_links", {}).get("html", {}) or {}).get("href", "")

            linked = False
            if link:
                ticket_url = f"{self.org_url}/_apis/wit/workItems/{ticket_external_id}"
                rel = client.patch(
                    f"/_apis/wit/workitems/{tc_id}?api-version={API_VERSION}",
                    headers={"Content-Type": "application/json-patch+json"},
                    content=_json_bytes(
                        [
                            {
                                "op": "add",
                                "path": "/relations/-",
                                "value": {
                                    "rel": "System.LinkTypes.Related",
                                    "url": ticket_url,
                                    "attributes": {"comment": "Q-Agent generated test case"},
                                },
                            }
                        ]
                    ),
                )
                linked = rel.status_code < 400
        return {"external_id": str(tc_id), "url": web_url, "status": "Design", "linked": linked}


register("ado", AzureDevOpsAdapter)
