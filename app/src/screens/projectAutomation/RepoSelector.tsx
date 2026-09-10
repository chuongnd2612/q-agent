import { useTranslation } from "react-i18next";
import { cn } from "@/lib/cn";
import { DropdownShell } from "@/components/ui/Dropdown";
import type { AutomationRepoOut } from "@/types/api";

/** Above this many repos the segmented control stops being scannable and becomes
 *  a horizontal-scroll hazard, so the control switches to a dropdown. */
const SEGMENTED_MAX = 3;

/**
 * Which automation repo the project Automation tab is browsing (#770).
 *
 * Three renderings, because "how many repos" changes what the control *is*:
 *
 * * **One repo — no control at all.** The overwhelmingly common case. A picker
 *   with a single option is chrome that asks a question with one answer; the repo
 *   header already names it, so the selector adds nothing and costs a row.
 * * **2–3 — a segmented control.** Every option visible, one click to switch, and
 *   comparing them (spec counts side by side) is the actual task.
 * * **4+ — a dropdown.** Reuses {@link DropdownShell}, which already portals its
 *   panel to `document.body` with fixed positioning anchored to the trigger rect
 *   and paints an **opaque** `rgb(24,24,32)` surface — both required here per
 *   CLAUDE.md, since this tab renders inside the project layout's
 *   `AnimatePresence`/`motion` wrapper, which is a transform stacking context that
 *   would otherwise trap the panel's `z-index`. Re-implementing the float would
 *   have re-acquired exactly that bug.
 *
 * Purely presentational: the selection lives in the URL (`?repo=`), owned by
 * `ProjectAutomationTab`. Nothing here touches Zustand.
 */
export function RepoSelector({
  repos,
  selectedId,
  onSelect,
}: {
  repos: AutomationRepoOut[];
  /** The resolved repo — never `null` when more than one repo exists. */
  selectedId: number | null;
  onSelect: (projectId: number) => void;
}) {
  const { t } = useTranslation("projects");

  // One repo (or none) needs no control — see the doc comment.
  if (repos.length <= 1) return null;

  const selected = repos.find((r) => r.id === selectedId) ?? null;
  const labelOf = (r: AutomationRepoOut) => r.repoLabel || r.repo || t("automation.defaultRepo");
  const countOf = (r: AutomationRepoOut) =>
    t("automation.repoSpecCount", { count: r.specCount });

  if (repos.length <= SEGMENTED_MAX) {
    return (
      <div
        className="flex items-center gap-0.5 rounded-[10px] border border-white/[0.08] bg-white/[0.04] p-0.5"
        role="group"
        aria-label={t("automation.repoSelectorLabel")}
        data-testid="repo-selector-segmented"
      >
        {repos.map((r) => {
          const active = r.id === selectedId;
          return (
            <button
              key={r.id}
              type="button"
              onClick={() => onSelect(r.id)}
              aria-pressed={active}
              title={`${labelOf(r)} · ${countOf(r)}`}
              className={cn(
                "flex items-center gap-1.5 rounded-[8px] px-2.5 py-1.5 text-[11.5px] font-semibold transition-colors",
                active ? "text-white" : "text-ink-dim hover:text-white",
              )}
              style={
                active
                  ? { background: "linear-gradient(135deg,rgba(139,92,246,.9),rgba(99,102,241,.75))" }
                  : undefined
              }
            >
              <span className="max-w-[160px] truncate font-mono">{labelOf(r)}</span>
              <span className={active ? "text-white/70" : "text-faint"}>{r.specCount}</span>
            </button>
          );
        })}
      </div>
    );
  }

  return (
    <div data-testid="repo-selector-dropdown">
      <DropdownShell
        active={!!selected}
        minWidth={260}
        label={
          <span className="font-mono">
            {selected ? labelOf(selected) : t("automation.repoSelectorLabel")}
          </span>
        }
      >
        {(close) => (
          <>
            {repos.map((r) => {
              const active = r.id === selectedId;
              return (
                <button
                  key={r.id}
                  type="button"
                  onClick={() => {
                    onSelect(r.id);
                    close();
                  }}
                  data-on={active}
                  className="flex w-full cursor-pointer items-center gap-2.5 rounded-[10px] px-2.5 py-2 text-left text-[13px] hover:bg-white/[0.06] data-[on=true]:bg-[rgba(139,92,246,.16)]"
                >
                  <span className="min-w-0 flex-1 truncate font-mono">{labelOf(r)}</span>
                  <span className="shrink-0 text-[11px] text-ink-dim">{countOf(r)}</span>
                </button>
              );
            })}
          </>
        )}
      </DropdownShell>
    </div>
  );
}
