import { Check, Pause, Play, Sparkles, Telescope } from "lucide-react";
import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { GlassCard } from "@/components/ui/GlassCard";
import {
  useAuthoringState,
  useContinueAuthoring,
  usePauseAuthoring,
  useSettings,
} from "@/hooks/queries";
import { toast } from "@/lib/toast";
import { THINKING_STEPS } from "./useThinkingSteps";
import { describeExploreStep } from "./exploreStep";
import type { AuthoringProgress, ExploreProgress, ExploreStep, GenProgress, HealProgress } from "./useAutomationEvents";

/** A raw tool/Bash step line in the authoring trail (emitted by the agent as
 * `▷ <tool>: …`). These are the "system" lines hidden in concise mode. */
function isToolLine(line: string): boolean {
  return line.trimStart().startsWith("▷");
}

/** Full-height placeholder card shown while the first generation pass runs. */
export function ThinkingBanner({ runCode, thinkStep }: { runCode: string | undefined; thinkStep: number }) {
  const { t } = useTranslation("pipeline");
  return (
    <GlassCard className="p-4 md:p-[26px]" style={{ borderColor: "rgba(139,92,246,.28)" }}>
      <div className="mb-[22px] flex items-center gap-[13px]">
        <div
          className="flex h-11 w-11 items-center justify-center rounded-[14px]"
          style={{ background: "linear-gradient(135deg,#8b5cf6,#6366f1)", boxShadow: "0 0 26px rgba(139,92,246,.6)" }}
        >
          <Sparkles size={22} color="#fff" />
        </div>
        <div>
          <div className="text-[15px] font-bold">{t("progress.thinking.title")}</div>
          <div className="mt-0.5 text-xs text-muted">{t("progress.thinking.subtitle", { runCode })}</div>
        </div>
      </div>
      <div className="flex flex-col gap-[13px]">
        {THINKING_STEPS.map((key, i) => {
          const done = i < thinkStep;
          const active = i === thinkStep;
          if (!done && !active) return null;
          return (
            <div key={key} className="flex items-center gap-3 text-[13.5px]">
              {done ? (
                <span className="flex h-[19px] w-[19px] shrink-0 items-center justify-center rounded-full bg-success">
                  <Check size={12} color="#fff" strokeWidth={3} />
                </span>
              ) : (
                <span
                  className="h-[19px] w-[19px] shrink-0 rounded-full border-2"
                  style={{ borderColor: "rgba(167,139,250,.35)", borderTopColor: "#a78bfa", animation: "spin .8s linear infinite" }}
                />
              )}
              <span className={done ? "text-muted" : "font-semibold text-ink"}>{t(key)}</span>
            </div>
          );
        })}
      </div>
    </GlassCard>
  );
}

/** Compact "Generating automation…" banner with live progress detail. */
export function GeneratingBanner({ genProgress }: { genProgress: GenProgress | null }) {
  const { t } = useTranslation("pipeline");
  return (
    <GlassCard className="mb-3.5 flex items-center gap-3 p-4" style={{ borderColor: "rgba(139,92,246,.28)" }}>
      <span
        className="h-[18px] w-[18px] shrink-0 rounded-full border-2"
        style={{ borderColor: "rgba(167,139,250,.35)", borderTopColor: "#a78bfa", animation: "spin .8s linear infinite" }}
      />
      <div className="min-w-0">
        <div className="text-[13.5px] font-bold">
          {t("progress.generating.title")}
          {genProgress && genProgress.total > 0 ? ` ${genProgress.done}/${genProgress.total}` : ""}
        </div>
        {genProgress && (genProgress.file || genProgress.message) && (
          <div className="mt-0.5 truncate text-xs text-muted">
            {genProgress.file ? <span className="font-mono">{genProgress.file}</span> : null}
            {genProgress.file && genProgress.message ? " · " : ""}
            {genProgress.message}
          </div>
        )}
      </div>
    </GlassCard>
  );
}

/** Compact self-heal progress banner shown while a heal is in flight. */
export function HealProgressBanner({ healProgress }: { healProgress: HealProgress }) {
  const { t } = useTranslation("pipeline");
  return (
    <GlassCard className="mb-3.5 flex items-center gap-3 p-4" style={{ borderColor: "rgba(16,185,129,.32)" }}>
      <span
        className="h-[18px] w-[18px] shrink-0 rounded-full border-2"
        style={{ borderColor: "rgba(52,211,153,.35)", borderTopColor: "#34d399", animation: "spin .8s linear infinite" }}
      />
      <div className="min-w-0">
        <div className="text-[13.5px] font-bold">
          {t("progress.heal.progressTitle", {
            caseCode: healProgress.caseCode,
            attempt: healProgress.attempt,
            maxAttempts: healProgress.maxAttempts,
          })}
        </div>
        <div className="mt-0.5 truncate text-xs text-muted">
          {healProgress.phase === "fixing"
            ? t("progress.heal.fixing", {
                detail: healProgress.error || t("progress.heal.addressingFailure"),
              })
            : t("progress.heal.runningSpec")}
        </div>
      </div>
    </GlassCard>
  );
}

