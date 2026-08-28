import { AlertTriangle, Loader2, RotateCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/Button";
import { useRegenerateRun } from "@/hooks/queries";
import { toast } from "@/lib/toast";
import type { RunDetailOut } from "@/types/api";

/** The run tickets whose analyze+generate failed (#758). */
export function failedRunTickets(run: RunDetailOut | undefined) {
  return (run?.runTickets ?? []).filter((rt) => rt.genStatus === "error");
}

/**
 * Why a run produced no test cases (#758).
 *
 * Review Center used to answer that question with "The AI hasn't generated any
 * test cases for this run yet" in every case — including the case where the AI
 * *had* run and failed on every ticket, which is what an expired Claude
 * credential looks like. That reading was not just unhelpful, it was wrong, and
 * the reason was sitting in `RunTicket.analysis_error` the whole time; it simply
 * was not on the wire.
 *
 * The provider's own message is shown **verbatim**. "Credentials expired",
 * "rate limited" and "no such model" need three different responses from the
 * user, and any paraphrase we write drops exactly the part that distinguishes
 * them.
 */
export function RunGenerationFailure({
  run,
  runId,
}: {
  run: RunDetailOut | undefined;
  runId: number;
}) {
  const { t } = useTranslation("runs");
  const regenerate = useRegenerateRun(runId);
  const failed = failedRunTickets(run);
  if (failed.length === 0) return null;

  const total = run?.runTickets.length ?? failed.length;
  const isTotal = failed.length === total;

  return (
    <div
      data-testid="run-generation-failure"
      className="mb-4 rounded-[18px] px-5 py-[18px]"
      style={{
        background: "rgba(251,113,133,.08)",
        boxShadow: "inset 0 0 0 1px rgba(251,113,133,.24)",
      }}
    >
      <div className="flex items-start gap-3.5">
        <span
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full"
          style={{ background: "rgba(251,113,133,.16)" }}
        >
          <AlertTriangle size={18} strokeWidth={2.3} color="#fb7185" />
        </span>
        <div className="min-w-0 flex-1">
          <h3 className="m-0 text-[15px] font-black tracking-tight" style={{ color: "#fb7185" }}>
            {isTotal ? t("genFailure.allTitle") : t("genFailure.someTitle")}
          </h3>
          <p className="m-0 mt-1 text-[12.5px] text-ink-dim">
            {isTotal
              ? t("genFailure.allBody")
              : t("genFailure.someBody", { failed: failed.length, total })}
          </p>
        </div>
        <Button
          variant="primary"
          data-testid="run-generation-retry"
          disabled={regenerate.isPending}
          onClick={() =>
            regenerate.mutate(undefined, {
              onSuccess: () => toast.success(t("genFailure.retryStarted")),
              onError: (e) =>
                toast.error(e instanceof Error ? e.message : t("genFailure.retryFailed")),
            })
          }
          className="shrink-0"
        >
          {regenerate.isPending ? (
            <Loader2 size={14} strokeWidth={2.4} className="animate-spin" />
          ) : (
            <RotateCw size={14} strokeWidth={2.4} />
          )}
          {t("genFailure.retry")}
        </Button>
      </div>

      <div className="mt-3.5 flex flex-col gap-1.5">
        {failed.map((rt) => (
          <div
            key={rt.ticketExternalId}
            className="rounded-[11px] px-3.5 py-2.5"
            style={{ background: "rgba(0,0,0,.22)" }}
          >
            <div className="font-mono text-[11.5px] font-bold text-[#fda4af]">
              {rt.ticketExternalId}
            </div>
            {/* Unedited, and wrapped rather than truncated: the useful part of a
                CLI error is often at the end. */}
            <div className="mt-0.5 whitespace-pre-wrap break-words text-[12px] leading-[1.45] text-ink-dim">
              {rt.analysisError || t("genFailure.noDetail")}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
