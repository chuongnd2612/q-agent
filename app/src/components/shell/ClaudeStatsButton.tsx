import { ChevronDown, Info, RefreshCw, ShieldCheck, Star } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { toast } from "@/lib/toast";
import {
  hubExpiryLabel,
  isCredentialExpired,
  isHubCredentialHealthy,
  readFileText,
} from "@/components/settings/ClaudeCredentialsCard";
import { Pill } from "@/components/ui/badges";
import { ClaudeLogo, Spinner } from "@/components/ui/misc";
import {
  useAiStats,
  useClaudeCredentialsStatus,
  useHubClaudeCredential,
  useHubDataEnabled,
  useHubWebUrl,
  useRefreshAiStats,
  useSetClaudeCredentialMode,
  useTestClaudeCredentials,
  useUploadOwnClaudeCredentials,
} from "@/hooks/queries";
import { claudeAccountLabel } from "@/lib/claudeAccount";
import { cn } from "@/lib/cn";
import { useIsMobile } from "@/hooks/useIsMobile";
import type {
  ByModelUsage,
  ClaudeCredentialsMeta,
  ClaudeCredentialsStatus,
  ClaudeStats,
  HubClaudeCredential,
  UsageWindow,
} from "@/types/api";

/** The credential actually in effect (matches the backend own→shared precedence). */
function effectiveCredMeta(s: ClaudeCredentialsStatus | undefined): ClaudeCredentialsMeta | null {
  if (!s) return null;
  return s.mode === "own" ? s.own : s.mode === "shared" ? s.shared : null;
}

/**
 * Does the *effective* credential look expired? (#528)
 *
 * With hub data on, "effective" means the credential EmeHub resolved — that is
 * what a run uses — so the local file's `expiresAt` is beside the point. Two
 * details are load-bearing:
 *
 *  - `status: "refreshable"` is healthy (see `isHubCredentialHealthy`), not
 *    expired. It is the ordinary live value, so getting it wrong pins an amber
 *    warning to the top bar of every working hub deployment.
 *  - When the hub can't be read (`available: false`) we fall back to judging the
 *    local credential — because that is exactly what the backend falls back to.
 *    An unreachable hub must not become a warning of its own (#491).
 */
function credentialLooksExpired(
  hubData: boolean,
  hubCred: HubClaudeCredential | undefined,
  credStatus: ClaudeCredentialsStatus | undefined,
): boolean {
  if (hubData && hubCred?.available) return !isHubCredentialHealthy(hubCred);
  return isCredentialExpired(effectiveCredMeta(credStatus));
}

const PANEL_WIDTH = 300;

