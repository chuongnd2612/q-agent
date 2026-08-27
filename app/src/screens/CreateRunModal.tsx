import { useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import { FlaskConical, Plus } from "lucide-react";
import { useMatch, useNavigate } from "react-router-dom";
import { toast } from "@/lib/toast";
import { Button } from "@/components/ui/Button";
import {
  ALL_TICKETS_PAGE_SIZE,
  useCreateRun,
  useProjectEnvironments,
  useSettings,
  useTickets,
} from "@/hooks/queries";
import { ToggleRow } from "@/components/settings/ToggleRow";
import { useIsMobile } from "@/hooks/useIsMobile";
import { useAuth } from "@/store/auth";
import { useUI } from "@/store/ui";
import { useTranslation } from "react-i18next";

/** The only framework there is. Selenium used to be offered here and was
 *  persisted to `Run.framework`, but every spec is stamped "Playwright" on the
 *  way in and only Playwright ever executes — so picking it silently gave you
 *  Playwright anyway (#671). Shown as a static pill, not a choice. */
const FRAMEWORK = "Playwright";

const segStyle = (on: boolean) =>
  "flex-1 rounded-[10px] border-none px-2 py-[9px] text-[12.5px] font-semibold cursor-pointer " +
  (on ? "bg-[rgba(139,92,246,.2)] text-white shadow-[inset_0_0_0_1px_rgba(139,92,246,.3)]" : "bg-white/[0.05] text-[#a0a0b2]");

/** The project the modal is creating a run *inside*, read from the URL.
 *
 * The modal is mounted at App level, above the project routes, so it cannot use
 * `useProjectRoute()`. The URL is the source of truth for navigation anyway
 * (CLAUDE.md), so matching it is the right read rather than a workaround — and it
 * returns `null` off a project route, which is the honest answer: the ticket
 * pickers then have nothing to scope to and offer nothing.
 */
function useModalProjectGuid(): string | null {
  const match = useMatch("/projects/:projectGuid/*");
  const guid = match?.params.projectGuid;
  return guid ? decodeURIComponent(guid) : null;
}

/** Create-Run modal: scope/framework/env/workers + link options, wired to POST /runs.
 *
 * Owns the three things the hidden `sync` stage used to (ADR 0015 §5, #732):
 * *link or not*, *which subset of tickets* and **dry run**. They are decided here
 * because this is where the run's scope is already being chosen, and stored on the
 * run — the Link stage reads them when it runs. Dry run in particular must keep a
 * route into the product; losing it would be a regression, not a simplification.
 */
export function CreateRunModal() {
  const { t } = useTranslation("runs");
  const open = useUI((s) => s.createRunOpen);
  const closeCreateRun = useUI((s) => s.closeCreateRun);
  const runScope = useUI((s) => s.runScope);
  const runEnv = useUI((s) => s.runEnv);
  const runWorkers = useUI((s) => s.runWorkers);
  const runRetry = useUI((s) => s.runRetry);
  const runBrowser = useUI((s) => s.runBrowser);
  const setRunField = useUI((s) => s.setRunField);
  const selected = useUI((s) => s.selected);
  const navigate = useNavigate();
  const selectedSprint = useUI((s) => s.selectedSprint);

  const isMobile = useIsMobile();
  // ENVIRONMENT used to be a hardcoded Staging/Production/Local. The backend
  // picks a run's base URL by case-insensitive NAME MATCH against the project's
  // own environments, which are free text — so a project whose environments are
  // "UAT"/"Dev" matched none of the three chips and fell back to the project
  // base URL with no warning, sending the run at the wrong host (#671).
  const { data: environments } = useProjectEnvironments();
  const envOptions = environments ?? [];
  // Settings' "Parallel workers" is the DEFAULT for a new run: the value is
  // persisted but no backend reader exists, so on its own it did nothing while
  // sitting next to a slider here that looked identical and did work (#672).
  const { data: settings } = useSettings();
  const parallelDefault = settings?.parallel;
  // Only this project's tickets (ADR 0015 §9). Containment, not a filter: the
  // scope pickers cannot offer another project's rows, which is what makes a
  // mixed-project run unreachable by construction. #727's server-side 400 stays as
  // a cheap invariant behind it — it is no longer the mechanism, but a picker is a
  // UI promise and the invariant is the enforcement.
  const projectGuid = useModalProjectGuid();
  const { data: ticketsPage } = useTickets({
    project: projectGuid ?? undefined,
    pageSize: ALL_TICKETS_PAGE_SIZE,
  });
  const tickets = ticketsPage?.items;
  const user = useAuth((s) => s.user);
  const userName = user ? `${user.firstName ?? ""} ${user.lastName ?? ""}`.trim() : "";
  const createRun = useCreateRun();

  // Link options. `dryRun` seeds from the workspace setting (#712) and can only be
  // tightened from there: the server ORs the two, so switching it off here cannot
  // undo a dry run someone turned on to protect a live board. Shown as locked in
  // that case rather than as a control that silently does nothing.
  const workspaceDryRun = settings?.dryRun ?? false;
  const [link, setLink] = useState(true);
  const [dryRun, setDryRun] = useState(false);
  const [linkSubset, setLinkSubset] = useState<Record<string, boolean>>({});
  const [subsetOpen, setSubsetOpen] = useState(false);

  // Seed the workers slider once per open. A ref rather than an `open`-only
  // dependency because the settings query may resolve after the modal is
  // already up, and re-seeding on every settings refetch would overwrite a
  // choice the user had just made for this run.
  const seededWorkers = useRef(false);
  useEffect(() => {
    if (!open) {
      seededWorkers.current = false;
      return;
    }
    if (seededWorkers.current || parallelDefault === undefined) return;
    seededWorkers.current = true;
    setRunField("runWorkers", parallelDefault);
  }, [open, parallelDefault, setRunField]);

  // Reset the link options every time the modal opens: they describe THIS run, and
  // carrying the previous run's dry-run choice forward silently is exactly the
  // second-copy-of-one-decision problem #737 removed from the Link screen.
  useEffect(() => {
    if (!open) return;
    setLink(true);
    setDryRun(false);
    setLinkSubset({});
    setSubsetOpen(false);
  }, [open]);

  // Keep the selection inside the real set: default to the first environment,
  // and clear a stale one rather than submitting a name nothing matches.
  useEffect(() => {
    if (!open || environments === undefined) return;
    if (envOptions.length === 0) {
      if (runEnv !== "") setRunField("runEnv", "");
    } else if (!envOptions.includes(runEnv)) {
      setRunField("runEnv", envOptions[0]);
    }
  }, [open, environments, envOptions, runEnv, setRunField]);

  if (!open) return null;

  // Every count and every picker below reads off THIS list, so nothing another
  // project owns can reach the run — including a selection left in the store from
  // before the user moved projects.
  const projectTickets = tickets ?? [];
  const selectedTickets = projectTickets.filter((t) => selected[t.externalId]);
  const selN = selectedTickets.length;
  const sprintName = selectedSprint?.name;
  const sprintTickets = sprintName ? projectTickets.filter((t) => t.sprint === sprintName) : [];
  const sprintN = sprintTickets.length;
  const assignedTickets = userName ? projectTickets.filter((t) => t.assignee === userName) : [];
  const assignedN = assignedTickets.length;
  // The tickets the chosen scope resolves to — what the link subset picks from.
  const scopeTickets =
    runScope === "selected" ? selectedTickets : runScope === "sprint" ? sprintTickets : assignedTickets;
  const subsetIds = scopeTickets.map((t) => t.externalId).filter((id) => linkSubset[id]);
  // An empty subset means "all of them", which is also what the API means by an
  // empty `linkTicketIds` — so the two agree without a second flag.
  const linkAll = subsetIds.length === 0;
  const effectiveDryRun = dryRun || workspaceDryRun;

  // "My assigned tickets" is only offered once an identity is configured.
  const scopeOptions = [
    { id: "selected" as const, label: "Selected tickets", sub: "Only the tickets you picked on the Tickets page", count: `${selN} selected` },
    {
      id: "sprint" as const,
      label: sprintName ? `Entire ${sprintName}` : "Entire sprint",
      sub: sprintName ? "Every ticket in the chosen sprint" : "Pick a sprint on the Tickets page first",
      count: `${sprintN} tickets`,
    },
    { id: "assigned" as const, label: "My assigned tickets", sub: "All tickets assigned to you", count: `${assignedN} tickets` },
  ].filter((o) => o.id !== "assigned" || !!userName);

  // A run can't start with no tickets.
  const canStart = scopeTickets.length > 0;

  const createSummary =
    (runScope === "selected" ? `${selN} selected tickets` : runScope === "sprint" ? `${sprintN} sprint tickets` : `${assignedN} assigned tickets`) +
    ` · ${FRAMEWORK}` +
    (runEnv ? ` · ${runEnv}` : "") +
    // Say it in the footer too. A dry run that is only visible if you scrolled the
    // options open is a dry run someone starts without knowing (#712's lesson).
    (effectiveDryRun ? ` · ${t("createRun.link.dryRunChip")}` : link ? "" : ` · ${t("createRun.link.noLink")}`);

  const handleStart = () => {
    if (!canStart) return;
    createRun.mutate(
      {
        scope: runScope,
        ticketIds: runScope === "selected" ? selectedTickets.map((t) => t.externalId) : [],
        framework: FRAMEWORK,
        browser: runBrowser,
        env: runEnv,
        workers: runWorkers,
        retryPolicy: runRetry,
        sprint: runScope === "sprint" ? selectedSprint?.name : undefined,
        // Link options (#732). Stored on the run; the Link stage reads them. The
        // server treats both as tightening constraints, so an ignored `dryRun:
        // false` here can never switch off a workspace-level dry run.
        link,
        dryRun,
        linkTicketIds: subsetIds,
        sprintPath: runScope === "sprint" ? selectedSprint?.path : undefined,
      },
      {
        onSuccess: (run) => {
          closeCreateRun();
          navigate(`/runs/${run.id}`);
        },
        onError: (err) => toast.error(err instanceof Error ? err.message : "Failed to create run"),
      },
    );
  };

  return (
    <div
      onClick={closeCreateRun}
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
            : "w-[min(600px,94vw)] overflow-hidden rounded-[22px] border border-white/[0.11]"
        }
        style={{ background: "rgba(22,22,30,.94)", backdropFilter: "blur(40px)", boxShadow: "0 40px 90px -20px rgba(0,0,0,.8)" }}
      >
        {isMobile && (
          <div className="flex shrink-0 justify-center pt-2.5">
            <span className="h-1 w-10 rounded-full bg-white/25" />
          </div>
        )}
        <div className="flex shrink-0 items-center gap-3 border-b border-white/[0.07] p-[20px_24px]">
          <div className="accent-gradient flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-[11px]">
            <Plus size={18} color="#fff" strokeWidth={2.4} />
          </div>
          <div className="flex-1">
            <div className="text-[17px] font-extrabold">{isMobile ? "New run" : "Create a Run"}</div>
            <div className="text-[12px] text-ink-dim">A batch QA session across one or many tickets</div>
          </div>
        </div>

        <div className={isMobile ? "flex-1 overflow-y-auto p-[20px]" : "max-h-[60vh] overflow-y-auto p-[22px_24px]"}>
          <div className="mb-2.5 text-[12px] font-semibold text-[#9494a6]">SCOPE</div>
          <div className="mb-5 flex flex-col gap-2">
            {scopeOptions.map((o) => {
              const on = runScope === o.id;
              return (
                <div
                  key={o.id}
                  onClick={() => setRunField("runScope", o.id)}
                  className="flex cursor-pointer items-center gap-[13px] rounded-[13px] border p-[14px]"
                  style={{
                    borderColor: on ? "rgba(139,92,246,.4)" : "rgba(255,255,255,.08)",
                    background: on ? "rgba(139,92,246,.1)" : "rgba(255,255,255,.03)",
                  }}
                >
                  <div
                    className="flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded-full border-2"
                    style={{ borderColor: on ? "#8b5cf6" : "rgba(255,255,255,.2)" }}
                  >
                    {on && <span className="h-2 w-2 rounded-full bg-[#8b5cf6]" />}
                  </div>
                  <div className="flex-1">
                    <div className="text-[14px] font-semibold">{o.label}</div>
                    <div className="text-[12px] text-ink-dim">{o.sub}</div>
                  </div>
                  <span className="text-[12px] font-bold text-violet">{o.count}</span>
                </div>
              );
            })}
          </div>

          <div className="mb-5 grid grid-cols-1 gap-4 md:grid-cols-2">
            <div>
              <div className="mb-2.5 text-[12px] font-semibold text-[#9494a6]">FRAMEWORK</div>
              <div className="flex gap-2">
                <span className="flex-1 rounded-[10px] bg-[rgba(139,92,246,.2)] px-2 py-[9px] text-center text-[12.5px] font-semibold text-white shadow-[inset_0_0_0_1px_rgba(139,92,246,.3)]">
                  {FRAMEWORK}
                </span>
              </div>
            </div>
            <div>
              <div className="mb-2.5 text-[12px] font-semibold text-[#9494a6]">ENVIRONMENT</div>
              {envOptions.length > 0 ? (
                <div className="flex flex-wrap gap-2">
                  {envOptions.map((e) => (
                    <button key={e} onClick={() => setRunField("runEnv", e)} className={segStyle(runEnv === e)}>
                      {e}
                    </button>
                  ))}
                </div>
              ) : (
                <div className="rounded-[10px] border border-white/[0.08] bg-white/[0.03] px-3 py-[9px] text-[12px] leading-relaxed text-ink-dim">
                  No environments configured — this run uses each project's base URL. Add
                  environments in Project settings.
                </div>
              )}
            </div>
          </div>

          <div className="flex items-center justify-between py-0.5">
            <div>
              <div className="text-[14px] font-semibold">Parallel workers</div>
              <div className="text-[12px] text-ink-dim">Execute up to {runWorkers} cases at once</div>
            </div>
            <div className="flex items-center gap-3">
              <input
                type="range"
                min={1}
                max={8}
                value={runWorkers}
                onChange={(e) => setRunField("runWorkers", Number(e.target.value))}
                className="w-[140px] accent-violet-500"
              />
              <span className="w-5 text-center font-mono text-[14px] font-bold">{runWorkers}</span>
            </div>
          </div>

          {/* Link options (#732, ADR 0015 §5). The Link stage is automatic and
              hidden now, so this is where its three decisions are made — and the
              only remaining route to a dry run other than the workspace setting. */}
          <div className="mt-5 border-t border-white/[0.07] pt-4" data-testid="create-run-link">
            <div className="mb-1 text-[12px] font-semibold text-[#9494a6]">
              {t("createRun.link.heading")}
            </div>
            <ToggleRow
              title={t("createRun.link.linkTitle")}
              description={t("createRun.link.linkDesc")}
              checked={link}
              onChange={setLink}
            />
            {/* Locked, not hidden, when the workspace is already in dry-run mode:
                the server ORs the two (#712), so an interactive switch here could
                only ever lie about turning it off. */}
            <div className={workspaceDryRun ? "pointer-events-none opacity-60" : ""}>
              <ToggleRow
                title={t("createRun.link.dryRunTitle")}
                description={
                  workspaceDryRun
                    ? t("createRun.link.dryRunLocked")
                    : t("createRun.link.dryRunDesc")
                }
                checked={effectiveDryRun}
                onChange={setDryRun}
              />
            </div>

            {link && scopeTickets.length > 1 && (
              <div className="pt-3">
                <button
                  type="button"
                  onClick={() => setSubsetOpen((v) => !v)}
                  className="text-[12.5px] font-semibold text-violet hover:underline"
                >
                  {linkAll
                    ? t("createRun.link.subsetAll", { count: scopeTickets.length })
                    : t("createRun.link.subsetSome", {
                        count: subsetIds.length,
                        total: scopeTickets.length,
                      })}
                </button>
                {subsetOpen && (
                  <div className="mt-2 flex max-h-[168px] flex-col gap-1 overflow-y-auto rounded-[12px] border border-white/[0.08] bg-white/[0.03] p-2">
                    {scopeTickets.map((ticket) => (
                      <label
                        key={ticket.externalId}
                        className="flex cursor-pointer items-center gap-2.5 rounded-[9px] px-2 py-1.5 text-[12.5px] hover:bg-white/[0.05]"
                      >
                        <input
                          type="checkbox"
                          checked={!!linkSubset[ticket.externalId]}
                          onChange={(e) =>
                            setLinkSubset((prev) => ({
                              ...prev,
                              [ticket.externalId]: e.target.checked,
                            }))
                          }
                          className="accent-violet-500"
                        />
                        <span className="font-mono text-[11.5px] text-violet">
                          {ticket.externalId}
                        </span>
                        <span className="min-w-0 flex-1 truncate text-ink-dim">{ticket.title}</span>
                      </label>
                    ))}
                  </div>
                )}
              </div>
            )}

            {effectiveDryRun && (
              <span
                className="mt-3 inline-flex items-center gap-2 rounded-full border border-[rgba(245,158,11,.3)] px-3 py-1.5 text-[11.5px] font-semibold text-warning-soft"
                style={{ background: "rgba(245,158,11,.1)" }}
                data-testid="create-run-dry-run"
              >
                <FlaskConical size={13} strokeWidth={2.2} />
                {t("createRun.link.dryRunChip")}
              </span>
            )}
          </div>
        </div>

        {isMobile ? (
          <div
            className="flex shrink-0 flex-col gap-2.5 border-t border-white/[0.07] p-[16px_20px]"
            style={{ paddingBottom: "calc(16px + env(safe-area-inset-bottom))" }}
          >
            <span className="text-center text-[12.5px] text-ink-dim">{createSummary}</span>
            <Button
              variant="primary"
              size="lg"
              className="w-full"
              onClick={handleStart}
              disabled={createRun.isPending || !canStart}
              title={!canStart ? "Select at least one ticket to run" : undefined}
            >
              {createRun.isPending ? "Starting…" : "Start run"}
            </Button>
          </div>
        ) : (
          <div className="flex items-center gap-[10px] border-t border-white/[0.07] p-[16px_24px]">
            <span className="flex-1 text-[12.5px] text-ink-dim">{createSummary}</span>
            <Button variant="glass" onClick={closeCreateRun}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={handleStart}
              disabled={createRun.isPending || !canStart}
              title={!canStart ? "Select at least one ticket to run" : undefined}
            >
              {createRun.isPending ? "Starting…" : "Start Run"}
            </Button>
          </div>
        )}
      </motion.div>
    </div>
  );
}
