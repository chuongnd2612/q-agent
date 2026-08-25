/**
 * Wire types mirroring the backend Pydantic schemas (api/app/schemas.py).
 * All fields are camelCase — the backend serializes with a camelCase alias
 * generator. Keep this file in sync with docs/API-CONTRACT.md.
 */

export type ProviderKind = "ado" | "jira" | "github";

/** Providers split into two categories: work-item sources (tickets) vs
 * repository sources (code). A project binds one connection of each. */
export type ProviderCategory = "work_item" | "repository";

/** A single named connection under a provider kind (ADR 0006). `categories`
 * lists every capability the connection's kind provides — e.g. Azure DevOps
 * is `["work_item", "repository"]` — so a per-project picker offers it when
 * its capability is included. */
export interface ConnectionOut {
  id: number;
  kind: ProviderKind;
  categories: ProviderCategory[];
  name: string;
  connected: boolean;
  config: Record<string, string>;
  secretFields: string[];
  lastSync: string | null;
  lastTestedAt: string | null;
}

/** Grouped provider catalog entry: one kind with its N connections. */
export interface ProviderGroupOut {
  kind: ProviderKind;
  categories: ProviderCategory[];
  name: string;
  connectionCount: number;
  connectedCount: number;
  connections: ConnectionOut[];
}

/** Body for PUT /connections/{id}. Untouched secrets are omitted so the backend
 * keeps the existing encrypted value. */
export interface ConnectionUpdate {
  name?: string;
  config?: Record<string, string>;
  secrets?: Record<string, string>;
}

export interface TestConnectionResult {
  ok: boolean;
  message: string;
  detail: Record<string, unknown>;
}

export interface ProjectOut {
  id: number;
  /** Stable public identifier (#585). Address a project by this — routes, links
   *  and API calls — never by `name`, which collides across users (#583) and
   *  changes on rename. Optional only while the G1 name bridge is in place. */
  guid?: string | null;
  providerKind: ProviderKind;
  externalId: string;
  /** Display text. Not an identifier. */
  name: string;
  active: boolean;
  /** The EmeHub project this row mirrors, when it mirrors one (#587) — the hub's
   *  numeric id, used to deep-link its project screen. `null` when there is none,
   *  in which case the UI shows a generic hint rather than a broken link. */
  hubProjectId?: string | null;
  meta: Record<string, unknown>;
}

export interface KnowledgeRoute {
  path: string;
  description: string;
  authRequired?: boolean;
}
export interface KnowledgeSelector {
  screen: string;
  element: string;
  selector: string;
}
export interface KnowledgeBody {
  branch: string;
  stack: string[];
  architecture: string;
  domain: string;
  locator: string;
  assets: number;
  pageObjects: number;
  fixtures: number;
  utilities: string[];
  // NOTE: these mirror the raw stored knowledge JSON keys (snake_case), which the
  // API returns verbatim inside `knowledge`.
  base_url?: string;
  routes?: KnowledgeRoute[];
  selectors?: KnowledgeSelector[];
  auth?: { login_flow?: string; login_url?: string; storage_state?: string };
  environments?: Array<{ name: string; base_url: string; notes: string }>;
  business_entities?: string[];
  page_object_names?: string[];
  fixture_names?: string[];
}

// -------------------------------------------------------------- project config
export interface TestAccountOut {
  role: string;
  username: string;
  notes: string;
  hasPassword: boolean;
}
export interface TestAccountIn {
  role: string;
  username: string;
  password: string; // blank preserves the stored secret
  notes: string;
}
export interface EnvironmentCfg {
  name: string;
  baseUrl: string;
  notes: string;
}
export interface ProjectRepo {
  name: string;
  repoUrl: string;
  defaultBranch: string;
  localRepoPath: string;
  default: boolean;
}
export interface AvailableRepo {
  name: string;
  cloneUrl: string;
  webUrl: string;
  defaultBranch: string;
}
export interface AvailableReposOut {
  provider: string;
  repos: AvailableRepo[];
  error: string;
}
export interface RepoKnowledgeOut {
  name: string;
  repoUrl: string;
  defaultBranch: string;
  localRepoPath: string;
  default: boolean;
  status: KnowledgeStatus;
  confidence: number;
  version: string;
  needsRefresh: boolean;
  lastIndexed: string | null;
  docPath: string;
  lastError: string;
}
export interface ProjectConfigOut {
  key: string;
  name: string;
  baseUrl: string;
  repos: ProjectRepo[];
  localRepoPath: string;
  repoUrl: string;
  environments: EnvironmentCfg[];
  testAccounts: TestAccountOut[];
  extra: Record<string, string>;
  manualAuth: boolean;
  /** The work-item connection this project's tickets come from (ADR 0006). */
  workItemConnectionId: number | null;
  /** The repository connection this project's code lives on (ADR 0006). */
  repositoryConnectionId: number | null;
}
export interface ProjectConfigUpdate {
  baseUrl?: string;
  repos?: ProjectRepo[];
  localRepoPath?: string;
  repoUrl?: string;
  environments?: EnvironmentCfg[];
  testAccounts?: TestAccountIn[];
  extra?: Record<string, string>;
  manualAuth?: boolean;
  workItemConnectionId?: number | null;
  repositoryConnectionId?: number | null;
}

