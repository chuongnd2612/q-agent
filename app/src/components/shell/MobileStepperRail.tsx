import { Check } from "lucide-react";
import { useEffect, useRef } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { isRunComplete, runStatusToStage } from "@/components/ui/PipelineRail";
import { useRun } from "@/hooks/queries";

/**
 * The in-run pipeline as a horizontal, swipeable stepper — the mobile
 * replacement for the desktop `RunSidebar` pipeline. Six pills (the navigable
 * stages) that auto-scroll to centre the active pill on navigation. Shown only
 * inside a run, below the mobile top bar. See MOBILE_SPEC §1c.
 */
// The per-run pipeline as pills. Stage numbers match runStatusToStage +
// navConfig.PIPELINE (6 stages; Analyze/processing folds into Review, and
// Sync/Select are pre-run — none are shown here).
const STEPPER: { label: string; seg: string; stage: number }[] = [
  { label: "Review", seg: "review", stage: 1 },
  { label: "Link", seg: "sync", stage: 2 },
  { label: "Automation", seg: "automation", stage: 3 },
  { label: "Execution", seg: "execution", stage: 4 },
  { label: "Evidence", seg: "evidence", stage: 5 },
  { label: "Publish", seg: "comment", stage: 6 },
];

export function MobileStepperRail({ runId }: { runId: number }) {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const { data: run } = useRun(runId);
  const railRef = useRef<HTMLDivElement>(null);
  const activeRef = useRef<HTMLButtonElement>(null);

  const urlSeg = pathname.match(/^\/runs\/\d+(?:\/(\w+))?/)?.[1] ?? "";
  const currentStage = run ? runStatusToStage[run.status] ?? 0 : 0;
  // See RunSidebar: a finished run has no current stage (#724).
  const runComplete = isRunComplete(run?.status);

  // Centre the active pill whenever the route (or, once loaded, the run) changes.
  useEffect(() => {
    const rail = railRef.current;
    const active = activeRef.current;
    if (!rail || !active) return;
    rail.scrollTo({
      left: active.offsetLeft - (rail.clientWidth - active.offsetWidth) / 2,
      behavior: "smooth",
    });
  }, [urlSeg, currentStage]);

  return (
    <div
      ref={railRef}
      className="scrollbar-none z-[19] flex shrink-0 gap-1.5 overflow-x-auto rounded-[16px] bg-[rgba(12,12,18,.5)] px-3 py-2.5"
    >
      {STEPPER.map((step) => {
        const active = urlSeg === step.seg;
        const done = runComplete || currentStage > step.stage;
        return (
          <button
            key={step.label}
            ref={active ? activeRef : undefined}
            onClick={() => navigate(`/runs/${runId}/${step.seg}`)}
            className="flex shrink-0 items-center gap-2 whitespace-nowrap rounded-[20px] border py-1.5 pl-1.5 pr-3 text-[12px] font-bold transition-colors"
            style={{
              borderColor: active ? "rgba(139,92,246,.5)" : "rgba(255,255,255,.08)",
              background: active ? "rgba(139,92,246,.18)" : "rgba(255,255,255,.03)",
              color: active ? "#ECECF1" : "#8b8b9e",
            }}
          >
            <span
              className="flex h-[19px] w-[19px] items-center justify-center rounded-full font-mono text-[9.5px] font-bold"
              style={{
                background:
                  done || active ? "linear-gradient(135deg,#8b5cf6,#6366f1)" : "rgba(255,255,255,.09)",
                color: done || active ? "#fff" : "#7a7a8c",
              }}
            >
              {done ? <Check size={11} strokeWidth={3} /> : step.stage}
            </span>
            {step.label}
          </button>
        );
      })}
    </div>
  );
}
