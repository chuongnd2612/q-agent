// Classify a captured sessionStorage map so a half-finished login can never be
// saved as if it were a session, nor replayed into an authoring browser (#618).
//
// MSAL keeps two very different things in sessionStorage:
//
//   * HANDSHAKE state, present only while a redirect login is in flight —
//     `msal.interaction.status`, `<clientId>.code.verifier`,
//     `<clientId>.request.params`, `<clientId>.request.origin`.
//   * TOKENS, present once login has completed — `msal.token.keys.<clientId>`,
//     `msal.account.keys`, and per-entity `…-idtoken-…` / `…-accesstoken-…` /
//     `…-refreshtoken-…` entries.
//
// A capture that ends mid-redirect therefore stores handshake keys and no tokens.
// Replaying those is WORSE than replaying nothing: MSAL reads a stale
// `interaction.status`, believes an interaction is already running, and either
// restarts the redirect or fails with `interaction_in_progress` — so the operator
// sees an unauthenticated app despite having "captured a login".
//
// Observed on a real box: 4 handshake keys, zero tokens, and only `msal.version`
// in localStorage, while the IdP cookies WERE present — i.e. the profile could
// have re-authenticated silently if the poisoned replay had not pre-empted it.
//
// Vendored copy — keep in sync with api/app/services/pw_scripts/session_auth_state.cjs
// (a test asserts the two files are byte-identical).

const HANDSHAKE_MARKERS = [
  'msal.interaction.status',
  '.code.verifier',
  '.request.params',
  '.request.origin',
];

const TOKEN_MARKERS = [
  'msal.token.keys',
  'msal.account.keys',
  '-idtoken-',
  '-accesstoken-',
  '-refreshtoken-',
];

function hasAny(keys, markers) {
  return keys.some((k) => markers.some((m) => k.includes(m)));
}

/**
 * Classify one origin's sessionStorage map.
 * Returns 'empty' | 'authenticated' | 'handshake-only' | 'unknown'.
 *
 * 'unknown' is deliberate and must be treated as REPLAYABLE: a non-MSAL app keeps
 * arbitrary sessionStorage, and this guard exists only to catch the MSAL
 * mid-redirect trap — it must never start discarding other apps' state.
 */
function classify(map) {
  const keys = Object.keys(map || {});
  if (keys.length === 0) return 'empty';
  if (hasAny(keys, TOKEN_MARKERS)) return 'authenticated';
  if (hasAny(keys, HANDSHAKE_MARKERS)) return 'handshake-only';
  return 'unknown';
}

/** True when this origin's map must not be persisted or replayed. */
function isPoisoned(map) {
  return classify(map) === 'handshake-only';
}

/**
 * Drop origins whose state is a half-finished handshake, returning
 * `[cleaned, dropped]`. Everything else is preserved untouched.
 */
function sanitize(byOrigin) {
  const cleaned = {};
  const dropped = [];
  for (const [origin, map] of Object.entries(byOrigin || {})) {
    if (isPoisoned(map)) dropped.push(origin);
    else cleaned[origin] = map;
  }
  return [cleaned, dropped];
}

/** True when at least one origin looks genuinely logged in. */
function anyAuthenticated(byOrigin) {
  return Object.values(byOrigin || {}).some((m) => classify(m) === 'authenticated');
}

module.exports = { classify, isPoisoned, sanitize, anyAuthenticated };