/** Saved manual-login session state for a project (GET/DELETE /projects/{key}/auth). */
export interface AuthState {
  exists: boolean;
  capturedAt: string | null;
  capturing: boolean;
}

// ---------------------------------------------------- shared namespace (ADR 0009)
/** One repo's (or the bare project's, when `repo` is blank) knowledge status
 * within a shared-catalog entry (`GET /shared/projects`). */
export interface SharedProjectKnowledgeOut {
  repo: string;
  status: KnowledgeStatus;
  confidence: number;
  version: string;
  lastIndexed: string | null;
}

/** A shared-namespace project the catalog lists for members to browse/clone. */
export interface SharedProjectOut {
  key: string;
  name: string;
  providerKind: string;
  hasConfig: boolean;
  baseUrl: string;
  repos: ProjectRepo[];
  workItemConnectionId: number | null;
  repositoryConnectionId: number | null;
  knowledge: SharedProjectKnowledgeOut[];
  alreadyCloned: boolean;
}

/** Admin: create/update the shared project shell + its config
 * (`POST /shared/projects/{key}`). */
export interface SharedProjectCreate {
  name?: string;
  providerKind?: string;
  externalId?: string;
  baseUrl?: string;
  repos?: ProjectRepo[];
  workItemConnectionId?: number | null;
  repositoryConnectionId?: number | null;
  environments?: EnvironmentCfg[];
  testAccounts?: TestAccountIn[];
  extra?: Record<string, string>;
  manualAuth?: boolean;
}

/** Summary of what `POST /shared/projects/{key}/clone` copied. */
export interface CloneResultOut {
  projectKey: string;
  projectsCloned: number;
  configCloned: boolean;
  knowledgeCloned: string[];
  artifactsCopied: string[];
  docPath: string;
  lastError: string;
}

export type KnowledgeStatus = "not_indexed" | "indexing" | "indexed" | "stale" | "error";

export interface ProjectKnowledgeOut {
  key: string;
  projectKey?: string;
  name: string;
  provider: string;
  repo: string;
  framework: string;
  status: KnowledgeStatus;
  confidence: number;
  version: string;
  needsRefresh: boolean;
  lastIndexed: string | null;
  knowledge: Partial<KnowledgeBody>;
  docPath: string;
  lastError?: string;
  /** Where the row came from (#603). `"local"` is a real knowledge base; `"hub"`
   *  is a status-only projection of EmeHub's project summary, appended by
   *  `GET /projects/knowledge` so the grid badge is right on first paint. A
   *  `"hub"` row has no `knowledge`, no `lastIndexed` and no `repo` — render it as
   *  a badge, never as a knowledge base. */
  source?: "local" | "hub";
}

/** Where an unmet setup item is fixed (#642). Stable keys, so routing stays the
 *  frontend's business — under hub management the fix lives in EmeHub, not here. */
export type ReadinessFix = "settings" | "project" | "hub" | "install-agent";

/** One prerequisite for a working run (#642).
 *
 *  `required` is settings-dependent: an unpaired Local Agent blocks nothing when
 *  runs execute on the server, and nagging about it would train the user to
 *  ignore the whole checklist. */
export interface ReadinessItem {
  key: string;
  ready: boolean;
  required: boolean;
  fix: ReadinessFix;
  detail: string;
  /** The setting's authority is somewhere Q-Agent cannot see (today: EmeHub), so
   *  its state is unknown rather than met or unmet (#651). Never a blocker —
   *  claiming "missing" about something we did not check is what made the
   *  Automation screen demand a Claude credential that was plainly working. */
  managed?: boolean;
}

export interface Readiness {
  /** True when every *required* item is met — the question the UI actually asks. */
  ready: boolean;
  hubManaged: boolean;
  items: ReadinessItem[];
}

/** One case that the last generation pass could not produce a spec for (#641). */
export interface GenerationFailure {
  caseId: number;
  code: string;
  message: string;
}

/** Why the previous generation pass produced nothing (#641). Durable, so a
 *  failure the user did not witness live is still answerable afterwards. */
export interface GenerationError {
  at: string;
  attempted: number;
  failures: GenerationFailure[];
}

export interface AutomationStatus {
  generating: boolean;
  /** `null` once a pass completes with no failures. */
  lastError?: GenerationError | null;
}

// ----------------------------------------------------- DOM exploration (ADR 0010)
/** What the exploration agent should find — a blocked case's screen/goal. */
export interface ExploreTarget {
  ticket?: string;
  screen?: string;
  goal?: string;
}

