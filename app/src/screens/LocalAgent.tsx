/**
 * Local Agent screen (global, non run-scoped — routed alongside `settings`).
 * Lets the user pair a device running `npx @q-agent/agent`, which executes
 * Playwright suites headed on their own machine (so manual login/MFA happens
 * right where the user is, and session cookies never leave that machine).
 *
 * Flow: "Add device" mints a short-lived pairing code (`POST
 * /agent/devices/pair-code`); the user runs the printed `npx @q-agent/agent
 * pair <code> --server <origin>` command locally. Once paired, the device
 * shows up in the list below (`GET /agent/devices`) and becomes selectable as
 * an execution target on the Execution screen.
 */

import { API_BASE } from "@/lib/api";
import { withBase } from "@/lib/basePath";
import { useEffect, useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import { Check, Copy, Cpu, Download, Laptop, Trash2 } from "lucide-react";
import { toast } from "@/lib/toast";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { GlassCard } from "@/components/ui/GlassCard";
import { Spinner } from "@/components/ui/misc";
import { useAgentDevices, usePairCode, useRevokeDevice } from "@/hooks/queries";
import { relativeTime } from "@/screens/auth/profile/sessions";
import type { AgentDeviceOut } from "@/types/api";

/** A device whose `lastSeenAt` is within this window is treated as "Connected".
 * The agent idle-polls the job queue every ~3s (each poll refreshes
 * `lastSeenAt` server-side), so a generous window absorbs network jitter and
 * the client's own 5s device-list poll while still flipping to "Offline" a few
 * seconds after the agent stops. */
const ONLINE_WINDOW_MS = 30_000;

function isDeviceOnline(device: AgentDeviceOut, now: number): boolean {
  if (!device.lastSeenAt) return false;
  return now - Date.parse(device.lastSeenAt) < ONLINE_WINDOW_MS;
}

export function LocalAgent() {
  const { t } = useTranslation("dashboard");
  const { data: devices, isLoading } = useAgentDevices();
  const pairCode = usePairCode();
  const revokeDevice = useRevokeDevice();

  const [pairing, setPairing] = useState<{
    code: string;
    expiresAt: number;
    knownIds: number[];
  } | null>(null);
  const [revoking, setRevoking] = useState<AgentDeviceOut | null>(null);

  // A clock that ticks every 5s so the Connected/Offline badge (and relative
  // "last seen") recompute even when the polled device list is unchanged —
  // React Query's structural sharing skips re-renders when data is deep-equal.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 5_000);
    return () => clearInterval(id);
  }, []);

  // Auto-dismiss the pairing card once the device it paired shows up in the
  // polled list (issue: the code stayed on screen after a successful pair).
  useEffect(() => {
    if (!pairing || !devices) return;
    const known = new Set(pairing.knownIds);
    const paired = devices.find((d) => !known.has(d.id));
    if (paired) {
      toast.success(t("localAgent.toast.paired", { name: paired.name || t("localAgent.deviceFallback") }));
      setPairing(null);
    }
  }, [devices, pairing]);

  const handleAddDevice = () => {
    pairCode.mutate(undefined, {
      onSuccess: (data) =>
        setPairing({
          code: data.code,
          expiresAt: Date.now() + data.expiresIn * 1000,
          knownIds: (devices ?? []).map((d) => d.id),
        }),
      onError: (err) =>
        toast.error(err instanceof Error ? err.message : t("localAgent.toast.pairCodeFailed")),
    });
  };

  // The agent calls the API's `/agent/...` routes; on the same-origin (tunnel)
  // deployment those live under `/api`. Local dev with a separate API port
  // should instead point --server straight at the API (see the note below).
  const command = pairing
    ? `npx @q-agent/agent pair ${pairing.code} --server ${window.location.origin}${API_BASE}`
    : "";

  return (
    <div className="mx-auto max-w-[860px] py-10">
      <div className="mb-[22px]">
        <div className="mb-[5px] text-[13px] font-medium text-muted">{t("localAgent.eyebrow")}</div>
        <h1 className="m-0 text-[28px] font-black tracking-[-0.03em]">{t("localAgent.title")}</h1>
      </div>

      <GlassCard className="mb-5 p-[22px]">
        <div className="flex gap-[14px]">
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[12px] border border-[rgba(139,92,246,.3)] bg-[rgba(139,92,246,.14)]">
            <Cpu size={19} className="text-violet" strokeWidth={2} />
          </span>
          <div className="flex-1">
            <div className="mb-1 text-[14.5px] font-bold">{t("localAgent.about.title")}</div>
            <p className="m-0 text-[13px] leading-[1.65] text-[#c3c3d0]">
              <Trans
                t={t}
                i18nKey="localAgent.about.body"
                components={{ b: <b className="font-semibold text-[#ececf1]" /> }}
              />
            </p>
          </div>
        </div>
        <div className="mt-[18px] rounded-xl border border-white/[0.07] bg-white/[0.02] p-3.5">
          <div className="mb-1.5 text-[11px] font-semibold tracking-[.06em] text-[#6c6c7e]">
            {t("localAgent.prereq.title")}
          </div>
          <ul className="m-0 list-none space-y-1 p-0 text-[12.5px] text-[#c3c3d0]">
            <li>&middot; {t("localAgent.prereq.item1")}</li>
            <li>&middot; {t("localAgent.prereq.item2")}</li>
          </ul>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-3">
          <a
            href={withBase("/downloads/qagent-agent-setup.exe")}
            download
            className="inline-flex items-center gap-2 rounded-xl px-4 py-2 text-[13px] font-semibold text-white transition-opacity hover:opacity-90"
            style={{ background: "linear-gradient(135deg,#8b5cf6,#6366f1)" }}
          >
            <Download size={15} strokeWidth={2.2} /> {t("localAgent.download")}
          </a>
          <span className="text-[12px] text-[#8b8b9e]">
            <Trans
              t={t}
              i18nKey="localAgent.nativeAppNote"
              components={{
                code: (
                  <code className="rounded bg-white/[0.06] px-1.5 py-0.5 font-mono text-[11.5px] text-[#c4b5fd]" />
                ),
              }}
            />
          </span>
        </div>
      </GlassCard>

      <div className="mb-3 flex items-center justify-between">
        <div className="text-[12px] font-bold tracking-[0.08em] text-[#6c6c7e]">{t("localAgent.pairedDevices")}</div>
        <Button variant="primary" size="sm" onClick={handleAddDevice} disabled={pairCode.isPending}>
          {pairCode.isPending ? <Spinner size={13} /> : <Laptop size={14} strokeWidth={2.4} />}
          {t("localAgent.addDevice")}
        </Button>
      </div>

      {pairing && (
        <PairingCommand
          code={pairing.code}
          command={command}
          expiresAt={pairing.expiresAt}
          onExpire={() => setPairing(null)}
        />
      )}

      <GlassCard className="p-2">
        {isLoading ? (
          <div className="flex flex-col gap-2 p-2">
            {Array.from({ length: 2 }).map((_, i) => (
              <div key={i} className="h-[58px] animate-pulse rounded-xl bg-white/[0.04]" />
            ))}
          </div>
        ) : !devices?.length ? (
          <div className="p-8 text-center text-[13px] text-ink-dim">
            {t("localAgent.noDevices")}
          </div>
        ) : (
          <div className="flex flex-col gap-2 p-2">
            {devices.map((d) => (
              <DeviceRow
                key={d.id}
                device={d}
                online={isDeviceOnline(d, now)}
                onRevoke={() => setRevoking(d)}
              />
            ))}
          </div>
        )}
      </GlassCard>

      <ConfirmDialog
        open={revoking !== null}
        title={t("localAgent.revoke.title")}
        message={t("localAgent.revoke.message", { name: revoking?.name ?? t("localAgent.revoke.thisDevice") })}
        confirmLabel={t("localAgent.revoke.confirm")}
        danger
        loading={revokeDevice.isPending}
        onConfirm={() => {
          if (!revoking) return;
          revokeDevice.mutate(revoking.id, {
            onSuccess: () => {
              toast.success(t("localAgent.toast.revoked"));
              setRevoking(null);
            },
            onError: (err) =>
              toast.error(err instanceof Error ? err.message : t("localAgent.toast.revokeFailed")),
          });
        }}
        onClose={() => setRevoking(null)}
      />
    </div>
  );
}

