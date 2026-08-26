"""Pydantic v2 schemas — the HTTP wire contract shared by backend + frontend.

Field names are camelCase on the wire (via alias) to match the TypeScript client
and the design's data shapes, while staying snake_case in Python.
"""

from __future__ import annotations

from datetime import datetime

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    """Base: populate from ORM attrs, serialize camelCase, accept either casing."""

    model_config = ConfigDict(
        from_attributes=True,
        alias_generator=to_camel,
        populate_by_name=True,
    )


# ---------------------------------------------------------------- Providers
class ConnectionOut(ApiModel):
    """A single named provider connection (secrets masked to field names only)."""

    id: int
    kind: str
    categories: list[str]  # work_item, repository — a kind may carry both
    name: str
    connected: bool
    config: dict = Field(default_factory=dict)
    secret_fields: list[str] = Field(default_factory=list)
    last_sync: datetime | None = None
    last_tested_at: datetime | None = None


class ProviderGroupOut(ApiModel):
    """A provider kind and its connections (grouped catalog for Settings)."""

    kind: str
    categories: list[str]
    name: str
    connection_count: int = 0
    connected_count: int = 0
    connections: list[ConnectionOut] = Field(default_factory=list)


class ConnectionCreate(ApiModel):
    """Create an empty connection under a provider kind."""

    name: str = ""


class ConnectionUpdate(ApiModel):
    """Patch a connection. Untouched secrets are omitted (not blanked)."""

    name: str | None = None
    config: dict[str, str] | None = None
    secrets: dict[str, str] | None = None  # plaintext in, encrypted at rest


class TestConnectionResult(ApiModel):
    ok: bool
    message: str
    detail: dict = Field(default_factory=dict)


# ---------------------------------------------------------------- Projects
class ProjectOut(ApiModel):
    id: int
    #: Stable public identifier (#585) — address a project by this, not by `name`.
    #: Names collide across users and change when a project is renamed; this does
    #: neither. Optional only while the G1 bridge is in place.
    guid: str | None = None
    provider_kind: str
    external_id: str
    #: Display text. Not an identifier.
    name: str
    active: bool
    #: The EmeHub project this row mirrors, when it mirrors one (#587). The SPA
    #: deep-links to `<hub web origin>/app/projects/{hubProjectId}` from the
    #: read-only settings tab; `None` means "no link we can complete", and the UI
    #: shows a generic hint instead of a broken one.
    hub_project_id: str | None = None
    meta: dict = Field(default_factory=dict)


class ConnectionProjectOut(ApiModel):
    """A single project available under a work-item connection's org.

    Populates the Sync dialog's Project dropdown — the org's projects for the
    chosen connection, so a sync can target a project other than the connection's
    configured default.
    """

    external_id: str
    name: str
    state: str = ""


class KnowledgeBody(ApiModel):
    """The learned knowledge base contents (what project-bootstrap produces).

    ``model_config`` allows extra keys so the richer, discovered fields
    (base_url, routes, selectors, auth, environments, business_entities, …)
    survive round-trips without each needing an explicit field here.
    """

    model_config = ConfigDict(
        from_attributes=True,
        alias_generator=to_camel,
        populate_by_name=True,
        extra="allow",
    )

    branch: str = "main"
    stack: list[str] = Field(default_factory=list)
    architecture: str = ""
    domain: str = ""
    locator: str = ""
    assets: int = 0
    page_objects: int = 0
    fixtures: int = 0
    utilities: list[str] = Field(default_factory=list)
    base_url: str = ""
    routes: list[dict] = Field(default_factory=list)
    selectors: list[dict] = Field(default_factory=list)
    auth: dict = Field(default_factory=dict)
    environments: list[dict] = Field(default_factory=list)
    business_entities: list[str] = Field(default_factory=list)


# ---------------------------------------------------------- Project config
class TestAccountIn(ApiModel):
    """A test account submitted from the Project Details page (password plaintext in)."""

    role: str = ""
    username: str = ""
    password: str = ""  # blank preserves the stored secret
    notes: str = ""


class TestAccountOut(ApiModel):
    """A test account returned to the UI — password is never included."""

    role: str = ""
    username: str = ""
    notes: str = ""
    has_password: bool = False


class EnvironmentCfg(ApiModel):
    name: str = ""
    base_url: str = ""
    notes: str = ""


class ProjectRepo(ApiModel):
    """A repository that belongs to a project (an ADO/GitHub project holds many)."""

    name: str
    repo_url: str = ""
    default_branch: str = ""
    local_repo_path: str = ""
    default: bool = False  # the repo automation targets by default


class ProjectConfigOut(ApiModel):
    key: str
    name: str = ""
    # Per-project provider bindings (ADR 0006).
    work_item_connection_id: int | None = None
    repository_connection_id: int | None = None
    base_url: str = ""
    repos: list[ProjectRepo] = Field(default_factory=list)
    # Legacy single-repo fields (kept for backward compatibility).
    local_repo_path: str = ""
    repo_url: str = ""
    environments: list[EnvironmentCfg] = Field(default_factory=list)
    test_accounts: list[TestAccountOut] = Field(default_factory=list)
    extra: dict = Field(default_factory=dict)
    # Capture a real (headed) browser login before running specs when no saved session exists.
    manual_auth: bool = False


class ProjectConfigUpdate(ApiModel):
    work_item_connection_id: int | None = None
    repository_connection_id: int | None = None
    base_url: str | None = None
    repos: list[ProjectRepo] | None = None
    local_repo_path: str | None = None
    repo_url: str | None = None
    environments: list[EnvironmentCfg] | None = None
    test_accounts: list[TestAccountIn] | None = None
    extra: dict | None = None
    manual_auth: bool | None = None


