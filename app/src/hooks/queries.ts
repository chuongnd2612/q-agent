/**
 * TanStack Query hooks for every Q-Agent resource. Screens import these instead
 * of calling `api.*` directly, so cache keys + invalidation stay consistent.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryOptions,
} from "@tanstack/react-query";
import { toast } from "@/lib/toast";
import { ApiError, api } from "@/lib/api";
import {
  fetchHubSsoConfig,
  mintHubDataToken,
  type HubSsoConfig,
} from "@/lib/hubSso";
import { queryKeys } from "@/lib/queryKeys";
import type {
  AnnotationShape,
  AutomationSpecOut,
  ClaudeCredentialsUpload,
  ConnectionUpdate,
  ExecutionTarget,
  ExploreRequest,
  KnowledgeBuildRequest,
  ProjectConfigUpdate,
  ProviderKind,
  Readiness,
  RunCreate,
  RunOut,
  SettingsUpdate,
  SharedProjectCreate,
  SyncRequest,
  TestCaseCreate,
  TestCaseOut,
  TestCaseUpdate,
  TicketFilters,
} from "@/types/api";

// -------------------------------------------------------------- health
/**
 * Setup readiness (#642/#643): what this account still needs before a run works.
 *
 * Polled rather than fetched once, because the blockers are fixed OUTSIDE this
 * screen — pairing an agent, capturing a login, connecting a provider — and a
 * checklist that keeps showing a solved problem is the fastest way to teach the
 * user to ignore it. `Readiness` is cheap (four small queries server-side).
 */
export const useReadiness = () =>
  useQuery({
    queryKey: queryKeys.readiness,
    queryFn: api.readiness,
    refetchInterval: 30_000,
    // Losing it on a transient blip would flip banners off and back on, which
    // reads as flicker; keep the last answer while refetching.
    placeholderData: (prev: Readiness | undefined) => prev,
  });

export const useCapabilities = () =>
  useQuery({ queryKey: queryKeys.capabilities, queryFn: api.capabilities });

// Claude usage stats for the top-bar chip + panel. The plan-limit % is fetched
// lazily on the server (background CLI `/usage` call), so the first response is
// `limitsStatus: "loading"` (skeleton). Poll fast while loading so the skeleton
// resolves within a couple seconds of the server cache warming up, then back off
// to a light poll once it's "ready"/"unavailable".
export const useAiStats = () =>
  useQuery({
    queryKey: queryKeys.aiStats,
    // Wrap so react-query's fetch context isn't passed as the `force` arg.
    queryFn: () => api.aiStats(),
    refetchInterval: (q) =>
      q.state.data?.limitsStatus === "loading" ? 3_000 : 30_000,
  });

// Manual reload for the stats panel: forces the server to bypass its caches and
// re-read the CLI `/usage`. The fresh result (often `limitsStatus: "loading"`)
// is written straight into the cache so `useAiStats`'s fast poll takes over.
export const useRefreshAiStats = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.aiStats(true),
    onSuccess: (data) => qc.setQueryData(queryKeys.aiStats, data),
  });
};

// Claude CLI credentials (#95) — own (per-user) + shared (admin-only) status.
export const useClaudeCredentialsStatus = () =>
  useQuery({
    queryKey: queryKeys.claudeCredentialsStatus,
    queryFn: api.claudeCredentials.status,
  });

// On-demand credential test (real minimal Claude call). Refresh the status
// afterwards so the passive expired/active indicator reflects the outcome.
export const useTestClaudeCredentials = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (scope?: "effective" | "shared" | "own") =>
      api.claudeCredentials.test(scope),
    onSettled: () =>
      qc.invalidateQueries({ queryKey: queryKeys.claudeCredentialsStatus }),
  });
};

export const useUploadOwnClaudeCredentials = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ClaudeCredentialsUpload) =>
      api.claudeCredentials.uploadOwn(body),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: queryKeys.claudeCredentialsStatus }),
  });
};

export const useSetClaudeCredentialMode = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (mode: "own" | "shared") => api.claudeCredentials.setMode(mode),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: queryKeys.claudeCredentialsStatus }),
  });
};

export const useDeleteOwnClaudeCredentials = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.claudeCredentials.deleteOwn(),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: queryKeys.claudeCredentialsStatus }),
  });
};

export const useUploadSharedClaudeCredentials = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ClaudeCredentialsUpload) =>
      api.claudeCredentials.uploadShared(body),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: queryKeys.claudeCredentialsStatus }),
  });
};

export const useDeleteSharedClaudeCredentials = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.claudeCredentials.deleteShared(),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: queryKeys.claudeCredentialsStatus }),
  });
};

// -------------------------------------------------------------- providers + connections
// Grouped provider catalog (ADR 0006): one entry per kind with its N connections.
export const useProviders = () =>
  useQuery({ queryKey: queryKeys.providers, queryFn: api.listProviders });

export const useCreateConnection = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ kind, name }: { kind: ProviderKind; name: string }) =>
      api.createConnection(kind, { name }),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.providers }),
  });
};

export const useUpdateConnection = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: number; body: ConnectionUpdate }) =>
      api.updateConnection(id, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.providers }),
  });
};

export const useDeleteConnection = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.deleteConnection(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.providers }),
  });
};

// Test a single connection (probe → set connected/last_tested_at). Bound to a
// connection id so each row's Test button drives its own connection.
export const useTestConnection = (id: number) => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.testConnection(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.providers }),
  });
};

