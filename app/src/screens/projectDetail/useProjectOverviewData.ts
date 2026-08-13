import { useTranslation } from "react-i18next";
import {
  ALL_TICKETS_PAGE_SIZE,
  useProjectRepos,
  useProjects,
  useRuns,
  useTickets,
} from "@/hooks/queries";
import { knowledgeStatusStyle, providerLabel } from "@/data/projects";
import { providerGlyph } from "@/components/ui/badges";
import type { ProviderKind } from "@/types/api";
import type { ProjectMeta } from "./types";

/**
 * Loads a project's summary data (project record, repos, tickets, runs) and
 * derives the values the ProjectDetail header + overview render: the aggregate
 * knowledge status/confidence, the status pill styling, the provider glyph, and
 * the {@link ProjectMeta} record. Pure relocation of the derivations previously
 * inline in `ProjectDetail`.
 *
 * @param key The project identifier from the route — a GUID (#587), or a name
 *   for an older deep link. Passed to the API unchanged: the backend resolves
 *   either through `resolve_project_identifier`.
 */
export function useProjectOverviewData(key: string) {
  const { t } = useTranslation("projects");
  const { data: projects } = useProjects();
  const { data: repos } = useProjectRepos(key);
  const { data: ticketsPage } = useTickets({ pageSize: ALL_TICKETS_PAGE_SIZE });
  const tickets = ticketsPage?.items;
  const { data: runs } = useRuns();

  // GUID first — that is the identity. Falling back to the name keeps a
  // pre-#587 bookmark working; it is a *display* match, and the reason it can no
  // longer be the primary one is that two users may each have a "Surency" (#583).
  const project =
    projects?.find((p) => p.guid === key) ?? projects?.find((p) => p.name === key);
  const providerKind: ProviderKind = project?.providerKind ?? "ado";
  const repoList = repos ?? [];
  const indexedRepos = repoList.filter((r) => r.status === "indexed");
  const meta: ProjectMeta = {
    name: project?.name ?? key,
    repo: repoList.length ? t("header.repoCount", { count: repoList.length }) : "",
    framework: "Playwright",
    provider: providerLabel[providerKind],
    providerKind,
    tickets: (tickets ?? []).filter((t) => t.providerKind === providerKind).length,
    runs: (runs ?? []).filter((r) => r.status !== "done").length,
    rate: "—",
  };

  // Aggregate knowledge status across the project's repos.
  const status = indexedRepos.length ? "indexed" : "not_indexed";
  const confidence = indexedRepos.length
    ? Math.round(indexedRepos.reduce((s, r) => s + r.confidence, 0) / indexedRepos.length)
    : 0;
  const [, statusColor, statusBg, statusDot] = knowledgeStatusStyle(status);
  const statusLabel = repoList.length
    ? t("header.reposIndexed", { indexed: indexedRepos.length, total: repoList.length })
    : t("header.noRepos");
  const [glyph, glyphBg] = providerGlyph[meta.providerKind] ?? ["?", "#6b7280"];
  const glyphColor = meta.providerKind === "github" ? "#12121a" : "#fff";

  return {
    /** The matched project row, or `undefined` while the list is still loading. */
    project,
    meta,
    providerKind,
    repoList,
    confidence,
    statusColor,
    statusBg,
    statusDot,
    statusLabel,
    glyph,
    glyphBg,
    glyphColor,
  };
}
