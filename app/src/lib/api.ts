/**
 * Typed HTTP client for the Q-Agent backend. Thin wrapper over fetch — one
 * method per endpoint in docs/API-CONTRACT.md. Screens consume these through
 * TanStack Query hooks (see src/hooks/) using the keys in src/lib/queryKeys.ts.
 */

import { useAuth } from "@/store/auth";
import type {
  AdminUser,
  AgentDeviceOut,
  AiActivity,
  AnnotationShape,
  AuditEventOut,
  AuditStats,
  AuthSession,
  AuthState,
  AuthTokens,
  ClaudeStats,
  ClaudeCredentialsStatus,
  ClaudeCredentialsTestResult,
  ClaudeCredentialsUpload,
  AutomationSpecOut,
  AutomationStatus,
  BackendLogOut,
  BackendLogStats,
  CloneResultOut,
  CreateLinkRequest,
  ExecutionTarget,
  LinkedTestCaseOut,
  LinkStatusOut,
  EvidenceGrouped,
  EvidenceOut,
  ExecutionOut,
  ExploreRequest,
  ExploreStartOut,
  ExploreStatus,
  HealReport,
  PairCodeOut,
  InviteUserResponse,
  AvailableReposOut,
  ConnectionOut,
  ConnectionProjectOut,
  ConnectionUpdate,
  KnowledgeBuildRequest,
  ProjectConfigOut,
  ProjectConfigUpdate,
  ProjectKnowledgeOut,
  ProjectOut,
  RepoKnowledgeOut,
  ProviderGroupOut,
  ProviderKind,
  ReportOut,
  RunCreate,
  RunDetailOut,
  RunAiUsage,
  RunOut,
  RunRepoOption,
  RunTicketOut,
  SettingsOut,
  SettingsUpdate,
  SharedProjectCreate,
  SharedProjectOut,
  SprintOut,
  SyncRequest,
  SyncResult,
  TestCaseCreate,
  TestCaseOut,
  TestCaseUpdate,
  TestConnectionResult,
  TicketCommentOut,
  TicketDetailOut,
  TicketFilters,
  TicketOut,
  TicketPage,
  LoginResponse,
  TwoFactorSetup,
  User,
  UserRole,
  WorkItemMetadataOut,
} from "@/types/api";

// Default to the same-origin `/api` prefix, which the Vite dev proxy forwards
// to the backend (prefix stripped). Same-origin means no CORS and it works
// behind a single tunnel; the `/api` prefix keeps API calls from colliding
// with the SPA's own client routes (`/runs`, `/projects`, …). Override with
// `VITE_API_BASE` (e.g. an absolute `https://api.example.com`) when the API is
// served from a different origin.
export const API_BASE: string =
  (import.meta.env.VITE_API_BASE as string | undefined)?.replace(/\/$/, "") ?? "/api";

/** Absolute websocket base for `new WebSocket(...)`. When `API_BASE` is an
 * absolute http(s) URL, swap the scheme to ws(s). When it's a same-origin
 * relative prefix (the default `/api`), derive scheme + host from the current
 * page so the URL is absolute (relative WS URLs are invalid). */