class AuthStateOut(ApiModel):
    """State of a project's saved manual-login session (storageState.json)."""

    exists: bool = False
    captured_at: datetime | None = None
    capturing: bool = False


class ProjectKnowledgeOut(ApiModel):
    key: str
    project_key: str = ""
    name: str
    provider: str = ""
    repo: str = ""
    framework: str = "Playwright"
    status: str = "not_indexed"
    confidence: int = 0
    version: str = "v1"
    needs_refresh: bool = False
    last_indexed: datetime | None = None
    knowledge: dict = Field(default_factory=dict)
    doc_path: str = ""
    last_error: str = ""
    #: Where this row came from (#603). `"local"` is a real `project_knowledge`
    #: row; `"hub"` is a **status-only** projection of the hub's project summary,
    #: served so the Projects grid badge is right on first paint without fanning
    #: out one hub call per repo. A `"hub"` row carries no `knowledge` blob, no
    #: `lastIndexed` and no repo, and is never persisted — read it as a badge, not
    #: as a knowledge base.
    source: str = "local"


class KnowledgeBuildRequest(ApiModel):
    name: str | None = None
    provider: str | None = None
    repo: str | None = None
    framework: str | None = None


# ------------------------------------------------------- Project repositories
class AvailableRepoOut(ApiModel):
    """A repo discovered from the project's provider (for the picker)."""

    name: str
    clone_url: str = ""
    web_url: str = ""
    default_branch: str = ""


class AvailableReposOut(ApiModel):
    provider: str = ""
    repos: list[AvailableRepoOut] = Field(default_factory=list)
    error: str = ""


class RepoKnowledgeOut(ApiModel):
    """A project's repo plus the status of its per-repo knowledge base."""

    name: str
    repo_url: str = ""
    default_branch: str = ""
    local_repo_path: str = ""
    default: bool = False
    status: str = "not_indexed"
    confidence: int = 0
    version: str = "v1"
    needs_refresh: bool = False
    last_indexed: datetime | None = None


# ------------------------------------------------------- DOM exploration (ADR 0010)
class ExploreTarget(ApiModel):
    """What the exploration agent should find — a blocked case's screen/goal."""

    ticket: str | None = None
    screen: str | None = None
    goal: str | None = None


class ExploreRequest(ApiModel):
    """Body for ``POST /projects/{key}/repos/{repo}/explore`` (ADR 0010 §7)."""

    target: ExploreTarget
    run_id: int | None = None
    case_id: int | None = None
    allow_state_changing: bool = False


class ExploreStartOut(ApiModel):
    """Immediate response — the session started; poll/WS for progress (ADR 0010 §7).

    ``mode`` distinguishes the dispatch path (``"local-agent"`` when the session
    was queued for a paired device, ``None``/``"server"`` for the in-process
    loop) so the SPA can label where exploration is running (epic #336)."""

    started: bool
    session_id: str
    mode: str | None = None


class ExploreStatusOut(ApiModel):
    """Navigation-survival poll: whether a session is in-flight for this repo, plus
    the latest terminal result summary when one has completed."""

    exploring: bool
    session_id: str | None = None
    stop_reason: str | None = None
    steps_taken: int | None = None
    wrote_kb: bool | None = None
    discovered_routes: int | None = None
    discovered_selectors: int | None = None


class ExplorationResultOut(ApiModel):
    """The full outcome of one exploration session (maps the
    :class:`app.services.exploration_agent.ExplorationResult` dataclass)."""

    discovered: dict = Field(default_factory=dict)
    log: list[dict] = Field(default_factory=list)
    stop_reason: str
    steps_taken: int
    budget_spent: dict = Field(default_factory=dict)
    wrote_kb: bool = False

    @classmethod
    def from_result(cls, result) -> "ExplorationResultOut":  # noqa: ANN001 - ExplorationResult
        """Build the wire model from an ``ExplorationResult`` dataclass instance."""
        return cls(
            discovered=result.discovered,
            log=result.log,
            stop_reason=result.stop_reason,
            steps_taken=result.steps_taken,
            budget_spent=result.budget_spent,
            wrote_kb=result.wrote_kb,
        )


# ---------------------------------------- Agent-driven exploration (epic #336)
class ExploreClaimOut(ApiModel):
    """The claim payload the paired agent gets from ``POST /agent/explore/next``
    (the frozen wire contract) — everything it needs to drive the loop locally."""

    session_id: str
    base_url: str
    origin: str
    target: ExploreTarget
    max_steps: int
    allow_state_changing: bool
    project_key: str
    repo: str
    run_id: int | None = None


class ExploreDecideRequest(ApiModel):
    """Body for ``POST /agent/explore/{id}/decide`` — the agent's current page
    state + action history, from which the server asks Claude for the next step."""

    observation: dict = Field(default_factory=dict)
    history: list[dict] = Field(default_factory=list)
    steps_taken: int = 0


class ExploreDecideStartOut(ApiModel):
    """Immediate response — the async decide job started; poll for its result."""

    job_id: str
    status: str = "running"


class ExploreDecideStatusOut(ApiModel):
    """Poll response for a decide job: ``running`` | ``done`` (with ``result``) |
    ``error``. ``result`` is ``{action, args, reasoning, stop?, stopReason?}``."""

    status: str
    result: dict | None = None
    error: str | None = None


