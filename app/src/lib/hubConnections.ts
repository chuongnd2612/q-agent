/**
 * Hub-owned provider connections — read-only, informational (C4 of #501).
 *
 * **These cannot be used for anything.** The hub's `/connections` reports
 * `hasPat: true` and never the PAT, and the endpoint that would let us borrow a
 * hub connection (`POST /connections/{id}/proxy`) is deliberately unbuilt. So
 * every real provider call — ticket sync, repo discovery, connection test —
 * keeps running on Q-Agent's own connections, and this module exists purely so a
 * user can *see* what the hub holds instead of wondering why Q-Agent can't.
 *
 * Two deliberate shapes here:
 *
 * 1. **Its own client, not `lib/api.ts`.** The typed client has no notion of the
 *    per-request `X-Hub-Token` header, and this call must never inherit its
 *    error surfacing: a hub hiccup is not an app error (see 2).
 * 2. **It never throws.** Every failure path — hub off, no live hub session, an
 *    expired 15-minute token, a hub that isn't answering — resolves to `[]`. The
 *    Settings screen's job is the *local* connection picker, and a hub problem
 *    must not put an error over it or block it.
 */

import { API_BASE } from "@/lib/api";
import { mintHubDataToken } from "@/lib/hubSso";
import { useAuth } from "@/store/auth";

/** One hub-owned connection, exactly as `GET /hub/connections` serves it.
 * There is no secret field, and there is no field that could carry one. */
export interface HubConnectionOut {
  id: string;
  kind: string;
  label: string;
  baseUrl: string;
  capabilities: string[];
  supportedCapabilities: string[];
  connected: boolean;
  /** Whether the *hub* holds a PAT for this connection. We never receive it. */
  hasPat: boolean;
  lastSync: string | null;
  lastTestedAt: string | null;
  shared: boolean;
}

/**
 * List the connections EmeHub holds, or `[]` if we can't tell.
 *
 * `[]` is returned for "hub off", "no hub session", "token expired" and "hub
 * down" alike, and the caller deliberately can't distinguish them: in all four
 * the honest UI is to show nothing extra rather than an error the user can do
 * nothing about.
 */
export async function fetchHubConnections(): Promise<HubConnectionOut[]> {
  try {
    // No live hub session (or SSO off) → nothing to ask for. Skipping the
    // request entirely keeps the flag-off path free of network noise.
    const hubToken = await mintHubDataToken();
    if (!hubToken) return [];

    const accessToken = useAuth.getState().accessToken;
    const res = await fetch(`${API_BASE}/hub/connections`, {
      headers: {
        Accept: "application/json",
        "X-Hub-Token": hubToken,
        ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      },
    });
    if (!res.ok) return [];
    const body: unknown = await res.json();
    return Array.isArray(body) ? (body as HubConnectionOut[]) : [];
  } catch {
    // Network error, aborted request, unparseable body — all "we don't know
    // what the hub has", which renders identically to "the hub has nothing".
    return [];
  }
}
