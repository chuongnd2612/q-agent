import { ChevronRight, GitBranch, Pencil, RotateCw, Sparkles } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { GlassCard } from "@/components/ui/GlassCard";
import { Button } from "@/components/ui/Button";
import { confidenceColor } from "@/data/projects";
import { useRepoKnowledge } from "@/hooks/queries";
import type { KnowledgeBody, RepoKnowledgeOut } from "@/types/api";
import { KnowledgeEditPanel } from "./KnowledgeEditPanel";

/** Detail view for a single repo's knowledge base (loads it on demand). */
export function RepoKnowledgeDetail({
  projectKey,
  repoMeta,
  building,
  onBuild,
}: {
  projectKey: string;
  repoMeta: RepoKnowledgeOut;
  building: boolean;
  onBuild: () => void;
}) {
  const { t } = useTranslation("projects");
  const { data: knowledge } = useRepoKnowledge(
    projectKey,
    repoMeta.status === "indexed" ? repoMeta.name : null,
  );
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const toggle = (k: string) => setOpen((o) => ({ ...o, [k]: !o[k] }));
  // The KB was rebuild-only until #828; this opens the per-entry edit panel.
  const [editing, setEditing] = useState(false);

  if (repoMeta.status === "indexing") {
    return (
      <div className="glass flex flex-col items-center rounded-[22px] px-8 py-14 text-center">
        <span
          className="mb-5 h-[46px] w-[46px] rounded-full border-[3px]"
          style={{
            borderColor: "rgba(167,139,250,.3)",
            borderTopColor: "#a78bfa",
            animation: "spin .8s linear infinite",
          }}
        />
        <h2 className="m-0 mb-2 text-[19px] font-extrabold">
          {t("repoKnowledge.buildingTitle", { repo: repoMeta.name })}
        </h2>
        <p className="m-0 max-w-[420px] text-[13.5px] leading-relaxed text-ink-dim">
          {t("repoKnowledge.buildingBody")}
        </p>
      </div>
    );
  }

  if (repoMeta.status === "error") {
    return (
      <div className="glass flex flex-col items-center rounded-[22px] px-8 py-14 text-center">
        <div
          className="mb-5 flex h-[68px] w-[68px] items-center justify-center rounded-[22px]"
          style={{ background: "rgba(244,63,94,.14)" }}
        >
          <Sparkles size={30} color="#fb7185" strokeWidth={1.9} />
        </div>
        <h2 className="m-0 mb-2 text-[19px] font-extrabold">
          {t("repoKnowledge.errorTitle", { repo: repoMeta.name })}
        </h2>
        <p className="m-0 mb-[22px] max-w-[420px] text-[13.5px] leading-relaxed text-[#fb7185]">
          {repoMeta.lastError || t("repoKnowledge.buildFailed")}
        </p>
        <Button variant="primary" size="lg" onClick={onBuild} disabled={building}>
          <RotateCw size={16} strokeWidth={2.3} />{" "}
          {building ? t("repoKnowledge.building") : t("repoKnowledge.retryBuild")}
        </Button>
      </div>
    );
  }

  if (repoMeta.status !== "indexed") {
    return (
      <div className="glass flex flex-col items-center rounded-[22px] px-8 py-14 text-center">
        <div
          className="mb-5 flex h-[68px] w-[68px] items-center justify-center rounded-[22px]"
          style={{ background: "linear-gradient(135deg,rgba(139,92,246,.24),rgba(99,102,241,.12))" }}
        >
          <Sparkles size={30} color="#a78bfa" strokeWidth={1.9} />
        </div>
        <h2 className="m-0 mb-2 text-[19px] font-extrabold">
          {t("repoKnowledge.notIndexedTitle", { repo: repoMeta.name })}
        </h2>
        <p className="m-0 mb-[22px] max-w-[420px] text-[13.5px] leading-relaxed text-ink-dim">
          {t("repoKnowledge.notIndexedBody")}
        </p>
        <Button variant="primary" size="lg" onClick={onBuild} disabled={building}>
          <Sparkles size={16} strokeWidth={2.3} />{" "}
          {building ? t("repoKnowledge.building") : t("repoKnowledge.buildKnowledge")}
        </Button>
      </div>
    );
  }

  const kn = (knowledge?.knowledge ?? {}) as Partial<KnowledgeBody>;
  const confidence = knowledge?.confidence ?? repoMeta.confidence;
  const sections = [
    { key: "arch", label: t("repoKnowledge.sectionArch"), body: kn.architecture || "—" },
    { key: "domain", label: t("repoKnowledge.sectionDomain"), body: kn.domain || "—" },
    { key: "locator", label: t("repoKnowledge.sectionLocator"), body: kn.locator || "—" },
  ];

  return (
    <div>
      <div className="glass mb-3.5 flex flex-wrap items-center gap-3 rounded-2xl p-[16px_18px]">
        <div className="flex min-w-[220px] flex-1 items-center gap-[11px]">
          <div
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl"
            style={{ background: "linear-gradient(135deg,#8b5cf6,#6366f1)" }}
          >
            <GitBranch size={20} color="#fff" strokeWidth={2.2} />
          </div>
          <div>
            <div className="flex items-center gap-2 text-[14px] font-bold">
              <span className="font-mono">{repoMeta.name}</span>
              <span className="text-[11px] font-semibold text-[#8b8b9e]">
                {knowledge?.version ?? repoMeta.version}
              </span>
            </div>
            <div className="text-[12px] text-[#8b8b9e]">
              {t("repoKnowledge.lastIndexed", {
                date: repoMeta.lastIndexed ? new Date(repoMeta.lastIndexed).toLocaleString() : "—",
                confidence,
              })}
            </div>
            {repoMeta.docPath && (
              <div className="mt-0.5 truncate font-mono text-[11px] text-[#6c6c7e]" title={repoMeta.docPath}>
                knowledge.md · knowledge.json → {repoMeta.docPath}
              </div>
            )}
          </div>
        </div>
        <Button variant="glass" onClick={() => setEditing((e) => !e)}>
          <Pencil size={14} strokeWidth={2.2} /> {t("repoKnowledge.edit.open")}
        </Button>
        <Button variant="glass" onClick={onBuild} disabled={building}>
          <RotateCw size={14} strokeWidth={2.2} />{" "}
          {building ? t("repoKnowledge.reindexing") : t("repoKnowledge.reindex")}
        </Button>
      </div>

      {editing && (
        <KnowledgeEditPanel
          projectKey={projectKey}
          repo={repoMeta.name}
          knowledge={kn}
          onClose={() => setEditing(false)}
        />
      )}

      {repoMeta.needsRefresh && (
        <div
          className="mb-3.5 flex items-center gap-[11px] rounded-[14px] p-[13px_16px]"
          style={{ background: "rgba(251,191,36,.1)", border: "1px solid rgba(251,191,36,.28)" }}
        >
          <span className="text-[13px] flex-1 text-[#fbdf9e]">
            {t("repoKnowledge.needsRefresh")}
          </span>
          <button
            onClick={onBuild}
            className="cursor-pointer rounded-[10px] border-none bg-[#fbbf24] px-3.5 py-2 text-[12.5px] font-bold text-[#3a2e05]"
          >
            {t("repoKnowledge.rebuildNow")}
          </button>
        </div>
      )}

      <div className="mb-3.5 grid grid-cols-1 gap-3.5 md:grid-cols-3">
        <GlassCard className="p-[18px]">
          <div className="mb-3 text-[12.5px] font-semibold text-[#9494a6]">
            {t("repoKnowledge.baseUrl")}
          </div>
          <div className="break-all font-mono text-[13px] font-bold">{kn.base_url || "—"}</div>
          <div className="mt-1.5 text-[12px] text-[#8b8b9e]">
            {t("repoKnowledge.branch", { branch: kn.branch || repoMeta.defaultBranch || "main" })}
          </div>
        </GlassCard>
        <GlassCard className="p-[18px]">
          <div className="mb-3 text-[12.5px] font-semibold text-[#9494a6]">
            {t("repoKnowledge.routesSelectors")}
          </div>
          <div className="text-[16px] font-extrabold">
            {(kn.routes?.length ?? 0)} · {(kn.selectors?.length ?? 0)}
          </div>
          <div className="mt-1.5 text-[12px] text-[#8b8b9e]">
            {t("repoKnowledge.discoveredFromSource")}
          </div>
        </GlassCard>
        <GlassCard className="p-[18px]">
          <div className="mb-3 text-[12.5px] font-semibold text-[#9494a6]">
            {t("repoKnowledge.knowledgeConfidence")}
          </div>
          <div className="mb-2 text-[22px] font-black" style={{ color: confidenceColor(confidence) }}>
            {confidence}%
          </div>
          <div className="h-1.5 overflow-hidden rounded-md bg-white/[0.08]">
            <div
              className="h-full rounded-md"
              style={{ width: `${confidence}%`, background: "linear-gradient(90deg,#8b5cf6,#22d3ee)" }}
            />
          </div>
        </GlassCard>
      </div>

      <div className="mb-3.5 grid grid-cols-1 gap-3.5 md:grid-cols-[1.3fr_1fr]">
        <GlassCard className="p-5">
          <div className="mb-3 text-[13px] font-bold">{t("repoKnowledge.technologyStack")}</div>
          <div className="mb-[18px] flex flex-wrap gap-2">
            {(kn.stack ?? []).map((tech) => (
              <span
                key={tech}
                className="rounded-[10px] px-3 py-1.5 text-[12px] font-semibold text-violet"
                style={{ background: "rgba(139,92,246,.14)" }}
              >
                {tech}
              </span>
            ))}
          </div>
          <div className="mb-3 text-[13px] font-bold">{t("repoKnowledge.reusableUtilities")}</div>
          <div className="flex flex-wrap gap-2">
            {(kn.utilities ?? []).map((u) => (
              <span
                key={u}
                className="rounded-[9px] border border-white/[0.08] bg-white/[0.05] px-[11px] py-[5px] font-mono text-[11.5px] text-ink-soft"
              >
                {u}
              </span>
            ))}
          </div>
        </GlassCard>
        <GlassCard className="p-5">
          <div className="mb-3.5 text-[13px] font-bold">{t("repoKnowledge.existingAssets")}</div>
          <div className="flex flex-col gap-3">
            {[
              [t("repoKnowledge.specFiles"), kn.assets ?? 0, "#67e8f9"],
              [t("repoKnowledge.pageObjects"), kn.pageObjects ?? 0, "#a78bfa"],
              [t("repoKnowledge.sharedFixtures"), kn.fixtures ?? 0, "#6ee7b7"],
            ].map(([label, val, color]) => (
              <div key={label as string} className="flex items-center gap-3">
                <div className="flex-1 text-[13px] font-semibold">{label}</div>
                <div className="text-[20px] font-black" style={{ color: color as string }}>
                  {val as number}
                </div>
              </div>
            ))}
          </div>
        </GlassCard>
      </div>

      <div className="flex flex-col gap-2.5">
        {sections.map((sec) => (
          <div
            key={sec.key}
            className="glass overflow-hidden rounded-2xl"
            style={{ backdropFilter: "blur(20px)" }}
          >
            <div
              onClick={() => toggle(sec.key)}
              className="flex cursor-pointer items-center gap-3 p-[15px_18px]"
            >
              <span className="flex-1 text-[14px] font-semibold">{sec.label}</span>
              <ChevronRight
                size={16}
                color="#8b8b9e"
                strokeWidth={2.4}
                style={{
                  transition: "transform .25s",
                  transform: open[sec.key] ? "rotate(90deg)" : "none",
                }}
              />
            </div>
            {open[sec.key] && (
              <div className="p-[0_18px_16px_46px] text-[13px] leading-relaxed text-ink-soft animate-[fadeInUp_.3s_ease_both]">
                {sec.body}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