/** Body for `POST /projects/{key}/repos/{repo}/explore` (ADR 0010 §7). */
export interface ExploreRequest {
  target: ExploreTarget;
  runId?: number;
  caseId?: number;
  allowStateChanging?: boolean;
}

/** Immediate response — the session started; poll/WS for progress. */
export interface ExploreStartOut {
  started: boolean;
  sessionId: string;
}

/** Navigation-survival poll: whether a session is in-flight for this repo, plus
 * the latest terminal result summary once one has completed. */
export interface ExploreStatus {
  exploring: boolean;
  sessionId: string | null;
  stopReason?: string | null;
  stepsTaken?: number | null;
  wroteKb?: boolean | null;
  discoveredRoutes?: number | null;
  discoveredSelectors?: number | null;
}

/** A route the exploration agent observed on the live app. */
export interface DiscoveredRoute {
  path: string;
  description: string;
}

/** A selector the exploration agent verified against the live DOM, stamped with
 * the locator strategy that actually worked (`data-testid` → css → role → label). */
export interface DiscoveredSelector {
  screen: string;
  element: string;
  selector: string;
  strategy: string;
}

/** One entry in the ordered exploration log (also the shape of each
 * `explore.progress` WS step, minus the streaming budget fields). */
export interface ExploreLogEntry {
  step: number;
  reasoning: string;
  action: string;
  args: Record<string, unknown>;
  observedUrl: string;
}

/** The full outcome of one exploration session (mirrors `ExplorationResultOut`).
 * Not served by a poll endpoint — reconstructed from the WS stream + status. */
export interface ExplorationResult {
  discovered: { routes?: DiscoveredRoute[]; selectors?: DiscoveredSelector[] };
  log: ExploreLogEntry[];
  stopReason: string;
  stepsTaken: number;
  budgetSpent: { usd?: number; tokens?: number };
  wroteKb: boolean;
}

export interface KnowledgeBuildRequest {
  name?: string;
  provider?: string;
  repo?: string;
  framework?: string;
}

export interface PullRequestOut {
  repo: string;
  num: string;
  title: string;
  status: string;
  color: string;
  /** Web URL to open the PR in the provider (empty when unknown). */
  url: string;
}
export interface CommentOut {
  who: string;
  ini: string;
  role: string;
  when: string;
  text: string;
}
export interface AttachmentOut {
  name: string;
  size: string;
}

export interface TicketOut {
  id: number;
  externalId: string;
  providerKind: ProviderKind;
  /** The work-item connection this ticket was synced from (ADR 0006). */
  connectionId: number | null;
  title: string;
  workItemType: string;
  status: string;
  priority: string;
  assignee: string;
  sprint: string;
  areaPath: string;
  /** Jira epic key/name (empty for ADO or unlinked tickets). */
  epic: string;
  labels: string[];
  acCount: number;
}

/** Paginated envelope for GET /tickets. */
export interface TicketPage {
  items: TicketOut[];
  total: number;
  page: number;
  pageSize: number;
}

export interface AreaPathOut {
  id: string;
  name: string;
  path: string;
}
export interface EpicOut {
  key: string;
  name: string;
}
export interface WorkItemMetadataOut {
  areaPaths: AreaPathOut[];
  workItemTypes: string[];
  states: string[];
  epics: EpicOut[];
}

export interface TicketDetailOut extends TicketOut {
  description: string;
  note: string;
  acceptanceCriteria: string[];
  /** Original provider AC as rich HTML — rendered read-only (sanitized) when the
   * criteria don't split cleanly into a numbered list (#225). */
  acceptanceCriteriaHtml: string;
  comments: CommentOut[];
  attachments: AttachmentOut[];
  linkedPrs: PullRequestOut[];
}

export interface SprintOut {
  id: string;
  name: string;
  path: string; // ADO iteration path (Project\Sprint) or Jira sprint id
  startDate?: string | null;
  finishDate?: string | null;
  state?: string | null;
}

/** A project available under a work-item connection's org
 * (`GET /connections/{id}/projects`) — populates the Sync dialog Project dropdown. */
export interface ConnectionProjectOut {
  externalId: string;
  name: string;
  state: string;
}

export interface SyncRequest {
  /** The work-item connection to sync from (ADR 0006). Falls back on the
   * backend to the project binding, then first-of-kind. */
  connectionId?: number;
  providerKind?: ProviderKind;
  /** Project override — sync from this project instead of the connection's
   * configured default. */
  project?: string | null;
  mode?: string;
  sprint?: string | null;
  sprintPath?: string | null;
  areaPath?: string | null;
  states?: string[];
  workItemTypes?: string[];
  ticketIds?: string[];
}

