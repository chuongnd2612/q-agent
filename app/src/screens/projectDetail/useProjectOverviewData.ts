import { useTranslation } from "react-i18next";
import {
  projectCountsKey,
  useProjectCounts,
  useProjectRepos,
  useProjects,
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
  const { data: repos } = useProjectRepos(key);
  // The single source for per-project figures (ADR 0015 §8, #733). This hook used
  // to fetch the WHOLE workspace's tickets and count the ones whose providerKind
  // matched — a second counting path, an unscoped read, and a count that was
  // wrong the moment two projects shared a provider. All three go away together.
  const { byProject, projects } = useProjectCounts();
  // Same query, deduped by react-query. Needed separately because
  // `useProjectCounts().isLoading` also covers the runs list, which has nothing
  // to do with whether the project's NAME is known yet.
  const { isLoading: projectsLoading } = useProjects();

  // GUID first — that is the identity. Falling back to the name keeps a
  // pre-#587 bookmark working; it is a *display* match, and the reason it can no
  // longer be the primary one is that two users may each have a "Surency" (#583).
  const project =
    projects?.find((p) => p.guid === key) ?? projects?.find((p) => p.name === key);
  const providerKind: ProviderKind = project?.providerKind ?? "ado";
  const counts = project ? byProject.get(projectCountsKey(project)) : undefined;
  const repoList = repos ?? [];
  const indexedRepos = repoList.filter((r) => r.status === "indexed");
  const meta: ProjectMeta = {
    name: project?.name ?? key,
    repo: repoList.length ? t("header.repoCount", { count: repoList.length }) : "",
    framework: "Playwright",
    provider: providerLabel[providerKind],
    providerKind,
    tickets: counts?.tickets ?? 0,
    runs: counts?.runs ?? 0,
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
    /**
     * True only while the project LIST is in flight, i.e. while `meta.name` is
     * still the route key. The header renders a placeholder rather than that
     * key, which for a GUID route (#587) is a 36-character UUID that reads as
     * real data. Deliberately NOT `project === undefined`: once the list has
     * resolved, falling back to the key is the right answer, not a load.
     */
    metaLoading: projectsLoading,
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
