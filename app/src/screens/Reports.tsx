import { motion } from "framer-motion";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Download } from "lucide-react";
import { toast } from "@/lib/toast";
import { GlassCard } from "@/components/ui/GlassCard";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/misc";
import { runColor, runEffectiveStatus, timeAgo } from "@/components/dashboard/runStatus";
import { useReports, useRuns } from "@/hooks/queries";

interface FlakyRow {
  id: string;
  name: string;
  runs: string;
  rate: string;
}

export function Reports() {
  const { t } = useTranslation("reports");
  const { t: tCommon } = useTranslation("common");
  const { data: reports, isLoading: reportsLoading, isError: reportsError } = useReports();
  const { data: runs, isLoading: runsLoading } = useRuns();
  const navigate = useNavigate();

  // Everything on this screen is derived from real run reports (GET /reports).
  // Newest first for the summary cards; oldest→newest (last 7) for the trend.
  const byNewest = [...(reports ?? [])].sort(
    (a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime(),
  );
  const latest = byNewest[0] ?? null;
  const previous = byNewest[1] ?? null;

  const passRate = latest ? latest.passRate : null;
  const avgDuration = latest
    ? `${Math.round(latest.durationS / 60)}m ${Math.round(latest.durationS % 60)}s`
    : "—";

  // Pass-rate delta vs the previous report (percentage points); omitted when
  // there aren't two reports to compare.
  const passRateDelta = latest && previous ? latest.passRate - previous.passRate : null;

  // Trend bars: real pass rate per report over the last (up to) 7 reports.
  const trend = [...(reports ?? [])]
    .sort((a, b) => new Date(a.createdAt).getTime() - new Date(b.createdAt).getTime())
    .slice(-7)
    .map((r) => ({
      key: r.id,
      d: new Date(r.createdAt).toLocaleDateString(undefined, { month: "numeric", day: "numeric" }),
      v: Math.max(0, Math.min(1, r.passRate / 100)),
    }));
  const canTrend = trend.length >= 2;

  // Flaky tests come from the latest report's `data` blob when present.
  const flaky = (latest?.data?.flaky as FlakyRow[] | undefined) ?? [];
  const flakyRate = (latest?.data?.flakyRate as string | undefined) ?? (latest ? "0%" : "—");

  const recentRuns = [...(runs ?? [])]
    .sort((a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime())
    .slice(0, 4);

  const ringOffset = 377 - (377 * Math.max(0, Math.min(100, passRate ?? 0))) / 100;

  return (
    <div className="px-1 pb-10 pt-0.5">
      <div className="mb-[22px] flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="mb-[5px] text-[13px] font-medium text-muted">{t("reports.header.subtitle")}</div>
          <h1 className="m-0 text-[28px] font-black tracking-tight">{t("reports.header.title")}</h1>
        </div>
        <Button className="w-full md:w-auto" onClick={() => toast(t("reports.toast.exported"))}>
          <Download size={15} /> {t("reports.header.export")}
        </Button>
      </div>

      <div className="mb-3.5 grid grid-cols-1 gap-3.5 md:grid-cols-[.9fr_1.5fr_.9fr]">
        <GlassCard className="flex flex-col items-center justify-center gap-3.5 p-5">
          <div className="relative h-[150px] w-[150px]">
            <svg width="150" height="150" viewBox="0 0 150 150">
              <circle cx="75" cy="75" r="60" fill="none" stroke="rgba(255,255,255,.07)" strokeWidth="13" />
              <circle
                cx="75"
                cy="75"
                r="60"
                fill="none"
                stroke="url(#reportsRingGrad)"
                strokeWidth="13"
                strokeLinecap="round"
                strokeDasharray="377"
                strokeDashoffset={ringOffset}
                transform="rotate(-90 75 75)"
                style={{ transition: "stroke-dashoffset .4s ease" }}
              />
              <defs>
                <linearGradient id="reportsRingGrad" x1="0" y1="0" x2="1" y2="1">
                  <stop offset="0" stopColor="#22d3ee" />
                  <stop offset="1" stopColor="#8b5cf6" />
                </linearGradient>
              </defs>
            </svg>
            <div className="absolute inset-0 flex flex-col items-center justify-center">
              <div className="text-[32px] font-black leading-none tracking-tight">
                {passRate == null ? "—" : `${passRate.toFixed(1)}%`}
              </div>
              <div className="mt-0.5 text-[11.5px] text-muted">{t("reports.summary.passRate")}</div>
            </div>
          </div>
          {passRateDelta == null ? (
            <div className="text-[12px] font-semibold text-muted">
              {passRate == null ? t("reports.summary.noReportsYet") : t("reports.summary.firstReportedRun")}
            </div>
          ) : (
            <div
              className="text-[12px] font-semibold"
              style={{ color: passRateDelta >= 0 ? "#6ee7b7" : "#fb7185" }}
            >
              {passRateDelta >= 0 ? "▲" : "▼"} {t("reports.summary.ptsVsPrevious", { value: Math.abs(passRateDelta).toFixed(1) })}
            </div>
          )}
        </GlassCard>

        <GlassCard className="p-5">
          <div className="mb-[18px] text-[14px] font-bold">{t("reports.trend.title")}</div>
          {canTrend ? (
            <div className="relative flex h-[150px] items-end gap-3 pb-6">
              {trend.map((b) => (
                <div key={b.key} className="flex h-full flex-1 flex-col items-center justify-end gap-2">
                  <motion.div
                    initial={{ scaleY: 0.02 }}
                    animate={{ scaleY: b.v }}
                    transition={{ duration: 0.6, ease: "easeOut" }}
                    className="w-full max-w-[34px] origin-bottom rounded-[8px_8px_3px_3px]"
                    style={{ height: "100%", background: "linear-gradient(180deg,#8b5cf6,#6366f1)" }}
                  />
                  <span className="absolute bottom-0 text-[11px] text-[#7a7a8c]">{b.d}</span>
                </div>
              ))}
            </div>
          ) : (
            <div className="flex h-[150px] items-center justify-center text-center text-[12.5px] text-ink-dim">
              {t("reports.trend.notEnough")}
            </div>
          )}
        </GlassCard>

        <div className="flex flex-col gap-3.5">
          <GlassCard className="flex-1 p-[18px]">
            <div className="mb-2 text-[12.5px] text-[#9494a6]">{t("reports.summary.avgRunDuration")}</div>
            <div className="text-[28px] font-black tracking-tight">{avgDuration}</div>
            <div className="mt-1.5 text-[12px] font-semibold text-muted">
              {latest ? t("reports.summary.envLatestRun", { env: latest.env }) : t("reports.summary.noReportsYetLower")}
            </div>
          </GlassCard>
          <GlassCard className="flex-1 p-[18px]">
            <div className="mb-2 text-[12.5px] text-[#9494a6]">{t("reports.summary.flakyRate")}</div>
            <div className="text-[28px] font-black tracking-tight">{flakyRate}</div>
            <div
              className="mt-1.5 text-[12px] font-semibold"
              style={{ color: flaky.length ? "#fb7185" : "#6ee7b7" }}
            >
              {flaky.length ? `▲ ${t("reports.summary.flakyWatch", { count: flaky.length })}` : t("reports.summary.noFlakyTests")}
            </div>
          </GlassCard>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-3.5 md:grid-cols-2">
        <GlassCard className="p-5">
          <div className="mb-4 text-[15px] font-bold">{t("reports.recentRuns.title")}</div>
          {runsLoading ? (
            <div className="flex justify-center py-6">
              <Spinner />
            </div>
          ) : recentRuns.length === 0 ? (
            <p className="m-0 py-6 text-center text-[12.5px] text-ink-dim">{t("reports.recentRuns.empty")}</p>
          ) : (
            <div className="flex flex-col gap-[9px]">
              {recentRuns.map((r) => {
                const color = runColor(runEffectiveStatus(r));
                return (
                  <div
                    key={r.id}
                    onClick={() => navigate(`/runs/${r.id}`)}
                    className="flex cursor-pointer items-center gap-3 rounded-[13px] p-3"
                    style={{ background: "rgba(255,255,255,.03)", border: "1px solid rgba(255,255,255,.05)" }}
                  >
                    <span
                      className="h-[9px] w-[9px] shrink-0 rounded-full"
                      style={{ background: color, boxShadow: `0 0 8px ${color}` }}
                    />
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-[13px] font-semibold">
                        {r.code} · {r.name}
                      </div>
                      <div className="font-mono text-[11px] text-[#7a7a8c]">{t("reports.recentRuns.tickets", { count: r.ticketIds.length })}</div>
                    </div>
                    <div className="text-right">
                      <div className="text-[13px] font-extrabold" style={{ color }}>
                        {r.status === "done" ? t("reports.status.done") : t("reports.status.inProgress")}
                      </div>
                      <div className="text-[10.5px] text-[#7a7a8c]">{timeAgo(r.createdAt)}</div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </GlassCard>

        <GlassCard className="p-5">
          <div className="mb-4 flex items-center gap-2.5">
            <span className="flex-1 text-[15px] font-bold">{t("reports.flaky.title")}</span>
            <span className="rounded-full px-[9px] py-[3px] text-[11px] font-semibold text-[#fbbf24]" style={{ background: "rgba(251,191,36,.13)" }}>
              {t("reports.flaky.needsAttention")}
            </span>
          </div>
          {reportsLoading ? (
            <div className="flex justify-center py-6">
              <Spinner />
            </div>
          ) : reportsError ? (
            // Don't claim there are no flaky tests when the load failed (#491).
            <p className="m-0 py-6 text-center text-[12.5px] text-danger-soft" role="alert">
              {tCommon("loadFailed.title")}
            </p>
          ) : flaky.length === 0 ? (
            <p className="m-0 py-6 text-center text-[12.5px] text-ink-dim">{t("reports.flaky.empty")}</p>
          ) : (
            <div className="flex flex-col gap-[9px]">
              {flaky.map((f) => (
                <div
                  key={f.id}
                  className="flex items-center gap-3 rounded-[13px] p-3"
                  style={{ background: "rgba(255,255,255,.03)", border: "1px solid rgba(255,255,255,.05)" }}
                >
                  <span className="font-mono text-[11.5px] font-semibold text-[#a78bfa]">{f.id}</span>
                  <span className="flex-1 text-[13px] text-[#dcdce4]">{f.name}</span>
                  <span className="text-[11px] text-[#7a7a8c]">{f.runs}</span>
                  <span className="text-[13px] font-extrabold text-[#fbbf24]">{f.rate}</span>
                </div>
              ))}
            </div>
          )}
        </GlassCard>
      </div>
    </div>
  );
}
