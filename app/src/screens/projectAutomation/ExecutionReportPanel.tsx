import { useMemo, useState } from "react";
import {
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  FileCode2,
  MinusCircle,
  Search,
  XCircle,
  Zap,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { Segmented } from "@/components/ui/Segmented";
import { ErrorState, Spinner } from "@/components/ui/misc";
import { ApiError } from "@/lib/api";
import { useExecutionReport } from "@/hooks/queries";
import {
  flattenReport,
  formatDuration,
  outcomeCounts,
  REPORT_OUTCOMES,
  type ReportOutcome,
  type ReportTest,
} from "@/lib/playwrightReport";
import type { ProjectExecutionOut } from "@/types/api";
import { ReportTestDetail } from "./ReportTestDetail";

/** Statuses an execution can still move out of — mirrors `ProjectSuiteBar`. */
const PROGRESSING = new Set(["queued", "running", "dispatched"]);

type Filter = ReportOutcome | "all";

/**
 * Playwright's report, rendered by us from the raw JSON (#801).
 *
 * ## Raw JSON, and Playwright's own verdicts
 *
 * The document comes from `GET /executions/{id}/report` exactly as Playwright's
 * JSON reporter wrote it (#798), and nothing here recomputes an outcome: the
 * summary is the report's own `stats`, and each row's bucket is the test's own
 * `status` (`expected` / `unexpected` / `flaky` / `skipped`). Recomputing from the
 * results is precisely how the **flaky** category disappears — a flaky test's last
 * result passed — which is why `parsePlaywrightReport` is deliberately not used
 * here. See `lib/playwrightReport.ts`.
 *
 * ## No Traces tab, no video
 *
 * Traces are multi-MB per test and are not uploaded, so there is no tab for them
 * rather than an empty one that looks like Playwright's and never works. The
 * attachments that *do* exist — failure screenshots, uploaded as `Evidence`
 * (#799) — render inline in the detail.
 *
 * ## Opaque surface
 *
 * `bg-pop`, not `GlassCard`: this is the densest, most text-heavy panel in the
 * tab (step trees and stack traces at 11px) and it layers over the shell's
 * animated background, where a translucent card makes that text genuinely
 * unreadable — the same finding as `ProjectFilePanel`. Every colour is an
 * appearance token (#783/#784), so the panel follows dark/light and the accent.
 *
 * ## Local selection state
 *
 * Which test is expanded is `useState`, not a query param, because the thing it
 * selects *within* is itself not addressable: `executionId` is the execution this
 * tab started this visit (see `ProjectAutomationTab`), deliberately not read from
 * the URL. A `?test=` param that cannot survive a reload would be a URL that lies.
 */
export function ExecutionReportPanel({
  execution,
}: {
  /** The execution whose report to show — the one this tab started. */
  execution: ProjectExecutionOut;
}) {
  const { t } = useTranslation("projects");
  const progressing = PROGRESSING.has(execution.status);
  // The report is written once, when the run ends: asking earlier buys a
  // guaranteed 404 per poll.
  const report = useExecutionReport(execution.id, !progressing);

  const [filter, setFilter] = useState<Filter>("all");
  const [search, setSearch] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);

  const tests = useMemo(() => flattenReport(report.data), [report.data]);
  const counts = outcomeCounts(report.data?.stats);
  const total = REPORT_OUTCOMES.reduce((sum, o) => sum + counts[o], 0);

  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return tests.filter((test) => {
      if (filter !== "all" && test.outcome !== filter) return false;
      if (!needle) return true;
      return (
        test.title.toLowerCase().includes(needle) ||
        test.file.toLowerCase().includes(needle) ||
        test.suitePath.join(" ").toLowerCase().includes(needle) ||
        test.projectName.toLowerCase().includes(needle)
      );
    });
  }, [tests, filter, search]);

  // Grouped by file, in first-seen order — the order the report lists them,
  // which is the order of the tree the specs came from.
  const groups = useMemo(() => {
    const byFile = new Map<string, ReportTest[]>();
    for (const test of visible) {
      const bucket = byFile.get(test.file);
      if (bucket) bucket.push(test);
      else byFile.set(test.file, [test]);
    }
    return [...byFile.entries()];
  }, [visible]);

  if (progressing) return null;

  return (
    <div
      className="overflow-hidden rounded-2xl border border-bd2 bg-pop"
      data-testid="execution-report"
    >
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-bd3 px-4 py-3">
        <FileCode2 size={15} className="shrink-0 text-p" strokeWidth={2.2} />
        <span className="text-[13px] font-bold text-txt">{t("report.title")}</span>
        <span className="font-mono text-[11.5px] text-faint">#{execution.id}</span>
        {report.data?.config?.version && (
          <span className="font-mono text-[10.5px] text-faint">
            {t("report.playwrightVersion", { version: report.data.config.version })}
          </span>
        )}
      </header>

      {report.isLoading && (
        <div className="flex items-center justify-center py-10">
          <Spinner size={18} />
        </div>
      )}

      {/* A 404 is a state, not a failure: it means this execution stored no
          report — every execution that predates #798, and any run that produced
          none. Saying so beats an error card offering a retry that cannot help. */}
      {report.isError &&
        (report.error instanceof ApiError && report.error.status === 404 ? (
          <p className="m-0 px-4 py-6 text-[11.5px] leading-relaxed text-muted" data-testid="report-absent">
            {t("report.absent")}
          </p>
        ) : (
          <ErrorState
            title={t("report.failedTitle")}
            body={t("report.failedBody")}
            retryLabel={t("automation.retry")}
            onRetry={() => void report.refetch()}
          />
        ))}

      {report.data && (
        <>
          <SummaryBar
            counts={counts}
            total={total}
            durationMs={report.data.stats?.duration}
          />

          <div className="flex flex-col gap-2.5 border-b border-bd3 px-4 py-3 md:flex-row md:items-center">
            <Segmented<Filter>
              options={[
                { value: "all", label: t("report.filters.all", { count: total }) },
                ...REPORT_OUTCOMES.map((outcome) => ({
                  value: outcome as Filter,
                  label: t(`report.filters.${outcome}`, { count: counts[outcome] }),
                })),
              ]}
              value={filter}
              onChange={setFilter}
            />
            <label className="flex min-w-0 flex-1 items-center gap-1.5 rounded-xl border border-bd2 bg-field px-2.5 py-1.5">
              <Search size={13} className="shrink-0 text-faint" />
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder={t("report.searchPlaceholder")}
                aria-label={t("report.searchPlaceholder")}
                data-testid="report-search"
                className="min-w-0 flex-1 bg-transparent text-[12px] text-txt outline-none placeholder:text-faint"
              />
            </label>
          </div>

          {groups.length === 0 ? (
            <p className="m-0 px-4 py-6 text-[11.5px] text-muted" data-testid="report-no-matches">
              {t("report.noMatches")}
            </p>
          ) : (
            <div className="flex flex-col">
              {groups.map(([file, fileTests]) => (
                <section key={file}>
                  <h3
                    className="m-0 flex items-center gap-2 border-b border-bd3 bg-inset px-4 py-1.5 font-mono text-[11.5px] font-bold text-txt3"
                    data-testid="report-file-group"
                  >
                    {file}
                    <span className="text-[10.5px] font-semibold text-faint">
                      {t("report.testCount", { count: fileTests.length })}
                    </span>
                  </h3>
                  {fileTests.map((test) => (
                    <TestRow
                      key={test.key}
                      test={test}
                      open={expanded === test.key}
                      onToggle={() =>
                        setExpanded((current) => (current === test.key ? null : test.key))
                      }
                      results={execution.results ?? []}
                    />
                  ))}
                </section>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

/** Totals straight off the report's `stats` — no arithmetic beyond the sum. */
function SummaryBar({
  counts,
  total,
  durationMs,
}: {
  counts: Record<ReportOutcome, number>;
  total: number;
  durationMs: number | undefined;
}) {
  const { t } = useTranslation("projects");
  return (
    <div
      className="flex flex-wrap items-center gap-x-4 gap-y-1.5 border-b border-bd3 px-4 py-2.5 text-[11.5px]"
      data-testid="report-summary"
    >
      <span className="font-bold text-txt2">{t("report.total", { count: total })}</span>
      {REPORT_OUTCOMES.map((outcome) => (
        <span key={outcome} className="flex items-center gap-1.5">
          <OutcomeIcon outcome={outcome} />
          <span className={counts[outcome] > 0 ? "text-txt3" : "text-faint"}>
            {t(`report.summary.${outcome}`, { count: counts[outcome] })}
          </span>
        </span>
      ))}
      <span className="ml-auto font-mono text-faint">{formatDuration(durationMs)}</span>
    </div>
  );
}

/** One test: status icon, title, `file:line`, duration and Playwright project. */
function TestRow({
  test,
  open,
  onToggle,
  results,
}: {
  test: ReportTest;
  open: boolean;
  onToggle: () => void;
  results: ProjectExecutionOut["results"];
}) {
  return (
    <div className="border-b border-bd3 last:border-b-0">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        data-testid="report-test-row"
        className="flex w-full cursor-pointer items-center gap-2 px-4 py-2 text-left hover:bg-card3"
      >
        {open ? (
          <ChevronDown size={13} className="shrink-0 text-faint" />
        ) : (
          <ChevronRight size={13} className="shrink-0 text-faint" />
        )}
        <OutcomeIcon outcome={test.outcome} />
        <span className="min-w-0 flex-1 truncate text-[12.5px] text-txt2">
          {test.suitePath.length > 0 && (
            <span className="text-faint">{test.suitePath.join(" › ")} › </span>
          )}
          {test.title}
        </span>
        {test.results.length > 1 && (
          <span className="shrink-0 rounded-md bg-warn-tint px-1.5 py-0.5 text-[10px] font-bold text-warn">
            ×{test.results.length}
          </span>
        )}
        {test.projectName && (
          <span className="hidden shrink-0 font-mono text-[10.5px] text-faint sm:inline">
            {test.projectName}
          </span>
        )}
        <span className="shrink-0 font-mono text-[10.5px] text-faint">
          {test.file}:{test.line}
        </span>
        <span className="w-14 shrink-0 text-right font-mono text-[10.5px] text-faint">
          {formatDuration(test.durationMs)}
        </span>
      </button>
      {open && <ReportTestDetail test={test} results={results ?? []} />}
    </div>
  );
}

/** The four outcome glyphs, in status tokens so they follow the theme. */
function OutcomeIcon({ outcome }: { outcome: ReportOutcome }) {
  if (outcome === "passed")
    return <CheckCircle2 size={13} className="shrink-0 text-ok" aria-hidden />;
  if (outcome === "failed")
    return <XCircle size={13} className="shrink-0 text-danger" aria-hidden />;
  if (outcome === "flaky")
    return <Zap size={13} className="shrink-0 text-warn" aria-hidden />;
  return <MinusCircle size={13} className="shrink-0 text-neutral" aria-hidden />;
}
