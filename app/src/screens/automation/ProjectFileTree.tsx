import { FileCode, Lock } from "lucide-react";
import { useTranslation } from "react-i18next";
import { GlassCard } from "@/components/ui/GlassCard";
import { CollapsibleSection } from "@/components/settings/CollapsibleSection";
import { PathTooltip } from "./PathTooltip";
import { baseName, kindLabelKey, type ProjectFileGroup } from "./projectFiles";

/**
 * The automation project's file list, shown beside the spec editor (#543).
 *
 * Files are grouped by `kind` so the layering is legible at a glance (#537 doc
 * §20's ownership model). Exactly one row — `specPath` — is the **editable** spec
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
}: {
  groups: ProjectFileGroup[];
  /** Path of the editable spec — the default selection. */
  specPath: string;
  /** Currently open file; equals `specPath` when the editor is showing the spec. */
  selectedPath: string;
  onSelect: (path: string) => void;
}) {
  const { t } = useTranslation("pipeline");
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
                {g.files.map((f) => {
                  const active = f.path === selectedPath;
                  const editable = f.path === specPath;
                  return (
                    <PathTooltip key={f.path} label={f.path}>
                      <button
                        type="button"
                        onClick={() => onSelect(f.path)}
                        aria-current={active ? "true" : undefined}
                        className="flex items-center gap-2 rounded-[10px] px-2.5 py-1.5 text-left hover:bg-white/5"
                        style={active ? { background: "rgba(139,92,246,.14)" } : undefined}
                      >
                        <FileCode size={13} color={active ? "#a78bfa" : "#8b8b9e"} />
                        <span className="min-w-0 flex-1 truncate font-mono text-[11.5px] text-ink-soft">
                          {baseName(f.path)}
                        </span>
                        {!editable && (
                          <Lock size={11} className="shrink-0 text-faint" aria-hidden="true" />
                        )}
                      </button>
                    </PathTooltip>
                  );
                })}
              </div>
            </CollapsibleSection>
          );
        })}
      </div>
    </GlassCard>
  );
}
