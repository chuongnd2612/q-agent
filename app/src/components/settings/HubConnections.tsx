import { useQuery } from "@tanstack/react-query";
import { Info, Lock } from "lucide-react";
import { useTranslation } from "react-i18next";
import { fetchHubConnections, type HubConnectionOut } from "@/lib/hubConnections";
import { PROVIDER_META, relativeTime } from "@/components/settings/providerMeta";
import type { ProviderKind } from "@/types/api";

/**
 * Connections EmeHub holds, listed **read-only** under the local provider
 * connections (C4 of #501).
 *
 * The whole design problem here is honesty. A user who sees their Azure DevOps
 * connection listed on this screen will reasonably assume Q-Agent can sync with
 * it — and it cannot: the hub reports `hasPat` and never the PAT, and the proxy
 * endpoint that would let us borrow the connection is deliberately unbuilt. So
 * the copy says so plainly, in the section header *and* on every row, rather
 * than leaving the user to discover it when a sync mysteriously does nothing.
 *
 * It renders **nothing at all** when there are no hub connections — which is
 * also every failure case (flag off, no hub session, expired token, hub down),
 * because `fetchHubConnections` never throws. That makes the flag-off screen
 * byte-identical to before and guarantees a hub problem can never sit on top of
 * the local picker.
 */
export function HubConnections() {
  const { t } = useTranslation("settings");
  const { data } = useQuery({
    queryKey: ["hub", "connections"],
    queryFn: fetchHubConnections,
    // A hub read is an enhancement: never retry it, never let it block or
    // spin the screen it decorates.
    retry: false,
    staleTime: 60_000,
  });

  const connections = data ?? [];
  if (connections.length === 0) return null;

  return (
    <div
      data-testid="hub-connections"
      className="overflow-hidden rounded-2xl border border-[rgba(139,92,246,.22)] bg-[rgba(139,92,246,.05)]"
    >
      <div className="flex flex-wrap items-start gap-[13px] px-[16px] py-[14px] md:flex-nowrap md:px-[22px] md:py-[16px]">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-[rgba(139,92,246,.18)] text-[#c4b5fd]">
          <Lock size={17} strokeWidth={2.2} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[15px] font-bold">{t("hubConnections.title")}</span>
            <span className="rounded-md border border-[rgba(139,92,246,.3)] bg-[rgba(139,92,246,.16)] px-1.5 py-[2px] text-[10px] font-bold uppercase tracking-wide text-[#c4b5fd]">
              {t("hubConnections.badge")}
            </span>
          </div>
          <div className="mt-1 text-[11.5px] leading-[1.5] text-muted">
            {t("hubConnections.description")}
          </div>
        </div>
      </div>

      <div className="mx-[16px] mb-[14px] flex items-start gap-2 rounded-xl border border-white/[0.08] bg-white/[0.03] px-3 py-2.5 md:mx-[22px]">
        <Info size={14} strokeWidth={2.2} className="mt-[2px] shrink-0 text-[#c4b5fd]" />
        <div className="text-[11.5px] leading-[1.55] text-muted">{t("hubConnections.notice")}</div>
      </div>

      <div className="border-t border-white/[0.06]">
        {connections.map((conn) => (
          <HubConnectionRow key={conn.id} connection={conn} />
        ))}
      </div>
    </div>
  );
}

/** One hub connection: identity + what the hub knows, and nothing actionable.
 * There is no Test, no Save and no Delete here on purpose — this row is a
 * report about someone else's connection, not a control. */
function HubConnectionRow({ connection }: { connection: HubConnectionOut }) {
  const { t } = useTranslation("settings");
  const meta = PROVIDER_META[connection.kind as ProviderKind];
  const capabilities = connection.capabilities.length
    ? connection.capabilities
    : connection.supportedCapabilities;

  return (
    <div className="flex flex-wrap items-center gap-[13px] border-b border-white/[0.05] px-[16px] py-[13px] last:border-b-0 md:flex-nowrap md:px-[22px]">
      <div
        className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-[13px] font-black opacity-70"
        style={{ background: meta?.color ?? "#3f3f46", color: meta?.glyphColor ?? "#fff" }}
      >
        {meta?.glyph ?? connection.kind.slice(0, 1).toUpperCase()}
      </div>

      <div className="min-w-0 flex-1">
        <div className="truncate text-[13.5px] font-semibold">{connection.label}</div>
        <div className="truncate text-[11px] text-muted">
          {[meta?.name ?? connection.kind, connection.baseUrl].filter(Boolean).join(" · ")}
        </div>
        {capabilities.length > 0 && (
          <div className="mt-1 flex flex-wrap gap-1">
            {capabilities.map((cap) => (
              <span
                key={cap}
                className="rounded border border-white/[0.1] bg-white/[0.04] px-1.5 py-[1px] text-[10px] text-muted"
              >
                {cap}
              </span>
            ))}
          </div>
        )}
      </div>

      <div className="flex shrink-0 flex-col items-start gap-1 md:items-end">
        <div className="flex items-center gap-2">
          <span
            className={`rounded-md px-2 py-[3px] text-[10.5px] font-semibold ${
              connection.connected
                ? "bg-[rgba(52,211,153,.14)] text-[#6ee7b7]"
                : "bg-white/[0.06] text-muted"
            }`}
          >
            {connection.connected
              ? t("hubConnections.status.connected")
              : t("hubConnections.status.disconnected")}
          </span>
          {connection.shared && (
            <span className="rounded-md bg-white/[0.06] px-2 py-[3px] text-[10.5px] text-muted">
              {t("hubConnections.shared")}
            </span>
          )}
        </div>
        <div className="text-[10.5px] text-muted">
          {t("hubConnections.credentialAtHub", {
            state: connection.hasPat
              ? t("hubConnections.credentialHeld")
              : t("hubConnections.credentialMissing"),
          })}
        </div>
        <div className="text-[10.5px] text-muted">
          {t("hubConnections.lastSync", { when: relativeTime(connection.lastSync) })} ·{" "}
          {t("hubConnections.lastTested", { when: relativeTime(connection.lastTestedAt) })}
        </div>
        <div className="text-[10.5px] font-semibold text-[#fbbf24]">
          {t("hubConnections.notUsableHere")}
        </div>
      </div>
    </div>
  );
}
