import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { FolderKanban } from "lucide-react";
import { providerGlyph } from "@/components/ui/badges";
import { Spinner } from "@/components/ui/misc";
import { confidenceColor, providerLabel } from "@/data/projects";
import { runBadge, runColor, runEffectiveStatus } from "@/components/dashboard/runStatus";
import type { ProjectCounts } from "@/hooks/queries";
import { EMPTY_PROJECT_COUNTS, projectCountsKey } from "@/hooks/queries";
import type { ProjectOut } from "@/types/api";

/** Shared column template — the header and every row use the same one, so they
 *  stay aligned while the whole table scrolls horizontally as one block. */
const COLUMNS =
  "minmax(190px,2fr) minmax(140px,1.4fr) 78px 96px 78px minmax(130px,1.3fr) minmax(120px,1.1fr)";

/**
 * The Dashboard's project comparison table (ADR 0015 §1, #733) — one row per
 * project, so a QA lead can compare projects at a glance and click through into
 * one. Replaces the old KPI-card grid as the screen's centre of gravity.
 *
 * Every figure comes from `useProjectCounts()`, the single source ADR 0015 §8
 * requires; nothing here reads a literal total off `project.meta`.
 *
 * The grid is wider than a narrow viewport, so it scrolls inside its own
 * `overflow-x-auto` container — the page body never scrolls sideways.
 */
