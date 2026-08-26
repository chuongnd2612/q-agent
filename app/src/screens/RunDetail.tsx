import { useState } from "react";
import { motion } from "framer-motion";
import {
  Activity,
  ArrowLeft,
  ArrowRight,
  Brain,
  Check,
  Clock,
  Cpu,
  FileText,
  Image as ImageIcon,
  Send,
  Terminal,
  type LucideIcon,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/Button";
import { providerGlyph } from "@/components/ui/badges";
import { isRunComplete, PipelineRail, runStatusToStage } from "@/components/ui/PipelineRail";
import { useNavigate, useParams } from "react-router-dom";
import { useProjectRouteSegment, useRunPath } from "@/hooks/useRunRouteId";
import { ALL_TICKETS_PAGE_SIZE, useRun, useRunAiUsage, useTickets } from "@/hooks/queries";
import { useRunEvents } from "@/hooks/useRunEvents";
import type { ProgressEvent, RunAiProcess, RunAiTicket, RunAiUsage, RunTicketOut } from "@/types/api";

const RUN_STATUS_LABEL: Record<string, string> = {
  processing: "AI processing",
  review: "Ready for review",
  sync: "Creating & linking",
  automation: "Automation",
  executing: "Executing",
  evidence: "Evidence ready",
  comment: "Publishing",
  done: "Complete",
  cancelled: "Cancelled",
  failed: "Failed",
};

/** Badge color per status; in-progress stages share the design's amber. */
const RUN_STATUS_BADGE: Record<string, { color: string; bg: string }> = {
  cancelled: { color: "#d1d5db", bg: "rgba(156,163,175,.16)" },
  failed: { color: "#fb7185", bg: "rgba(244,63,94,.14)" },
};

/** Run detail: pipeline stage, live processing banner, and per-ticket generation status. */
export function RunDetail() {
  const { t } = useTranslation("runs");
  const runId = Number(useParams().runId);
  const runPath = useRunPath();
  const projectSegment = useProjectRouteSegment();
  const navigate = useNavigate();

  const { data: run, isLoading } = useRun(runId);
  const { data: ticketsPage } = useTickets({ pageSize: ALL_TICKETS_PAGE_SIZE });
  const tickets = ticketsPage?.items;
  const { data: aiUsage } = useRunAiUsage(runId);

  // Live phase messages per ticket, keyed by ticket externalId — updated from the
  // analysis.phase WS event since RunTicketOut.genStatus alone has no message text.
  const [phaseMsgs, setPhaseMsgs] = useState<Record<string, string>>({});
  useRunEvents((evt: ProgressEvent) => {
    if (evt.event === "analysis.phase") {
      const { ticket, message } = evt.payload as { ticket?: string; message?: string };
      if (ticket && message) setPhaseMsgs((m) => ({ ...m, [ticket]: message }));
    }
  });

  if (isLoading || !run) {
    return (
      <div className="px-1 pb-10 pt-0.5">
        <div className="glass mb-4 h-8 w-24 animate-pulse rounded-lg" />
        <div className="glass mb-4 h-[76px] animate-pulse rounded-[18px]" />
        <div className="flex flex-col gap-[11px]">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="glass h-[66px] animate-pulse rounded-2xl" />
          ))}
        </div>
      </div>
    );
  }

  const processing = run.status === "processing";
  const goReview = () => {
    navigate(runPath("review"));
  };

  const total = run.runTickets.length;
  const analyzed = run.runTickets.filter((rt) => rt.genStatus === "done").length;
  const pct = total ? Math.round((analyzed / total) * 100) : 0;
  const headline = Object.values(phaseMsgs).at(-1) ?? t("detail.readingRequirements");

  return (
    <div className="px-1 pb-10 pt-0.5">
      <button
        onClick={() =>
          navigate(projectSegment ? `/projects/${projectSegment}/runs` : "/projects")
        }
        className="mb-3.5 flex cursor-pointer items-center gap-[7px] border-none bg-transparent p-0 text-[12.5px] font-semibold text-ink-dim hover:text-[#c7c7d4]"
      >
        <ArrowLeft size={14} strokeWidth={2.2} />
        {t("detail.back")}
      </button>

      <div className="mb-4 flex flex-col gap-3 md:flex-row md:items-start md:justify-between md:gap-4">
        <div>
          <div className="mb-1.5 flex items-center gap-2.5">
            <span className="font-mono text-[13px] font-semibold text-violet">{run.code}</span>
            <span
              className="rounded-full px-2.5 py-[3px] text-[11px] font-bold"
              style={{
                color: RUN_STATUS_BADGE[run.status]?.color ?? "#fbbf24",
                background: RUN_STATUS_BADGE[run.status]?.bg ?? "rgba(245,158,11,.14)",
              }}
            >
              {RUN_STATUS_LABEL[run.status] ?? run.status}
            </span>
          </div>
          <h1 className="m-0 text-[22px] font-black tracking-tight md:text-[26px]">{run.name}</h1>
          <div className="mt-1.5 text-[12.5px] text-ink-dim">
            {t("detail.meta", {
              scope: run.scopeLabel,
              framework: run.framework,
              env: run.env,
              workers: run.workers,
            })}
          </div>
        </div>
        <Button variant="primary" onClick={goReview} className="w-full shrink-0 md:w-auto">
          {t("detail.openReview")}
          <ArrowRight size={14} strokeWidth={2.2} />
        </Button>
      </div>

      <div className="mb-4 hidden md:block">
        <PipelineRail
          stage={runStatusToStage[run.status] ?? 0}
          complete={isRunComplete(run.status)}
        />
      </div>

      {processing && (
        <div
          className="relative mb-3.5 overflow-hidden rounded-[20px] p-4 md:p-[22px_24px]"
          style={{
            background: "linear-gradient(135deg,rgba(139,92,246,.2),rgba(99,102,241,.08))",
            border: "1px solid rgba(139,92,246,.34)",
            boxShadow: "0 0 40px -12px rgba(139,92,246,.4)",
          }}
        >
          <div
            className="pointer-events-none absolute bottom-0 left-0 top-0 w-[120px]"
            style={{
              background: "linear-gradient(90deg,transparent,rgba(167,139,250,.18),transparent)",
              animation: "procScan 2.2s ease-in-out infinite",
            }}
          />
          <div className="relative flex items-center gap-[15px]">
            <div
              className="relative flex h-[44px] w-[44px] shrink-0 items-center justify-center rounded-[13px]"
              style={{
                background: "linear-gradient(135deg,#8b5cf6,#6366f1)",
                boxShadow: "0 8px 24px -6px rgba(139,92,246,.7)",
                animation: "logoPulse 2.2s ease-in-out infinite",
              }}
            >
              {/* Splash-logo treatment: pulsing halo + staggered expanding rings. */}
              <span
                className="pointer-events-none absolute"
                style={{
                  inset: "-8px",
                  borderRadius: "50%",
                  background: "radial-gradient(circle,rgba(139,92,246,.55),transparent 68%)",
                  filter: "blur(9px)",
                  animation: "logoHalo 2.6s ease-in-out infinite",
                }}
              />
              {[0, 0.8, 1.6].map((delay) => (
                <span
                  key={delay}
                  className="pointer-events-none absolute inset-0"
                  style={{
                    borderRadius: "13px",
                    border: "1.5px solid rgba(167,139,250,.45)",
                    animation: `ring 2.4s ease-out infinite ${delay}s`,
                  }}
                />
              ))}
              <svg
                className="relative z-[1]"
                width="22"
                height="22"
                viewBox="0 0 24 24"
                fill="none"
                stroke="#fff"
                strokeWidth="2.2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M12 3l1.9 5.3L19 10l-5.1 1.7L12 17l-1.9-5.3L5 10l5.1-1.7z" />
              </svg>
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-[10px]">
                <span className="text-[15px] font-extrabold tracking-[-.01em]">
                  {t("detail.analyzing")}
                </span>
                <span className="inline-flex gap-[3px]">
                  <span
                    className="h-[5px] w-[5px] rounded-full bg-[#c4b5fd]"
                    style={{ animation: "procDot 1.2s ease-in-out infinite" }}
                  />
                  <span
                    className="h-[5px] w-[5px] rounded-full bg-[#c4b5fd]"
                    style={{ animation: "procDot 1.2s ease-in-out .2s infinite" }}
                  />
                  <span
                    className="h-[5px] w-[5px] rounded-full bg-[#c4b5fd]"
                    style={{ animation: "procDot 1.2s ease-in-out .4s infinite" }}
                  />
                </span>
              </div>
              <div className="mt-[3px] text-[12.5px] text-[#c3b8e8]">{headline}</div>
            </div>
            <div className="shrink-0 text-right">
              <div className="text-[22px] font-black tracking-[-.02em] text-white">
                {analyzed}
                <span className="text-[13px] font-bold text-[#b9a8e6]">/{total}</span>
              </div>
              <div className="text-[10.5px] font-semibold tracking-[.03em] text-[#b9a8e6]">
                {t("detail.ticketsAnalyzed")}
              </div>
            </div>
          </div>
          <div
            className="relative mt-[16px] h-[6px] overflow-hidden rounded-[6px]"
            style={{ background: "rgba(255,255,255,.09)" }}
          >
            <div
              className="h-full rounded-[6px]"
              style={{
                background: "linear-gradient(90deg,#8b5cf6,#22d3ee)",
                width: pct + "%",
                transition: "width .5s cubic-bezier(.2,.8,.2,1)",
                boxShadow: "0 0 12px rgba(139,92,246,.7)",
              }}
            />
          </div>
        </div>
      )}

      <div className="flex flex-col gap-[11px]">
        {run.runTickets.map((rt, i) => (
          <RunTicketRow
            key={rt.ticketExternalId}
            runTicket={rt}
            title={tickets?.find((t) => t.externalId === rt.ticketExternalId)?.title ?? rt.ticketExternalId}
            providerKind={tickets?.find((t) => t.externalId === rt.ticketExternalId)?.providerKind ?? "ado"}
            phaseMsg={phaseMsgs[rt.ticketExternalId]}
            index={i}
            onReview={() =>
              navigate(runPath("review") + "?ticket=" + encodeURIComponent(rt.ticketExternalId))
            }
          />
        ))}
      </div>

      {aiUsage && aiUsage.processes.length > 0 && (
        <AiUsageCard
          usage={aiUsage}
          runCode={run.code}
          resolveTicket={(externalId) => {
            const t = tickets?.find((x) => x.externalId === externalId);
            return { title: t?.title ?? externalId, providerKind: t?.providerKind ?? "ado" };
          }}
        />
      )}
    </div>
  );
}

