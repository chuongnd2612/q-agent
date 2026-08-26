import { Navigate, useLocation, useParams } from "react-router-dom";
import { useRun } from "@/hooks/queries";
import { Spinner } from "@/components/ui/misc";

/**
 * Compatibility redirects for the pre-#728 flat routes (ADR 0015 slice 2).
 *
 * Containment moved every ticket-, run- and report-shaped list *inside* a
 * project. The old flat URLs are still in bookmarks, in the tour's step routes
 * and in links people have pasted at each other, so they resolve rather than
 * 404 — but they cannot resolve to the same thing, because "the runs list" no
 * longer names one list.
 */

/** The sentinel project segment for a run that belongs to no resolvable project.
 *
 * #727 backfilled `Run.project_guid` for everything it could resolve, so a NULL
 * means the walk genuinely found no project — an install whose project is only
 * *indexed*, for instance. Those runs still have to be openable, so they get a
 * project segment that is not a GUID instead of being made unreachable. It
 * matches the API's `?project=unassigned` bucket, which exists for the same
 * reason.
 */
export const UNASSIGNED_PROJECT_SEGMENT = "unassigned";

/**
 * `/runs/:runId/*` → `/projects/<project>/runs/:runId/*`.
 *
 * The run knows its own project now (`run.projectGuid`, #727), which is exactly
 * why this redirect can exist at all: before slice 1 there was no way to answer
 * "which project is this run in?" from the run alone.
 *
 * This is also what keeps the ~30 in-run `navigate("/runs/" + runId + "/…")`
 * call sites working untouched. Slice 4 replaces them when the overlay lands;
 * until then the redirect is the bridge, not a leftover.
 */
export function LegacyRunRedirect() {
  const { runId } = useParams();
  const { pathname } = useLocation();
  const id = Number(runId);
  const valid = !Number.isNaN(id);
  const { data: run, isLoading, isError } = useRun(valid ? id : null);

  // A nonexistent or invalid run has no project to nest under. Send it to the
  // projects list rather than guessing one — the run guard (RunLayout) refuses
  // the same case for the same reason.
  if (!valid || isError) return <Navigate to="/projects" replace />;

  if (isLoading || !run) {
    return (
      <div className="glass flex flex-1 items-center justify-center rounded-[22px] py-20">
        <Spinner size={22} />
      </div>
    );
  }

  // Everything after `/runs/:runId` — the stage segment and any query string.
  const rest = pathname.replace(new RegExp(`^/runs/${runId}`), "");
  const project = run.projectGuid ?? UNASSIGNED_PROJECT_SEGMENT;
  return (
    <Navigate to={`/projects/${encodeURIComponent(project)}/runs/${id}${rest}`} replace />
  );
}

/**
 * `/tickets`, `/runs`, `/reports` → `/projects`.
 *
 * These had no project in the URL and there is deliberately no "current
 * project" to fall back on — ADR 0015 removed the project pill precisely because
 * it was an implicit switcher, and CLAUDE.md's rule against "the latest run"
 * applies to projects for the same reason: a silent default sends the user
 * somewhere they did not ask for and cannot see. So the honest nested equivalent
 * of an unscoped list is "choose the project first".
 *
 * The cross-project question these lists *did* answer — what is running right
 * now — moves to the Dashboard (#733), not here.
 */
export function LegacyListRedirect() {
  return <Navigate to="/projects" replace />;
}
