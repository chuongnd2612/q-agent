import { BookOpen, GitBranch, Plus, Star } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { GlassCard } from "@/components/ui/GlassCard";
import { Button } from "@/components/ui/Button";
import { providerLabel } from "@/data/projects";
import { toast } from "@/lib/toast";
import { useBuildRepoKnowledge } from "@/hooks/queries";
import type { ProviderKind, RepoKnowledgeOut } from "@/types/api";
import { RepoKnowledgeDetail } from "./RepoKnowledgeDetail";

const repoStatusStyle = (status: string): [string, string] =>
  status === "indexed"
    ? ["#6ee7b7", "rgba(16,185,129,.16)"]
    : status === "indexing"
      ? ["#8b5cf6", "rgba(139,92,246,.16)"]
      : status === "stale"
        ? ["#fbbf24", "rgba(251,191,36,.14)"]
        : status === "error"
          ? ["#f43f5e", "rgba(244,63,94,.14)"]
          : ["#8b8b9e", "rgba(255,255,255,.06)"];

/**
 * Project Knowledge tab — per-repo. Lists the project's repositories, each with
 * its own knowledge-base status + Build/Re-index action, and shows the selected
 * repo's knowledge base detail.
 */
export function KnowledgeTab({
  projectKey,
  providerKind,
  repos,
  onManageRepos,
  onOpenBusiness,
}: {
  projectKey: string;
  providerKind: ProviderKind;
  repos: RepoKnowledgeOut[];
  onManageRepos: () => void;
  onOpenBusiness: () => void;
}) {
  const { t } = useTranslation("projects");
  const buildRepo = useBuildRepoKnowledge(projectKey);
  const defaultRepoName = repos.find((r) => r.default)?.name ?? repos[0]?.name ?? null;
  const [selected, setSelected] = useState<string | null>(defaultRepoName);
  const active = selected ?? defaultRepoName;

  useEffect(() => {
    if (active && !repos.some((r) => r.name === active)) setSelected(defaultRepoName);
  }, [repos, active, defaultRepoName]);

  const onBuild = (repo: string) =>
    buildRepo.mutate(
      { repo, body: { provider: providerLabel[providerKind] } },
      {
        onSuccess: () => toast.success(t("knowledgeTab.buildSuccess", { repo })),
        onError: (e) => toast.error(e instanceof Error ? e.message : t("knowledgeTab.buildError")),
      },
    );

  // No repository is a SUPPORTED state, not a dead end (#826, ADR 0016 §6):
  // test-case authoring runs on Business Knowledge alone, so the empty state
  // says what is already working and points at it, instead of offering only a
  // build button that has nothing to index. Automation/execution/self-heal do
  // still need a repo, which is why "Manage repositories" stays — demoted, not
  // removed.
  if (repos.length === 0) {
    return (
      <div
        className="glass flex flex-col items-center rounded-[22px] px-8 py-14 text-center"
        data-testid="knowledge-empty"
      >
        <div
          className="mb-5 flex h-[72px] w-[72px] items-center justify-center rounded-[22px]"
          style={{ background: "linear-gradient(135deg,rgba(139,92,246,.24),rgba(99,102,241,.12))" }}
        >
          <BookOpen size={32} color="#a78bfa" strokeWidth={1.9} />
        </div>
        <h2 className="m-0 mb-2 text-[21px] font-extrabold">{t("knowledgeTab.emptyTitle")}</h2>
        <p className="m-0 mb-[22px] max-w-[460px] text-[13.5px] leading-relaxed text-ink-dim">
          {t("knowledgeTab.emptyBody")}
        </p>
        <div className="flex flex-wrap items-center justify-center gap-2.5">
          <Button variant="primary" size="lg" onClick={onOpenBusiness}>
            <BookOpen size={16} strokeWidth={2.3} /> {t("knowledgeTab.openBusiness")}
          </Button>
          <Button variant="ghost" size="lg" onClick={onManageRepos}>
            <Plus size={16} strokeWidth={2.3} /> {t("knowledgeTab.manageRepositories")}
          </Button>
        </div>
      </div>
    );
  }

  // A repo is "building" when the server reports it as indexing (survives
  // navigation) or when there's an in-flight build mutation for it.
  const pendingRepo = buildRepo.isPending ? (buildRepo.variables?.repo ?? null) : null;
  const isBuilding = (repo: RepoKnowledgeOut) =>
    repo.status === "indexing" || pendingRepo === repo.name;

  return (
    <div className="grid grid-cols-1 items-start gap-3.5 md:grid-cols-[260px_1fr]">
      <GlassCard className="p-2">
        <div className="flex items-center justify-between px-2.5 pb-1.5 pt-2">
          <span className="text-[10.5px] font-semibold tracking-wider text-faint">
            {t("knowledgeTab.repositories")}
          </span>
          <button
            onClick={onManageRepos}
            className="text-[11px] font-semibold text-violet hover:text-[#c4b5fd]"
          >
            {t("knowledgeTab.manage")}
          </button>
        </div>
        <div className="flex flex-col gap-0.5">
          {repos.map((r) => {
            const [dot, bg] = repoStatusStyle(r.status);
            const on = active === r.name;
            return (
              <button
                key={r.name}
                onClick={() => setSelected(r.name)}
                className="flex items-center gap-2 rounded-[10px] px-2.5 py-2 text-left hover:bg-white/5"
                style={on ? { background: "rgba(139,92,246,.14)" } : undefined}
              >
                <GitBranch size={14} color={on ? "#a78bfa" : "#8b8b9e"} />
                <span className="flex-1 truncate font-mono text-xs text-ink-soft">{r.name}</span>
                {r.default && <Star size={11} color="#fbbf24" fill="#fbbf24" />}
                {r.status === "indexing" ? (
                  <span
                    className="h-3 w-3 shrink-0 rounded-full border-2"
                    style={{
                      borderColor: "rgba(167,139,250,.35)",
                      borderTopColor: "#a78bfa",
                      animation: "spin .8s linear infinite",
                    }}
                    title="indexing"
                  />
                ) : (
                  <span
                    className="h-2 w-2 shrink-0 rounded-full"
                    style={{ background: dot }}
                    title={r.status}
                  />
                )}
                <span className="sr-only">{bg}</span>
              </button>
            );
          })}
        </div>
      </GlassCard>

      {active ? (
        <RepoKnowledgeDetail
          projectKey={projectKey}
          repoMeta={repos.find((r) => r.name === active)!}
          building={isBuilding(repos.find((r) => r.name === active)!)}
          onBuild={() => onBuild(active)}
        />
      ) : null}
    </div>
  );
}
