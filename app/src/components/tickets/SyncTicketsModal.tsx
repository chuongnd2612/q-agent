import { motion } from "framer-motion";
import { RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "@/lib/toast";
import { Button } from "@/components/ui/Button";
import { MultiSelect, Select } from "@/components/ui/Dropdown";
import { PROVIDER_META } from "@/components/settings/providerMeta";
import {
  useConnectionProjects,
  useConnectionSprints,
  useConnectionWorkItemMetadata,
  useSyncTickets,
} from "@/hooks/queries";
import { useIsMobile } from "@/hooks/useIsMobile";
import type { ProviderKind, SyncRequest } from "@/types/api";

const segStyle = (on: boolean) =>
  "flex-1 rounded-[9px] border-none px-2 py-[9px] text-[12.5px] font-semibold cursor-pointer " +
  (on
    ? "bg-[rgba(139,92,246,.2)] text-white shadow-[inset_0_0_0_1px_rgba(139,92,246,.3)]"
    : "bg-transparent text-[#a0a0b2]");

/**
 * Sync-tickets dialog (Basic / Advanced), matching the design's provider-aware
 * modal. Basic picks a Project + Sprint/Iteration (an iteration scopes to that
 * sprint, else the project's open items are pulled); Advanced filters by fields
 * that are **reflected by the selected connection's provider** — the options
 * come from `/connections/{id}/work-item-metadata` + `/sprints`, so ADO shows
 * area path / states / work-item types, Jira shows its statuses / issue types,
 * etc. Fields with no options for the provider are hidden. On mobile (below
 * `md`) the dialog is a bottom sheet; on desktop a centered modal.
 */
export function SyncTicketsModal({
  connectionId,
  projectGuid,
  providerKind,
  configuredProject,
  sourceLabel,
  onClose,
}: {
  connectionId: number | null;
  /** The project this sync runs for (#732). Sent so the server can resolve the
   *  TICKET SOURCE itself when the project has one but this screen hasn't loaded
   *  its config yet, and so synced tickets are stamped with the project. */
  projectGuid?: string | null;
  providerKind?: ProviderKind;
  /** The connection's configured default project — the Basic tab's initial pick. */
  configuredProject?: string;
  sourceLabel: string;
  onClose: () => void;
}) {
  const { t } = useTranslation("tickets");
  const isMobile = useIsMobile();
  const [tab, setTab] = useState<"basic" | "advanced">("basic");
  const [project, setProject] = useState<string | null>(configuredProject ?? null);
  const [sprintPath, setSprintPath] = useState<string | null>(null);
  const [areaPath, setAreaPath] = useState<string | null>(null);
  const [states, setStates] = useState<string[]>([]);
  const [workItemTypes, setWorkItemTypes] = useState<string[]>([]);

  const { data: projects } = useConnectionProjects(connectionId);
  const { data: metadata } = useConnectionWorkItemMetadata(connectionId);
  const { data: sprints } = useConnectionSprints(connectionId);
  const sync = useSyncTickets();

  const meta = providerKind ? PROVIDER_META[providerKind] : null;
  const isAdo = providerKind === "ado";

  // Default the Project pick to the connection's configured project once known.
  useEffect(() => {
    if (project == null && configuredProject) setProject(configuredProject);
  }, [project, configuredProject]);

  const projectOptions = (projects ?? []).map((p) => ({ value: p.name, label: p.name }));
  const sprintOptions = (sprints ?? []).map((s) => ({ value: s.path, label: s.name }));
  const areaOptions = (metadata?.areaPaths ?? []).map((a) => ({ value: a.path, label: a.name, hint: a.path }));
  const stateOptions = (metadata?.states ?? []).map((s) => ({ value: s, label: s }));
  const typeOptions = (metadata?.workItemTypes ?? []).map((t) => ({ value: t, label: t }));
  const hasAdvancedFields =
    sprintOptions.length > 0 ||
    (isAdo && areaOptions.length > 0) ||
    stateOptions.length > 0 ||
    typeOptions.length > 0;

  const runSync = () => {
    const sprintName = sprintPath
      ? (sprints ?? []).find((s) => s.path === sprintPath)?.name
      : undefined;
    const req: SyncRequest = {
      connectionId: connectionId ?? undefined,
      projectGuid: projectGuid ?? undefined,
      providerKind,
      project: project ?? undefined,
      // A chosen sprint/iteration is sprint-scoped; otherwise pull the project's
      // open items (also the Advanced fallback when no sprint is picked).
      mode: sprintPath ? "sprint" : "all",
      sprint: sprintName,
      sprintPath: sprintPath ?? undefined,
      areaPath: isAdo ? areaPath ?? undefined : undefined,
      states,
      workItemTypes,
    };
    sync.mutate(req, {
      onSuccess: (res) => {
        toast.success(t("syncDialog.syncedCount", { count: res.synced }));
        onClose();
      },
      onError: (err) => toast.error(err instanceof Error ? err.message : t("syncDialog.syncFailed")),
    });
  };

  return (
    <div
      onClick={onClose}
      className={
        "fixed inset-0 z-50 flex justify-center " +
        (isMobile ? "items-end" : "items-center p-5")
      }
      style={{ background: "rgba(6,6,10,.62)", backdropFilter: "blur(7px)" }}
    >
      <motion.div
        onClick={(e) => e.stopPropagation()}
        // Mobile: a bottom sheet that slides up. Desktop: a centered scale-in card.
        initial={isMobile ? { y: "100%" } : { opacity: 0, scale: 0.96 }}
        animate={isMobile ? { y: 0 } : { opacity: 1, scale: 1 }}
        transition={isMobile ? { duration: 0.32, ease: [0.2, 0.8, 0.2, 1] } : { duration: 0.22, ease: "easeOut" }}
        className={
          isMobile
            ? "flex max-h-[92vh] w-full flex-col overflow-hidden rounded-t-[26px] border-x border-t border-white/[0.11]"
            : "w-[min(560px,94vw)] overflow-hidden rounded-[22px] border border-white/[0.11]"
        }
        style={{ background: "rgba(22,22,30,.94)", backdropFilter: "blur(40px)", boxShadow: "0 40px 90px -20px rgba(0,0,0,.8)" }}
      >
        {isMobile && (
          <div className="flex shrink-0 justify-center pt-2.5">
            <span className="h-1 w-10 rounded-full bg-white/25" />
          </div>
        )}
        <div className="flex shrink-0 items-center gap-3 border-b border-white/[0.07] p-[20px_24px]">
          <div
            className="flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-[11px] text-[15px] font-black"
            style={{ background: meta?.color ?? "#8b5cf6", color: meta?.glyphColor ?? "#fff" }}
          >
            {meta?.glyph ?? "?"}
          </div>
          <div className="flex-1">
            <div className="text-[17px] font-extrabold">{t("syncDialog.title")}</div>
            <div className="text-[12px] text-ink-dim">{t("syncDialog.pullFrom", { source: sourceLabel })}</div>
          </div>
        </div>

        <div className="shrink-0 p-[16px_24px_4px]">
          <div className="flex gap-1.5 rounded-[11px] border border-white/[0.07] bg-white/[0.04] p-1">
            <button className={segStyle(tab === "basic")} onClick={() => setTab("basic")}>
              {t("syncDialog.basic")}
            </button>
            <button className={segStyle(tab === "advanced")} onClick={() => setTab("advanced")}>
              {t("syncDialog.advanced")}
            </button>
          </div>
        </div>

        <div
          className={
            (isMobile ? "flex-1" : "max-h-[56vh]") + " overflow-y-auto p-[16px_24px_20px]"
          }
        >
          {tab === "basic" ? (
            <>
              <div className="mb-2.5 text-[11px] font-bold tracking-[0.06em] text-[#6c6c7e]">
                {t("syncDialog.whatToPull")}
              </div>
              <div className="grid grid-cols-1 gap-x-2.5 gap-y-3 md:grid-cols-2">
                <Field label={t("syncDialog.project")}>
                  <Select
                    value={project}
                    options={projectOptions}
                    placeholder={configuredProject ?? t("syncDialog.selectProject")}
                    onChange={setProject}
                    emptyLabel={t("syncDialog.noProjectsFound")}
                    fullWidth
                  />
                </Field>
                <Field label={isAdo ? t("syncDialog.iteration") : t("syncDialog.sprint")}>
                  <Select
                    value={sprintPath}
                    options={sprintOptions}
                    placeholder={t("syncDialog.allOpenItems")}
                    onChange={setSprintPath}
                    emptyLabel={t("syncDialog.noSprintsFound")}
                    fullWidth
                  />
                </Field>
              </div>
              <div className="mt-3 text-[12px] text-ink-dim">
                {sprintPath
                  ? t("syncDialog.pullsChosenIteration")
                  : t("syncDialog.noIterationChosen")}
              </div>
            </>
          ) : (
            <>
              <div className="mb-2.5 text-[11px] font-bold tracking-[0.06em] text-[#6c6c7e]">
                {t("syncDialog.filterByField")}
              </div>
              {hasAdvancedFields ? (
                <div className="grid grid-cols-1 gap-x-2.5 gap-y-3 md:grid-cols-2">
                  {sprintOptions.length > 0 && (
                    <Field label={t("syncDialog.sprint")}>
                      <Select value={sprintPath} options={sprintOptions} placeholder={t("syncDialog.any")} onChange={setSprintPath} fullWidth />
                    </Field>
                  )}
                  {isAdo && areaOptions.length > 0 && (
                    <Field label={t("syncDialog.areaPath")}>
                      <Select value={areaPath} options={areaOptions} placeholder={t("syncDialog.any")} onChange={setAreaPath} fullWidth />
                    </Field>
                  )}
                  {stateOptions.length > 0 && (
                    <Field label={isAdo ? t("syncDialog.states") : t("syncDialog.status")}>
                      <MultiSelect values={states} options={stateOptions} placeholder={t("syncDialog.any")} onChange={setStates} fullWidth />
                    </Field>
                  )}
                  {typeOptions.length > 0 && (
                    <Field label={isAdo ? t("syncDialog.workItemTypes") : t("syncDialog.issueType")}>
                      <MultiSelect values={workItemTypes} options={typeOptions} placeholder={t("syncDialog.any")} onChange={setWorkItemTypes} fullWidth />
                    </Field>
                  )}
                </div>
              ) : (
                <div className="rounded-[12px] border border-dashed border-white/[0.14] p-4 text-center text-[12.5px] text-ink-dim">
                  {t("syncDialog.noFilterableFields")}
                </div>
              )}
            </>
          )}
        </div>

        <div
          className="flex shrink-0 items-center gap-2.5 border-t border-white/[0.07] p-[16px_24px]"
          style={isMobile ? { paddingBottom: "calc(16px + env(safe-area-inset-bottom))" } : undefined}
        >
          <span className="flex-1 text-[11.5px] text-[#7a7a8c]">{t("syncDialog.credentialsEncrypted")}</span>
          <Button variant="glass" onClick={onClose} disabled={sync.isPending}>
            {t("common:cancel")}
          </Button>
          <Button onClick={runSync} disabled={sync.isPending || connectionId == null}>
            <RefreshCw size={14} className={sync.isPending ? "animate-[spin_.7s_linear_infinite]" : ""} />
            {sync.isPending ? t("syncDialog.syncing") : t("syncDialog.syncNow")}
          </Button>
        </div>
      </motion.div>
    </div>
  );
}

/** Labeled field wrapper for the Advanced filter grid. */
function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-1.5 text-[11.5px] font-semibold text-[#9494a6]">{label}</div>
      {children}
    </div>
  );
}
