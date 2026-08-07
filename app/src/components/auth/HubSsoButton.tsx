/**
 * "Sign in with EmeHub" — the manual SSO entry point on `/login` (#481, B4).
 *
 * Renders **only** when the backend reports the integration on, so with
 * `QAGENT_HUB_SSO_ENABLED` off (the default) `/login` is byte-for-byte the screen
 * it has always been: no button, no divider, no layout shift. Local email +
 * password keeps working underneath either way — this slice is purely additive.
 *
 * Why a button is needed at all, given `HubSsoEntry` already auto-bounces a
 * first-time visitor to `/sso/callback`: that bounce is one-shot per tab. Anyone
 * who has already used it up — a visitor who signed out, or who came back after
 * the callback fell through to `/login` — has no way back to the hub without
 * this. So the click deliberately calls `clearHubSsoAttempt()` first: without
 * that, `HubSsoEntry`'s marker is still set, and pressing the button would land
 * on `/sso/callback` only to be bounced straight back to `/login`, looking
 * broken. Clearing the marker is what makes the button an actual retry.
 *
 * The flag is read through `fetchHubSsoConfig()` (`GET /health`) rather than
 * `/capabilities`, because this screen is anonymous and only `/health` is in the
 * backend's auth allowlist (#478).
 */

import { useEffect, useState, type CSSProperties } from "react";
import { useTranslation } from "react-i18next";
import { withBase } from "@/lib/basePath";
import { clearHubSsoAttempt, fetchHubSsoConfig } from "@/lib/hubSso";

/** The hub's mark, inline so the button needs no network fetch and no binary asset. */
function EmeHubMark() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M12 2.5 20.5 7v10L12 21.5 3.5 17V7L12 2.5Z"
        stroke="currentColor"
        strokeWidth="1.9"
        strokeLinejoin="round"
      />
      <path d="M12 7.5v9M8 9.75v4.5M16 9.75v4.5" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" />
    </svg>
  );
}

const outlineBtn: CSSProperties = {
  background: "rgba(255,255,255,.04)",
  borderColor: "rgba(255,255,255,.18)",
};

export function HubSsoButton() {
  const { t } = useTranslation("auth");
  const [enabled, setEnabled] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const { hubSsoEnabled, hubBaseUrl } = await fetchHubSsoConfig();
        // Require BOTH: a flag with no hub origin configured can't be launched,
        // and offering a button that cannot work is worse than offering none.
        if (!cancelled) setEnabled(hubSsoEnabled && Boolean(hubBaseUrl));
      } catch {
        // Probe failed → render nothing. Never block or degrade the local form
        // because the flag couldn't be read.
        if (!cancelled) setEnabled(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (!enabled) return null;

  return (
    <>
      <div className="mt-[18px] flex items-center gap-3" aria-hidden="true">
        <span className="h-px flex-1" style={{ background: "rgba(255,255,255,.09)" }} />
        <span className="text-[11.5px] font-semibold uppercase tracking-[0.14em] text-faint">
          {t("sso.dividerOr")}
        </span>
        <span className="h-px flex-1" style={{ background: "rgba(255,255,255,.09)" }} />
      </div>

      <button
        type="button"
        onClick={() => {
          // Retire the one-shot marker so `/sso/callback` actually attempts the
          // hub instead of bouncing us back here (see the module header).
          clearHubSsoAttempt();
          // A full assign rather than `navigate`, matching HubSsoEntry: the
          // callback screen must mount clean, outside this route's subtree.
          window.location.assign(withBase("/sso/callback"));
        }}
        className="mt-[14px] flex h-[46px] w-full items-center justify-center gap-[9px] rounded-xl border text-[14.5px] font-bold text-ink transition-colors hover:bg-white/[.07]"
        style={outlineBtn}
      >
        <EmeHubMark />
        {t("sso.signInWith")}
      </button>
    </>
  );
}
