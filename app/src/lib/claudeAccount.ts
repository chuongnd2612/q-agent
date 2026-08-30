import type { ClaudeCredentialsStatus, HubClaudeCredential } from "@/types/api";

/** The three words the chip and the panel badge are allowed to show. */
export type ClaudeAccountLabel = "Personal" | "Shared" | "Not set";

/**
 * Which Claude account would a run authenticate with? (#763)
 *
 * One helper so the top-bar chip and the panel's CREDENTIAL badge below it
 * cannot disagree — and, more importantly, so the chip does not read the wrong
 * store. With hub data on, EmeHub resolves the credential a run uses
 * (`/ai/credentials/hub` → the hub's `/credentials/claude/resolve`), and
 * Q-Agent's own local credential store says nothing about it; labelling from
 * the local store there would put a confident, wrong word in the top bar (the
 * same class of defect as #760).
 *
 * - hub data **on**  → `hubCred`: `available: false` (or no answer yet) →
 *   `Not set`; `source === "own"` → `Personal`; anything else present →
 *   `Shared`. That last fallback matches how `HubCredentialSummary` already
 *   renders the source line, so the badge and the body can't contradict.
 *   Crucially the fallback sits *inside* `available`, so "no credential" can
 *   never come out as "Shared" — the bug EmeHub hit in #240.
 * - hub data **off** → the local `credStatus.mode`, exactly as the panel badge
 *   has always done.
 *
 * The copy is shared verbatim with EmeHub's chip (`chuongnd2612/emehub#240`)
 * so the two products read identically for the same user at the same moment.
 */
export function claudeAccountLabel(
  hubData: boolean,
  hubCred: HubClaudeCredential | undefined,
  credStatus: ClaudeCredentialsStatus | undefined,
): ClaudeAccountLabel {
  if (hubData) {
    if (hubCred?.available !== true) return "Not set";
    return hubCred.source === "own" ? "Personal" : "Shared";
  }
  const mode = credStatus?.mode ?? "none";
  return mode === "own" ? "Personal" : mode === "shared" ? "Shared" : "Not set";
}