class ExploreEventRequest(ApiModel):
    """Body for ``POST /agent/explore/{id}/events`` — a progress event to relay
    onto the run WebSocket (when the session has a run)."""

    event: str
    payload: dict = Field(default_factory=dict)


class ExploreFinalizeRequest(ApiModel):
    """Body for ``POST /agent/explore/{id}/finalize`` — the session's terminal
    outcome; ``discovered`` is KB-merged only when it carries observed data."""

    discovered: dict = Field(default_factory=dict)
    log: list[dict] = Field(default_factory=list)
    stop_reason: str | None = None
    steps_taken: int = 0


class ExploreFinalizeOut(ApiModel):
    """Finalize response — whether the observed discovery was written to the KB."""

    ok: bool = True
    wrote_kb: bool = False


# ------------------------------------------ Agent-driven live authoring (#400/403)
class AuthoringClaimOut(ApiModel):
    """Claim payload for ``POST /agent/authoring/next`` — everything the paired
    agent needs to drive live spec-authoring locally: it launches a headed,
    pre-authenticated Chrome, points its local ``browser-harness`` at it, and
    runs its local ``claude`` agentically with ``system_prompt`` +``task_prompt``
    (composed server-side, since the agent has no ``skills/`` dir), writing the
    spec + a ``discovered.json`` sidecar into a temp workspace."""

    session_id: str
    base_url: str
    origin: str
    project_key: str
    repo: str
    case_id: int
    run_id: int | None = None
    spec_filename: str
    # The automation project, so the agent can RUN the spec it just authored
    # before reporting it (#657). Same bundle shape the execution claim sends —
    # the verification has to go through the real execution path, because a
    # verification that differs from the real run can pass while the run fails,
    # which is the failure being fixed. ``None`` ⇒ nothing to stage, so the agent
    # cannot verify and says so instead of implying it checked.
    project: dict | None = None
    # Headless setting the real execution would use. Sent rather than guessed: a
    # headless run can fail a bot-protected app whose spec is perfectly good, and
    # a verification that disagrees with execution is worse than none.
    headless: bool = True
    sidecar_filename: str = "discovered.json"
    system_prompt: str
    task_prompt: str
    model: str
    max_budget_usd: float
    # Authoring log verbosity (#438) — mirrors the Settings value so the agent's
    # own AGENT LOG filters the same way the web trail does: "concise" hides the
    # raw tool/Bash step lines, "verbose" shows them.
    log_verbosity: str = "concise"
    # The run owner's effective Claude credential (.credentials.json content), so
    # the agent's local `claude` authenticates with the app's saved credential
    # instead of a separate `claude login` on the agent machine. Empty ⇒ the agent
    # falls back to its own local login. Sensitive — sent only to the owner's paired
    # device over the authenticated channel; the agent writes it locked-down and
    # deletes it with the job workspace.
    claude_credentials: str = ""


class AuthoringEventRequest(ApiModel):
    """Body for ``POST /agent/authoring/{id}/events`` — a progress event relayed
    onto the run WebSocket (when the session has a run)."""

    event: str
    payload: dict = Field(default_factory=dict)


class AuthoringFinalizeRequest(ApiModel):
    """Body for ``POST /agent/authoring/{id}/finalize`` — the authored spec code,
    the runtime-verified ``discovered`` routes/selectors, a short summary, and the
    Claude ``cost_usd`` the agent's agentic run spent (so it rolls into the run's
    cost breakdown — the agent-side Claude isn't otherwise visible server-side)."""

    code: str = ""
    discovered: dict = Field(default_factory=dict)
    summary: str = ""
    ok: bool = True
    cost_usd: float = 0.0
    # The `.credentials.json` content as it stands after the agent's `claude` run.
    # If the CLI rotated the OAuth token on the device, the server captures the
    # fresher token back into the store so the uploaded credential auto-rotates
    # (#cred-rotate). Empty ⇒ nothing to capture. Never logged.
    refreshed_credentials: str = ""


class AuthoringFinalizeOut(ApiModel):
    """Finalize response — whether a runnable spec was persisted."""

    ok: bool = True


# ------------------------------------- Pause / resume a live authoring session (#619)
class AuthoringEventOut(ApiModel):
    """Reply to ``POST /agent/authoring/{id}/events``.

    ``control`` piggybacks the user's Pause onto the channel the agent ALREADY
    polls (one progress post per Claude step), so pause is delivered within a step
    with no second poller and no process-memory flag. ``""`` means carry on;
    ``"pause"`` means stop Claude but keep Chrome, the workdir and
    ``CLAUDE_CONFIG_DIR`` alive and park.
    """

    ok: bool = True
    control: str = ""


class AuthoringPausedRequest(ApiModel):
    """Body for ``POST /agent/authoring/{id}/paused`` — the device confirming it parked.

    ``claude_session_id`` is Claude CLI's OWN session id, read off the
    ``--output-format stream-json`` envelope. It is the only value ``claude
    --resume`` accepts; the queue's ``session_id`` is Q-Agent's id and is useless
    for that. An EMPTY value is meaningful, not an error: it is precisely the
    signal that Continue must fall back to a fresh guided pass.

    ``cost_usd`` is the session total so far (the agent accumulates across passes),
    stored absolutely so a retried post cannot double-count the session budget.
    """

    claude_session_id: str = ""
    cost_usd: float = 0.0


class AuthoringPausedOut(ApiModel):
    """Pause acknowledgement — how much of the SESSION budget is left to resume with."""

    ok: bool = True
    status: str = "paused"
    remaining_budget_usd: float = 0.0


