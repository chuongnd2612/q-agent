import { AlertTriangle, ArrowRight, BarChart3, Check, Loader2, RotateCw, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate, useParams } from "react-router-dom";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/misc";
import { useUI } from "@/store/ui";
import {
  useComments,
  useCommentMutations,
  useLinkStatus,
  useReport,
  useRun,
  useRunCases,
} from "@/hooks/queries";
import { cn } from "@/lib/cn";
import type { TicketCommentOut } from "@/types/api";

/**
 * The run's terminal stage (ADR 0015 §6, #731).
 *
 * A **stage**, not a modal, and that is the whole point: a modal dies with the
 * overlay, so a finished run would reopen somewhere arbitrary. Because this is a
 * route, exiting and reopening a finished run lands back here — which is also
 * what makes "Retry failed publish" reachable a day later instead of only in the
 * seconds after the failure.
 *
 * Two variants off ONE input: whether any ticket's comment failed to publish.
 * Per-ticket publish state drives it, so a successful retry flips the screen to
 * the success variant with no extra state to keep in sync.
 */
export function RunComplete() {
  const { t } = useTranslation("runs");
  const navigate = useNavigate();
  const { runId, projectGuid } = useParams();
  const id = Number(runId);
  const { data: run } = useRun(id);
  const { data: comments, isLoading: commentsLoading } = useComments(id);
  const { data: report } = useReport(id);
  const { data: cases } = useRunCases(id);
  const { data: linkStatus } = useLinkStatus(id);
  const { retry } = useCommentMutations(id);
  const openCreateRun = useUI((s) => s.openCreateRun);

  const projectPath = `/projects/${encodeURIComponent(projectGuid ?? "")}`;
  const rows = comments ?? [];
  const failed = rows.filter((c) => c.status === "failed");
  const needsAttention = failed.length > 0;

  // Approved cases that actually reached the provider. The link results are the
  // authority — an approved case whose creation failed was not linked, and
  // counting approvals alone would overstate what the run delivered.
  const approved = (cases ?? []).filter((c) => c.approval === "approved").length;
  const linked = (linkStatus?.results ?? [])
    .filter((r) => r.linked && !r.error)
    .reduce((sum, r) => sum + r.count, 0);

  const figures = [
    { key: "tickets", value: run?.ticketIds.length ?? 0, color: "#a78bfa" },
    { key: "cases", value: linked || approved, color: "#8b5cf6" },
    { key: "passed", value: report?.passed ?? 0, color: "#6ee7b7" },
    { key: "failed", value: report?.failed ?? 0, color: "#fb7185" },
    {
      key: "passRate",
      value: report?.passRate != null ? `${Math.round(report.passRate)}%` : "—",
      color: "#ececf1",
    },
  ];

  if (commentsLoading) {
    return (
      <div className="flex justify-center py-20">
        <Spinner size={22} />
      </div>
    );
  }

  const accent = needsAttention ? "#fbbf24" : "#10b981";

  return (
    <div className="mx-auto w-full max-w-[900px] px-1 pb-6 pt-2">
      {/* ------------------------------------------------------------ banner */}
      <div
        data-testid={needsAttention ? "run-complete-attention" : "run-complete-success"}
        className="mb-5 flex items-start gap-3.5 rounded-[18px] px-5 py-[18px]"
        style={{
          background: needsAttention ? "rgba(251,191,36,.09)" : "rgba(16,185,129,.09)",
          boxShadow: `inset 0 0 0 1px ${needsAttention ? "rgba(251,191,36,.24)" : "rgba(16,185,129,.24)"}`,
        }}
      >
        <span
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full"
          style={{ background: needsAttention ? "rgba(251,191,36,.18)" : "rgba(16,185,129,.18)" }}
        >
          {needsAttention ? (
            <AlertTriangle size={18} strokeWidth={2.3} color={accent} />
          ) : (
            <Check size={18} strokeWidth={3} color={accent} />
          )}
        </span>
        <div className="min-w-0 flex-1">
          <h2 className="m-0 text-[17px] font-black tracking-tight" style={{ color: accent }}>
            {needsAttention ? t("complete.attention.title") : t("complete.success.title")}
          </h2>
          <p className="m-0 mt-1 text-[13px] text-ink-dim">
            {needsAttention
              ? t("complete.attention.body", { failed: failed.length, total: rows.length })
              : t("complete.success.body", { code: run?.code ?? "" })}
          </p>
        </div>
        {needsAttention && (
          <Button
            variant="primary"
            data-testid="run-complete-retry"
            onClick={() => retry.mutate()}
            disabled={retry.isPending}
            className="shrink-0"
          >
            {retry.isPending ? (
              <Loader2 size={14} strokeWidth={2.4} className="animate-spin" />
            ) : (
              <RotateCw size={14} strokeWidth={2.4} />
            )}
            {t("complete.retryFailed")}
          </Button>
        )}
      </div>

      {/* ----------------------------------------------------------- figures */}
      <div className="mb-5 grid grid-cols-2 gap-3 md:grid-cols-5">
        {figures.map((f) => (
          <div key={f.key} className="rounded-[16px] px-[18px] py-[15px]" style={{ background: "rgba(255,255,255,.035)" }}>
            <div className="text-[24px] font-black leading-none tracking-tight" style={{ color: f.color }}>
              {f.value}
            </div>
            <div className="mt-1.5 text-[12.5px] text-ink-dim">{t(`complete.figure.${f.key}`)}</div>
          </div>
        ))}
      </div>

      {/* ------------------------------------------------ per-ticket publish */}
      <div className="mb-5 rounded-[18px] p-1" style={{ background: "rgba(255,255,255,.025)" }}>
        <div className="px-4 pb-2 pt-3 text-[13px] font-bold">{t("complete.publishList")}</div>
        {rows.length === 0 ? (
          <p className="m-0 px-4 pb-4 text-[12.5px] text-ink-dim">{t("complete.noComments")}</p>
        ) : (
          <div className="flex flex-col gap-1.5 p-1.5">
            {rows.map((comment) => (
              <PublishRow key={comment.id} comment={comment} />
            ))}
          </div>
        )}
      </div>

      {/* ------------------------------------------------------------- exits */}
      <div className="flex flex-col gap-2.5 md:flex-row">
        <Button
          variant="primary"
          data-testid="run-complete-reports"
          onClick={() => navigate(`${projectPath}/reports`)}
          className="w-full md:w-auto"
        >
          <BarChart3 size={15} strokeWidth={2.2} />
          {t("complete.openReports")}
        </Button>
        <Button
          variant="glass"
          data-testid="run-complete-another"
          onClick={openCreateRun}
          className="w-full md:w-auto"
        >
          <ArrowRight size={15} strokeWidth={2.2} />
          {t("complete.startAnother")}
        </Button>
      </div>
    </div>
  );
}

