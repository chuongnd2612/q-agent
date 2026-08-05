/**
 * `/sso/callback` — the EmeHub bootstrap screen (#480, B3).
 *
 * Registered in `router.tsx` as a **top-level ungated sibling**, like
 * `/signed-out`:
 *   - *not* under `RedirectIfAuthed`, which would bounce a returning user
 *     mid-bootstrap;
 *   - *not* under `RequireAuth`, because arriving anonymous is the entire point.
 *
 * On mount it runs the round trip from `lib/hubSso.ts`, installs the resulting
 * (perfectly ordinary) Q-Agent session in the auth store, and navigates to
 * `?next=…` — or `/`. Because the backend hands back a login-shaped body, that
 * `setSession` call is the only contact this feature has with the store.
 *
 * Failure is not one screen. "The hub is unreachable" and "you are not signed in
 * at the hub" must never render the same thing (`docs/HUB-INTEGRATION.md` §5):
 * the first is a transient outage with a Retry, the second is a normal
 * fall-through to Q-Agent's own `/login`.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { AlertTriangle, RefreshCw } from "lucide-react";
import { AuthLayout, RedirectLoader } from "@/components/auth/AuthLayout";
import { useAuth } from "@/store/auth";
import {
  clearHubSsoAttempt,
  completeSsoBootstrap,
  fetchHubSsoConfig,
  HubSsoError,
  markHubSsoAttempted,
  requestHubAgentToken,
  type HubSsoFailure,
} from "@/lib/hubSso";

const GRADIENT = "linear-gradient(135deg,#8b5cf6,#6366f1)";

/** Failure reasons that are the user's cue to go sign in here instead. Anything
 * else is an outage or a misconfiguration and gets an explicit message — we
 * never render the login form to explain that the hub is down. */
const FALL_THROUGH_TO_LOGIN: HubSsoFailure[] = ["not-signed-in"];

export function SsoCallback() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const { t } = useTranslation("auth");
  const [failure, setFailure] = useState<HubSsoFailure | null>(null);
  const next = params.get("next");

  // React 18 StrictMode double-invokes effects in dev; the bootstrap must not
  // run twice (it would mint two sessions and burn the hub token twice).
  const started = useRef(false);

  const run = useCallback(async () => {
    setFailure(null);
    // Mark up front: if anything below fails we must not be sent back here.
    markHubSsoAttempted();
    try {
      const { hubSsoEnabled, hubBaseUrl } = await fetchHubSsoConfig();
      if (!hubSsoEnabled || !hubBaseUrl) {
        // Someone typed the URL with the integration off — nothing to do here.
        navigate("/login", { replace: true });
        return;
      }
      const hubToken = await requestHubAgentToken(hubBaseUrl);
      const session = await completeSsoBootstrap(hubToken, next);
      useAuth.getState().setSession({ accessToken: session.accessToken, user: session.user });
      navigate(session.next || "/", { replace: true });
    } catch (err) {
      const reason: HubSsoFailure =
        err instanceof HubSsoError ? err.reason : "hub-unreachable";
      if (FALL_THROUGH_TO_LOGIN.includes(reason)) {
        navigate("/login", { replace: true });
        return;
      }
      setFailure(reason);
    }
  }, [navigate, next]);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    void run();
  }, [run]);

  if (!failure) return <RedirectLoader label={t("sso.connecting")} />;

  return (
    <AuthLayout>
      <div className="text-center">
        <div
          className="mx-auto mb-5 flex h-[60px] w-[60px] items-center justify-center rounded-[19px]"
          style={{
            background: GRADIENT,
            boxShadow: "0 12px 30px -8px rgba(139,92,246,.7)",
            animation: "scaleIn .4s ease both",
          }}
        >
          <AlertTriangle size={28} color="#fff" strokeWidth={2.2} />
        </div>
        <h2 className="m-0 mb-2 text-[24px] font-black tracking-[-0.02em]">
          {t(`sso.failure.${failure}.title`)}
        </h2>
        <p className="m-0 mb-6 text-[13.5px] leading-relaxed text-muted">
          {t(`sso.failure.${failure}.body`)}
        </p>
        <button
          type="button"
          onClick={() => {
            // A deliberate retry clears the one-shot marker so the entry-point
            // redirect is allowed to fire again on the next anonymous load.
            clearHubSsoAttempt();
            started.current = true;
            void run();
          }}
          className="flex h-[46px] w-full items-center justify-center gap-[9px] rounded-xl border-none text-[14.5px] font-bold text-white transition-[filter] hover:brightness-110"
          style={{ background: GRADIENT, boxShadow: "0 10px 26px -8px rgba(139,92,246,.8)" }}
        >
          <RefreshCw size={17} strokeWidth={2.4} />
          {t("sso.retry")}
        </button>
        <button
          type="button"
          onClick={() => navigate("/login", { replace: true })}
          className="mt-3 h-[42px] w-full rounded-xl border border-white/10 bg-transparent text-[13.5px] font-semibold text-muted transition hover:text-ink"
        >
          {t("sso.useLocalSignIn")}
        </button>
      </div>
    </AuthLayout>
  );
}