class AuthoringResumeOut(ApiModel):
    """Reply to the parked device's resume poll.

    ``action`` is one of ``wait`` (still paused — hold the browser open),
    ``resume`` (the user pressed Continue) or ``abort`` (tear the browser and
    workdir down and finalize: the pause expired, the session was stopped, or the
    session budget is spent).

    ``guidance`` is the turns not yet delivered; ``guidance_history`` is every turn
    ever given, which the device needs only on the FALLBACK path — a fresh Claude
    pass has no memory of earlier turns, a genuine ``--resume`` does.
    """

    action: str = "wait"
    reason: str = ""
    guidance: list[str] = Field(default_factory=list)
    guidance_history: list[str] = Field(default_factory=list)
    claude_session_id: str = ""
    remaining_budget_usd: float = 0.0
    resume_count: int = 0


class AuthoringPauseControlOut(ApiModel):
    """Reply to the user's Pause / Continue on the authoring trail.

    ``outcome`` is the service's verdict verbatim (``requested``,
    ``already-paused``, ``not-running``, ``resuming``, ``budget-exhausted``,
    ``expired``, ``not-found``) so the UI can say what actually happened instead
    of guessing from a bare 200.
    """

    ok: bool = True
    outcome: str = ""
    status: str = ""


class AuthoringGuidanceRequest(ApiModel):
    """Body for the user's Continue — the guidance typed in the spec chat."""

    guidance: str = ""


# ------------------------------------------------------- Shared namespace (#120)
class SharedProjectKnowledgeOut(ApiModel):
    """One repo's (or the bare project's, when ``repo`` is blank) knowledge status
    within a shared-catalog entry."""

    repo: str = ""
    status: str = "not_indexed"
    confidence: int = 0
    version: str = "v1"
    last_indexed: datetime | None = None


class SharedProjectOut(ApiModel):
    """A shared-namespace project the catalog lists for members to browse/clone."""

    key: str
    name: str
    provider_kind: str = ""
    has_config: bool = False
    base_url: str = ""
    repos: list[ProjectRepo] = Field(default_factory=list)
    work_item_connection_id: int | None = None
    repository_connection_id: int | None = None
    knowledge: list[SharedProjectKnowledgeOut] = Field(default_factory=list)
    already_cloned: bool = False


class SharedProjectCreate(ApiModel):
    """Admin: create/update the shared project shell + its config (ADR 0009 §2)."""

    name: str = ""
    provider_kind: str = ""
    external_id: str = ""
    base_url: str = ""
    repos: list[ProjectRepo] = Field(default_factory=list)
    # Connections used only to build shared knowledge — dropped on clone (ADR 0009 §4).
    work_item_connection_id: int | None = None
    repository_connection_id: int | None = None
    environments: list[EnvironmentCfg] = Field(default_factory=list)
    test_accounts: list[TestAccountIn] = Field(default_factory=list)
    extra: dict = Field(default_factory=dict)
    manual_auth: bool = False


class CloneResultOut(ApiModel):
    """Summary of what a clone copied (ADR 0009 §4)."""

    project_key: str
    projects_cloned: int = 0
    config_cloned: bool = False
    knowledge_cloned: list[str] = Field(default_factory=list)
    artifacts_copied: list[str] = Field(default_factory=list)
    doc_path: str = ""
    last_error: str = ""


# ---------------------------------------------------------------- Tickets
class PullRequestOut(ApiModel):
    repo: str
    num: str
    title: str
    status: str
    color: str = "#a78bfa"
    url: str = ""


class CommentOut(ApiModel):
    who: str
    ini: str = ""
    role: str = ""
    when: str = ""
    text: str


class AttachmentOut(ApiModel):
    name: str
    size: str = ""


class TicketOut(ApiModel):
    id: int
    external_id: str
    provider_kind: str
    connection_id: int | None = None
    title: str
    work_item_type: str = "User Story"
    status: str
    priority: str
    assignee: str = ""
    sprint: str = ""
    area_path: str = ""
    epic: str = ""
    labels: list[str] = Field(default_factory=list)
    ac_count: int = 0


class TicketPageOut(ApiModel):
    """Paged ``GET /tickets`` envelope — ``total`` is computed before limit/offset."""

    items: list[TicketOut] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 25


class TicketDetailOut(TicketOut):
    description: str = ""
    note: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    acceptance_criteria_html: str = ""
    comments: list[CommentOut] = Field(default_factory=list)
    attachments: list[AttachmentOut] = Field(default_factory=list)
    linked_prs: list[PullRequestOut] = Field(default_factory=list)


class SprintOut(ApiModel):
    id: str
    name: str
    path: str  # ADO iteration path (Project\Sprint) or Jira sprint id
    start_date: str | None = None
    finish_date: str | None = None
    state: str | None = None


class SyncRequest(ApiModel):
    # A work-item connection to sync from (ADR 0006). Falls back to the first
    # connection of ``provider_kind`` when omitted.
    connection_id: int | None = None
    provider_kind: str | None = None
    # Optional project override — when set, the adapter fetches from this project
    # instead of the connection's configured default (Sync dialog Project dropdown).
    project: str | None = None
    mode: str = "sprint"  # sprint | assigned | selected | all
    sprint: str | None = None
    sprint_path: str | None = None
    area_path: str | None = None
    states: list[str] = Field(default_factory=list)
    work_item_types: list[str] = Field(default_factory=list)
    ticket_ids: list[str] = Field(default_factory=list)


