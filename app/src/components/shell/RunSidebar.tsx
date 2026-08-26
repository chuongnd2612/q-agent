import { ArrowLeft, Check, ChevronsUpDown } from "lucide-react";
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useLocation, useNavigate } from "react-router-dom";
import { useRunPath } from "@/hooks/useRunRouteId";
import { cn } from "@/lib/cn";
import { RunSwitcher } from "@/components/shell/RunSwitcher";
import { GLOBAL_MINI, PIPELINE } from "@/components/shell/navConfig";
import { runColor, runEffectiveStatus, runRateLabel } from "@/components/dashboard/runStatus";
import { isRunComplete, runStatusToStage } from "@/components/ui/PipelineRail";
import { useRun } from "@/hooks/queries";

/**
 * Workspace-mode sidebar shown while inside a run (`/runs/:runId/*`). The whole
 * sidebar becomes the run: an "All of Q-Agent" exit, a run identity card with a
 * switcher, the pipeline-as-navigation, and a pinned global mini-row. Run-scoped
 * screens are reachable only from here, so they can't be opened without a run.
 */
export function RunSidebar({ runId }: { runId: number }) {
  const navigate = useNavigate();
  const runPath = useRunPath();
  const { pathname } = useLocation();
  const { t } = useTranslation("nav");
  const { data: run } = useRun(runId);
  const [switcherOpen, setSwitcherOpen] = useState(false);
  const switchBtnRef = useRef<HTMLButtonElement>(null);

  // Current URL sub-segment (`review` | `sync` | … ), null on the run index.
  const urlSeg = pathname.match(/^\/runs\/\d+(?:\/(\w+))?/)?.[1] ?? null;
  // 1-based pipeline stage the run is currently at. Terminal statuses
  // (failed/cancelled) don't map to a stage, so fall back to the stage the run
  // failed AT (`failedStage`) — otherwise nothing is highlighted for a failed run.
  const currentStage = run
    ? (runStatusToStage[run.status] ??
        (run.failedStage ? runStatusToStage[run.failedStage] : undefined) ??
        0)
    : 0;
  const runComplete = isRunComplete(run?.status);
  const effectiveStatus = run ? runEffectiveStatus(run) : null;
  const accent = effectiveStatus ? runColor(effectiveStatus) : "#a0a0b2";

  return (
    <aside className="glass-strong flex w-[248px] shrink-0 flex-col rounded-[22px] p-[20px_14px] shadow-[0_24px_60px_-20px_rgba(0,0,0,.6)]">
      <button
        onClick={() => navigate("/")}
        className="mb-3 flex items-center gap-2.5 rounded-[10px] border border-white/[0.08] bg-white/[0.04] px-2.5 py-[7px] text-left text-[11.5px] font-semibold text-ink-dim transition-colors hover:bg-white/[0.07]"
      >
        <ArrowLeft size={14} strokeWidth={2} />
        {t("run.back")}
      </button>

      <div
        className="relative mb-1.5 rounded-[13px] p-3"
        style={{
          background:
            "linear-gradient(135deg,rgba(139,92,246,.18),rgba(99,102,241,.08))",
          border: "1px solid rgba(139,92,246,.3)",
        }}
      >
        <div className="mb-1.5 flex items-center gap-2">
          <span className="font-mono text-[10.5px] font-bold text-[#c4b5fd]">
            {run?.code ?? `RUN-${runId}`}
          </span>
          {run && (
            <span
              className="rounded-full px-2 py-0.5 text-[9.5px] font-bold"
              style={{
                background: `${accent}2e`,
                color: accent,
              }}
            >
              {runRateLabel(effectiveStatus ?? run.status)}
            </span>
          )}
        </div>
        <div className="text-[13px] font-extrabold leading-[1.25] tracking-tight">
          {run?.name ?? t("run.loading")}
        </div>
        {run && (
          <div className="mt-[5px] text-[10px] text-[#b9a8e6]">
            {t("run.meta", {
              count: run.ticketIds.length,
              framework: run.framework,
              env: run.env,
            })}
          </div>
        )}
        <button
          ref={switchBtnRef}
          onClick={() => setSwitcherOpen((o) => !o)}
          title={t("topbar.switchRun")}
          className="absolute right-2.5 top-2.5 flex h-[22px] w-[22px] items-center justify-center rounded-[7px] bg-white/[0.08] text-[#c7c7d4] transition-colors hover:bg-white/[0.16]"
        >
          <ChevronsUpDown size={13} strokeWidth={2} />
        </button>
        <RunSwitcher
          open={switcherOpen}
          onClose={() => setSwitcherOpen(false)}
          anchorRef={switchBtnRef}
          runId={runId}
        />
      </div>

      <div className="px-2 pb-1.5 pt-2 text-[10px] font-semibold tracking-[0.11em] text-[#5c5c6e]">
        {t("sections.pipeline")}
      </div>

      <nav className="relative flex flex-col gap-px overflow-y-auto py-1.5">
        {/* connector rail behind the nodes (node center ≈ 18px from the left) */}
        <div className="absolute bottom-5 left-[18px] top-5 w-0.5 bg-white/[0.09]" />
        {PIPELINE.map((step) => {
          // A finished run has no current stage (#724) — Publish kept showing "6"
          // next to a run that had already published, because completion was
          // `stage < currentStage` and `done` maps to the last stage number.
          const done = runComplete || (currentStage > 0 && step.stage < currentStage);
          const isCurrent = !runComplete && step.stage === currentStage;
          const activeUrl = step.seg != null && step.seg === urlSeg;
          const clickable = step.seg != null;
          const emphasized = isCurrent || activeUrl;

          const node = (
            <span
              className="relative z-10 flex h-5 w-5 shrink-0 items-center justify-center rounded-full font-mono text-[9.5px] font-bold"
              style={{
                background: done
                  ? "#10b981"
                  : emphasized
                    ? "linear-gradient(135deg,#8b5cf6,#6366f1)"
                    : "#12121a",
                border: done || emphasized ? "1px solid transparent" : "1px solid rgba(255,255,255,.14)",
                color: done || emphasized ? "#fff" : "#7a7a8c",
                boxShadow: emphasized ? "0 0 0 4px rgba(139,92,246,.2)" : undefined,
              }}
            >
              {done ? <Check size={11} strokeWidth={3} /> : step.stage}
            </span>
          );

          const label = (
            <span
              className="flex-1 text-[12px] font-semibold"
              style={{
                color: activeUrl || isCurrent
                  ? "#fff"
                  : done
                    ? "#b4b4c2"
                    : clickable
                      ? "#c7c7d4"
                      : "#8b8b9e",
              }}
            >
              {t(`pipeline.${step.key}`)}
            </span>
          );

          const stepClass = cn(
            "flex items-center gap-[11px] rounded-[9px] px-2 py-[5px] text-left",
            clickable && !activeUrl && "hover:bg-white/[0.05]",
          );
          const stepStyle = activeUrl
            ? {
                background:
                  "linear-gradient(135deg,rgba(139,92,246,.2),rgba(99,102,241,.1))",
                boxShadow: "inset 0 0 0 1px rgba(139,92,246,.28)",
              }
            : undefined;

          return clickable ? (
            <button
              key={step.label}
              data-tour={`stage-${step.seg}`}
              onClick={() => navigate(runPath(step.seg ?? undefined))}
              className={cn(stepClass, "w-full border-none")}
              style={stepStyle}
            >
              {node}
              {label}
            </button>
          ) : (
            <div key={step.label} className={stepClass} style={stepStyle}>
              {node}
              {label}
            </div>
          );
        })}
      </nav>

      <div className="mt-auto border-t border-white/[0.06] pt-2.5">
        <div className="px-2 pb-1.5 text-[10px] font-semibold tracking-[0.11em] text-[#5c5c6e]">
          {t("sections.global")}
        </div>
        <div className="flex gap-1.5 px-1">
          {GLOBAL_MINI.map((m) => {
            const Icon = m.icon;
            return (
              <button
                key={m.path}
                onClick={() => navigate(m.path)}
                title={t(`items.${m.key}`)}
                className="flex h-[30px] flex-1 items-center justify-center rounded-[9px] bg-white/[0.04] text-[#8b8b9e] transition-colors hover:bg-white/[0.08] hover:text-white"
              >
                <Icon size={15} strokeWidth={2} />
              </button>
            );
          })}
        </div>
      </div>
    </aside>
  );
}
