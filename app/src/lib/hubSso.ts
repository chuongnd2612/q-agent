/**
 * EmeHub SSO bootstrap — the browser half of the round trip (#480, B3).
 *
 * The flow, end to end (`docs/HUB-INTEGRATION.md` §2.1 / §3 B3):
 *
 *   `POST {hubBaseUrl}/auth/agent-token`  (hub origin, cookies + X-CSRF-Token)
 *      → `POST /auth/sso/complete`        (Q-Agent, same origin)
 *      → an ordinary Q-Agent session, then navigate to `next`
 *
 * Two rules are load-bearing here:
 *
 * 1. **Call `/auth/agent-token`, never `/auth/refresh`.** The former does not
 *    rotate the hub's refresh token; the latter does. Calling `/auth/refresh`
 *    from here would race an open hub tab into a mutual logout.
 * 2. **`/auth/sso/complete` returns a login-shaped body.** So this module hands
 *    the result straight to the existing auth store and nothing else in the
 *    frontend — not `lib/api.ts`, not `RequireAuth` — needs to know the hub
 *    exists. It deliberately does not add methods to `lib/api.ts`: the hub call
 *    is cross-origin with its own cookie/CSRF rules and must not inherit the
 *    typed client's bearer-token + 401→refresh behaviour.
 */

import { withBase } from "@/lib/basePath";
import { API_BASE } from "@/lib/api";
import type { User } from "@/types/api";

/** Sentinel that keeps a signed-out visitor from ping-ponging between `/login`
 * and `/sso/callback` forever. Session-scoped on purpose: one attempt per tab,
 * and a fresh tab (or a fresh visit after signing in at the hub) tries again. */
const ATTEMPT_KEY = "qagent.hubSso.attempted";

export function hasAttemptedHubSso(): boolean {
  try {
    return sessionStorage.getItem(ATTEMPT_KEY) === "1";
  } catch {
    // Private-mode / storage-disabled: treat as "already attempted" so we fail
    // closed to the login form rather than looping.
    return true;
  }
}

export function markHubSsoAttempted(): void {
  try {
    sessionStorage.setItem(ATTEMPT_KEY, "1");
  } catch {
    /* nothing we can do; hasAttemptedHubSso() fails closed */
  }
}

/** Clear the one-shot marker so a deliberate retry can run again. */
export function clearHubSsoAttempt(): void {
  try {
    sessionStorage.removeItem(ATTEMPT_KEY);
  } catch {
    /* ignore */
  }
}

export interface HubSsoConfig {
  hubSsoEnabled: boolean;
  hubBaseUrl: string;
}

/**
 * Read the SSO flag + hub origin from `GET /health`.
 *
 * `/health` rather than `/capabilities` because this runs while anonymous and
 * only `/health` is in the backend's auth allowlist (#478). Raw `fetch` rather
 * than `api.health()` so a probe failure here can be distinguished from an auth
 * failure without touching `lib/api.ts`'s shared error path (that rework is B5).
 */
export async function fetchHubSsoConfig(): Promise<HubSsoConfig> {
  const res = await fetch(`${API_BASE}/health`, { credentials: "include" });
  if (!res.ok) throw new Error(`health probe failed (HTTP ${res.status})`);
  const body = (await res.json()) as Partial<HubSsoConfig>;
  return {
    hubSsoEnabled: body.hubSsoEnabled === true,
    hubBaseUrl: (body.hubBaseUrl ?? "").replace(/\/$/, ""),
  };
}

/** Why a bootstrap attempt failed. Drives which screen the user sees — telling
 * someone they're logged out when the hub is merely unreachable is the single
 * most confusing outcome available (§5). */
export type HubSsoFailure =
  /** Refused / DNS / timeout / 502–504 — the hub is down. Never show the login form. */
  | "hub-unreachable"
  /** 401 — simply not signed in at the hub. Not an error: fall through to `/login`. */
  | "not-signed-in"
  /** 403 — stale CSRF cookie state at the hub; re-sign-in there. */
  | "csrf-mismatch"
  /** 400 — this agent's audience isn't registered on the hub. Operator error. */
  | "misconfigured"
  /** The hub minted a token but Q-Agent refused it (bad secret, dead account…). */
  | "rejected-by-qagent";

export class HubSsoError extends Error {
  constructor(
    public reason: HubSsoFailure,
    message: string,
  ) {
    super(message);
    this.name = "HubSsoError";
  }
}

/** Read a browser cookie by name. The hub's `emehub_csrf` cookie is
 * deliberately JS-readable (§2.1) and is scoped to the shared parent domain so
 * this page can see it. */
function readCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const escaped = name.replace(/([.$?*|{}()[\]\\/+^])/g, "\\$1");
  const match = document.cookie.match(new RegExp("(?:^|; )" + escaped + "=([^;]*)"));
  return match ? decodeURIComponent(match[1]) : null;
}

/**
 * Ask the hub for a short-lived agent token for this agent.
 *
 * `credentials: 'include'` sends the shared `emehub_refresh` cookie;
 * `X-CSRF-Token` carries the readable `emehub_csrf` cookie. **This is
 * `/auth/agent-token`, not `/auth/refresh`** — see the module header.
 *
 * The header MUST be `X-CSRF-Token` (#495). `docs/HUB-INTEGRATION.md` §2.1
 * originally specified `X-CSRF`, which the hub never reads
 * (`emehub/api/app/deps_auth.py`: `CSRF_HEADER = "X-CSRF-Token"`), so the
 * double-submit check saw no header at all and answered 403 every time.
 */
export async function requestHubAgentToken(hubBaseUrl: string): Promise<string> {
  let res: Response;
  try {
    res = await fetch(`${hubBaseUrl}/auth/agent-token`, {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": readCookie("emehub_csrf") ?? "",
      },
      body: JSON.stringify({ audience: "qagent" }),
    });
  } catch (err) {
    // A network-layer throw is refused/DNS/timeout — the hub is down, and the
    // user's session is very likely still fine.
    throw new HubSsoError("hub-unreachable", err instanceof Error ? err.message : "Network error");
  }

  if (!res.ok) {
    if (res.status === 401) throw new HubSsoError("not-signed-in", "Not signed in at EmeHub");
    if (res.status === 403) throw new HubSsoError("csrf-mismatch", "EmeHub CSRF check failed");
    if (res.status === 400) throw new HubSsoError("misconfigured", "Audience not registered");
    if (res.status >= 500) throw new HubSsoError("hub-unreachable", `EmeHub error ${res.status}`);
    throw new HubSsoError("hub-unreachable", `Unexpected EmeHub response ${res.status}`);
  }

  const body = (await res.json()) as { accessToken?: string };
  if (!body.accessToken) throw new HubSsoError("hub-unreachable", "EmeHub returned no token");
  return body.accessToken;
}

/**
 * Mint a fresh EmeHub agent token for a **hub data read** (#498), or return
 * `null` when one isn't obtainable.
 *
 * Callers attach the result as the `X-Hub-Token` header on a Q-Agent API request;
 * our backend spends it on one hub call and never stores it (`deps_hub.py`).
 *
 * Minted per use rather than cached, because agent tokens live 15 minutes **and**
 * are bound to a live hub session — a cached one is expired or about to be, and
 * acting on a stale one fails in ways that look like bugs.
 *
 * Returns `null` rather than throwing when the hub can't authorise us — not
 * signed in there, session revoked, hub unreachable, or the integration is off.
 * Every caller is expected to degrade to local data in that case, so a missing
 * hub token must not break a screen.
 */
export async function mintHubDataToken(): Promise<string | null> {
  try {
    const { hubSsoEnabled, hubBaseUrl } = await fetchHubSsoConfig();
    if (!hubSsoEnabled || !hubBaseUrl) return null;
    return await requestHubAgentToken(hubBaseUrl);
  } catch {
    // HubSsoError (any reason) or a failed probe — the caller falls back to
    // local data. Deliberately swallowed: a hub read is an enhancement, never a
    // precondition for the screen working.
    return null;
  }
}

/** Login-shaped response from `POST /auth/sso/complete` — identical to
 * `/auth/login` plus the clamped `next` the backend echoes back. */
export interface SsoCompleteResult {
  accessToken: string;
  user: User;
  next: string;
}

/**
 * Trade the hub token for a normal Q-Agent session.
 *
 * Same-origin relative path (like every other `/auth/*` call) so the httpOnly
 * `qagent_refresh` cookie is set on our own origin — from here on the hub is
 * out of the loop and the session behaves like any password login.
 */
export async function completeSsoBootstrap(
  hubToken: string,
  next: string | null,
): Promise<SsoCompleteResult> {
  // Same-origin, but the app may be mounted under a prefix — and this is a raw
  // fetch rather than a call through `lib/api.ts`, so nothing else adds it.
  const res = await fetch(withBase("/auth/sso/complete"), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ hubToken, next: next ?? undefined }),
  });
  if (!res.ok) {
    throw new HubSsoError("rejected-by-qagent", `Q-Agent refused the EmeHub token (${res.status})`);
  }
  const body = (await res.json()) as Partial<SsoCompleteResult>;
  if (!body.accessToken || !body.user) {
    throw new HubSsoError("rejected-by-qagent", "Bootstrap response was not a session");
  }
  return { accessToken: body.accessToken, user: body.user, next: body.next ?? "/" };
}
