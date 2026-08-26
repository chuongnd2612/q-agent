import { useCallback, useState } from "react";
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  KeyRound,
  Laptop,
  Play,
  RotateCw,
  Server,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { toast } from "@/lib/toast";
import { Button } from "@/components/ui/Button";
import { Pill, execStyle, productDefectStyle } from "@/components/ui/badges";
import { ProgressRing, Spinner } from "@/components/ui/misc";
import { PipelineRail } from "@/components/ui/PipelineRail";
import { ApiError } from "@/lib/api";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useRunPath } from "@/hooks/useRunRouteId";
import { useExecution, useRun, useSettings, useStartExecution } from "@/hooks/queries";
import { SetupBlockers } from "@/components/setup/SetupBlockers";
import { useSetupGuard } from "@/components/setup/useSetupGuard";
import { useRunEvents } from "@/hooks/useRunEvents";
import type { ExecutionResultOut, ExecutionTarget, ProgressEvent } from "@/types/api";

/** Truncates long ticket ids for the fixed-width queue column (design's r.tidShort). */
function shortTicket(id: string): string {
  return id.length > 10 ? id.slice(0, 10) : id;
}

export function Execution() {
  const { t } = useTranslation("pipeline");
  const runId = Number(useParams().runId);
  const runPath = useRunPath();
  const navigate = useNavigate();

  const { data: run } = useRun(runId);
  const { data: execution, isLoading } = useExecution(runId);
  const { data: settings } = useSettings();
  const startExecution = useStartExecution(runId);
  const guardSetup = useSetupGuard();

  // Manual-login prompt state, driven by the run WebSocket. When the backend
  // (or a Local Agent, whose events the server re-emits unchanged) opens a
  // browser for the operator to log in, it emits `exec.auth.waiting`;
  // `exec.auth.captured`/`exec.auth.error` clear it.
  const [authWaiting, setAuthWaiting] = useState<{ url: string } | null>(null);
  const onRunEvent = useCallback((evt: ProgressEvent) => {
    if (evt.event === "exec.auth.waiting") {
      setAuthWaiting({ url: String(evt.payload?.url ?? "") });
    } else if (evt.event === "exec.auth.captured") {
      setAuthWaiting(null);
      toast.success(t("execution.toast.loginCaptured"));
    } else if (evt.event === "exec.auth.error") {
      setAuthWaiting(null);
      toast.error(String(evt.payload?.message ?? t("execution.toast.manualLoginFailed")));
    }
  }, [t]);
  useRunEvents(onRunEvent);

  const status = execution?.status ?? "idle";
  const isIdle = !execution || status === "idle" || status === "pending";
  const isRunning = status === "running";
  const isDone = status === "done" || status === "completed";

  const total = execution?.total ?? 0;
  const passed = execution?.passed ?? 0;
  const failed = execution?.failed ?? 0;
  const remaining = Math.max(0, total - passed - failed);
  const progress = execution?.progress ?? 0;

  const results = execution?.results ?? [];
  const runningResult = results.find((r) => r.status === "running");
  const current: ExecutionResultOut | undefined =
    runningResult ?? (isDone ? results[results.length - 1] : undefined);

  const handleRun = () => {
    // Pre-flight (#643): with "My machine" as the target (#161's deliberate
    // default) an account that has never paired a device would just get a 409
    // here. Refuse up front and say what to install instead — the 409 handler
    // below stays as the server-side backstop.
    guardSetup(["localAgent"], () => runNow());
  };

  const runNow = () => {
    // No per-run target: the backend resolves the workspace-wide
    // `executionTarget` setting (configured on the Settings screen).
    startExecution.mutate(
      {},
      {
        onError: (err) => {
          if (err instanceof ApiError && err.status === 409) {
            toast.error(err.message || t("execution.toast.noLocalAgent"), {
              action: { label: t("execution.toast.localAgentAction"), onClick: () => navigate("/local-agent") },
            });
            return;
          }
          toast.error(err instanceof Error ? err.message : t("execution.toast.startFailed"));
        },
      },
    );
  };

  // Once an execution has been created, its own target is authoritative for
  // banner copy; before that, the workspace default setting drives it.
  const effectiveTarget: ExecutionTarget = execution?.target ?? settings?.executionTarget ?? "server";

  return (
    <div className="px-1 pb-10 pt-0.5">
      <div className="mb-3.5 flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="mb-[5px] text-[13px] font-medium text-ink-dim">
            {run?.code ?? `RUN-${runId}`} &middot; {run?.framework ?? "Playwright"} &middot;{" "}
            {run?.env ?? "Staging"} &middot;{" "}
            {t("execution.parallelWorkers", { count: run?.workers ?? execution?.workers ?? 0 })}
          </div>
          <h1 className="m-0 text-[24px] font-black tracking-tight md:text-[28px]">{t("execution.title")}</h1>
        </div>
        <div className="flex flex-col gap-2.5 md:flex-row md:items-center md:gap-3">
          <Link
            to="/settings#execution"
            title={t("execution.target.changeTooltip")}
            className="glass flex w-fit items-center gap-1.5 rounded-xl px-3 py-1.5 text-[12px] font-semibold text-ink-dim transition-colors hover:text-white"
          >
            {effectiveTarget === "local-agent" ? (
              <Laptop size={13} strokeWidth={2.2} />
            ) : (
              <Server size={13} strokeWidth={2.2} />
            )}
            {effectiveTarget === "local-agent" ? t("execution.target.myMachine") : t("execution.target.server")}
          </Link>
          <Button
            variant="primary"
            size="lg"
            onClick={handleRun}
            disabled={isRunning || startExecution.isPending}
            className="w-full md:w-auto"
          >
          {isRunning || startExecution.isPending ? (
            <>
              <Spinner size={15} />
              {t("execution.button.running")}
            </>
          ) : isDone ? (
            <>
              <RotateCw size={15} strokeWidth={2.4} />
              {t("execution.button.rerun")}
            </>
          ) : (
            <>
              <Play size={15} fill="#fff" stroke="none" />
              {t("execution.button.runSuite")}
            </>
          )}
          </Button>
        </div>
      </div>

      {authWaiting && (
        <div
          className="mb-3.5 flex flex-wrap items-center gap-3.5 rounded-[16px] p-4 md:p-[15px_18px]"
          style={{ background: "rgba(139,92,246,.12)", border: "1px solid rgba(139,92,246,.34)" }}
        >
          <div
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl"
            style={{ background: "linear-gradient(135deg,#8b5cf6,#6366f1)" }}
          >
            <KeyRound size={18} color="#fff" strokeWidth={2.2} />
          </div>
          <div className="flex-1">
            <div className="mb-0.5 flex items-center gap-2 text-[14px] font-bold">
              <Spinner size={13} /> {t("execution.auth.waiting")}
            </div>
            <p className="m-0 text-[12.5px] leading-relaxed text-[#c3c3d4]">
              {effectiveTarget === "local-agent"
                ? t("execution.auth.localAgent")
                : t("execution.auth.server")}
              {authWaiting.url ? (
                <>
                  {" "}{t("execution.auth.at")}{" "}
                  <a
                    href={authWaiting.url}
                    target="_blank"
                    rel="noreferrer"
                    className="break-all font-mono text-violet hover:text-[#c4b5fd]"
                  >
                    {authWaiting.url}
                  </a>
                </>
              ) : null}
              {t("execution.auth.thenClose")}
            </p>
          </div>
        </div>
      )}

      <div className="mb-3.5 hidden md:block">
        <PipelineRail stage={4} />
      </div>

      {/* Only the prerequisite this screen's action needs (#643) — listing the
          authoring blockers here too would make both screens look broken. */}
      <SetupBlockers only={["localAgent", "claudeCredential"]} />

      <div className="mb-3.5 grid grid-cols-[1.1fr_1fr] gap-2.5 md:gap-3.5">
        <div className="glass flex items-center gap-3 rounded-[18px] p-3 md:gap-5 md:p-[18px_22px]">
          <ProgressRing value={progress} label={<span className="text-lg font-black">{progress}%</span>} />
          <div className="flex flex-1 gap-[18px]">
            <div>
              <div className="text-[22px] font-black leading-none text-[#6ee7b7]">{passed}</div>
              <div className="mt-0.5 text-[11px] text-ink-dim">{t("execution.stats.passed")}</div>
            </div>
            <div>
              <div className="text-[22px] font-black leading-none text-[#fb7185]">{failed}</div>
              <div className="mt-0.5 text-[11px] text-ink-dim">{t("execution.stats.failed")}</div>
            </div>
            <div>
              <div className="text-[22px] font-black leading-none text-[#c3c3d0]">{remaining}</div>
              <div className="mt-0.5 text-[11px] text-ink-dim">{t("execution.stats.remaining")}</div>
            </div>
          </div>
        </div>

        <div className="glass flex flex-col justify-center gap-2 rounded-[18px] p-3 md:p-[18px_22px]">
          <div className="text-[11px] font-semibold tracking-[.06em] text-[#6c6c7e]">{t("execution.currentlyExecuting")}</div>
          <div className="flex items-center gap-2.5">
            <span className="font-mono text-[12px] font-semibold text-violet">
              {current?.ticketExternalId ?? "—"}
            </span>
            {isRunning && <Spinner size={13} />}
          </div>
          <div className="overflow-hidden text-ellipsis whitespace-nowrap text-[13.5px] font-semibold text-[#dcdce4]">
            {current?.title ?? (isIdle ? t("execution.notStarted") : "—")}
          </div>
          {isDone && (
            <Button variant="glass" size="sm" onClick={() => navigate(runPath("evidence"))} className="mt-1 self-start">
              {t("execution.collectEvidence")}
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M5 12h14M13 6l6 6-6 6" />
              </svg>
            </Button>
          )}
        </div>
      </div>

      <div className="glass rounded-[18px] p-2">
        <div className="p-[9px_12px_6px] text-[11px] font-semibold tracking-[.08em] text-[#6c6c7e]">
          {t("execution.queue.heading")} &middot; {progress}%
        </div>
        {isLoading ? (
          <div className="flex flex-col gap-2 p-2">
            {Array.from({ length: 6 }).map((_, i) => (
              <div key={i} className="h-[42px] animate-pulse rounded-xl bg-white/[0.04]" />
            ))}
          </div>
        ) : !results.length ? (
          <div className="p-6 text-center text-[13px] text-ink-dim">
            {t("execution.queue.empty")}
          </div>
        ) : (
          results.map((r) => <ExecRow key={r.id} result={r} />)
        )}
      </div>

      {execution?.log ? <ExecutionLog log={execution.log} /> : null}
    </div>
  );
}

