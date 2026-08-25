/**
 * Typed HTTP client for the Q-Agent backend. Thin wrapper over fetch — one
 * method per endpoint in docs/API-CONTRACT.md. Screens consume these through
 * TanStack Query hooks (see src/hooks/) using the keys in src/lib/queryKeys.ts.
 */

import { BASE_PREFIX, stripBase, withBase } from "@/lib/basePath";
import { useAuth } from "@/store/auth";
import type {
  HubClaudeCredential,
  AdminUser,
  AgentDeviceOut,
  AiActivity,
  AuthoringStateOut,
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
  AutomationExportPreflight,
  AutomationExportResult,
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
  Readiness,
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
  TicketFilterOptions,
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
//
// Both prefixes follow the app's mount point, so an app served at `/qagent/`
// calls `/qagent/api/*` and `/qagent/auth/*`. The front door strips that prefix
// again, so the backend sees exactly the paths it sees standalone.
export const API_BASE: string =
  (import.meta.env.VITE_API_BASE as string | undefined)?.replace(/\/$/, "") ??
  withBase("/api");

/** Same-origin prefix for `/auth/*` — see `isAuthPath` for why it is separate.
 *
 * `BASE_PREFIX`, not `withBase("")`: that helper only rewrites paths that start
 * with a slash and returns `""` unchanged, which silently produced an unprefixed
 * `/auth/refresh` — a 405 against the hub's SPA rather than this app's API. */
export const AUTH_BASE: string = BASE_PREFIX;

/** Absolute websocket base for `new WebSocket(...)`. When `API_BASE` is an
 * absolute http(s) URL, swap the scheme to ws(s). When it's a same-origin
 * relative prefix (the default `/api`), derive scheme + host from the current
 * page so the URL is absolute (relative WS URLs are invalid). */
function wsBase(): string {
  if (/^https?:\/\//.test(API_BASE)) return API_BASE.replace(/^http/, "ws");
  const proto =
    typeof location !== "undefined" && location.protocol === "https:"
      ? "wss:"
      : "ws:";
  const host =
    typeof location !== "undefined" ? location.host : "127.0.0.1:8787";
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
  const match = document.cookie.match(
    new RegExp("(?:^|; )" + escaped + "=([^;]*)"),
  );
  return match ? decodeURIComponent(match[1]) : null;
}

/** `/auth/*` calls are SAME-ORIGIN (relative path, via the Vite dev proxy /
 * same-host in prod) so the httpOnly refresh + CSRF cookies flow. They also
 * opt out of the silent 401→refresh retry to avoid recursion. */
function isAuthPath(path: string): boolean {
  return path === "/auth" || path.startsWith("/auth/");
}

/**
 * The service could not be reached at all — refused, DNS, timeout, aborted.
 *
 * Distinct from :class:`ApiError`, which always carries an HTTP status and
 * therefore means the service *answered*. That distinction is the whole point of
 * B5 (#482): "the service is down" and "you are logged out" are different facts
 * and must not render the same screen. A bare `fetch` rejection used to surface
 * as an untyped `TypeError`, which callers could only show as a generic failure.
 */
export class NetworkError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "NetworkError";
  }
}

/**
 * Service reachability, as observed by the last request.
 *
 * A tiny subscribable rather than Zustand state: `lib/api.ts` is imported by the
 * store itself, so depending on the store here would be circular.
 *
 * Note this is deliberately **not** an authentication signal. It says nothing
 * about whether the user is signed in, and nothing anywhere may treat
 * `unreachable` as permission to proceed — see the `request` 401 branch.
 */
let serviceReachable = true;
const reachabilityListeners = new Set<(reachable: boolean) => void>();

function setServiceReachable(reachable: boolean): void {
  if (serviceReachable === reachable) return;
  serviceReachable = reachable;
  for (const listener of reachabilityListeners) listener(reachable);
}

export function isServiceReachable(): boolean {
  return serviceReachable;
}

export function subscribeServiceReachable(
  listener: (reachable: boolean) => void,
): () => void {
  reachabilityListeners.add(listener);
  return () => reachabilityListeners.delete(listener);
}

