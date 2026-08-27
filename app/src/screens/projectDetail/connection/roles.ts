import type { ProjectConfigOut, ProjectConfigUpdate, ProviderCategory } from "@/types/api";

/**
 * The project's three connection roles (ADR 0015 §3).
 *
 * A project holds several connections and exactly one fills each role. Two of
 * the three already existed as `ProjectConfig` columns (ADR 0006 §3) — this tab
 * mostly *surfaces* bindings the backend has had all along; only `testCase` is
 * new (#732).
 *
 * `capability` is what makes a connection eligible for the role: Azure DevOps
 * carries both capabilities, so an ADO connection legitimately appears in every
 * picker, while Jira is work-item-only and GitHub repository-only.
 */
export type ConnectionRole = "ticketSource" | "codeKnowledge" | "testCase";

export interface RoleSpec {
  id: ConnectionRole;
  capability: ProviderCategory;
  /** The config field this role binds, read and written by the same key. */
  field: "workItemConnectionId" | "repositoryConnectionId" | "testCaseConnectionId";
  /** Role pill colours, matching the v2 design handoff. */
  color: string;
  background: string;
  /** The ticket source is the project's defining binding, so its card is the
   *  only one the design gives an accent border. */
  accent: boolean;
}

export const ROLES: RoleSpec[] = [
  {
    id: "ticketSource",
    capability: "work_item",
    field: "workItemConnectionId",
    color: "#c4b5fd",
    background: "rgba(139,92,246,.2)",
    accent: true,
  },
  {
    id: "codeKnowledge",
    capability: "repository",
    field: "repositoryConnectionId",
    color: "#67e8f9",
    background: "rgba(34,211,238,.14)",
    accent: false,
  },
  {
    id: "testCase",
    capability: "work_item",
    field: "testCaseConnectionId",
    color: "#6ee7b7",
    background: "rgba(16,185,129,.14)",
    accent: false,
  },
];

/** The connection id bound to a role, resolving the TEST CASE TARGET's default.
 *
 * An unset target is not "unconfigured", it means *the same place the tickets
 * came from* — the behaviour every consumer had before the role existed, and the
 * one the API still applies server-side. Showing it as empty here would invite
 * someone to "fix" a project that is working correctly. */
export function boundConnectionId(
  config: ProjectConfigOut,
  role: RoleSpec,
): { id: number | null; inherited: boolean } {
  const explicit = config[role.field] ?? null;
  if (role.id === "testCase" && explicit == null) {
    return { id: config.workItemConnectionId ?? null, inherited: true };
  }
  return { id: explicit, inherited: false };
}

/** A patch binding one role, leaving the other two exactly as they are. */
export function bindRolePatch(role: RoleSpec, connectionId: number | null): ProjectConfigUpdate {
  return { [role.field]: connectionId } as ProjectConfigUpdate;
}
