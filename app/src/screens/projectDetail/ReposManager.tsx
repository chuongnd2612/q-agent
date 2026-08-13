import { Plus, Search, Star, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { GlassCard } from "@/components/ui/GlassCard";
import { Button } from "@/components/ui/Button";
import { useConnectionRepos } from "@/hooks/queries";
import type { ProjectRepo } from "@/types/api";
import { inputCls } from "./formStyles";

/**
 * Repositories manager — a project can own many repos, each with its own
 * knowledge base. Discover them from the project's bound repository connection
 * or add manually, pick which repo automation targets by default, and set an
 * optional local path. Remember to Save settings to persist changes.
 */
export function ReposManager({
  repoConnectionId,
  repoConnectionName,
  repos,
  setRepos,
  readOnly = false,
}: {
  repoConnectionId: number | null;
  repoConnectionName: string;
  repos: ProjectRepo[];
  setRepos: (updater: (r: ProjectRepo[]) => ProjectRepo[]) => void;
  /** EmeHub owns this project's configuration (#587): show the repos, offer no
   *  way to change them. The API refuses the write regardless. */
  readOnly?: boolean;
}) {
  const { t } = useTranslation("projects");
  const [discovering, setDiscovering] = useState(false);
  const { data: available, isFetching } = useConnectionRepos(repoConnectionId, discovering);

  const setDefault = (idx: number) =>
    setRepos((rs) => rs.map((r, i) => ({ ...r, default: i === idx })));
  const removeRepo = (idx: number) =>
    setRepos((rs) => {
      const next = rs.filter((_, i) => i !== idx);
      if (next.length && !next.some((r) => r.default)) next[0].default = true;
      return next;
    });
  const addRepo = (repo: ProjectRepo) =>
    setRepos((rs) =>
      repo.name && rs.some((r) => r.name === repo.name)
        ? rs
        : [...rs, { ...repo, default: rs.length === 0 }],
    );
  const updateRepo = (idx: number, patch: Partial<ProjectRepo>) =>
    setRepos((rs) => rs.map((r, i) => (i === idx ? { ...r, ...patch } : r)));

  const known = new Set(repos.map((r) => r.name));
  const discovered = (available?.repos ?? []).filter((r) => !known.has(r.name));

  return (
    <GlassCard className="p-5">
      <div className="mb-1 flex flex-wrap items-center gap-2">
        <div className="flex-1 text-[14px] font-bold">{t("repos.repositories")}</div>
        {readOnly ? null : (
        <>
        <Button
          variant="glass"
          onClick={() => setDiscovering(true)}
          disabled={isFetching || !repoConnectionId}
          title={repoConnectionId ? undefined : t("repos.bindProviderFirst")}
        >
          <Search size={14} strokeWidth={2.4} />{" "}
          {isFetching
            ? t("repos.discovering")
            : t("repos.discoverFrom", { name: repoConnectionName || t("repos.repositoryProvider") })}
        </Button>
        <Button
          variant="glass"
          onClick={() => addRepo({ name: "", repoUrl: "", defaultBranch: "", localRepoPath: "", default: false })}
        >
          <Plus size={14} strokeWidth={2.4} /> {t("repos.addManually")}
        </Button>
        </>
        )}
      </div>
      <p className="mb-4 text-[12.5px] leading-relaxed text-ink-dim">
        {t("repos.blurb1")}{" "}
        <Star size={11} className="inline align-[-1px]" color="#fbbf24" fill="#fbbf24" />{" "}
        {t("repos.blurb2")}{" "}
        <span className="font-mono">workspace/repos/&lt;project&gt;/&lt;repo&gt;</span>
        {t("repos.blurb3")}
      </p>

      {available?.error && discovering && (
        <div className="mb-3 rounded-[10px] border border-[rgba(244,63,94,.28)] bg-[rgba(244,63,94,.1)] px-3 py-2 text-[12px] text-[#fb7185]">
          {available.error}
        </div>
      )}

      {!readOnly && discovered.length > 0 && (
        <div className="mb-3.5 rounded-[12px] border border-white/[0.08] bg-white/[0.03] p-2.5">
          <div className="mb-1.5 px-1 text-[11px] font-semibold tracking-wider text-faint">
            {t("repos.discoveredHint")}
          </div>
          <div className="flex flex-wrap gap-2">
            {discovered.map((r) => (
              <button
                key={r.name}
                onClick={() =>
                  addRepo({
                    name: r.name,
                    repoUrl: r.cloneUrl,
                    defaultBranch: r.defaultBranch,
                    localRepoPath: "",
                    default: false,
                  })
                }
                className="flex items-center gap-1.5 rounded-[9px] border border-white/[0.1] bg-white/[0.04] px-2.5 py-1.5 font-mono text-[11.5px] text-ink-soft hover:bg-white/[0.08]"
              >
                <Plus size={12} strokeWidth={2.4} /> {r.name}
              </button>
            ))}
          </div>
        </div>
      )}

      {repos.length === 0 ? (
        <div className="text-[12.5px] text-[#6c6c7e]">{t("repos.noRepositories")}</div>
      ) : (
        <div className="flex flex-col gap-3">
          {repos.map((r, i) => (
            <div
              key={i}
              className="rounded-[12px] border border-white/[0.08] bg-white/[0.02] p-3"
            >
              <div className="mb-2 flex items-center gap-2.5">
                <button
                  onClick={() => (readOnly ? undefined : setDefault(i))}
                  disabled={readOnly}
                  title={r.default ? t("repos.defaultTarget") : t("repos.setDefault")}
                  className="flex h-6 w-6 items-center justify-center rounded-[7px]"
                  style={{ background: r.default ? "rgba(251,191,36,.16)" : "rgba(255,255,255,.05)" }}
                >
                  <Star size={14} color="#fbbf24" fill={r.default ? "#fbbf24" : "none"} />
                </button>
                <input
                  className={`${inputCls} max-w-[220px] font-mono`}
                  placeholder={t("repos.namePlaceholder")}
                  value={r.name}
                  disabled={readOnly}
                  onChange={(e) => updateRepo(i, { name: e.target.value })}
                />
                {r.default && (
                  <span className="rounded-md bg-[rgba(251,191,36,.14)] px-2 py-0.5 text-[10.5px] font-bold text-[#fbbf24]">
                    {t("repos.defaultBadge")}
                  </span>
                )}
                <div className="flex-1" />
                {readOnly ? null : (
                <button
                  onClick={() => removeRepo(i)}
                  className="flex h-[34px] w-[34px] items-center justify-center rounded-[10px] border border-white/[0.08] bg-white/[0.03] text-[#e06c75] hover:bg-white/[0.06]"
                  title={t("repos.removeRepository")}
                >
                  <Trash2 size={14} strokeWidth={2.1} />
                </button>
                )}
              </div>
              <div className="grid grid-cols-1 gap-2.5 md:grid-cols-[2fr_1fr]">
                <input
                  className={inputCls}
                  placeholder={t("repos.cloneUrlPlaceholder")}
                  value={r.repoUrl}
                  disabled={readOnly}
                  onChange={(e) => updateRepo(i, { repoUrl: e.target.value })}
                />
                <input
                  className={inputCls}
                  placeholder={t("repos.localPathPlaceholder")}
                  value={r.localRepoPath}
                  disabled={readOnly}
                  onChange={(e) => updateRepo(i, { localRepoPath: e.target.value })}
                />
              </div>
            </div>
          ))}
        </div>
      )}
    </GlassCard>
  );
}
