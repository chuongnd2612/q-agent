import { ChevronDown, Wand2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import type { HealAttempt, HealReport } from "@/types/api";

/** Relative "time ago" from an ISO timestamp, for the heal report header. `t` is
 * the `pipeline` namespace translator supplied by the calling component. */
export function healTimeAgo(iso: string, t: TFunction): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const s = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (s < 60) return t("progress.heal.time.justNow");
  const m = Math.round(s / 60);
  if (m < 60) return t("progress.heal.time.minutes", { count: m });
  const h = Math.round(m / 60);
  if (h < 24) return t("progress.heal.time.hours", { count: h });
  return t("progress.heal.time.days", { count: Math.round(h / 24) });
}

/** Renders a unified-diff string with +/-/@@ lines colored. */
export function DiffBlock({ diff }: { diff: string }) {
  return (
    <div className="mt-2 overflow-x-auto rounded-lg border border-bd bg-[var(--code)] p-2.5">
      <pre className="m-0 font-mono text-[11.5px] leading-[1.6]">
        {diff.split("\n").map((line, i) => {
          const c = line.startsWith("+")
            ? "var(--ok)"
            : line.startsWith("-")
              ? "var(--danger)"
              : line.startsWith("@@")
                ? "var(--cyanSoft)"
                : "var(--muted)";
          return (
            <div key={i} style={{ color: c, whiteSpace: "pre" }}>
              {line || " "}
            </div>
          );
        })}
      </pre>
    </div>
  );
}

/** Collapsible "Self-heal timeline" — the per-attempt failure, what Claude
 * changed (diff), and the final outcome of the last heal for a spec. */
export function HealTimeline({ report }: { report: HealReport }) {
  const { t } = useTranslation("pipeline");
  const [open, setOpen] = useState(true);
  const healed = report.finalStatus === "pass";
  const n = report.attempts.length;
  return (
    <div className="overflow-hidden rounded-2xl border border-bd2" style={{ background: "var(--code)" }}>
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2.5 border-b border-bd3 px-4 py-3 text-left hover:bg-inset"
      >
        <Wand2 size={14} className="shrink-0 text-ok" />
        <span className="text-[13px] font-bold">{t("progress.heal.timeline.title")}</span>
        <span
          className="rounded-full px-2 py-0.5 text-[11px] font-bold"
          style={
            healed
              ? { background: "var(--okTint)", color: "var(--ok)" }
              : { background: "var(--dangerTint)", color: "var(--danger)" }
          }
        >
          {healed
            ? t("progress.heal.timeline.healedAfter", { count: n })
            : t("progress.heal.timeline.stillFailing", { count: n })}
        </span>
        <span className="ml-auto text-[11px] text-faint">{healTimeAgo(report.healedAt, t)}</span>
        <ChevronDown
          size={15}
          className="shrink-0 text-muted transition-transform"
          style={{ transform: open ? "rotate(180deg)" : "none" }}
        />
      </button>
      {open && (
        <div className="flex flex-col gap-2.5 p-3.5">
          {report.attempts.map((a: HealAttempt) => (
            <div key={a.attempt} className="rounded-xl border border-bd3 bg-inset p-3">
              <div className="flex items-center gap-2">
                <span className="text-[12.5px] font-bold">{t("progress.heal.timeline.attempt", { n: a.attempt })}</span>
                <span
                  className="rounded-md px-1.5 py-0.5 text-[10px] font-bold"
                  style={
                    a.status === "pass"
                      ? { background: "var(--okTint)", color: "var(--ok)" }
                      : { background: "var(--dangerTint)", color: "var(--danger)" }
                  }
                >
                  {a.status === "pass" ? t("progress.heal.timeline.passed") : t("progress.heal.timeline.failed")}
                </span>
                <span className="ml-auto font-mono text-[11px] text-faint">
                  {(a.durationMs / 1000).toFixed(1)}s
                </span>
              </div>
              {a.error && (
                <div className="mt-2 max-h-40 overflow-auto rounded-lg border border-bd3 bg-[var(--code)] p-2.5">
                  <pre className="m-0 whitespace-pre-wrap break-words font-mono text-[11.5px] leading-[1.55] text-[var(--danger)]">
                    {a.error}
                  </pre>
                </div>
              )}
              {a.fixed && (
                <>
                  <div className="mt-2 flex items-center gap-1.5 text-[11.5px] font-semibold text-ok">
                    <Wand2 size={12} /> {t("progress.heal.timeline.claudeRewrote")}
                  </div>
                  {a.diff && <DiffBlock diff={a.diff} />}
                </>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
