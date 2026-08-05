/**
 * "Can't reach Q-Agent" banner + Retry (#482, B5).
 *
 * `lib/api.ts` used to collapse transport failure into the logout path: a 401
 * whose refresh attempt failed *because the backend was unreachable* was
 * indistinguishable from a dead session, so the user was logged out and bounced
 * to `/login`. Telling someone they are signed out when the service is merely
 * down is the single most confusing outcome available
 * (`docs/HUB-INTEGRATION.md` §3 B5).
 *
 * `api.ts` now keeps those cases apart and exposes reachability; this renders it.
 * Retry refetches the queries that failed rather than reloading the page — a
 * reload would drop in-memory state and, if the backend is still down, land on a
 * dead shell.
 *
 * This is **not** an auth surface. It never grants access, never clears the
 * session, and appearing does not mean the user is signed out.
 */

import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { CloudOff, RefreshCw } from "lucide-react";
import { isServiceReachable, subscribeServiceReachable } from "@/lib/api";

export function ServiceUnreachableBanner() {
  const { t } = useTranslation("common");
  const queryClient = useQueryClient();
  const [reachable, setReachable] = useState(isServiceReachable);
  const [retrying, setRetrying] = useState(false);

  useEffect(() => subscribeServiceReachable(setReachable), []);

  if (reachable) return null;

  return (
    <div
      // Fixed + high z-index, and no backdrop-filter: an ancestor filter/transform
      // would trap this in a stacking context (CLAUDE.md), and the app background
      // is animated, so a filter here would also cause compositing artifacts.
      // Sits BELOW the app header (which is ~68px tall) rather than at top-0:
      // centred at the very top it covered the header's search field.
      className="fixed left-1/2 top-[84px] z-[9999] flex -translate-x-1/2 items-center gap-3 rounded-xl border px-4 py-3 text-[13px] shadow-2xl"
      style={{ background: "#2a1721", borderColor: "rgba(248,113,113,.34)" }}
      role="status"
      aria-live="polite"
    >
      <CloudOff size={16} className="shrink-0 text-danger-soft" />
      <div className="min-w-0">
        <p className="m-0 font-bold text-ink">{t("serviceUnreachable.title")}</p>
        <p className="m-0 text-[12.5px] text-muted">{t("serviceUnreachable.body")}</p>
      </div>
      <button
        type="button"
        disabled={retrying}
        onClick={async () => {
          setRetrying(true);
          try {
            // Refetch rather than reload: a reload loses in-memory state and, if
            // the backend is still down, renders a dead shell. A successful
            // request flips reachability back on its own (see api.ts).
            await queryClient.refetchQueries();
          } finally {
            setRetrying(false);
          }
        }}
        className="ml-1 flex h-[32px] shrink-0 items-center gap-[6px] rounded-lg border px-3 text-[12.5px] font-bold text-ink transition-colors hover:bg-white/[.07] disabled:opacity-60"
        style={{ background: "rgba(255,255,255,.05)", borderColor: "rgba(255,255,255,.18)" }}
      >
        <RefreshCw size={13} className={retrying ? "animate-spin" : undefined} />
        {retrying ? t("serviceUnreachable.retrying") : t("serviceUnreachable.retry")}
      </button>
    </div>
  );
}
