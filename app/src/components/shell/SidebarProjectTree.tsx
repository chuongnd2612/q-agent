import { ChevronRight } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useLocation, useNavigate } from "react-router-dom";
import { providerGlyph } from "@/components/ui/badges";
import { cn } from "@/lib/cn";
import {
  EMPTY_PROJECT_COUNTS,
  projectCountsKey,
  useProjectCounts,
  type ProjectCounts,
} from "@/hooks/queries";
import {
  PROJECT_TABS,
  projectTabPath,
  type ProjectTab,
} from "@/screens/projectDetail/projectTabs";
import type { ProjectOut } from "@/types/api";

/**
 * The sidebar's project tree (ADR 0015 §1, slice 3 / #729).
 *
 * The project is the container, so the sidebar lists **projects**, not global
 * ticket/run/report screens. Every project is an expandable row revealing the
 * same six tabs the project detail screen shows (`PROJECT_TABS` — imported, not
 * re-declared, so the tree and the tab bar can never disagree), with live counts
 * for Tickets and Runs and a pulsing badge for a run that is currently in
 * flight.
 *
 * **Counts have exactly one source** (ADR 0015 §8): `useProjectCounts()`, the
 * same hook the Dashboard comparison table reads. Runs, cases and the active run
 * come from ONE workspace-wide `GET /runs` grouped client-side by
 * `run.projectGuid`, so adding a project to the tree costs no extra runs
 * request. Nothing here reads a literal per-project total.
 *
 * Rendered by both `GlobalSidebar` (desktop) and `MobileDrawer` (mobile) so the
 * two presentations cannot drift.
 */
