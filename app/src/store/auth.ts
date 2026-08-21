/**
 * Auth session state (Zustand) — ADR 0007. Companion to the UI store
 * (`store/ui.ts`), but for the authenticated principal + access token rather
 * than ephemeral UI.
 *
 * The access token is held **in memory only** — never persisted to
 * localStorage. The durable credential is the backend's httpOnly refresh
 * cookie; a fresh page load restores the session via `bootstrap()`, which
 * exchanges that cookie for a new access token. `lib/api.ts` reads the token
 * from here (`useAuth.getState()`) to attach the `Authorization` header and to
 * drive the silent 401 → refresh → retry flow.
 */

import { create } from "zustand";
import { restoreSession } from "@/lib/api";
import type { User } from "@/types/api";

export type AuthStatus = "idle" | "loading" | "authed" | "anon";

interface AuthState {
  user: User | null;
  accessToken: string | null;
  /** "idle" until `bootstrap()` runs; "loading" while refreshing; then
   * "authed" (session restored) or "anon" (no valid session). */
  status: AuthStatus;
  /** Install a freshly minted access token + principal (login / refresh). */
  setSession: (session: { accessToken: string; user: User }) => void;
  /** Clear all session state. Call after a server logout or a failed refresh. */
  logout: () => void;
  /** Restore a session from the refresh cookie. Safe to call once on app load
   * (RequireAuth guards on `status === "idle"`); concurrent calls are ignored. */
  bootstrap: () => Promise<void>;
}

export const useAuth = create<AuthState>((set, get) => ({
  user: null,
  accessToken: null,
  status: "idle",

  setSession: ({ accessToken, user }) => set({ accessToken, user, status: "authed" }),

  logout: () => set({ user: null, accessToken: null, status: "anon" }),

  bootstrap: async () => {
    if (get().status === "loading") return;
    set({ status: "loading" });
    // Boot goes through the SAME ladder as the 401 path (#611): refresh cookie
    // first, then the session authority (the hub). Calling `api.auth.refresh()`
    // raw here meant a boot could only be rescued by a cookie — and an SSO session
    // deliberately has none (#531/#532), so every reload of /qagent ended at
    // /login while the hub knew exactly who was signed in. `restoreSession`
    // installs the session itself on success, so there is nothing to set here.
    const outcome = await restoreSession();
    if (outcome === "refreshed" && get().status === "authed") return;
    // `expired` is a real answer. `unreachable` is not — but at boot there is no
    // token to render anything with either way, so both land anonymous for now and
    // the guard sends them to /login. A distinct service-down state belongs with
    // the wider degradation work, not here.
    set({ user: null, accessToken: null, status: "anon" });
  },
}));
