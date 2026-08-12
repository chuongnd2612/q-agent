import { ChevronsDownUp, ChevronsUpDown, Copy, Download, Lock } from "lucide-react";
import { useTranslation } from "react-i18next";
import { OverflowMenu } from "@/components/ui/OverflowMenu";
import { toast } from "@/lib/toast";
import type { ProjectFile } from "@/types/api";
import { CodeHighlight } from "./CodeViewer";
import { useCodeFolding } from "./useCodeFolding";
import { baseName, kindLabelKey } from "./projectFiles";

/**
 * Read-only viewer for a **support** file of the automation project (page object,
 * fixture, test data, util, config) — #543.
 *
 * Deliberately offers no editable affordance: no Edit, no Save, no Regenerate, no
 * chat. Editing support files is deferred because a write must route through the
 * quality gate, not straight to disk. Only inert actions (fold, copy, download)
 * are exposed, and the header states the file is read-only.
 *
 * The spec file itself never renders here — it keeps the full `SpecCodePanel`.
 */
export function ProjectFilePanel({ file }: { file: ProjectFile }) {
  const { t } = useTranslation("pipeline");
  const { foldRanges, folded, toggleFold, collapseAll, expandAll } = useCodeFolding(
    file.code,
    file.path,
  );

  const copy = () => {
    navigator.clipboard.writeText(file.code);
    toast.success(t("projectFiles.copied"));
  };
  const download = () => {
    const blob = new Blob([file.code], { type: "text/typescript" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = baseName(file.path);
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div
      className="overflow-hidden rounded-2xl border border-white/[0.09]"
      // Opaque surface (no backdrop-filter): this panel layers over the animated
      // shell, and a filter would also trap child stacking contexts.
      style={{ background: "rgba(8,8,13,.92)" }}
    >
      <div className="flex flex-wrap items-center gap-2.5 border-b border-white/[0.06] px-4 py-3">
        <span className="font-mono text-[12.5px] text-ink-soft">{file.path}</span>
        <span
          className="rounded-md px-2 py-0.5 text-[10px] font-bold"
          style={{ background: "rgba(139,92,246,.14)", color: "#c4b5fd" }}
        >
          {t(`projectFiles.kinds.${kindLabelKey(file.kind)}`)}
        </span>
        <span className="flex items-center gap-1 text-[11px] font-semibold text-faint">
          <Lock size={11} /> {t("projectFiles.readOnly")}
        </span>
        <div className="ml-auto flex items-center gap-1.5">
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
                key: "copy",
                label: t("spec.menu.copy"),
                icon: <Copy size={14} />,
                onClick: copy,
              },
              {
                key: "download",
                label: t("spec.menu.download"),
                icon: <Download size={14} />,
                onClick: download,
              },
            ]}
          />
        </div>
      </div>
      {file.code.trim() ? (
        <CodeHighlight
          code={file.code}
          foldRanges={foldRanges}
          folded={folded}
          onToggle={toggleFold}
        />
      ) : (
        <div
          className="flex items-center justify-center px-4 py-16 text-center text-xs text-muted"
          style={{ minHeight: 200 }}
        >
          {t("projectFiles.emptyFile")}
        </div>
      )}
      <div className="border-t border-white/[0.06] px-4 py-3">
        <span className="text-xs text-muted">{t("projectFiles.footer")}</span>
      </div>
    </div>
  );
}