/** Compact tokens → "1.5K" / "2.4M" (integers below 1000 stay as-is). */
function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1).replace(/\.0$/, "")}K`;
  return String(n);
}

/** USD → "$42.60". */
function fmtCost(usd: number): string {
  return `$${usd.toFixed(2)}`;
}

/** ISO (UTC) → local time-of-day "3:45 PM"; empty for missing/invalid. */
function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

/** ISO (UTC) → local "Jul 6, 3:45 PM"; empty for missing/invalid. */
function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** Strip the leading "Claude " so the chip/breakdown show the short name. */
function shortModel(label: string): string {
  return label.replace(/^Claude\s+/i, "").trim() || label;
}

/** Safe fallback so the panel renders while the endpoint is on the old shape. */
const EMPTY_WINDOW: UsageWindow = {
  costUsd: 0,
  tokens: 0,
  requests: 0,
  resetsAt: "",
  pctUsed: -1,
  resetLabel: "",
};

/**
 * Top-bar Claude account chip (`● ⭐ Personal ▾`) + a portalled dropdown panel of
 * usage stats read from `GET /ai/stats`. Present in both the global TopBar and the
 * run-context header. Follows the project's floating-overlay rule: the panel is
 * portalled to `document.body`, fixed-positioned anchored below-right of the chip
 * with an opaque background, and closes on outside-click or Escape.
 */
export function ClaudeStatsButton() {
  // The hub-mode strings are credential vocabulary, so they live in the
  // `settings` namespace alongside the Settings card's. The rest of this panel
  // predates i18n and is deliberately left as it was — flag off must render
  // exactly what it renders today.
  const { t } = useTranslation("settings");
  const { data: stats, isPending } = useAiStats();
  const { data: credStatus } = useClaudeCredentialsStatus();
  const { enabled: hubData } = useHubDataEnabled();
  const { data: hubCred } = useHubClaudeCredential();
  const [open, setOpen] = useState(false);
  const btnRef = useRef<HTMLButtonElement>(null);

  // Loading = the model setting hasn't arrived yet on first page load.
  const loading = isPending && !stats;
  // The chip names the ACCOUNT a run would authenticate with, not the model
  // (#763). The model is still one click away, in the panel's header. Derived
  // through `claudeAccountLabel` so the chip and the panel's CREDENTIAL badge
  // are literally the same computation.
  const label = claudeAccountLabel(hubData, hubCred, credStatus);
  const operational = stats?.operational ?? false;
  // Three-state health: down (no CLI) → red, warn (CLI up but the credential is
  // expired/invalid) → amber, ok → green. The old dot only reflected the binary.
  // An OBSERVED rejection outranks a stored status (#736): the hub reports its row
  // "active"/"refreshable" because the row exists, which says nothing about whether the
  // material still authenticates — so the dot stayed green while every call failed.
  const rejected = stats?.credentialOk === false;
  const expired = rejected || credentialLooksExpired(hubData, hubCred, credStatus);
  const health = !operational ? "down" : expired ? "warn" : "ok";
  const dotColor = health === "down" ? "#f43f5e" : health === "warn" ? "#f59e0b" : "#34d399";
  const healthTitle =
    health === "down"
      ? "Claude CLI unavailable"
      : health === "warn"
        ? hubData
          ? t("credential.popover.hubExpiredTitle")
          : "Claude credential expired — open to test or re-upload"
        : undefined;

  return (
    <>
      <button
        ref={btnRef}
        onClick={() => !loading && setOpen((o) => !o)}
        disabled={loading}
        title={healthTitle}
        className="flex h-[38px] items-center gap-2 rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 text-[12.5px] font-semibold text-ink-soft hover:bg-white/[0.09] disabled:hover:bg-white/[0.04]"
      >
        {loading ? (
          <>
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-white/25" />
            <Star size={12} strokeWidth={2} className="text-ink-dim" />
            {/* Label skeleton is hidden on mobile where the chip is icon-only. */}
            <span className="hidden h-3 w-16 animate-pulse rounded bg-white/[0.14] md:block" />
          </>
        ) : (
          <>
            <span className="relative flex h-1.5 w-1.5">
              {health === "ok" && (
                <span
                  className="absolute inline-flex h-full w-full animate-ping rounded-full opacity-70"
                  style={{ background: "#34d399" }}
                />
              )}
              <span
                className="relative inline-flex h-1.5 w-1.5 rounded-full"
                style={{ background: dotColor }}
              />
            </span>
            <Star size={12} strokeWidth={2} style={{ color: "#fbbf24" }} fill="#fbbf24" />
            {/* On mobile the chip is compact (dot + star only) to fit the top bar;
                the account label + chevron show from `md` up. */}
            <span className="hidden md:inline">{label}</span>
            <ChevronDown size={12} strokeWidth={2} className="hidden md:block" />
          </>
        )}
      </button>

      {stats && (
        <StatsPanel open={open} onClose={() => setOpen(false)} anchorRef={btnRef} stats={stats} />
      )}
    </>
  );
}

/**
 * One rolling window row (session / week). The right-hand value + bar reflect the
 * plan-limit % from the CLI's `/usage`:
 *  - `loading`     → skeleton (the % is being fetched in the background),
 *  - `ready`       → "N% used" + a filled gauge (width = pctUsed),
 *  - `unavailable` → fall back to the window's cost + a faint bar.
 * The sub-line always shows real tokens · requests · cost · resets.
 */
function UsageRow({
  label,
  window,
  resetsAsDate,
  status,
}: {
  label: string;
  window: UsageWindow;
  resetsAsDate?: boolean;
  status: ClaudeStats["limitsStatus"];
}) {
  const loading = status === "loading";
  const hasPct = window.pctUsed >= 0;
  const pct = Math.max(0, Math.min(100, window.pctUsed));
  // Prefer the CLI's authoritative (already-localized) reset label; else format the ISO.
  const reset =
    window.resetLabel || (resetsAsDate ? fmtDateTime(window.resetsAt) : fmtTime(window.resetsAt));
  const sub = `${fmtTokens(window.tokens)} tokens · ${window.requests} requests · ${fmtCost(
    window.costUsd,
  )}${reset ? ` · resets ${reset}` : ""}`;

  return (
    <div className="mt-[15px]">
      <div className="flex items-center justify-between">
        <span className="text-[11px] font-semibold text-ink-soft">{label}</span>
        {loading ? (
          <span className="h-3 w-14 animate-pulse rounded bg-white/[0.12]" />
        ) : hasPct ? (
          <span className="text-[12px] font-bold text-ink">{pct}% used</span>
        ) : (
          <span className="text-[12px] font-bold text-ink">{fmtCost(window.costUsd)}</span>
        )}
      </div>

      {loading ? (
        <div className="mt-2 h-[6px] w-full animate-pulse rounded-full bg-white/[0.12]" />
      ) : hasPct ? (
        <div className="mt-2 h-[6px] w-full overflow-hidden rounded-full bg-white/[0.08]">
          <div
            className="h-full rounded-full"
            style={{
              width: `${pct}%`,
              background: "linear-gradient(90deg,#8b5cf6,#22d3ee)",
              transition: "width .5s cubic-bezier(.2,.8,.2,1)",
            }}
          />
        </div>
      ) : (
        <div className="mt-2 h-[3px] w-full rounded-full bg-white/[0.06]" />
      )}

      {loading ? (
        <div className="mt-2 h-2.5 w-3/4 animate-pulse rounded bg-white/[0.08]" />
      ) : (
        <div className="mt-1.5 text-[10.5px] text-ink-dim">{sub}</div>
      )}
    </div>
  );
}

function StatsPanel({
  open,
  onClose,
  anchorRef,
  stats,
}: {
  open: boolean;
  onClose: () => void;
  anchorRef: React.RefObject<HTMLButtonElement | null>;
  stats: ClaudeStats;
}) {
  const { t } = useTranslation("settings");
  const refresh = useRefreshAiStats();
  const { data: credStatus } = useClaudeCredentialsStatus();
  const { enabled: hubData } = useHubDataEnabled();
  const { data: hubCred } = useHubClaudeCredential();
  const hubWebUrl = useHubWebUrl();
  const setMode = useSetClaudeCredentialMode();
  const uploadOwn = useUploadOwnClaudeCredentials();
  const test = useTestClaudeCredentials();
  const fileRef = useRef<HTMLInputElement>(null);
  const isMobile = useIsMobile();
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const switching = setMode.isPending || uploadOwn.isPending;
  const effMeta = effectiveCredMeta(credStatus);
  const rejected = stats.credentialOk === false;
  const expired = rejected || credentialLooksExpired(hubData, hubCred, credStatus);
  const accountName = effMeta?.accountEmail ?? null;
  const accountOrg = effMeta?.accountOrg ?? null;
  // Three-state health line (mirrors the chip dot): down / warn / ok.
  const health = !stats.operational ? "down" : expired ? "warn" : "ok";
  const healthColor = health === "down" ? "#f43f5e" : health === "warn" ? "#f59e0b" : "#34d399";
  const healthLabel =
    health === "down"
      ? "Unavailable"
      : health === "warn"
        // "Rejected" rather than "expired" when we watched it happen: expiry is a guess
        // from a timestamp, a rejection is what the CLI told us.
        ? rejected
          ? "Credential rejected"
          : "Credential expired"
        : "Operational";

  const runTest = () =>
    test.mutate(undefined, {
      onSuccess: (r) => (r.ok ? toast.success(r.message) : toast.error(r.message)),
      onError: (e) => toast.error((e as Error).message || "Test failed"),
    });

  useEffect(() => {
    if (!open) return;
    const place = () => {
      // On mobile the panel is a full-width bottom sheet, so no anchoring needed.
      if (isMobile) return;
      const el = anchorRef.current;
      if (!el) return;
      const r = el.getBoundingClientRect();
      setPos({
        top: r.bottom + 6,
        left: Math.max(12, Math.min(r.right - PANEL_WIDTH, window.innerWidth - PANEL_WIDTH - 12)),
      });
    };
    place();
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (anchorRef.current?.contains(t)) return;
      if ((e.target as HTMLElement).closest?.("[data-claude-stats]")) return;
      onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    const reposition = () => place();
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    window.addEventListener("scroll", reposition, true);
    window.addEventListener("resize", reposition);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", reposition, true);
      window.removeEventListener("resize", reposition);
    };
  }, [open, anchorRef, onClose, isMobile]);

  if (!open) return null;
  if (!isMobile && !pos) return null;

  const credMode = credStatus?.mode ?? "none";
  // Same helper as the chip above — one source for the three words (#763).
  const credBadge = claudeAccountLabel(hubData, hubCred, credStatus);
  const credName =
    credMode === "own"
      ? "Your personal credentials"
      : credMode === "shared"
        ? "Shared Claude account"
        : "No credential configured";
  const credSourceLabel =
    credMode === "own"
      ? "Using your own Claude plan"
      : credMode === "shared"
        ? "Maintained by your workspace admin"
        : "Upload one below";

  // Switch to the shared account (non-destructive — keeps any personal token on file).
  const chooseShared = () => {
    if (switching || credMode === "shared") return;
    if (!credStatus?.hasShared) {
      toast.error("No shared Claude account is configured");
      return;
    }
    setMode.mutate("shared", {
      onSuccess: () => toast.success("Switched to the shared account"),
      onError: (err) => toast.error((err as Error).message || "Couldn't switch"),
    });
  };

  // Switch to personal: reuse the token on file if there is one, else upload one.
  const choosePersonal = () => {
    if (switching || credMode === "own") return;
    if (credStatus?.hasOwn) {
      setMode.mutate("own", {
        onSuccess: () => toast.success("Switched to your personal credentials"),
        onError: (err) => toast.error((err as Error).message || "Couldn't switch"),
      });
    } else {
      fileRef.current?.click();
    }
  };

  const onFile = async (file: File | null | undefined) => {
    if (!file) return;
    let contents: string;
    try {
      contents = await readFileText(file);
    } catch {
      toast.error("Couldn't read that file");
      return;
    }
    try {
      await uploadOwn.mutateAsync({ credentials: contents });
      toast.success("Using your personal Claude credentials");
    } catch (err) {
      toast.error((err as Error).message || "Upload failed");
    }
  };

  const short = shortModel(stats.modelLabel);
  const limitsStatus = stats.limitsStatus ?? "unavailable";
  const session = stats.session ?? EMPTY_WINDOW;
  const week = stats.week ?? EMPTY_WINDOW;

  // The weekly bar prefers the CLI's authoritative plan-limit % (`pctUsed`).
  // When that's unavailable (no interactive CLI / headless deploy → pctUsed -1),
  // fall back to the configured weekly token budget so the bar still renders —
  // this is the "usage bar" the Weekly token budget setting promises.
  const weekBudget = stats.own?.weekBudget ?? 0;
  const weekTokens = stats.own?.weekTokens ?? 0;
  const weekPctFromBudget =
    weekBudget > 0 ? Math.round((weekTokens / weekBudget) * 100) : -1;
  const effectiveWeek =
    week.pctUsed >= 0 ? week : { ...week, pctUsed: weekPctFromBudget };
  const bd = stats.breakdown ?? { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 };
  const byModel: ByModelUsage[] = stats.byModel ?? [];

  const breakdown: Array<[string, number, string]> = [
    ["Input", bd.input, "#a78bfa"],
    ["Output", bd.output, "#22d3ee"],
    ["Cache read", bd.cacheRead, "#34d399"],
    ["Cache write", bd.cacheWrite, "#fbbf24"],
  ];

  const content = (
    <>
      {/* Header */}
      <div className="flex items-center gap-2.5">
        <span
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[10px]"
          style={{ background: "linear-gradient(135deg,#8b5cf6,#f59e0b)" }}
        >
          <Star size={16} strokeWidth={2} color="#fff" fill="#fff" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="truncate text-[13px] font-bold text-ink">{stats.modelLabel}</div>
          <div
            className="flex items-center gap-1.5 text-[11px] font-semibold"
            style={{ color: healthColor }}
          >
            <span className="h-1.5 w-1.5 rounded-full" style={{ background: healthColor }} />
            {healthLabel}
          </div>
        </div>
        <Pill color="#c4b5fd" bg="rgba(139,92,246,.16)">
          {stats.ctxWindow} ctx
        </Pill>
        <button
          type="button"
          onClick={() => refresh.mutate()}
          disabled={refresh.isPending}
          title="Refresh stats"
          aria-label="Refresh stats"
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-white/[0.08] bg-white/[0.05] text-ink-soft hover:bg-white/[0.1] disabled:opacity-70"
        >
          <RefreshCw
            size={13}
            strokeWidth={2}
            className={refresh.isPending || limitsStatus === "loading" ? "animate-spin" : ""}
          />
        </button>
      </div>

      {/* What actually went wrong, when we watched it go wrong (#736). An amber dot
          that does not say why leaves the user testing a credential the store already
          reports as healthy — which is exactly the loop this closes. */}
      {rejected && (
        <div
          className="mt-[14px] rounded-xl border border-[rgba(245,158,11,.28)] p-3"
          style={{ background: "rgba(245,158,11,.08)" }}
          data-testid="credential-rejected"
        >
          <div className="mb-1 text-[10px] font-bold tracking-[0.06em] text-warning-soft">
            {t("credential.popover.rejectedTitle")}
          </div>
          <div className="text-[11.5px] leading-relaxed text-ink-soft">
            {hubData
              ? t("credential.popover.rejectedHubBody")
              : t("credential.popover.rejectedLocalBody")}
          </div>
          {stats.credentialDetail && (
            <div className="mt-1.5 line-clamp-2 font-mono text-[10.5px] text-faint">
              {stats.credentialDetail}
            </div>
          )}
        </div>
      )}

      {/* Credential summary. With hub data on, EmeHub owns the credential: show
          what it resolved and drop the self-configuration controls — the
          mode switch and upload wouldn't change which account a run uses, and
          `POST /ai/credentials/test` exercises the *local* resolution, so a
          green result there would say nothing about the hub's credential. */}
      {hubData ? (
        <HubCredentialSummary cred={hubCred} hubWebUrl={hubWebUrl} />
      ) : (
      <div className="mt-[14px] rounded-xl border border-[rgba(217,119,87,.22)] bg-[rgba(217,119,87,.06)] p-3">
        <div className="mb-[9px] flex items-center gap-2">
          <span className="text-[10px] font-bold tracking-[0.06em] text-[#e0a58c]">CREDENTIAL</span>
          <span className="ml-auto rounded-full bg-white/[0.06] px-2 py-[2px] text-[9px] font-bold text-[#c7c7d4]">
            {credBadge}
          </span>
        </div>
        <div className="flex items-center gap-[9px]">
          <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-[rgba(217,119,87,.16)]">
            <ClaudeLogo size={15} />
          </span>
          <div className="min-w-0 flex-1">
            <div className="truncate text-[12.5px] font-bold text-ink">
              {accountName ?? credName}
            </div>
            <div className="truncate text-[10.5px] text-ink-dim">
              {accountName ? accountOrg ?? credName : credSourceLabel}
            </div>
          </div>
        </div>
        <div className="mt-[11px] flex gap-1.5">
          <button
            type="button"
            onClick={chooseShared}
            disabled={switching}
            className={cn(
              "flex-1 rounded-lg py-1.5 text-[11.5px] font-semibold transition-colors disabled:opacity-60",
              credMode === "shared"
                ? "bg-white/[0.12] text-ink"
                : "bg-transparent text-ink-dim hover:bg-white/[0.06]",
            )}
          >
            Shared
          </button>
          <button
            type="button"
            onClick={choosePersonal}
            disabled={switching}
            className={cn(
              "flex-1 rounded-lg py-1.5 text-[11.5px] font-semibold transition-colors disabled:opacity-60",
              credMode === "own"
                ? "bg-white/[0.12] text-ink"
                : "bg-transparent text-ink-dim hover:bg-white/[0.06]",
            )}
          >
            Personal
          </button>
          <input
            ref={fileRef}
            type="file"
            accept=".json,application/json"
            className="hidden"
            onChange={(e) => onFile(e.target.files?.[0])}
          />
        </div>
        <button
          type="button"
          onClick={runTest}
          disabled={test.isPending || credMode === "none"}
          className="mt-1.5 flex w-full items-center justify-center gap-1.5 rounded-lg border border-white/[0.1] bg-white/[0.04] py-[7px] text-[11.5px] font-semibold text-ink-soft transition-colors hover:bg-white/[0.08] disabled:opacity-60"
        >
          {test.isPending ? <Spinner size={12} /> : <ShieldCheck size={12} strokeWidth={2.2} />}
          {test.isPending ? "Testing…" : "Test credential"}
        </button>
        {expired && (
          <div className="mt-1.5 text-[10.5px] font-semibold text-[#fbbf24]">
            This credential looks expired — re-upload it or switch account.
          </div>
        )}
      </div>
      )}

      {/* Rolling windows */}
      <UsageRow label="Current session" window={session} status={limitsStatus} />
      <UsageRow label="Current week" window={effectiveWeek} resetsAsDate status={limitsStatus} />

      {/* Token breakdown */}
      <div className="mt-[15px] mb-[7px] text-[9px] font-bold tracking-[0.1em] text-ink-dim">
        TOKEN BREAKDOWN · {short.toUpperCase()}
      </div>
      <div className="flex flex-col gap-1.5">
        {breakdown.map(([name, value, color]) => (
          <div key={name} className="flex items-center gap-2">
            <span className="h-2 w-2 rounded-full" style={{ background: color }} />
            <span className="flex-1 text-[11px] text-ink-soft">{name}</span>
            <span className="font-mono text-[11px] font-semibold text-ink">{fmtTokens(value)}</span>
          </div>
        ))}
      </div>

      {/* By model — only when more than one model has usage */}
      {byModel.length > 1 && (
        <>
          <div className="mt-[15px] mb-[7px] text-[9px] font-bold tracking-[0.1em] text-ink-dim">
            BY MODEL
          </div>
          <div className="flex flex-col gap-1.5">
            {byModel.map((m) => (
              <div key={m.model} className="flex items-center gap-2">
                <span className="min-w-0 flex-1 truncate text-[11px] text-ink-soft">
                  {m.modelLabel}
                </span>
                <span className="text-[10px] text-ink-dim">
                  {fmtTokens(m.input + m.output + m.cacheRead + m.cacheWrite)} tokens
                </span>
                <span className="font-mono text-[11px] font-semibold text-ink">
                  {fmtCost(m.costUsd)}
                </span>
              </div>
            ))}
          </div>
        </>
      )}

      {/* Estimate disclaimer — cost is computed from token counts × model pricing
          (per-message cost isn't recorded), aggregated from local Claude sessions. */}
      <div className="mt-[13px] flex items-start gap-1.5 text-[10px] leading-[1.45] text-ink-dim">
        <Info size={12} strokeWidth={2} className="mt-px shrink-0" />
        <span>
          Estimated — costs are computed from token usage × model pricing, from local
          Claude sessions on this machine.
        </span>
      </div>
    </>
  );

  // Mobile: a full-width bottom sheet (scrim + slide-up panel with a grab handle).
  if (isMobile) {
    return createPortal(
      <div
        className="fixed inset-0 z-[1000]"
        style={{ background: "rgba(4,4,8,.6)", backdropFilter: "blur(2px)" }}
        onClick={onClose}
      >
        <div
          data-claude-stats
          onClick={(e) => e.stopPropagation()}
          className="absolute inset-x-0 bottom-0 max-h-[92vh] animate-[fadeInUp_.3s_ease] overflow-y-auto rounded-t-[26px] border-t border-white/[0.12] p-4"
          style={{
            background: "rgba(24,24,32,.99)",
            paddingBottom: "calc(16px + env(safe-area-inset-bottom))",
          }}
        >
          <div className="mb-3 flex justify-center">
            <span className="h-1 w-10 rounded-full bg-white/25" />
          </div>
          {content}
        </div>
      </div>,
      document.body,
    );
  }

  // Desktop: an anchored dropdown panel below the chip.
  return createPortal(
    <div
      data-claude-stats
      className="fixed z-[1000] rounded-[14px] border border-white/[0.12] p-[14px] shadow-[0_30px_70px_-20px_#000]"
      style={{ top: pos!.top, left: pos!.left, width: PANEL_WIDTH, background: "rgba(24,24,32,.97)" }}
    >
      {content}
    </div>,
    document.body,
  );
}

/**
 * Read-only credential summary for hub mode (#528).
 *
 * Everything here comes from `GET /ai/credentials/hub` — the account EmeHub
 * resolves, which is what a run actually authenticates with. There is no mode
 * switch, no upload and no "Test credential" button, because none of the three
 * would decide or describe the outcome: `POST /ai/credentials/test` exercises
 * the *local* resolution, so a green result would be an answer to a different
 * question. The deep link goes where the account can really be changed.
 *
 * When the hub can't be read this degrades to a plain statement of that fact
 * plus the fallback note. It never renders an error state and never disappears —
 * an unreachable hub must leave every screen usable (#491).
 */
function HubCredentialSummary({
  cred,
  hubWebUrl,
}: {
  cred: HubClaudeCredential | undefined;
  hubWebUrl: string | null;
}) {
  const { t } = useTranslation("settings");
  const available = cred?.available === true;
  const healthy = isHubCredentialHealthy(cred);
  const sourceLabel =
    cred?.source === "own"
      ? t("credential.card.hubSourceOwn")
      : t("credential.card.hubSourceShared");
  // `refreshable` means "the hub holds a refresh token and renews on use" — the
  // ordinary state of a live Claude OAuth credential, not a problem.
  const statusLabel =
    cred?.status === "refreshable"
      ? t("credential.card.hubStatusRefreshable")
      : cred?.status ?? null;
  const meta = available
    ? [statusLabel, cred?.subscriptionType, hubExpiryLabel(cred)].filter(Boolean).join(" · ")
    : null;

  return (
    <div className="mt-[14px] rounded-xl border border-[rgba(139,92,246,.28)] bg-[rgba(139,92,246,.07)] p-3">
      <div className="mb-[9px] flex items-center gap-2">
        <span className="text-[10px] font-bold tracking-[0.06em] text-[#c4b5fd]">CREDENTIAL</span>
        <span className="ml-auto rounded-full bg-white/[0.06] px-2 py-[2px] text-[9px] font-bold text-[#c7c7d4]">
          {t("credential.popover.hubBadge")}
        </span>
      </div>
      <div className="flex items-center gap-[9px]">
        <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-[rgba(217,119,87,.16)]">
          <ClaudeLogo size={15} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="truncate text-[12.5px] font-bold text-ink">
            {available ? cred?.label || sourceLabel : t("credential.popover.hubUnavailableTitle")}
          </div>
          <div className="truncate text-[10.5px] text-ink-dim">
            {available ? sourceLabel : t("credential.card.hubLocalFallbackNote")}
          </div>
        </div>
      </div>
      {meta && <div className="mt-[9px] text-[10.5px] text-ink-dim">{meta}</div>}
      {/* Only when the hub answered: "runs use the account EmeHub resolved"
          reads as a contradiction directly under "couldn't read it". */}
      {available && (
        <div className="mt-[9px] text-[10.5px] leading-[1.45] text-ink-dim">
          {t("credential.card.hubManagedBody")}
        </div>
      )}
      {hubWebUrl && (
        <a
          href={`${hubWebUrl}/app/claude`}
          target="_blank"
          rel="noreferrer"
          className="mt-[7px] inline-flex items-center gap-1 text-[11px] font-bold text-violet"
        >
          {t("credential.card.hubManageLink")}
        </a>
      )}
      {available && !healthy && (
        <div className="mt-1.5 text-[10.5px] font-semibold text-[#fbbf24]">
          {t("credential.popover.hubExpiredNote")}
        </div>
      )}
    </div>
  );
}