class AreaPathOut(ApiModel):
    id: str
    name: str
    path: str


class EpicOut(ApiModel):
    key: str
    name: str


class WorkItemMetadataOut(ApiModel):
    """Filter options for a provider's project (populates the query dropdowns)."""

    area_paths: list[AreaPathOut] = Field(default_factory=list)
    work_item_types: list[str] = Field(default_factory=list)
    states: list[str] = Field(default_factory=list)
    epics: list[EpicOut] = Field(default_factory=list)


class HubQueryRequest(ApiModel):
    """A clause query bound for EmeHub, plus the destination it runs against.

    ``query`` is passed to the hub **untouched**. It is the same shape the Tickets
    query builder already produces — ``{"clauses": [{field, operator, values}],
    "match": "all"|"any"}`` — and the hub validates it, refusing an unrunnable
    clause with the offending index rather than dropping it. Re-encoding it here
    would put a second, weaker validator in the path of the one that matters.
    """

    query: dict[str, Any] = Field(default_factory=dict)
    provider_kind: str | None = None
    connection_id: int | None = None
    project: str | None = None
    page: int = 1
    page_size: int = 50


class HubSyncRequest(ApiModel):
    """Ask EmeHub to pull work items from the provider into its store.

    Either a clause ``query`` or explicit ``ticket_ids``. The hub does the
    provider call with its own PAT, so no credential crosses (#503).
    """

    query: dict[str, Any] | None = None
    ticket_ids: list[str] = Field(default_factory=list)
    provider_kind: str | None = None
    connection_id: int | None = None
    project: str | None = None


class TicketFilterOptionsOut(ApiModel):
    """Distinct filter values read off the caller's OWN ticket rows (#517).

    The query builder's dropdowns are populated from here rather than from
    ``WorkItemMetadataOut``, because that one calls a provider adapter and a
    mirrored hub connection holds no PAT and never will (#501/#514) — so on the
    screen this exists to serve, it cannot answer at all.

    Reading the rows instead has a property the provider read does not: **every
    value offered is a value some ticket actually has**, so no clause the builder
    can build returns an empty list. The tradeoff, which the UI states: a value
    absent from the mirrored set is not offered.

    ``labels`` is returned for completeness of "what is in your rows"; ``GET
    /tickets`` has no label filter, so the builder offers no label clause.
    """

    work_item_types: list[str] = Field(default_factory=list)
    states: list[str] = Field(default_factory=list)
    area_paths: list[str] = Field(default_factory=list)
    sprints: list[str] = Field(default_factory=list)
    epics: list[str] = Field(default_factory=list)
    assignees: list[str] = Field(default_factory=list)
    priorities: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    #: How many of the caller's tickets the values were read from.
    ticket_count: int = 0
    #: True when the tickets in scope are EmeHub's to manage — a mirrored
    #: connection (no PAT, by design) or rows already reconciled to a hub ticket.
    #: The Tickets screen hides its own Sync control on this, because that path
    #: needs a local provider credential that a mirrored connection has not got.
    hub_managed: bool = False


class SyncResult(ApiModel):
    synced: int
    tickets: list[TicketOut] = Field(default_factory=list)


class TicketDeleteRequest(ApiModel):
    """Body for bulk local-delete (``POST /tickets/delete``): the external ids to
    remove. Deletion is LOCAL only — it never calls the provider, so a re-sync
    restores the tickets."""

    external_ids: list[str] = Field(default_factory=list)


class TicketDeleteResult(ApiModel):
    """Result of a bulk local-delete: how many Ticket rows were removed."""

    deleted: int


# ---------------------------------------------------------------- Test cases
class TestStep(ApiModel):
    a: str = ""
    e: str = ""


class TestCaseOut(ApiModel):
    id: int
    run_id: int
    ticket_external_id: str
    code: str
    title: str
    objective: str = ""
    precondition: str = ""
    steps: list[TestStep] = Field(default_factory=list)
    test_data: list[dict] = Field(default_factory=list)
    linked_ac: list[str] = Field(default_factory=list)
    priority: str = "Medium"
    test_type: str = "Functional"
    automation: str = "Playwright"
    platform: str = "Web"
    duration: str = "—"
    approval: str = "pending"
    source: str = "ai"
    edited: bool = False


class TestCaseUpdate(ApiModel):
    title: str | None = None
    precondition: str | None = None
    steps: list[TestStep] | None = None
    test_data: list[dict] | None = None
    priority: str | None = None
    test_type: str | None = None
    automation: str | None = None


class TestCaseCreate(ApiModel):
    ticket_external_id: str
    title: str
    precondition: str = ""
    steps: list[TestStep] = Field(default_factory=list)
    priority: str = "Medium"
    test_type: str = "Functional"
    automation: str = "Manual"
    platform: str = "Web"


class ApprovalUpdate(ApiModel):
    approval: str  # approved | rejected | pending


# ---------------------------------------------------------------- Linked test cases
class LinkedTestCaseOut(ApiModel):
    id: int
    ticket_external_id: str
    provider_kind: str
    external_id: str
    title: str
    status: str = "Design"
    url: str = ""
    linked: bool = False
    updated_at: datetime | None = None


class CreateLinkRequest(ApiModel):
    """Create approved test cases in the provider; link them when ``link`` is true.

    ``dry_run`` = local mode: create the LinkedTestCase rows locally with a
    ``LOCAL-`` marker and DO NOT write anything to the provider (avoids polluting a
    live project during local development).
    """

    link: bool = True
    ticket_ids: list[str] = Field(default_factory=list)  # empty = all tickets in the run
    dry_run: bool = False


