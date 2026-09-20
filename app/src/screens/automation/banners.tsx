import { AlertTriangle, Telescope } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Pill } from "@/components/ui/badges";
import { PRODUCT_DEFECT_HUE } from "./specStatus";
import { RegenerateWithNote } from "./RegenerateWithNote";

/** Terminal "product defect" banner — fuchsia, distinct from a script failure. */
export function ProductDefectBanner() {
  const { t } = useTranslation("pipeline");
  return (
    <div
      className="flex items-start gap-3 rounded-2xl border border-defect/40 bg-defect/[0.08] p-4"
    >
      <AlertTriangle size={18} strokeWidth={2.2} className="mt-0.5 shrink-0" style={{ color: PRODUCT_DEFECT_HUE }} />
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="text-[13.5px] font-bold text-defect">
            {t("progress.productDefect.title")}
          </span>
          <Pill color={PRODUCT_DEFECT_HUE} bg="var(--defectTint)">
            {t("progress.productDefect.terminal")}
          </Pill>
        </div>
        <p className="m-0 mt-1 text-xs leading-relaxed text-muted">
          {t("progress.productDefect.body")}
        </p>
      </div>
    </div>
  );
}

/**
 * Blocked banner — dashed amber border with the block reason and an unblock path:
 * fix the underlying cause (e.g. project bootstrap), then Regenerate to retry.
 */
export function BlockedBanner({
  reason,
  onRegenerate,
  regenerating,
  onExplore,
  exploring,
}: {
  reason: string;
  onRegenerate: (comment?: string) => void;
  regenerating: boolean;
  /** Kick off a DOM-exploration session to discover the missing routes/selectors
   * (ADR 0010). When omitted, only the Regenerate path is offered. */
  onExplore?: () => void;
  exploring?: boolean;
}) {
  const { t } = useTranslation("pipeline");
  return (
    <div
      className="rounded-2xl border border-dashed border-warn/50 bg-warn/[0.06] p-4"
    >
      <div className="mb-2 flex items-center gap-2">
        <Pill color="var(--warn)" bg="var(--warnTint)">
          {t("progress.blocked.pill")}
        </Pill>
        <span className="text-[13px] font-bold">{t("progress.blocked.title")}</span>
      </div>
      <p className="m-0 mb-3 text-xs leading-relaxed text-txt3">
        {reason || t("progress.blocked.defaultReason")}
      </p>
      <div className="flex flex-wrap items-center gap-3">
        {onExplore && (
          <button
            onClick={onExplore}
            disabled={exploring || regenerating}
            title={t("progress.blocked.exploreTitle")}
            className="flex items-center gap-1.5 rounded-[9px] border border-info/30 bg-info/10 px-[13px] py-1.5 text-[12px] font-semibold text-info hover:bg-info/20 disabled:opacity-60"
          >
            {exploring ? (
              <span
                className="h-[13px] w-[13px] rounded-full border-2"
                style={{ borderColor: "var(--infoTint)", borderTopColor: "var(--info)", animation: "spin .8s linear infinite" }}
              />
            ) : (
              <Telescope size={13} />
            )}
            {exploring ? t("progress.blocked.exploring") : t("progress.blocked.exploreCta")}
          </button>
        )}
        <RegenerateWithNote
          label={t("progress.blocked.regenerateRetry")}
          variant="amber"
          regenerating={regenerating}
          onRegenerate={onRegenerate}
        />
        <span className="text-[11px] text-muted">
          {t("progress.blocked.hint")}
        </span>
      </div>
    </div>
  );
}

/**
 * Non-destructive note shown in the code panel when the last regeneration was
 * rejected by the placeholder gate — the previous good spec was kept, so the code
 * shown is unchanged.
 */
export function GateRejectedNote({ reason }: { reason: string }) {
  const { t } = useTranslation("pipeline");
  return (
    <div
      className="flex items-start gap-2 border-b border-bd3 bg-warn/[0.06] px-4 py-2.5"
    >
      <AlertTriangle size={13} className="mt-[1px] shrink-0 text-warn" />
      <span className="text-[11.5px] leading-relaxed text-warn">
        {t("progress.gateRejected.text")}{reason ? ` — ${reason}` : ""}
      </span>
    </div>
  );
}
