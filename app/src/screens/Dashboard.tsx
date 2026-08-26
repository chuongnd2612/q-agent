import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { Sparkles } from "lucide-react";
import { GlassCard } from "@/components/ui/GlassCard";
import { Spinner } from "@/components/ui/misc";
import {
  runColor,
  runEffectiveStatus,
  runMeta,
  runRateLabel,
  timeAgo,
} from "@/components/dashboard/runStatus";
import { KpiStrip, type Kpi } from "@/screens/dashboard/KpiStrip";
import { ProjectComparisonTable } from "@/screens/dashboard/ProjectComparisonTable";
import { RunningNow } from "@/screens/dashboard/RunningNow";
import {
  useAuditEvents,
  useProjectCounts,
  useReports,
  useRunCases,
  useRuns,
} from "@/hooks/queries";
import { useAuth } from "@/store/auth";
import type { ProjectOut } from "@/types/api";

const initials = (name: string) =>
  name.split(" ").map((w) => w[0]).join("").slice(0, 2).toUpperCase() || "?";

// Icon-chip colours per actor type, matching the design palette.
const ACTOR_BG: Record<string, string> = {
  ai: "rgba(139,92,246,.16)",
  user: "rgba(16,185,129,.14)",
  system: "rgba(147,197,253,.16)",
};
const ACTOR_FG: Record<string, string> = {
  ai: "#a78bfa",
  user: "#6ee7b7",
  system: "#93c5fd",
};

/** Runs a worker will never advance further (ADR 0005) — i.e. not "running now". */
const TERMINAL = new Set(["done", "cancelled", "failed"]);

/**
 * Dashboard — the workspace's project comparison table (ADR 0015 §1, #733).
 *
 * Three things, in this order:
 *  1. a compressed KPI strip in the header (the old four-card grid, condensed);
 *  2. **"Running now"**, the one cross-project view in the app. Slice 3 removes
 *     the global Runs list from the sidebar, so this is where "what is executing
 *     right now" gets answered — it reads `useRuns()` with **no** project
 *     argument, workspace-wide, deliberately;
 *  3. the per-project comparison table, then the activity feed and latest runs.
 *
 * Every per-project figure comes from `useProjectCounts()` — the single counts
 * source ADR 0015 §8 mandates, shared with the sidebar tree, the Projects cards
 * and the project Overview. Nothing here reads `project.meta`.
 */
