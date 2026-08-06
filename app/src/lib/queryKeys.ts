/** Central TanStack Query key factory — keeps cache keys consistent across screens. */

export const queryKeys = {
  capabilities: ["capabilities"] as const,
  aiStats: ["aiStats"] as const,
  claudeCredentialsStatus: ["claudeCredentials", "status"] as const,
  providers: ["providers"] as const,
  connectionProjects: (id: number) => ["connections", id, "projects"] as const,
  connectionSprints: (id: number) => ["connections", id, "sprints"] as const,
  connectionWorkItemMetadata: (id: number) =>
    ["connections", id, "work-item-metadata"] as const,
  connectionRepos: (id: number) => ["connections", id, "repos"] as const,
  settings: ["settings"] as const,
  projects: ["projects"] as const,
  sharedProjects: ["shared", "projects"] as const,
  sharedProjectConfig: (key: string) => ["shared", "projects", key, "config"] as const,
  sharedProjectAuth: (key: string) => ["shared", "projects", key, "auth"] as const,
  knowledgeList: ["projects", "knowledge"] as const,
  projectKnowledge: (key: string) => ["projects", key, "knowledge"] as const,
  projectConfig: (key: string) => ["projects", key, "config"] as const,
  projectAuth: (key: string) => ["projects", key, "auth"] as const,
  projectRepos: (key: string) => ["projects", key, "repos"] as const,
  repoKnowledge: (key: string, repo: string) =>
    ["projects", key, "repos", repo, "knowledge"] as const,
  tickets: (filters?: Record<string, string | number | undefined>) =>
    ["tickets", filters ?? {}] as const,
  ticket: (externalId: string) => ["tickets", "detail", externalId] as const,
  ticketFilterOptions: (connectionId: number | null, providerKind: string | null) =>
    ["tickets", "filter-options", connectionId ?? 0, providerKind ?? ""] as const,
  linkedCases: (externalId: string) => ["tickets", externalId, "linked-cases"] as const,
  linkStatus: (runId: number | string) => ["runs", runId, "linked"] as const,
  runs: ["runs"] as const,
  run: (runId: number | string) => ["runs", runId] as const,
  runRepos: (runId: number | string) => ["runs", runId, "repos"] as const,
  runAiUsage: (runId: number | string) => ["runs", runId, "ai-usage"] as const,
  runCases: (runId: number | string) => ["runs", runId, "cases"] as const,
  specs: (runId: number | string) => ["runs", runId, "automation"] as const,
  automationStatus: (runId: number | string) =>
    ["runs", runId, "automation", "status"] as const,
  healStatus: (caseId: number) => ["cases", caseId, "heal", "status"] as const,
  healReport: (caseId: number) => ["cases", caseId, "heal", "report"] as const,
  exploreStatus: (projectKey: string, repo: string) =>
    ["projects", projectKey, "repos", repo, "explore", "status"] as const,
  execution: (runId: number | string) => ["runs", runId, "execution"] as const,
  evidence: (runId: number | string) => ["runs", runId, "evidence"] as const,
  report: (runId: number | string) => ["runs", runId, "report"] as const,
  reports: ["reports"] as const,
  comments: (runId: number | string) => ["runs", runId, "comments"] as const,
  auditEvents: (filters?: Record<string, string | undefined>) =>
    ["audit", "events", filters ?? {}] as const,
  auditStats: ["audit", "stats"] as const,
  backendLogs: (filters?: Record<string, string | undefined>) =>
    ["audit", "logs", filters ?? {}] as const,
  backendLogStats: ["audit", "logs", "stats"] as const,
  agentDevices: ["agentDevices"] as const,
};