function RunTicketRow({
  runTicket,
  title,
  providerKind,
  phaseMsg,
  index,
  onReview,
}: {
  runTicket: RunTicketOut;
  title: string;
  providerKind: string;
  phaseMsg?: string;
  index: number;
  onReview: () => void;
}) {
  const { t } = useTranslation("runs");
  const [glyph, color] = providerGlyph[providerKind] ?? ["?", "#8b8b9e"];
  const done = runTicket.genStatus === "done";
  const analyzing = runTicket.genStatus === "analyzing";
  const generating = runTicket.genStatus === "generating";
  const current = analyzing || generating;
  const queued = runTicket.genStatus === "queued";
  const errored = runTicket.genStatus === "error";
  // Prefer the live phase message; else a status-specific default per the design.
  const statusText = phaseMsg ?? (generating ? t("detail.generatingCases") : t("detail.readingTicket"));

  return (
    <motion.div
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, delay: Math.min(index * 0.04, 0.3), ease: "easeOut" }}
      className="glass flex items-center gap-[14px] rounded-2xl p-[16px_18px]"
    >
      {/* Provider avatar; while this ticket is being processed it pulses with the
          same treatment as the main "analyzing" logo — a soft halo and staggered
          expanding rings around a gently scaling glyph. */}
      <div className="relative h-[34px] w-[34px] shrink-0">
        {current && (
          <>
            <span
              className="pointer-events-none absolute"
              style={{
                inset: "-7px",
                borderRadius: "50%",
                background: "radial-gradient(circle,rgba(139,92,246,.55),transparent 68%)",
                filter: "blur(7px)",
                animation: "logoHalo 2.6s ease-in-out infinite",
              }}
            />
            {[0, 0.8, 1.6].map((delay) => (
              <span
                key={delay}
                className="pointer-events-none absolute inset-0"
                style={{
                  borderRadius: "10px",
                  border: "1.5px solid rgba(167,139,250,.45)",
                  animation: `ring 2.4s ease-out infinite ${delay}s`,
                }}
              />
            ))}
          </>
        )}
        <div
          className="relative z-[1] flex h-full w-full items-center justify-center rounded-[10px] text-[14px] font-black text-white"
          style={{
            background: color,
            animation: current ? "logoPulse 2.2s ease-in-out infinite" : undefined,
          }}
        >
          {glyph}
        </div>
      </div>
      <div className="min-w-0 flex-1">
        <div className="mb-0.5 flex items-center gap-[9px]">
          <span className="font-mono text-[11.5px] font-semibold text-violet">{runTicket.ticketExternalId}</span>
        </div>
        <div className="overflow-hidden text-ellipsis whitespace-nowrap text-[14px] font-semibold">{title}</div>
      </div>

      {current && (
        <div className="flex items-center gap-[9px] text-[12.5px] font-semibold text-[#c4b5fd]">
          {statusText}
        </div>
      )}
      {done && (
        <Button variant="success" size="sm" onClick={onReview}>
          <Check size={13} strokeWidth={2.6} />
          {t("detail.review")}
        </Button>
      )}
      {queued && (
        <span className="flex items-center gap-1.5 text-[12px] font-semibold text-[#6b7280]">
          <Clock size={13} strokeWidth={2} />
          {t("detail.queued")}
        </span>
      )}
      {errored && <span className="text-[12px] font-semibold text-[#fb7185]">{t("detail.analysisFailed")}</span>}
    </motion.div>
  );
}