export interface TicketFilters {
  status?: string;
  assignee?: string;
  sprint?: string;
  areaPath?: string;
  states?: string;
  workItemTypes?: string;
  q?: string;
  /** Scope the list to a single work-item connection (ADR 0006). */
  connectionId?: number;
  providerKind?: ProviderKind;
  priority?: string;
  /** Jira epic key. */
  epic?: string;
  /** 1-based page number; defaults to 1 on the backend. */
  page?: number;
  /** Page size; defaults to 25 on the backend. */
  pageSize?: number;
}
/**
 * `GET /tickets/filter-options` — the query builder's dropdown values, read off
 * the caller's own ticket rows (#517).
 *
 * Not `WorkItemMetadataOut`: that one calls a provider adapter, and a mirrored
 * EmeHub connection holds no PAT by design (#501/#514), so on hub-managed
 * tickets it cannot answer at all.
 */
export interface TicketFilterOptions {
  workItemTypes: string[];
  states: string[];
  areaPaths: string[];
  sprints: string[];
  epics: string[];
  assignees: string[];
  priorities: string[];
  /** Present in the rows, but `GET /tickets` has no label filter — unused by the
   * builder, kept because it is part of "what is in your tickets". */
  labels: string[];
  ticketCount: number;
  /** True when the tickets in scope are EmeHub's to manage; the screen hides its
   * own Sync control on this, because Sync needs a local provider credential a
   * mirrored connection has not got. */
  hubManaged: boolean;
}

export interface SyncResult {
  synced: number;
  tickets: TicketOut[];
}

export interface TestStep {
  a: string;
  e: string;
}

/** A single reviewer-editable test-data entry (a labelled input value). */
export interface TestDatum {
  field: string;
  value: string;
}

export type ApprovalStatus = "pending" | "approved" | "rejected";

export interface TestCaseOut {
  id: number;
  runId: number;
  ticketExternalId: string;
  code: string;
  title: string;
  objective: string;
  precondition: string;
  steps: TestStep[];
  testData: TestDatum[];
  linkedAc: string[];
  priority: string;
  testType: string;
  automation: string;
  platform: string;
  duration: string;
  approval: ApprovalStatus;
  source: string;
  edited: boolean;
}

export interface TestCaseUpdate {
  title?: string;
  precondition?: string;
  steps?: TestStep[];
  testData?: TestDatum[];
  priority?: string;
  testType?: string;
  automation?: string;
}
export interface TestCaseCreate {
  ticketExternalId: string;
  title: string;
  precondition?: string;
  steps?: TestStep[];
  priority?: string;
  testType?: string;
  automation?: string;
  platform?: string;
}

export interface LinkedTestCaseOut {
  id: number;
  ticketExternalId: string;
  providerKind: string;
  externalId: string;
  title: string;
  status: string;
  url: string;
  linked: boolean;
  updatedAt: string | null;
}

export interface LinkTicketResult {
  ticketExternalId: string;
  providerKind: string;
  count: number;
  created: boolean;
  linked: boolean;
  local: boolean;
  error: string;
}

export interface LinkStatusOut {
  status: "idle" | "running" | "done";
  results: LinkTicketResult[];
}

export interface CreateLinkRequest {
  link?: boolean;
  ticketIds?: string[];
  dryRun?: boolean;
}

export type RunStatus =
  | "processing"
  | "review"
  | "sync"
  | "automation"
  | "executing"
  | "evidence"
  | "comment"
  | "done"
  | "cancelled"
  | "failed";

/** QA verdict from a run's latest execution — decoupled from the pipeline
 * `RunStatus`. Drives the headline outcome (see `runEffectiveStatus`). */
export type RunResult = "not_run" | "passed" | "failed" | "mixed";

export interface RunTicketOut {
  ticketExternalId: string;
  position: number;
  genStatus: string;
  repo: string;
  analysis: Record<string, unknown>;
}

export interface RunRepoOption {
  name: string;
  default: boolean;
  status: KnowledgeStatus;
}

export interface RunOut {
  id: number;
  code: string;
  name: string;
  scope: string;
  scopeLabel: string;
  framework: string;
  browser: string;
  env: string;
  workers: number;
  retryPolicy: number;
  status: RunStatus;
  createdAt: string;
  finishedAt?: string;
  cancelledAt?: string;
  failedStage?: string;
  ticketIds: string[];
  /** Number of test cases in the run. */
  caseCount: number;
  /** Cases in the latest execution (the "passed / N" denominator). */
  total: number;
  /** Passed cases in the latest execution. */
  passed: number;
  /** Pass rate (0..100) from the latest report; null until finalized. */
  passRate: number | null;
  /** QA verdict from the latest execution, independent of the pipeline `status`.
   * "not_run" until tests execute. See `runEffectiveStatus`. */
  result: RunResult;
}
export interface RunDetailOut extends RunOut {
  runTickets: RunTicketOut[];
}

export interface RunCreate {
  scope?: string;
  ticketIds?: string[];
  framework?: string;
  browser?: string;
  env?: string;
  workers?: number;
  retryPolicy?: number;
  sprint?: string | null;
  sprintPath?: string | null;
}