class LinkTicketResult(ApiModel):
    ticket_external_id: str
    provider_kind: str
    count: int = 0
    created: bool = False
    linked: bool = False
    local: bool = False  # created locally only — provider was not touched
    error: str = ""


class LinkStatusOut(ApiModel):
    status: str = "idle"  # idle | running | done
    results: list[LinkTicketResult] = Field(default_factory=list)


# ---------------------------------------------------------------- Runs
class RunTicketOut(ApiModel):
    ticket_external_id: str
    position: int = 0
    gen_status: str = "queued"
    repo: str = ""
    analysis: dict = Field(default_factory=dict)


class RunRepoOptionOut(ApiModel):
    """A project repo offered as a work item's target, with its knowledge status."""

    name: str
    default: bool = False
    status: str = "not_indexed"


class RunTicketRepoUpdate(ApiModel):
    """Set a work item's target repo ("" resets it to the project default)."""

    repo: str = ""


class RunOut(ApiModel):
    id: int
    code: str
    name: str
    scope: str
    scope_label: str
    framework: str
    browser: str
    env: str
    workers: int
    retry_policy: int
    status: str
    created_at: datetime
    finished_at: datetime | None = None
    cancelled_at: datetime | None = None
    failed_stage: str | None = None
    ticket_ids: list[str] = Field(default_factory=list)
    # Aggregates for the runs list (attached in the router; default 0/None so
    # mutation responses that don't compute them still serialize).
    case_count: int = 0
    total: int = 0  # cases in the latest execution (the "/N" denominator)
    passed: int = 0  # passed in the latest execution
    pass_rate: float | None = None  # 0..100 from the latest report; None until finalized
    # QA verdict from the latest execution, decoupled from the pipeline `status`
    # (which only tracks stage/lifecycle). "not_run" until tests execute; then
    # "passed" / "failed" (>=1 fail) / "mixed" (some pass + some fail). Lets the UI
    # show the test outcome instead of conflating it with a post-execution
    # pipeline hiccup (see the "Incomplete" display state).
    result: str = "not_run"


class RunDetailOut(RunOut):
    run_tickets: list[RunTicketOut] = Field(default_factory=list)


class RunCreate(ApiModel):
    scope: str = "selected"  # single | selected | assigned | sprint
    ticket_ids: list[str] = Field(default_factory=list)
    framework: str = "Playwright"
    browser: str = "chromium"
    env: str = "Staging"
    workers: int = 4
    retry_policy: int = 2
    sprint: str | None = None
    sprint_path: str | None = None


# ---------------------------------------------------------------- Automation
class AutomationSpecOut(ApiModel):
    id: int
    test_case_id: int
    filename: str
    language: str = "TypeScript"
    framework: str = "Playwright"
    code: str = ""
    status: str = "draft"
    block_reason: str = ""
    gate_report: str = ""


class AutomationSpecUpdate(ApiModel):
    """Manual edits to a generated spec's source code (persisted + written to disk)."""

    code: str


class AutomationSpecRegenerate(ApiModel):
    """Optional free-text reviewer note steering a single-case spec regeneration.

    The comment is injected into the generation prompt as reviewer guidance and
    recorded in the audit log; it is never persisted on the spec row and cannot
    bypass the placeholder / invented-reference gate.
    """

    comment: str | None = None


class AutomationExportRequest(ApiModel):
    """A user-triggered export of the automation project to their own git remote (#549).

    Every field is chosen by the user — the target remote and the branch are never
    inferred, and nothing here has a server-side default that would let an export
    happen implicitly. ``branch`` is refused when it names the remote's default
    branch (or any mainline-shaped name); ``projectId`` is optional and only needed
    when a run's specs span more than one automation project.
    """

    remoteUrl: str
    branch: str
    projectId: int | None = None
    message: str | None = None


class SpecChatRequest(ApiModel):
    """A reviewer's chat instruction to edit the selected spec (AI chat panel).

    Claude really edits the spec (see ``spec_service.generate_chat_edit``); the
    edited code is re-gated and persisted like a manual edit. ``model`` optionally
    selects the Claude model; ``messageId`` lets the client correlate the async
    WS reply/error back to the placeholder message it optimistically rendered.
    """

    message: str
    model: str | None = None
    messageId: str | None = None


# ---------------------------------------------------------------- Execution
class EvidenceOut(ApiModel):
    id: int
    kind: str
    filename: str = ""
    path: str = ""
    size_bytes: int = 0
    annotated: bool = False
    meta: dict = Field(default_factory=dict)


class ExecutionResultOut(ApiModel):
    id: int
    test_case_id: int
    ticket_external_id: str
    case_code: str
    title: str = ""
    status: str
    failure_class: str = ""
    duration_ms: int = 0
    error_message: str = ""
    console_logs: list = Field(default_factory=list)
    network_logs: list = Field(default_factory=list)
    evidence: list[EvidenceOut] = Field(default_factory=list)


class ExecutionOut(ApiModel):
    id: int
    run_id: int
    status: str
    env: str
    browser: str
    workers: int
    total: int
    passed: int
    failed: int
    progress: int
    log: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    results: list[ExecutionResultOut] = Field(default_factory=list)


class ExecutionStart(ApiModel):
    workers: int | None = None
    env: str | None = None