export function ProjectComparisonTable({
  projects,
  byProject,
  isLoading,
  ticketsLoading,
}: {
  projects: ProjectOut[] | undefined;
  byProject: Map<string, ProjectCounts>;
  isLoading: boolean;
  ticketsLoading: boolean;
}) {
  const { t } = useTranslation("dashboard");
  const navigate = useNavigate();

  if (isLoading) {
    return (
      <div
        className="mb-4 flex justify-center rounded-[20px] py-10"
        style={{ background: "rgba(8,8,13,.72)", border: "1px solid rgba(255,255,255,.07)" }}
      >
        <Spinner />
      </div>
    );
  }

  if (!projects?.length) {
    return (
      <div
        className="mb-4 flex flex-col items-center rounded-[20px] px-6 py-12 text-center"
        style={{ background: "rgba(8,8,13,.72)", border: "1px solid rgba(255,255,255,.07)" }}
      >
        <div className="mb-4 flex h-[58px] w-[58px] items-center justify-center rounded-[18px] bg-white/[0.05]">
          <FolderKanban size={24} className="text-muted" />
        </div>
        <h2 className="m-0 mb-1.5 text-[17px] font-extrabold">
          {t("dashboard.projectTable.emptyTitle")}
        </h2>
        <p className="m-0 max-w-[360px] text-[13px] leading-relaxed text-ink-dim">
          {t("dashboard.projectTable.emptyBody")}
        </p>
      </div>
    );
  }

  return (
    <div
      className="mb-4 overflow-hidden rounded-[20px]"
      // Opaque, not glass: this panel is dense small text over the animated
      // shell backdrop (CLAUDE.md — no `backdrop-filter` here).
      style={{ background: "rgba(8,8,13,.72)", border: "1px solid rgba(255,255,255,.07)" }}
    >
      <div className="overflow-x-auto">
        <div className="min-w-[860px]">
          <div
            className="grid gap-3 px-5 py-3.5 text-[10px] font-bold uppercase tracking-[.08em] text-[#6c6c7e]"
            style={{
              gridTemplateColumns: COLUMNS,
              borderBottom: "1px solid rgba(255,255,255,.07)",
            }}
          >
            <span>{t("dashboard.projectTable.columns.project")}</span>
            <span>{t("dashboard.projectTable.columns.ticketSource")}</span>
            <span className="text-right">{t("dashboard.projectTable.columns.tickets")}</span>
            <span className="text-right">{t("dashboard.projectTable.columns.testCases")}</span>
            <span className="text-right">{t("dashboard.projectTable.columns.runs")}</span>
            <span>{t("dashboard.projectTable.columns.activeRun")}</span>
            <span>{t("dashboard.projectTable.columns.knowledge")}</span>
          </div>

          {projects.map((p) => {
            const key = projectCountsKey(p);
            const counts = byProject.get(key) ?? EMPTY_PROJECT_COUNTS;
            const [glyph, glyphBg] = providerGlyph[p.providerKind] ?? ["?", "#6b7280"];
            const active = counts.activeRun;
            const activeStatus = active ? runEffectiveStatus(active) : null;
            const conf = counts.confidence;
            return (
              <div
                key={p.id}
                role="button"
                tabIndex={0}
                data-testid="dash-project-row"
                onClick={() => navigate(`/projects/${encodeURIComponent(key)}`)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    navigate(`/projects/${encodeURIComponent(key)}`);
                  }
                }}
                className="grid cursor-pointer items-center gap-3 px-5 py-[15px] hover:bg-[rgba(139,92,246,.07)]"
                style={{
                  gridTemplateColumns: COLUMNS,
                  borderTop: "1px solid rgba(255,255,255,.045)",
                }}
              >
                {/* Project */}
                <div className="flex min-w-0 items-center gap-[11px]">
                  <span
                    className="flex h-[30px] w-[30px] shrink-0 items-center justify-center rounded-[9px] text-[13px] font-black"
                    style={{
                      background: glyphBg,
                      color: p.providerKind === "github" ? "#12121a" : "#fff",
                    }}
                  >
                    {glyph}
                  </span>
                  <span className="min-w-0">
                    <span className="block truncate text-[13.5px] font-bold">{p.name}</span>
                    <span className="block truncate font-mono text-[10.5px] text-[#7a7a8c]">
                      {p.externalId}
                    </span>
                  </span>
                </div>

                {/* Ticket source — the project's provider, read-only (ADR 0015 §3). */}
                <div className="min-w-0">
                  <div className="truncate text-[12.5px] font-semibold text-[#dcdce4]">
                    {providerLabel[p.providerKind] ?? p.providerKind}
                  </div>
                  <div className="truncate text-[10.5px] text-[#7a7a8c]">
                    {p.active
                      ? t("dashboard.projectTable.sourceActive")
                      : t("dashboard.projectTable.sourceInactive")}
                  </div>
                </div>

                {/* Tickets */}
                <span className="text-right font-mono text-[14px] font-extrabold">
                  {counts.tickets == null ? (ticketsLoading ? "…" : "—") : counts.tickets}
                </span>

                {/* Test cases */}
                <span className="text-right font-mono text-[14px] font-extrabold text-[#c4b5fd]">
                  {counts.cases}
                </span>

                {/* Runs */}
                <span className="text-right font-mono text-[14px] font-extrabold text-[#67e8f9]">
                  {counts.runs}
                </span>

                {/* Active run */}
                <div className="min-w-0">
                  {active && activeStatus ? (
                    <span className="flex items-center gap-[7px]">
                      <span
                        className="h-1.5 w-1.5 shrink-0 rounded-full"
                        style={{
                          background: runColor(activeStatus),
                          animation: "pulseDot 1.7s infinite",
                        }}
                      />
                      <span className="min-w-0">
                        <span className="block truncate font-mono text-[10.5px] font-semibold text-[#a78bfa]">
                          {active.code}
                        </span>
                        <span
                          className="block truncate text-[10.5px]"
                          style={{ color: runColor(activeStatus) }}
                        >
                          {runBadge(activeStatus).label}
                        </span>
                      </span>
                    </span>
                  ) : (
                    <span className="text-[12px] text-[#5c5c6e]">—</span>
                  )}
                </div>

                {/* Knowledge confidence */}
                <div className="flex items-center gap-2.5">
                  <span
                    className="w-[34px] text-[12.5px] font-bold"
                    style={{ color: conf == null ? "#5c5c6e" : confidenceColor(conf) }}
                  >
                    {conf == null ? "—" : `${conf}%`}
                  </span>
                  <span
                    className="h-[5px] flex-1 overflow-hidden rounded-[5px]"
                    style={{ background: "rgba(255,255,255,.08)" }}
                  >
                    <span
                      className="block h-full rounded-[5px]"
                      style={{
                        width: `${conf ?? 0}%`,
                        background: "linear-gradient(90deg,#8b5cf6,#22d3ee)",
                      }}
                    />
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