export const useConnectionProjects = (id: number | null) =>
  useQuery({
    queryKey: queryKeys.connectionProjects(id ?? 0),
    queryFn: () => api.connectionProjects(id as number),
    enabled: id != null,
    staleTime: 60_000,
  });

export const useConnectionSprints = (id: number | null) =>
  useQuery({
    queryKey: queryKeys.connectionSprints(id ?? 0),
    queryFn: () => api.connectionSprints(id as number),
    enabled: id != null,
    staleTime: 60_000,
  });

export const useConnectionWorkItemMetadata = (id: number | null) =>
  useQuery({
    queryKey: queryKeys.connectionWorkItemMetadata(id ?? 0),
    queryFn: () => api.connectionWorkItemMetadata(id as number),
    enabled: id != null,
    staleTime: 60_000,
  });

export const useConnectionRepos = (id: number | null, enabled: boolean) =>
  useQuery({
    queryKey: queryKeys.connectionRepos(id ?? 0),
    queryFn: () => api.connectionRepos(id as number),
    enabled: id != null && enabled,
    staleTime: 60_000,
  });

export const useSettings = () =>
  useQuery({ queryKey: queryKeys.settings, queryFn: api.getSettings });

export const useUpdateSettings = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: SettingsUpdate) => api.updateSettings(body),
    onSuccess: (data) => qc.setQueryData(queryKeys.settings, data),
  });
};

// -------------------------------------------------------------- projects
export const useProjects = () =>
  useQuery({
    queryKey: queryKeys.projects,
    // The token is deliberately NOT in the key: it changes on every mint, so
    // keying on it would make each fetch a cache miss.
    queryFn: async () => api.listProjects(await hubTokenForRead()),
  });

export const useProjectEnvironments = () =>
  useQuery({
    queryKey: queryKeys.projectEnvironments,
    queryFn: api.listProjectEnvironments,
  });

export const useRefreshProjects = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.refreshProjects,
    onSuccess: (data) => qc.setQueryData(queryKeys.projects, data),
  });
};

// -------------------------------------------------------------- shared namespace (ADR 0009)
// Catalog of the admin-curated shared namespace, so any member can browse
// before cloning (GET /shared/projects — any authed caller).
export const useSharedProjects = () =>
  useQuery({
    queryKey: queryKeys.sharedProjects,
    queryFn: api.listSharedProjects,
    // Poll while any repo's knowledge is building so the "Building…" pill and
    // the Build-knowledge button clear once the background build finishes
    // (mirrors useProjectRepos).
    refetchInterval: (q) =>
      q.state.data?.some((p) =>
        p.knowledge.some((k) => k.status === "indexing"),
      )
        ? 2000
        : false,
  });

// Clone a shared project into the caller's own scope. On success, the
// project + its knowledge now exist under the caller's owner scope, so
// refresh both lists. 409 means the caller already owns that key — surfaced
// as a friendly toast instead of the generic error message.
export const useCloneSharedProject = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (key: string) => api.cloneSharedProject(key),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.projects });
      qc.invalidateQueries({ queryKey: queryKeys.knowledgeList });
      qc.invalidateQueries({ queryKey: queryKeys.sharedProjects });
      toast.success(
        "Project cloned. Re-bind its provider connections in Project Settings to enable sync.",
      );
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 409) {
        toast.error("You already have this project.");
        return;
      }
      toast.error(e instanceof Error ? e.message : "Failed to clone project");
    },
  });
};

// Admin: create/update the shared project shell + config (owner_id=None).
export const useCreateSharedProject = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ key, body }: { key: string; body: SharedProjectCreate }) =>
      api.createSharedProject(key, body),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: queryKeys.sharedProjects }),
  });
};

// Admin: build/rebuild the shared project's bare-key knowledge base.
export const useBuildSharedKnowledge = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ key, body }: { key: string; body: KnowledgeBuildRequest }) =>
      api.buildSharedKnowledge(key, body),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: queryKeys.sharedProjects }),
  });
};

// Admin: build/rebuild a shared project's per-repo knowledge base.
export const useBuildSharedRepoKnowledge = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      key,
      repo,
      body,
    }: {
      key: string;
      repo: string;
      body: KnowledgeBuildRequest;
    }) => api.buildSharedRepoKnowledge(key, repo, body),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: queryKeys.sharedProjects }),
  });
};

// Admin: the shared project's full config for the settings page (owner_id=None).
export const useSharedProjectConfig = (key: string | null) =>
  useQuery({
    queryKey: queryKeys.sharedProjectConfig(key ?? ""),
    queryFn: () => api.getSharedProjectConfig(key as string),
    enabled: !!key,
    retry: false,
  });

// Admin: the shared project's saved manual-login session (polls while capturing).
export const useSharedProjectAuth = (key: string | null) =>
  useQuery({
    queryKey: queryKeys.sharedProjectAuth(key ?? ""),
    queryFn: () => api.getSharedProjectAuth(key as string),
    enabled: !!key,
    retry: false,
    refetchInterval: (q) => (q.state.data?.capturing ? 1500 : false),
  });

export const useClearSharedProjectAuth = (key: string) => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.clearSharedProjectAuth(key),
    onSuccess: (data) =>
      qc.setQueryData(queryKeys.sharedProjectAuth(key), data),
  });
};

export const useCaptureSharedProjectAuth = (key: string) => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.captureSharedProjectAuth(key),
    onSuccess: (data) =>
      qc.setQueryData(queryKeys.sharedProjectAuth(key), data),
  });
};