export function Dashboard() {
  const { t } = useTranslation("dashboard");
  const navigate = useNavigate();
  // Workspace-wide on purpose: "running now" is cross-project (ADR 0015 §1).
  const { data: runs, isLoading: runsLoading } = useRuns();
  const { data: reports } = useReports();
  const { data: activity } = useAuditEvents({});
  const { projects, byProject, isLoading: countsLoading, ticketsLoading } = useProjectCounts();
  const user = useAuth((s) => s.user);
  const firstName = user?.firstName?.trim() ?? "";

  // Runs sorted newest-first, reused by "running now" and the latest-runs list.
  const recentRuns = useMemo(
    () =>
      [...(runs ?? [])].sort(
        (a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime(),
      ),
    [runs],
  );
  const liveRuns = useMemo(
    () => recentRuns.filter((r) => !TERMINAL.has(r.status)),
    [recentRuns],
  );
  // The newest run backs the "cases in review" KPI, kept from the old card grid.
  const newestRun = recentRuns[0] ?? null;
  const { data: newestRunCases } = useRunCases(newestRun?.id ?? null);
  const projectsByGuid = useMemo(() => {
    const m = new Map<string, ProjectOut>();
    for (const p of projects ?? []) if (p.guid) m.set(p.guid, p);
    return m;
  }, [projects]);

  // Aggregate real report metrics; show em dash when no reports exist yet.
  const reportCount = reports?.length ?? 0;
  const passRateLabel = reportCount
    ? `${(reports!.reduce((sum, r) => sum + r.passRate, 0) / reportCount).toFixed(1)}%`
    : "—";
  const avgRuntimeLabel = reportCount
    ? `${Math.round(reports!.reduce((sum, r) => sum + r.durationS, 0) / reportCount)}s`
    : "—";
  const acrossLabel = reportCount
    ? t("dashboard.stats.acrossReports", { count: reportCount })
    : t("dashboard.stats.noReportsYet");

  // Suite health, previously the ring panel — folded into the strip's captions
  // rather than dropped when the KPI grid was compressed.
  const suitePassed = reports?.reduce((sum, r) => sum + r.passed, 0) ?? 0;
  const suiteFailed = reports?.reduce((sum, r) => sum + r.failed, 0) ?? 0;
  const suiteTotal = suitePassed + suiteFailed;
  const suitePassRate = suiteTotal ? (suitePassed / suiteTotal) * 100 : null;

  const reviewRuns = (runs ?? []).filter((r) => r.status === "review");
  const casesInReview =
    newestRunCases?.filter((c) => c.approval === "pending").length ?? 0;

  const kpis: Kpi[] = [
    {
      label: t("dashboard.stats.activeRuns"),
      value: runsLoading ? "—" : String(liveRuns.length),
      caption: reviewRuns[0]
        ? t("dashboard.stats.inReviewWithCode", { code: reviewRuns[0].code })
        : t("dashboard.stats.allCaughtUp"),
      color: "#a78bfa",
    },
    {
      label: t("dashboard.stats.casesInReview"),
      value: newestRun ? String(casesInReview) : "—",
      caption: newestRun
        ? t("dashboard.stats.inCode", { code: newestRun.code })
        : t("dashboard.stats.noRunsYet"),
      color: "#22d3ee",
    },
    {
      label: t("dashboard.stats.passRate"),
      value: passRateLabel,
      caption: acrossLabel,
      color: "#8b5cf6",
    },
    {
      label: t("dashboard.stats.avgRuntime"),
      value: avgRuntimeLabel,
      caption: acrossLabel,
      color: "#f59e0b",
    },
    {
      label: t("dashboard.suiteHealth.title"),
      value: suitePassRate == null ? "—" : `${suitePassRate.toFixed(1)}%`,
      caption: t("dashboard.suiteHealth.passedFailed", {
        passed: suitePassed.toLocaleString(),
        failed: suiteFailed.toLocaleString(),
      }),
      color: "#6ee7b7",
    },
  ];

  return (
    <div className="px-1 pb-10 pt-0.5">
      <div className="mb-4 flex flex-col gap-3.5">
        <div>
          <div className="mb-[5px] text-[13px] font-medium text-muted">
            {new Date().toLocaleDateString("en-US", {
              weekday: "long",
              month: "long",
              day: "numeric",
            })}{" "}
            · {t("dashboard.greeting")}
            {firstName ? `, ${firstName}` : ""} · {t("dashboard.subtitle")}
          </div>
          <h1 className="m-0 text-[26px] font-black tracking-tight md:text-[32px]">
            {t("dashboard.missionControl")}
          </h1>
        </div>
        <KpiStrip items={kpis} />
      </div>

      <RunningNow runs={liveRuns} projectNames={projectsByGuid} />

      <ProjectComparisonTable
        projects={projects}
        byProject={byProject}
        isLoading={countsLoading}
        ticketsLoading={ticketsLoading}
      />

      <div className="grid grid-cols-1 gap-3.5 md:grid-cols-2">
        <GlassCard tilt className="p-5">
          <div className="mb-4 text-[15px] font-bold">{t("dashboard.recentActivity.title")}</div>
          <div className="flex flex-col gap-0.5">
            {(activity ?? []).length === 0 ? (
              <p className="m-0 px-1.5 py-3 text-[12.5px] text-ink-dim">
                {t("dashboard.recentActivity.empty")}
              </p>
            ) : (
              (activity ?? []).slice(0, 5).map((e) => (
                <div
                  key={e.id}
                  className="flex gap-[13px] rounded-xl px-1.5 py-2.5 hover:bg-white/[0.04]"
                >
                  <div
                    className="flex h-[30px] w-[30px] shrink-0 items-center justify-center rounded-[9px]"
                    style={{ background: ACTOR_BG[e.actorType] ?? ACTOR_BG.system }}
                  >
                    {e.actorType === "ai" ? (
                      <Sparkles size={15} color={ACTOR_FG.ai} strokeWidth={2.2} />
                    ) : (
                      <span
                        className="text-[10px] font-bold"
                        style={{ color: ACTOR_FG[e.actorType] ?? ACTOR_FG.system }}
                      >
                        {initials(e.actor)}
                      </span>
                    )}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="text-[13px] text-[#dcdce4]">
                      <span className="font-bold">{e.actor}</span> {e.action}
                      {e.target ? ` · ${e.target}` : ""}
                    </div>
                    <div className="mt-0.5 text-[11.5px] text-[#7a7a8c]">{timeAgo(e.ts)}</div>
                  </div>
                </div>
              ))
            )}
          </div>
        </GlassCard>

        <GlassCard tilt className="p-5">
          <div className="mb-4 flex items-center">
            <span className="flex-1 text-[15px] font-bold">{t("dashboard.recentRuns.title")}</span>
          </div>
          {runsLoading ? (
            <div className="flex justify-center py-6">
              <Spinner />
            </div>
          ) : recentRuns.length === 0 ? (
            <div className="flex flex-col items-center gap-2 py-6 text-center">
              <Sparkles size={20} className="text-muted" />
              <p className="m-0 text-[12.5px] text-ink-dim">{t("dashboard.recentRuns.empty")}</p>
            </div>
          ) : (
            <div className="flex flex-col gap-[9px]">
              {recentRuns.slice(0, 4).map((r) => {
                const color = runColor(runEffectiveStatus(r));
                const project = r.projectGuid ? projectsByGuid.get(r.projectGuid) : undefined;
                return (
                  <div
                    key={r.id}
                    onClick={() => navigate(`/runs/${r.id}`)}
                    className="flex cursor-pointer items-center gap-3 rounded-[14px] p-3"
                    style={{
                      background: "rgba(255,255,255,.03)",
                      border: "1px solid rgba(255,255,255,.05)",
                    }}
                  >
                    <span
                      className="h-[9px] w-[9px] shrink-0 rounded-full"
                      style={{ background: color, boxShadow: `0 0 10px ${color}` }}
                    />
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-[13px] font-semibold">
                        {r.code} · {r.name}
                      </div>
                      <div className="truncate font-mono text-[11px] text-[#7a7a8c]">
                        {project ? `${project.name} · ` : ""}
                        {runMeta(r)}
                      </div>
                    </div>
                    <div className="text-right">
                      <div className="text-[13px] font-extrabold" style={{ color }}>
                        {runRateLabel(runEffectiveStatus(r))}
                      </div>
                      <div className="text-[10.5px] text-[#7a7a8c]">{timeAgo(r.createdAt)}</div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </GlassCard>
      </div>
    </div>
  );
}
