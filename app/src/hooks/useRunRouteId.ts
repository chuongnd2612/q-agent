import { useLocation } from "react-router-dom";

/** Matches both the nested run route (ADR 0015) and the pre-#728 flat one, which
 * stays resolvable for bookmarks and redirects through `LegacyRunRedirect`.
 * Capture 1 is the project segment (absent on the flat form), capture 2 the run. */
const RUN_PATH = /^(?:\/projects\/([^/]+))?\/runs\/(\d+)/;

/**
 * The runId from the current URL when inside a run route, else null.
 * Unlike the old useResolvedRunId, this NEVER falls back to a "latest run" default —
 * run-scoped chrome must show nothing (or a picker) when no run is in the URL.
 */
export function useRunRouteId(): number | null {
  const { pathname } = useLocation();
  const m = pathname.match(RUN_PATH);
  return m ? Number(m[2]) : null;
}

/**
 * Build paths inside the current run, without every call site having to know the
 * route shape (ADR 0015 nested the run under its project).
 *
 * Returns a function so a component can produce several stage links from one
 * hook call. Passing no segment gives the run's root. Off a run route it falls
 * back to the flat form, which the legacy redirect resolves — so a stray caller
 * degrades to a redirect rather than to a broken link.
 *
 * Read from the pathname rather than `useParams`, deliberately: the app shell
 * renders in an ANCESTOR route of the run, and `useParams` only sees params
 * declared at or above the calling route — so `projectGuid` would come back
 * undefined for exactly the sidebar and stepper components that most need it.
 */
export function useRunPath(): (segment?: string) => string {
  const { pathname } = useLocation();
  const match = pathname.match(RUN_PATH);
  const projectSegment = match?.[1];
  const runId = match?.[2] ?? "";
  const base = projectSegment
    ? `/projects/${projectSegment}/runs/${runId}`
    : `/runs/${runId}`;
  return (segment?: string) => (segment ? `${base}/${segment}` : base);
}

/**
 * The project segment of the current run/project route, or null. Already
 * URL-encoded — it is taken verbatim from the pathname.
 */
export function useProjectRouteSegment(): string | null {
  const { pathname } = useLocation();
  return pathname.match(/^\/projects\/([^/]+)/)?.[1] ?? null;
}