export type SpecStatus =
  | "draft"
  | "blocked"
  | "running"
  | "passed"
  | "failed"
  | "product_defect";

/** Which layer of the automation project a file belongs to (#537 doc §20's
 * ownership model): `page`/`component` carry app-UI knowledge, `fixture` test
 * setup, `data` scenario input, `util`/`config` generic plumbing, and `spec` the
 * business intent. Kept as a widened `string` on the wire so an unknown kind
 * from a newer server degrades into an "Other" group instead of breaking. */
export type ProjectFileKind =
  | "page"
  | "component"
  | "fixture"
  | "data"
  | "util"
  | "config"
  | "spec";

/** One file of the persistent automation project shipped alongside a spec.
 * `path` is project-relative (e.g. `pages/LoginPage.ts`). Read-only in the UI —
 * editing support files must route through the quality gate (#543). */
export interface ProjectFile {
  path: string;
  kind: string;
  code: string;
}

export interface AutomationSpecOut {
  id: number;
  testCaseId: number;
  filename: string;
  language: string;
  framework: string;
  code: string;
  status: string;
  blockReason: string;
  gateReport: string;
  /** The ticket's REUSE/EXTEND/CREATE automation plan as a JSON string (#544),
   * mirroring `gateReport`. `null` when the case was never planned (a legacy spec,
   * or a project whose planning failed) — the plan panel then renders nothing. */
  planReport?: string | null;
  /** The automation project's files, when this spec lives in one (#537). Absent
   * for legacy specs (`project_id IS NULL`) — the screen then renders exactly as
   * before, with no file list. */
  projectFiles?: ProjectFile[];
  /** The persistent git-backed automation project this spec lives in (#540).
   * `null`/absent for a legacy spec — which is also what makes the project
   * unexportable (#549), since there is no repo to push. */
  projectId?: number | null;
}

/** Prefill + readiness for exporting the automation project to a customer-owned
 * remote (#549). Read-only: fetching it pushes nothing. `credentialsError` is an
 * actionable sentence explaining why `hasCredentials` is false, so the UI can say
 * what to fix *before* the user triggers a push. */
export interface AutomationExportPreflight {
  projectId: number;
  projectSlug: string;
  projectKey: string;
  repo: string;
  /** Suggested branch — never the remote's default (the server refuses those). */
  branch: string;
  /** Suggested remote, credentials redacted. May be empty. */
  remoteUrl: string;
  commit: string | null;
  connection: string | null;
  hasCredentials: boolean;
  credentialsError: string | null;
  credentialsCode: string | null;
  /** False until a provider adapter can open a PR; the UI then reports the branch. */
  canOpenPullRequest: boolean;
}

/** Result of a user-triggered export. `remote` is always redacted, and `prUrl` is
 * `null` while no provider adapter can open a pull request. */
export interface AutomationExportResult {
  ok: boolean;
  projectId: number;
  branch: string;
  remote: string;
  commit: string;
  committed: boolean;
  pushed: boolean;
  upToDate: boolean;
  created: boolean;
  prUrl: string | null;
  detail: string;
}

/** Payload of the `automation.chat.reply` run-WS event: the successful result
 * of a chat-driven spec edit. `text` is Claude's prose explanation, `prevCode`
 * the code before the edit (for Undo), and `spec` the re-gated updated spec. */
export interface ChatReplyPayload {
  caseId: number;
  messageId: string;
  text: string;
  prevCode: string;
  spec: AutomationSpecOut;
}

/** Payload of the `automation.chat.error` run-WS event: a chat-edit that failed
 * (Claude error, gate failure, etc.). `messageId` correlates it to the pending
 * client-side message. */
export interface ChatErrorPayload {
  caseId: number;
  messageId: string;
  error: string;
}

export interface HealAttempt {
  attempt: number;
  status: "pass" | "fail";
  error: string;
  durationMs: number;
  outputTail: string;
  fixed: boolean;
  diff: string;
}

export interface HealReport {
  caseId: number;
  finalStatus: "pass" | "fail";
  maxAttempts: number;
  healedAt: string;
  attempts: HealAttempt[];
}

export interface AuditEventOut {
  id: string;
  ts: string;
  category: string;
  actor: string;
  actorType: "user" | "ai" | "system";
  action: string;
  target: string;
  ip: string;
  status: "success" | "warning" | "error";
  meta: string;
  /** Run this event belongs to (e.g. "RUN-202"); "" when not run-scoped (#394). */
  runCode: string;
  /** Structured extra detail for the expanded row (#396); null when none. */
  detail: AuditEventDetail | null;
}

/** Exploration step in an event's detail trail (#396). */
export interface AuditEventStep {
  n: number;
  action: string;
  target: string;
  reasoning: string;
  url: string;
  ok?: boolean | null;
  skipped?: boolean;
}

