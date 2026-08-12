import { AlertTriangle, CheckCircle2, GitBranch, Upload } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/Button";
import { CollapsibleSection } from "@/components/settings/CollapsibleSection";
import { Spinner } from "@/components/ui/misc";
import {
  useAutomationExportPreflight,
  useExportAutomationProject,
} from "@/hooks/queries";
import { toast } from "@/lib/toast";
import type { AutomationExportResult } from "@/types/api";

const INPUT_CLASS =
  "w-full rounded-[11px] border border-white/[0.09] bg-white/[0.04] px-[13px] py-2.5 font-mono text-[12.5px] text-ink outline-none focus:border-[rgba(139,92,246,.5)]";

/**
 * "Export automation project" — push the run's git-backed automation project to a
 * remote the **customer** owns, so they can run the suite in their own CI (#549).
 *
 * Three properties are deliberate, and each is a rule from the slice rather than a
 * styling choice:
 *
 * * **Nothing pushes automatically.** The panel's only side effect on mount is the
 *   read-only preflight (which pushes nothing); a push happens on click and nowhere
 *   else. There is no push-on-generate and no retry-on-focus.
 * * **The target and branch are the user's.** Both are editable text, prefilled with
 *   a suggestion from the server. The suggested branch carries the
 *   `qagent/automation/…` prefix, so the happy path can never be the remote's
 *   default branch — the server refuses that, and mainline names, outright.
 * * **A refusal is the normal outcome, not an exception.** A diverged remote branch
 *   is reported in place with the server's own explanation (Q-Agent will not
 *   force-push and will not merge AI-authored code into hand edits), because the fix
 *   is the user's decision.
 *
 * Collapsed by default via `CollapsibleSection` (#536) — an export is an occasional,
 * deliberate act, so it should not occupy the screen while generating specs. Nothing
 * here goes into Zustand: the two inputs and the last result are local component
 * state, and no navigation state is introduced.
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
  const [open, setOpen] = useState(false);
  const { data: preflight, isLoading } = useAutomationExportPreflight(runId, projectId, open);
  const exportProject = useExportAutomationProject(runId);

  const [remoteUrl, setRemoteUrl] = useState("");
  const [branch, setBranch] = useState("");
  const [result, setResult] = useState<AutomationExportResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Prefill from the server's suggestion, once, and never over a value the user has
  // started editing. A redacted suggestion (`https://***@…`) is not offered as a
  // remote — it would be pushed verbatim and fail — so only clean URLs prefill.
  useEffect(() => {
    if (!preflight) return;
    setBranch((b) => b || preflight.branch);
    setRemoteUrl((u) => u || (preflight.remoteUrl.includes("***") ? "" : preflight.remoteUrl));
  }, [preflight]);

  if (projectId == null) return null;

  const busy = exportProject.isPending;
  const canExport = !busy && remoteUrl.trim().length > 0 && branch.trim().length > 0;

  const submit = () => {
    if (!canExport) return;
    setResult(null);
    setError(null);
    exportProject.mutate(
      { remoteUrl: remoteUrl.trim(), branch: branch.trim(), projectId },
      {
        onSuccess: (data) => {
          setResult(data);
          toast.success(
            data.pushed
              ? t("export.pushed", { branch: data.branch })
              : t("export.alreadyUpToDate", { branch: data.branch }),
          );
        },
        onError: (e) => {
          const message = e instanceof Error ? e.message : t("export.failed");
          setError(message);
          toast.error(message);
        },
      },
    );
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
      <CollapsibleSection title={t("export.title")} onOpenChange={setOpen}>
        <p className="m-0 mb-3 text-xs leading-relaxed text-muted">{t("export.description")}</p>

        {isLoading && (
          <div className="flex items-center gap-2 text-xs text-muted">
            <Spinner size={13} /> {t("export.loading")}
          </div>
        )}

        {preflight && !preflight.hasCredentials && (
          <div
            className="mb-3 flex items-start gap-2 rounded-[11px] border border-[rgba(245,158,11,.28)] px-3 py-2.5 text-[12px] leading-relaxed text-warning-soft"
            style={{ background: "rgba(245,158,11,.1)" }}
            role="alert"
            data-testid="export-credentials-error"
          >
            <AlertTriangle size={14} className="mt-0.5 shrink-0" />
            <span>{preflight.credentialsError}</span>
          </div>
        )}

        <div className="flex flex-col gap-3">
          <label className="flex flex-col gap-1.5">
            <span className="text-[11.5px] font-semibold text-ink-soft">
              {t("export.remoteLabel")}
            </span>
            <input
              className={INPUT_CLASS}
              value={remoteUrl}
              onChange={(e) => setRemoteUrl(e.target.value)}
              placeholder="https://github.com/acme/automation.git"
              spellCheck={false}
              data-testid="export-remote-input"
            />
            <span className="text-[11px] text-faint">{t("export.remoteHint")}</span>
          </label>
          <label className="flex flex-col gap-1.5">
            <span className="text-[11.5px] font-semibold text-ink-soft">
              {t("export.branchLabel")}
            </span>
            <input
              className={INPUT_CLASS}
              value={branch}
              onChange={(e) => setBranch(e.target.value)}
              placeholder="qagent/automation/suite"
              spellCheck={false}
              data-testid="export-branch-input"
            />
            <span className="text-[11px] text-faint">{t("export.branchHint")}</span>
          </label>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="primary"
              onClick={submit}
              disabled={!canExport}
              data-testid="export-submit"
            >
              {busy ? <Spinner size={14} /> : <Upload size={15} strokeWidth={2.2} />}
              {busy ? t("export.pushing") : t("export.action")}
            </Button>
            {preflight?.commit && (
              <span className="font-mono text-[11px] text-faint">
                {t("export.head", { commit: preflight.commit.slice(0, 8) })}
              </span>
            )}
          </div>
        </div>

        {error && (
          <div
            className="mt-3 flex items-start gap-2 rounded-[11px] border border-[rgba(244,63,94,.28)] px-3 py-2.5 text-[12px] leading-relaxed text-[#fb7185]"
            style={{ background: "rgba(244,63,94,.1)" }}
            role="alert"
            data-testid="export-error"
          >
            <AlertTriangle size={14} className="mt-0.5 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        {result && (
          <div
            className="mt-3 rounded-[11px] border border-[rgba(16,185,129,.28)] px-3 py-2.5 text-[12px] leading-relaxed"
            style={{ background: "rgba(16,185,129,.09)" }}
            data-testid="export-result"
          >
            <div className="flex items-center gap-2 font-semibold text-[#6ee7b7]">
              <CheckCircle2 size={14} />
              {result.pushed
                ? t("export.pushed", { branch: result.branch })
                : t("export.alreadyUpToDate", { branch: result.branch })}
            </div>
            <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-ink-soft">
              <span className="flex items-center gap-1.5 font-mono text-[11.5px]">
                <GitBranch size={12} /> {result.branch}
              </span>
              <span className="font-mono text-[11.5px] text-muted">{result.remote}</span>
              <span className="font-mono text-[11.5px] text-faint">
                {result.commit.slice(0, 8)}
              </span>
            </div>
            {/* No adapter can open a pull request yet (#549), so the branch is
                reported and the user opens the PR on their own host. */}
            <div className="mt-1.5 text-[11.5px] text-muted">
              {result.prUrl ? (
                <a href={result.prUrl} target="_blank" rel="noreferrer" className="text-violet">
                  {t("export.openPr")}
                </a>
              ) : (
                t("export.openPrYourself")
              )}
            </div>
          </div>
        )}
      </CollapsibleSection>
    </div>
  );
}