/** Collapsible panel showing raw Playwright stdout/stderr for the run. Collapsed by default. */
function ExecutionLog({ log }: { log: string }) {
  const { t } = useTranslation("pipeline");
  const [open, setOpen] = useState(false);
  return (
    <div className="glass mt-3.5 rounded-[18px] p-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full cursor-pointer items-center gap-2 rounded-xl bg-transparent p-[9px_12px] text-left transition-colors hover:bg-white/[0.04]"
        aria-expanded={open}
      >
        {open ? (
          <ChevronDown size={14} className="text-ink-dim" />
        ) : (
          <ChevronRight size={14} className="text-ink-dim" />
        )}
        <span className="text-[11px] font-semibold tracking-[.08em] text-[#6c6c7e]">{t("execution.log.heading")}</span>
      </button>
      {open && (
        <pre className="mx-1 mb-1 max-h-[320px] overflow-auto whitespace-pre-wrap rounded-xl border border-white/[0.09] bg-[rgba(8,8,13,.7)] p-3.5 font-mono text-[12px] leading-relaxed text-[#c7c7d4]">
          {log}
        </pre>
      )}
    </div>
  );
}

function ExecRow({ result }: { result: ExecutionResultOut }) {
  // A confirmed product defect is a failed case whose failureClass says so. It gets
  // the fuchsia "Product defect" treatment (dot glow, label, pill) so it reads
  // distinctly from a plain red script "Failed". Any other status — or a fail that
  // is unclassified / not a product defect — renders with the shared execColors.
  const isProductDefect = result.status === "fail" && result.failureClass === "product_defect";
  const [color, label] = isProductDefect ? productDefectStyle() : execStyle(result.status);
  return (
    <div className="flex items-center gap-3 rounded-xl p-[11px_13px] transition-colors hover:bg-white/[0.04]">
      {result.status === "running" && <Spinner size={15} />}
      <span
        className="h-[9px] w-[9px] shrink-0 rounded-full"
        style={{ background: color, boxShadow: `0 0 8px ${color}` }}
      />
      <span className="w-[66px] shrink-0 font-mono text-[11px] font-semibold text-[#7a7a8c]">
        {shortTicket(result.ticketExternalId)}
      </span>
      <span className="shrink-0 font-mono text-[11.5px] font-semibold text-violet">{result.caseCode}</span>
      <span className="flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-[13px] text-[#dcdce4]">
        {result.title}
      </span>
      {isProductDefect ? (
        <Pill color={color} bg="rgba(217,70,239,.14)">
          <AlertTriangle size={11} strokeWidth={2.4} />
          {label}
        </Pill>
      ) : (
        <span className="shrink-0 text-[11px] font-bold" style={{ color }}>
          {label}
        </span>
      )}
    </div>
  );
}