/** Structured extra detail carried by an audit event (currently exploration). */
export interface AuditEventDetail {
  stopReason?: string;
  wroteKb?: boolean;
  steps?: AuditEventStep[];
  routes?: { path: string; description?: string }[];
  selectors?: { screen: string; element: string; selector: string; strategy: string }[];
}

export interface AuditStats {
  eventsToday: number;
  aiActions: number;
  userActions: number;
  failures: number;
}

export interface BackendLogOut {
  ts: string;
  level: "info" | "warn" | "error" | "debug";
  service: string;
  message: string;
  durationMs: number | null;
  trace: string;
}

export interface BackendLogStats {
  logVolume: number;
  warnings: number;
  errors: number;
}

export type ExecCaseStatus = "pending" | "running" | "pass" | "fail" | "skipped";

export type FailureClass =
  | ""
  | "test_defect"
  | "product_defect"
  | "flaky"
  | "environment"
  | "timeout";

export interface EvidenceOut {
  id: number;
  kind: string;
  filename: string;
  path: string;
  sizeBytes: number;
  annotated: boolean;
  meta: Record<string, unknown>;
}

export interface ExecutionResultOut {
  id: number;
  testCaseId: number;
  ticketExternalId: string;
  caseCode: string;
  title: string;
  status: ExecCaseStatus;
  failureClass: string;
  durationMs: number;
  errorMessage: string;
  consoleLogs: Array<Record<string, unknown>>;
  networkLogs: Array<Record<string, unknown>>;
  evidence: EvidenceOut[];
}

/** Where an Execution runs — the server (legacy) or a paired Local Agent
 * device on the user's own machine. */
export type ExecutionTarget = "server" | "local-agent";

/** How approved cases become Playwright specs (#400) — generate blind from the
 * KB then heal, or drive the real app live via browser-harness and emit from
 * the verified DOM. */
export type AuthoringMode = "blind" | "live-harness";
export type HealMode = "classic" | "live-harness";
export type AuthoringLogVerbosity = "verbose" | "concise";

export interface ExecutionOut {
  id: number;
  runId: number;
  status: string;
  target: ExecutionTarget;
  env: string;
  browser: string;
  workers: number;
  total: number;
  passed: number;
  failed: number;
  progress: number;
  startedAt: string | null;
  finishedAt: string | null;
  log: string;
  results: ExecutionResultOut[];
}

/**
 * Live-authoring pause state for one case (`GET /cases/{id}/authoring`, #619).
 *
 * Fetched rather than derived from the WS stream because the `paused` event fires
 * once: a user who reloads mid-pause would otherwise see a dead spinner with no
 * way to continue the session their device is still holding a browser open for.
 */
export interface AuthoringStateOut {
  active: boolean;
  status: string;
  canPause: boolean;
  canContinue: boolean;
  pausePending?: boolean;
  /** False ⇒ Continue will run a FRESH guided pass, not `claude --resume`. */
  resumable?: boolean;
  guidancePending?: number;
  guidanceGiven?: number;
  /** The guidance turns already sent this session (#644), oldest first — so a
   *  user resuming a second time can see what they already said instead of
   *  repeating it. Optional: an older server sends only the counts above. */
  guidanceHistory?: string[];
  costUsdSoFar?: number;
  remainingBudgetUsd?: number;
  resumeCount?: number;
}

/** A paired Local Agent device (`GET /agent/devices`). */
export interface AgentDeviceOut {
  id: number;
  name: string;
  lastSeenAt: string | null;
  createdAt: string;
}

/** Response from `POST /agent/devices/pair-code` — a short-lived code the
 * user hands to `npx @q-agent/agent pair <code>` on their machine. */
export interface PairCodeOut {
  code: string;
  expiresIn: number;
}

export interface AnnotationShape {
  tool: string;
  x: number;
  y: number;
  w?: number;
  h?: number;
  x2?: number;
  y2?: number;
  text?: string;
  color?: string;
}

export interface ReportOut {
  id: number;
  runId: number;
  executionId: number | null;
  overallResult: string;
  passRate: number;
  passed: number;
  failed: number;
  durationS: number;
  env: string;
  data: Record<string, unknown>;
  createdAt: string;
}

export type PublishStatus = "draft" | "publishing" | "published" | "failed";
/** One evidence file a comment will attach when published (#696). A *plan*, not a
 * result — nothing is uploaded until publish. */
export interface CommentAttachment {
  caseCode: string;
  kind: string;
  filename: string;
  sizeBytes: number;
}

export interface TicketCommentOut {
  id: number;
  runId: number;
  ticketExternalId: string;
  providerKind: ProviderKind;
  body: string;
  status: PublishStatus;
  targetStatus: string;
  externalCommentId: string;
  errorMessage: string;
  attachments: CommentAttachment[];
}