/**
 * Request deadlines (#490).
 *
 * Without one, a backend that accepts a connection and then never answers — a
 * hung app behind a live proxy, the classic pool-exhaustion signature — leaves
 * the UI loading forever: `fetch` waits indefinitely, so the `catch` that marks
 * the service unreachable never runs and the user cannot tell "slow" from "never
 * coming back".
 *
 * The ceiling is per-path rather than global because a handful of endpoints are
 * legitimately slow, and a deadline short enough to be useful for a list query
 * would abort them mid-flight. Keyed off the path so no call site has to opt in
 * and the whole policy is auditable in one place.
 */
const DEFAULT_TIMEOUT_MS = 60_000;

/** Endpoints that block on Claude or on a provider's API, and so must be allowed
 * to take minutes. The ceiling matches the backend's own longest budget
 * (`QAGENT_CLAUDE_BOOTSTRAP_TIMEOUT_S`, 1200s) — still bounded, so a genuinely
 * dead connection eventually errors instead of hanging forever. */
const SLOW_TIMEOUT_MS = 20 * 60_000;

const SLOW_PATHS: RegExp[] = [
  /^\/ai\/credentials\/test/, // a real Claude round trip (claude_timeout_s = 300)
  /^\/cases\/\d+\/regenerate/, // Claude regenerates a case synchronously
  /^\/runs\/[^/]+\/regenerate/,
  /^\/runs\/[^/]+\/comments\/prepare/, // Claude drafts every comment synchronously
  /^\/runs\/[^/]+\/testcases\/create-link/, // writes to ADO/Jira
  /^\/tickets\/sync/, // provider sync loop
  /^\/projects\/refresh/, // provider project/repo discovery
];

function timeoutFor(path: string): number {
  return SLOW_PATHS.some((re) => re.test(path))
    ? SLOW_TIMEOUT_MS
    : DEFAULT_TIMEOUT_MS;
}

/** Outcome of a refresh attempt. The three cases must stay distinguishable:
 * only `expired` is authoritative evidence that the session is dead. */
type RefreshOutcome =
  /** New access token installed — replay the original request. */
  | "refreshed"
  /** The server answered and refused. The session really is over. */
  | "expired"
  /** Never reached the server. Says nothing about the session — do NOT log out. */
  | "unreachable";

/** Single in-flight refresh shared by all callers, so a burst of concurrent
 * 401s triggers exactly one `POST /auth/refresh`. */
let refreshInFlight: Promise<RefreshOutcome> | null = null;

/**
 * Fallback renewal, injected rather than imported (#531).
 *
 * An SSO session has no `qagent_refresh` cookie, so `/auth/refresh` is *supposed*
 * to fail for it — identity is re-derived from the hub instead. This module must
 * not know that: `lib/hubSso.ts` reads `API_BASE` from here, so importing it back
 * would make the cycle real, and the whole point of the login-shaped bootstrap
 * response is that `lib/api.ts` stays ignorant of the hub. So the renewer is
 * registered at startup by whoever does know (`app/sessionRenewal.ts`).
 *
 * Returns `refreshed` when a session was installed, `expired` when the authority
 * says nobody is signed in, and `unreachable` when it could not tell — the same
 * three cases, and the same rule: only `expired` justifies signing someone out.
 */
type SessionRenewer = () => Promise<RefreshOutcome>;
let renewFromAuthority: SessionRenewer | null = null;
export function setSessionRenewer(renewer: SessionRenewer | null): void {
  renewFromAuthority = renewer;
}

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

/**
 * Restore a session on app boot, using the same ladder as the 401 path (#611).
 *
 * `store/auth.ts`'s `bootstrap()` used to call `api.auth.refresh()` raw, so a boot
 * could only ever be rescued by the `qagent_refresh` cookie — and an SSO session
 * deliberately has none (#531/#532). A reload therefore always ended at /login even
 * though the hub knew perfectly well who was signed in. Routing boot through
 * `tryRefresh` means the cookie is tried first, then the hub, with the
 * unreachable-vs-expired distinction and the in-flight coalescing already encoded
 * there rather than duplicated in the store.
 *
 * Exported (rather than exporting `tryRefresh` itself) to keep the 401 machinery
 * private and give the boot path an obvious name.
 */
export function restoreSession(): Promise<RefreshOutcome> {
  return tryRefresh();
}

