import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { ChevronRight, Download, Search, Trash2 } from "lucide-react";
import { toast } from "@/lib/toast";
import {
  useAuditEvents,
  useAuditStats,
  useBackendLogs,
  useBackendLogStats,
  useClearAuditEvents,
} from "@/hooks/queries";
import type { AuditEventOut } from "@/types/api";

// Colour maps copied verbatim from the design export; the first tuple element is
// an i18n key (under the `audit.` namespace prefix), resolved with t() at render.
const CAT: Record<string, [string, string, string]> = {
  ai: ["categories.ai", "#a78bfa", "rgba(139,92,246,.14)"],
  sync: ["categories.sync", "#67e8f9", "rgba(34,211,238,.13)"],
  review: ["categories.review", "#6ee7b7", "rgba(16,185,129,.14)"],
  execution: ["categories.execution", "#fbbf24", "rgba(251,191,36,.13)"],
  auth: ["categories.auth", "#f9a8d4", "rgba(244,114,182,.14)"],
  knowledge: ["categories.knowledge", "#c4b5fd", "rgba(196,181,253,.14)"],
  integration: ["categories.integration", "#93c5fd", "rgba(147,197,253,.14)"],
  run: ["categories.run", "#fca5a5", "rgba(252,165,165,.13)"],
  automation: ["categories.automation", "#f0abfc", "rgba(240,171,252,.14)"],
  comment: ["categories.comment", "#5eead4", "rgba(94,234,212,.13)"],
  settings: ["categories.settings", "#d4d4d8", "rgba(212,212,216,.12)"],
};
const ACTOR: Record<string, [string, string]> = {
  user: ["#c4b5fd", "rgba(139,92,246,.16)"],
  ai: ["#a78bfa", "linear-gradient(135deg,#8b5cf6,#6366f1)"],
  system: ["#93c5fd", "rgba(147,197,253,.16)"],
};
const STATUS: Record<string, [string, string, string]> = {
  success: ["#6ee7b7", "rgba(16,185,129,.14)", "statuses.success"],
  warning: ["#fbbf24", "rgba(251,191,36,.13)", "statuses.warning"],
  error: ["#fb7185", "rgba(244,63,94,.14)", "statuses.error"],
};
const LEVEL: Record<string, [string, string, string]> = {
  info: ["INFO", "#6ee7b7", "rgba(16,185,129,.14)"],
  warn: ["WARN", "#fbbf24", "rgba(251,191,36,.14)"],
  error: ["ERROR", "#fb7185", "rgba(244,63,94,.16)"],
  debug: ["DEBUG", "#93c5fd", "rgba(147,197,253,.14)"],
};

// Chip labels are i18n keys (under the `audit.` prefix), resolved with t() at render.
const CAT_CHIPS: Array<[string, string]> = [
  ["all", "chips.allEvents"], ["ai", "categories.ai"], ["sync", "categories.sync"], ["review", "categories.review"],
  ["execution", "categories.execution"], ["auth", "categories.auth"], ["integration", "categories.integration"], ["settings", "categories.settings"],
];
const ACTOR_CHIPS: Array<[string, string]> = [
  ["all", "chips.everyone"], ["user", "chips.people"], ["ai", "chips.qAgent"], ["system", "chips.system"],
];
const LEVEL_CHIPS: Array<[string, string]> = [
  ["all", "chips.all"], ["info", "levels.info"], ["warn", "levels.warn"], ["error", "levels.error"], ["debug", "levels.debug"],
];

const panel: React.CSSProperties = {
  background: "rgba(255,255,255,.035)",
  backdropFilter: "blur(22px)",
  WebkitBackdropFilter: "blur(22px)",
  border: "1px solid rgba(255,255,255,.07)",
};

