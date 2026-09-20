import { RotateCcw } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

/**
 * Regenerate control with an on-demand reviewer note.
 *
 * Renders a trigger button (the code-panel "Regenerate" or the amber blocked-banner
 * "Regenerate to retry", chosen by `variant`). Clicking it reveals an inline
 * `<textarea>` for a free-text note plus Cancel / Regenerate. Confirming calls
 * `onRegenerate(note.trim() || undefined)` — an empty note behaves like today's
 * plain regenerate — then collapses and clears the field.
 *
 * @param label Trigger button text (e.g. "Regenerate", "Regenerate to retry").
 * @param regenerating Whether a regeneration is in flight (spinner + disabled state).
 * @param disabled Force-disable the trigger (e.g. terminal product defect).
 * @param variant Visual style: "default" (violet-neutral panel) or "amber" (banner).
 * @param onRegenerate Called with the trimmed note (or undefined when empty).
 * @param openSignal When this number changes, force-open the composer (used by the
 *   RegenSummary "Feedback" button so the reviewer can regenerate again with guidance).
 */
export function RegenerateWithNote({
  label,
  regenerating,
  disabled = false,
  variant = "default",
  onRegenerate,
  openSignal,
}: {
  label: string;
  regenerating: boolean;
  disabled?: boolean;
  variant?: "default" | "amber";
  onRegenerate: (comment?: string) => void;
  openSignal?: number;
}) {
  const { t } = useTranslation("pipeline");
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Force-open when the parent bumps openSignal (Feedback button). Compare against
  // the previous value so it re-opens on every bump, never on mount.
  const prevSignal = useRef(openSignal);
  useEffect(() => {
    if (openSignal !== undefined && openSignal !== prevSignal.current) {
      prevSignal.current = openSignal;
      setOpen(true);
    }
  }, [openSignal]);

  // Focus the note field whenever the composer opens.
  useEffect(() => {
    if (open) textareaRef.current?.focus();
  }, [open]);

  const confirm = () => {
    onRegenerate(note.trim() || undefined);
    setOpen(false);
    setNote("");
  };

  const cancel = () => {
    setOpen(false);
    setNote("");
  };

  const spinnerStyle =
    variant === "amber"
      ? { borderColor: "var(--warnTint)", borderTopColor: "var(--warn)" }
      : { borderColor: "var(--pb)", borderTopColor: "var(--psText)" };

  const triggerClass =
    variant === "amber"
      ? "flex items-center gap-1.5 rounded-[9px] border border-warn/40 bg-warn/15 px-[11px] py-1.5 text-[11.5px] font-semibold text-warn hover:bg-warn/25 disabled:opacity-60"
      : "flex items-center gap-1.5 rounded-[9px] border border-bd2 bg-card2 px-[11px] py-1.5 text-[11.5px] font-semibold text-txt3 hover:bg-card3 disabled:opacity-60";

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        disabled={regenerating || disabled}
        title={t("progress.regenNote.triggerTitle")}
        className={triggerClass}
      >
        {regenerating ? (
          <span
            className="h-[13px] w-[13px] rounded-full border-2"
            style={{ ...spinnerStyle, animation: "spin .8s linear infinite" }}
          />
        ) : (
          <RotateCcw size={13} />
        )}
        {regenerating ? t("progress.regenNote.regenerating") : label}
      </button>
    );
  }

  const confirmClass =
    variant === "amber"
      ? "rounded-[9px] border border-warn/40 bg-warn/15 px-[11px] py-1.5 text-[11.5px] font-semibold text-warn hover:bg-warn/25 disabled:opacity-60"
      : "rounded-[9px] border border-pb bg-pt px-[11px] py-1.5 text-[11.5px] font-semibold text-ps-text-text hover:bg-pb disabled:opacity-60";

  return (
    <div className="flex w-full flex-col gap-2">
      <textarea
        ref={textareaRef}
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder={t("progress.regenNote.placeholder")}
        spellCheck={false}
        className="block w-full resize-y rounded-[9px] border border-bd2 px-3 py-2 font-mono text-[12px] leading-[1.6] text-txt outline-none placeholder:text-faint"
        style={{ minHeight: 70, background: "var(--code)" }}
      />
      <div className="flex items-center gap-1.5">
        <button
          onClick={confirm}
          disabled={regenerating}
          className={`flex items-center gap-1.5 ${confirmClass}`}
        >
          {regenerating ? (
            <span
              className="h-[13px] w-[13px] rounded-full border-2"
              style={{ ...spinnerStyle, animation: "spin .8s linear infinite" }}
            />
          ) : (
            <RotateCcw size={13} />
          )}
          {regenerating ? t("progress.regenNote.regenerating") : label}
        </button>
        <button
          onClick={cancel}
          disabled={regenerating}
          className="rounded-[9px] border border-bd2 bg-card2 px-[11px] py-1.5 text-[11.5px] font-semibold text-txt3 hover:bg-card3 disabled:opacity-60"
        >
          {t("progress.regenNote.cancel")}
        </button>
      </div>
    </div>
  );
}
