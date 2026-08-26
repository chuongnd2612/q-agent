import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Radio } from "lucide-react";
import { runBadge, runColor, runEffectiveStatus, runMeta, timeAgoShort } from "@/components/dashboard/runStatus";
import type { ProjectOut, RunOut } from "@/types/api";

/**
 * "What is running right now", across every project (ADR 0015 §1, #733).
 *
 * This is the one genuinely cross-project question in the app, and slice 3 takes
 * the global Runs list out of the sidebar — so this strip is the only place left
 * that answers it. It reads the **workspace-wide** run list (`useRuns()` with no
 * project argument) on purpose; everything else on this screen is project-scoped.
 *
 * Opaque surfaces, no `backdrop-filter`: this sits over the animated shell
 * backdrop and carries small text (CLAUDE.md).
 */
export function RunningNow({
  runs,
  projectNames,
}: {
  /** Non-terminal runs, newest first. */
  runs: RunOut[];
  /** GUID → display name, for the project each run belongs to. */
  projectNames: Map<string, ProjectOut>;
}) {
  const { t } = useTranslation("dashboard");
  const navigate = useNavigate();

  return (
    <section
      className="mb-4 rounded-[18px] p-[18px]"
      style={{ background: "rgba(8,8,13,.72)", border: "1px solid rgba(139,92,246,.22)" }}
    >
      <div className="mb-3 flex items-center gap-2.5">
        <Radio size={15} className="shrink-0" color="#a78bfa" strokeWidth={2.2} />
        <span className="text-[13.5px] font-bold">{t("dashboard.runningNow.title")}</span>
        <span className="text-[11.5px] text-muted">{t("dashboard.runningNow.subtitle")}</span>
        <span className="ml-auto text-[11.5px] font-semibold text-[#a78bfa]">
          {t("dashboard.runningNow.count", { count: runs.length })}
        </span>
      </div>

      {runs.length === 0 ? (
        <p className="m-0 text-[12.5px] text-ink-dim">{t("dashboard.runningNow.empty")}</p>
      ) : (
        <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2 xl:grid-cols-3">
          {runs.map((r) => {
            const status = runEffectiveStatus(r);
            const color = runColor(status);
            const project = r.projectGuid ? projectNames.get(r.projectGuid) : undefined;
            return (
              <button
                key={r.id}
                onClick={() => navigate(`/runs/${r.id}`)}
                className="flex cursor-pointer items-center gap-3 rounded-[14px] border-none p-3 text-left"
                style={{ background: "rgba(255,255,255,.045)" }}
              >
                <span
                  className="h-2 w-2 shrink-0 rounded-full"
                  style={{ background: color, animation: "pulseDot 1.7s infinite" }}
                />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[12.5px] font-semibold text-[#dcdce4]">
                    {project?.name ?? t("dashboard.runningNow.unassigned")}
                  </span>
                  <span className="block truncate font-mono text-[10.5px] text-[#7a7a8c]">
                    {r.code} · {runMeta(r)}
                  </span>
                </span>
                <span className="shrink-0 text-right">
                  <span className="block text-[11.5px] font-bold" style={{ color }}>
                    {runBadge(status).label}
                  </span>
                  <span className="block text-[10.5px] text-[#7a7a8c]">
                    {timeAgoShort(r.createdAt)}
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      )}
    </section>
  );
}
