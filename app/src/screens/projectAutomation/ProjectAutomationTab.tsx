import { useMemo } from "react";
import { GitCommitHorizontal, Info, Layers } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";
import { ErrorState, Spinner } from "@/components/ui/misc";
import { timeAgo } from "@/components/dashboard/runStatus";
import { api } from "@/lib/api";
import { useAutomationFile, useAutomationRepos, useAutomationTree } from "@/hooks/queries";
import { useProjectRoute } from "@/screens/ProjectDetail";
import { ExportProjectPanel } from "@/screens/automation/ExportProjectPanel";
import { ProjectFilePanel } from "@/screens/automation/ProjectFilePanel";
import { ProjectFileTree } from "@/screens/automation/ProjectFileTree";
import { groupProjectFiles } from "@/screens/automation/projectFiles";
import type { AutomationRepoOut, AutomationTreeOut } from "@/types/api";
import {
  FileLoadingSkeleton,
  NoFileSelected,
  NoRepoEmpty,
  ScaffoldOnlyEmpty,
} from "./ProjectAutomationEmpty";
import { RepoSelector } from "./RepoSelector";

/**
 * The project's Automation tab (#765/#770) — browse the specs and the automation
 * repo a project has accumulated, outside any run.
 *
 * ## Where the state lives
 *
 * `?repo=<automation projectId>&file=<posix path>`, and nowhere else. Per
 * CLAUDE.md the URL is the source of truth; nothing here touches Zustand. The
 * repo is addressed by its **numeric id** rather than its slug: it is stable,
 * unambiguous, safe when `repo` is `""` (the single-repo case), and it is what the
 * API takes — a slug would read prettier but would tie a bookmark to a name.
 * Both params are written with `replace: true`, so clicking through eight files
 * leaves one back-stack entry instead of eight.
 *
 * The selection is **resolved, not required**: `?repo=` wins when it names a repo
 * this project actually has, else the repo with the most specs, else the first.
 * The param is written only when the user picks — landing on the tab does not
 * rewrite the URL to say what the default already is.
 *
 * ## Why the content is a separate request
 *
 * `useAutomationTree` returns metadata only (~25KB for a 200-file repo);
 * `useAutomationFile` fetches one file's `code` and is `enabled` only once a path
 * is selected. That split is the entire point of #768 — eager-loading the tree's
 * contents would undo it — which is also why nothing is auto-opened.
 *
 * `specPath=""` is passed to `ProjectFileTree` deliberately: no row is the
 * editable spec here, so every row shows the read-only lock. This tab is a
 * browser; writes still belong to the run overlay's quality gate.
 *
 * ## The repo is one repo, not a per-project view of one
 *
 * `AutomationProject` is keyed on `(owner_id, provider project_key, repo)`, so one
 * automation repo is legitimately written to by runs from several q-agent
 * projects. The tree is therefore **not** filtered by project: filtering would
 * misrepresent the repo's real contents and break the ZIP's correspondence to the
 * tree the user just browsed. The header says so in one line instead, and slice
 * #772's provenance panel is what names the run and project behind any individual
 * spec.
 */
