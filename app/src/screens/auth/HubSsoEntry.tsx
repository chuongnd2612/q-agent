/**
 * The EmeHub SSO entry point (#480, B3).
 *
 * Wraps the `/login` route only. With the integration on, an anonymous visitor
 * who has not yet tried the hub is sent to `/sso/callback` **once** — that is
 * what makes "log in at the hub → Launch QAgent → land here already signed in"
 * work for someone who navigates straight to Q-Agent.
 *
 * The one-shot `sessionStorage` marker (`lib/hubSso.ts`) is the whole safety
 * mechanism: `/sso/callback` sends a genuinely signed-out visitor back to
 * `/login`, so without it the two routes would ping-pong forever.
 *
 * It sits here rather than inside `RequireAuth` deliberately — `RequireAuth`,
 * `store/auth.ts` and `lib/api.ts` are untouched by this feature, which is the
 * point of the backend returning a login-shaped session (§3 B3). Everything that
 * reaches `/login` has already been established as anonymous by
 * `RedirectIfAuthed` above it, so this gate never has to reason about auth state.
 */

import { withBase } from "@/lib/basePath";
import { useEffect, useState } from "react";
import { Outlet } from "react-router-dom";
import { RedirectLoader } from "@/components/auth/AuthLayout";
import { fetchHubSsoConfig, hasAttemptedHubSso, markHubSsoAttempted } from "@/lib/hubSso";

export function HubSsoEntry() {
  // Already tried this tab (or storage is unavailable) → render the login form
  // immediately, with no probe and no flash.
  const [probing, setProbing] = useState(() => !hasAttemptedHubSso());

  useEffect(() => {
    if (!probing) return;
    let cancelled = false;
    void (async () => {
      try {
        const { hubSsoEnabled, hubBaseUrl } = await fetchHubSsoConfig();
        if (cancelled) return;
        if (hubSsoEnabled && hubBaseUrl) {
          markHubSsoAttempted();
          // A full assign rather than `navigate`: the callback screen must mount
          // clean, outside this guard's subtree.
          window.location.assign(withBase("/sso/callback"));
          return;
        }
      } catch {
        // The flag probe failed — fall through to local sign-in rather than
        // stranding the user on a loader. Not marked as attempted: the next load
        // in this tab may well reach the backend.
      }
      if (!cancelled) setProbing(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [probing]);

  if (probing) return <RedirectLoader />;
  return <Outlet />;
}