# ---------------------------------------------------------------- Annotation
class AnnotationShape(ApiModel):
    tool: str  # rectangle | arrow | highlight | circle | text
    x: float
    y: float
    w: float = 0
    h: float = 0
    x2: float = 0
    y2: float = 0
    text: str = ""
    color: str = "#f43f5e"


class AnnotateRequest(ApiModel):
    shapes: list[AnnotationShape] = Field(default_factory=list)


# ---------------------------------------------------------------- Reports
class ReportOut(ApiModel):
    id: int
    run_id: int
    execution_id: int | None = None
    overall_result: str
    pass_rate: float
    passed: int
    failed: int
    duration_s: int
    env: str
    data: dict = Field(default_factory=dict)
    created_at: datetime


# ---------------------------------------------------------------- Comments / publish
class CommentAttachmentOut(ApiModel):
    """One evidence file this comment will attach when it is published (#696).

    A *plan*, not a result: nothing is uploaded until publish, so this is what the
    reviewer is approving. It replaces the two hardcoded `evidence.zip` / `trace.zip`
    chips the UI used to draw with no files behind them.
    """

    case_code: str = ""
    kind: str = ""
    filename: str = ""
    size_bytes: int = 0


class TicketCommentOut(ApiModel):
    id: int
    run_id: int
    ticket_external_id: str
    provider_kind: str
    body: str
    status: str
    target_status: str = ""
    external_comment_id: str = ""
    error_message: str = ""
    attachments: list[CommentAttachmentOut] = Field(default_factory=list)


class CommentPreviewOut(ApiModel):
    """The comment rendered the way its provider will show it (#707).

    HTML, deliberately: it is the same string the adapter posts, so the preview cannot
    drift from what is published — and a preview that drifts is worse than none.
    """

    html: str = ""


class CommentEdit(ApiModel):
    body: str | None = None
    target_status: str | None = None


class PublishRequest(ApiModel):
    ticket_ids: list[str] = Field(default_factory=list)  # empty = all


# ---------------------------------------------------------------- Settings
class SettingsOut(ApiModel):
    parallel: int = 4
    retry_flaky: bool = True
    screenshot_on_fail: bool = True
    video: bool = False
    max_cases_per_ticket: int = 8
    headless: bool = True
    auto_annotate: bool = True
    # Never write to a provider (#712) — see `settings_store.DEFAULTS` for why this is
    # a setting rather than a per-click choice, and why it is enforced server-side.
    dry_run: bool = False
    neural_background: bool = True
    claude_model: str = "claude-sonnet-5"
    # Per-action model overrides keyed by skill name (#175); {} = defaults/global.
    skill_models: dict[str, str] = Field(default_factory=dict)
    # Ticket concurrency for analyze+generate (#179); 0 = auto (3 Postgres/1 SQLite).
    ai_pipeline_workers: int = 0
    weekly_token_budget: int = 0
    # Default execution target for new runs (Local Agent feature — see
    # EXEC_TARGETS): "server" (legacy in-process runner) or "local-agent"
    # (queued for a paired device to claim).
    execution_target: str = "server"
    # Spec authoring mode (#400): "blind" (generate from KB + heal) or
    # "live-harness" (drive the real app via browser-harness, emit from live DOM).
    authoring_mode: str = "blind"
    # Self-heal engine (#428): "classic" (generate fix + re-run Playwright) or
    # "live-harness" (drive the real app via browser-harness, reusing live-authoring).
    heal_mode: str = "classic"
    # Per-session Claude $ ceiling for a live browser-harness run — shared by live
    # authoring and live self-heal (#430). Enforced via the CLI's --max-budget-usd.
    authoring_cost_budget_usd: float = 2.00
    # Verbosity of the live-authoring step trail in the UI (#400): "concise"
    # (Claude narration + phase status only) or "verbose" (also raw tool/Bash calls).
    authoring_log_verbosity: str = "concise"
    # Global spec quality-gate toggle. When False, spec generation/edit/heal skip
    # the placeholder/invented-reference gate, the AI automation-reviewer and the
    # playwright --list parse check, accepting every spec as runnable (#gate-toggle).
    gate_enabled: bool = True


class SettingsUpdate(ApiModel):
    parallel: int | None = None
    retry_flaky: bool | None = None
    screenshot_on_fail: bool | None = None
    video: bool | None = None
    max_cases_per_ticket: int | None = None
    headless: bool | None = None
    auto_annotate: bool | None = None
    dry_run: bool | None = None
    neural_background: bool | None = None
    claude_model: str | None = None
    skill_models: dict[str, str] | None = None
    ai_pipeline_workers: int | None = None
    weekly_token_budget: int | None = None
    execution_target: str | None = None
    authoring_mode: str | None = None
    heal_mode: str | None = None
    authoring_cost_budget_usd: float | None = None
    authoring_log_verbosity: str | None = None
    gate_enabled: bool | None = None


# ---------------------------------------------------------------- Auth (ADR 0007)
class UserOut(ApiModel):
    """Public shape of a user account (never carries password_hash/totp_secret)."""

    id: int
    email: str
    first_name: str = ""
    last_name: str = ""
    role: str = "member"
    is_active: bool = True
    totp_enabled: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_active: datetime | None = None  # stamped on login/refresh; null if never


class AdminUserOut(UserOut):
    """``UserOut`` plus admin-only fields for the workspace user list."""

    # "personal" (has own credential), "shared" (falls back to the shared
    # credential), or "none" (no Claude credential resolves for this user).
    credential_source: str = "none"


class LoginRequest(ApiModel):
    email: str
    password: str
    remember: bool = False