/**
 * All knowledge rows, including the hub status-only rows the server appends (#603).
 *
 * `enabled` exists for the Projects grid: those hub rows are projected from the
 * summary `GET /projects` mirrors into the project row, so asking for knowledge
 * *before* the projects request has landed can legitimately answer "nothing from
 * the hub yet" and paint a stale badge. The grid gates on its projects query;
 * every other caller leaves it alone.
 */
export const useKnowledgeList = (enabled = true) =>
  useQuery({ queryKey: queryKeys.knowledgeList, queryFn: api.listKnowledge, enabled });

export const useProjectKnowledge = (key: string | null) =>
  useQuery({
    queryKey: queryKeys.projectKnowledge(key ?? ""),
    queryFn: async () =>
      api.getProjectKnowledge(key as string, await hubTokenForRead()),
    enabled: !!key,
    retry: false,
  });

export const useBuildKnowledge = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ key, body }: { key: string; body: KnowledgeBuildRequest }) =>
      api.buildKnowledge(key, body),
    onSuccess: (data) => {
      qc.setQueryData(queryKeys.projectKnowledge(data.key), data);
      qc.invalidateQueries({ queryKey: queryKeys.knowledgeList });
    },
  });
};

export const useProjectConfig = (key: string | null) =>
  useQuery({
    queryKey: queryKeys.projectConfig(key ?? ""),
    queryFn: async () =>
      api.getProjectConfig(key as string, await hubTokenForRead()),
    enabled: !!key,
    retry: false,
  });

export const useSaveProjectConfig = (key: string) => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ProjectConfigUpdate) => api.saveProjectConfig(key, body),
    onSuccess: (data) => {
      qc.setQueryData(queryKeys.projectConfig(key), data);
      qc.invalidateQueries({ queryKey: queryKeys.projectRepos(key) });
    },
  });
};

export const useProjectAuth = (key: string | null) =>
  useQuery({
    queryKey: queryKeys.projectAuth(key ?? ""),
    queryFn: () => api.getProjectAuth(key as string),
    enabled: !!key,
    retry: false,
    // While a capture is running on the host, poll so the UI flips to
    // "captured" automatically once the operator finishes logging in.
    refetchInterval: (q) => (q.state.data?.capturing ? 1500 : false),
  });

export const useClearProjectAuth = (key: string) => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.clearProjectAuth(key),
    onSuccess: (data) => {
      qc.setQueryData(queryKeys.projectAuth(key), data);
      qc.invalidateQueries({ queryKey: queryKeys.projectAuth(key) });
    },
  });
};

export const useCaptureProjectAuth = (key: string) => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.captureProjectAuth(key),
    onSuccess: (data) => {
      // Seed the cache with `capturing: true` so useProjectAuth starts polling.
      qc.setQueryData(queryKeys.projectAuth(key), data);
      qc.invalidateQueries({ queryKey: queryKeys.projectAuth(key) });
    },
  });
};

export const useProjectRepos = (key: string | null) =>
  useQuery({
    queryKey: queryKeys.projectRepos(key ?? ""),
    queryFn: async () =>
      api.listProjectRepos(key as string, await hubTokenForRead()),
    enabled: !!key,
    refetchInterval: (q) =>
      q.state.data?.some((r) => r.status === "indexing") ? 2000 : false,
  });

export const useRepoKnowledge = (key: string | null, repo: string | null) =>
  useQuery({
    queryKey: queryKeys.repoKnowledge(key ?? "", repo ?? ""),
    queryFn: async () =>
      api.getRepoKnowledge(
        key as string,
        repo as string,
        await hubTokenForRead(),
      ),
    enabled: !!key && !!repo,
    retry: false,
  });

export const useBuildRepoKnowledge = (key: string) => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      repo,
      body,
    }: {
      repo: string;
      body: KnowledgeBuildRequest;
    }) => api.buildRepoKnowledge(key, repo, body),
    onSuccess: (data, vars) => {
      qc.setQueryData(queryKeys.repoKnowledge(key, vars.repo), data);
      qc.invalidateQueries({ queryKey: queryKeys.projectRepos(key) });
    },
  });
};

// -------------------------------------------------------------- tickets
/** Page size for screens that need "every ticket" for lookups/counts (Automation,
 * CreateLinkSync, ProjectDetail, RunDetail, CreateRunModal) rather than the
 * paginated Tickets screen's page-at-a-time list. */
export const ALL_TICKETS_PAGE_SIZE = 1000;

/**
 * Is an EmeHub token even obtainable in this deployment? (#500)
 *
 * Memoised because it is *deployment configuration* — `GET /health`'s SSO flag —
 * not hub data. Caching the answer avoids a `/health` probe on every ticket
 * fetch; caching hub data itself is explicitly forbidden (the hub has no webhook,
 * ETag or revision counter, so a data cache goes stale silently). With the flag
 * off this resolves `false` once and no hub call is ever attempted.
 */
let hubConfigProbe: Promise<HubSsoConfig> | null = null;
/** `GET /health`'s deployment flags, probed at most once per page load. Never
 * rejects — an unreachable backend resolves to "no hub", which is the safe read
 * for both the token path below and the read-only-UI switch (#528). */
function hubConfig(): Promise<HubSsoConfig> {
  hubConfigProbe ??= fetchHubSsoConfig().catch((): HubSsoConfig => ({
    hubSsoEnabled: false,
    hubDataEnabled: false,
    hubBaseUrl: "",
  }));
  return hubConfigProbe;
}

function hubTokenObtainable(): Promise<boolean> {
  return hubConfig().then((cfg) => cfg.hubSsoEnabled && !!cfg.hubBaseUrl);
}