function chipStyle(active: boolean, accent: "violet" | "cyan"): React.CSSProperties {
  if (!active) return { background: "rgba(255,255,255,.05)", color: "#a0a0b2" };
  return accent === "violet"
    ? { background: "rgba(139,92,246,.2)", color: "#fff", boxShadow: "inset 0 0 0 1px rgba(139,92,246,.3)" }
    : { background: "rgba(34,211,238,.16)", color: "#67e8f9", boxShadow: "inset 0 0 0 1px rgba(34,211,238,.3)" };
}

function StatCard({ label, value, color }: { label: string; value: string; color: string }) {
  return (
    <div style={panel} className="rounded-2xl px-[18px] py-4">
      <div className="mb-2 text-[12px] text-[#9494a6]">{label}</div>
      <div className="text-[24px] font-black tracking-tight" style={{ color }}>{value}</div>
    </div>
  );
}

const initials = (name: string) =>
  name.split(" ").map((x) => x[0]).join("").slice(0, 2).toUpperCase();

function toCsv(events: AuditEventOut[]): string {
  const head = ["id", "ts", "category", "actor", "actorType", "action", "target", "ip", "status", "meta"];
  const esc = (v: string) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const rows = events.map((e) => head.map((k) => esc((e as unknown as Record<string, string>)[k])).join(","));
  return [head.join(","), ...rows].join("\n");
}