function tryRefresh(): Promise<RefreshOutcome> {
  if (!refreshInFlight) {
    refreshInFlight = api.auth
      .refresh()
      .then(({ accessToken, user }): RefreshOutcome => {
        useAuth.getState().setSession({ accessToken, user });
        return "refreshed";
      })
      .catch((err): RefreshOutcome | Promise<RefreshOutcome> => {
        // The critical branch. Previously every failure collapsed to `false`,
        // which the caller read as "session dead" and answered with a logout +
        // redirect to /login — so a backend that was merely unreachable told the
        // user they'd been signed out, the single most confusing outcome
        // available (docs/HUB-INTEGRATION.md §3 B5).
        if (err instanceof NetworkError) return "unreachable";
        // 502-504 mean the proxy answered but the app behind it did not — that
        // is the service being down, not the session ending.
        if (err instanceof ApiError && err.status >= 502 && err.status <= 504)
          return "unreachable";
        // Cookie renewal is out, but it may never have been the authority. An SSO
        // session has no refresh cookie at all (#531), so reaching here is its
        // NORMAL renewal path, not the end of it — ask the hub before concluding
        // anything. Local logins have no renewer registered and fall straight
        // through, so their fast path is unchanged.
        if (renewFromAuthority) return renewFromAuthority();
        return "expired";
      })
      .finally(() => {
        refreshInFlight = null;
      });
  }
  return refreshInFlight;
}

