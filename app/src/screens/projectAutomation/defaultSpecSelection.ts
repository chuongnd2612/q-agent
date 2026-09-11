import { ticketOf } from "@/screens/automation/projectFiles";
import type { ProjectFileMeta, RunOut } from "@/types/api";

/**
 * Which specs in a shared automation repo belong to **this** project (#800).
 *
 * ## The problem this solves
 *
 * An `AutomationProject` is keyed on `(owner, provider project key, repo)`, so
 * one repo is legitimately written to by runs from several q-agent projects —
 * which is why the Automation tab's tree is deliberately unfiltered (#765). The
 * run bar, though, must never post another project's specs, so "run everything
 * in the repo" is not an option and the default selection has to be attributed.
 *
 * ## How attribution is derived, and why this way
 *
 * The authoritative provenance (#769) is the join
 * `AutomationSpec -> TestCase -> Run`, and the API exposes it **one file at a
 * time**, on `GET …/file?path=` — deriving a default selection from it would
 * cost one request per spec in the repo, which is exactly the eager-loading
 * #768 removed. So the same lineage is reconstructed client-side from its two
 * ends, both of which the SPA already holds:
 *
 * 1. `GET /runs?project=<guid>` gives every run **of this project**, each with
 *    its `ticketIds` — the `Run.project_guid` leg of the join, server-stamped.
 * 2. Since #540 a project-backed spec's path is `tests/<TICKET>/<file>.spec.ts`,
 *    so `ticketOf(path)` is the `TestCase -> ticket` leg.
 *
 * A spec is selected when its ticket directory is one this project's runs
 * covered.
 *
 * ## It is deliberately conservative, in one direction only
 *
 * Every uncertainty resolves to **not selected**:
 *
 * * a spec with no ticket directory (a legacy bare path, or one written straight
 *   into `tests/`) carries nothing to attribute, so it is left out rather than
 *   guessed in;
 * * a ticket this project's runs never mention is another project's, or a
 *   hand-added file — left out;
 * * while the runs query is still loading, the set is empty and nothing is
 *   pre-ticked.
 *
 * That asymmetry is the point: a missed spec is a checkbox the user ticks, while
 * a wrongly-included one silently runs another project's suite. Nothing here is
 * a filter on what is *shown* — the tree stays whole, and the panel says in one
 * line that the repo holds other projects' specs and they are not selected.
 *
 * @param files Every row of the repo's tree (all kinds; non-specs are ignored).
 * @param runs This project's runs, or undefined while they load.
 * @returns The repo-relative posix paths to pre-tick, in tree order.
 */
export function defaultSpecSelection(
  files: ProjectFileMeta[],
  runs: RunOut[] | undefined,
): string[] {
  if (!runs || runs.length === 0) return [];
  const tickets = new Set<string>();
  for (const run of runs) {
    for (const id of run.ticketIds ?? []) {
      const value = id.trim();
      if (value) tickets.add(value);
    }
  }
  if (tickets.size === 0) return [];
  return files
    .filter((f) => f.kind === "spec" && ticketMatches(ticketOf(f.path), tickets))
    .map((f) => f.path);
}

/**
 * Whether a spec's ticket directory names one of this project's ticket ids.
 *
 * Exact match first. The suffix legs exist because a provider id is not written
 * the same way everywhere: a run can carry the bare ADO id (`1428`) while the
 * spec directory carries the prefixed form the generator used (`SUR-1428`), and
 * vice versa — the same mismatch `useRunSocket` already compensates for when it
 * matches an `exec.case.result` back to a row. The separator is required on both
 * legs, so `428` can never match `SUR-1428`.
 *
 * @param ticket The spec's ticket directory, or "" when it has none.
 * @param tickets Ticket external ids drawn from this project's runs.
 */
function ticketMatches(ticket: string, tickets: Set<string>): boolean {
  if (!ticket) return false;
  if (tickets.has(ticket)) return true;
  for (const id of tickets) {
    if (ticket.endsWith(`-${id}`) || id.endsWith(`-${ticket}`)) return true;
  }
  return false;
}
