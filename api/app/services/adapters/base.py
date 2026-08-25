"""Abstract provider adapter interface.

Every provider (ADO / Jira / GitHub) implements this contract so the sync and
publish layers stay provider-agnostic. Adapters talk to the **real** REST APIs
via httpx; there is no mock path (ADR 0001).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ProviderError(RuntimeError):
    """Raised on adapter configuration or upstream API failures."""


class NormalizedTicket(dict):
    """A provider-agnostic ticket dict.

    Expected keys (all optional except external_id/title):
      external_id, provider_kind, title, work_item_type, status, priority,
      assignee, sprint, area_path, epic, description, note, labels(list[str]),
      acceptance_criteria(list[str]), acceptance_criteria_html(str),
      comments(list[dict]), attachments(list[dict]), linked_prs(list[dict]).
    """


class ProviderAdapter(ABC):
    """Base class for provider integrations."""

    kind: str = ""

    def __init__(self, config: dict, secrets: dict) -> None:
        self.config = config or {}
        self.secrets = secrets or {}

    # -- Connectivity -----------------------------------------------------
    @abstractmethod
    def test_connection(self) -> dict[str, Any]:
        """Verify credentials/reachability. Returns {ok, message, detail}."""

    # -- Read -------------------------------------------------------------
    @abstractmethod
    def list_projects(self) -> list[dict[str, Any]]:
        """Return [{external_id, name, ...}] for connectable projects."""

    @abstractmethod
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
        """Fetch and normalize tickets for the given selection mode.

        ``sprint`` is the human name; ``sprint_path`` is the provider-native
        identifier from :meth:`list_sprints` (ADO iteration path / Jira sprint id)
        and is preferred when present.

        ``project`` overrides the connection's configured default project when
        provided (Sync dialog Project dropdown); providers without a project
        concept ignore it.

        ``include_comments`` is False for bulk sync (comments would require one
        extra request per ticket — an N+1 that makes sync crawl); comments are
        loaded lazily on the ticket-detail view via :meth:`fetch_comments`.
        """

    def fetch_comments(self, ticket_external_id: str) -> list[dict[str, Any]]:
        """Fetch a single ticket's comments on demand. Default: none."""
        return []

    def list_sprints(self) -> list[dict[str, Any]]:
        """Return the project's sprints/iterations as [{id, name, path, ...}].

        Default: none (e.g. GitHub). Providers with iterations override this.
        """
        return []

    def list_work_item_metadata(self) -> dict[str, Any]:
        """Return filter options: {area_paths[], work_item_types[], states[]}.

        Default: empty. Providers override with project-specific metadata.
        """
        return {"area_paths": [], "work_item_types": [], "states": []}

    def list_repos(self) -> list[dict[str, Any]]:
        """Return the git repositories in the configured project/org.

        Each entry: ``{name, clone_url, default_branch, web_url}``. Used by the
        Project Details page to let a user pick which repos a project owns (an
        ADO/GitHub project can hold many). Default: none.
        """
        return []

    # -- Write ------------------------------------------------------------
    def supports_attachments(self) -> bool:
        """Whether this provider can take real file attachments (#696).

        Default **False**, and that is the honest default: every adapter's
        ``publish_comment`` has always *accepted* an ``attachments`` argument and
        silently dropped it, so callers had no way to tell whether evidence had
        actually been attached — and the UI advertised attachments that never existed.
        An adapter that grows the capability overrides this; a caller that cannot
        attach says so in the comment instead of implying it did.
        """
        return False

    @abstractmethod
    def publish_comment(
        self,
        ticket_external_id: str,
        body: str,
        *,
        attachments: list[str] | None = None,
    ) -> str:
        """Post a comment to the work item. Returns the external comment id.

        ``attachments`` are absolute paths to upload alongside the comment, and are
        honoured only when :meth:`supports_attachments` is True.
        """

    def update_status(self, ticket_external_id: str, target_status: str) -> None:
        """Transition the work item's status (optional; override if supported)."""
        return None

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
        """Create a test case in the provider and (optionally) link it to the work item.

        Returns ``{external_id, url, status, linked}``. Override per provider;
        default raises since not every provider supports test cases.
        """
        raise ProviderError("Creating test cases is not supported for this provider")

    def list_test_cases(self, ticket_external_id: str | None = None) -> list[dict[str, Any]]:
        """List existing test cases in the provider (optionally scoped to a work item).

        Returns ``[{external_id, title, state}]``. Used to continue the existing
        numbering/naming when generating and to manage provider test cases in-app.
        Default: none.
        """
        return []