class LoginResponse(ApiModel):
    """Successful login, or an MFA challenge when totp is enabled.

    On success: ``{accessToken, user}``. When MFA is required:
    ``{mfaRequired: true, mfaToken}`` (and accessToken/user are null).
    """

    access_token: str | None = None
    user: UserOut | None = None
    mfa_required: bool = False
    mfa_token: str | None = None


class MfaLoginRequest(ApiModel):
    mfa_token: str
    code: str


class SsoCompleteRequest(ApiModel):
    """Body of ``POST /auth/sso/complete`` — the EmeHub bootstrap (#480).

    ``hub_token`` is the short-lived agent token minted by the hub's
    ``POST /auth/agent-token`` (``docs/HUB-INTEGRATION.md`` §2). ``next`` is the
    in-app path the caller wants to land on and is echoed back so the SPA has a
    single source of truth for the post-bootstrap navigation.
    """

    hub_token: str
    next: str | None = None
    #: True when this is a *renewal* of an existing SSO session rather than a
    #: sign-in (#531). The exchange is identical; only the audit trail differs —
    #: an access token ageing out is not a sign-in event and must not be logged as
    #: one, or the trail fills with entries nobody performed.
    silent: bool = False


class SsoCompleteResponse(LoginResponse):
    """Deliberately **login-shaped** (``{accessToken, user}``), plus ``next``.

    Returning the same body as ``/auth/login`` is what keeps the whole frontend
    auth stack — ``store/auth.ts``, ``lib/api.ts``'s 401→refresh→retry and
    ``RequireAuth`` — untouched by the hub integration: after this call the
    browser holds an ordinary Q-Agent session and nothing downstream knows or
    cares that it started at the hub.
    """

    next: str = "/"


class RefreshResponse(ApiModel):
    access_token: str
    user: UserOut


class RequestResetRequest(ApiModel):
    email: str


class RequestResetResponse(ApiModel):
    """Email delivery is a dev stub — ``token`` is only populated when not in prod."""

    ok: bool = True
    token: str | None = None


class ResetRequest(ApiModel):
    token: str
    password: str


class UpdateMeRequest(ApiModel):
    first_name: str | None = None
    last_name: str | None = None


class ChangePasswordRequest(ApiModel):
    current_password: str
    new_password: str


class TotpSetupResponse(ApiModel):
    secret: str
    otpauth_uri: str


class TotpCodeRequest(ApiModel):
    code: str


class TotpDisableRequest(ApiModel):
    code: str | None = None
    password: str | None = None


class SessionOut(ApiModel):
    id: str
    user_agent: str = ""
    ip: str = ""
    created_at: datetime | None = None
    last_seen_at: datetime | None = None
    expires_at: datetime | None = None
    current: bool = False


class AdminCreateUserRequest(ApiModel):
    email: str
    first_name: str = ""
    last_name: str = ""
    role: str = "member"
    password: str


class AdminUpdateUserRequest(ApiModel):
    role: str | None = None
    is_active: bool | None = None


class AdminInviteUserRequest(ApiModel):
    """Invite a teammate by email — no password; they set one via /auth/reset."""

    email: str
    first_name: str = ""
    last_name: str = ""
    role: str = "member"


class AdminInviteUserResponse(ApiModel):
    """The newly-invited user plus the set-password token they need.

    Always populated (#673). No email is ever sent — there is no mailer — so
    this token is the invited user's only route to a password, and the admin
    is expected to pass the ``/forgot?token=…`` link on by hand.
    """

    user: UserOut
    reset_token: str


class OkResponse(ApiModel):
    ok: bool = True


# --------------------------------------------------------- Claude credentials (#95)
class ClaudeCredentialsUpload(ApiModel):
    """Body for uploading/replacing a Claude CLI ``.credentials.json``.

    ``credentials`` is the raw file contents (JSON text) — never echoed back.
    """

    credentials: str
    label: str = ""


class ClaudeCredentialModeUpdate(ApiModel):
    """Body for switching the signed-in user's preferred credential mode.

    ``mode`` is ``"own"`` (use my uploaded personal credential) or ``"shared"``
    (prefer the workspace shared account without deleting my upload).
    """

    mode: str


class ClaudeCredentialsMetaOut(ApiModel):
    """Public metadata for one credential row — never the token itself."""

    # "active" | "expired" — "expired" is set when a real CLI call (or the test
    # endpoint) reported the token is no longer usable, so the UI can flag it.
    status: str = "active"
    # Account identity the CLI wrote to <config_dir>/.claude.json after auth —
    # populated once a call has run under the credential.
    account_email: str | None = None
    account_org: str | None = None
    subscription_type: str | None = None
    expires_at: datetime | None = None
    scopes: list[str] = Field(default_factory=list)
    last_refreshed: datetime | None = None  # the row's updated_at
    # Active users with no own credential (only meaningful for the shared row).
    assigned_users: int | None = None


class ClaudeCredentialsStatusOut(ApiModel):
    """Whether own/shared credentials exist, and which one is effective. Never
    carries the token itself."""

    has_own: bool = False
    has_shared: bool = False
    mode: str = "none"  # "own" | "shared" | "none"
    own: ClaudeCredentialsMetaOut | None = None
    shared: ClaudeCredentialsMetaOut | None = None


class ClaudeCredentialsTestOut(ApiModel):
    """Result of an on-demand credential test (a real minimal Claude call)."""

    ok: bool = False
    # "ok" | "invalid" | "no_credential" | "error"
    result: str = "error"
    message: str = ""
