import { ChevronDown, Telescope } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import type { ExploreStatus } from "@/types/api";
import { RegenerateWithNote } from "./RegenerateWithNote";
import { describeExploreStep, EXPLORE_HUE } from "./exploreStep";
import type { ExploreProgress } from "./useAutomationEvents";

/** Friendly label for a session's stop reason (ADR 0010 §4). `t` is the
 * `pipeline` namespace translator supplied by the calling component. */
function stopReasonLabel(reason: string | null | undefined, t: TFunction): string {
  switch (reason) {
    case "done":
      return t("progress.explore.stop.done");
    case "step_cap":
      return t("progress.explore.stop.stepCap");
    case "budget":
      return t("progress.explore.stop.budget");
    case "repeat":
      return t("progress.explore.stop.repeat");
    case "unreachable":
      return t("progress.explore.stop.unreachable");
    default:
      return reason || t("progress.explore.stop.finished");
  }
}

/**
 * Collapsible "Exploration results" panel shown after a DOM-exploration session
 * ends (mirrors HealTimeline). The step trail comes from the WS stream
 * (`progress`); the durable discovered counts + stop reason come from the
 * repo-scoped `explore/status` poll (`status`). When the KB was enriched, a
 * Regenerate CTA lets the reviewer retry the now-grounded case.
 */
export function ExploreReview({
  progress,
  status,
  regenerating,
  onRegenerate,
}: {
  progress: ExploreProgress;
  status: ExploreStatus | undefined;
  regenerating: boolean;
  onRegenerate: (comment?: string) => void;
}) {
  const { t } = useTranslation("pipeline");
  const [open, setOpen] = useState(true);
  const routes = status?.discoveredRoutes ?? 0;
  const selectors = status?.discoveredSelectors ?? 0;
  const wroteKb = status?.wroteKb ?? false;
  const steps = progress.steps;

  return (
    <div className="overflow-hidden rounded-2xl border border-bd2 bg-code">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2.5 border-b border-bd3 px-4 py-3 text-left hover:bg-card"
      >
        <Telescope size={14} className="shrink-0" style={{ color: EXPLORE_HUE }} />
        <span className="text-[13px] font-bold">{t("progress.explore.review.title")}</span>
        <span
          className="rounded-full px-2 py-0.5 text-[11px] font-bold"
          style={
            wroteKb
              ? { background: "var(--infoTint)", color: EXPLORE_HUE }
              : { background: "var(--neutralTint)", color: "var(--neutral)" }
          }
        >
          {wroteKb
            ? t("progress.explore.review.kbEnriched", {
                routes: t("progress.explore.review.routesCount", { count: routes }),
                selectors: t("progress.explore.review.selectorsCount", { count: selectors }),
              })
            : t("progress.explore.review.nothingDiscovered")}
        </span>
        <span className="ml-auto text-[11px] text-faint">{stopReasonLabel(status?.stopReason, t)}</span>
        <ChevronDown
          size={15}
          className="shrink-0 text-muted transition-transform"
          style={{ transform: open ? "rotate(180deg)" : "none" }}
        />
      </button>
      {open && (
        <div className="flex flex-col gap-3 p-3.5">
          {steps.length > 0 && (
            <div className="flex flex-col gap-2">
              {steps.map((s) => (
                <div key={s.step} className="rounded-xl border border-bd3 bg-inset p-2.5">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-[11px] text-faint">{s.step}</span>
                    <span className="text-[12.5px] font-semibold text-txt">{describeExploreStep(s, t)}</span>
                    {s.ok === false && (
                      <span className="rounded-md bg-danger-tint px-1.5 py-0.5 text-[10px] font-bold text-danger">
                        {t("progress.explore.review.noop")}
                      </span>
                    )}
                    {s.observedUrl && (
                      <span className="ml-auto truncate font-mono text-[11px] text-faint">{s.observedUrl}</span>
                    )}
                  </div>
                  {s.reasoning && <p className="m-0 mt-1 text-[11.5px] leading-relaxed text-muted">{s.reasoning}</p>}
                </div>
              ))}
            </div>
          )}
          {wroteKb ? (
            <div className="flex flex-wrap items-center gap-3">
              <RegenerateWithNote
                label={t("progress.explore.review.regenerateWithKb")}
                regenerating={regenerating}
                onRegenerate={onRegenerate}
              />
              <span className="text-[11px] text-muted">
                {t("progress.explore.review.wroteKbHint")}
              </span>
            </div>
          ) : (
            <p className="m-0 text-[11.5px] leading-relaxed text-muted">
              {t("progress.explore.review.noKbHint")}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