/** USD → "$42.60". */
function fmtCost(usd: number): string {
  return `$${usd.toFixed(2)}`;
}

/** Compact token/number → "1.5K" / "2.4M" (integers below 1000 stay as-is). */
function fmtCompact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1).replace(/\.0$/, "")}K`;
  return String(n);
}

/** Lucide icon per AI-process key (falls back to Cpu for unknown kinds). */
const PROCESS_ICON: Record<string, LucideIcon> = {
  analyze: Brain,
  generate: FileText,
  automation: Terminal,
  analysis: Activity,
  publish: Send,
  evidence: ImageIcon,
};

const AI_COST_GRID = "1.5fr .8fr .8fr .8fr .7fr";

/** One process sub-row (INPUT/OUTPUT/TOKENS/COST) under a ticket group. */
function AiProcessRow({ process }: { process: RunAiProcess }) {
  const Icon = PROCESS_ICON[process.key] ?? Cpu;
  return (
    <div
      className="grid items-center gap-[10px] p-[10px_18px_10px_40px]"
      style={{ gridTemplateColumns: AI_COST_GRID, borderTop: "1px solid rgba(255,255,255,.045)" }}
    >
      <div className="flex min-w-0 items-center gap-[10px]">
        <span
          className="flex h-[24px] w-[24px] shrink-0 items-center justify-center rounded-[7px]"
          style={{ background: "rgba(139,92,246,.14)" }}
        >
          <Icon size={13} strokeWidth={2} color="#a78bfa" />
        </span>
        <div className="min-w-0">
          <div className="overflow-hidden text-ellipsis whitespace-nowrap text-[12px] font-semibold">
            {process.name}
          </div>
          <div className="text-[10.5px] text-[#7a7a8c]">{process.meta}</div>
        </div>
      </div>
      <span className="text-right font-mono text-[12px] text-[#9a9aae]">{fmtCompact(process.input)}</span>
      <span className="text-right font-mono text-[12px] text-[#9a9aae]">{fmtCompact(process.output)}</span>
      <span className="text-right font-mono text-[12px] font-semibold text-[#c7c7d4]">
        {fmtCompact(process.tokens)}
      </span>
      <span className="text-right font-mono text-[12.5px] font-bold text-[#6ee7b7]">
        {fmtCost(process.costUsd)}
      </span>
    </div>
  );
}

/**
 * A ticket group header (provider glyph + code + title + the ticket's total
 * spend) followed by its per-process sub-rows. The synthetic run-level group
 * (empty ticket id) collects calls with no ticket attribution.
 */
function AiTicketGroup({
  ticket,
  resolveTicket,
}: {
  ticket: RunAiTicket;
  resolveTicket: (externalId: string) => { title: string; providerKind: string };
}) {
  const { t } = useTranslation("runs");
  const runLevel = ticket.ticketExternalId === "";
  const { title, providerKind } = resolveTicket(ticket.ticketExternalId);
  const [glyph, color] = providerGlyph[providerKind] ?? ["?", "#8b8b9e"];

  return (
    <div>
      <div
        className="flex items-center gap-[11px] p-[12px_18px]"
        style={{ borderTop: "1px solid rgba(255,255,255,.06)", background: "rgba(255,255,255,.025)" }}
      >
        {runLevel ? (
          <span
            className="flex h-[28px] w-[28px] shrink-0 items-center justify-center rounded-[8px]"
            style={{ background: "rgba(139,92,246,.14)" }}
          >
            <Cpu size={15} strokeWidth={2} color="#a78bfa" />
          </span>
        ) : (
          <span
            className="flex h-[28px] w-[28px] shrink-0 items-center justify-center rounded-[8px] text-[12px] font-black text-white"
            style={{ background: color }}
          >
            {glyph}
          </span>
        )}
        <div className="min-w-0 flex-1">
          {runLevel ? (
            <div className="text-[13px] font-bold">{t("detail.ai.runLevel")}</div>
          ) : (
            <>
              <div className="font-mono text-[11px] font-semibold text-violet">
                {ticket.ticketExternalId}
              </div>
              <div className="overflow-hidden text-ellipsis whitespace-nowrap text-[12.5px] font-semibold">
                {title}
              </div>
            </>
          )}
        </div>
        <div className="shrink-0 text-right">
          <div className="font-mono text-[13.5px] font-black tracking-[-.02em] text-[#6ee7b7]">
            {fmtCost(ticket.costUsd)}
          </div>
          <div className="text-[10px] text-[#8b8b9e]">
            {t("detail.ai.tokens", { value: fmtCompact(ticket.tokens) })}
          </div>
        </div>
      </div>
      {ticket.processes.map((p) => (
        <AiProcessRow key={p.key} process={p} />
      ))}
    </div>
  );
}

/**
 * AI usage & cost card — per-run Claude spend read from `GET /runs/{id}/ai-usage`,
 * grouped by ticket with each ticket's processes (Analyze / Generate) as
 * sub-rows. Falls back to the flat per-process list if the API returns no ticket
 * grouping. Rendered at the bottom of the run overview only when there is usage.
 */
function AiUsageCard({
  usage,
  runCode,
  resolveTicket,
}: {
  usage: RunAiUsage;
  runCode: string;
  resolveTicket: (externalId: string) => { title: string; providerKind: string };
}) {
  const { t } = useTranslation("runs");
  const grouped = usage.tickets && usage.tickets.length > 0;
  return (
    <div className="glass mt-[18px] overflow-hidden rounded-[18px]">
      {/* Header */}
      <div
        className="flex items-center gap-[11px] p-[15px_18px]"
        style={{ borderBottom: "1px solid rgba(255,255,255,.06)" }}
      >
        <div
          className="flex h-[30px] w-[30px] shrink-0 items-center justify-center rounded-[9px]"
          style={{ background: "rgba(217,119,87,.16)", border: "1px solid rgba(217,119,87,.3)" }}
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="#D97757">
            <path d="M12 2.4l2.6 6.6 6.9.4-5.3 4.4 1.8 6.7L12 17.3 6 20.9l1.8-6.7L2.5 9.4l6.9-.4z" />
          </svg>
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-[14px] font-bold">{t("detail.ai.title")}</div>
          <div className="text-[11.5px] text-[#8b8b9e]">
            {t("detail.ai.subtitle", { code: runCode, model: usage.modelLabel })}
          </div>
        </div>
        <div className="text-right">
          <div className="font-mono text-[18px] font-black tracking-[-.02em] text-[#6ee7b7]">
            {fmtCost(usage.totalCostUsd)}
          </div>
          <div className="text-[10.5px] text-[#8b8b9e]">
            {t("detail.ai.tokens", { value: fmtCompact(usage.totalTokens) })}
          </div>
        </div>
      </div>

      {/* Desktop: column headers + grid rows (unchanged). */}
      <div className="hidden md:block">
        <div
          className="grid items-center gap-[10px] p-[9px_18px] text-[10px] font-bold tracking-[.05em] text-[#6c6c7e]"
          style={{ gridTemplateColumns: AI_COST_GRID, background: "rgba(255,255,255,.02)" }}
        >
          <span>{grouped ? t("detail.ai.colTicketProcess") : t("detail.ai.colProcess")}</span>
          <span className="text-right">{t("detail.ai.colInput")}</span>
          <span className="text-right">{t("detail.ai.colOutput")}</span>
          <span className="text-right">{t("detail.ai.colTokens")}</span>
          <span className="text-right">{t("detail.ai.colCost")}</span>
        </div>

        {grouped
          ? usage.tickets.map((t) => (
              <AiTicketGroup
                key={t.ticketExternalId || "__run-level"}
                ticket={t}
                resolveTicket={resolveTicket}
              />
            ))
          : usage.processes.map((p) => <AiProcessRow key={p.key} process={p} />)}
      </div>

      {/* Mobile: stacked cards instead of the wide grid table. */}
      <div className="md:hidden">
        {grouped
          ? usage.tickets.map((t) => (
              <AiTicketGroupMobile
                key={t.ticketExternalId || "__run-level"}
                ticket={t}
                resolveTicket={resolveTicket}
              />
            ))
          : usage.processes.map((p) => <AiProcessRowMobile key={p.key} process={p} />)}
      </div>
    </div>
  );
}

/** Mobile stacked-row equivalent of {@link AiProcessRow} (no grid, name + tokens/cost stack). */
function AiProcessRowMobile({ process }: { process: RunAiProcess }) {
  const { t } = useTranslation("runs");
  const Icon = PROCESS_ICON[process.key] ?? Cpu;
  return (
    <div
      className="flex items-center gap-2.5 p-[9px_14px_9px_36px]"
      style={{ borderTop: "1px solid rgba(255,255,255,.045)" }}
    >
      <span
        className="flex h-5 w-5 shrink-0 items-center justify-center rounded-[6px]"
        style={{ background: "rgba(139,92,246,.14)" }}
      >
        <Icon size={11} strokeWidth={2} color="#a78bfa" />
      </span>
      <div className="min-w-0 flex-1">
        <div className="truncate text-[11.5px] font-semibold">{process.name}</div>
        <div className="truncate text-[10px] text-[#7a7a8c]">{process.meta}</div>
      </div>
      <div className="shrink-0 text-right">
        <div className="font-mono text-[11px] font-semibold text-[#c7c7d4]">
          {t("detail.ai.tok", { value: fmtCompact(process.tokens) })}
        </div>
        <div className="font-mono text-[11px] font-bold text-[#6ee7b7]">{fmtCost(process.costUsd)}</div>
      </div>
    </div>
  );
}

/** Mobile stacked-row equivalent of {@link AiTicketGroup} — same header, cards instead of a grid. */
function AiTicketGroupMobile({
  ticket,
  resolveTicket,
}: {
  ticket: RunAiTicket;
  resolveTicket: (externalId: string) => { title: string; providerKind: string };
}) {
  const { t } = useTranslation("runs");
  const runLevel = ticket.ticketExternalId === "";
  const { title, providerKind } = resolveTicket(ticket.ticketExternalId);
  const [glyph, color] = providerGlyph[providerKind] ?? ["?", "#8b8b9e"];

  return (
    <div>
      <div
        className="flex items-center gap-[11px] p-[12px_14px]"
        style={{ borderTop: "1px solid rgba(255,255,255,.06)", background: "rgba(255,255,255,.025)" }}
      >
        {runLevel ? (
          <span
            className="flex h-7 w-7 shrink-0 items-center justify-center rounded-[8px]"
            style={{ background: "rgba(139,92,246,.14)" }}
          >
            <Cpu size={14} strokeWidth={2} color="#a78bfa" />
          </span>
        ) : (
          <span
            className="flex h-7 w-7 shrink-0 items-center justify-center rounded-[8px] text-[11px] font-black text-white"
            style={{ background: color }}
          >
            {glyph}
          </span>
        )}
        <div className="min-w-0 flex-1">
          {runLevel ? (
            <div className="text-[12.5px] font-bold">{t("detail.ai.runLevel")}</div>
          ) : (
            <>
              <div className="font-mono text-[10.5px] font-semibold text-violet">{ticket.ticketExternalId}</div>
              <div className="truncate text-[12px] font-semibold">{title}</div>
            </>
          )}
        </div>
        <div className="shrink-0 text-right">
          <div className="font-mono text-[12.5px] font-black tracking-[-.02em] text-[#6ee7b7]">
            {fmtCost(ticket.costUsd)}
          </div>
          <div className="text-[9.5px] text-[#8b8b9e]">
            {t("detail.ai.tok", { value: fmtCompact(ticket.tokens) })}
          </div>
        </div>
      </div>
      {ticket.processes.map((p) => (
        <AiProcessRowMobile key={p.key} process={p} />
      ))}
    </div>
  );
}
