import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useQueryClient } from "@tanstack/react-query";
import { useRunEvents } from "@/hooks/useRunEvents";
import { queryKeys } from "@/lib/queryKeys";
import { toast } from "@/lib/toast";
import type { ProgressEvent } from "@/types/api";

/** Latest automation.progress detail for the "Generating…" banner. */
export type GenProgress = {
  done: number;
  total: number;
  file: string;
  message: string;
};

/** Live self-heal progress from the WS stream. */
export type HealProgress = {
  caseId: number;
  ticket: string;
  caseCode: string;
  attempt: number;
  maxAttempts: number;
  phase: "running" | "fixing" | "passed" | "failed" | "product_defect";
  message: string;
  error: string;
};

/** One step of a DOM-exploration session, as streamed on `explore.progress`
 * (ADR 0010). `action === "done"` marks the terminal step. */
export type ExploreStep = {
  step: number;
  reasoning: string;
  action: string;
  args: Record<string, unknown>;
  observedUrl: string;
  ok?: boolean;
  spentUsd: number;
  remainingBudgetUsd: number;
};

/** Live DOM-exploration progress: the ordered step trail for one session. The
 * banner reads it while a session runs; the discovery-review panel reads the
 * same trail once the session ends (the repo-scoped `explore/status` poll
 * supplies the durable discovered counts). */
export type ExploreProgress = {
  sessionId: string;
  steps: ExploreStep[];
  /** True once the model emitted the terminal `done` action. */
  done: boolean;
};

/** Live authoring trail (#400): the rolling step log the paired agent streams on
 * `authoring.progress` while it drives browser-harness to author a spec. */
export type AuthoringProgress = {
  caseId: number;
  lines: string[];
  /** True once the agent emitted a terminal `done`/`failed` phase. */
  done: boolean;
  /** #619: parked mid-session with the browser still open, awaiting Continue. */
  paused?: boolean;
  /** Claude $ the agentic authoring run spent (set on the terminal event). */
  costUsd?: number;
};

/**
 * Wraps the run's WS event handling for the Automation screen: captures live
 * generation and self-heal progress for the banners, surfaces per-spec errors as
 * toasts, invalidates the relevant queries on a terminal heal phase, and clears
 * the generation banner once generation is no longer active.
 *
 * @param runId The active run's id.
 * @param generating Whether generation is currently in flight.
 * @returns The current generation, self-heal, and DOM-exploration progress (or null).
 */