export interface SettingsOut {
  parallel: number;
  retryFlaky: boolean;
  screenshotOnFail: boolean;
  video: boolean;
  maxCasesPerTicket: number;
  headless: boolean;
  autoAnnotate: boolean;
  neuralBackground: boolean;
  claudeModel: string;
  /** Per-action model overrides keyed by skill name (#175). Absent keys inherit
   * the built-in default / global model. */
  skillModels: Record<string, string>;
  /** Ticket concurrency for analyze+generate (#179). 0 = auto (3 on Postgres,
   * 1 on SQLite). */
  aiPipelineWorkers: number;
  weeklyTokenBudget: number;
  /** Default execution target for new runs — the server, or a paired Local
   * Agent on the user's machine. Configured on the Settings screen. */
  executionTarget: ExecutionTarget;
  /** Spec authoring mode (#400). "blind" = generate from the KB then heal
   * failures; "live-harness" = drive the real app via browser-harness to
   * discover real selectors, then emit the spec. Configured on Settings. */
  authoringMode: AuthoringMode;
  /** Self-heal engine (#428). "classic" = generate a fix from the failure + DOM
   * then re-run Playwright; "live-harness" = drive the real app via browser-harness
   * (reusing the live-authoring pipeline), seeded with the failing spec + error. */
  healMode: HealMode;
  /** Per-session Claude $ ceiling for a live browser-harness run — shared by live
   * authoring and live self-heal (#430). Raise it when a heal/author needs to
   * create data + drive a long flow. */
  authoringCostBudgetUsd: number;
  /** Verbosity of the live-authoring step trail (#400). "concise" shows only
   * user-readable lines (Claude narration + phase status); "verbose" also shows
   * the raw browser-harness/Bash tool calls. Presentation-only. */
  authoringLogVerbosity: AuthoringLogVerbosity;
  /** Global spec quality-gate toggle. When false, spec generation/edit/heal skip
   * the placeholder/invented-reference gate, the AI reviewer and the parse check,
   * accepting every generated spec as runnable. */
  gateEnabled: boolean;
}
export type SettingsUpdate = Partial<SettingsOut>;

/* ── Auth (ADR 0007) ─────────────────────────────────────────────────────
 * camelCase wire shapes for the multi-user auth vertical. The durable
 * credential is an httpOnly refresh cookie; the access token is in-memory. */

export type UserRole = "admin" | "member";

/** The authenticated principal (GET /auth/me, embedded in login/refresh). */
export interface User {
  id: number;
  email: string;
  firstName: string;
  lastName: string;
  role: UserRole;
  isActive: boolean;
  /** Whether the user has an active TOTP (authenticator app) enrollment. */
  totpEnabled: boolean;
  /** Stamped on successful login/refresh (#95); `null` if never. */
  lastActive: string | null;
}

/** `User` plus admin-only fields — GET /auth/users (#95). */
export interface AdminUser extends User {
  /** "personal" (has own credential), "shared" (falls back to the shared
   * credential), or "none" (nothing resolves for this user). */
  credentialSource: "personal" | "shared" | "none";
}

/** One active refresh session for the profile "Active sessions" list. */
export interface AuthSession {
  id: string;
  userAgent: string;
  ip: string;
  lastSeenAt: string;
  /** True for the session backing the current browser. */
  current: boolean;
}

/** Response to `POST /auth/users/invite` (#94) — the invited user plus the
 * set-password token. Always present (#673): nothing is emailed, so the admin
 * hands the resulting `/forgot?token=…` link to the invitee themselves. */
export interface InviteUserResponse {
  user: User;
  resetToken: string;
}

/** A minted access token plus its principal (login success / refresh). */
export interface AuthTokens {
  accessToken: string;
  user: User;
}

/** POST /auth/login → either a session, or an MFA challenge to complete. */
export type LoginResponse = AuthTokens | { mfaRequired: true; mfaToken: string };

/** TOTP enrollment material returned by POST /auth/2fa/setup. */
export interface TwoFactorSetup {
  secret: string;
  otpauthUri: string;
}

/** A single rolling usage window (session or week) for the top-bar panel. */
export interface UsageWindow {
  costUsd: number; // spend in this window, USD
  tokens: number; // total tokens in this window
  requests: number; // request count in this window
  resetsAt: string; // ISO (UTC); render in local tz
  pctUsed: number; // plan-limit % used (from the CLI's /usage); -1 = unknown
  resetLabel: string; // authoritative reset text from the CLI (e.g. "Jul 7, 3:20am (Asia/Saigon)"); "" = none
}

/** Per-model usage rollup for the panel's "By model" list. */
export interface ByModelUsage {
  model: string; // "claude-sonnet-5"
  modelLabel: string; // "Claude Sonnet 5"
  input: number;
  output: number;
  cacheRead: number;
  cacheWrite: number;
  costUsd: number;
}

