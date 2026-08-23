// Classify a captured sessionStorage map so a half-finished login can never be
// saved as if it were a session, nor replayed into an authoring browser (#618).
//
// MSAL keeps two very different things in sessionStorage:
//
//   * HANDSHAKE state, present only while a redirect login is in flight —
//     `msal.interaction.status`, `<clientId>.code.verifier`,
//     `<clientId>.request.params`, `<clientId>.request.origin`.
//   * TOKENS, present once login has completed — `…token.keys.<clientId>`,
//     `…account.keys`, and per-entity `…idtoken…` / `…accesstoken…` /
//     `…refreshtoken…` entries.
//
// WHICH storage holds the tokens is the app's decision, via MSAL's
// `cacheLocation`: `sessionStorage` for some apps, `localStorage` for others. So
// a sessionStorage map holding ONLY handshake keys does not by itself mean the
// login failed — under `localStorage` that is exactly what a SUCCESSFUL login
// leaves behind (#638). Never conclude "not logged in" from this map alone;
// `anyAuthenticatedAcross` exists to judge both stores together.
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
// Dropping such a map is still right, but it is NOT a licence to skip restoring
// auth: the authoring launcher must fall back to the captured storageState
// (cookies + localStorage) rather than trusting the Chrome profile (#638).
//
// Vendored copy — keep in sync with api/app/services/pw_scripts/session_auth_state.cjs
// (a test asserts the two files are byte-identical).

const HANDSHAKE_MARKERS = [
  'msal.interaction.status',
  '.code.verifier',
  '.request.params',
  '.request.origin',
];

// Match the STABLE part of each key, because MSAL's cache schema versions the
// prefix and changes the delimiter (#638):
//
//   v1: msal.token.keys.<clientId>          msal.<home-id>-<env>-idtoken-<clientId>-…
//   v2: msal.2.token.keys.<clientId>        msal.2|<home-id>|<authority>|idtoken|<clientId>|…
//
// The original markers hard-coded the v1 prefix and DASH delimiters, so no v2
// token matched. An authenticated v2 map that still carried a handshake leftover
// — the normal state right after a redirect login — was therefore classified
// 'handshake-only' and thrown away, and a completed login was announced as
// "NO authenticated session captured".
const TOKEN_MARKERS = [
  'token.keys',
  'account.keys',
  'idtoken',
  'accesstoken',
  'refreshtoken',
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

/**
 * True when at least one origin in ANY of the given `{origin: map}` collections
 * looks logged in.
 *
 * Which storage holds the tokens is the app's choice, not ours: MSAL's
 * `cacheLocation` is `sessionStorage` for some apps and `localStorage` for
 * others. Under `localStorage` a COMPLETED login leaves the tokens in
 * localStorage and only handshake leftovers in sessionStorage — so judging the
 * capture on sessionStorage alone declares a perfectly good login "not
 * captured" (#638). Pass both maps.
 */
function anyAuthenticatedAcross(...byOriginMaps) {
  return byOriginMaps.some((m) => anyAuthenticated(m));
}

/**
 * Reduce a parsed Playwright `storageState.json` to `{cookies, localByOrigin}`,
 * where `localByOrigin` is `{origin: {key: value}}` — the shape the replay code
 * wants. Returns null when there is nothing usable to restore.
 *
 * Pure so the authoring launcher's auth material can be unit-tested without
 * spawning Chrome (#638).
 */
function storageStateToMaps(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const localByOrigin = {};
  for (const o of raw.origins || []) {
    if (!o || !o.origin) continue;
    const map = {};
    for (const e of o.localStorage || []) {
      if (e && typeof e.name === 'string') map[e.name] = e.value;
    }
    if (Object.keys(map).length) localByOrigin[o.origin] = map;
  }
  const cookies = Array.isArray(raw.cookies) ? raw.cookies : [];
  if (!cookies.length && !Object.keys(localByOrigin).length) return null;
  return { cookies, localByOrigin };
}

module.exports = {
  classify, isPoisoned, sanitize, anyAuthenticated, anyAuthenticatedAcross, storageStateToMaps,
};
