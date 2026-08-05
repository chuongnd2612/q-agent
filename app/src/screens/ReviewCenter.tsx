import { AnimatePresence, motion } from "framer-motion";
import { Check, ChevronDown, ChevronRight, Plus, Sparkles, X } from "lucide-react";
import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/Button";
import { GlassCard } from "@/components/ui/GlassCard";
import { PipelineRail } from "@/components/ui/PipelineRail";
import { approvalStyle, Pill, priorityBg, priorityColor } from "@/components/ui/badges";
import { Select } from "@/components/ui/Dropdown";
import { EmptyState, ErrorState, Spinner } from "@/components/ui/misc";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useCaseMutations, useCreateAndLink, useRun, useRunCases } from "@/hooks/queries";
import { useUI, type CaseDraft } from "@/store/ui";
import type { TestCaseOut } from "@/types/api";

/** Groups the run's flat case list by ticket, preserving first-seen order. */
function groupByTicket(cases: TestCaseOut[]): Array<{ ticketExternalId: string; cases: TestCaseOut[] }> {
  const order: string[] = [];
  const byTicket = new Map<string, TestCaseOut[]>();
  for (const c of cases) {
    if (!byTicket.has(c.ticketExternalId)) {
      order.push(c.ticketExternalId);
      byTicket.set(c.ticketExternalId, []);
    }
    byTicket.get(c.ticketExternalId)!.push(c);
  }
  return order.map((tid) => ({ ticketExternalId: tid, cases: byTicket.get(tid)! }));
}

const platformIcon: Record<string, string> = { Web: "🖥", Mobile: "📱", API: "🔌" };