/** Claude usage stats for the top-bar chip + panel (GET /ai/stats). */
export interface ClaudeStats {
  model: string; // "claude-sonnet-5"
  modelLabel: string; // "Claude Sonnet 5"
  operational: boolean;
  ctxWindow: string; // "200K"
  session: UsageWindow; // current rolling session
  week: UsageWindow; // current rolling week
  breakdown: { input: number; output: number; cacheRead: number; cacheWrite: number };
  byModel: ByModelUsage[];
  limitsStatus: "loading" | "ready" | "unavailable"; // state of the CLI /usage % fetch
  /** Signed-in user's own DB-recorded usage (#95). Drives the weekly-budget
   * fallback bar when the CLI plan-limit % (`limitsStatus`) is unavailable. */
  own?: { costMonth: number; weekTokens: number; weekBudget: number };
}

/** One AI process (ticket-analysis phase, automation, etc.) and its token spend. */
export interface RunAiProcess {
  key: string; // stable process kind ("analyze" | "generate" | "automation" | …)
  name: string; // display label
  meta: string; // sub-line (e.g. "12 tickets · 34 cases")
  input: number; // input tokens
  output: number; // output tokens
  tokens: number; // total tokens
  costUsd: number; // spend in USD
}

/** One ticket's AI usage within a run, with its per-process sub-rows. */
export interface RunAiTicket {
  ticketExternalId: string; // "" == run-level (calls with no ticket attribution)
  input: number;
  output: number;
  tokens: number;
  costUsd: number;
  processes: RunAiProcess[]; // this ticket's processes, sorted by costUsd desc
}

/** Per-process AI usage + cost for a run (GET /runs/{id}/ai-usage). */
export interface RunAiUsage {
  runId: number;
  modelLabel: string; // "Claude Sonnet 4.6"
  totalCostUsd: number;
  totalTokens: number;
  processes: RunAiProcess[]; // flat, sorted by costUsd desc; [] if none
  tickets: RunAiTicket[]; // grouped by ticket, cost desc; run-level ("") last
}

/** Evidence grouped-by-ticket response for GET /runs/{id}/evidence. */
export interface EvidenceGrouped {
  tickets: Array<{
    id: string;
    title: string;
    pass: number;
    fail: number;
    /** Approved, automatable cases on the ticket — the denominator for "passed". */
    approved: number;
    provGlyph: string;
    provColor: string;
    statusLabel: string;
  }>;
  byTicket: Record<string, ExecutionResultOut[]>;
}

/** Claude CLI activity (observability). */
export interface AiCall {
  id: number;
  label: string;
  skill?: string | null;
  status: "running" | "ok" | "error";
  startedAt: string;
  durationMs?: number;
  error?: string;
}
export interface AiActivity {
  running: AiCall[];
  recent: AiCall[];
}

/** Claude CLI credentials status (#95) — GET /ai/credentials. Never carries the
 * token itself; `mode` is which credential is actually effective for the
 * signed-in user (own beats shared). */
export interface ClaudeCredentialsStatus {
  hasOwn: boolean;
  hasShared: boolean;
  mode: "own" | "shared" | "none";
  own: ClaudeCredentialsMeta | null;
  shared: ClaudeCredentialsMeta | null;
}

/** Per-credential metadata parsed from an uploaded `.credentials.json`. Never
 * carries the token itself. */
export interface ClaudeCredentialsMeta {
  /** "active" | "expired" — "expired" once a real call reported the token dead. */
  status: string;
  /** Account identity from the CLI's .claude.json — null until a call has run. */
  accountEmail: string | null;
  accountOrg: string | null;
  subscriptionType: string | null;
  expiresAt: string | null; // ISO
  scopes: string[];
  lastRefreshed: string | null; // ISO — the row's updated_at
  /** Active users with no own credential — only populated for the shared row. */
  assignedUsers: number | null;
}

/** Result of POST /ai/credentials/test — a real minimal Claude call. */
export interface ClaudeCredentialsTestResult {
  ok: boolean;
  result: "ok" | "invalid" | "no_credential" | "error";
  message: string;
}

/** Body for PUT /ai/credentials and PUT /ai/credentials/shared — the raw
 * contents of a Claude CLI `.credentials.json` file. */
export interface ClaudeCredentialsUpload {
  credentials: string;
  label?: string;
}

/** WebSocket progress message shape. */
export interface ProgressEvent {
  event: string;
  runId: string;
  payload: Record<string, unknown>;
}


/** Claude credential as EmeHub resolves it (#512). Sanitised server-side: the
 * credential material is never included. `available: false` means the hub could
 * not be consulted (flag off, no hub session, hub down) — not an error. */
export interface HubClaudeCredential {
  available: boolean;
  source?: string | null;
  status?: string | null;
  label?: string | null;
  expiresAt?: string | null;
  daysLeft?: number | null;
  scopes?: string[] | null;
  subscriptionType?: string | null;
}