export function useAutomationEvents(runId: number, generating: boolean) {
  const qc = useQueryClient();
  const { t } = useTranslation("pipeline");

  // Latest automation.progress detail for the banner. Captured from the WS
  // stream (see onRunEvent); cleared when generation finishes.
  const [genProgress, setGenProgress] = useState<GenProgress | null>(null);

  // Live self-heal progress (from the WS stream). Cleared shortly after a
  // terminal phase (passed/failed).
  const [healProgress, setHealProgress] = useState<HealProgress | null>(null);

  // Live DOM-exploration trail (from the WS stream). Accumulates each step for
  // the live banner; the same trail powers the discovery-review panel once the
  // session ends. Reset only when a new session starts (a different sessionId).
  const [exploreProgress, setExploreProgress] = useState<ExploreProgress | null>(null);

  // Live authoring trail (from the WS stream). Accumulates each streamed step for
  // one case; cleared shortly after the terminal done/failed phase.
  const [authoringProgress, setAuthoringProgress] = useState<AuthoringProgress | null>(null);

  // Capture live progress for the banner, surface per-spec generation errors,
  // and clear progress when the background pass finishes (run.status flips once
  // every case has been attempted).
  const onRunEvent = useCallback((evt: ProgressEvent) => {
    if (evt.event === "run.status") {
      setGenProgress(null);
      return;
    }
    if (evt.event === "automation.progress") {
      const p = evt.payload as {
        message?: string;
        file?: string;
        error?: string;
        done?: number;
        total?: number;
      };
      const message = p.error || p.message || "";
      setGenProgress({
        done: typeof p.done === "number" ? p.done : 0,
        total: typeof p.total === "number" ? p.total : 0,
        file: p.file ?? "",
        message,
      });
      if (message.toLowerCase().startsWith("error")) {
        toast.error(`${p.file ?? t("progress.events.specFallback")}: ${message}`);
      }
      return;
    }
    if (evt.event === "heal.progress") {
      const p = evt.payload as {
        caseId: number;
        ticket: string;
        caseCode: string;
        attempt: number;
        maxAttempts: number;
        phase: "running" | "fixing" | "passed" | "failed" | "product_defect";
        message: string;
        error?: string;
      };
      setHealProgress({ ...p, error: p.error ?? "" });
      // Every terminal phase must clear + reconcile — otherwise the banner (and,
      // before, the button) sticks. `product_defect` is terminal too (the heal
      // routed the case to the report rather than fixing it).
      if (p.phase === "passed" || p.phase === "failed" || p.phase === "product_defect") {
        if (p.phase === "passed") toast.success(t("progress.events.healFixed", { caseCode: p.caseCode }));
        else if (p.phase === "product_defect")
          toast.error(t("progress.events.productDefect", { caseCode: p.caseCode }));
        else
          toast.error(
            t("progress.events.healGaveUp", {
              caseCode: p.caseCode,
              detail: p.message || t("progress.events.stillFailing"),
            }),
          );
        // The spec code, latest execution result, and heal trail changed on the server.
        qc.invalidateQueries({ queryKey: queryKeys.specs(runId) });
        qc.invalidateQueries({ queryKey: queryKeys.execution(runId) });
        qc.invalidateQueries({ queryKey: queryKeys.healStatus(p.caseId) });
        qc.invalidateQueries({ queryKey: queryKeys.healReport(p.caseId) });
        setTimeout(() => setHealProgress(null), 4000);
      }
    }
    if (evt.event === "authoring.progress") {
      const p = evt.payload as {
        case?: number;
        caseId?: number;
        phase?: string;
        message?: string;
        costUsd?: number;
      };
      const caseId = p.case ?? p.caseId ?? 0;
      const message = (p.message ?? "").trim();
      const terminal = p.phase === "done" || p.phase === "failed";
      // #619: `paused` means the device stopped Claude but kept Chrome, the temp
      // workdir and CLAUDE_CONFIG_DIR alive. It is NOT terminal — the trail must
      // stay, minus the "working…" spinner, and the Continue control appears.
      const paused = p.phase === "paused";
      // On session start, refetch specs so the newly-running row appears (a fresh
      // Generate returns before the background thread creates the row).
      if (p.phase === "launching") {
        qc.invalidateQueries({ queryKey: queryKeys.specs(runId) });
      }
      setAuthoringProgress((prev) => {
        // Same case still running ⇒ keep appending; otherwise start a fresh trail.
        const sameCase = prev != null && prev.caseId === caseId && !prev.done;
        const lines = sameCase ? [...prev.lines] : [];
        if (message) lines.push(message);
        const costUsd = typeof p.costUsd === "number" ? p.costUsd : sameCase ? prev?.costUsd : undefined;
        return { caseId, lines: lines.slice(-40), done: terminal, costUsd, paused };
      });
      // Pausing/resuming changes what the Pause/Continue controls may do, and the
      // server is the authority on that — so refetch rather than inferring it here.
      if (paused || p.phase === "guidance") {
        qc.invalidateQueries({ queryKey: queryKeys.authoringState(caseId) });
      }
      if (terminal) {
        // Persistent completion feedback incl. the Claude $ the authoring run spent
        // (the in-panel trail vanishes once the code replaces it).
        const cost = typeof p.costUsd === "number" ? ` · $${p.costUsd.toFixed(2)}` : "";
        if (p.phase === "done") toast.success(`${t("progress.authoring.authored")}${cost}`);
        else toast.error(`${t("progress.authoring.failed")}${cost}`);
        // The spec + cases changed on the server — refresh so the row reflects it.
        qc.invalidateQueries({ queryKey: queryKeys.specs(runId) });
        qc.invalidateQueries({ queryKey: queryKeys.runCases(runId) });
        setTimeout(() => setAuthoringProgress(null), 8000);
      }
      return;
    }
    if (evt.event === "explore.progress") {
      const p = evt.payload as {
        sessionId?: string;
        step: number;
        reasoning?: string;
        action: string;
        args?: Record<string, unknown>;
        observedUrl?: string;
        ok?: boolean;
        spentUsd?: number;
        remainingBudgetUsd?: number;
      };
      const sessionId = p.sessionId ?? "";
      const step: ExploreStep = {
        step: p.step,
        reasoning: p.reasoning ?? "",
        action: p.action,
        args: p.args ?? {},
        observedUrl: p.observedUrl ?? "",
        ok: p.ok,
        spentUsd: p.spentUsd ?? 0,
        remainingBudgetUsd: p.remainingBudgetUsd ?? 0,
      };
      const isDone = p.action === "done";
      setExploreProgress((prev) => {
        // A different sessionId ⇒ a fresh run: start a new trail.
        const sameSession = prev != null && prev.sessionId === sessionId;
        const steps = sameSession ? [...prev.steps] : [];
        const existing = steps.findIndex((s) => s.step === step.step);
        if (existing >= 0) steps[existing] = step; // WS re-delivery: replace in place
        else steps.push(step);
        return { sessionId, steps, done: (sameSession && prev.done) || isDone };
      });
      if (isDone) {
        // Terminal step: the KB may have gained runtime-verified entries that
        // unblock the case — refresh specs + cases so the banner reflects it.
        qc.invalidateQueries({ queryKey: queryKeys.specs(runId) });
        qc.invalidateQueries({ queryKey: queryKeys.runCases(runId) });
      }
      return;
    }
  }, [qc, runId, t]);
  useRunEvents(onRunEvent);

  // Belt-and-braces: clear the banner detail whenever generation is no longer
  // active (covers the case where the run.status event is missed).
  useEffect(() => {
    if (!generating) setGenProgress(null);
  }, [generating]);

  return { genProgress, healProgress, exploreProgress, authoringProgress };
}
