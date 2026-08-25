import { Clock3, FileArchive, GitBranch } from "lucide-react";
import { useTranslation } from "react-i18next";
import { CollapsibleSection } from "@/components/settings/CollapsibleSection";

/**
 * "Export automation project" — the run's git-backed automation suite, handed to the
 * customer so they can run it in their own CI (#549).
 *
 * **This is a coming-soon placeholder (#680).** The feature is Version 2, and its
 * scope is *two* exports — a ZIP download and a push to a git remote. Only the remote
 * half was ever built, and on its own it opened by confronting the user with plumbing
 * they had not set up (a repository connection with a stored PAT), so the panel mostly
 * showed a configuration error where a feature was advertised.
 *
 * Two properties of this placeholder are deliberate:
 *
 * * **No preflight request.** The panel's expand has no side effect at all. There is
 *   nothing to be ready for yet, so asking the server about readiness could only
 *   produce a warning about a prerequisite for a feature that is not offered.
 * * **The form is removed, not disabled.** A greyed-out remote/branch form with a dead
 *   button reads as "almost working" and invites the user to hunt for what they got
 *   wrong. A named, dated-forward "coming in v2" state is the honest shape.
 *
 * The backend (`automation_export_service`) is untouched and dormant; v2 builds the ZIP
 * path and re-enables the remote path on top of it.
 */
export function ExportProjectPanel({
  projectId,
}: {
  /** The automation project this would export; `null` for a legacy run (panel hidden). */
  projectId: number | null;
}) {
  const { t } = useTranslation("pipeline");

  if (projectId == null) return null;

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
        <div
          className="mb-3 inline-flex items-center gap-1.5 rounded-full border border-[rgba(139,92,246,.32)] px-2.5 py-1 text-[11px] font-semibold text-violet"
          style={{ background: "rgba(139,92,246,.12)" }}
          data-testid="export-coming-soon"
        >
          <Clock3 size={12} strokeWidth={2.4} />
          {t("export.comingSoon")}
        </div>

        <p className="m-0 mb-3.5 text-xs leading-relaxed text-muted">
          {t("export.v2Description")}
        </p>

        <ul className="m-0 flex list-none flex-col gap-2.5 p-0">
          <li className="flex items-start gap-2.5">
            <FileArchive size={15} className="mt-0.5 shrink-0 text-ink-soft" strokeWidth={2} />
            <span className="text-[12px] leading-relaxed">
              <span className="font-semibold text-ink-soft">{t("export.zipTitle")}</span>
              <span className="text-muted"> — {t("export.zipHint")}</span>
            </span>
          </li>
          <li className="flex items-start gap-2.5">
            <GitBranch size={15} className="mt-0.5 shrink-0 text-ink-soft" strokeWidth={2} />
            <span className="text-[12px] leading-relaxed">
              <span className="font-semibold text-ink-soft">{t("export.remoteTitle")}</span>
              <span className="text-muted"> — {t("export.remoteHint2")}</span>
            </span>
          </li>
        </ul>
      </CollapsibleSection>
    </div>
  );
}