function PublishRow({ comment }: { comment: TicketCommentOut }) {
  const { t } = useTranslation("runs");
  const isFailed = comment.status === "failed";
  const isPublished = comment.status === "published";
  return (
    <div
      className="flex items-center gap-3 rounded-[13px] px-3.5 py-2.5"
      style={{ background: "rgba(255,255,255,.03)" }}
    >
      <span
        className="flex h-[22px] w-[22px] shrink-0 items-center justify-center rounded-full"
        style={{
          background: isFailed
            ? "rgba(251,113,133,.16)"
            : isPublished
              ? "rgba(16,185,129,.16)"
              : "rgba(255,255,255,.06)",
        }}
      >
        {isFailed ? (
          <X size={12} strokeWidth={3} color="#fb7185" />
        ) : isPublished ? (
          <Check size={12} strokeWidth={3} color="#6ee7b7" />
        ) : null}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[13px] font-semibold">{comment.ticketExternalId}</span>
        {/* The provider's own message, not a generic "something went wrong" —
            it is the only thing that tells the user whether a retry can help. */}
        {isFailed && comment.errorMessage && (
          <span className="block truncate text-[11.5px] text-ink-dim">{comment.errorMessage}</span>
        )}
      </span>
      <span
        className={cn("shrink-0 text-[11.5px] font-semibold")}
        style={{ color: isFailed ? "#fb7185" : isPublished ? "#6ee7b7" : "#8b8b9e" }}
      >
        {t(`complete.status.${isFailed ? "failed" : isPublished ? "published" : "pending"}`)}
      </span>
    </div>
  );
}
