import { ChevronsDownUp, ChevronsUpDown, Copy, Download, FileCode2, Pencil, Play, Save, Sparkles, Telescope, Wand2, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import { OverflowMenu } from "@/components/ui/OverflowMenu";
import type { AutomationSpecOut } from "@/types/api";
import { Pill } from "@/components/ui/badges";
import { GateRejectedNote } from "./banners";
import { CodeHighlight, type FoldRange } from "./CodeViewer";
import { AuthoringTrail } from "./ProgressBanners";
import { RegenerateWithNote } from "./RegenerateWithNote";
import { specDisplayPath } from "./projectFiles";

/**
 * The right-hand code panel for the selected spec: header toolbar (Save/Cancel
 * while editing, or Collapse/Expand/Edit/Regenerate/Run/Self-heal/Copy/Download),
 * the placeholder-gate note, the editing textarea vs the read-only highlighted
 * view, and the footer "Run tests" bar. Pure presentation — every action is a
 * callback prop owned by the parent screen.
 */
export function SpecCodePanel({
  selectedSpec,
  editing,
  draft,
  setDraft,
  foldRanges,
  folded,
  toggleFold,
  collapseAll,
  expandAll,
  generating,
  specRegenerating,
  healingThisCase,
  exploringThisCase,
  runningThisSpec,
  runSuppressed,
  isBlocked,
  isProductDefect,
  gateRejected,
  gateReport,
  authoringActive,
  authoringLines,
  authoringDone,
  updateSpecPending,
  startExecutionPending,
  copyLabel,
  changedLines,
  regenVersion,
  feedbackSignal,
  onCopy,
  onDownload,
  onStartEdit,
  onCancelEdit,
  onSaveEdit,
  onRegenerate,
  onRunSpec,
  onStartHeal,
  onStartExplore,
  onStartExecution,
  onOpenChat,
  codeOverride,
  scrollToLine,
  scrollSignal,
}: {
  selectedSpec: AutomationSpecOut | null;
  editing: boolean;
  draft: string;
  setDraft: (value: string) => void;
  foldRanges: FoldRange[];
  folded: Set<number>;
  toggleFold: (start: number) => void;
  collapseAll: () => void;
  expandAll: () => void;
  generating: boolean;
  specRegenerating: boolean;
  healingThisCase: boolean;
  exploringThisCase: boolean;
  runningThisSpec: boolean;
  runSuppressed: boolean;
  isBlocked: boolean;
  isProductDefect: boolean;
  gateRejected: boolean;
  gateReport: { outcome?: string; reason?: string } | null;
  /** While true, the spec is being authored live — show the streamed trail in
   * place of the (empty) code editor and suppress the code actions (#400). */
  authoringActive: boolean;
  authoringLines: string[];
  authoringDone: boolean;
  updateSpecPending: boolean;
  startExecutionPending: boolean;
  copyLabel: string;
  changedLines?: Set<number>;
  regenVersion?: number;
  feedbackSignal?: number;
  onCopy: () => void;
  onDownload: () => void;
  onStartEdit: () => void;
  onCancelEdit: () => void;
  onSaveEdit: () => void;
  onRegenerate: (comment?: string) => void;
  onRunSpec: () => void;
  onStartHeal: () => void;
  /** Kick off a DOM-exploration session (only meaningful for a blocked spec). */
  onStartExplore: () => void;
  onStartExecution: () => void;
  onOpenChat: () => void;
  /** When set, shown in the code viewer instead of the spec's code — used to
   * "type out" a chat edit's new code before the query-backed code settles. */
  codeOverride?: string;
  /** 0-based line to scroll into view when a chat edit is applied (its first
   * changed line). Paired with `scrollSignal`. */
  scrollToLine?: number;
  /** Bumps per applied chat edit so the viewer re-scrolls even when the first
   * changed line index repeats across edits. */
  scrollSignal?: number;
}) {
  const { t } = useTranslation("pipeline");
  return (
    <div
      className={`overflow-hidden rounded-2xl border ${
        isBlocked ? "border-dashed border-white/20" : "border-white/[0.09]"
      }`}
      style={{ background: "rgba(8,8,13,.8)", backdropFilter: "blur(22px)" }}
    >
      <div className="flex flex-wrap items-center gap-2.5 border-b border-white/[0.06] px-4 py-3">
        <span className="font-mono text-[12.5px] text-ink-soft">{specDisplayPath(selectedSpec?.filename)}</span>
        <span className="rounded-md px-2 py-0.5 text-[10px] font-bold" style={{ background: "rgba(34,211,238,.13)", color: "#67e8f9" }}>
          TypeScript
        </span>
        <div className="flex w-full flex-wrap items-center gap-1.5 md:ml-auto md:w-auto">
          {authoringActive ? (
            <span className="flex items-center gap-1.5 rounded-full bg-violet-400/15 px-2.5 py-1 text-[11px] font-semibold text-violet-300">
              <span
                className="h-[11px] w-[11px] rounded-full border-2"
                style={{ borderColor: "rgba(167,139,250,.35)", borderTopColor: "#a78bfa", animation: "spin .8s linear infinite" }}
              />
              authoring…
            </span>
          ) : editing ? (
            <>
              <button
                onClick={onSaveEdit}
                disabled={updateSpecPending}
                className="flex items-center gap-1.5 rounded-[9px] border border-violet/40 bg-violet/20 px-[11px] py-1.5 text-[11.5px] font-semibold text-violet hover:bg-violet/30 disabled:opacity-60"
              >
                <Save size={13} />
                {updateSpecPending ? t("spec.saving") : t("spec.save")}
              </button>
              <button
                onClick={onCancelEdit}
                disabled={updateSpecPending}
                className="flex items-center gap-1.5 rounded-[9px] border border-white/[0.09] bg-white/5 px-[11px] py-1.5 text-[11.5px] font-semibold text-ink-soft hover:bg-white/10 disabled:opacity-60"
              >
                <X size={13} />
                {t("spec.cancel")}
              </button>
            </>
          ) : (
            <>
              <RegenerateWithNote
                label={t("spec.regenerate")}
                regenerating={specRegenerating}
                disabled={isProductDefect}
                onRegenerate={onRegenerate}
                openSignal={feedbackSignal}
              />
              {regenVersion != null && (
                <Pill color="#a78bfa" bg="rgba(167,139,250,.14)">
                  v{regenVersion}
                </Pill>
              )}
              <button
                onClick={onRunSpec}
                disabled={generating || specRegenerating || healingThisCase || runningThisSpec || runSuppressed}
                title={
                  isBlocked
                    ? t("spec.run.titleBlocked")
                    : isProductDefect
                      ? t("spec.run.titleProductDefect")
                      : t("spec.run.title")
                }
                className="flex items-center gap-1.5 rounded-[9px] border border-cyan-400/25 bg-cyan-400/10 px-[11px] py-1.5 text-[11.5px] font-semibold text-cyan-300 hover:bg-cyan-400/20 disabled:opacity-60"
              >
                {runningThisSpec ? (
                  <span
                    className="h-[13px] w-[13px] rounded-full border-2"
                    style={{ borderColor: "rgba(34,211,238,.35)", borderTopColor: "#22d3ee", animation: "spin .8s linear infinite" }}
                  />
                ) : (
                  <Play size={13} fill="currentColor" />
                )}
                {runningThisSpec ? t("spec.run.running") : t("spec.run.label")}
              </button>
              <button
                onClick={onStartHeal}
                disabled={generating || specRegenerating || healingThisCase || runSuppressed}
                title={
                  isBlocked
                    ? t("spec.heal.titleBlocked")
                    : isProductDefect
                      ? t("spec.heal.titleProductDefect")
                      : t("spec.heal.title")
                }
                className="flex items-center gap-1.5 rounded-[9px] border border-emerald-400/25 bg-emerald-400/10 px-[11px] py-1.5 text-[11.5px] font-semibold text-emerald-300 hover:bg-emerald-400/20 disabled:opacity-60"
              >
                {healingThisCase ? (
                  <span
                    className="h-[13px] w-[13px] rounded-full border-2"
                    style={{ borderColor: "rgba(52,211,153,.35)", borderTopColor: "#34d399", animation: "spin .8s linear infinite" }}
                  />
                ) : (
                  <Wand2 size={13} />
                )}
                {healingThisCase ? t("spec.heal.healing") : t("spec.heal.label")}
              </button>
              {isBlocked && (
                <button
                  onClick={onStartExplore}
                  disabled={generating || specRegenerating || healingThisCase || exploringThisCase}
                  title={t("spec.explore.title")}
                  className="flex items-center gap-1.5 rounded-[9px] border border-sky-400/25 bg-sky-400/10 px-[11px] py-1.5 text-[11.5px] font-semibold text-sky-300 hover:bg-sky-400/20 disabled:opacity-60"
                >
                  {exploringThisCase ? (
                    <span
                      className="h-[13px] w-[13px] rounded-full border-2"
                      style={{ borderColor: "rgba(56,189,248,.35)", borderTopColor: "#38bdf8", animation: "spin .8s linear infinite" }}
                    />
                  ) : (
                    <Telescope size={13} />
                  )}
                  {exploringThisCase ? t("spec.explore.exploring") : t("spec.explore.label")}
                </button>
              )}
              <button
                onClick={onOpenChat}
                disabled={generating || specRegenerating}
                title={t("spec.chat.title")}
                className="flex items-center gap-1.5 rounded-[9px] border border-violet-400/25 bg-violet-400/10 px-[11px] py-1.5 text-[11.5px] font-semibold text-violet-300 hover:bg-violet-400/20 disabled:opacity-60"
              >
                <Sparkles size={13} /> {t("spec.chat.label")}
              </button>
              <OverflowMenu
                items={[
                  {
                    key: "collapse",
                    label: t("spec.menu.collapseAll"),
                    icon: <ChevronsDownUp size={14} />,
                    onClick: collapseAll,
                    disabled: foldRanges.length === 0,
                  },
                  {
                    key: "expand",
                    label: t("spec.menu.expandAll"),
                    icon: <ChevronsUpDown size={14} />,
                    onClick: expandAll,
                    disabled: folded.size === 0,
                  },
                  {
                    key: "edit",
                    label: t("spec.menu.editManually"),
                    icon: <Pencil size={14} />,
                    onClick: onStartEdit,
                    disabled: generating || specRegenerating,
                  },
                  {
                    key: "copy",
                    label: copyLabel === "Copy" ? t("spec.menu.copy") : copyLabel,
                    icon: <Copy size={14} />,
                    onClick: onCopy,
                    disabled: specRegenerating,
                  },
                  {
                    key: "download",
                    label: t("spec.menu.download"),
                    icon: <Download size={14} />,
                    onClick: onDownload,
                    disabled: specRegenerating,
                  },
                ]}
              />
            </>
          )}
        </div>
      </div>
      {gateRejected && <GateRejectedNote reason={gateReport?.reason ?? ""} />}
      {editing ? (
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          spellCheck={false}
          wrap="off"
          className="block w-full resize-y overflow-auto whitespace-pre px-4 py-[18px] font-mono text-[12.5px] leading-[1.75] text-ink outline-none"
          style={{ minHeight: 380, background: "rgba(8,8,13,.6)", tabSize: 2 }}
        />
      ) : authoringActive ? (
        <div className="px-4 py-[18px]" style={{ minHeight: 380, background: "rgba(8,8,13,.6)" }}>
          <AuthoringTrail lines={authoringLines} done={authoringDone} />
        </div>
      ) : selectedSpec && !(selectedSpec.code ?? "").trim() ? (
        // Spec row exists but has no code yet (not generated) — show a friendly
        // empty state instead of a blank one-line editor.
        <div
          className="flex flex-col items-center justify-center gap-2 px-4 py-16 text-center"
          style={{ minHeight: 380, background: "rgba(8,8,13,.6)" }}
        >
          <FileCode2 size={30} className="text-faint" />
          <div className="text-[13.5px] font-semibold text-ink-soft">{t("spec.notGenerated.title")}</div>
          <div className="max-w-sm text-xs text-muted">{t("spec.notGenerated.hint")}</div>
        </div>
      ) : selectedSpec ? (
        <div className="relative">
          <div
            style={{
              opacity: generating || specRegenerating ? 0.4 : 1,
              transition: "opacity .2s ease",
            }}
          >
            <CodeHighlight
              code={codeOverride ?? selectedSpec.code}
              foldRanges={foldRanges}
              folded={folded}
              onToggle={toggleFold}
              changedLines={changedLines}
              scrollToLine={scrollToLine}
              scrollSignal={scrollSignal}
            />
          </div>
          {(generating || specRegenerating) && (
            <div className="pointer-events-none absolute inset-0 flex items-start justify-center pt-8">
              <div className="flex items-center gap-2 rounded-full border border-white/10 bg-black/70 px-3.5 py-1.5 text-[11.5px] font-semibold text-ink-soft backdrop-blur">
                <span
                  className="h-[13px] w-[13px] rounded-full border-2"
                  style={{ borderColor: "rgba(167,139,250,.35)", borderTopColor: "#a78bfa", animation: "spin .8s linear infinite" }}
                />
                {specRegenerating ? t("spec.overlay.regenerating") : t("spec.overlay.updating")}
              </div>
            </div>
          )}
        </div>
      ) : null}
      <div className="flex flex-col gap-2.5 border-t border-white/[0.06] px-4 py-3.5 md:flex-row md:items-center">
        <span className="text-xs text-muted md:flex-1">{t("spec.footer.description")}</span>
        <button
          onClick={onStartExecution}
          disabled={startExecutionPending}
          className="flex w-full items-center justify-center gap-2 rounded-xl px-[18px] py-2.5 text-[13px] font-bold text-white disabled:opacity-60 md:w-auto"
          style={{ background: "linear-gradient(135deg,#8b5cf6,#6366f1)", boxShadow: "0 8px 22px -8px rgba(139,92,246,.8)" }}
        >
          <Play size={14} fill="#fff" />
          {t("spec.runTests")}
        </button>
      </div>
    </div>
  );
}
