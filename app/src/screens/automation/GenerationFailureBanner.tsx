import { AlertTriangle, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/Button";
import type { GenerationError } from "@/types/api";

/**
 * Why the last generation pass produced nothing (#641).
 *
 * Until this existed, a per-case failure was published over the run WebSocket and
 * nowhere else, and the pass ended by flipping the run to `automation` exactly
 * like a successful one — so a user who was not watching the screen at that
 * second was shown the generic "No automation yet" empty state, which reads as
 * "nothing to generate". The two most common causes are one-click fixable
 * prerequisites ("No local agent paired", "No base URL in the project context"),
 * which made the silence especially expensive.
 *
 * Messages come from the server verbatim: they already name the missing thing,
 * and paraphrasing them here would put a second, drifting copy of that wording in
 * the frontend.
 */
export function GenerationFailureBanner({
  error,
  generating,
  onRetry,
}: {
  error: GenerationError;
  generating: boolean;
  onRetry: () => void;
}) {
  const { t } = useTranslation("pipeline");
  const failed = error.failures.length;
  return (
    <div
      className="mb-3.5 rounded-[18px] border px-4 py-3.5"
      style={{ background: "rgba(244,63,94,.10)", borderColor: "rgba(244,63,94,.30)" }}
    >
      <div className="flex items-start gap-3">
        <AlertTriangle size={18} className="mt-[1px] shrink-0 text-danger-soft" strokeWidth={2.2} />
        <div className="min-w-0 flex-1">
          <div className="text-[13.5px] font-bold text-danger-soft">
            {t("spec.genFailed.title", { failed, attempted: error.attempted })}
          </div>
          <ul className="m-0 mt-2 flex list-none flex-col gap-1.5 p-0">
            {error.failures.map((f) => (
              <li key={f.caseId} className="flex flex-wrap items-baseline gap-2 text-[12.5px]">
                <span className="rounded-md bg-white/[0.07] px-1.5 py-[1px] font-mono text-[11.5px] text-ink-soft">
                  {f.code}
                </span>
                <span className="text-ink-dim">{f.message}</span>
              </li>
            ))}
          </ul>
          <div className="mt-3 flex items-center gap-2.5">
            <Button variant="glass" size="sm" onClick={onRetry} disabled={generating}>
              <RefreshCw size={13} strokeWidth={2.2} /> {t("spec.genFailed.retry")}
            </Button>
            <span className="text-[11.5px] text-faint">
              {t("spec.genFailed.at", { at: new Date(error.at).toLocaleString() })}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