/** Prominent 6-digit pairing code + server URL (to type into the Local Agent
 * app), with the CLI command as a secondary option and a live expiry countdown.
 * Calls `onExpire` once the code's TTL elapses so the caller can clear it. */
function PairingCommand({
  code,
  command,
  expiresAt,
  onExpire,
}: {
  code: string;
  command: string;
  expiresAt: number;
  onExpire: () => void;
}) {
  const { t } = useTranslation("dashboard");
  const [remainingMs, setRemainingMs] = useState(() => expiresAt - Date.now());
  const [copied, setCopied] = useState<string | null>(null);

  useEffect(() => {
    const tick = () => {
      const left = expiresAt - Date.now();
      setRemainingMs(left);
      if (left <= 0) onExpire();
    };
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [expiresAt, onExpire]);

  if (remainingMs <= 0) return null;

  const totalSec = Math.max(0, Math.ceil(remainingMs / 1000));
  const mm = Math.floor(totalSec / 60);
  const ss = totalSec % 60;

  const copyText = async (text: string, key: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(key);
      setTimeout(() => setCopied((c) => (c === key ? null : c)), 1400);
    } catch {
      toast.error(t("localAgent.toast.copyFailed"));
    }
  };

  return (
    <GlassCard className="mb-3 p-[18px]">
      <div className="mb-3 flex items-center gap-2">
        <Laptop size={15} className="text-violet" strokeWidth={2.2} />
        <span className="text-[13px] font-semibold">{t("localAgent.pairing.title")}</span>
        <span className="ml-auto font-mono text-[12px] text-[#8b8b9e]">
          {t("localAgent.pairing.expiresIn", { time: `${mm}:${ss.toString().padStart(2, "0")}` })}
        </span>
      </div>

      <div className="text-[11px] font-semibold tracking-[0.06em] text-[#6c6c7e]">{t("localAgent.pairing.codeLabel")}</div>
      <div className="mt-1.5 flex items-center gap-2.5 rounded-xl border border-white/10 bg-[#16161f] p-3 pl-4">
        <span className="flex-1 font-mono text-[30px] font-bold tracking-[0.28em] text-ink">{code}</span>
        <button
          type="button"
          onClick={() => copyText(code, "code")}
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-[#8b8b9e] transition-colors hover:bg-white/[0.08] hover:text-white"
          aria-label={t("localAgent.pairing.copyCode")}
        >
          {copied === "code" ? <Check size={16} className="text-[#6ee7b7]" /> : <Copy size={16} />}
        </button>
      </div>

      <p className="mt-3 mb-0 text-[11.5px] leading-[1.55] text-[#8b8b9e]">
        <Trans
          t={t}
          i18nKey="localAgent.pairing.instructions"
          components={{ b: <b className="text-[#c3c3d0]" /> }}
        />
      </p>
      <div className="mt-2 flex items-center gap-2.5 rounded-xl border border-white/10 bg-[#16161f] p-2.5 pl-3.5">
        <code className="min-w-0 flex-1 overflow-x-auto whitespace-nowrap font-mono text-[12px] text-[#c3c3d0]">
          {command}
        </code>
        <button
          type="button"
          onClick={() => copyText(command, "cmd")}
          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-[#8b8b9e] transition-colors hover:bg-white/[0.08] hover:text-white"
          aria-label={t("localAgent.pairing.copyCommand")}
        >
          {copied === "cmd" ? <Check size={15} className="text-[#6ee7b7]" /> : <Copy size={15} />}
        </button>
      </div>
    </GlassCard>
  );
}

function DeviceRow({
  device,
  online,
  onRevoke,
}: {
  device: AgentDeviceOut;
  online: boolean;
  onRevoke: () => void;
}) {
  const { t } = useTranslation("dashboard");
  return (
    <div className="flex items-center gap-3.5 rounded-[13px] border border-white/[0.06] bg-white/[0.03] p-3">
      <div className="flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-[10px] bg-white/[0.05] text-ink-dim">
        <Laptop size={16} strokeWidth={2} />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-[13.5px] font-semibold text-ink">
            {device.name || t("localAgent.unnamedDevice")}
          </span>
          <StatusBadge online={online} />
        </div>
        <div className="text-[11.5px] text-muted">
          {t("localAgent.device.lastSeenPaired", {
            last: device.lastSeenAt ? relativeTime(device.lastSeenAt) : t("localAgent.device.never"),
            paired: relativeTime(device.createdAt),
          })}
        </div>
      </div>
      <button
        type="button"
        onClick={onRevoke}
        className="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[12px] font-semibold text-danger-soft transition-colors hover:bg-[rgba(244,63,94,.1)]"
      >
        <Trash2 size={13} strokeWidth={2.2} />
        {t("localAgent.revoke.button")}
      </button>
    </div>
  );
}

/** Live connection pill for a paired device. "Connected" means the agent has
 * checked in within {@link ONLINE_WINDOW_MS}; "Offline" devices stay paired and
 * reconnect on their own next launch (no re-pairing needed). */
function StatusBadge({ online }: { online: boolean }) {
  const { t } = useTranslation("dashboard");
  return (
    <span
      className={
        online
          ? "inline-flex shrink-0 items-center gap-1.5 rounded-full border border-[rgba(52,211,153,.3)] bg-[rgba(52,211,153,.12)] px-2 py-0.5 text-[10.5px] font-semibold text-[#6ee7b7]"
          : "inline-flex shrink-0 items-center gap-1.5 rounded-full border border-white/10 bg-white/[0.04] px-2 py-0.5 text-[10.5px] font-semibold text-muted"
      }
    >
      <span
        className={`h-1.5 w-1.5 rounded-full ${online ? "bg-[#34d399]" : "bg-[#6c6c7e]"}`}
        aria-hidden
      />
      {online ? t("localAgent.status.connected") : t("localAgent.status.offline")}
    </span>
  );
}
