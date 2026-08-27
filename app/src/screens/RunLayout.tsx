import { useEffect, useRef } from "react";
import { Navigate, Outlet, useLocation, useNavigate, useParams } from "react-router-dom";
import { RunSocketProvider } from "@/hooks/useRunEvents";
import { useExecution, useRun, useRunCases } from "@/hooks/queries";
import { Spinner } from "@/components/ui/misc";
import { useTranslation } from "react-i18next";
import { UNASSIGNED_PROJECT_SEGMENT } from "@/screens/LegacyRedirects";
import { RunOverlay } from "@/components/runs/RunOverlay";
import {
  RUN_STAGES,
  activeAutoStage,
  furthestStage as furthestStageOf,
  isTerminalStatus,
  readResumeStage,
  stageIndex,
  stageIndexForSegment,
  writeResumeStage,
  type RunStageKey,
} from "@/components/runs/runStages";

/**
 * The run wizard (ADR 0015 §4, #730) — layout for every run-scoped route,
 * `/projects/:projectGuid/runs/:runId/:stage`.
 *
 * Owns three things: the guard, the single run WebSocket, and the overlay
 * chrome that the stage screens render inside.
 *
 * ## The guard stays
 *
 * Slice 8 deletes the "pick a run" interstitial, not this. Unreachable-by-design
 * is not the same as unauthorised: run stages can no longer be *navigated to*
 * without a run, but a hand-typed or stale `:runId` still arrives, and only this
 * refuses it. Rejection lands on the project's Runs tab and never auto-selects a
 * run.
 *
 * ## Two state variables
 *
 * `run.status` is **furthest progress** and only moves forward (ADR 0005). The
 * **viewed stage** is the `:stage` path segment. Back is a pure URL change — it
 * fires no mutation, so walking back to Review cannot drag the server's status
 * with it. See `runStages.ts`.
 */