/** The streamed step log body — Claude's messages + browser-harness tool calls —
 * with a working spinner until done. Reused by the banner and the code panel.
 *
 * Respects the `authoringLogVerbosity` setting: in "concise" (default) the raw
 * tool/Bash lines are hidden so users see only Claude's narration + phase status;
 * "verbose" shows everything. Auto-scrolls to the latest line as it streams. */
export function AuthoringTrail({
  lines,
  done,
  paused = false,
}: {
  lines: string[];
  done: boolean;
  /** #619: a paused session is not "working" — a spinner there reads as a hang. */
  paused?: boolean;
}) {
  const { data: settings } = useSettings();
  const concise = (settings?.authoringLogVerbosity ?? "concise") === "concise";
  const shown = concise ? lines.filter((l) => !isToolLine(l)) : lines;

  // Keep the newest line in view as the trail streams (and when it finishes) —
  // scroll the trail container only, never the page.
  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [shown.length, done]);

  return (
    <div ref={scrollRef} className="flex max-h-[280px] flex-col gap-1.5 overflow-auto font-mono text-[12px]">
      {shown.map((l, i) => (
        <div key={i} className="whitespace-pre-wrap break-words text-muted">
          {l}
        </div>
      ))}
      {!done && !paused && (
        <div className="flex items-center gap-2 text-[12px]">
          <span
            className="h-[14px] w-[14px] shrink-0 rounded-full border-2"
            style={{ borderColor: "rgba(167,139,250,.35)", borderTopColor: "#a78bfa", animation: "spin .8s linear infinite" }}
          />
          <span className="text-ink">working…</span>
        </div>
      )}
    </div>
  );
}

/**
 * Pause / Continue for a live-authoring session (#619).
 *
 * Renders nothing unless the case actually has a live session, so the same
 * component is safe in both the banner and the code panel. The button set is
 * driven by the SERVER's view of the session (`canPause` / `canContinue`), not by
 * the WS trail: the trail is a stream of lines, and a user who reloaded has none
 * of it while their device is still holding a browser open.
 */
export function AuthoringPauseControls({ caseId }: { caseId: number }) {
  const { t } = useTranslation("pipeline");
  const { data } = useAuthoringState(caseId, caseId > 0);
  const pause = usePauseAuthoring(caseId);
  const resume = useContinueAuthoring(caseId);
  if (!data?.active) return null;
  const paused = data.status === "paused";
  const busy = pause.isPending || resume.isPending;
  const fail = (err: unknown) =>
    toast.error((err as { message?: string })?.message || String(err));
  return (
    <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-white/[0.06] pt-3">
      {paused ? (
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            resume
              .mutateAsync("")
              .then(() => toast.success(t("progress.authoring.continuing")))
              .catch(fail);
          }}
          className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[12px] font-semibold text-white disabled:opacity-50"
          style={{ background: "linear-gradient(135deg,#8b5cf6,#6366f1)" }}
        >
          <Play size={13} /> {t("progress.authoring.continue")}
        </button>
      ) : (
        <button
          type="button"
          disabled={busy || !data.canPause}
          onClick={() => {
            pause.mutateAsync().catch(fail);
          }}
          className="inline-flex items-center gap-1.5 rounded-lg bg-white/[0.06] px-2.5 py-1.5 text-[12px] font-semibold text-ink disabled:opacity-50"
        >
          <Pause size={13} />{" "}
          {data.pausePending ? t("progress.authoring.pausing") : t("progress.authoring.pause")}
        </button>
      )}
      <span className="min-w-0 flex-1 text-[11px] text-muted">
        {paused
          ? data.resumable
            ? t("progress.authoring.pausedHint")
            : t("progress.authoring.pausedFreshHint")
          : t("progress.authoring.pauseHint")}
      </span>
      {typeof data.remainingBudgetUsd === "number" && data.remainingBudgetUsd > 0 && (
        <span className="rounded-full bg-white/[0.06] px-2 py-0.5 font-mono text-[11px] text-muted">
          {t("progress.authoring.budgetLeft", {
            amount: data.remainingBudgetUsd.toFixed(2),
          })}
        </span>
      )}
    </div>
  );
}

