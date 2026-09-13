/**
 * "Clone from shared" catalog (issue #121 / ADR 0009 §2,§4). Lists the
 * admin-curated shared-namespace projects (`GET /shared/projects`) so any
 * member can browse ready-built knowledge and clone it into their own scope
 * instead of rebuilding it from scratch. Rendered on the Projects screen,
 * above the member's own project grid.
 */

import { Boxes, Check, Copy } from "lucide-react";
import { useTranslation } from "react-i18next";
import { GlassCard } from "@/components/ui/GlassCard";
import { Button } from "@/components/ui/Button";
import { Pill, providerGlyph } from "@/components/ui/badges";
import { Spinner } from "@/components/ui/misc";
import { knowledgeStatusStyle } from "@/data/projects";
import { useCloneSharedProject, useSharedProjects } from "@/hooks/queries";
import type { ProviderKind, SharedProjectOut } from "@/types/api";
import { Skeleton, useSkeleton } from "@/components/ui/Skeleton";

export function SharedProjectsCatalog() {
  const { t } = useTranslation("projects");
  const { data: shared, isLoading } = useSharedProjects();
  const clone = useCloneSharedProject();
  const showSkeleton = useSkeleton(isLoading);

  if (showSkeleton) {
    return <Skeleton className="mb-4 h-[92px]" />;
  }
  if (!shared?.length) return null;

  return (
    <GlassCard className="mb-4 p-[18px]">
      <div className="mb-3 flex items-center gap-2.5">
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[10px] bg-[rgba(139,92,246,.16)] text-[#c4b5fd]">
          <Boxes size={16} strokeWidth={2.2} />
        </span>
        <div>
          <div className="text-[14px] font-bold">{t("shared.title")}</div>
          <div className="text-[11.5px] text-[#8b8b9e]">{t("shared.subtitle")}</div>
        </div>
      </div>

      <div className="flex flex-col gap-2">
        {shared.map((p) => (
          <SharedProjectRow
            key={p.key}
            project={p}
            cloning={clone.isPending && clone.variables === p.key}
            onClone={() => clone.mutate(p.key)}
          />
        ))}
      </div>
    </GlassCard>
  );
}

function SharedProjectRow({
  project,
  cloning,
  onClone,
}: {
  project: SharedProjectOut;
  cloning: boolean;
  onClone: () => void;
}) {
  const { t } = useTranslation("projects");
  const kind = (project.providerKind || "ado") as ProviderKind;
  const [glyph, glyphBg] = providerGlyph[kind] ?? ["?", "#6b7280"];
  const glyphColor = kind === "github" ? "#12121a" : "#fff";
  // Cloning reuses already-built knowledge — there's nothing to reuse until at
  // least one repo is indexed, so block the clone otherwise (mirrors the API).
  const ready = project.knowledge.some((k) => k.status === "indexed");

  return (
    <div
      className="flex items-center gap-3 rounded-[14px] border border-white/[0.06] bg-white/[0.03] p-[12px_14px]"
      style={project.alreadyCloned ? { opacity: 0.6 } : undefined}
    >
      <div
        className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[9px] text-[13px] font-black"
        style={{ background: glyphBg, color: glyphColor }}
      >
        {glyph}
      </div>
      <div className="min-w-0 flex-1">
        <div className="truncate text-[13px] font-bold">{project.name}</div>
        <div className="mt-1 flex flex-wrap items-center gap-1.5">
          {project.knowledge.length === 0 ? (
            <span className="text-[11px] text-[#6c6c7e]">{t("shared.noKnowledge")}</span>
          ) : (
            project.knowledge.map((k) => {
              const [label, color, bg] = knowledgeStatusStyle(k.status);
              return (
                <Pill key={k.repo || "_"} color={color} bg={bg}>
                  {k.repo ? `${k.repo} · ` : ""}
                  {label}
                  {k.status === "indexed" ? ` · ${k.confidence}%` : ""}
                </Pill>
              );
            })
          )}
        </div>
      </div>
      {project.alreadyCloned ? (
        <span className="flex shrink-0 items-center gap-1.5 text-[12px] font-semibold text-[#6ee7b7]">
          <Check size={14} strokeWidth={2.4} /> {t("shared.cloned")}
        </span>
      ) : (
        <Button
          variant="glass"
          size="sm"
          className="shrink-0"
          disabled={cloning || !ready}
          onClick={onClone}
          title={ready ? undefined : t("shared.notReady")}
        >
          {cloning ? <Spinner size={13} /> : <Copy size={13} strokeWidth={2.2} />}
          {cloning ? t("shared.cloning") : t("shared.clone")}
        </Button>
      )}
    </div>
  );
}
