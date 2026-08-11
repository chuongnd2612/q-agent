/**
 * Where the hub becomes Q-Agent's session authority (#531).
 *
 * This module exists so the two halves need not know each other: `lib/api.ts`
 * owns the 401 path and the auth store, `lib/hubSso.ts` owns the hub round trip
 * and reads `API_BASE` from `lib/api.ts`. Importing hubSso *from* api would close
 * that loop and undo the deliberate arrangement whereby the transport layer has
 * never heard of the hub. So the knowledge lives here, at the edge, and is
 * injected once at startup.
 *
 * ## Why identity is derived and not stored
 *
 * An SSO session carries no `qagent_refresh` cookie. Signing out on the hub used
 * to leave Q-Agent signed in as the previous user — and because Q-Agent was then
 * never anonymous, it never reached `/login`, where the SSO handoff is the only
 * thing that could have corrected the identity. The next person to use that
 * browser inherited the session, and with it the previous user's projects,
 * tickets and runs.
 *
 * Nothing is cached now, so nothing can go stale. Every renewal asks the hub, and
 * the answer always describes whoever is signed in there at that moment.
 */

import { setSessionRenewer, markLoggingOut } from "@/lib/api";
import { renewSessionFromHub } from "@/lib/hubSso";
import { withBase } from "@/lib/basePath";
import { useAuth } from "@/store/auth";

/** Renewals closer together than this are the same event — a burst of 401s, or a
 * tab being flicked back and forth. One hub round trip covers them all. */
const COALESCE_MS = 5_000;

let lastRenewAt = 0;
let inFlight: Promise<"refreshed" | "expired" | "unreachable"> | null = null;

/**
 * Ask the hub who is signed in, and reconcile Q-Agent with the answer.
 *
 * The three outcomes map onto `lib/api.ts`'s refresh outcomes deliberately, and
 * the distinction is the load-bearing part: only a hub that *answered* and said
 * "nobody" may sign someone out. A hub that could not be reached says nothing
 * about the session, and telling a user they are logged out because the hub is
 * down is the single most confusing outcome available.
 */
async function renew(): Promise<"refreshed" | "expired" | "unreachable"> {
  if (inFlight) return inFlight;

  inFlight = (async () => {
    const result = await renewSessionFromHub();
    lastRenewAt = Date.now();

    if (result.outcome === "renewed") {
      // Installs whoever the hub currently says — which is how a switched user is
      // picked up rather than papered over.
      useAuth.getState().setSession({
        accessToken: result.session.accessToken,
        user: result.session.user,
      });
      return "refreshed";
    }

    if (result.outcome === "signed-out-at-hub") return "expired";
    return "unreachable";
  })().finally(() => {
    inFlight = null;
  });

  return inFlight;
}

/**
 * Re-check identity when a hidden tab comes back to the front.
 *
 * Renewal is otherwise driven by a 401, and access tokens last 15 minutes, so an
 * idle tab would keep rendering the previous user's data until something happened
 * to ask the API. Signing out on the hub and switching back to a Q-Agent tab is
 * exactly that situation, and it is the one the user notices.
 *
 * A changed identity is installed silently. A hub that reports nobody signed in
 * ends the session here too, which is the behaviour this whole change is for:
 * **logging out on the hub logs you out of Q-Agent.**
 */
function onVisible(): void {
  if (document.visibilityState !== "visible") return;
  // Nothing to reconcile while anonymous — `HubSsoEntry` handles arrival.
  if (useAuth.getState().status !== "authed") return;
  if (Date.now() - lastRenewAt < COALESCE_MS) return;

  void renew().then((outcome) => {
    if (outcome !== "expired") return;
    // Mark first: clearing the session 401s any request already in flight, and
    // the interceptor's own redirect would otherwise race this one.
    markLoggingOut();
    useAuth.getState().logout();
    window.location.assign(withBase("/login"));
  });
}

/**
 * Wire the hub in as the session authority. Called once, from `main.tsx`.
 *
 * Safe to call when the integration is off: `renewSessionFromHub` reports
 * `unknown` for a deployment without SSO, which is `unreachable` here — and
 * `unreachable` changes nothing, so a local-only deployment behaves exactly as it
 * did before.
 */
export function installHubSessionAuthority(): void {
  setSessionRenewer(renew);
  document.addEventListener("visibilitychange", onVisible);
}