export function AuditLog() {
  const { t } = useTranslation("reports");
  const [view, setView] = useState<"activity" | "backend">("activity");
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState("all");
  const [actor, setActor] = useState("all");
  const [level, setLevel] = useState("all");
  const [service, setService] = useState("all");
  const [liveTail, setLiveTail] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);

  const q = search.trim() || undefined;
  const { data: events } = useAuditEvents({
    category: category === "all" ? undefined : category,
    actor: actor === "all" ? undefined : actor,
    q,
  });
  const { data: stats } = useAuditStats();
  const { data: logs } = useBackendLogs(
    { level: level === "all" ? undefined : level, service: service === "all" ? undefined : service, q },
    view === "backend" && liveTail,
  );
  const { data: logStats } = useBackendLogStats(view === "backend" && liveTail);
  const clearEvents = useClearAuditEvents();

  const clearAll = () => {
    if (!window.confirm(t("audit.confirmClear"))) return;
    clearEvents.mutate(undefined, {
      onSuccess: (r) => toast.success(t("audit.toast.cleared", { count: r.deleted })),
      onError: (e) => toast.error(e instanceof Error ? e.message : t("audit.toast.clearFailed")),
    });
  };

  const rows = events ?? [];
  const logRows = logs ?? [];
  const services = useMemo(
    () => Array.from(new Set((logs ?? []).map((l) => l.service))).sort(),
    [logs],
  );

  const exportCsv = () => {
    const blob = new Blob([toCsv(rows)], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "audit-log.csv";
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="animate-fade-in-up px-1 pb-10 pt-0.5">
      <div className="mb-[18px] flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="mb-[5px] text-[13px] font-medium text-[#8b8b9e]">
            {t("audit.subtitle")}
          </div>
          <h1 className="m-0 text-[28px] font-black tracking-tight">{t("audit.title")}</h1>
        </div>
        {/* Export and Clear both act on the activity events (`rows`), and there is
            no backend operation that clears the in-memory log buffer — so the
            toolbar belongs to the activity tab only. Backend logs carry their own
            controls (level, service, live tail) above the terminal. */}
        {view === "activity" && (
        <div className="flex items-center gap-2">
          <button
            onClick={exportCsv}
            className="flex flex-1 items-center justify-center gap-[7px] rounded-xl border border-white/[0.09] bg-white/[0.05] px-[15px] py-2.5 text-[13px] font-semibold text-[#dcdce4] hover:bg-white/[0.1] md:flex-none"
          >
            <Download size={15} /> {t("audit.exportCsv")}
          </button>
          <button
            onClick={clearAll}
            disabled={clearEvents.isPending || rows.length === 0}
            className="flex flex-1 items-center justify-center gap-[7px] rounded-xl border border-[rgba(244,63,94,.28)] bg-[rgba(244,63,94,.13)] px-[15px] py-2.5 text-[13px] font-semibold text-[#fb7185] hover:bg-[rgba(244,63,94,.2)] disabled:cursor-not-allowed disabled:opacity-50 md:flex-none"
          >
            <Trash2 size={15} /> {clearEvents.isPending ? t("audit.clearing") : t("audit.clearLog")}
          </button>
        </div>
        )}
      </div>

      <div className="mb-4 flex gap-2">
        {(["activity", "backend"] as const).map((v) => (
          <button
            key={v}
            onClick={() => setView(v)}
            className="flex-1 rounded-[11px] px-4 py-2 text-[13px] font-semibold md:flex-none"
            style={
              view === v
                ? {
                    background: "linear-gradient(135deg,rgba(139,92,246,.24),rgba(99,102,241,.12))",
                    color: "#fff",
                    boxShadow: "inset 0 0 0 1px rgba(139,92,246,.3)",
                  }
                : { background: "rgba(255,255,255,.04)", color: "#a0a0b2" }
            }
          >
            {v === "activity" ? t("audit.tabs.activity") : t("audit.tabs.backend")}
          </button>
        ))}
      </div>

      {view === "activity" ? (
        <>
          <div className="mb-4 grid grid-cols-2 gap-3.5 md:grid-cols-4">
            <StatCard label={t("audit.stats.eventsToday")} value={String(stats?.eventsToday ?? 0)} color="#a78bfa" />
            <StatCard label={t("audit.stats.aiActions")} value={String(stats?.aiActions ?? 0)} color="#67e8f9" />
            <StatCard label={t("audit.stats.userActions")} value={String(stats?.userActions ?? 0)} color="#6ee7b7" />
            <StatCard label={t("audit.stats.failuresWarnings")} value={String(stats?.failures ?? 0)} color="#fb7185" />
          </div>

          <div style={panel} className="mb-3.5 flex flex-col gap-2.5 rounded-2xl px-3.5 py-3 md:flex-row md:flex-wrap md:items-center">
            <div className="flex h-9 w-full items-center gap-2 rounded-[11px] border border-white/[0.08] bg-white/[0.04] px-3 md:min-w-[200px] md:max-w-[300px] md:flex-1">
              <Search size={14} className="text-[#7a7a8c]" />
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder={t("audit.searchActivity")}
                className="flex-1 bg-transparent text-[13px] text-ink outline-none"
              />
            </div>
            <div className="scrollbar-none flex gap-[7px] overflow-x-auto md:flex-wrap md:overflow-visible">
              {ACTOR_CHIPS.map(([id, label]) => (
                <button
                  key={id}
                  onClick={() => setActor(id)}
                  className="shrink-0 whitespace-nowrap rounded-[10px] px-[13px] py-[7px] text-[12.5px] font-semibold"
                  style={chipStyle(actor === id, "cyan")}
                >
                  {t(`audit.${label}`)}
                </button>
              ))}
            </div>
          </div>

          <div className="scrollbar-none mb-4 flex gap-[7px] overflow-x-auto md:flex-wrap md:overflow-visible">
            {CAT_CHIPS.map(([id, label]) => (
              <button
                key={id}
                onClick={() => setCategory(id)}
                className="shrink-0 whitespace-nowrap rounded-[10px] px-[13px] py-[7px] text-[12.5px] font-semibold"
                style={chipStyle(category === id, "violet")}
              >
                {t(`audit.${label}`)}
              </button>
            ))}
          </div>

          {/* Desktop: table. Mobile: vertical timeline of stacked entries (see below). */}
          <div style={panel} className="hidden overflow-hidden rounded-[18px] md:block">
            <div
              className="grid gap-3 border-b border-white/[0.06] bg-white/[0.04] px-[18px] py-[11px] text-[10px] font-bold tracking-[0.06em] text-[#7a7a8c]"
              style={{ gridTemplateColumns: "150px 1fr 130px 108px 26px" }}
            >
              <span>{t("audit.table.timestamp")}</span><span>{t("audit.table.event")}</span><span>{t("audit.table.actor")}</span><span>{t("audit.table.status")}</span><span />
            </div>
            {rows.map((e) => {
              const cat = CAT[e.category] ?? ["Other", "#c3c3d0", "rgba(255,255,255,.08)"];
              const st = STATUS[e.status] ?? STATUS.success;
              const [ac, acBg] = ACTOR[e.actorType] ?? ACTOR.system;
              const isAi = e.actorType === "ai";
              const [day, timePart] = e.ts.includes("T") ? e.ts.split("T") : e.ts.split(" ");
              const time = (timePart || "").slice(0, 5);
              const open = expanded === e.id;
              return (
                <div key={e.id} className="border-b border-white/[0.045]">
                  <div
                    onClick={() => setExpanded(open ? null : e.id)}
                    className="grid cursor-pointer items-center gap-3 px-[18px] py-[13px] hover:bg-white/[0.03]"
                    style={{ gridTemplateColumns: "150px 1fr 130px 108px 26px" }}
                  >
                    <div>
                      <div className="font-mono text-[12.5px] font-semibold text-[#c7c7d4]">{time}</div>
                      <div className="font-mono text-[10.5px] text-[#7a7a8c]">{day}</div>
                    </div>
                    <div className="min-w-0">
                      <div className="mb-0.5 flex items-center gap-2">
                        <span
                          className="shrink-0 rounded-full px-2 py-0.5 text-[10px] font-bold"
                          style={{ background: cat[2], color: cat[1] }}
                        >
                          {t(`audit.${cat[0]}`)}
                        </span>
                        <span className="truncate text-[13.5px] font-semibold">{e.action}</span>
                      </div>
                      <div className="truncate text-[12px] text-[#8b8b9e]">{e.target}</div>
                    </div>
                    <div className="flex min-w-0 items-center gap-2">
                      <span
                        className="flex h-6 w-6 shrink-0 items-center justify-center rounded-[7px] text-[9.5px] font-bold"
                        style={{ background: acBg, color: isAi ? "#fff" : ac }}
                      >
                        {isAi ? (
                          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M12 3l1.9 5.3L19 10l-5.1 1.7L12 17l-1.9-5.3L5 10l5.1-1.7z" />
                          </svg>
                        ) : (
                          initials(e.actor)
                        )}
                      </span>
                      <span className="truncate text-[12.5px] font-semibold">{e.actor}</span>
                    </div>
                    <div>
                      <span
                        className="rounded-full px-2.5 py-[3px] text-[11px] font-bold"
                        style={{ background: st[1], color: st[0] }}
                      >
                        {t(`audit.${st[2]}`)}
                      </span>
                    </div>
                    <ChevronRight
                      size={15}
                      className="shrink-0 text-[#8b8b9e] transition-transform"
                      style={open ? { transform: "rotate(90deg)" } : undefined}
                    />
                  </div>
                  {open && (
                    <div className="animate-fade-in-up px-[18px] pb-4 pt-0.5">
                      <div
                        className="grid gap-x-[22px] gap-y-2.5 rounded-xl border border-white/[0.06] bg-white/[0.03] px-4 py-3.5"
                        style={{ gridTemplateColumns: "repeat(2,minmax(0,1fr))" }}
                      >
                        <Field label={t("audit.field.eventId")} value={e.id} mono color="#67e8f9" />
                        <Field label={t("audit.field.timestamp")} value={e.ts.replace("T", " ").slice(0, 19)} mono />
                        <Field label={t("audit.field.target")} value={e.target} />
                        <Field label={t("audit.field.ipAddress")} value={e.ip} mono />
                        <div className="col-span-full">
                          <div className="mb-[3px] text-[10.5px] text-[#7a7a8c]">{t("audit.field.details")}</div>
                          <div className="text-[12.5px] leading-relaxed text-[#c3c3d0]">{e.meta}</div>
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
            {rows.length === 0 && (
              <div className="flex flex-col items-center px-8 py-12 text-center">
                <div className="mb-1 text-[14px] font-semibold">{t("audit.empty.eventsTitle")}</div>
                <div className="text-[12.5px] text-[#8b8b9e]">{t("audit.empty.eventsHint")}</div>
              </div>
            )}
          </div>

          {/* Mobile timeline — a rail of glowing category-coloured dots joined by a line,
              one card per event (tap to expand the same detail grid as desktop). */}
          <div className="md:hidden">
            {rows.map((e, i) => {
              const cat = CAT[e.category] ?? ["Other", "#c3c3d0", "rgba(255,255,255,.08)"];
              const st = STATUS[e.status] ?? STATUS.success;
              const [ac, acBg] = ACTOR[e.actorType] ?? ACTOR.system;
              const isAi = e.actorType === "ai";
              const [day, timePart] = e.ts.includes("T") ? e.ts.split("T") : e.ts.split(" ");
              const time = (timePart || "").slice(0, 5);
              const open = expanded === e.id;
              return (
                <div key={e.id} className="relative pb-3 pl-5 last:pb-0">
                  {i < rows.length - 1 && (
                    <span className="absolute left-[3px] top-[14px] bottom-0 w-[2px] bg-white/[0.08]" />
                  )}
                  <span
                    className="absolute left-0 top-[6px] h-[8px] w-[8px] rounded-full"
                    style={{ background: cat[1], boxShadow: `0 0 6px ${cat[1]}` }}
                  />
                  <div
                    style={panel}
                    onClick={() => setExpanded(open ? null : e.id)}
                    className="cursor-pointer rounded-[14px] px-3.5 py-3"
                  >
                    <div className="mb-1.5 flex items-center gap-2">
                      <span
                        className="shrink-0 rounded-full px-2 py-0.5 text-[10px] font-bold"
                        style={{ background: cat[2], color: cat[1] }}
                      >
                        {t(`audit.${cat[0]}`)}
                      </span>
                      <span className="font-mono text-[10.5px] text-[#7a7a8c]">
                        {day} {time}
                      </span>
                      <ChevronRight
                        size={14}
                        className="ml-auto shrink-0 text-[#8b8b9e] transition-transform"
                        style={open ? { transform: "rotate(90deg)" } : undefined}
                      />
                    </div>
                    <div className="mb-1 flex items-center gap-2">
                      <span
                        className="flex h-5 w-5 shrink-0 items-center justify-center rounded-[6px] text-[8.5px] font-bold"
                        style={{ background: acBg, color: isAi ? "#fff" : ac }}
                      >
                        {isAi ? (
                          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M12 3l1.9 5.3L19 10l-5.1 1.7L12 17l-1.9-5.3L5 10l5.1-1.7z" />
                          </svg>
                        ) : (
                          initials(e.actor)
                        )}
                      </span>
                      <span className="truncate text-[12.5px] font-semibold">{e.actor}</span>
                      <span
                        className="ml-auto shrink-0 rounded-full px-2 py-[2px] text-[10px] font-bold"
                        style={{ background: st[1], color: st[0] }}
                      >
                        {t(`audit.${st[2]}`)}
                      </span>
                    </div>
                    <div className="truncate text-[13px] font-semibold">{e.action}</div>
                    <div className="truncate text-[11.5px] text-[#8b8b9e]">{e.target}</div>
                    {open && (
                      <div className="animate-fade-in-up mt-3 grid grid-cols-1 gap-y-2.5 rounded-xl border border-white/[0.06] bg-white/[0.03] px-3.5 py-3">
                        <Field label={t("audit.field.eventId")} value={e.id} mono color="#67e8f9" />
                        <Field label={t("audit.field.timestamp")} value={e.ts.replace("T", " ").slice(0, 19)} mono />
                        <Field label={t("audit.field.target")} value={e.target} />
                        <Field label={t("audit.field.ipAddress")} value={e.ip} mono />
                        <div>
                          <div className="mb-[3px] text-[10.5px] text-[#7a7a8c]">{t("audit.field.details")}</div>
                          <div className="text-[12.5px] leading-relaxed text-[#c3c3d0]">{e.meta}</div>
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
            {rows.length === 0 && (
              <div style={panel} className="flex flex-col items-center rounded-[18px] px-8 py-12 text-center">
                <div className="mb-1 text-[14px] font-semibold">{t("audit.empty.eventsTitle")}</div>
                <div className="text-[12.5px] text-[#8b8b9e]">{t("audit.empty.eventsHint")}</div>
              </div>
            )}
          </div>
          {rows.length > 0 && (
            <div className="mt-3.5 text-center text-[12px] text-[#7a7a8c]">{t("audit.showingEvents", { count: rows.length })}</div>
          )}
        </>
      ) : (
        <>
          <div className="mb-4 grid grid-cols-2 gap-3.5 md:grid-cols-3">
            <StatCard label={t("audit.stats.logVolume")} value={(logStats?.logVolume ?? 0).toLocaleString()} color="#67e8f9" />
            <StatCard label={t("audit.stats.warnings1h")} value={String(logStats?.warnings ?? 0)} color="#fbbf24" />
            <StatCard label={t("audit.stats.errors1h")} value={String(logStats?.errors ?? 0)} color="#fb7185" />
          </div>

          <div style={panel} className="mb-3.5 flex flex-col gap-2.5 rounded-2xl px-3.5 py-3 md:flex-row md:flex-wrap md:items-center">
            <div className="flex h-9 w-full items-center gap-2 rounded-[11px] border border-white/[0.08] bg-white/[0.04] px-3 md:min-w-[200px] md:max-w-[300px] md:flex-1">
              <Search size={14} className="text-[#7a7a8c]" />
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder={t("audit.searchBackend")}
                className="flex-1 bg-transparent text-[13px] text-ink outline-none"
              />
            </div>
            <div className="scrollbar-none flex gap-[7px] overflow-x-auto md:flex-wrap md:overflow-visible">
              {LEVEL_CHIPS.map(([id, label]) => (
                <button
                  key={id}
                  onClick={() => setLevel(id)}
                  className="shrink-0 whitespace-nowrap rounded-[10px] px-[13px] py-[7px] text-[12.5px] font-semibold"
                  style={chipStyle(level === id, "violet")}
                >
                  {t(`audit.${label}`)}
                </button>
              ))}
            </div>
            <button
              onClick={() => setLiveTail((v) => !v)}
              className="flex w-full items-center justify-center gap-2 rounded-[10px] px-[13px] py-[7px] text-[12.5px] font-semibold md:w-auto"
              style={
                liveTail
                  ? { background: "rgba(16,185,129,.16)", color: "#6ee7b7", boxShadow: "inset 0 0 0 1px rgba(16,185,129,.3)" }
                  : { background: "rgba(255,255,255,.05)", color: "#a0a0b2" }
              }
            >
              <span
                className="h-2 w-2 rounded-full"
                style={{ background: liveTail ? "#10b981" : "#6b7280", animation: liveTail ? "pulseDot 1.4s infinite" : undefined }}
              />
              {liveTail ? t("audit.liveTailOn") : t("audit.liveTail")}
            </button>
          </div>

          <div className="scrollbar-none mb-4 flex gap-[7px] overflow-x-auto md:flex-wrap md:overflow-visible">
            {[["all", "chips.allServices"], ...services.map((s) => [s, s] as [string, string])].map(([id, label]) => (
              <button
                key={id}
                onClick={() => setService(id)}
                className="shrink-0 whitespace-nowrap rounded-[9px] px-3 py-1.5 font-mono text-[11.5px] font-semibold"
                style={chipStyle(service === id, "cyan")}
              >
                {id === "all" ? t(`audit.${label}`) : label}
              </button>
            ))}
          </div>

          <div
            className="overflow-hidden rounded-[18px] border border-white/[0.08]"
            style={{ background: "rgba(8,8,13,.72)", backdropFilter: "blur(22px)", WebkitBackdropFilter: "blur(22px)" }}
          >
            <div className="flex items-center gap-2 border-b border-white/[0.06] px-4 py-[11px]">
              <span className="h-[11px] w-[11px] shrink-0 rounded-full" style={{ background: "#f43f5e" }} />
              <span className="h-[11px] w-[11px] shrink-0 rounded-full" style={{ background: "#fbbf24" }} />
              <span className="h-[11px] w-[11px] shrink-0 rounded-full" style={{ background: "#10b981" }} />
              <span className="ml-2 truncate font-mono text-[12px] text-[#8b8b9e]">q-agent · api · in-memory log buffer</span>
              <span className="ml-auto shrink-0 font-mono text-[11px] text-[#7a7a8c]">{t("audit.lines", { count: logRows.length })}</span>
            </div>
            <div className="max-h-[520px] overflow-y-auto font-mono text-[12px]">
              {/* Desktop: fixed-column terminal row. Mobile: stacked compact line (see below). */}
              {logRows.map((l, i) => {
                const lv = LEVEL[l.level] ?? LEVEL.info;
                const msColor =
                  l.durationMs == null ? "#6c6c7e" : l.durationMs > 1500 ? "#fb7185" : l.durationMs > 500 ? "#fbbf24" : "#8b8b9e";
                return (
                  <div key={`${l.trace}-${i}`} className="border-b border-white/[0.035] hover:bg-white/[0.03]">
                    <div
                      className="hidden animate-fade-in-up items-baseline gap-3 px-4 py-2 leading-normal md:grid"
                      style={{ gridTemplateColumns: "104px 62px 128px 1fr 62px" }}
                    >
                      <span className="text-[#6c6c7e]">{l.ts}</span>
                      <span
                        className="rounded-md py-0.5 text-center text-[10px] font-bold tracking-[0.04em]"
                        style={{ color: lv[1], background: lv[2] }}
                      >
                        {lv[0]}
                      </span>
                      <span className="text-[#93c5fd]">{l.service}</span>
                      <span className="truncate text-[#c7c7d4]">{l.message}</span>
                      <span className="text-right" style={{ color: msColor }}>
                        {l.durationMs == null ? "—" : `${l.durationMs}ms`}
                      </span>
                    </div>
                    <div className="animate-fade-in-up px-4 py-2 leading-normal md:hidden">
                      <div className="flex items-center gap-2">
                        <span
                          className="shrink-0 rounded-md px-1.5 py-0.5 text-[10px] font-bold tracking-[0.04em]"
                          style={{ color: lv[1], background: lv[2] }}
                        >
                          {lv[0]}
                        </span>
                        <span className="shrink-0 truncate text-[#93c5fd]">{l.service}</span>
                        <span className="ml-auto shrink-0" style={{ color: msColor }}>
                          {l.durationMs == null ? "—" : `${l.durationMs}ms`}
                        </span>
                      </div>
                      <div className="mt-1 flex items-center gap-2">
                        <span className="truncate text-[#c7c7d4]">{l.message}</span>
                      </div>
                      <div className="mt-1 text-[#6c6c7e]">{l.ts}</div>
                    </div>
                  </div>
                );
              })}
              {logRows.length === 0 && (
                <div className="flex flex-col items-center px-8 py-12 text-center" style={{ fontFamily: "var(--font-sans, sans-serif)" }}>
                  <div className="mb-1 text-[14px] font-semibold">{t("audit.empty.logsTitle")}</div>
                  <div className="text-[12.5px] text-[#8b8b9e]">{t("audit.empty.logsHint")}</div>
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function Field({ label, value, mono, color }: { label: string; value: string; mono?: boolean; color?: string }) {
  return (
    <div>
      <div className="mb-[3px] text-[10.5px] text-[#7a7a8c]">{label}</div>
      <div className={`text-[12.5px] ${mono ? "font-mono" : ""}`} style={{ color: color ?? "#dcdce4" }}>
        {value}
      </div>
    </div>
  );
}