async function request<T>(
  path: string,
  init?: RequestInit,
  retried = false,
): Promise<T> {
  const authPath = isAuthPath(path);
  // `/auth/*` keeps its own prefix rather than riding under API_BASE, so the
  // refresh cookie stays path-scoped to it (ADR 0007). Both are still relative
  // to wherever the app is mounted.
  const url = authPath ? AUTH_BASE + path : API_BASE + path;

  const token = useAuth.getState().accessToken;
  const csrf = getCookie("qagent_csrf");
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...((init?.headers as Record<string, string> | undefined) ?? {}),
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (csrf) headers["X-CSRF-Token"] = csrf;

  let res: Response;
  const timeoutMs = timeoutFor(path);
  try {
    res = await fetch(url, {
      ...init,
      credentials: "include",
      headers,
      // Bound the wait so a hung backend surfaces as unreachable instead of
      // loading forever (#490). Respect a caller-supplied signal if there ever
      // is one rather than silently dropping it.
      signal: init?.signal ?? AbortSignal.timeout(timeoutMs),
    });
  } catch (err) {
    // Refused / DNS / timeout / aborted — we never got an answer. Surface it as
    // a typed error (it used to escape as a bare TypeError) and flip the
    // reachability flag so the shell can offer a Retry instead of a blank toast.
    setServiceReachable(false);
    const timedOut = err instanceof DOMException && err.name === "TimeoutError";
    throw new NetworkError(
      timedOut
        ? `The service did not respond within ${Math.round(timeoutMs / 1000)}s`
        : err instanceof Error
          ? err.message
          : "Could not reach the service",
    );
  }

  // A gateway error means the proxy answered but the app behind it did not, so
  // the service IS down even though we got a response (#490). Without this, a
  // 502/503/504 on an ordinary call showed no banner and no message at all —
  // `tryRefresh` only classifies these on the 401→refresh path.
  if (res.status === 502 || res.status === 503 || res.status === 504) {
    setServiceReachable(false);
  } else {
    // The service answered on its own behalf — so it is reachable. A 401 or a
    // 500 is still an answer, and clearing the banner here is what makes Retry
    // recover.
    setServiceReachable(true);
  }

  // Silent recovery: on a 401 for a non-auth call, refresh the access token
  // once and replay the request.
  if (res.status === 401 && !authPath && !retried) {
    const outcome = await tryRefresh();
    if (outcome === "refreshed") return request<T>(path, init, true);
    if (outcome === "unreachable") {
      // We could not establish that the session is dead, so we must NOT act as
      // if it were: no logout, no redirect to /login. Fail closed — the caller
      // gets an error and the shell shows "unreachable" — but never fall open.
      setServiceReachable(false);
      throw new NetworkError(
        "Could not reach the service to renew your session",
      );
    }
    // `expired`: the server authoritatively refused the refresh. The session is
    // genuinely over — this is the one case that means "you are logged out".
    //
    // An explicit logout is orchestrating its own navigation to /signed-out —
    // stay inert so a background 401 doesn't flip the store to anon (which would
    // trip RequireAuth to /login) or hard-redirect over it.
    if (!loggingOut) {
      useAuth.getState().logout();
      // `pathname` arrives with the mount prefix attached and this is a hard
      // navigation the router never sees, so both sides need converting: strip
      // the prefix before matching a route, add it back before assigning.
      const onPublicAuthRoute =
        typeof window !== "undefined" &&
        /^\/(login|signed-out|forgot)/.test(
          stripBase(window.location.pathname),
        );
      if (typeof window !== "undefined" && !onPublicAuthRoute) {
        window.location.assign(withBase("/login"));
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
    if (!detail.trim())
      detail = res.statusText.trim() || `Request failed (HTTP ${res.status})`;
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

const get = <T>(p: string) => request<T>(p);
/**
 * A GET carrying a freshly-minted EmeHub agent token (#500).
 *
 * The backend spends it on one hub call and never stores it (`api/app/deps_hub.py`),
 * which is why it rides on the request rather than living anywhere: agent tokens
 * last 15 minutes **and** are bound to a live hub session, so a cached one is
 * expired or about to be. A `null` token sends no header at all — the ordinary
 * state when the hub is off or the browser has no hub session — and the backend
 * then serves purely local data.
 */
const getWithHubToken = <T>(p: string, hubToken: string | null) =>
  request<T>(
    p,
    hubToken ? { headers: { "X-Hub-Token": hubToken } } : undefined,
  );
/** POST variant of {@link getWithHubToken} — same contract: a `null` token sends
 * no header, and the backend then resolves everything locally (#505). */
const postWithHubToken = <T>(
  p: string,
  body: unknown,
  hubToken: string | null,
) =>
  request<T>(p, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
    ...(hubToken ? { headers: { "X-Hub-Token": hubToken } } : {}),
  });
const post = <T>(p: string, body?: unknown) =>
  request<T>(p, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
const put = <T>(p: string, body?: unknown) =>
  request<T>(p, { method: "PUT", body: JSON.stringify(body ?? {}) });
const patch = <T>(p: string, body?: unknown) =>
  request<T>(p, { method: "PATCH", body: JSON.stringify(body ?? {}) });
const del = <T>(p: string) => request<T>(p, { method: "DELETE" });

function qs(params: Record<string, string | number | undefined>): string {
  const entries = Object.entries(params).filter(
    ([, v]) => v != null && v !== "",
  );
  if (!entries.length) return "";
  return (
    "?" +
    entries.map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`).join("&")
  );
}

export const api = {
  // health / observability
  // `/health` is the only one of these readable while anonymous (it's in the
  // backend's auth allowlist, `/capabilities` isn't) — hence `hubSsoEnabled`
  // riding along here for the login screen (#478).
  health: () =>
    get<{
      status: string;
      version: string;
      hubSsoEnabled: boolean;
      hubDataEnabled: boolean;
    }>("/health"),
  // Setup readiness for the signed-in user (#642) — what still blocks a run.
  readiness: () => get<Readiness>("/readiness"),
  capabilities: () =>
    get<{ claude: boolean; version: string }>("/capabilities"),
  aiActivity: () => get<AiActivity>("/ai/activity"),
  aiStats: (force = false) =>
    get<ClaudeStats>(`/ai/stats${force ? "?refresh=true" : ""}`),
  aiWsUrl: () => `${wsBase()}/ws/ai${wsToken()}`,

  // Claude CLI credentials management (#95): own (per-user) + shared (admin-only).
  claudeCredentials: {
    status: () => get<ClaudeCredentialsStatus>("/ai/credentials"),
    // The credential EmeHub would resolve for this user (#512). Sanitised
    // server-side — the material never reaches the browser. `available: false`
    // covers flag-off, no hub session and hub-down alike; all mean "show the
    // local card as-is".
    hub: (hubToken: string | null = null) =>
      getWithHubToken<HubClaudeCredential>("/ai/credentials/hub", hubToken),
    // Real minimal Claude call under a credential — authoritative. `scope`
    // selects which: effective (default), the shared account, or own.
    test: (scope?: "effective" | "shared" | "own") =>
      post<ClaudeCredentialsTestResult>(
        `/ai/credentials/test${scope ? `?scope=${scope}` : ""}`,
      ),
    uploadOwn: (body: ClaudeCredentialsUpload) =>
      put<void>("/ai/credentials", body),
    // Non-destructive switch between own/shared (keeps the uploaded token on file).
    setMode: (mode: "own" | "shared") =>
      put<void>("/ai/credentials/mode", { mode }),
    deleteOwn: () => del<void>("/ai/credentials"),
    uploadShared: (body: ClaudeCredentialsUpload) =>
      put<void>("/ai/credentials/shared", body),
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

    reset: (body: { token: string; password: string }) =>
      post<void>("/auth/reset", body),

    twofaSetup: () => post<TwoFactorSetup>("/auth/2fa/setup"),
    twofaEnable: (body: { code: string }) =>
      post<void>("/auth/2fa/enable", body),
    twofaDisable: (body: { code: string }) =>
      post<void>("/auth/2fa/disable", body),

    sessions: () => get<AuthSession[]>("/auth/sessions"),
    revokeSession: (id: string) =>
      del<void>(`/auth/sessions/${encodeURIComponent(id)}`),
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
    inviteUser: (body: {
      email: string;
      firstName?: string;
      lastName?: string;
      role: UserRole;
    }) => post<InviteUserResponse>("/auth/users/invite", body),
    updateUser: (
      id: number,
      body: Partial<{
        firstName: string;
        lastName: string;
        role: UserRole;
        isActive: boolean;
      }>,
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
  testConnection: (id: number) =>
    post<TestConnectionResult>(`/connections/${id}/test`),
  connectionProjects: (id: number) =>
    get<ConnectionProjectOut[]>(`/connections/${id}/projects`),
  connectionSprints: (id: number) =>
    get<SprintOut[]>(`/connections/${id}/sprints`),
  connectionWorkItemMetadata: (id: number) =>
    get<WorkItemMetadataOut>(`/connections/${id}/work-item-metadata`),
  connectionRepos: (id: number) =>
    get<AvailableReposOut>(`/connections/${id}/repos`),

  // settings
  getSettings: () => get<SettingsOut>("/settings"),
  updateSettings: (body: SettingsUpdate) => put<SettingsOut>("/settings", body),

  // projects
  // Carries the hub token so the backend can mirror EmeHub's projects into this
  // user's workspace before answering (#591). Without it the mirror declines and
  // a hub user sees "No connected projects" forever — the config/repos reads got
  // the token, this one never did.
  listProjects: (hubToken: string | null = null) =>
    getWithHubToken<ProjectOut[]>("/projects", hubToken),
  refreshProjects: () => post<ProjectOut[]>("/projects/refresh"),

  // shared namespace (ADR 0009): admin-curated catalog members clone from.
  listSharedProjects: () => get<SharedProjectOut[]>("/shared/projects"),
  cloneSharedProject: (key: string) =>
    post<CloneResultOut>(`/shared/projects/${encodeURIComponent(key)}/clone`),
  createSharedProject: (key: string, body: SharedProjectCreate) =>
    post<ProjectConfigOut>(`/shared/projects/${encodeURIComponent(key)}`, body),
  buildSharedKnowledge: (key: string, body: KnowledgeBuildRequest) =>
    post<ProjectKnowledgeOut>(
      `/shared/projects/${encodeURIComponent(key)}/knowledge/build`,
      body,
    ),
  buildSharedRepoKnowledge: (
    key: string,
    repo: string,
    body: KnowledgeBuildRequest,
  ) =>
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
  // `hubToken` mirrors a HUB-indexed knowledge base before the read (#598) — the
  // same reason `getProjectConfig` needs it (#592). Without the header the backend
  // serves whatever is already local, which for a hub project is nothing at all:
  // that is exactly the bug, a project shown as `Indexed` on the hub and empty here.
  getProjectKnowledge: (key: string, hubToken: string | null = null) =>
    getWithHubToken<ProjectKnowledgeOut>(
      `/projects/${encodeURIComponent(key)}/knowledge`,
      hubToken,
    ),
  buildKnowledge: (key: string, body: KnowledgeBuildRequest) =>
    post<ProjectKnowledgeOut>(
      `/projects/${encodeURIComponent(key)}/knowledge/build`,
      body,
    ),

  // project config (test account, base URL, environments, repos)
  //
  // `hubToken` is what lets the backend mirror a HUB-owned project's config before
  // reading it (#592). Without the header `ensure_project_config` returns on its
  // first line and the Settings tab renders the bare mirrored row -- no repos, no
  // environments -- with nothing logged, because that path is deliberately silent
  // so a hub outage cannot break the screen. Optional and purely additive: a null
  // token still serves local config.
  getProjectConfig: (key: string, hubToken: string | null = null) =>
    getWithHubToken<ProjectConfigOut>(
      `/projects/${encodeURIComponent(key)}/config`,
      hubToken,
    ),
  saveProjectConfig: (key: string, body: ProjectConfigUpdate) =>
    put<ProjectConfigOut>(`/projects/${encodeURIComponent(key)}/config`, body),

  // project manual-login (saved browser session)
  getProjectAuth: (key: string) =>
    get<AuthState>(`/projects/${encodeURIComponent(key)}/auth`),
  clearProjectAuth: (key: string) =>
    del<AuthState>(`/projects/${encodeURIComponent(key)}/auth`),
  captureProjectAuth: (key: string) =>
    post<AuthState>(`/projects/${encodeURIComponent(key)}/auth/capture`),

  // project repos + per-repo knowledge
  // Same hub mirror as `getProjectConfig` (#592): repos come from the hub's project
  // config, so this read needs the token for the same reason.
  listProjectRepos: (key: string, hubToken: string | null = null) =>
    getWithHubToken<RepoKnowledgeOut[]>(
      `/projects/${encodeURIComponent(key)}/repos`,
      hubToken,
    ),
  // Same hub knowledge mirror as `getProjectKnowledge` (#598).
  getRepoKnowledge: (
    key: string,
    repo: string,
    hubToken: string | null = null,
  ) =>
    getWithHubToken<ProjectKnowledgeOut>(
      `/projects/${encodeURIComponent(key)}/repos/${encodeURIComponent(repo)}/knowledge`,
      hubToken,
    ),
  buildRepoKnowledge: (
    key: string,
    repo: string,
    body: KnowledgeBuildRequest,
  ) =>
    post<ProjectKnowledgeOut>(
      `/projects/${encodeURIComponent(key)}/repos/${encodeURIComponent(repo)}/knowledge/build`,
      body,
    ),

  // tickets
  // `hubToken` is optional and purely additive: with it the backend may serve the
  // list with EmeHub's values overlaid (#500), without it — or if the hub fails —
  // it serves the local list. A hub read is an enhancement, never a precondition.
  listTickets: (params: TicketFilters = {}, hubToken: string | null = null) =>
    getWithHubToken<TicketPage>(
      "/tickets" + qs(params as Record<string, string | number | undefined>),
      hubToken,
    ),
  // The query builder's dropdown source (#517). No hub token: it is a distinct
  // read over local rows, so it answers with the hub down and with a mirrored
  // connection that has no credential — which is exactly when it is needed.
  ticketFilterOptions: (
    connectionId: number | null,
    providerKind: string | null,
  ) =>
    get<TicketFilterOptions>(
      "/tickets/filter-options" +
        qs({
          connectionId: connectionId ?? undefined,
          providerKind: providerKind ?? undefined,
        }),
    ),
  getTicket: (externalId: string) =>
    get<TicketDetailOut>(`/tickets/${externalId}`),
  linkedCases: (externalId: string) =>
    get<LinkedTestCaseOut[]>(
      `/tickets/${encodeURIComponent(externalId)}/linked-cases`,
    ),
  syncTickets: (body: SyncRequest) => post<SyncResult>("/tickets/sync", body),
  // Local-only delete — never calls the provider, so a re-sync restores tickets.
  deleteTicket: (externalId: string) =>
    del<void>(`/tickets/${encodeURIComponent(externalId)}`),
  deleteTickets: (externalIds: string[]) =>
    post<{ deleted: number }>("/tickets/delete", { externalIds }),

  // runs
  listRuns: () => get<RunOut[]>("/runs"),
  // The three run-start calls accept an optional hub token so the backend can
  // resolve the Claude credential from EmeHub (#499/#505). Omitted -> resolved
  // locally, exactly as before.
  createRun: (body: RunCreate, hubToken: string | null = null) =>
    postWithHubToken<RunDetailOut>("/runs", body, hubToken),
  // Seed (or return the existing) fully-populated demo run for the product tour
  // / Getting Started page. No AI pipeline — the backend inserts the row graph
  // directly, owner-stamped and idempotent (one `RUN-DEMO` per user).
  createSampleRun: () => post<RunDetailOut>("/runs/sample"),
  getRun: (runId: number | string) => get<RunDetailOut>(`/runs/${runId}`),
  regenerateRun: (runId: number | string, hubToken: string | null = null) =>
    postWithHubToken<RunDetailOut>(
      `/runs/${runId}/regenerate`,
      undefined,
      hubToken,
    ),
  cancelRun: (runId: number | string) => post<RunOut>(`/runs/${runId}/cancel`),
  // Stop + clean up a run in ANY status (in-progress → cancel; terminal → force
  // clean up orphaned in-flight rows/queues). See #420.
  stopRun: (runId: number | string) => post<RunOut>(`/runs/${runId}/stop`),
  retryRun: (runId: number | string, hubToken: string | null = null) =>
    postWithHubToken<RunOut>(`/runs/${runId}/retry`, undefined, hubToken),
  deleteRun: (runId: number | string) => del<void>(`/runs/${runId}`),
  runRepos: (runId: number | string) =>
    get<RunRepoOption[]>(`/runs/${runId}/repos`),
  runAiUsage: (runId: number | string) =>
    get<RunAiUsage>(`/runs/${runId}/ai-usage`),
  setRunTicketRepo: (runId: number | string, tid: string, repo: string) =>
    post<RunTicketOut>(
      `/runs/${runId}/tickets/${encodeURIComponent(tid)}/repo`,
      { repo },
    ),

  // review
  listCases: (runId: number | string) =>
    get<TestCaseOut[]>(`/runs/${runId}/cases`),
  addCase: (runId: number | string, body: TestCaseCreate) =>
    post<TestCaseOut>(`/runs/${runId}/cases`, body),
  updateCase: (caseId: number, body: TestCaseUpdate) =>
    patch<TestCaseOut>(`/cases/${caseId}`, body),
  setApproval: (
    caseId: number,
    approval: "approved" | "rejected" | "pending",
  ) => post<TestCaseOut>(`/cases/${caseId}/approval`, { approval }),
  regenerateCase: (caseId: number) =>
    post<TestCaseOut>(`/cases/${caseId}/regenerate`),
  approveAll: (runId: number | string) =>
    post<TestCaseOut[]>(`/runs/${runId}/approve-all`),
  approveTicket: (runId: number | string, tid: string) =>
    post<TestCaseOut[]>(`/runs/${runId}/tickets/${tid}/approve`),
  createAndLink: (runId: number | string, body: CreateLinkRequest) =>
    post<LinkStatusOut>(`/runs/${runId}/testcases/create-link`, body),
  linkStatus: (runId: number | string) =>
    get<LinkStatusOut>(`/runs/${runId}/linked`),

  // automation
  generateAutomation: (runId: number | string, force = false) =>
    post<AutomationSpecOut[]>(
      `/runs/${runId}/automation/generate${force ? "?force=true" : ""}`,
    ),
  automationStatus: (runId: number | string) =>
    get<AutomationStatus>(`/runs/${runId}/automation/status`),
  listSpecs: (runId: number | string) =>
    get<AutomationSpecOut[]>(`/runs/${runId}/automation`),
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
  sendSpecChat: (
    caseId: number,
    message: string,
    model?: string,
    messageId?: string,
  ) =>
    // `routedToAuthoring` (#619): while live authoring holds this case the message
    // is banked as GUIDANCE for the paused Claude session instead of starting a
    // spec-edit pass — so there will be no `automation.chat.reply`, and the caller
    // must resolve its own pending bubble from this response.
    post<{
      started: boolean;
      caseId: number;
      routedToAuthoring?: boolean;
      authoringStatus?: string;
      guidancePending?: number;
    }>(`/cases/${caseId}/spec/chat`, {
      message,
      model,
      messageId,
    }),
  /** Live-authoring pause state for one case (#619). */
  getAuthoringState: (caseId: number) =>
    get<AuthoringStateOut>(`/cases/${caseId}/authoring`),
  pauseAuthoring: (caseId: number) =>
    post<{ ok: boolean; outcome: string }>(`/cases/${caseId}/authoring/pause`, {}),
  continueAuthoring: (caseId: number, guidance: string) =>
    post<{ ok: boolean; outcome: string }>(`/cases/${caseId}/authoring/continue`, {
      guidance,
    }),
  /** Stop live-authoring this case now, leaving it re-runnable (#645). Idempotent
   *  — `cancelled: false` means there was nothing live, which is not an error. */
  cancelAuthoring: (caseId: number) =>
    post<{ cancelled: boolean; was?: string }>(`/cases/${caseId}/authoring/cancel`, {}),
  updateSpec: (caseId: number, code: string) =>
    patch<AutomationSpecOut>(`/cases/${caseId}/spec`, { code }),
  healSpec: (caseId: number) =>
    post<{ started: boolean; maxAttempts: number }>(
      `/cases/${caseId}/spec/heal`,
    ),
  healStatus: (caseId: number) =>
    get<{ healing: boolean; attempt: number; maxAttempts: number }>(
      `/cases/${caseId}/spec/heal/status`,
    ),
  healReport: (caseId: number) =>
    get<HealReport | Record<string, never>>(
      `/cases/${caseId}/spec/heal/report`,
    ),
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
  // Export the automation project to a customer-owned git remote (#549). Both are
  // user-triggered: the GET only prefills the panel (it pushes nothing) and the POST
  // is the explicit action, with the remote and branch chosen by the user.
  automationExportPreflight: (
    runId: number | string,
    projectId?: number | null,
  ) =>
    get<AutomationExportPreflight>(
      `/runs/${runId}/automation/export${projectId ? `?projectId=${projectId}` : ""}`,
    ),
  exportAutomationProject: (
    runId: number | string,
    body: {
      remoteUrl: string;
      branch: string;
      projectId?: number | null;
      message?: string;
    },
  ) => post<AutomationExportResult>(`/runs/${runId}/automation/export`, body),

  exploreStatus: (projectKey: string, repo: string) =>
    get<ExploreStatus>(
      `/projects/${encodeURIComponent(projectKey)}/repos/${encodeURIComponent(repo)}/explore/status`,
    ),

  // execution
  startExecution: (
    runId: number | string,
    body: { workers?: number; env?: string; target?: ExecutionTarget } = {},
  ) => post<ExecutionOut>(`/runs/${runId}/execution`, body),
  getExecution: (runId: number | string) =>
    get<ExecutionOut>(`/runs/${runId}/execution`),

  // Local Agent device pairing (#? Local Agent feature) — user-authed device
  // management. The job-claim/push protocol (`/agent/jobs/*`) is device-authed
  // and consumed only by the Node CLI, not the SPA.
  agentDevices: {
    pairCode: () => post<PairCodeOut>("/agent/devices/pair-code"),
    list: () => get<AgentDeviceOut[]>("/agent/devices"),
    revoke: (id: number) => del<{ ok: boolean }>(`/agent/devices/${id}`),
  },

  // evidence
  getEvidence: (runId: number | string) =>
    get<EvidenceGrouped>(`/runs/${runId}/evidence`),
  annotate: (evidenceId: number, shapes: AnnotationShape[]) =>
    post<EvidenceOut>(`/evidence/${evidenceId}/annotate`, { shapes }),
  autoAnnotateEvidence: (evidenceId: number) =>
    post<EvidenceOut>(`/evidence/${evidenceId}/auto-annotate`),

  // reports
  buildReport: (runId: number | string) =>
    post<ReportOut>(`/runs/${runId}/report`),
  getReport: (runId: number | string) =>
    get<ReportOut>(`/runs/${runId}/report`),
  listReports: () => get<ReportOut[]>("/reports"),

  // comments / publish
  prepareComments: (runId: number | string) =>
    post<TicketCommentOut[]>(`/runs/${runId}/comments/prepare`),
  listComments: (runId: number | string) =>
    get<TicketCommentOut[]>(`/runs/${runId}/comments`),
  editComment: (
    commentId: number,
    body: { body?: string; targetStatus?: string },
  ) => patch<TicketCommentOut>(`/comments/${commentId}`, body),
  publishComment: (commentId: number) =>
    post<TicketCommentOut>(`/comments/${commentId}/publish`),
  publishAll: (runId: number | string, ticketIds: string[] = []) =>
    post<TicketCommentOut[]>(`/runs/${runId}/comments/publish`, { ticketIds }),
  retryComments: (runId: number | string) =>
    post<TicketCommentOut[]>(`/runs/${runId}/comments/retry`),

  // audit log
  auditEvents: (
    params: {
      category?: string;
      actor?: string;
      q?: string;
      run?: string;
    } = {},
  ) => get<AuditEventOut[]>("/audit/events" + qs(params)),
  auditStats: () => get<AuditStats>("/audit/stats"),
  clearAuditEvents: () => del<{ deleted: number }>("/audit/events"),
  backendLogs: (
    params: { level?: string; service?: string; q?: string } = {},
  ) => get<BackendLogOut[]>("/audit/logs" + qs(params)),
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