/**
 * Does EmeHub own Claude credentials and projects here? (#528)
 *
 * `resolved` is separate from `enabled` on purpose: until `/health` answers we
 * don't know, and rendering a self-configuration control we may be about to hide
 * is exactly the defect this closes. Callers hide such controls until
 * `resolved`, and gate side effects (Projects' one-shot auto-refresh) on it too.
 *
 * Deployment configuration, so it is cached for the whole session — unlike hub
 * *data*, which is never cached (the hub has no webhook or revision counter).
 */
export function useHubDataEnabled(): { enabled: boolean; resolved: boolean } {
  const { data } = useQuery({
    queryKey: ["hub", "dataEnabled"] as const,
    queryFn: () => hubConfig().then((cfg) => cfg.hubDataEnabled),
    staleTime: Infinity,
    gcTime: Infinity,
    retry: false,
  });
  return { enabled: data === true, resolved: data !== undefined };
}

/**
 * EmeHub's **web** origin for deep links, or `null` when there isn't one.
 *
 * `hubBaseUrl` from `/health` carries the API prefix (`…/api`); the UI lives at
 * the origin. Shares the one `/health` probe with `useHubDataEnabled`, and
 * resolves `null` rather than throwing so a missing link is just a missing link.
 */
export function useHubWebUrl(): string | null {
  const { data } = useQuery({
    queryKey: ["hub", "webUrl"] as const,
    queryFn: () =>
      hubConfig().then(
        (cfg) => cfg.hubBaseUrl.replace(/\/api\/?$/, "") || null,
      ),
    staleTime: Infinity,
    gcTime: Infinity,
    retry: false,
  });
  return data ?? null;
}

/**
 * Mint a hub token for one hub-backed request, or `null`.
 *
 * Used for ticket reads and, since #505, for the three run-start mutations —
 * which is what lets the backend resolve the Claude credential from the hub
 * (#499) instead of always falling back to the local one.
 *
 * Never throws and never blocks the query on the hub: `mintHubDataToken()`
 * already swallows every failure into `null`, and a `null` token simply means the
 * backend serves local tickets. That is the whole degradation story — the hub
 * being down must never turn into an error state or an empty list (#491).
 */
async function hubTokenForRead(): Promise<string | null> {
  if (!(await hubTokenObtainable())) return null;
  return mintHubDataToken();
}

/**
 * The credential EmeHub would resolve for this user (#512).
 *
 * Mints a hub token per read like every other hub-backed query. Always resolves —
 * `available: false` when the hub can't be consulted — so Settings renders
 * whether or not the hub is reachable.
 */
export const useHubClaudeCredential = () =>
  useQuery({
    queryKey: ["claude-credentials", "hub"] as const,
    queryFn: async () => api.claudeCredentials.hub(await hubTokenForRead()),
  });

export const useTickets = (filters: TicketFilters = {}) =>
  useQuery({
    // The token is deliberately NOT part of the key: it changes every mint, and
    // keying on it would make every fetch a cache miss.
    queryKey: queryKeys.tickets(
      filters as Record<string, string | number | undefined>,
    ),
    queryFn: async () => api.listTickets(filters, await hubTokenForRead()),
  });

/**
 * The query builder's dropdown values, plus whether these tickets are the hub's
 * to manage (#517).
 *
 * Deliberately **not** hub-token-carrying: the endpoint is a distinct read over
 * local rows, so it resolves whether or not EmeHub is reachable. That is the
 * point — the screen must stay usable on local rows with no error state (#491),
 * and the Sync control's visibility must not flicker with hub availability.
 */
export const useTicketFilterOptions = (
  connectionId: number | null,
  providerKind: string | null,
) =>
  useQuery({
    queryKey: queryKeys.ticketFilterOptions(connectionId, providerKind),
    queryFn: () => api.ticketFilterOptions(connectionId, providerKind),
    staleTime: 60_000,
  });

export const useTicket = (externalId: string | null) =>
  useQuery({
    queryKey: queryKeys.ticket(externalId ?? ""),
    queryFn: () => api.getTicket(externalId as string),
    enabled: !!externalId,
  });

export const useLinkedCases = (externalId: string | null) =>
  useQuery({
    queryKey: queryKeys.linkedCases(externalId ?? ""),
    queryFn: () => api.linkedCases(externalId as string),
    enabled: !!externalId,
  });

export const useSyncTickets = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: SyncRequest) => api.syncTickets(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["tickets"] }),
  });
};

/** Local-delete a single ticket (never calls the provider; a re-sync restores it). */
export const useDeleteTicket = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (externalId: string) => api.deleteTicket(externalId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["tickets"] }),
  });
};

/** Bulk local-delete tickets by external id; resolves with the deleted count. */
export const useDeleteTickets = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (externalIds: string[]) => api.deleteTickets(externalIds),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["tickets"] }),
  });
};

// -------------------------------------------------------------- runs
export const useRuns = () =>
  useQuery({ queryKey: queryKeys.runs, queryFn: api.listRuns });

// Statuses where the backend is actively advancing the run (not waiting on the
// user and not terminal). The run WebSocket drives refreshes, but its events are
// fire-and-forget — a dropped/late terminal event leaves the detail screen frozen
// on a stale snapshot (e.g. "0/N" with tickets stuck "Generating…"). Polling
// while progressing self-heals that; it stops once the run is idle/terminal.
const PROGRESSING_RUN_STATUSES = new Set([
  "processing",
  "sync",
  "automation",
  "executing",
  "comment",
]);

