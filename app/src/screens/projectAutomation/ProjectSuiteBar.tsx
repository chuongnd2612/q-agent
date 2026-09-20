import { Info, Play } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Spinner } from "@/components/ui/misc";
import type { ProjectExecutionOut } from "@/types/api";

/** Statuses a project execution can still move out of — see the API's
 *  `execution_service.finalize`, which lands it on passed/failed/error. */
const PROGRESSING = new Set(["queued", "running", "dispatched"]);

/**
 * Run the ticked specs out of this project's automation repo (#800) — the
 * project-scoped sibling of `RunSuiteBar`.
 *
 * A sibling rather than a shared component: the two bars agree on their shape
 * and on one sentence, and on nothing else. `RunSuiteBar` runs "every runnable
 * approved case in the Run" and needs no count, no selection and no progress of
 * its own (the Execution screen is one click away). This one runs an **explicit
 * selection** out of a repo that is shared across projects, and has nowhere to
 * navigate to — there is no run-scoped Execution screen for a run-less
 * execution — so it renders the progress in place. Folding both into one
 * component would mean a prop for each of those differences.
 *
 * ## Disabled with a reason, never a 400
 *
 * `_selected_specs` rejects an empty selection outright (there is deliberately
 * no "run everything" default, because "every spec in the repo" is not "this
 * project's specs"). So an empty selection disables the button *and says why*,
 * the same rule `runnableCount` follows in `Automation.tsx` (#701) — the user
 * should never be able to click into a server-side refusal.
 *
 * ## Opaque, not `GlassCard`
 *
 * This sits over the shell's animated constellation background and carries two
 * lines of small explanatory text; a translucent card makes that text genuinely
 * unreadable. Same surface, and same reason, as `ExportProjectPanel` and
 * `RepoHeader` directly above it.
 */
export function ProjectSuiteBar({
  selectedCount,
  specCount,
  pending,
  execution,
  onRun,
}: {
  /** How many specs are ticked in the tree. */
  selectedCount: number;
  /** How many spec files the repo holds in total — the honest denominator for
   * "N of M", and what makes the "other projects' specs" line concrete. */
  specCount: number;
  /** A start request is in flight. */
  pending: boolean;
  /** The execution started from this tab, once there is one. */
  execution: ProjectExecutionOut | null;
  onRun: () => void;
}) {
  const { t } = useTranslation("projects");
  const runnable = selectedCount > 0;
  const reason = runnable
    ? t("automation.runSuite.description", { count: selectedCount })
    : t("automation.runSuite.noneSelected");

  return (
    <div
      className="flex flex-col gap-2.5 rounded-2xl border border-bd2 px-4 py-3.5"
      style={{ background: "var(--pop)" }}
      data-testid="project-suite-bar"
    >
      <div className="flex flex-col gap-2.5 md:flex-row md:items-center">
        <span className="text-xs leading-relaxed text-muted md:flex-1" data-testid="project-suite-reason">
          {reason}
        </span>
        <button
          type="button"
          onClick={onRun}
          disabled={pending || !runnable}
          title={reason}
          data-testid="project-suite-run"
          className="flex w-full items-center justify-center gap-2 rounded-xl px-[18px] py-2.5 text-[13px] font-bold text-p-on disabled:opacity-60 md:w-auto md:shrink-0"
          style={{
            background: "var(--pg)",
            boxShadow: "0 8px 22px -8px var(--pglow)",
          }}
        >
          {pending ? <Spinner size={14} /> : <Play size={14} fill="var(--pOn)" />}
          {t("automation.runSuite.label", { count: selectedCount })}
        </button>
      </div>

      {/* The quiet, unconditional line: the tree is unfiltered on purpose, so the
          user has to be told that what is *pre-ticked* is narrower than what is
          *listed* — otherwise an unticked spec reads as a bug rather than as
          another project's file. Unconditional because it is a property of the
          schema, not a guess about this repo's contents. */}
      <p
        className="m-0 flex items-start gap-1.5 text-[11px] leading-relaxed text-muted"
        data-testid="project-suite-note"
      >
        <Info size={12} className="mt-[2px] shrink-0 text-faint" />
        {t("automation.runSuite.sharedRepoNote", { selected: selectedCount, total: specCount })}
      </p>

      {execution && <ExecutionProgress execution={execution} />}
    </div>
  );
}

/**
 * Live progress for the execution this tab started.
 *
 * Rendered here rather than linked to, because there is nowhere to link: the
 * Execution screen is run-scoped (`/runs/:runId/execution`) and a project
 * execution has no run. The numbers come from the cached row that
 * `useProjectExecutionSocket` patches on each event and `useProjectExecution`
 * re-reads while the row is non-terminal.
 */
function ExecutionProgress({ execution }: { execution: ProjectExecutionOut }) {
  const { t } = useTranslation("projects");
  const active = PROGRESSING.has(execution.status);
  // `progress` is only meaningful while running; a finished execution is 100%
  // whatever the last event happened to carry.
  const percent = active ? Math.max(0, Math.min(100, execution.progress)) : 100;

  return (
    <div className="flex flex-col gap-1.5 border-t border-bd pt-2.5" data-testid="project-exec-progress">
      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[11.5px]">
        {active && <Spinner size={12} />}
        <span className="font-mono font-bold text-txt">#{execution.id}</span>
        <span className="text-muted">
          {t(`automation.runSuite.status.${active ? "running" : "finished"}`)}
        </span>
        <span className="text-ok">
          {t("automation.runSuite.passed", { count: execution.passed })}
        </span>
        <span className={execution.failed > 0 ? "text-danger" : "text-faint"}>
          {t("automation.runSuite.failed", { count: execution.failed })}
        </span>
        <span className="text-faint">
          {t("automation.runSuite.ofTotal", { count: execution.total })}
        </span>
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-card3">
        <div
          className="h-full rounded-full transition-all"
          style={{
            width: `${percent}%`,
            background: execution.failed > 0 ? "var(--danger)" : "var(--pg)",
          }}
        />
      </div>
    </div>
  );
}
