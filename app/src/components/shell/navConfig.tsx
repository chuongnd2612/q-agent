import {
  Boxes,
  FolderKanban,
  GraduationCap,
  LayoutDashboard,
  Laptop,
  Settings,
  ShieldCheck,
  Users,
  type LucideProps,
} from "lucide-react";
import { type ComponentType } from "react";

/**
 * Shared navigation definitions for the app shell. The desktop `GlobalSidebar`
 * and the mobile `MobileDrawer` render the SAME routes — this module is the
 * single source of truth for the nav groups so the two presentations can never
 * drift.
 *
 * There is no in-run branch any more (#734). The sidebar used to swap into a
 * "run workspace" mode, so this module also carried the pipeline-as-navigation
 * (`PIPELINE`) and a pinned global mini-row (`GLOBAL_MINI`) for getting back
 * out of it. A run is now a full-screen overlay with its own stepper
 * (`components/runs/runStages.ts`), so both are gone along with the mode.
 */
export interface NavItem {
  path: string;
  /** English source label — kept for the stable `data-tour` id and as fallback. */
  label: string;
  /** i18n key into the `nav` namespace (`items.<key>`); see ADR 0011. */
  key: string;
  /** Explicit `data-tour` id. Without one the id is derived from `label`, so
   *  renaming a label silently breaks the tour step that targets it — which is
   *  exactly what "Projects" → "All projects" would have done (#729). */
  tourId?: string;
  icon: ComponentType<{ size?: number; strokeWidth?: number }>;
}

/** The `data-tour` id for a nav item: explicit when given, else derived from the
 *  English source label. */
export const navTourId = (n: NavItem): string =>
  n.tourId ?? `nav-${n.label.toLowerCase().replace(/\s+/g, "-")}`;

/**
 * Global-only navigation, rendered ABOVE the project tree.
 *
 * Under ADR 0015 the project is the container, so nothing ticket- or run-shaped
 * exists at workspace level any more: the global **Tickets / Runs / Reports**
 * entries are gone (#729) and those screens are reached as a project's own tabs,
 * via the sidebar project tree (`SidebarProjectTree`) or the Projects list. The
 * cross-project "what is running right now" question they used to answer moved
 * to the Dashboard's comparison table (#733) — removing the lists without that
 * would have been a regression.
 *
 * Run-scoped screens (Review / Automation / …) never appear here either, which
 * is what prevents opening one without a run.
 */
export const PRIMARY_NAV: NavItem[] = [
  { path: "/", label: "Dashboard", key: "dashboard", icon: LayoutDashboard },
];

/** The "system" group, rendered BELOW the project tree. */
export const SECONDARY_NAV: NavItem[] = [
  {
    path: "/projects",
    label: "All projects",
    key: "allProjects",
    // Was labelled "Projects"; the product tour targets it by its old id.
    tourId: "nav-projects",
    icon: FolderKanban,
  },
  { path: "/getting-started", label: "Getting Started", key: "gettingStarted", icon: GraduationCap },
  { path: "/local-agent", label: "Local Agent", key: "localAgent", icon: Laptop },
  { path: "/settings", label: "Settings", key: "settings", icon: Settings },
];

/** Claude credentials nav icon — the Claude sunburst as a monochrome *stroked*
 * line icon (`currentColor`), matching the design's nav treatment and the other
 * line icons in the rail. NOT the filled brand-orange `ClaudeLogo`. */
export const ClaudeNavIcon = ({ size = 18, strokeWidth = 2 }: LucideProps) => (
  <svg
    width={size}
    height={size}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={strokeWidth}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <path d="M12 2.4l2.6 6.6 6.9.4-5.3 4.4 1.8 6.7L12 17.3 6 20.9l1.8-6.7L2.5 9.4l6.9-.4z" />
  </svg>
);

/** Admin-only navigation — rendered in a dedicated, gated ADMIN section. */
export const ADMIN_NAV: NavItem[] = [
  { path: "/settings/users", label: "Users", key: "users", icon: Users },
  { path: "/settings/claude-credentials", label: "Claude credentials", key: "claudeCredentials", icon: ClaudeNavIcon },
  { path: "/settings/shared-workspace", label: "Shared workspace", key: "sharedWorkspace", icon: Boxes },
  { path: "/audit", label: "Audit Log", key: "auditLog", icon: ShieldCheck },
];

/**
 * Pick the ONE nav path that best matches the current URL. The admin pages live
 * UNDER /settings/* (e.g. /settings/claude-credentials), so a naive
 * `startsWith("/settings")` would light up both "Settings" AND the admin item.
 * Instead we pick the item whose path is the *longest* boundary-aware match, so
 * a nested route highlights only its own item — never its ancestor.
 */
export function activeNavPath(items: NavItem[], pathname: string): string | null {
  const matchLength = (path: string): number => {
    if (path === "/") return pathname === "/" ? 0 : -1;
    return pathname === path || pathname.startsWith(`${path}/`) ? path.length : -1;
  };
  return items.reduce<{ path: string | null; len: number }>(
    (best, n) => {
      const len = matchLength(n.path);
      return len > best.len ? { path: n.path, len } : best;
    },
    { path: null, len: -1 },
  ).path;
}