function wsBase(): string {
  if (/^https?:\/\//.test(API_BASE)) return API_BASE.replace(/^http/, "ws");
  const proto = typeof location !== "undefined" && location.protocol === "https:" ? "wss:" : "ws:";
  const host = typeof location !== "undefined" ? location.host : "127.0.0.1:8787";
  return `${proto}//${host}${API_BASE}`;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Read a browser cookie by name (used for the `qagent_csrf` double-submit
 * token). Returns null when absent or in a non-DOM context. */
function getCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const escaped = name.replace(/([.$?*|{}()[\]\\/+^])/g, "\\$1");
  const match = document.cookie.match(new RegExp("(?:^|; )" + escaped + "=([^;]*)"));
  return match ? decodeURIComponent(match[1]) : null;
}

/** `/auth/*` calls are SAME-ORIGIN (relative path, via the Vite dev proxy /
 * same-host in prod) so the httpOnly refresh + CSRF cookies flow. They also
 * opt out of the silent 401→refresh retry to avoid recursion. */
function isAuthPath(path: string): boolean {
  return path === "/auth" || path.startsWith("/auth/");
}

/** Single in-flight refresh shared by all callers, so a burst of concurrent
 * 401s triggers exactly one `POST /auth/refresh`. */
let refreshInFlight: Promise<boolean> | null = null;

/** Set while an *explicit* logout is in progress. In-flight authenticated
 * requests 401 once the refresh cookie is cleared; without this, the 401
 * interceptor's hard redirect to /login would race ahead of the intentional
 * navigation to /signed-out. Auto-clears so genuine session-expiry redirects
 * resume. */
let loggingOut = false;
export function markLoggingOut(): void {
  loggingOut = true;
  setTimeout(() => {
    loggingOut = false;
  }, 4000);
}

function tryRefresh(): Promise<boolean> {
  if (!refreshInFlight) {
    refreshInFlight = api.auth
      .refresh()
      .then(({ accessToken, user }) => {
        useAuth.getState().setSession({ accessToken, user });
        return true;
      })
      .catch(() => false)
      .finally(() => {
        refreshInFlight = null;
      });
  }
  return refreshInFlight;
}

async function request<T>(path: string, init?: RequestInit, retried = false): Promise<T> {
  const authPath = isAuthPath(path);
  const url = authPath ? path : API_BASE + path;

  const token = useAuth.getState().accessToken;
  const csrf = getCookie("qagent_csrf");
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...((init?.headers as Record<string, string> | undefined) ?? {}),
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (csrf) headers["X-CSRF-Token"] = csrf;

  const res = await fetch(url, { ...init, credentials: "include", headers });

  // Silent recovery: on a 401 for a non-auth call, refresh the access token
  // once and replay the request. If refresh fails, the session is dead — clear
  // it and bounce to /login, then rethrow so the caller still sees the error.
  if (res.status === 401 && !authPath && !retried) {
    if (await tryRefresh()) return request<T>(path, init, true);
    // An explicit logout is orchestrating its own navigation to /signed-out —
    // stay inert so a background 401 doesn't flip the store to anon (which would
    // trip RequireAuth to /login) or hard-redirect over it.
    if (!loggingOut) {
      useAuth.getState().logout();
      const onPublicAuthRoute =
        typeof window !== "undefined" &&
        /^\/(login|signed-out|forgot)/.test(window.location.pathname);
      if (typeof window !== "undefined" && !onPublicAuthRoute) {
        window.location.assign("/login");
      }
    }
  }

  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.json();
      const raw = (body as { detail?: unknown }).detail;
      // FastAPI 422s put a list of validation objects in `detail` — not
      // user-facing, so only surface a plain string detail.
      if (typeof raw === "string") detail = raw;
    } catch {
      /* ignore non-JSON error bodies */
    }
    // Never throw an empty message: `res.statusText` is "" over HTTP/2 (and
    // behind the tunnel), which previously produced a blank error toast when the
    // body carried no string detail. Fall back to the status text, then a
    // status-coded default so the toast always says something.
    if (!detail.trim()) detail = res.statusText.trim() || `Request failed (HTTP ${res.status})`;
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

const get = <T>(p: string) => request<T>(p);
const post = <T>(p: string, body?: unknown) =>
  request<T>(p, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
const put = <T>(p: string, body?: unknown) =>
  request<T>(p, { method: "PUT", body: JSON.stringify(body ?? {}) });
const patch = <T>(p: string, body?: unknown) =>
  request<T>(p, { method: "PATCH", body: JSON.stringify(body ?? {}) });
const del = <T>(p: string) => request<T>(p, { method: "DELETE" });

function qs(params: Record<string, string | number | undefined>): string {
  const entries = Object.entries(params).filter(([, v]) => v != null && v !== "");
  if (!entries.length) return "";
  return "?" + entries.map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`).join("&");
}

export const api = {
  // health / observability
  // `/health` is the only one of these readable while anonymous (it's in the
  // backend's auth allowlist, `/capabilities` isn't) — hence `hubSsoEnabled`
  // riding along here for the login screen (#478).
  health: () => get<{ status: string; version: string; hubSsoEnabled: boolean }>("/health"),
  capabilities: () => get<{ claude: boolean; version: string }>("/capabilities"),
  aiActivity: () => get<AiActivity>("/ai/activity"),
  aiStats: (force = false) => get<ClaudeStats>(`/ai/stats${force ? "?refresh=true" : ""}`),
  aiWsUrl: () => `${wsBase()}/ws/ai${wsToken()}`,

  // Claude CLI credentials management (#95): own (per-user) + shared (admin-only).
  claudeCredentials: {
    status: () => get<ClaudeCredentialsStatus>("/ai/credentials"),
    // Real minimal Claude call under a credential — authoritative. `scope`
    // selects which: effective (default), the shared account, or own.
    test: (scope?: "effective" | "shared" | "own") =>
      post<ClaudeCredentialsTestResult>(`/ai/credentials/test${scope ? `?scope=${scope}` : ""}`),
    uploadOwn: (body: ClaudeCredentialsUpload) => put<void>("/ai/credentials", body),
    // Non-destructive switch between own/shared (keeps the uploaded token on file).
    setMode: (mode: "own" | "shared") => put<void>("/ai/credentials/mode", { mode }),
    deleteOwn: () => del<void>("/ai/credentials"),
    uploadShared: (body: ClaudeCredentialsUpload) => put<void>("/ai/credentials/shared", body),
    deleteShared: () => del<void>("/ai/credentials/shared"),
  },

  // auth (ADR 0007). SAME-ORIGIN relative paths so httpOnly refresh + CSRF
  // cookies flow (Vite proxy in dev; same host in prod) — do NOT prefix with
  // API_BASE.
  auth: {
    login: (body: { email: string; password: string; remember?: boolean }) =>
      post<LoginResponse>("/auth/login", body),
    loginMfa: (body: { mfaToken: string; code: string }) =>
      post<AuthTokens>("/auth/login/mfa", body),
    refresh: () => post<AuthTokens>("/auth/refresh"),
    logout: () => post<void>("/auth/logout"),

    me: () => get<User>("/auth/me"),
    updateMe: (body: Partial<Pick<User, "firstName" | "lastName" | "email">>) =>
      patch<User>("/auth/me", body),
    changePassword: (body: { currentPassword: string; newPassword: string }) =>
      post<void>("/auth/change-password", body),

    requestReset: (body: { email: string }) => post<void>("/auth/request-reset", body),
    reset: (body: { token: string; password: string }) => post<void>("/auth/reset", body),

    twofaSetup: () => post<TwoFactorSetup>("/auth/2fa/setup"),
    twofaEnable: (body: { code: string }) => post<void>("/auth/2fa/enable", body),
    twofaDisable: (body: { code: string }) => post<void>("/auth/2fa/disable", body),

    sessions: () => get<AuthSession[]>("/auth/sessions"),
    revokeSession: (id: string) => del<void>(`/auth/sessions/${encodeURIComponent(id)}`),
    revokeOthers: () => post<void>("/auth/sessions/revoke-others"),
    deleteMe: () => del<void>("/auth/me"),

    // admin — user management (#78 / #77 / #94)
    users: () => get<AdminUser[]>("/auth/users"),
    createUser: (body: {
      email: string;
      firstName: string;
      lastName: string;
      role: UserRole;
      password?: string;
    }) => post<User>("/auth/users", body),
    inviteUser: (body: { email: string; firstName?: string; lastName?: string; role: UserRole }) =>
      post<InviteUserResponse>("/auth/users/invite", body),
    updateUser: (
      id: number,
      body: Partial<{ firstName: string; lastName: string; role: UserRole; isActive: boolean }>,
    ) => patch<User>(`/auth/users/${id}`, body),
    deleteUser: (id: number) => del<void>(`/auth/users/${id}`),
  },

  // providers + connections (ADR 0006)
  listProviders: () => get<ProviderGroupOut[]>("/providers"),
  createConnection: (kind: ProviderKind, body: { name: string }) =>
    post<ConnectionOut>(`/providers/${kind}/connections`, body),
  updateConnection: (id: number, body: ConnectionUpdate) =>
    put<ConnectionOut>(`/connections/${id}`, body),
  deleteConnection: (id: number) => del<void>(`/connections/${id}`),
  testConnection: (id: number) => post<TestConnectionResult>(`/connections/${id}/test`),
  connectionProjects: (id: number) =>
    get<ConnectionProjectOut[]>(`/connections/${id}/projects`),
  connectionSprints: (id: number) => get<SprintOut[]>(`/connections/${id}/sprints`),
  connectionWorkItemMetadata: (id: number) =>
    get<WorkItemMetadataOut>(`/connections/${id}/work-item-metadata`),
  connectionRepos: (id: number) => get<AvailableReposOut>(`/connections/${id}/repos`),

  // settings
  getSettings: () => get<SettingsOut>("/settings"),
  updateSettings: (body: SettingsUpdate) => put<SettingsOut>("/settings", body),

  // projects
  listProjects: () => get<ProjectOut[]>("/projects"),
  refreshProjects: () => post<ProjectOut[]>("/projects/refresh"),

  // shared namespace (ADR 0009): admin-curated catalog members clone from.
  listSharedProjects: () => get<SharedProjectOut[]>("/shared/projects"),
  cloneSharedProject: (key: string) =>
    post<CloneResultOut>(`/shared/projects/${encodeURIComponent(key)}/clone`),
  createSharedProject: (key: string, body: SharedProjectCreate) =>
    post<ProjectConfigOut>(`/shared/projects/${encodeURIComponent(key)}`, body),
  buildSharedKnowledge: (key: string, body: KnowledgeBuildRequest) =>
    post<ProjectKnowledgeOut>(`/shared/projects/${encodeURIComponent(key)}/knowledge/build`, body),
  buildSharedRepoKnowledge: (key: string, repo: string, body: KnowledgeBuildRequest) =>
    post<ProjectKnowledgeOut>(
      `/shared/projects/${encodeURIComponent(key)}/repos/${encodeURIComponent(repo)}/knowledge/build`,
      body,
    ),
  // shared project full config + manual-login session (admin settings page).
  getSharedProjectConfig: (key: string) =>
    get<ProjectConfigOut>(`/shared/projects/${encodeURIComponent(key)}/config`),
  getSharedProjectAuth: (key: string) =>
    get<AuthState>(`/shared/projects/${encodeURIComponent(key)}/auth`),
  clearSharedProjectAuth: (key: string) =>
    del<AuthState>(`/shared/projects/${encodeURIComponent(key)}/auth`),
  captureSharedProjectAuth: (key: string) =>
    post<AuthState>(`/shared/projects/${encodeURIComponent(key)}/auth/capture`),

  // project knowledge
  listKnowledge: () => get<ProjectKnowledgeOut[]>("/projects/knowledge"),
  getProjectKnowledge: (key: string) =>
    get<ProjectKnowledgeOut>(`/projects/${encodeURIComponent(key)}/knowledge`),
  buildKnowledge: (key: string, body: KnowledgeBuildRequest) =>
    post<ProjectKnowledgeOut>(`/projects/${encodeURIComponent(key)}/knowledge/build`, body),

  // project config (test account, base URL, environments, repos)
  getProjectConfig: (key: string) =>
    get<ProjectConfigOut>(`/projects/${encodeURIComponent(key)}/config`),
  saveProjectConfig: (key: string, body: ProjectConfigUpdate) =>
    put<ProjectConfigOut>(`/projects/${encodeURIComponent(key)}/config`, body),

  // project manual-login (saved browser session)
  getProjectAuth: (key: string) => get<AuthState>(`/projects/${encodeURIComponent(key)}/auth`),
  clearProjectAuth: (key: string) => del<AuthState>(`/projects/${encodeURIComponent(key)}/auth`),
  captureProjectAuth: (key: string) =>
    post<AuthState>(`/projects/${encodeURIComponent(key)}/auth/capture`),

  // project repos + per-repo knowledge
  listProjectRepos: (key: string) =>
    get<RepoKnowledgeOut[]>(`/projects/${encodeURIComponent(key)}/repos`),
  getRepoKnowledge: (key: string, repo: string) =>
    get<ProjectKnowledgeOut>(
      `/projects/${encodeURIComponent(key)}/repos/${encodeURIComponent(repo)}/knowledge`,
    ),
  buildRepoKnowledge: (key: string, repo: string, body: KnowledgeBuildRequest) =>
    post<ProjectKnowledgeOut>(
      `/projects/${encodeURIComponent(key)}/repos/${encodeURIComponent(repo)}/knowledge/build`,
      body,
    ),

  // tickets
  listTickets: (params: TicketFilters = {}) =>
    get<TicketPage>("/tickets" + qs(params as Record<string, string | number | undefined>)),
  getTicket: (externalId: string) => get<TicketDetailOut>(`/tickets/${externalId}`),
  linkedCases: (externalId: string) =>
    get<LinkedTestCaseOut[]>(`/tickets/${encodeURIComponent(externalId)}/linked-cases`),
  syncTickets: (body: SyncRequest) => post<SyncResult>("/tickets/sync", body),
  // Local-only delete — never calls the provider, so a re-sync restores tickets.
  deleteTicket: (externalId: string) => del<void>(`/tickets/${encodeURIComponent(externalId)}`),
  deleteTickets: (externalIds: string[]) =>
    post<{ deleted: number }>("/tickets/delete", { externalIds }),

  // runs
  listRuns: () => get<RunOut[]>("/runs"),
  createRun: (body: RunCreate) => post<RunDetailOut>("/runs", body),
  // Seed (or return the existing) fully-populated demo run for the product tour
  // / Getting Started page. No AI pipeline — the backend inserts the row graph
  // directly, owner-stamped and idempotent (one `RUN-DEMO` per user).
  createSampleRun: () => post<RunDetailOut>("/runs/sample"),
  getRun: (runId: number | string) => get<RunDetailOut>(`/runs/${runId}`),
  regenerateRun: (runId: number | string) => post<RunDetailOut>(`/runs/${runId}/regenerate`),
  cancelRun: (runId: number | string) => post<RunOut>(`/runs/${runId}/cancel`),
  // Stop + clean up a run in ANY status (in-progress → cancel; terminal → force
  // clean up orphaned in-flight rows/queues). See #420.
  stopRun: (runId: number | string) => post<RunOut>(`/runs/${runId}/stop`),
  retryRun: (runId: number | string) => post<RunOut>(`/runs/${runId}/retry`),
  deleteRun: (runId: number | string) => del<void>(`/runs/${runId}`),
  runRepos: (runId: number | string) => get<RunRepoOption[]>(`/runs/${runId}/repos`),
  runAiUsage: (runId: number | string) => get<RunAiUsage>(`/runs/${runId}/ai-usage`),
  setRunTicketRepo: (runId: number | string, tid: string, repo: string) =>
    post<RunTicketOut>(`/runs/${runId}/tickets/${encodeURIComponent(tid)}/repo`, { repo }),

  // review
  listCases: (runId: number | string) => get<TestCaseOut[]>(`/runs/${runId}/cases`),
  addCase: (runId: number | string, body: TestCaseCreate) =>
    post<TestCaseOut>(`/runs/${runId}/cases`, body),
  updateCase: (caseId: number, body: TestCaseUpdate) => patch<TestCaseOut>(`/cases/${caseId}`, body),
  setApproval: (caseId: number, approval: "approved" | "rejected" | "pending") =>
    post<TestCaseOut>(`/cases/${caseId}/approval`, { approval }),
  regenerateCase: (caseId: number) => post<TestCaseOut>(`/cases/${caseId}/regenerate`),
  approveAll: (runId: number | string) => post<TestCaseOut[]>(`/runs/${runId}/approve-all`),
  approveTicket: (runId: number | string, tid: string) =>
    post<TestCaseOut[]>(`/runs/${runId}/tickets/${tid}/approve`),
  createAndLink: (runId: number | string, body: CreateLinkRequest) =>
    post<LinkStatusOut>(`/runs/${runId}/testcases/create-link`, body),
  linkStatus: (runId: number | string) => get<LinkStatusOut>(`/runs/${runId}/linked`),

  // automation
  generateAutomation: (runId: number | string, force = false) =>
    post<AutomationSpecOut[]>(
      `/runs/${runId}/automation/generate${force ? "?force=true" : ""}`,
    ),
  automationStatus: (runId: number | string) =>
    get<AutomationStatus>(`/runs/${runId}/automation/status`),
  listSpecs: (runId: number | string) => get<AutomationSpecOut[]>(`/runs/${runId}/automation`),
  getSpec: (caseId: number) => get<AutomationSpecOut>(`/cases/${caseId}/spec`),
  // Fire-and-forget: regeneration runs off-request on the server (it makes
  // multiple Claude calls and would otherwise exceed the proxy timeout). The
  // result is streamed over the run WS as `spec.regenerated`.
  regenerateSpec: (caseId: number, comment?: string) =>
    post<{ started: boolean; caseId: number }>(
      `/cases/${caseId}/spec/regenerate`,
      comment ? { comment } : undefined,
    ),
  // Fire-and-forget like `regenerateSpec`: Claude edits the selected spec off
  // -request on the server; the reply + edited spec arrive over the run WS as
  // `automation.chat.reply` (or `automation.chat.error`). `messageId` correlates
  // the WS reply back to the pending client-side message.
  sendSpecChat: (caseId: number, message: string, model?: string, messageId?: string) =>
    post<{ started: boolean; caseId: number }>(`/cases/${caseId}/spec/chat`, {
      message,
      model,
      messageId,
    }),
  updateSpec: (caseId: number, code: string) => patch<AutomationSpecOut>(`/cases/${caseId}/spec`, { code }),
  healSpec: (caseId: number) =>
    post<{ started: boolean; maxAttempts: number }>(`/cases/${caseId}/spec/heal`),
  healStatus: (caseId: number) =>
    get<{ healing: boolean; attempt: number; maxAttempts: number }>(
      `/cases/${caseId}/spec/heal/status`,
    ),
  healReport: (caseId: number) =>
    get<HealReport | Record<string, never>>(`/cases/${caseId}/spec/heal/report`),
  runSpec: (caseId: number) => post<ExecutionOut>(`/cases/${caseId}/spec/run`),

  // DOM exploration (ADR 0010) — user-triggered from a blocked case. Fire-and-
  // forget like self-heal: the POST only starts the background observe→decide→act
  // loop (long-running) and returns a session id; progress streams as
  // `explore.progress` on the run WS and `explore/status` supports poll survival.
  exploreSpec: (projectKey: string, repo: string, body: ExploreRequest) =>
    post<ExploreStartOut>(
      `/projects/${encodeURIComponent(projectKey)}/repos/${encodeURIComponent(repo)}/explore`,
      body,
    ),
  exploreStatus: (projectKey: string, repo: string) =>
    get<ExploreStatus>(
      `/projects/${encodeURIComponent(projectKey)}/repos/${encodeURIComponent(repo)}/explore/status`,
    ),

  // execution
  startExecution: (
    runId: number | string,
    body: { workers?: number; env?: string; target?: ExecutionTarget } = {},
  ) => post<ExecutionOut>(`/runs/${runId}/execution`, body),
  getExecution: (runId: number | string) => get<ExecutionOut>(`/runs/${runId}/execution`),

  // Local Agent device pairing (#? Local Agent feature) — user-authed device
  // management. The job-claim/push protocol (`/agent/jobs/*`) is device-authed
  // and consumed only by the Node CLI, not the SPA.
  agentDevices: {
    pairCode: () => post<PairCodeOut>("/agent/devices/pair-code"),
    list: () => get<AgentDeviceOut[]>("/agent/devices"),
    revoke: (id: number) => del<{ ok: boolean }>(`/agent/devices/${id}`),
  },

  // evidence
  getEvidence: (runId: number | string) => get<EvidenceGrouped>(`/runs/${runId}/evidence`),
  annotate: (evidenceId: number, shapes: AnnotationShape[]) =>
    post<EvidenceOut>(`/evidence/${evidenceId}/annotate`, { shapes }),
  autoAnnotateEvidence: (evidenceId: number) =>
    post<EvidenceOut>(`/evidence/${evidenceId}/auto-annotate`),

  // reports
  buildReport: (runId: number | string) => post<ReportOut>(`/runs/${runId}/report`),
  getReport: (runId: number | string) => get<ReportOut>(`/runs/${runId}/report`),
  listReports: () => get<ReportOut[]>("/reports"),

  // comments / publish
  prepareComments: (runId: number | string) =>
    post<TicketCommentOut[]>(`/runs/${runId}/comments/prepare`),
  listComments: (runId: number | string) => get<TicketCommentOut[]>(`/runs/${runId}/comments`),
  editComment: (commentId: number, body: { body?: string; targetStatus?: string }) =>
    patch<TicketCommentOut>(`/comments/${commentId}`, body),
  publishComment: (commentId: number) => post<TicketCommentOut>(`/comments/${commentId}/publish`),
  publishAll: (runId: number | string, ticketIds: string[] = []) =>
    post<TicketCommentOut[]>(`/runs/${runId}/comments/publish`, { ticketIds }),
  retryComments: (runId: number | string) => post<TicketCommentOut[]>(`/runs/${runId}/comments/retry`),

  // audit log
  auditEvents: (params: { category?: string; actor?: string; q?: string; run?: string } = {}) =>
    get<AuditEventOut[]>("/audit/events" + qs(params)),
  auditStats: () => get<AuditStats>("/audit/stats"),
  clearAuditEvents: () => del<{ deleted: number }>("/audit/events"),
  backendLogs: (params: { level?: string; service?: string; q?: string } = {}) =>
    get<BackendLogOut[]>("/audit/logs" + qs(params)),
  backendLogStats: () => get<BackendLogStats>("/audit/logs/stats"),

  // artifacts — a browser <img>/<video> can't send the Authorization header,
  // and the backend /artifacts guard reads the access token from ?token= only
  // (same as WebSocket URLs), so append it here or images 401.
  artifactUrl: (path: string) => `${API_BASE}/artifacts/${path}${wsToken()}`,
  wsUrl: (runId: number | string) => `${wsBase()}/ws/runs/${runId}${wsToken()}`,
};

/** `?token=<accessToken>` query suffix for WebSocket URLs (WS can't carry an
 * Authorization header). Read from the auth store at connect time. */
function wsToken(): string {
  const t = useAuth.getState().accessToken;
  return t ? `?token=${encodeURIComponent(t)}` : "";
}
