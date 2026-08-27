import { AnimatePresence, motion } from "framer-motion";
import { ArrowLeft, ArrowRight, Check, Loader2, X } from "lucide-react";
import { type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/cn";
import {
  RUN_STAGES,
  STAGE_COUNT,
  type RunStageKey,
} from "@/components/runs/runStages";

/**
 * The run wizard's chrome (ADR 0015 §4, #730) — top bar, stepper, footer.
 *
 * ## Why a portal
 *
 * A run is a **full-screen overlay on top of the project**, not a screen inside
 * the shell. `position: fixed` cannot do that from where the run route renders:
 * `AppLayout`'s route wrapper is a `motion.div` with a transform, and a
 * transform creates a containing block for fixed descendants — so the overlay
 * would be trapped inside the content column, under the sidebar. Portalling to
 * `document.body` is the fix (CLAUDE.md's rule for floating overlays), with
 * `createPortal` on the OUTSIDE and `AnimatePresence` as the direct parent of
 * the animating element, or it never mounts.
 *
 * ## The stepper indicates, it does not navigate
 *
 * Future stages are locked and the pills are not buttons. Movement is Back /
 * Next only, so the user cannot skip a stage's gate by clicking ahead — and
 * cannot land on a stage whose prerequisites have not run.
 */
export function RunOverlay({
  title,
  subtitle,
  viewedStage,
  furthestStage,
  busyLabel,
  onExit,
  onBack,
  onNext,
  backDisabled,
  nextDisabled,
  nextLabel,
  nextHint,
  children,
}: {
  title: string;
  subtitle: string;
  /** The stage being LOOKED AT — from the URL. */
  viewedStage: number;
  /** How far the RUN has got — from `run.status`. Never the same variable. */
  furthestStage: number;
  /** Label for the spinner chip while a hidden automatic stage works. */
  busyLabel: string | null;
  onExit: () => void;
  onBack: () => void;
  onNext: () => void;
  backDisabled: boolean;
  nextDisabled: boolean;
  nextLabel: string;
  /** Why Next is disabled — shown beside it, so the gate is never mute. */
  nextHint: string | null;
  children: ReactNode;
}) {
  const { t } = useTranslation("runs");

  return createPortal(
    <AnimatePresence>
      <motion.div
        key="run-overlay"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.18, ease: "easeOut" }}
        data-testid="run-overlay"
        // Opaque, not translucent: this sits over the animated app background,
        // and `backdrop-filter` over animated content both causes compositing
        // artifacts and creates its own stacking-context trap (CLAUDE.md).
        className="fixed inset-0 z-[80] flex flex-col"
        style={{ background: "#0b0b11" }}
      >
        {/* ---------------------------------------------------------- top bar */}
        <header className="flex shrink-0 items-center gap-3 border-b border-white/[0.07] px-4 py-3 md:px-6">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <h1 className="m-0 truncate text-[16px] font-black tracking-tight md:text-[18px]">
                {title}
              </h1>
              {/* The ONLY indication that a hidden automatic stage is working
                  (Analyze / Link). No stepper entry, no footer strip — the
                  wizard advances by itself when the worker finishes. */}
              <AnimatePresence>
                {busyLabel && (
                  <motion.span
                    key={busyLabel}
                    initial={{ opacity: 0, scale: 0.94 }}
                    animate={{ opacity: 1, scale: 1 }}
                    exit={{ opacity: 0, scale: 0.94 }}
                    data-testid="run-busy-chip"
                    className="flex shrink-0 items-center gap-1.5 rounded-full border border-white/[0.1] bg-white/[0.05] px-2.5 py-1 text-[11px] font-semibold text-ink-soft"
                  >
                    <Loader2 size={11} strokeWidth={2.4} className="animate-spin" />
                    {busyLabel}
                  </motion.span>
                )}
              </AnimatePresence>
            </div>
            <div className="mt-0.5 truncate text-[12px] text-ink-dim">{subtitle}</div>
          </div>
          <button
            type="button"
            onClick={onExit}
            data-testid="run-exit"
            aria-label={t("overlay.exit")}
            className="flex shrink-0 cursor-pointer items-center gap-1.5 rounded-[10px] border border-white/[0.1] bg-white/[0.05] px-3 py-1.5 text-[12px] font-semibold text-ink-soft hover:bg-white/[0.09]"
          >
            <X size={13} strokeWidth={2.2} />
            <span className="hidden md:inline">{t("overlay.exit")}</span>
          </button>
        </header>

        {/* --------------------------------------------------------- stepper */}
        <nav
          aria-label={t("overlay.stepperLabel")}
          className="scrollbar-none flex shrink-0 gap-2 overflow-x-auto border-b border-white/[0.06] px-4 py-2.5 md:px-6"
        >
          {RUN_STAGES.map((stage, index) => (
            <StagePill
              key={stage.key}
              stageKey={stage.key}
              index={index}
              viewedStage={viewedStage}
              furthestStage={furthestStage}
            />
          ))}
        </nav>

        {/* ----------------------------------------------------------- stage */}
        <main className="min-h-0 flex-1 overflow-y-auto px-3 py-3 md:px-6 md:py-4">
          {children}
        </main>

        {/* ---------------------------------------------------------- footer */}
        <footer className="flex shrink-0 items-center gap-3 border-t border-white/[0.07] px-4 py-3 md:px-6">
          <button
            type="button"
            onClick={onBack}
            disabled={backDisabled}
            data-testid="run-back"
            className={cn(
              "flex items-center gap-1.5 rounded-[11px] border border-white/[0.1] px-4 py-2 text-[13px] font-semibold",
              backDisabled
                ? "cursor-not-allowed bg-white/[0.02] text-ink-dim/50"
                : "cursor-pointer bg-white/[0.05] text-ink-soft hover:bg-white/[0.09]",
            )}
          >
            <ArrowLeft size={14} strokeWidth={2.2} />
            {t("overlay.back")}
          </button>

          <div className="min-w-0 flex-1 text-center text-[12px] text-ink-dim">
            {/* `viewedStage < 0` is the terminal completion stage (#731): it is
                not step N of five, so it gets no step counter. */}
            {nextHint ??
              (viewedStage < 0
                ? t("overlay.allStagesComplete")
                : t("overlay.stepOf", { current: viewedStage + 1, total: STAGE_COUNT }))}
          </div>

          <button
            type="button"
            onClick={onNext}
            disabled={nextDisabled}
            data-testid="run-next"
            className={cn(
              "flex items-center gap-1.5 rounded-[11px] border-none px-4 py-2 text-[13px] font-semibold",
              nextDisabled
                ? "cursor-not-allowed bg-white/[0.05] text-ink-dim/60"
                : "cursor-pointer text-white",
            )}
            style={
              nextDisabled
                ? undefined
                : { background: "linear-gradient(135deg,#8b5cf6,#6366f1)" }
            }
          >
            {nextLabel}
            <ArrowRight size={14} strokeWidth={2.2} />
          </button>
        </footer>
      </motion.div>
    </AnimatePresence>,
    document.body,
  );
}