export const useRun = (
  runId: number | string | null,
  opts?: Partial<UseQueryOptions>,
) =>
  useQuery({
    queryKey: queryKeys.run(runId ?? 0),
    queryFn: () => api.getRun(runId as number),
    enabled: runId != null,
    refetchInterval: (q) =>
      PROGRESSING_RUN_STATUSES.has(
        (q.state.data as RunOut | undefined)?.status ?? "",
      )
        ? 2500
        : false,
    ...(opts as object),
  });

export const useCreateRun = () => {
  const qc = useQueryClient();
  return useMutation({
    // Carries a freshly-minted hub token so the backend can resolve the Claude
    // credential from EmeHub at run start (#499/#505). `null` -> no header, and
    // the run starts on the local credential exactly as before: a run must never
    // be blocked because the hub is unreachable or there is no hub session.
    mutationFn: async (body: RunCreate) =>
      api.createRun(body, await hubTokenForRead()),
    onSuccess: (run) => {
      qc.invalidateQueries({ queryKey: queryKeys.runs });
      qc.setQueryData(queryKeys.run(run.id), run);
    },
  });
};

// Seed (or fetch the existing) sample run for the product tour / Getting Started
// page. Idempotent server-side; on success refresh the Runs list and prime the
// run detail cache so navigating straight into it is instant.
export const useEnsureSampleRun = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.createSampleRun(),
    onSuccess: (run) => {
      qc.invalidateQueries({ queryKey: queryKeys.runs });
      qc.setQueryData(queryKeys.run(run.id), run);
    },
  });
};

export const useRegenerateRun = (runId: number | string) => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => api.regenerateRun(runId, await hubTokenForRead()),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.run(runId) }),
  });
};

export const useCancelRun = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (runId: number | string) => api.cancelRun(runId),
    onSuccess: (run) => {
      qc.invalidateQueries({ queryKey: queryKeys.runs });
      qc.invalidateQueries({ queryKey: queryKeys.run(run.id) });
    },
  });
};

export const useStopRun = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (runId: number | string) => api.stopRun(runId),
    onSuccess: (run) => {
      qc.invalidateQueries({ queryKey: queryKeys.runs });
      qc.invalidateQueries({ queryKey: queryKeys.run(run.id) });
      qc.invalidateQueries({ queryKey: queryKeys.specs(run.id) });
      qc.invalidateQueries({ queryKey: queryKeys.execution(run.id) });
    },
  });
};

export const useRetryRun = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (runId: number | string) =>
      api.retryRun(runId, await hubTokenForRead()),
    onSuccess: (run) => {
      qc.invalidateQueries({ queryKey: queryKeys.runs });
      qc.invalidateQueries({ queryKey: queryKeys.run(run.id) });
    },
  });
};

export const useDeleteRun = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (runId: number | string) => api.deleteRun(runId),
    onSuccess: (_data, runId) => {
      qc.invalidateQueries({ queryKey: queryKeys.runs });
      qc.invalidateQueries({ queryKey: queryKeys.run(runId) });
    },
  });
};

export const useRunRepos = (runId: number | string | null) =>
  useQuery({
    queryKey: queryKeys.runRepos(runId ?? 0),
    queryFn: () => api.runRepos(runId as number),
    enabled: runId != null,
  });

// Per-process AI usage + cost for a run. Errors gracefully (e.g. 404 while the
// backend endpoint is still rolling out) — the card just doesn't render. Polls
// lightly while the run is still producing AI work.
export const useRunAiUsage = (runId: number | string | null) =>
  useQuery({
    queryKey: queryKeys.runAiUsage(runId ?? 0),
    queryFn: () => api.runAiUsage(runId as number),
    enabled: runId != null,
    retry: false,
    refetchInterval: 15_000,
  });

export const useSetRunTicketRepo = (runId: number | string) => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ tid, repo }: { tid: string; repo: string }) =>
      api.setRunTicketRepo(runId, tid, repo),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.run(runId) });
      qc.invalidateQueries({ queryKey: queryKeys.runRepos(runId) });
    },
  });
};

// -------------------------------------------------------------- review / cases
export const useRunCases = (runId: number | string | null) =>
  useQuery({
    queryKey: queryKeys.runCases(runId ?? 0),
    queryFn: () => api.listCases(runId as number),
    enabled: runId != null,
  });