export function ReviewCenter() {
  const { t } = useTranslation("runs");
  const { t: tCommon } = useTranslation("common");
  const runId = Number(useParams().runId);
  const navigate = useNavigate();
  const { data: run } = useRun(runId);
  const { data: cases, isLoading, isError, refetch } = useRunCases(runId);
  const { setApproval, regenerateCase, approveAll, approveTicket, updateCase } =
    useCaseMutations(runId);
  const createAndLink = useCreateAndLink(runId);

  // Which ticket accordion is expanded — a deep-linkable selection in the URL.
  const [searchParams, setSearchParams] = useSearchParams();
  const reviewOpenTicket = searchParams.get("ticket");
  const toggleReviewTicket = (tid: string) =>
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (next.get("ticket") === tid) next.delete("ticket");
        else next.set("ticket", tid);
        return next;
      },
      { replace: true },
    );

  const reviewSel = useUI((s) => s.reviewSel);
  const toggleReviewSel = useUI((s) => s.toggleReviewSel);
  const clearReviewSel = useUI((s) => s.clearReviewSel);

  const selectedIds = useMemo(
    () => Object.keys(reviewSel).filter((k) => reviewSel[Number(k)]).map(Number),
    [reviewSel],
  );

  const approveSelected = () => {
    selectedIds.forEach((caseId) => setApproval.mutate({ caseId, approval: "approved" }));
    clearReviewSel();
  };
  const startCreateLink = (link: boolean, dryRun = false) => {
    createAndLink.mutate({ link, dryRun });
    navigate("/runs/" + runId + "/sync");
  };

  const tickets = useMemo(() => groupByTicket(cases ?? []), [cases]);

  const stats = useMemo(() => {
    const all = cases ?? [];
    const approved = all.filter((c) => c.approval === "approved").length;
    const rejected = all.filter((c) => c.approval === "rejected").length;
    const pending = all.length - approved - rejected;
    const pct = all.length ? Math.round((approved / all.length) * 100) : 0;
    return { approved, rejected, pending, pct };
  }, [cases]);

  return (
    <div className="animate-fade-in-up px-1 pb-10 pt-0.5">
      <div className="mb-3.5 flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="mb-1 text-[13px] font-medium text-muted">
            {run?.code} &middot; {run?.name}
          </div>
          <h1 className="m-0 text-[24px] font-black tracking-tight md:text-[28px]">{t("review.title")}</h1>
        </div>
        <div className="hidden flex-wrap justify-end gap-2.5 md:flex">
          {selectedIds.length > 0 && (
            <Button variant="success" onClick={approveSelected}>
              {t("review.approveSelected", { count: selectedIds.length })}
            </Button>
          )}
          <Button variant="glass" onClick={() => approveAll.mutate()} disabled={approveAll.isPending}>
            {t("review.approveAll")}
          </Button>
          <Button
            variant="glass"
            onClick={() => startCreateLink(false, true)}
            title={t("review.createLocallyHint")}
          >
            {t("review.createLocally")}
          </Button>
          <Button
            onClick={() => startCreateLink(false)}
            className="border-[rgba(139,92,246,.32)] bg-[rgba(139,92,246,.16)] text-[#c4b5fd] hover:bg-[rgba(139,92,246,.24)]"
          >
            {t("review.createCases")}
          </Button>
          <Button variant="primary" onClick={() => startCreateLink(true)}>
            {t("review.createAndLink")}
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M5 12h14M13 6l6 6-6 6" />
            </svg>
          </Button>
        </div>
      </div>

      <div className="mb-3.5 hidden md:block">
        <PipelineRail stage={1} />
      </div>

      <div className="mb-4 grid grid-cols-3 gap-3 md:grid-cols-4">
        <GlassCard className="px-[18px] py-4">
          <div className="text-[26px] font-black leading-none text-success-soft">{stats.approved}</div>
          <div className="mt-[5px] text-xs text-muted">{t("review.stat.approved")}</div>
        </GlassCard>
        <GlassCard className="px-[18px] py-4">
          <div className="text-[26px] font-black leading-none text-warning-soft">{stats.pending}</div>
          <div className="mt-[5px] text-xs text-muted">{t("review.stat.needsReview")}</div>
        </GlassCard>
        <GlassCard className="px-[18px] py-4">
          <div className="text-[26px] font-black leading-none text-danger-soft">{stats.rejected}</div>
          <div className="mt-[5px] text-xs text-muted">{t("review.stat.rejected")}</div>
        </GlassCard>
        <div
          className="col-span-3 rounded-[20px] border px-[18px] py-4 md:col-span-1"
          style={{
            background: "linear-gradient(135deg,rgba(139,92,246,.16),rgba(99,102,241,.08))",
            borderColor: "rgba(139,92,246,.26)",
          }}
        >
          <div className="text-[26px] font-black leading-none">{stats.pct}%</div>
          <div className="mt-[5px] text-xs text-ink-soft">{t("review.stat.reviewed")}</div>
        </div>
      </div>

      {isLoading && (
        <div className="flex flex-col gap-2.5">
          {[0, 1, 2].map((i) => (
            <div key={i} className="glass h-[74px] animate-pulse rounded-[18px]" />
          ))}
        </div>
      )}

      {/* A failed load is NOT an empty list (#491). */}
      {!isLoading && isError && (
        <ErrorState
          title={tCommon("loadFailed.title")}
          body={tCommon("loadFailed.body")}
          retryLabel={tCommon("loadFailed.retry")}
          onRetry={() => void refetch()}
        />
      )}

      {!isLoading && !isError && tickets.length === 0 && (
        <EmptyState
          icon={<Sparkles size={30} className="text-violet" />}
          title={t("review.empty.title")}
          body={t("review.empty.body")}
        />
      )}

      <div className="flex flex-col gap-2.5">
        {tickets.map((rt) => (
          <TicketAccordion
            key={rt.ticketExternalId}
            ticketExternalId={rt.ticketExternalId}
            cases={rt.cases}
            open={reviewOpenTicket === rt.ticketExternalId}
            onToggle={() => toggleReviewTicket(rt.ticketExternalId)}
            onApproveTicket={() => approveTicket.mutate(rt.ticketExternalId)}
            approveTicketPending={approveTicket.isPending}
            onSetApproval={(caseId, approval) => setApproval.mutate({ caseId, approval })}
            onRegenerate={(caseId) => regenerateCase.mutate(caseId)}
            regeneratingCaseId={regenerateCase.isPending ? (regenerateCase.variables as number) : null}
            onSave={(caseId, body) => updateCase.mutate({ caseId, body })}
            onSetAutomation={(caseId, automation) => updateCase.mutate({ caseId, body: { automation } })}
          />
        ))}
      </div>

      {/* Mobile sticky bottom action bar — progress + approve/continue, replacing
          the desktop header's button row (MOBILE_SPEC §4 pattern 3). Opaque bg
          (no backdrop-filter) since it layers over the animated background. */}
      {(cases?.length ?? 0) > 0 && (
        <div
          className="sticky bottom-0 z-10 -mx-1 mt-4 border-t border-white/[0.08] px-4 pb-[max(12px,env(safe-area-inset-bottom))] pt-3 md:hidden"
          style={{ background: "rgba(12,12,18,.92)" }}
        >
          <div className="mb-2 flex items-center justify-between text-[11.5px] font-semibold text-ink-dim">
            <span>
              {t("review.approvedCount", { approved: stats.approved, total: cases?.length ?? 0 })}
            </span>
            <span>{stats.pct}%</span>
          </div>
          <div className="mb-3 h-[6px] overflow-hidden rounded-full bg-white/[0.08]">
            <div
              className="h-full rounded-full"
              style={{ width: `${stats.pct}%`, background: "linear-gradient(90deg,#8b5cf6,#22d3ee)" }}
            />
          </div>
          <div className="flex gap-2.5">
            <Button
              variant="glass"
              className="flex-1"
              onClick={() => (selectedIds.length > 0 ? approveSelected() : approveAll.mutate())}
              disabled={approveAll.isPending}
            >
              {selectedIds.length > 0
                ? t("review.approveSelected", { count: selectedIds.length })
                : t("review.approveAll")}
            </Button>
            <Button
              variant="primary"
              className="flex-[1.35] disabled:opacity-50"
              onClick={() => startCreateLink(true)}
              disabled={stats.approved === 0}
            >
              {t("review.continueToSync")}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

function TicketAccordion({
  ticketExternalId,
  cases,
  open,
  onToggle,
  onApproveTicket,
  approveTicketPending,
  onSetApproval,
  onRegenerate,
  regeneratingCaseId,
  onSave,
  onSetAutomation,
}: {
  ticketExternalId: string;
  cases: TestCaseOut[];
  open: boolean;
  onToggle: () => void;
  onApproveTicket: () => void;
  approveTicketPending: boolean;
  onSetApproval: (caseId: number, approval: "approved" | "rejected") => void;
  onRegenerate: (caseId: number) => void;
  regeneratingCaseId: number | null;
  onSave: (caseId: number, body: CaseDraft) => void;
  onSetAutomation: (caseId: number, automation: string) => void;
}) {
  const { t } = useTranslation("runs");
  const approved = cases.filter((c) => c.approval === "approved").length;
  const pending = cases.length - approved;
  const total = cases.length;
  const pct = total ? Math.round((approved / total) * 100) : 0;
  const fullyApproved = total > 0 && approved === total;
  const title = cases[0]?.title ?? ticketExternalId;

  return (
    <GlassCard className="overflow-hidden" tilt={false}>
      <div
        className="flex cursor-pointer items-center gap-3 px-4 py-3.5 md:gap-3.5 md:px-[18px] md:py-4"
        onClick={onToggle}
      >
        <div
          className="flex h-[42px] w-[42px] shrink-0 items-center justify-center rounded-full"
          style={{ background: `conic-gradient(#8b5cf6 ${pct}%, rgba(255,255,255,.08) ${pct}%)` }}
        >
          <div
            className="flex h-8 w-8 items-center justify-center rounded-full text-[11px] font-extrabold"
            style={{ background: "#14141c", color: pct === 100 ? "#6ee7b7" : "#a78bfa" }}
          >
            {pct}%
          </div>
        </div>
        <div className="min-w-0 flex-1">
          <div className="mb-0.5 flex items-center gap-2.5">
            <span className="font-mono text-xs font-semibold text-violet">{ticketExternalId}</span>
            <span className="hidden text-[11px] text-faint md:inline">{t("review.ticket.testCases", { count: total })}</span>
          </div>
          <div className="truncate text-[14.5px] font-semibold">{title}</div>
        </div>
        {/* Mobile: compact approved/total count pill (desktop shows the two pills + button instead). */}
        <span
          className="shrink-0 rounded-full px-2.5 py-[3px] text-[11px] font-bold md:hidden"
          style={{ color: "#a78bfa", background: "rgba(139,92,246,.14)" }}
        >
          {approved}/{total}
        </span>
        <div className="hidden shrink-0 items-center gap-1.5 text-[11.5px] font-semibold md:flex">
          <Pill color="#6ee7b7" bg="rgba(16,185,129,.14)">
            {t("review.ticket.approvedCount", { count: approved })}
          </Pill>
          <Pill color="#fbbf24" bg="rgba(251,191,36,.14)">
            {t("review.ticket.pendingCount", { count: pending })}
          </Pill>
        </div>
        <button
          onClick={(e) => {
            e.stopPropagation();
            onApproveTicket();
          }}
          disabled={fullyApproved || approveTicketPending}
          className="hidden items-center gap-1.5 rounded-[10px] border px-3 py-1.5 text-[11.5px] font-semibold disabled:cursor-default md:flex"
          style={
            fullyApproved
              ? { background: "rgba(16,185,129,.16)", borderColor: "rgba(16,185,129,.3)", color: "#6ee7b7" }
              : { background: "rgba(255,255,255,.05)", borderColor: "rgba(255,255,255,.1)", color: "#c3c3d0" }
          }
        >
          {fullyApproved && <Check size={13} />}
          {fullyApproved ? t("review.ticket.allApproved") : t("review.ticket.approveAll")}
        </button>
        <ChevronDown
          size={16}
          className="shrink-0 text-muted transition-transform"
          style={{ transform: open ? "rotate(180deg)" : undefined }}
        />
      </div>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.25, ease: "easeOut" }}
            className="overflow-hidden"
          >
            <div className="flex flex-col gap-2.5 px-4 pb-3.5 md:px-[18px] md:pb-4">
              {/* Mobile-only: the desktop header's "Approve all" button moves here,
                  above the per-case list (MOBILE_SPEC §3 Review Center). */}
              <button
                onClick={onApproveTicket}
                disabled={fullyApproved || approveTicketPending}
                className="flex w-full items-center justify-center gap-1.5 rounded-[10px] border px-3 py-2 text-[12.5px] font-semibold disabled:cursor-default md:hidden"
                style={
                  fullyApproved
                    ? { background: "rgba(16,185,129,.16)", borderColor: "rgba(16,185,129,.3)", color: "#6ee7b7" }
                    : { background: "rgba(255,255,255,.05)", borderColor: "rgba(255,255,255,.1)", color: "#c3c3d0" }
                }
              >
                {fullyApproved && <Check size={13} />}
                {fullyApproved ? t("review.ticket.allApproved") : t("review.ticket.approveAllCases", { count: total })}
              </button>
              {cases.map((c) => (
                <CaseRow
                  key={c.id}
                  testCase={c}
                  regenerating={regeneratingCaseId === c.id}
                  onSetApproval={(approval) => onSetApproval(c.id, approval)}
                  onRegenerate={() => onRegenerate(c.id)}
                  onSave={(body) => onSave(c.id, body)}
                  onSetAutomation={(automation) => onSetAutomation(c.id, automation)}
                />
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </GlassCard>
  );
}

function CaseRow({
  testCase: c,
  regenerating,
  onSetApproval,
  onRegenerate,
  onSave,
  onSetAutomation,
}: {
  testCase: TestCaseOut;
  regenerating: boolean;
  onSetApproval: (approval: "approved" | "rejected") => void;
  onRegenerate: () => void;
  onSave: (body: CaseDraft) => void;
  onSetAutomation: (automation: string) => void;
}) {
  const { t } = useTranslation("runs");
  const automationOptions = [
    { value: "Playwright", label: "Playwright" },
    { value: "Selenium", label: "Selenium" },
    { value: "Cypress", label: "Cypress" },
    { value: "Manual", label: t("review.case.manualOption") },
  ];
  const expandedCase = useUI((s) => s.expandedCase);
  const toggleCase = useUI((s) => s.toggleCase);
  const editingCase = useUI((s) => s.editingCase);
  const draft = useUI((s) => s.draft);
  const startEdit = useUI((s) => s.startEdit);
  const updateDraft = useUI((s) => s.updateDraft);
  const cancelEdit = useUI((s) => s.cancelEdit);
  const selected = useUI((s) => !!s.reviewSel[c.id]);
  const toggleReviewSel = useUI((s) => s.toggleReviewSel);

  const expanded = expandedCase === c.id;
  const isEditing = editingCase === c.id;
  const [color, label, bg] = approvalStyle(c.approval);
  const prColor = priorityColor(c.priority);
  const prBg = priorityBg(c.priority);
  const statusBorder =
    c.approval === "approved"
      ? "rgba(16,185,129,.2)"
      : c.approval === "rejected"
        ? "rgba(244,63,94,.2)"
        : "rgba(255,255,255,.07)";

  return (
    <div className="overflow-hidden rounded-[14px] border" style={{ background: "rgba(255,255,255,.03)", borderColor: statusBorder }}>
      <div className="flex cursor-pointer items-center gap-2 px-3 py-2.5 md:gap-2.5 md:px-[15px] md:py-3" onClick={() => toggleCase(c.id)}>
        <div
          onClick={(e) => {
            e.stopPropagation();
            toggleReviewSel(c.id);
          }}
          className="flex h-[18px] w-[18px] shrink-0 cursor-pointer items-center justify-center rounded-[6px] border transition-colors"
          style={{
            background: selected ? "linear-gradient(135deg,#8b5cf6,#6366f1)" : "rgba(255,255,255,.04)",
            borderColor: selected ? "transparent" : "rgba(255,255,255,.14)",
          }}
        >
          {selected && <Check size={12} color="#fff" strokeWidth={3.2} />}
        </div>
        <ChevronRight
          size={15}
          className="shrink-0 text-muted transition-transform"
          style={{ transform: expanded ? "rotate(90deg)" : undefined }}
        />
        <span className="shrink-0 font-mono text-[11.5px] font-semibold text-violet">{c.code}</span>
        <span className="flex-1 truncate text-[13.5px] font-semibold leading-snug">{c.title}</span>
        {regenerating && (
          <span className="hidden shrink-0 items-center gap-1.5 text-[11px] font-semibold text-violet md:flex">
            <Spinner size={12} />
            {t("review.case.regenerating")}
          </span>
        )}
        <Pill color={color} bg={bg}>
          {label}
        </Pill>
        <Pill color={prColor} bg={prBg}>
          {c.priority}
        </Pill>
      </div>
      <AnimatePresence initial={false}>
        {expanded && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.2 }}
            className="px-3 pb-3 pl-3 md:px-[15px] md:pb-[15px] md:pl-[41px]"
          >
            {!isEditing ? (
              <div>
                <div className="mb-3 flex flex-wrap items-center gap-2">
                  <span className="rounded-lg bg-white/5 px-2.5 py-[3px] text-[11px] text-ink-dim">
                    {t("review.case.type")} <b className="text-ink-soft">{c.testType}</b>
                  </span>
                  <span className="flex items-center gap-1.5 rounded-lg bg-white/5 px-2.5 py-[3px] text-[11px] text-ink-dim">
                    {platformIcon[c.platform] ?? "🖥"} {c.platform}
                  </span>
                  {/* Automation type is editable — only non-Manual cases are automatable. */}
                  <span className="ml-1 text-[11px] text-faint">{t("review.case.automation")}</span>
                  <Select
                    value={c.automation}
                    options={automationOptions}
                    placeholder="Manual"
                    allowClear={false}
                    onChange={(v) => v && v !== c.automation && onSetAutomation(v)}
                  />
                  {c.automation === "Manual" && (
                    <span className="text-[11px] text-[#94a3b8]">
                      {t("review.case.automationHint")}
                    </span>
                  )}
                </div>
                {c.objective ? (
                  <>
                    <div className="mb-1.5 text-[11px] font-semibold tracking-wider text-faint">{t("review.case.objective")}</div>
                    <p className="m-0 mb-3 text-[12.5px] leading-relaxed text-ink-soft">{c.objective}</p>
                  </>
                ) : null}
                <div className="mb-1.5 text-[11px] font-semibold tracking-wider text-faint">{t("review.case.precondition")}</div>
                <p className="m-0 mb-3 text-[12.5px] leading-relaxed text-ink-soft">{c.precondition || "—"}</p>
                {c.testData?.length ? (
                  <>
                    <div className="mb-1.5 text-[11px] font-semibold tracking-wider text-faint">{t("review.case.testData")}</div>
                    <div className="mb-3 flex flex-wrap gap-2">
                      {c.testData.map((d, i) => (
                        <span
                          key={i}
                          className="rounded-lg bg-white/5 px-2.5 py-[3px] text-[11px] text-ink-dim"
                        >
                          {d.field}: <b className="text-ink-soft">{d.value}</b>
                        </span>
                      ))}
                    </div>
                  </>
                ) : null}
                <div className="mb-3 overflow-hidden rounded-[11px] border border-white/[0.07]">
                  <div className="hidden grid-cols-[28px_1fr_1fr] gap-2.5 bg-white/[0.04] px-3 py-2 text-[10px] font-bold tracking-wider text-faint md:grid">
                    <span>{t("review.case.colNum")}</span>
                    <span>{t("review.case.colAction")}</span>
                    <span>{t("review.case.colExpected")}</span>
                  </div>
                  {c.steps.map((st, i) => (
                    <div
                      key={i}
                      className="grid grid-cols-[20px_1fr] gap-2 border-t border-white/5 px-3 py-2.5 text-xs leading-snug md:grid-cols-[28px_1fr_1fr] md:gap-2.5"
                    >
                      <span className="font-mono text-violet">{i + 1}</span>
                      <div className="flex flex-col gap-1 md:contents">
                        <span className="text-ink-soft">{st.a}</span>
                        <span className="text-ink-dim">
                          <span className="mr-1 text-faint md:hidden">&#8594;</span>
                          {st.e}
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
                {c.linkedAc?.length ? (
                  <div className="mb-3 flex flex-wrap items-center gap-1.5">
                    <span className="text-[11px] font-semibold tracking-wider text-faint">{t("review.case.coversAc")}</span>
                    {c.linkedAc.map((ac, i) => (
                      <span
                        key={i}
                        className="rounded-lg bg-white/5 px-2.5 py-[3px] text-[11px] text-ink-soft"
                      >
                        {ac}
                      </span>
                    ))}
                  </div>
                ) : null}
                <div className="flex flex-wrap gap-2">
                  <button
                    onClick={() => onSetApproval("approved")}
                    className="flex items-center gap-1.5 rounded-[10px] px-[13px] py-[7px] text-xs font-semibold"
                    style={
                      c.approval === "approved"
                        ? { background: "rgba(16,185,129,.16)", border: "1px solid rgba(16,185,129,.3)", color: "#6ee7b7" }
                        : { background: "rgba(255,255,255,.05)", border: "1px solid rgba(255,255,255,.1)", color: "#c3c3d0" }
                    }
                  >
                    <Check size={12} />
                    {c.approval === "approved" ? t("review.case.approved") : t("review.case.approve")}
                  </button>
                  <button
                    onClick={() => onSetApproval("rejected")}
                    className="flex items-center gap-1.5 rounded-[10px] px-[13px] py-[7px] text-xs font-semibold"
                    style={
                      c.approval === "rejected"
                        ? { background: "rgba(244,63,94,.13)", border: "1px solid rgba(244,63,94,.28)", color: "#fb7185" }
                        : { background: "rgba(255,255,255,.05)", border: "1px solid rgba(255,255,255,.1)", color: "#c3c3d0" }
                    }
                  >
                    <X size={12} />
                    {c.approval === "rejected" ? t("review.case.rejected") : t("review.case.reject")}
                  </button>
                  <button
                    onClick={onRegenerate}
                    disabled={regenerating}
                    className="flex items-center gap-1.5 rounded-[10px] border border-white/10 bg-white/5 px-[13px] py-[7px] text-xs font-semibold text-ink-soft disabled:opacity-70"
                  >
                    {regenerating ? (
                      <>
                        <Spinner size={12} />
                        {t("review.case.regeneratingBusy")}
                      </>
                    ) : (
                      <>
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                          <path d="M23 4v6h-6M1 20v-6h6" />
                          <path d="M3.5 9a9 9 0 0 1 14.8-3.4L23 10M1 14l4.7 4.4A9 9 0 0 0 20.5 15" />
                        </svg>
                        {t("review.case.regenerate")}
                      </>
                    )}
                  </button>
                  <button
                    onClick={() =>
                      startEdit(c.id, {
                        title: c.title,
                        precondition: c.precondition,
                        steps: c.steps,
                        testData: c.testData ?? [],
                      })
                    }
                    className="flex items-center gap-1.5 rounded-[10px] border border-white/10 bg-white/5 px-[13px] py-[7px] text-xs font-semibold text-ink-soft hover:bg-white/10 md:ml-auto"
                  >
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z" />
                    </svg>
                    {t("review.case.edit")}
                  </button>
                </div>
              </div>
            ) : (
              draft && (
                <motion.div initial={{ opacity: 0, y: -4 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.2 }}>
                  <div className="mb-1.5 text-[11px] font-semibold tracking-wider text-faint">{t("review.case.titleLabel")}</div>
                  <input
                    value={draft.title}
                    onChange={(e) => updateDraft({ title: e.target.value })}
                    className="mb-3 w-full rounded-[10px] border border-[rgba(139,92,246,.3)] bg-white/5 px-3 py-[9px] text-[13px] text-ink outline-none focus:border-[rgba(139,92,246,.6)]"
                  />
                  <div className="mb-1.5 text-[11px] font-semibold tracking-wider text-faint">{t("review.case.precondition")}</div>
                  <textarea
                    value={draft.precondition}
                    onChange={(e) => updateDraft({ precondition: e.target.value })}
                    rows={3}
                    className="mb-3 w-full resize-y rounded-[10px] border border-[rgba(139,92,246,.3)] bg-white/5 px-3 py-[9px] text-[13px] leading-relaxed text-ink outline-none focus:border-[rgba(139,92,246,.6)]"
                  />
                  <div className="mb-1.5 text-[11px] font-semibold tracking-wider text-faint">{t("review.case.testData")}</div>
                  <div className="mb-3 flex flex-col gap-2">
                    {draft.testData.map((d, i) => (
                      <div
                        key={i}
                        className="grid grid-cols-[1fr_1fr_auto] items-center gap-2"
                      >
                        <input
                          value={d.field}
                          placeholder={t("review.case.fieldPlaceholder")}
                          onChange={(e) => {
                            const testData = draft.testData.map((row, j) =>
                              j === i ? { ...row, field: e.target.value } : row,
                            );
                            updateDraft({ testData });
                          }}
                          className="w-full rounded-[9px] border border-white/[0.12] bg-white/5 px-[11px] py-2 text-[12.5px] text-ink-soft outline-none focus:border-[rgba(139,92,246,.5)]"
                        />
                        <input
                          value={d.value}
                          placeholder={t("review.case.valuePlaceholder")}
                          onChange={(e) => {
                            const testData = draft.testData.map((row, j) =>
                              j === i ? { ...row, value: e.target.value } : row,
                            );
                            updateDraft({ testData });
                          }}
                          className="w-full rounded-[9px] border border-white/[0.12] bg-white/5 px-[11px] py-2 text-[12.5px] text-ink-dim outline-none focus:border-[rgba(139,92,246,.5)]"
                        />
                        <button
                          type="button"
                          aria-label={t("review.case.removeFieldAria", { num: i + 1 })}
                          title={t("review.case.removeField")}
                          onClick={() => updateDraft({ testData: draft.testData.filter((_, j) => j !== i) })}
                          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-[9px] border border-white/[0.12] bg-white/5 text-ink-dim hover:border-[rgba(248,113,113,.5)] hover:text-red-400"
                        >
                          <X size={13} />
                        </button>
                      </div>
                    ))}
                    <button
                      onClick={() => updateDraft({ testData: [...draft.testData, { field: "", value: "" }] })}
                      className="flex items-center gap-1.5 self-start rounded-[9px] border border-dashed border-white/[0.16] bg-white/[0.04] px-3 py-1.5 text-xs font-semibold text-ink-dim hover:bg-white/[0.08]"
                    >
                      <Plus size={12} />
                      {t("review.case.addField")}
                    </button>
                  </div>
                  <div className="mb-1.5 text-[11px] font-semibold tracking-wider text-faint">{t("review.case.steps")}</div>
                  <div className="mb-3 flex flex-col gap-2">
                    {draft.steps.map((st, i) => (
                      <div
                        key={i}
                        className="grid grid-cols-[20px_1fr_auto] items-start gap-2 md:grid-cols-[24px_1fr_1fr_auto] md:items-center"
                      >
                        <span className="pt-2 font-mono text-xs text-violet md:pt-0">{i + 1}</span>
                        <div className="flex flex-col gap-2 md:contents">
                          <input
                            value={st.a}
                            placeholder={t("review.case.actionPlaceholder")}
                            onChange={(e) => {
                              const steps = draft.steps.map((s, j) => (j === i ? { ...s, a: e.target.value } : s));
                              updateDraft({ steps });
                            }}
                            className="w-full rounded-[9px] border border-white/[0.12] bg-white/5 px-[11px] py-2 text-[12.5px] text-ink-soft outline-none focus:border-[rgba(139,92,246,.5)]"
                          />
                          <input
                            value={st.e}
                            placeholder={t("review.case.expectedPlaceholder")}
                            onChange={(e) => {
                              const steps = draft.steps.map((s, j) => (j === i ? { ...s, e: e.target.value } : s));
                              updateDraft({ steps });
                            }}
                            className="w-full rounded-[9px] border border-white/[0.12] bg-white/5 px-[11px] py-2 text-[12.5px] text-ink-dim outline-none focus:border-[rgba(139,92,246,.5)]"
                          />
                        </div>
                        <button
                          type="button"
                          aria-label={t("review.case.removeStepAria", { num: i + 1 })}
                          title={t("review.case.removeStep")}
                          onClick={() => updateDraft({ steps: draft.steps.filter((_, j) => j !== i) })}
                          className="mt-1.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-[9px] border border-white/[0.12] bg-white/5 text-ink-dim hover:border-[rgba(248,113,113,.5)] hover:text-red-400 md:mt-0"
                        >
                          <X size={13} />
                        </button>
                      </div>
                    ))}
                    <button
                      onClick={() => updateDraft({ steps: [...draft.steps, { a: "", e: "" }] })}
                      className="flex items-center gap-1.5 self-start rounded-[9px] border border-dashed border-white/[0.16] bg-white/[0.04] px-3 py-1.5 text-xs font-semibold text-ink-dim hover:bg-white/[0.08]"
                    >
                      <Plus size={12} />
                      {t("review.case.addStep")}
                    </button>
                  </div>
                  <div className="flex gap-2">
                    <Button
                      variant="primary"
                      size="sm"
                      onClick={() => {
                        onSave({
                          ...draft,
                          testData: draft.testData.filter((d) => d.field.trim() || d.value.trim()),
                        });
                        cancelEdit();
                      }}
                    >
                      <Check size={13} />
                      {t("review.case.saveChanges")}
                    </Button>
                    <Button variant="glass" size="sm" onClick={cancelEdit}>
                      {t("review.case.cancel")}
                    </Button>
                  </div>
                </motion.div>
              )
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
