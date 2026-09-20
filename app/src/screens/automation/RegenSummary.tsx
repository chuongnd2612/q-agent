import { MessageSquarePlus, RotateCcw, Undo2 } from "lucide-react";
import { useTranslation } from "react-i18next";

/**
 * Derive short tags describing what a regeneration changed, from a heuristic over
 * the added and removed lines of the diff. Returns `pipeline` namespace
 * translation keys (the consuming {@link RegenSummary} maps each to `t()` for
 * display) so this plain module stays translation-free.
 *
 * Ordered so the most salient improvements surface first; each tag is emitted at
 * most once. Returns an empty array when nothing matches (the summary then omits
 * the "Applied:" line).
 *
 * @param added Added/changed lines in the new spec.
 * @param removed Lines dropped from the previous spec.
 */
export function deriveTags(added: string[], removed: string[]): string[] {
  const tags: string[] = [];
  const push = (t: string) => {
    if (!tags.includes(t)) tags.push(t);
  };
  const addedText = added.join("\n");
  const removedText = removed.join("\n");

  if (/getByTestId|data-testid/.test(addedText)) push("progress.regen.tags.dataTestId");
  if (/waitForLoadState\(\s*['"]networkidle['"]\s*\)/.test(addedText)) push("progress.regen.tags.networkIdle");
  if (/waitForTimeout\(/.test(removedText)) push("progress.regen.tags.removedHardWaits");
  if (/getByRole|getByLabel|getByText/.test(addedText)) push("progress.regen.tags.roleLabel");
  if (/toBeVisible|expect\(/.test(addedText)) push("progress.regen.tags.assertions");

  return tags;
}

/**
 * Green summary banner shown above the code panel right after a regeneration
 * changed the spec. States the new version + how many lines changed, optionally
 * the heuristic tags, and offers Feedback (reopen the note composer to regenerate
 * again with guidance) and Revert (restore the previous spec).
 *
 * @param version The new ephemeral per-case version number.
 * @param count Number of lines added/changed in the new spec.
 * @param tags Heuristic tags from {@link deriveTags}; the "Applied:" line is
 *   omitted when empty.
 * @param reverting Whether the revert PATCH is in flight (disables Revert).
 * @param onFeedback Reopen the reviewer-note composer.
 * @param onRevert Restore the previous spec code.
 */
export function RegenSummary({
  version,
  count,
  tags,
  reverting,
  blocked = false,
  onFeedback,
  onRevert,
}: {
  version: number;
  count: number;
  tags: string[];
  reverting: boolean;
  /** The regenerated spec still failed the quality gate — tone the banner amber
   * and say so, rather than implying a clean success. */
  blocked?: boolean;
  onFeedback: () => void;
  onRevert: () => void;
}) {
  const { t } = useTranslation("pipeline");
  return (
    <div
      className="flex flex-col gap-3 rounded-2xl border p-4 md:flex-row md:items-center"
      style={{
        borderColor: blocked ? "var(--warnTint)" : "var(--okTint)",
        background: blocked ? "var(--warnTint)" : "var(--okTint)",
      }}
    >
      <div className="min-w-0 md:flex-1">
        <div
          className="flex items-center gap-2 text-[13.5px] font-bold"
          style={{ color: blocked ? "var(--warn)" : "var(--ok)" }}
        >
          <RotateCcw size={14} strokeWidth={2.4} className="shrink-0" />
          {t("progress.regen.summary.title", { version })}
          {blocked ? t("progress.regen.summary.stillBlocked") : ""}
          {t("progress.regen.summary.linesChanged", { count })}
        </div>
        {tags.length > 0 && (
          <div className="mt-1 text-xs leading-relaxed text-muted">
            {t("progress.regen.summary.applied", { tags: tags.map((k) => t(k)).join(", ") })}
          </div>
        )}
      </div>
      <div className="flex items-center gap-1.5">
        <button
          onClick={onFeedback}
          className="flex items-center gap-1.5 rounded-[9px] border border-bd2 bg-card2 px-[11px] py-1.5 text-[11.5px] font-semibold text-txt3 hover:bg-card3"
        >
          <MessageSquarePlus size={13} />
          {t("progress.regen.summary.feedback")}
        </button>
        <button
          onClick={onRevert}
          disabled={reverting}
          className="flex items-center gap-1.5 rounded-[9px] border border-bd2 bg-card2 px-[11px] py-1.5 text-[11.5px] font-semibold text-txt3 hover:bg-card3 disabled:opacity-60"
        >
          {reverting ? (
            <span
              className="h-[13px] w-[13px] rounded-full border-2"
              style={{ borderColor: "var(--neutralTint)", borderTopColor: "var(--muted)", animation: "spin .8s linear infinite" }}
            />
          ) : (
            <Undo2 size={13} />
          )}
          {t("progress.regen.summary.revert")}
        </button>
      </div>
    </div>
  );
}
