import { FileCode, Sparkles } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/Button";

/**
 * "No automation yet" empty state. Offers generation when there are approved,
 * automatable cases; otherwise explains how to get some.
 *
 * @param automatableCount Number of approved, non-Manual cases ready to automate.
 * @param generating Whether generation is currently in flight (disables the button).
 * @param onGenerate Kicks off incremental generation.
 * @param failed The last pass failed and a banner above already says why (#641),
 *   so this must not also claim there is nothing to automate — the two together
 *   read as a contradiction, and "nothing approved" is the wrong half.
 */
export function NoAutomationEmptyState({
  automatableCount,
  generating,
  onGenerate,
  failed = false,
}: {
  automatableCount: number;
  generating: boolean;
  onGenerate: () => void;
  failed?: boolean;
}) {
  const { t } = useTranslation("pipeline");
  return (
    <div className="glass flex flex-col items-center rounded-[22px] px-5 py-10 text-center md:px-8 md:py-14">
      <div
        className="mb-5 flex h-[70px] w-[70px] items-center justify-center rounded-[22px]"
        style={{ background: "linear-gradient(135deg,rgba(139,92,246,.24),rgba(99,102,241,.12))" }}
      >
        <FileCode size={30} color="#a78bfa" strokeWidth={1.9} />
      </div>
      <h2 className="m-0 mb-2 text-xl font-extrabold">{t("spec.empty.title")}</h2>
      {automatableCount > 0 ? (
        <>
          <p className="m-0 mb-[22px] max-w-[420px] text-[13.5px] leading-relaxed text-ink-dim">
            {automatableCount === 1
              ? t("spec.empty.readyOne", { count: automatableCount })
              : t("spec.empty.readyOther", { count: automatableCount })}
          </p>
          <Button variant="primary" size="lg" onClick={onGenerate} disabled={generating} className="w-full md:w-auto">
            <Sparkles size={16} strokeWidth={2.2} /> {t("spec.empty.generate")}
          </Button>
        </>
      ) : failed ? null : (
        <p className="m-0 max-w-[420px] text-[13.5px] leading-relaxed text-ink-dim">
          {t("spec.empty.none")}
        </p>
      )}
    </div>
  );
}