export const useCaseMutations = (runId: number | string) => {
  const qc = useQueryClient();
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: queryKeys.runCases(runId) });
    qc.invalidateQueries({ queryKey: queryKeys.run(runId) });
  };
  // Optimistic approval writes (#635): flip the case(s) in the cache immediately
  // so the button and the per-ticket progress bar react on click, then reconcile
  // with the server on settle. On error the snapshot is restored.
  const optimistic = <V,>(mapFor: (v: V) => (c: TestCaseOut) => TestCaseOut) => ({
    onMutate: async (v: V) => {
      const key = queryKeys.runCases(runId);
      await qc.cancelQueries({ queryKey: key });
      const prev = qc.getQueryData<TestCaseOut[]>(key);
      qc.setQueryData<TestCaseOut[]>(key, (old) => old?.map(mapFor(v)));
      return { prev };
    },
    onError: (
      _e: unknown,
      _v: unknown,
      ctx: { prev?: TestCaseOut[] } | undefined,
    ) => {
      if (ctx?.prev) qc.setQueryData(queryKeys.runCases(runId), ctx.prev);
    },
    onSettled: invalidate,
  });
  return {
    addCase: useMutation({
      mutationFn: (body: TestCaseCreate) => api.addCase(runId, body),
      onSuccess: invalidate,
    }),
    updateCase: useMutation({
      mutationFn: ({
        caseId,
        body,
      }: {
        caseId: number;
        body: TestCaseUpdate;
      }) => api.updateCase(caseId, body),
      onSuccess: invalidate,
    }),
    setApproval: useMutation({
      mutationFn: ({
        caseId,
        approval,
      }: {
        caseId: number;
        approval: "approved" | "rejected" | "pending";
      }) => api.setApproval(caseId, approval),
      ...optimistic(
        ({ caseId, approval }) =>
          (c) =>
            c.id === caseId ? { ...c, approval } : c,
      ),
    }),
    regenerateCase: useMutation({
      mutationFn: async (caseId: number) =>
        api.regenerateCase(caseId, await hubTokenForRead()),
      onSuccess: invalidate,
    }),
    approveAll: useMutation({
      mutationFn: () => api.approveAll(runId),
      ...optimistic<void>(() => (c) => ({ ...c, approval: "approved" as const })),
    }),
    approveTicket: useMutation({
      mutationFn: (tid: string) => api.approveTicket(runId, tid),
      ...optimistic(
        (tid) => (c) =>
          c.ticketExternalId === tid
            ? { ...c, approval: "approved" as const }
            : c,
      ),
    }),
  };
};

// -------------------------------------------------------------- create & link
export const useLinkStatus = (runId: number | string | null) =>
  useQuery({
    queryKey: queryKeys.linkStatus(runId ?? 0),
    queryFn: () => api.linkStatus(runId as number),
    enabled: runId != null,
    refetchInterval: (q) => (q.state.data?.status === "running" ? 1200 : false),
  });

/** Key the create/link mutation so a DIFFERENT screen can see it in flight (#694).
 *
 * Review fires this and navigates to Sync in the same tick, so Sync renders before
 * the request comes back — with `linkStatus` still `"idle"`, i.e. showing the same
 * big Create button again, which reads as a lost click rather than as progress. */
export const CREATE_LINK_MUTATION_KEY = ["create-link"] as const;

export const useCreateAndLink = (runId: number | string) => {
  const qc = useQueryClient();
  return useMutation({
    mutationKey: CREATE_LINK_MUTATION_KEY,
    mutationFn: (body: {
      link?: boolean;
      ticketIds?: string[];
      dryRun?: boolean;
    }) => api.createAndLink(runId, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.linkStatus(runId) });
      qc.invalidateQueries({ queryKey: queryKeys.run(runId) });
    },
  });
};

// -------------------------------------------------------------- automation
export const useSpecs = (runId: number | string | null) =>
  useQuery<AutomationSpecOut[]>({
    queryKey: queryKeys.specs(runId ?? 0),
    queryFn: () => api.listSpecs(runId as number),
    enabled: runId != null,
  });

export const useAutomationStatus = (runId: number | string | null) =>
  useQuery({
    queryKey: queryKeys.automationStatus(runId ?? 0),
    queryFn: () => api.automationStatus(runId as number),
    enabled: runId != null,
    refetchInterval: (q) => (q.state.data?.generating ? 1500 : false),
  });

export const useGenerateAutomation = (runId: number | string) => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (force?: boolean) =>
      api.generateAutomation(runId, force ?? false, await hubTokenForRead()),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.specs(runId) });
      qc.invalidateQueries({ queryKey: queryKeys.run(runId) });
      qc.invalidateQueries({ queryKey: queryKeys.automationStatus(runId) });
    },
  });
};

/** Kick off an async spec regeneration. Resolves as soon as the background job
 * is accepted; the finished spec arrives over the run WS as `spec.regenerated`
 * (the Automation screen refreshes + diffs on that event), so there is nothing
 * to invalidate here. `runId` is accepted for call-site symmetry. */
export const useRegenerateSpec = (_runId: number | string) =>
  useMutation({
    mutationFn: async ({ caseId, comment }: { caseId: number; comment?: string }) =>
      api.regenerateSpec(caseId, comment, await hubTokenForRead()),
  });

/** Send a chat instruction to edit the selected spec. Fire-and-forget like
 * `useRegenerateSpec`: the edited spec + reply text arrive over the run WS as
 * `automation.chat.reply` (the `automation.` prefix already refreshes the specs
 * cache), so there is nothing to invalidate here. `runId` is accepted for
 * call-site symmetry. */
export const useSendSpecChat = (_runId: number | string) =>
  useMutation({
    mutationFn: ({
      caseId,
      message,
      model,
      messageId,
    }: {
      caseId: number;
      message: string;
      model?: string;
      messageId?: string;
    }) => api.sendSpecChat(caseId, message, model, messageId),
  });

// ------------------------------------- Pause / continue live authoring (#619)

/**
 * The case's live-authoring pause state.
 *
 * Polled while a session is live rather than read only from the WS: the `paused`
 * event fires once, so a reload mid-pause would otherwise leave the panel with a
 * spinner and no Continue — while the user's own machine keeps a Chrome window
 * open waiting for it.
 */
export const useAuthoringState = (caseId: number, enabled: boolean) =>
  useQuery({
    queryKey: queryKeys.authoringState(caseId),
    queryFn: () => api.getAuthoringState(caseId),
    enabled: enabled && caseId > 0,
    refetchInterval: enabled ? 4000 : false,
  });

export const usePauseAuthoring = (caseId: number) => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.pauseAuthoring(caseId),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.authoringState(caseId) }),
  });
};

