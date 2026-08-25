import { useEffect, useRef } from "react";
import { motion } from "framer-motion";
import { Plus } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { toast } from "@/lib/toast";
import { Button } from "@/components/ui/Button";
import {
  ALL_TICKETS_PAGE_SIZE,
  useCreateRun,
  useProjectEnvironments,
  useSettings,
  useTickets,
} from "@/hooks/queries";
import { useIsMobile } from "@/hooks/useIsMobile";
import { useAuth } from "@/store/auth";
import { useUI } from "@/store/ui";

/** The only framework there is. Selenium used to be offered here and was
 *  persisted to `Run.framework`, but every spec is stamped "Playwright" on the
 *  way in and only Playwright ever executes — so picking it silently gave you
 *  Playwright anyway (#671). Shown as a static pill, not a choice. */
const FRAMEWORK = "Playwright";

const segStyle = (on: boolean) =>
  "flex-1 rounded-[10px] border-none px-2 py-[9px] text-[12.5px] font-semibold cursor-pointer " +
  (on ? "bg-[rgba(139,92,246,.2)] text-white shadow-[inset_0_0_0_1px_rgba(139,92,246,.3)]" : "bg-white/[0.05] text-[#a0a0b2]");

/** Create-Run modal: scope/framework/env/workers, wired to POST /runs. */
export function CreateRunModal() {
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
  const { data: ticketsPage } = useTickets({ pageSize: ALL_TICKETS_PAGE_SIZE });
  const tickets = ticketsPage?.items;
  const user = useAuth((s) => s.user);
  const userName = user ? `${user.firstName ?? ""} ${user.lastName ?? ""}`.trim() : "";
  const createRun = useCreateRun();

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

  const selN = Object.values(selected).filter(Boolean).length;
  const sprintName = selectedSprint?.name;
  const sprintN = sprintName ? (tickets ?? []).filter((t) => t.sprint === sprintName).length : 0;
  const assignedN = userName ? (tickets ?? []).filter((t) => t.assignee === userName).length : 0;

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

  // Tickets the chosen scope resolves to — a run can't start with none.
  const scopeTicketCount = runScope === "selected" ? selN : runScope === "sprint" ? sprintN : assignedN;
  const canStart = scopeTicketCount > 0;

  const createSummary =
    (runScope === "selected" ? `${selN} selected tickets` : runScope === "sprint" ? `${sprintN} sprint tickets` : `${assignedN} assigned tickets`) +
    ` · ${FRAMEWORK}` +
    (runEnv ? ` · ${runEnv}` : "");

  const handleStart = () => {
    if (!canStart) return;
    createRun.mutate(
      {
        scope: runScope,
        ticketIds: runScope === "selected" ? Object.keys(selected).filter((k) => selected[k]) : [],
        framework: FRAMEWORK,
        browser: runBrowser,
        env: runEnv,
        workers: runWorkers,
        retryPolicy: runRetry,
        sprint: runScope === "sprint" ? selectedSprint?.name : undefined,
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