function StagePill({
  stageKey,
  index,
  viewedStage,
  furthestStage,
}: {
  stageKey: RunStageKey;
  index: number;
  viewedStage: number;
  furthestStage: number;
}) {
  const { t } = useTranslation("runs");
  const isViewed = index === viewedStage;
  // "Complete" is measured against the RUN's progress, not the user's position —
  // walking back to Review must not un-tick Automation.
  const isComplete = index < furthestStage;
  const isLocked = index > furthestStage;

  return (
    <div
      data-testid={`run-stage-${stageKey}`}
      data-state={isViewed ? "viewed" : isComplete ? "complete" : isLocked ? "locked" : "current"}
      aria-current={isViewed ? "step" : undefined}
      className={cn(
        "flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-[10px] px-3 py-[7px] text-[12.5px] font-semibold",
        isLocked && "text-ink-dim/45",
        !isLocked && !isViewed && "text-ink-dim",
      )}
      style={
        isViewed
          ? {
              background: "linear-gradient(135deg,rgba(139,92,246,.26),rgba(99,102,241,.13))",
              color: "#fff",
              boxShadow: "inset 0 0 0 1px rgba(139,92,246,.32)",
            }
          : { background: "rgba(255,255,255,.035)" }
      }
    >
      <span
        className="flex h-[18px] w-[18px] items-center justify-center rounded-full text-[10px] font-black"
        style={{
          background: isComplete ? "rgba(16,185,129,.18)" : "rgba(255,255,255,.07)",
          color: isComplete ? "#6ee7b7" : undefined,
        }}
      >
        {isComplete ? <Check size={11} strokeWidth={3} /> : index + 1}
      </span>
      {t(`overlay.stage.${stageKey}`)}
    </div>
  );
}
