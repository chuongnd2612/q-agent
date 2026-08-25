import { Clock3, Download, GitBranch } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/Button";
import { CollapsibleSection } from "@/components/settings/CollapsibleSection";
import { Spinner } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { toast } from "@/lib/toast";

/**
 * "Export automation project" — hand the run's automation suite to the customer.
 *
 * Two exports, two states, and the split is the whole design (#686):
 *
 * * **Export to ZIP — v1, and it works.** A download, and nothing else: no
 *   repository connection, no PAT, no branch policy, no network. That is *why* it
 *   is v1. The remote push had all four as prerequisites, so the panel used to open
 *   straight into a configuration error for a feature the user had not asked to
 *   configure.
 * * **Export to remote — v2, and it says so.** Kept visible, because it is the
 *   plan and users should know it is coming, but not offered as a button that
 *   fails. #680 briefly made the *whole* panel coming-soon, which removed a
 *   capability instead of staging one.
 *
 * No preflight request on expand: the ZIP needs nothing to be ready for, and the
 * remote is not on offer, so a readiness check could only produce a warning about a
 * prerequisite for neither. Collapsed by default (#536) — an export is an
 * occasional, deliberate act. Nothing here goes into Zustand.
 */
export function ExportProjectPanel({
  runId,
  projectId,
}: {
  runId: number;
  /** The automation project to export; `null` for a legacy run (panel hidden). */
  projectId: number | null;
}) {
  const { t } = useTranslation("pipeline");
  const [busy, setBusy] = useState(false);

  if (projectId == null) return null;

  const downloadZip = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const { blob, filename } = await api.exportAutomationProjectZip(runId, projectId);
      // The viewer's own browser saves it; the object URL is revoked immediately
      // after the click so the blob is not held for the life of the page.
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      anchor.click();
      URL.revokeObjectURL(url);
      toast.success(t("export.zipDownloaded", { filename }));
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t("export.zipFailed"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="mb-3.5 rounded-2xl border border-white/[0.09] p-4"
      // Opaque surface, not GlassCard: this panel is long-form text over the shell's
      // animated constellation background, and a translucent card made the hint text
      // genuinely hard to read (verified in the runtime screenshots). Same reasoning —
      // and the same value — as ProjectFilePanel.
      style={{ background: "rgba(8,8,13,.92)" }}
    >
      <CollapsibleSection title={t("export.title")}>
        <p className="m-0 mb-3.5 text-xs leading-relaxed text-muted">
          {t("export.description")}
        </p>

        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <Button
              variant="primary"
              onClick={downloadZip}
              disabled={busy}
              data-testid="export-zip"
            >
              {busy ? <Spinner size={14} /> : <Download size={15} strokeWidth={2.2} />}
              {busy ? t("export.zipping") : t("export.zipAction")}
            </Button>
            <span className="text-[11.5px] leading-relaxed text-faint">
              {t("export.zipHint")}
            </span>
          </div>

          <div className="h-px bg-white/[0.07]" />

          <div className="flex items-start gap-2.5" data-testid="export-remote-coming-soon">
            <GitBranch size={15} className="mt-0.5 shrink-0 text-ink-soft" strokeWidth={2} />
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[12.5px] font-semibold text-ink-soft">
                  {t("export.remoteTitle")}
                </span>
                <span
                  className="inline-flex items-center gap-1 rounded-full border border-[rgba(139,92,246,.32)] px-2 py-0.5 text-[10.5px] font-semibold text-violet"
                  style={{ background: "rgba(139,92,246,.12)" }}
                >
                  <Clock3 size={11} strokeWidth={2.4} />
                  {t("export.comingSoon")}
                </span>
              </div>
              <p className="m-0 mt-1 text-[11.5px] leading-relaxed text-muted">
                {t("export.remoteHint")}
              </p>
            </div>
          </div>
        </div>
      </CollapsibleSection>
    </div>
  );
}