export const useContinueAuthoring = (caseId: number) => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (guidance: string) => api.continueAuthoring(caseId, guidance),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.authoringState(caseId) }),
  });
};

/** Cancel a live-authoring session (#645). Invalidates the specs list too: the
 *  cancelled case's placeholder spec becomes `failed` with the reason, which is
 *  what the screen shows instead of a spinner that never resolves. */
export const useCancelAuthoring = (caseId: number) => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.cancelAuthoring(caseId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.authoringState(caseId) });
      // Coarse on purpose: the control is rendered in two places that don't both
      // know the run id, and cancelling is a rare, deliberate action — one extra
      // refetch is cheaper than threading `runId` through for it.
      qc.invalidateQueries({ queryKey: ["runs"] });
    },
  });
};

export const useUpdateSpec = (runId: number | string) => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ caseId, code }: { caseId: number; code: string }) =>
      api.updateSpec(caseId, code),
    onSuccess: (spec) => {
      qc.setQueryData<AutomationSpecOut[]>(queryKeys.specs(runId), (prev) =>
        prev ? prev.map((s) => (s.id === spec.id ? spec : s)) : prev,
      );
      qc.invalidateQueries({ queryKey: queryKeys.specs(runId) });
    },
  });
};

// Start a self-heal pass for a case. The POST only *kicks off* the background
// loop; the spec query is invalidated on the terminal WS event, not here.
export const useHealSpec = (_runId: number | string) =>
  useMutation({
    mutationFn: async (caseId: number) =>
      api.healSpec(caseId, await hubTokenForRead()),
  });

// Poll heal status so the "Healing…" state survives navigating away and back.
export const useHealStatus = (caseId: number, enabled: boolean) =>
  useQuery({
    queryKey: queryKeys.healStatus(caseId),
    queryFn: () => api.healStatus(caseId),
    enabled: !!caseId && enabled,
    // Fetch once on select (catches a heal already running after navigation),
    // then poll only while a heal is actually in flight — the live button state
    // during an active session is driven by the WS stream + mutation isPending.
    refetchInterval: (q) => (q.state.data?.healing ? 1500 : false),
  });

// Start a DOM-exploration session for a blocked case (ADR 0010). Like self-heal,
// the POST only kicks off the background observe→decide→act loop; progress
// arrives over the run WS (`explore.progress`) and the repo-scoped status poll.
export const useExploreSpec = () =>
  useMutation({
    mutationFn: ({
      projectKey,
      repo,
      body,
    }: {
      projectKey: string;
      repo: string;
      body: ExploreRequest;
    }) => api.exploreSpec(projectKey, repo, body),
  });

// Poll exploration status so the "Exploring…" state (and the discovered summary)
// survives navigating away and back. Repo-scoped — one session per repo at a time.
export const useExploreStatus = (
  projectKey: string,
  repo: string,
  enabled: boolean,
) =>
  useQuery({
    queryKey: queryKeys.exploreStatus(projectKey, repo),
    queryFn: () => api.exploreStatus(projectKey, repo),
    enabled: enabled && !!projectKey && !!repo,
    // Fetch once on select (catches a session already running after navigation),
    // then poll only while a session is actually in flight — mirrors useHealStatus.
    refetchInterval: (q) => (q.state.data?.exploring ? 1500 : false),
  });

// Prefill + readiness for exporting the automation project (#549). A plain query:
// it pushes nothing, so it is safe to fetch whenever the export panel is open.
export const useAutomationExportPreflight = (
  runId: number | string,
  projectId: number | null,
  enabled: boolean,
) =>
  useQuery({
    queryKey: queryKeys.automationExport(runId, projectId),
    queryFn: () => api.automationExportPreflight(runId, projectId),
    enabled: enabled && projectId != null,
    retry: false,
  });

// The export itself — the **only** push trigger in the client, deliberately a
// mutation behind an explicit button. Nothing invalidates or re-runs it on its own:
// pushing AI-authored commits to a customer's remote must never happen as a side
// effect of rendering. Invalidates the preflight so the reported HEAD/commit
// refreshes after a successful push.
export const useExportAutomationProject = (runId: number | string) => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: {
      remoteUrl: string;
      branch: string;
      projectId?: number | null;
      message?: string;
    }) => api.exportAutomationProject(runId, body),
    onSuccess: (_result, body) =>
      qc.invalidateQueries({
        queryKey: queryKeys.automationExport(runId, body.projectId ?? null),
      }),
  });
};

// The last self-heal trail for a case (per-attempt error + diff + outcome).
export const useHealReport = (caseId: number, enabled: boolean) =>
  useQuery({
    queryKey: queryKeys.healReport(caseId),
    queryFn: () => api.healReport(caseId),
    enabled: !!caseId && enabled,
  });

// Run just one case's spec (the "run this test" action). Invalidates the run's
// execution so the per-spec status dots refresh.
export const useRunSpec = (runId: number | string) => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (caseId: number) => api.runSpec(caseId),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: queryKeys.execution(runId) }),
  });
};

// -------------------------------------------------------------- execution
export const useExecution = (runId: number | string | null) =>
  useQuery({
    queryKey: queryKeys.execution(runId ?? 0),
    queryFn: () => api.getExecution(runId as number),
    enabled: runId != null,
    retry: false,
    // Poll while an execution is in flight so run state (incl. the per-spec
    // "Run" button) clears promptly even if a WS event is missed.
    refetchInterval: (q) => (q.state.data?.status === "running" ? 1200 : false),
  });

