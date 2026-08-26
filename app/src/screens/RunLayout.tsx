import { Navigate, Outlet, useParams } from "react-router-dom";
import { RunSocketProvider } from "@/hooks/useRunEvents";
import { useRun } from "@/hooks/queries";
import { Spinner } from "@/components/ui/misc";
import { UNASSIGNED_PROJECT_SEGMENT } from "@/screens/LegacyRedirects";

/**
 * Layout for every run-scoped route (`/projects/:projectGuid/runs/:runId/*`).
 * Coerces `:runId` to a number, confirms the run actually exists, and only then
 * mounts the single run WebSocket via `RunSocketProvider` (which persists across
 * intra-run navigation).
 *
 * The guard stays after ADR 0015 (slice 8 deletes the "pick a run" interstitial,
 * not this). Unreachable-by-design is not the same as unauthorised: run stages
 * can no longer be *navigated to* without a run, but a hand-typed or stale
 * `:runId` still arrives, and only this refuses it. Rejection now lands on the
 * project's Runs tab rather than the deleted global list — and never
 * auto-selects a run.
 */
export function RunLayout() {
  const { runId, projectGuid } = useParams();
  const id = Number(runId);
  const valid = !Number.isNaN(id);
  const { data: run, isLoading, isError } = useRun(valid ? id : null);

  // Where a refused run sends the user. A run with no resolvable project reaches
  // us under the `unassigned` sentinel (#727), which is not a project, so that
  // case falls back to the projects list.
  const fallback =
    projectGuid && projectGuid !== UNASSIGNED_PROJECT_SEGMENT
      ? `/projects/${encodeURIComponent(projectGuid)}/runs`
      : "/projects";

  // Invalid id or a run that doesn't exist → the project's runs (no auto-select).
  if (!valid || isError) return <Navigate to={fallback} replace />;

  // Don't mount the WebSocket until the run is confirmed to exist.
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
    return (
      <Navigate to={`/projects/${encodeURIComponent(stamped)}/runs/${id}`} replace />
    );
  }

  return (
    <RunSocketProvider runId={id}>
      <Outlet />
    </RunSocketProvider>
  );
}
