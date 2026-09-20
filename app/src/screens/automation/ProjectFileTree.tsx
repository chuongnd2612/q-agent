import { FileCode, Lock } from "lucide-react";
import { useTranslation } from "react-i18next";
import { GlassCard } from "@/components/ui/GlassCard";
import { CollapsibleSection } from "@/components/settings/CollapsibleSection";
import { PathTooltip } from "./PathTooltip";
import type { ProjectFileMeta } from "@/types/api";
import {
  baseName,
  groupSpecsByTicket,
  kindLabelKey,
  type ProjectFileGroup,
} from "./projectFiles";

/**
 * The automation project's file list, shown beside the spec editor (#543).
 *
 * Files are grouped by `kind` so the layering is legible at a glance (#537 doc
 * §20's ownership model), and **specs are sub-grouped by their ticket** (#659):
 * their paths are `tests/<TICKET>/…`, but the list rendered only basenames, so
 * specs from several tickets read as one undifferentiated pile and the ticket was
 * visible on hover alone. Exactly one row — `specPath` — is the **editable** spec
 * and selecting it returns to the normal editor; every other row opens read-only
 * and is marked with a lock, since editing support files must route through the
 * quality gate rather than straight to disk.
 *
 * Groups reuse `CollapsibleSection`, which is collapsed by default since #536 but
 * honours `defaultOpen` after mount — so the spec's group and the group holding
 * the current selection are opened, and the rest stay a scannable index.
 *
 * Renders nothing when there is no project: a legacy spec has no `projectFiles`
 * and must look exactly as it did before this slice.
 */
export function ProjectFileTree({
  groups,
  specPath,
  selectedPath,
  onSelect,
  checkedSpecs,
  onToggleSpec,
}: {
  groups: ProjectFileGroup<ProjectFileMeta>[];
  /** Path of the editable spec — the default selection. */
  specPath: string;
  /** Currently open file; equals `specPath` when the editor is showing the spec. */
  selectedPath: string;
  onSelect: (path: string) => void;
  /**
   * Ticked spec paths (#800). Passing it (with `onToggleSpec`) turns on the
   * multi-select checkboxes; omitting it leaves the tree exactly as the run
   * overlay has always rendered it.
   */
  checkedSpecs?: ReadonlySet<string>;
  /** Tick/untick one spec. **Spec rows only** — a page object, fixture or test
   * data file is not runnable on its own, so giving it a checkbox would offer a
   * selection the server refuses (`_selected_specs` requires `kind == "spec"`). */
  onToggleSpec?: (path: string) => void;
}) {
  const { t } = useTranslation("pipeline");
  const selectable = !!checkedSpecs && !!onToggleSpec;
  if (groups.length === 0) return null;
  return (
    <GlassCard className="p-2">
      <div className="px-2.5 pb-0.5 pt-2 text-[10.5px] font-semibold tracking-wider text-faint">
        {t("projectFiles.title")}
      </div>
      <div className="px-2.5 pb-1 text-[10.5px] text-muted">{t("projectFiles.readOnlyHint")}</div>
      <div className="px-1.5">
        {groups.map((g) => {
          const label = t(`projectFiles.kinds.${kindLabelKey(g.kind)}`);
          const holdsSelection = g.files.some((f) => f.path === selectedPath);
          return (
            <CollapsibleSection
              key={g.kind}
              title={`${label} · ${g.files.length}`}
              defaultOpen={g.kind === "spec" || holdsSelection}
            >
              <div className="mb-1 flex flex-col gap-0.5">
                {g.kind === "spec"
                  ? groupSpecsByTicket(g.files).map((tg) => (
                      <div key={tg.ticket || "__none"} className="flex flex-col gap-0.5">
                        {/* A file with no ticket directory renders bare — inventing a
                            header for it would be a label with nothing behind it. */}
                        {tg.ticket && (
                          <div className="px-2.5 pb-0.5 pt-1 font-mono text-[10.5px] font-semibold tracking-wider text-faint">
                            {tg.ticket}
                          </div>
                        )}
                        <div className={tg.ticket ? "flex flex-col gap-0.5 pl-2" : "flex flex-col gap-0.5"}>
                          {tg.files.map((f) => (
                            <FileRow
                              key={f.path}
                              file={f}
                              active={f.path === selectedPath}
                              editable={f.path === specPath}
                              onSelect={onSelect}
                              checked={selectable ? checkedSpecs!.has(f.path) : undefined}
                              onToggle={selectable ? onToggleSpec : undefined}
                            />
                          ))}
                        </div>
                      </div>
                    ))
                  : g.files.map((f) => (
                      <FileRow
                        key={f.path}
                        file={f}
                        active={f.path === selectedPath}
                        editable={f.path === specPath}
                        onSelect={onSelect}
                      />
                    ))}
              </div>
            </CollapsibleSection>
          );
        })}
      </div>
    </GlassCard>
  );
}

/** One file row. Extracted so the ticket-grouped and flat branches cannot drift
 *  apart — the lock, the active style and the tooltip must be identical.
 *
 *  The checkbox is a **sibling** of the open-file button, not a child of it: a
 *  button inside a button is invalid HTML and the nested control would never
 *  receive its own click. Ticking a spec therefore never opens it, and opening
 *  one never changes the selection — two independent actions on one row. */
function FileRow({
  file,
  active,
  editable,
  onSelect,
  checked,
  onToggle,
}: {
  file: ProjectFileMeta;
  active: boolean;
  editable: boolean;
  onSelect: (path: string) => void;
  /** `undefined` when this row is not selectable — no checkbox is rendered. */
  checked?: boolean;
  onToggle?: (path: string) => void;
}) {
  const { t } = useTranslation("pipeline");
  const row = (
    <PathTooltip label={file.path}>
      <button
        type="button"
        onClick={() => onSelect(file.path)}
        aria-current={active ? "true" : undefined}
        className="flex items-center gap-2 rounded-[10px] px-2.5 py-1.5 text-left hover:bg-card2"
        style={active ? { background: "var(--pt)" } : undefined}
      >
        <FileCode size={13} color={active ? "var(--psText)" : "var(--muted)"} />
        <span className="min-w-0 flex-1 truncate font-mono text-[11.5px] text-txt3">
          {baseName(file.path)}
        </span>
        {!editable && <Lock size={11} className="shrink-0 text-faint" aria-hidden="true" />}
      </button>
    </PathTooltip>
  );
  if (checked === undefined || !onToggle) return row;
  return (
    <div className="flex items-center gap-1.5">
      <input
        type="checkbox"
        checked={checked}
        onChange={() => onToggle(file.path)}
        aria-label={t("projectFiles.selectSpec", { name: baseName(file.path) })}
        data-testid="spec-checkbox"
        data-path={file.path}
        className="ml-1 h-3.5 w-3.5 shrink-0 accent-p"
      />
      <div className="min-w-0 flex-1">{row}</div>
    </div>
  );
}