export function RunLayout() {
  const { runId, projectGuid } = useParams();
  const { pathname } = useLocation();
  const navigate = useNavigate();
  // The stage is the segment AFTER the run id. Read from the pathname rather
  // than declared as a `:stage` param so each stage keeps its own literal route
  // (and its own code-split element) instead of collapsing into one wildcard.
  const stageSegment = pathname.match(/\/runs\/\d+\/([^/?#]+)/)?.[1];
  const { t } = useTranslation("runs");
  const id = Number(runId);
  const valid = !Number.isNaN(id);
  const { data: run, isLoading, isError } = useRun(valid ? id : null);
  const { data: cases } = useRunCases(valid ? id : null);
  const { data: execution } = useExecution(valid ? id : null);

  // Where a refused run sends the user. A run with no resolvable project reaches
  // us under the `unassigned` sentinel (#727), which is not a project, so that
  // case falls back to the projects list.
  const projectRunsPath =
    projectGuid && projectGuid !== UNASSIGNED_PROJECT_SEGMENT
      ? `/projects/${encodeURIComponent(projectGuid)}/runs`
      : "/projects";

  const viewedStage = stageIndexForSegment(stageSegment);
  const furthest = furthestStageOf(run);
  const auto = activeAutoStage(run?.status);

  const stagePath = (key: RunStageKey) => {
    const target = RUN_STAGES.find((s) => s.key === key);
    return `/projects/${encodeURIComponent(projectGuid ?? "")}/runs/${id}/${target?.seg ?? "review"}`;
  };

  // Remember where the user left off, so reopening the run lands there. Resume
  // is NOT navigation — it is read once, when a run is opened without a stage —
  // so it deliberately does not live in the URL, and it has to survive a reload,
  // which rules out the in-memory store. See `runStages.ts`.
  useEffect(() => {
    if (valid && viewedStage != null) {
      writeResumeStage(id, RUN_STAGES[viewedStage].key);
    }
  }, [valid, id, viewedStage]);

  // Auto-advance past the hidden stages. `processing` (Analyze) and `sync`
  // (Link) have no stepper entry, so the wizard parks on the human stage each
  // one precedes and moves on by itself when the worker finishes.
  //
  // Fires on TRANSITIONS only, never continuously: pinning the view while an
  // auto stage runs would stop the user walking back to an earlier stage, and
  // "revisiting an earlier stage stays fully editable" is the rule this slice
  // exists to honour.
  const previousAuto = useRef<RunStageKey | null>(null);
  useEffect(() => {
    if (!run || viewedStage == null) return;
    const was = previousAuto.current;
    const now = auto?.stage ?? null;
    previousAuto.current = now;
    if (now && now !== was) {
      if (viewedStage !== stageIndex(now)) navigate(stagePath(now), { replace: true });
    } else if (!now && was) {
      const target = RUN_STAGES[furthest]?.key;
      if (target && viewedStage !== furthest) navigate(stagePath(target), { replace: true });
    }
    // `stagePath` is derived from params that are already dependencies.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.status, auto?.stage, furthest, viewedStage, navigate]);

  // ------------------------------------------------------------------ guard
  if (!valid || isError) return <Navigate to={projectRunsPath} replace />;

  if (isLoading || !run) {
    return (
      <div className="glass flex flex-1 items-center justify-center rounded-[22px] py-20">
        <Spinner size={22} />
      </div>
    );
  }

  // A run opened under the WRONG project — a stale link, or a run id pasted into
  // another project's URL. It exists, so this is not a 404; it just is not this
  // project's run, so send it to the project it does belong to rather than
  // rendering it under a heading that misattributes it.
  const stamped = run.projectGuid;
  const addressedProject = projectGuid ? decodeURIComponent(projectGuid) : null;
  if (stamped && addressedProject && stamped !== addressedProject) {
    return <Navigate to={`/projects/${encodeURIComponent(stamped)}/runs/${id}`} replace />;
  }

  // The run root with no stage in the URL — resume where the user left off,
  // falling back to how far the run itself has got.
  if (viewedStage == null) {
    const resumed = readResumeStage(id);
    const landing = resumed ?? RUN_STAGES[furthest]?.key ?? "review";
    return <Navigate to={stagePath(landing)} replace />;
  }

  // ------------------------------------------------------------------ gates
  const approvedCases = (cases ?? []).filter((c) => c.approval === "approved").length;
  const suiteFinished = execution?.status === "done";
  const currentKey = RUN_STAGES[viewedStage].key;

  // Only the two gates the design names. Everything else is walk-through: a gate
  // the user cannot satisfy and was never told about is worse than no gate.
  let blockedReason: string | null = null;
  if (currentKey === "review" && approvedCases === 0) {
    blockedReason = t("overlay.gate.needsApprovedCase");
  } else if (currentKey === "execution" && !suiteFinished) {
    blockedReason = t("overlay.gate.needsSuiteFinished");
  }

  const isLastStage = viewedStage === RUN_STAGES.length - 1;
  const nextDisabled = blockedReason != null;

  const goBack = () => {
    if (viewedStage > 0) navigate(stagePath(RUN_STAGES[viewedStage - 1].key));
  };
  const goNext = () => {
    if (nextDisabled) return;
    if (isLastStage) {
      navigate(projectRunsPath);
      return;
    }
    navigate(stagePath(RUN_STAGES[viewedStage + 1].key));
  };

  return (
    <RunSocketProvider runId={id}>
      <RunOverlay
        title={`${run.code} · ${run.name}`}
        subtitle={t("overlay.subtitle", {
          scope: run.scopeLabel,
          env: run.env,
          count: run.ticketIds.length,
        })}
        viewedStage={viewedStage}
        furthestStage={isTerminalStatus(run.status) ? RUN_STAGES.length : furthest}
        busyLabel={auto ? t(`overlay.${auto.labelKey}`) : null}
        onExit={() => navigate(projectRunsPath)}
        onBack={goBack}
        onNext={goNext}
        backDisabled={viewedStage === 0}
        nextDisabled={nextDisabled}
        nextLabel={isLastStage ? t("overlay.finish") : t("overlay.next")}
        nextHint={blockedReason}
      >
        <Outlet />
      </RunOverlay>
    </RunSocketProvider>
  );
}
