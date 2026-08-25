import { ArrowRight, Check, Link2, RefreshCw, Sparkles } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "@/lib/toast";
import { Button } from "@/components/ui/Button";
import { PipelineRail } from "@/components/ui/PipelineRail";
import { providerGlyph } from "@/components/ui/badges";
import { Spinner } from "@/components/ui/misc";
import { providerLabel } from "@/data/projects";
import { useNavigate, useParams } from "react-router-dom";
import { useIsMutating } from "@tanstack/react-query";
import {
  ALL_TICKETS_PAGE_SIZE,
  CREATE_LINK_MUTATION_KEY,
  useCreateAndLink,
  useGenerateAutomation,
  useLinkStatus,
  useRun,
  useTickets,
} from "@/hooks/queries";
import type { LinkTicketResult, ProviderKind } from "@/types/api";

/**
 * Create & Link Test Cases — creates approved cases in the provider and links
 * them to each work item (pipeline stage between Review and Automation).
 */
export function CreateLinkSync() {
  const { t } = useTranslation("pipeline");
  const runId = Number(useParams().runId);
  const navigate = useNavigate();
  const { data: run } = useRun(runId);
  const { data: ticketsPage } = useTickets({ pageSize: ALL_TICKETS_PAGE_SIZE });
  const tickets = ticketsPage?.items;
  const { data: status } = useLinkStatus(runId);
  const createAndLink = useCreateAndLink(runId);
  const generateAutomation = useGenerateAutomation(runId);

  // Local mode: create cases locally only, never write to the live provider.
  // Persisted so the choice sticks across visits during local development.
  const [localMode, setLocalMode] = useState(
    () => localStorage.getItem("qagent.localCreateLink") === "1",
  );
  const toggleLocalMode = (on: boolean) => {
    setLocalMode(on);
    localStorage.setItem("qagent.localCreateLink", on ? "1" : "0");
  };

  // Review fires the mutation and navigates here in the same tick, so this screen
  // renders while the request is still in flight and `linkStatus` still says "idle"
  // — which showed the idle panel, i.e. the same big Create button, and read as a
  // lost click (#694). The mutation is keyed so it can be seen from here, where it
  // was started elsewhere.
  const starting = useIsMutating({ mutationKey: CREATE_LINK_MUTATION_KEY }) > 0;
  const state = starting ? "running" : (status?.status ?? "idle");
  const results = status?.results ?? [];
  const byTicket = new Map<string, LinkTicketResult>(results.map((r) => [r.ticketExternalId, r]));

  const runTickets = run?.runTickets ?? [];
  const providerOf = (tid: string): ProviderKind =>
    (tickets?.find((t) => t.externalId === tid)?.providerKind ??
      byTicket.get(tid)?.providerKind ??
      "ado") as ProviderKind;

  return (
    <div className="px-1 pb-10 pt-0.5">
      <div className="mb-3.5 flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="mb-[5px] text-[13px] font-medium text-ink-dim">
            {run?.code} &middot; {t("createLink.header.subtitle")}
          </div>
          <h1 className="m-0 text-[24px] font-black tracking-tight md:text-[28px]">{t("createLink.header.title")}</h1>
        </div>
        {state === "done" && (
          <Button
            variant="primary"
            size="lg"
            className="w-full md:w-auto"
            onClick={() => {
              generateAutomation.mutate(undefined);
              navigate("/runs/" + runId + "/automation");
            }}
          >
            {t("createLink.generateAutomation")} <ArrowRight size={15} strokeWidth={2.2} />
          </Button>
        )}
      </div>

      <div className="mb-4 hidden md:block">
        <PipelineRail stage={2} />
      </div>

      {state === "idle" && (
        <div className="glass flex flex-col items-center rounded-[22px] px-5 py-10 text-center md:px-8 md:py-12">
          <div
            className="mb-5 flex h-[70px] w-[70px] items-center justify-center rounded-[22px]"
            style={{ background: "linear-gradient(135deg,rgba(139,92,246,.24),rgba(99,102,241,.12))" }}
          >
            <Link2 size={30} color="#a78bfa" strokeWidth={1.9} />
          </div>
          <h2 className="m-0 mb-2 text-xl font-extrabold">{t("createLink.idle.title")}</h2>
          <p className="m-0 mb-[18px] max-w-[400px] text-[13.5px] leading-relaxed text-ink-dim">
            {localMode ? t("createLink.idle.descLocal") : t("createLink.idle.descProvider")}
          </p>

          <label
            className="mb-[18px] flex w-full cursor-pointer items-center gap-2.5 rounded-xl border px-[14px] py-2.5 md:w-auto"
            style={{
              background: localMode ? "rgba(139,92,246,.12)" : "rgba(255,255,255,.03)",
              borderColor: localMode ? "rgba(139,92,246,.35)" : "rgba(255,255,255,.08)",
            }}
          >
            <input
              type="checkbox"
              className="h-4 w-4 shrink-0 accent-violet"
              checked={localMode}
              onChange={(e) => toggleLocalMode(e.target.checked)}
            />
            <span className="text-[13px] font-semibold text-ink-soft">
              {t("createLink.idle.localToggle")}
            </span>
          </label>

          <Button
            variant="primary"
            size="lg"
            className="w-full md:w-auto"
            onClick={() =>
              createAndLink.mutate(
                { link: !localMode, dryRun: localMode },
                { onError: (e) => toast.error(e instanceof Error ? e.message : t("createLink.toast.createLinkFailed")) },
              )
            }
          >
            <Sparkles size={16} strokeWidth={2.2} />{" "}
            {localMode ? t("createLink.idle.createLocally") : t("createLink.idle.createAndLinkNow")}
          </Button>
        </div>
      )}

      {state === "running" && (
        <div
          className="glass mb-3.5 flex flex-wrap items-center gap-3 rounded-[22px] p-4 md:p-[20px_22px]"
          style={{ borderColor: "rgba(139,92,246,.28)" }}
        >
          <RefreshCw size={20} className="animate-[spin_.8s_linear_infinite] text-violet" />
          <div>
            <div className="text-[15px] font-bold">
              {starting ? t("createLink.starting.title") : t("createLink.running.title")}
            </div>
            <div className="text-[12px] text-ink-dim">
              {starting ? t("createLink.starting.subtitle") : t("createLink.running.subtitle")}
            </div>
          </div>
        </div>
      )}

      {state === "done" && (
        <div
          className="mb-3.5 flex flex-wrap items-center gap-[11px] rounded-2xl p-[14px_18px]"
          style={{ background: "rgba(16,185,129,.1)", border: "1px solid rgba(16,185,129,.28)" }}
        >
          <span className="flex h-[26px] w-[26px] shrink-0 items-center justify-center rounded-full bg-success">
            <Check size={15} color="#fff" strokeWidth={3} />
          </span>
          <span className="text-[14px] font-bold text-success-soft">
            {results.some((r) => r.local) ? t("createLink.done.createdLocally") : t("createLink.done.complete")}
          </span>
          <span className="flex-1 text-[12.5px] text-[#9fe8c8]">
            {results.some((r) => r.local)
              ? t("createLink.done.descLocal")
              : t("createLink.done.descProvider")}
          </span>
        </div>
      )}

      {state !== "idle" && (
        <div className="flex flex-col gap-[11px]">
          {runTickets.map((rt) => {
            const res = byTicket.get(rt.ticketExternalId);
            const kind = providerOf(rt.ticketExternalId);
            const [glyph, glyphBg] = providerGlyph[kind] ?? ["?", "#6b7280"];
            const title =
              tickets?.find((t) => t.externalId === rt.ticketExternalId)?.title ?? rt.ticketExternalId;
            return (
              <div
                key={rt.ticketExternalId}
                className="glass flex items-center gap-3 rounded-2xl p-3 md:gap-3.5 md:p-[16px_18px]"
              >
                <div
                  className="flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-[10px] text-[14px] font-black text-white"
                  style={{ background: glyphBg }}
                >
                  {glyph}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="mb-0.5 flex items-center gap-[9px]">
                    <span className="font-mono text-[11.5px] font-semibold text-violet">
                      {rt.ticketExternalId}
                    </span>
                    {res && (
                      <span className="text-[11px] text-[#7a7a8c]">
                        {t("createLink.ticket.testCasesCount", { count: res.count })}
                      </span>
                    )}
                  </div>
                  <div className="truncate text-[14px] font-semibold">{title}</div>
                </div>
                {res ? (
                  <div className="flex flex-col items-end gap-1">
                    {res.error ? (
                      <span className="text-[11.5px] font-bold text-danger-soft">{res.error}</span>
                    ) : (
                      <>
                        <span className="flex items-center gap-1.5 text-[11.5px] font-bold text-success-soft">
                          <Check size={13} strokeWidth={2.6} />{" "}
                          {res.local ? t("createLink.ticket.createdLocally") : t("createLink.ticket.testCasesCreated")}
                        </span>
                        {res.local ? (
                          <span className="text-[11px] font-semibold text-[#9494a6]">
                            {t("createLink.ticket.providerNotTouched")}
                          </span>
                        ) : (
                          res.linked && (
                            <span className="flex items-center gap-1.5 text-[11.5px] font-bold text-success-soft">
                              <Check size={13} strokeWidth={2.6} /> {t("createLink.ticket.linkedTo", { provider: providerLabel[kind] })}
                            </span>
                          )
                        )}
                      </>
                    )}
                  </div>
                ) : (
                  <span className="flex items-center gap-2 text-[12px] font-semibold text-[#6b7280]">
                    <Spinner size={13} /> {t("createLink.ticket.pending")}
                  </span>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