export const useStartExecution = (runId: number | string) => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (
      body: { workers?: number; env?: string; target?: ExecutionTarget } = {},
    ) => api.startExecution(runId, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.execution(runId) });
      qc.invalidateQueries({ queryKey: queryKeys.run(runId) });
    },
  });
};

// -------------------------------------------------------------- local agent devices
/** Paired Local Agent devices for the current user (Local Agent feature).
 *
 * Polled every 5s so (a) a device paired from the CLI/agent app shows up
 * without a manual refresh, and (b) each device's `lastSeenAt` stays fresh —
 * the Local Agent screen derives a live Connected/Offline badge from it. */
export const useAgentDevices = () =>
  useQuery({
    queryKey: queryKeys.agentDevices,
    queryFn: api.agentDevices.list,
    refetchInterval: 5_000,
  });

/** Issue a short-lived pairing code for `npx @q-agent/agent pair <code>`. */
export const usePairCode = () =>
  useMutation({ mutationFn: api.agentDevices.pairCode });

/** Revoke a paired device; refreshes the device list on success. */
export const useRevokeDevice = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.agentDevices.revoke(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.agentDevices }),
  });
};

// -------------------------------------------------------------- evidence
export const useEvidence = (runId: number | string | null) =>
  useQuery({
    queryKey: queryKeys.evidence(runId ?? 0),
    queryFn: () => api.getEvidence(runId as number),
    enabled: runId != null,
    retry: false,
  });

export const useAnnotate = (runId: number | string) => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      evidenceId,
      shapes,
    }: {
      evidenceId: number;
      shapes: AnnotationShape[];
    }) => api.annotate(evidenceId, shapes),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: queryKeys.evidence(runId) }),
  });
};

// Auto-analyze a failure screenshot with Claude vision and burn annotations on it.
export const useAutoAnnotate = (runId: number | string) => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (evidenceId: number) =>
      api.autoAnnotateEvidence(evidenceId, await hubTokenForRead()),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: queryKeys.evidence(runId) }),
  });
};

// -------------------------------------------------------------- reports
export const useReport = (runId: number | string | null) =>
  useQuery({
    queryKey: queryKeys.report(runId ?? 0),
    queryFn: () => api.getReport(runId as number),
    enabled: runId != null,
    retry: false,
  });

export const useReports = () =>
  useQuery({ queryKey: queryKeys.reports, queryFn: api.listReports });

// -------------------------------------------------------------- audit log
export const useAuditEvents = (filters: {
  category?: string;
  actor?: string;
  q?: string;
  run?: string;
}) =>
  useQuery({
    queryKey: queryKeys.auditEvents(filters),
    queryFn: () => api.auditEvents(filters),
  });

/** Per-run activity timeline (#394): the run's audit events, newest first.
 * `live` enables ~2s polling so an in-progress run's events stream in. */
export const useRunActivity = (runCode: string | undefined, live = false) =>
  useQuery({
    queryKey: queryKeys.auditEvents({ run: runCode ?? "" }),
    queryFn: () => api.auditEvents({ run: runCode! }),
    enabled: !!runCode,
    refetchInterval: live ? 2000 : false,
  });

export const useAuditStats = () =>
  useQuery({ queryKey: queryKeys.auditStats, queryFn: api.auditStats });

export const useClearAuditEvents = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.clearAuditEvents,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["audit", "events"] });
      qc.invalidateQueries({ queryKey: queryKeys.auditStats });
    },
  });
};

// `live` enables ~1.5s polling for the log tail.
export const useBackendLogs = (
  filters: { level?: string; service?: string; q?: string },
  live: boolean,
) =>
  useQuery({
    queryKey: queryKeys.backendLogs(filters),
    queryFn: () => api.backendLogs(filters),
    refetchInterval: live ? 1500 : false,
  });

export const useBackendLogStats = (live: boolean) =>
  useQuery({
    queryKey: queryKeys.backendLogStats,
    queryFn: api.backendLogStats,
    refetchInterval: live ? 1500 : false,
  });

export const useBuildReport = (runId: number | string) => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.buildReport(runId),
    onSuccess: (r) => qc.setQueryData(queryKeys.report(runId), r),
  });
};

// -------------------------------------------------------------- comments / publish
export const useComments = (runId: number | string | null) =>
  useQuery({
    queryKey: queryKeys.comments(runId ?? 0),
    queryFn: () => api.listComments(runId as number),
    enabled: runId != null,
  });

export const useCommentMutations = (runId: number | string) => {
  const qc = useQueryClient();
  const invalidate = () =>
    qc.invalidateQueries({ queryKey: queryKeys.comments(runId) });
  return {
    prepare: useMutation({
      mutationFn: async () => api.prepareComments(runId, await hubTokenForRead()),
      onSuccess: invalidate,
    }),
    edit: useMutation({
      mutationFn: ({
        commentId,
        body,
      }: {
        commentId: number;
        body: { body?: string; targetStatus?: string };
      }) => api.editComment(commentId, body),
      onSuccess: invalidate,
    }),
    regenerate: useMutation({
      mutationFn: async (commentId: number) =>
        api.regenerateComment(commentId, await hubTokenForRead()),
      onSuccess: invalidate,
    }),
    publishOne: useMutation({
      mutationFn: (commentId: number) => api.publishComment(commentId),
      onSuccess: invalidate,
    }),
    publishAll: useMutation({
      mutationFn: (ticketIds: string[]) => api.publishAll(runId, ticketIds),
      onSuccess: invalidate,
    }),
    retry: useMutation({
      mutationFn: () => api.retryComments(runId),
      onSuccess: invalidate,
    }),
  };
};
