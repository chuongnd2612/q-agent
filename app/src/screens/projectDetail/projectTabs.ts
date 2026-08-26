/**
 * The project's six tabs (ADR 0015 slice 2).
 *
 * Each tab is a **path segment**, not a `?tab=` query param, and the type lives
 * here rather than in `store/ui.ts`: the tab is navigation, and navigation never
 * lives in the store (CLAUDE.md). `?tab=` was the last navigation field left in
 * there.
 *
 * `tickets` and `runs` are back — they were deleted in #693 because they
 * navigated *out* of the project to the global lists, which is a different bug
 * from having them at all. Under containment they are real views of this
 * project's own rows (#727 gave the API the `?project=` filter they need).
 *
 * `connection` is where the project's provider bindings live. It renders the
 * existing settings tab for now; #732 rebuilds it as the three-role Connection
 * tab (TICKET SOURCE / CODE & KNOWLEDGE / TEST CASE TARGET).
 */
export const PROJECT_TABS = [
  { id: "overview", labelKey: "tabs.overview" },
  { id: "tickets", labelKey: "tabs.tickets" },
  { id: "runs", labelKey: "tabs.runs" },
  { id: "knowledge", labelKey: "tabs.knowledge" },
  { id: "connection", labelKey: "tabs.connection" },
  { id: "reports", labelKey: "tabs.reports" },
] as const;

export type ProjectTab = (typeof PROJECT_TABS)[number]["id"];

export const DEFAULT_PROJECT_TAB: ProjectTab = "overview";

/** The path segment a `?tab=` value maps to, for pre-#728 bookmarks.
 *
 * `settings` is folded into `connection` (the same bindings, renamed by the v2
 * design); `tickets`/`runs` were legal `?tab=` values before #693 removed them
 * and are legal again, so they map to themselves. Anything unrecognised reads as
 * `overview` rather than silently rendering a different tab than the URL claims.
 */
export function tabFromLegacyQuery(value: string | null): ProjectTab {
  if (!value) return DEFAULT_PROJECT_TAB;
  if (value === "settings") return "connection";
  const match = PROJECT_TABS.find((tab) => tab.id === value);
  return match ? match.id : DEFAULT_PROJECT_TAB;
}

/** The canonical path for one of a project's tabs. */
export function projectTabPath(projectGuid: string, tab: ProjectTab): string {
  return `/projects/${encodeURIComponent(projectGuid)}/${tab}`;
}