export function SidebarProjectTree({
  /** Called after a navigation — the mobile drawer uses it to close itself. */
  onNavigate,
  /** Tighter type scale for the desktop rail; the drawer wants touch targets. */
  variant = "desktop",
}: {
  onNavigate?: () => void;
  variant?: "desktop" | "mobile";
}) {
  const { t } = useTranslation(["nav", "projects"]);
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const { projects, byProject, isLoading } = useProjectCounts();

  // Which project (if any) the URL is inside, and which of its tabs. A run URL
  // (`/projects/:guid/runs/:runId`) reads as the Runs tab — the run belongs to
  // it — so the tree still shows where you are while a run is open.
  const routeSegment = pathname.match(/^\/projects\/([^/]+)/)?.[1];
  const routeKey = routeSegment ? decodeURIComponent(routeSegment) : null;
  const routeTab = pathname.match(/^\/projects\/[^/]+\/([^/]+)/)?.[1] ?? null;

  // Expansion is UI-only, so it lives in component state rather than the URL or
  // the store (CLAUDE.md: the store holds no navigation). `undefined` means "not
  // touched by the user", which falls back to *the project you are inside* — so
  // a cold load of the Dashboard shows every row collapsed, as specified.
  const [toggled, setToggled] = useState<Record<string, boolean>>({});

  const isCurrent = (p: ProjectOut) =>
    routeKey != null && (p.guid === routeKey || p.name === routeKey);

  const go = (path: string) => {
    navigate(path);
    onNavigate?.();
  };

  const mobile = variant === "mobile";

  return (
    <div data-tour="nav-project-tree" data-testid="sidebar-project-tree">
      <div className="flex items-center gap-2 px-2.5 pb-2 pt-3.5">
        <span className="text-[10px] font-semibold tracking-[0.11em] text-[#5c5c6e]">
          {t("nav:sections.projects")}
        </span>
        <span className="h-px flex-1 bg-white/[0.07]" />
        <span className="font-mono text-[10px] font-semibold text-[#5c5c6e]">
          {isLoading ? "" : (projects?.length ?? 0)}
        </span>
      </div>

      {!isLoading && !projects?.length && (
        <p className="m-0 px-2.5 pb-2 text-[11.5px] leading-relaxed text-[#6c6c7e]">
          {t("nav:tree.empty")}
        </p>
      )}

      {(projects ?? []).map((p) => {
        const key = projectCountsKey(p);
        const counts: ProjectCounts = byProject.get(key) ?? EMPTY_PROJECT_COUNTS;
        const current = isCurrent(p);
        const open = toggled[key] ?? current;
        const [glyph, glyphBg] = providerGlyph[p.providerKind] ?? ["?", "#6b7280"];
        const active = counts.activeRun;

        return (
          <div key={p.id} className="flex flex-col">
            <button
              data-testid="sidebar-project-row"
              data-project={key}
              aria-expanded={open}
              onClick={() => setToggled((s) => ({ ...s, [key]: !open }))}
              className={cn(
                "flex w-full items-center gap-2.5 rounded-[11px] border-none px-2.5 py-2 text-left font-semibold",
                mobile ? "text-[13.5px]" : "text-[13px]",
                current ? "text-white" : "text-[#c3c3d0] hover:bg-white/[0.06]",
              )}
              style={
                current
                  ? {
                      background: "rgba(139,92,246,.12)",
                      boxShadow: "inset 0 0 0 1px rgba(139,92,246,.2)",
                    }
                  : undefined
              }
            >
              <ChevronRight
                size={11}
                strokeWidth={2.6}
                className={cn(
                  "shrink-0 text-[#6c6c7e] transition-transform duration-200",
                  open && "rotate-90",
                )}
              />
              <span
                aria-hidden
                className="flex h-[19px] w-[19px] shrink-0 items-center justify-center rounded-[6px] text-[10px] font-black"
                style={{
                  background: glyphBg,
                  // GitHub's glyph plate is near-white; white-on-white would be
                  // invisible (same rule the Dashboard row uses).
                  color: p.providerKind === "github" ? "#12121a" : "#fff",
                }}
              >
                {glyph}
              </span>
              <span className="flex-1 truncate">{p.name}</span>
              {active && (
                <span
                  data-testid="sidebar-active-run"
                  title={t("nav:tree.activeRun", { code: active.code })}
                  className="flex shrink-0 items-center gap-1 rounded-full px-1.5 py-[2px] font-mono text-[9px] font-semibold"
                  style={{ background: "rgba(251,191,36,.14)", color: "#fbbf24" }}
                >
                  <span className="relative flex h-1 w-1 shrink-0">
                    <span
                      className="absolute inline-flex h-full w-full animate-ping rounded-full opacity-75"
                      style={{ background: "#fbbf24" }}
                    />
                    <span
                      className="relative inline-flex h-1 w-1 rounded-full"
                      style={{ background: "#fbbf24" }}
                    />
                  </span>
                  {active.code}
                </span>
              )}
            </button>

            {open && (
              <div className="ml-[17px] mb-[7px] mt-[3px] flex flex-col gap-px border-l border-white/[0.08] pl-3">
                {PROJECT_TABS.map((tab) => {
                  const on = current && routeTab === tab.id;
                  const count = tabCount(tab.id, counts);
                  return (
                    <button
                      key={tab.id}
                      data-testid={`sidebar-project-tab-${tab.id}`}
                      onClick={() => go(projectTabPath(key, tab.id))}
                      className={cn(
                        "flex w-full items-center gap-2 rounded-[9px] border-none px-2.5 py-[7px] text-left font-semibold",
                        mobile ? "text-[13px]" : "text-[12.5px]",
                        on ? "text-white" : "text-[#9a9aac] hover:bg-white/[0.05]",
                      )}
                      style={
                        on
                          ? {
                              background:
                                "linear-gradient(135deg,rgba(139,92,246,.22),rgba(99,102,241,.1))",
                              boxShadow: "inset 0 0 0 1px rgba(139,92,246,.26)",
                            }
                          : undefined
                      }
                    >
                      <span className="flex-1 truncate">{t(`projects:${tab.labelKey}`)}</span>
                      {count !== null && (
                        <span
                          data-testid={`sidebar-count-${tab.id}`}
                          className="font-mono text-[10px] font-semibold"
                          style={{ color: on ? "#c4b5fd" : "#6c6c7e" }}
                        >
                          {count}
                        </span>
                      )}
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/** Only Tickets and Runs carry a count (ADR 0015 §1). `null` renders nothing —
 *  which is also what a ticket total that has not landed yet shows, rather than
 *  a zero that would read as "this project has no tickets". */
function tabCount(tab: ProjectTab, counts: ProjectCounts): number | null {
  if (tab === "tickets") return counts.tickets;
  if (tab === "runs") return counts.runs;
  return null;
}
