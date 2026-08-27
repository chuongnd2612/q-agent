import { ExternalLink } from "lucide-react";
import { useTranslation } from "react-i18next";
import { toast } from "@/lib/toast";
import {
  useHubDataEnabled,
  useHubWebUrl,
  useProjectConfig,
  useSaveProjectConfig,
} from "@/hooks/queries";
import { ProjectSettingsForm } from "./ProjectSettingsForm";
import { ManualLoginStatus } from "./ManualLogin";

/**
 * Project Details → Settings tab. Thin wrapper: loads the current user's own
 * project config and renders {@link ProjectSettingsForm}, wiring the manual-login
 * widget to the owner-scoped auth endpoints.
 *
 * With `QAGENT_HUB_DATA_ENABLED` on, EmeHub owns project configuration, so this
 * renders read-only and points at the hub (#587) — the same rule #528 applied to
 * Claude credentials. **The flag is the switch**, not per-project guessing, which
 * keeps the behaviour predictable and reversible. The API refuses the `PUT` under
 * the same flag, so this is not a UI-only promise (#512).
 */
export function ProjectSettingsTab({
  projectKey,
  hubProjectId,
  hideConnections = false,
}: {
  projectKey: string;
  hubProjectId?: string | null;
  /** Passed through to the form — the Connection tab renders the three role
   *  cards itself and hides the form's duplicate pickers (#732). */
  hideConnections?: boolean;
}) {
  const { t } = useTranslation("projects");
  const { data: config, isLoading } = useProjectConfig(projectKey);
  const save = useSaveProjectConfig(projectKey);
  // `resolved` gates the read-only switch, not just `enabled`: rendering an
  // editable form and then withdrawing it once `/health` answers is exactly the
  // flash #528 closed. Until then, treat it as read-only — the safer half-answer.
  const { enabled: hubOwnsProjects, resolved: hubResolved } = useHubDataEnabled();
  const readOnly = !hubResolved || hubOwnsProjects;

  if (isLoading || !config) {
    return (
      <div className="glass rounded-[18px] p-8 text-center text-[13px] text-ink-dim">
        {t("common:loading")}
      </div>
    );
  }
  return (
    <>
      {hubResolved && hubOwnsProjects ? <HubManagedProjectNotice hubProjectId={hubProjectId} /> : null}
      <ProjectSettingsForm
        config={config}
        saving={save.isPending}
        readOnly={readOnly}
        hideConnections={hideConnections}
        onSave={(patch) =>
          save.mutate(patch, {
            onSuccess: () => toast.success(t("settingsTab.saved")),
            onError: (err) => toast.error(err instanceof Error ? err.message : t("settingsTab.saveError")),
          })
        }
        renderManualLogin={(hasBaseUrl) => (
          <ManualLoginStatus projectKey={projectKey} hasBaseUrl={hasBaseUrl} />
        )}
      />
    </>
  );
}

/**
 * "EmeHub owns this" banner, with a deep link to the hub's project screen when we
 * know which project it is.
 *
 * The hub routes by **numeric id** (`<hub web origin>/app/projects/{id}`), not by
 * key, so a project we never mirrored has no link we can complete — it gets the
 * generic hint instead. A guessed link would be worse than none: it lands on
 * someone's 404 and reads as a Q-Agent bug.
 *
 * The hub being unreachable is not an error state (#491): `useHubWebUrl` resolves
 * `null` rather than throwing, and the notice still says who owns the setting.
 */
function HubManagedProjectNotice({ hubProjectId }: { hubProjectId?: string | null }) {
  const { t } = useTranslation("projects");
  const hubWebUrl = useHubWebUrl();
  const href = hubWebUrl && hubProjectId ? `${hubWebUrl}/app/projects/${hubProjectId}` : null;

  return (
    <div
      className="mb-3.5 rounded-[16px] border p-4"
      style={{ background: "rgba(139,92,246,.07)", borderColor: "rgba(139,92,246,.28)" }}
      data-testid="hub-managed-project-notice"
    >
      <div className="mb-1.5 text-[14px] font-bold text-ink">{t("hubManaged.title")}</div>
      <p className="m-0 text-[12.5px] leading-relaxed text-[#a6a6b6]">
        {href ? t("hubManaged.body") : t("hubManaged.bodyNoLink")}
      </p>
      {href ? (
        <a
          href={href}
          target="_blank"
          rel="noreferrer"
          className="mt-3 inline-flex items-center gap-1.5 rounded-[10px] border border-white/[0.1] bg-white/[0.04] px-3 py-1.5 text-[12.5px] font-semibold text-ink hover:bg-white/[0.08]"
        >
          {t("hubManaged.openInHub")}
          <ExternalLink size={13} strokeWidth={2.2} />
        </a>
      ) : null}
    </div>
  );
}
