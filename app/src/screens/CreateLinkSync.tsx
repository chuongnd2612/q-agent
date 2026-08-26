import { ArrowRight, Check, FlaskConical, Link2, RefreshCw, Sparkles } from "lucide-react";
import { useTranslation } from "react-i18next";
import { toast } from "@/lib/toast";
import { Button } from "@/components/ui/Button";
import { PipelineRail } from "@/components/ui/PipelineRail";
import { providerGlyph } from "@/components/ui/badges";
import { Spinner } from "@/components/ui/misc";
import { providerLabel } from "@/data/projects";
import { useNavigate, useParams } from "react-router-dom";
import { useRunPath } from "@/hooks/useRunRouteId";
import { useIsMutating } from "@tanstack/react-query";
import {
  ALL_TICKETS_PAGE_SIZE,
  CREATE_LINK_MUTATION_KEY,
  useCreateAndLink,
  useGenerateAutomation,
  useLinkStatus,
  useRun,
  useSettings,
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
  const runPath = useRunPath();
  const navigate = useNavigate();
  const { data: run } = useRun(runId);
  const { data: ticketsPage } = useTickets({ pageSize: ALL_TICKETS_PAGE_SIZE });
  const tickets = ticketsPage?.items;
  const { data: status, isFetched: statusFetched } = useLinkStatus(runId);
  const createAndLink = useCreateAndLink(runId);
  const generateAutomation = useGenerateAutomation(runId);

  // Dry run is a WORKSPACE setting now (#712), so this screen reflects it rather than
  // keeping a second, per-browser copy of the same decision in localStorage. Two places
  // to set one thing is how a user ends up writing to a real provider because they
  // switched it off in the wrong one. The server enforces it either way.
  const { data: settings } = useSettings();
  const dryRun = settings?.dryRun ?? false;

  // Review fires the mutation and navigates here in the same tick, so this screen
  // renders while the request is still in flight and `linkStatus` still says "idle"
  // — which showed the idle panel, i.e. the same big Create button, and read as a
  // lost click (#694). The mutation is keyed so it can be seen from here, where it
  // was started elsewhere.
  const starting = useIsMutating({ mutationKey: CREATE_LINK_MUTATION_KEY }) > 0;
  // Until the status has actually been READ, there is no state to render (#737).
  // `status?.status ?? "idle"` treated "not fetched yet" as "nothing has happened",
  // so arriving from Review flashed the idle panel — checkbox, big Create button and
  // all — before the real state replaced it a moment later. On a screen whose idle
  // panel offers to write to a provider, showing it for work that is already done is
  // worse than a beat of nothing.
  const state = starting
    ? "running"
    : !statusFetched
      ? "loading"
      : (status?.status ?? "idle");
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
              navigate(runPath("automation"));
            }}
          >
            {t("createLink.generateAutomation")} <ArrowRight size={15} strokeWidth={2.2} />
          </Button>
        )}
      </div>

      <div className="mb-4 hidden md:block">
        <PipelineRail stage={2} />
      </div>

      {state === "loading" && (
        <div className="glass flex items-center justify-center gap-2.5 rounded-[22px] px-5 py-12">
          <Spinner size={15} />
          <span className="text-[13px] text-ink-dim">{t("createLink.loading")}</span>
        </div>
      )}

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
            {dryRun ? t("createLink.idle.descLocal") : t("createLink.idle.descProvider")}
          </p>

          {/* The checkbox is gone (#737). Dry run is a workspace setting (#712), and a
              second per-browser copy of the same decision is how someone writes to a
              real provider because they switched it off in the other place. Shown as a
              read-only chip so the mode is still visible where it matters. */}
          {dryRun && (
            <span
              className="mb-[18px] flex items-center gap-2 rounded-full border border-[rgba(245,158,11,.3)] px-3 py-1.5 text-[11.5px] font-semibold text-warning-soft"
              style={{ background: "rgba(245,158,11,.1)" }}
              data-testid="sync-dry-run"
            >
              <FlaskConical size={13} strokeWidth={2.2} />
              {t("createLink.idle.dryRun")}
            </span>
          )}

          <Button
            variant="primary"
            size="lg"
            className="w-full md:w-auto"
            onClick={() =>
              createAndLink.mutate(
                // The SETTING decides; the server enforces it and can only tighten
                // what is asked for (#712), so this is what it is asking for.
                { link: !dryRun, dryRun },
                { onError: (e) => toast.error(e instanceof Error ? e.message : t("createLink.toast.createLinkFailed")) },
              )
            }
          >
            <Sparkles size={16} strokeWidth={2.2} />{" "}
            {dryRun ? t("createLink.idle.createLocally") : t("createLink.idle.createAndLinkNow")}
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
