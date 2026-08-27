import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Check, ChevronRight, ExternalLink, Link2Off } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/Button";
import { Select } from "@/components/ui/Dropdown";
import { Spinner } from "@/components/ui/misc";
import {
  PROVIDER_META,
  connectionConfigSummary,
  relativeTime,
} from "@/components/settings/providerMeta";
import { useTestConnection } from "@/hooks/queries";
import { toast } from "@/lib/toast";
import type { ConnectionOut } from "@/types/api";
import type { RoleSpec } from "./roles";

/**
 * One of the project's three connection roles (ADR 0015 §3).
 *
 * Collapsed it is the binding at a glance — provider glyph, connection name, the
 * role pill, a config summary and live connection state. Expanded it shows the
 * connection's non-secret fields, a **Test connection** action, a link out to the
 * credential vault, and the last sync.
 *
 * The card binds; it never edits credentials. Workspace Settings → Integrations
 * stays the single vault (ADR 0006 §5) and the project only decides *which*
 * connection it uses — two places to type the same PAT is how one of them ends up
 * stale and nobody can tell which.
 *
 * Opaque background rather than `glass`: this is a text-heavy panel over the
 * animated shell, which is exactly the case CLAUDE.md says the translucent card
 * makes unreadable.
 */
export function RoleCard({
  role,
  connection,
  inherited,
  options,
  onBind,
  saving,
  readOnly,
}: {
  role: RoleSpec;
  connection: ConnectionOut | null;
  /** The TEST CASE TARGET showing the ticket source because it has no explicit
   *  binding of its own — a working default, not a gap. */
  inherited: boolean;
  options: ConnectionOut[];
  onBind: (connectionId: number | null) => void;
  saving: boolean;
  readOnly: boolean;
}) {
  const { t } = useTranslation("projects");
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const test = useTestConnection(connection?.id ?? 0);

  const meta = connection ? PROVIDER_META[connection.kind] : null;
  const summary =
    connection && meta ? connectionConfigSummary(connection.kind, connection.config) : "";
  const connected = !!connection?.connected;

  const fields = connection
    ? Object.entries(connection.config).filter(([, value]) => value && String(value).trim())
    : [];

  const runTest = () => {
    if (!connection) return;
    test.mutate(undefined, {
      onSuccess: (res) =>
        res.ok
          ? toast.success(t("connectionTab.test.ok"), { description: res.message })
          : toast.error(t("connectionTab.test.failed"), { description: res.message }),
      onError: (err) =>
        toast.error(t("connectionTab.test.failed"), {
          description: err instanceof Error ? err.message : undefined,
        }),
    });
  };

  return (
    <div
      className="overflow-hidden rounded-[18px] border"
      style={{
        background: "rgba(24,24,32,.92)",
        borderColor: role.accent ? "rgba(139,92,246,.28)" : "rgba(255,255,255,.08)",
      }}
      data-testid={`connection-role-${role.id}`}
    >
      <div className="flex flex-wrap items-center gap-3 p-[16px_18px]">
        <div
          className="flex h-[38px] w-[38px] shrink-0 items-center justify-center rounded-[11px] text-[15px] font-black"
          style={{
            background: meta ? meta.color : "rgba(255,255,255,.06)",
            color: meta ? meta.glyphColor : "#8b8b9e",
          }}
        >
          {meta ? meta.glyph : <Link2Off size={16} strokeWidth={2.2} />}
        </div>

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2.5">
            <span className="text-[14px] font-bold">
              {connection ? connection.name : t("connectionTab.unbound")}
            </span>
            <span
              className="rounded-full px-2 py-[3px] text-[8.5px] font-bold uppercase tracking-[.07em]"
              style={{ background: role.background, color: role.color }}
            >
              {t(`connectionTab.roles.${role.id}.label`)}
            </span>
            {inherited && (
              <span className="text-[10.5px] font-semibold text-ink-dim">
                {t("connectionTab.inherited")}
              </span>
            )}
          </div>
          <div className="mt-[3px] text-[12px] text-ink-dim">
            {summary || t(`connectionTab.roles.${role.id}.purpose`)}
          </div>
        </div>

        <span
          className="flex shrink-0 items-center gap-1.5 text-[11.5px] font-semibold"
          style={{ color: connection ? (connected ? "#6ee7b7" : "#fb7185") : "#8b8b9e" }}
        >
          <span
            className="h-[7px] w-[7px] rounded-full"
            style={{ background: connection ? (connected ? "#6ee7b7" : "#fb7185") : "#4b4b57" }}
          />
          {connection
            ? connected
              ? t("connectionTab.state.connected")
              : t("connectionTab.state.notConnected")
            : t("connectionTab.state.none")}
        </span>

        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          aria-label={t("connectionTab.toggle")}
          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[10px] border border-white/[0.1] bg-white/[0.05] text-ink hover:bg-white/[0.1]"
        >
          <ChevronRight
            size={15}
            strokeWidth={2.4}
            className={"transition-transform " + (open ? "rotate-90" : "")}
          />
        </button>
      </div>

      {open && (
        <div className="border-t border-white/[0.06] p-[14px_18px_18px]">
          <div className="mb-3.5">
            <div className="mb-1.5 text-[10.5px] font-semibold uppercase tracking-wide text-ink-dim">
              {t("connectionTab.boundTo")}
            </div>
            {/* `Select` is div-based and takes no `disabled`, so read-only is a
                non-interactive shell — with nothing focusable inside it there is
                no keyboard route around `pointer-events: none`. */}
            <div className={readOnly ? "pointer-events-none opacity-60" : ""}>
              <Select
                value={connection ? String(connection.id) : null}
                options={options.map((c) => ({
                  value: String(c.id),
                  label: `${PROVIDER_META[c.kind].name} · ${c.name}`,
                }))}
                placeholder={t("connectionTab.choose")}
                emptyLabel={t("connectionTab.noEligible")}
                onChange={(v) => onBind(v ? Number(v) : null)}
                fullWidth
              />
            </div>
          </div>

          {fields.length > 0 && (
            <div className="mb-3.5 grid grid-cols-1 gap-2.5 md:grid-cols-2">
              {fields.map(([key, value]) => (
                <div
                  key={key}
                  className="rounded-[12px] border border-white/[0.07] bg-white/[0.03] p-[11px_13px]"
                >
                  <div className="mb-1 text-[10.5px] font-semibold text-ink-dim">{key}</div>
                  <div className="break-all font-mono text-[12.5px] font-semibold text-ink">
                    {String(value)}
                  </div>
                </div>
              ))}
            </div>
          )}

          <div className="flex flex-wrap items-center gap-2.5">
            <Button variant="glass" onClick={runTest} disabled={!connection || test.isPending}>
              {test.isPending ? <Spinner size={13} /> : <Check size={14} strokeWidth={2.4} />}
              {t("connectionTab.testConnection")}
            </Button>
            <Button variant="glass" onClick={() => navigate("/settings")}>
              <ExternalLink size={13} strokeWidth={2.2} />
              {t("connectionTab.editCredentials")}
            </Button>
            <span className="ml-auto text-[11.5px] text-ink-dim">
              {saving
                ? t("connectionTab.saving")
                : t("connectionTab.lastSync", { time: relativeTime(connection?.lastSync ?? null) })}
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