export function ProjectAutomationTab() {
  const { t } = useTranslation("projects");
  const { projectKey, projectGuid, goTab } = useProjectRoute();
  // `projectGuid` is the canonical id but is null until the project query lands;
  // `projectKey` is whatever the URL carried and the API resolves either.
  const guid = projectGuid ?? projectKey ?? null;
  const [searchParams, setSearchParams] = useSearchParams();

  const repoParam = searchParams.get("repo");
  const fileParam = searchParams.get("file");

  const repos = useAutomationRepos(guid || null);

  // Resolution order per the spec: the URL, else the repo with the most specs
  // (the one the user almost certainly means), else the first.
  const selectedRepo = useMemo<AutomationRepoOut | null>(() => {
    const list = repos.data;
    if (!list || list.length === 0) return null;
    const fromUrl = repoParam ? list.find((r) => String(r.id) === repoParam) : undefined;
    if (fromUrl) return fromUrl;
    return [...list].sort((a, b) => b.specCount - a.specCount || a.id - b.id)[0];
  }, [repos.data, repoParam]);

  const tree = useAutomationTree(guid || null, selectedRepo?.id ?? null);

  // Only ask for a file the tree actually has. A stale `?file=` from another repo
  // would otherwise spend a request on a guaranteed 404.
  const selectedPath = useMemo(() => {
    if (!fileParam || !tree.data) return null;
    return tree.data.files.some((f) => f.path === fileParam) ? fileParam : null;
  }, [fileParam, tree.data]);

  const file = useAutomationFile(guid || null, selectedRepo?.id ?? null, selectedPath);

  const patchParams = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(searchParams);
    for (const [k, v] of Object.entries(patch)) {
      if (v == null) next.delete(k);
      else next.set(k, v);
    }
    setSearchParams(next, { replace: true });
  };

  // Switching repos drops the open file: a path is only meaningful inside the
  // repo it came from, and keeping it would ask for a file that isn't there.
  const selectRepo = (id: number) => patchParams({ repo: String(id), file: null });
  const selectFile = (path: string) => patchParams({ file: path });

  const groups = useMemo(
    () => (tree.data ? groupProjectFiles(tree.data.files) : []),
    [tree.data],
  );
  const hasSpecs = groups.some((g) => g.kind === "spec");

  if (repos.isLoading) {
    return (
      <div className="flex items-center justify-center py-20">
        <Spinner size={22} />
      </div>
    );
  }

  if (repos.isError) {
    return (
      <ErrorState
        title={t("automation.loadFailedTitle")}
        body={t("automation.loadFailedBody")}
        retryLabel={t("automation.retry")}
        onRetry={() => void repos.refetch()}
      />
    );
  }

  // State 1 of 3: the project has no automation repo at all.
  if (!selectedRepo) return <NoRepoEmpty onViewRuns={() => goTab("runs")} />;

  const scaffoldOnly = !!tree.data && tree.data.files.length === 0;

  return (
    <div className="flex flex-col gap-3.5">
      <RepoHeader
        repos={repos.data ?? []}
        repo={selectedRepo}
        tree={tree.data ?? null}
        onSelectRepo={selectRepo}
      />

      {/* Same panel as the run overlay, pointed at the project-keyed ZIP (#770).
          Shown for a scaffold-only repo too — the skeleton is a real download. */}
      <ExportProjectPanel
        download={() => api.exportProjectAutomationZip(guid, selectedRepo.id)}
      />

      {tree.isLoading && (
        <div className="flex items-center justify-center py-16">
          <Spinner size={20} />
        </div>
      )}

      {tree.isError && (
        <ErrorState
          title={t("automation.treeFailedTitle")}
          body={t("automation.treeFailedBody")}
          retryLabel={t("automation.retry")}
          onRetry={() => void tree.refetch()}
        />
      )}

      {/* State 2 of 3: scaffolded, nothing generated into it yet. */}
      {scaffoldOnly && <ScaffoldOnlyEmpty onViewRuns={() => goTab("runs")} />}

      {tree.data && !scaffoldOnly && (
        <div className="flex flex-col gap-3.5 md:grid md:grid-cols-[250px_1fr] md:items-start">
          <div className="flex flex-col gap-2">
            {/* State 3 is NOT an empty state: pages/components/fixtures are real
                accumulated value, so the tree renders in full and only the
                missing Specs group is called out. */}
            {!hasSpecs && (
              <div
                className="rounded-xl border border-white/[0.09] px-3 py-2 text-[11.5px] leading-relaxed text-muted"
                style={{ background: "rgba(8,8,13,.92)" }}
                data-testid="automation-no-specs-note"
              >
                {t("automation.noSpecsNote")}
              </div>
            )}
            <ProjectFileTree
              groups={groups}
              // No row is editable here, so every row gets the read-only lock.
              specPath=""
              selectedPath={selectedPath ?? ""}
              onSelect={selectFile}
            />
          </div>

          <div className="flex min-w-0 flex-col gap-3.5">
            {/* ---- SLICE #772 RENDER SLOT: ProvenancePanel ------------------
                Slice G fills exactly this spot, above ProjectFilePanel, with:

                  <ProvenancePanel
                    provenance={file.data.provenance}   // SpecProvenance | null
                    path={file.data.path}
                  />

                from `@/screens/projectAutomation/ProvenancePanel`. Nothing else
                in this file needs to change: `file.data.provenance` is already
                on the response (`AutomationFileOut`) and is `null` for a
                non-spec file by design. Left empty on purpose — a panel that
                renders "no provenance" for every page object would be noise. */}

            {file.isError ? (
              <ErrorState
                title={t("automation.fileFailedTitle")}
                body={t("automation.fileFailedBody")}
                retryLabel={t("automation.retry")}
                onRetry={() => void file.refetch()}
              />
            ) : selectedPath == null ? (
              <NoFileSelected />
            ) : file.data ? (
              <ProjectFilePanel file={file.data} />
            ) : (
              <FileLoadingSkeleton />
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * The selected repo's identity and shape, plus the selector.
 *
 * Opaque surface rather than `GlassCard`: this is text-heavy chrome layered over
 * the shell's animated constellation background, where a translucent card makes
 * small text genuinely hard to read (the same finding, and the same fix, as
 * `ProjectFilePanel` and `ExportProjectPanel`).
 *
 * Counts come from the **tree** once it has loaded and from the repo list before
 * that, so the numbers never disagree with the tree on screen.
 */
function RepoHeader({
  repos,
  repo,
  tree,
  onSelectRepo,
}: {
  repos: AutomationRepoOut[];
  repo: AutomationRepoOut;
  tree: AutomationTreeOut | null;
  onSelectRepo: (id: number) => void;
}) {
  const { t } = useTranslation("projects");
  const label = repo.repoLabel || repo.repo || t("automation.defaultRepo");
  const fileCount = tree?.fileCount ?? repo.fileCount;
  const updatedAt = tree?.updatedAt ?? repo.updatedAt;
  const head = tree?.headCommit ?? "";

  return (
    <div
      className="rounded-2xl border border-white/[0.09] px-4 py-3.5"
      style={{ background: "rgba(8,8,13,.92)" }}
      data-testid="automation-repo-header"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <Layers size={16} className="shrink-0 text-violet" strokeWidth={2.2} />
        <span className="font-mono text-[13.5px] font-bold text-ink">{label}</span>
        <span className="text-[11.5px] text-faint">
          {t("automation.repoFileCount", { count: fileCount })} ·{" "}
          {t("automation.repoSpecCount", { count: repo.specCount })}
        </span>
        {repo.baseVersion && (
          <span
            className="rounded-md px-2 py-0.5 font-mono text-[10.5px] font-bold"
            style={{ background: "rgba(139,92,246,.14)", color: "#c4b5fd" }}
          >
            {repo.baseVersion}
          </span>
        )}
        {head && (
          <span
            className="flex items-center gap-1 font-mono text-[10.5px] text-faint"
            title={head}
          >
            <GitCommitHorizontal size={12} /> {head.slice(0, 7)}
          </span>
        )}
        {updatedAt && (
          <span className="text-[11.5px] text-faint">
            {t("automation.repoUpdated", { when: timeAgo(updatedAt) })}
          </span>
        )}
        <div className="ml-auto">
          <RepoSelector repos={repos} selectedId={repo.id} onSelect={onSelectRepo} />
        </div>
      </div>

      {/* The honest framing, in one quiet line: this is the caller's own clone
          (automation projects are keyed per user), of a repo that is shared
          across their projects (keyed on the *provider* project key, so runs
          from several q-agent projects write into it). Both halves are properties
          of the schema, not guesses — so neither is conditional, and neither
          claims a count the API does not report. */}
      <p
        className="m-0 mt-2 flex items-start gap-1.5 text-[11px] leading-relaxed text-muted"
        data-testid="automation-repo-note"
      >
        <Info size={12} className="mt-[2px] shrink-0 text-faint" />
        {t("automation.repoNote")}
      </p>
    </div>
  );
}
