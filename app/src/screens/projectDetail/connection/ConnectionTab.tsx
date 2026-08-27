import { Link2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import {
  useHubDataEnabled,
  useProjectConfig,
  useProviders,
  useSaveProjectConfig,
} from "@/hooks/queries";
import { toast } from "@/lib/toast";
import { ProjectSettingsTab } from "../ProjectSettingsTab";
import { RoleCard } from "./RoleCard";
import { ROLES, bindRolePatch, boundConnectionId } from "./roles";

/**
 * Project → **Connection** (ADR 0015 §3, #732).
 *
 * The project's three connection roles in one place: `TICKET SOURCE` (the only
 * place tickets come from), `CODE & KNOWLEDGE` (repo + PRs feeding Project
 * Knowledge and automation) and `TEST CASE TARGET` (where approved cases are
 * created, linked and published back).
 *
 * This is the *only* place a project's provider is chosen. Nothing in the ticket
 * flow switches it any more — the Tickets tab reads the binding made here — which
 * is the containment ADR 0015 §1 is for: a list that could show a provider having
 * nothing to do with the project the user thought they were in.
 *
 * The rest of the project's runtime configuration (base URL, repos, environments,
 * test accounts, manual login) is unchanged and still rendered below, because it
 * has never had another home and deleting it here would not move it anywhere.
 */
export function ConnectionTab({
  projectKey,
  hubProjectId,
}: {
  projectKey: string;
  hubProjectId?: string | null;
}) {
  const { t } = useTranslation("projects");
  const { data: config, isLoading } = useProjectConfig(projectKey);
  const { data: providers } = useProviders();
  const save = useSaveProjectConfig(projectKey);
  // `resolved` gates read-only, not just `enabled`: offering a binding and then
  // withdrawing it once `/health` answers is the flash #528 closed.
  const { enabled: hubOwnsProjects, resolved: hubResolved } = useHubDataEnabled();
  const readOnly = !hubResolved || hubOwnsProjects;

  const allConnections = (providers ?? []).flatMap((g) => g.connections);

  if (isLoading || !config) {
    return (
      <div className="glass rounded-[18px] p-8 text-center text-[13px] text-ink-dim">
        {t("common:loading")}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <div
        className="flex items-center gap-3.5 rounded-[16px] border p-[15px_18px]"
        style={{
          background: "linear-gradient(135deg,rgba(139,92,246,.14),rgba(99,102,241,.05))",
          borderColor: "rgba(139,92,246,.24)",
        }}
        data-testid="connection-tab-intro"
      >
        <span
          className="flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-[10px]"
          style={{ background: "rgba(139,92,246,.2)" }}
        >
          <Link2 size={17} color="#c4b5fd" strokeWidth={2} />
        </span>
        <div className="flex-1">
          <div className="text-[13.5px] font-bold">{t("connectionTab.intro.title")}</div>
          <div className="mt-0.5 text-[12px] text-[#b9a8e6]">{t("connectionTab.intro.body")}</div>
        </div>
      </div>

      {ROLES.map((role) => {
        const { id, inherited } = boundConnectionId(config, role);
        const connection = allConnections.find((c) => c.id === id) ?? null;
        return (
          <RoleCard
            key={role.id}
            role={role}
            connection={connection}
            inherited={inherited}
            options={allConnections.filter((c) => c.categories.includes(role.capability))}
            saving={save.isPending}
            readOnly={readOnly}
            onBind={(connectionId) =>
              save.mutate(bindRolePatch(role, connectionId), {
                onSuccess: () => toast.success(t("connectionTab.bound")),
                onError: (err) =>
                  toast.error(
                    err instanceof Error ? err.message : t("settingsTab.saveError"),
                  ),
              })
            }
          />
        );
      })}

      <div className="mt-2.5">
        <ProjectSettingsTab projectKey={projectKey} hubProjectId={hubProjectId} hideConnections />
      </div>
    </div>
  );
}
