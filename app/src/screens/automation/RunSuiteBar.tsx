import { Play } from "lucide-react";
import { useTranslation } from "react-i18next";

/**
 * The suite-wide "Run tests" bar: executes every runnable approved spec in the
 * Run, in parallel, then lands on the Execution screen.
 *
 * It used to be the footer of `SpecCodePanel`, which put it below the whole code
 * viewer (unreachable without scrolling a long spec) and made it vanish entirely
 * while a project file was open, since that branch renders `ProjectFilePanel`
 * instead (#701). It is a page-level action, so it lives at page level, above the
 * spec column — and deliberately keeps its explanatory sentence, which is what
 * distinguishes it from the per-spec `Run` in the panel's toolbar.
 */
export function RunSuiteBar({
  pending,
  runnable,
  onRun,
}: {
  pending: boolean;
  /** Whether the run currently has anything the server would execute. When
   * false the button is disabled with a reason rather than 400-ing on click. */
  runnable: boolean;
  onRun: () => void;
}) {
  const { t } = useTranslation("pipeline");
  return (
    <div
      className="mb-3.5 flex flex-col gap-2.5 rounded-2xl border border-white/[0.09] px-4 py-3.5 md:flex-row md:items-center"
      // Opaque, like ExportProjectPanel above it: this sits over the shell's
      // animated background and carries a line of explanatory text.
      style={{ background: "rgba(8,8,13,.92)" }}
    >
      <span className="text-xs leading-relaxed text-muted md:flex-1">
        {runnable ? t("automation.runSuite.description") : t("automation.runSuite.noneRunnable")}
      </span>
      <button
        onClick={onRun}
        disabled={pending || !runnable}
        title={runnable ? t("automation.runSuite.description") : t("automation.runSuite.noneRunnable")}
        className="flex w-full items-center justify-center gap-2 rounded-xl px-[18px] py-2.5 text-[13px] font-bold text-white disabled:opacity-60 md:w-auto md:shrink-0"
        style={{ background: "linear-gradient(135deg,#8b5cf6,#6366f1)", boxShadow: "0 8px 22px -8px rgba(139,92,246,.8)" }}
      >
        <Play size={14} fill="#fff" />
        {t("automation.runSuite.label")}
      </button>
    </div>
  );
}