/** Cost pill shown once the authoring run reports its Claude spend. */
export function AuthoringCost({ costUsd }: { costUsd: number | undefined }) {
  if (typeof costUsd !== "number") return null;
  return (
    <span className="rounded-full bg-white/[0.06] px-2 py-0.5 font-mono text-[11px] text-muted">
      ${costUsd.toFixed(2)}
    </span>
  );
}

/** Live authoring trail (#400): the streamed step log while the paired agent
 * drives browser-harness to author a spec — Claude's messages + tool calls. */
export function AuthoringProgressBanner({ authoringProgress }: { authoringProgress: AuthoringProgress }) {
  const { lines, done, costUsd, paused } = authoringProgress;
  return (
    <GlassCard className="mb-3.5 p-4 md:p-[22px]" style={{ borderColor: "rgba(139,92,246,.32)" }}>
      <div className="mb-[14px] flex items-center gap-[13px]">
        <div
          className="flex h-11 w-11 items-center justify-center rounded-[14px]"
          style={{ background: "linear-gradient(135deg,#8b5cf6,#6366f1)", boxShadow: "0 0 26px rgba(139,92,246,.55)" }}
        >
          <Sparkles size={22} color="#fff" />
        </div>
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-[15px] font-bold">
            Live authoring
            <AuthoringCost costUsd={costUsd} />
          </div>
          <div className="mt-0.5 text-xs text-muted">Claude is driving the browser to author this spec</div>
        </div>
      </div>
      <AuthoringTrail lines={lines} done={done} paused={paused} />
      {!done && <AuthoringPauseControls caseId={authoringProgress.caseId} />}
    </GlassCard>
  );
}

/** Live DOM-exploration banner: a stepped list of what the agent has observed
 * and done so far, driving toward unblocking the case (mirrors ThinkingBanner). */
export function ExploreProgressBanner({ exploreProgress }: { exploreProgress: ExploreProgress }) {
  const { t } = useTranslation("pipeline");
  const { steps } = exploreProgress;
  const latest = steps[steps.length - 1] as ExploreStep | undefined;
  return (
    <GlassCard className="mb-3.5 p-4 md:p-[22px]" style={{ borderColor: "rgba(56,189,248,.32)" }}>
      <div className="mb-[18px] flex items-center gap-[13px]">
        <div
          className="flex h-11 w-11 items-center justify-center rounded-[14px]"
          style={{ background: "linear-gradient(135deg,#38bdf8,#0ea5e9)", boxShadow: "0 0 26px rgba(56,189,248,.55)" }}
        >
          <Telescope size={22} color="#fff" />
        </div>
        <div className="min-w-0">
          <div className="text-[15px] font-bold">{t("progress.explore.banner.title")}</div>
          <div className="mt-0.5 text-xs text-muted">
            {t("progress.explore.banner.subtitle")}
            {latest
              ? ` · ${t("progress.explore.banner.budgetLeft", { amount: latest.remainingBudgetUsd.toFixed(2) })}`
              : ""}
          </div>
        </div>
      </div>
      <div className="flex flex-col gap-[11px]">
        {steps.map((s) => {
          const done = s.action === "done";
          const failed = s.ok === false;
          return (
            <div key={s.step} className="flex items-start gap-3 text-[13.5px]">
              <span
                className={`mt-[1px] flex h-[19px] w-[19px] shrink-0 items-center justify-center rounded-full ${
                  failed ? "bg-rose-500/80" : "bg-sky-500"
                }`}
              >
                <Check size={12} color="#fff" strokeWidth={3} />
              </span>
              <div className="min-w-0">
                <span className="font-semibold text-ink">{describeExploreStep(s, t)}</span>
                {s.reasoning && <span className="ml-1.5 text-xs text-muted">— {s.reasoning}</span>}
                {s.observedUrl && !done && (
                  <span className="ml-1.5 font-mono text-[11px] text-faint">@ {s.observedUrl}</span>
                )}
              </div>
            </div>
          );
        })}
        {!exploreProgress.done && (
          <div className="flex items-center gap-3 text-[13.5px]">
            <span
              className="h-[19px] w-[19px] shrink-0 rounded-full border-2"
              style={{ borderColor: "rgba(56,189,248,.35)", borderTopColor: "#38bdf8", animation: "spin .8s linear infinite" }}
            />
            <span className="font-semibold text-ink">{t("progress.explore.banner.deciding")}</span>
          </div>
        )}
      </div>
    </GlassCard>
  );
}
