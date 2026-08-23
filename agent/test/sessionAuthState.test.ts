import { strict as assert } from "node:assert";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

// eslint-disable-next-line @typescript-eslint/no-var-requires
const authState = require("../../vendor/session_auth_state.cjs");

const REPO = join(__dirname, "..", "..", "..");

/**
 * #618: live authoring landed unauthenticated because the capture stored MSAL's
 * MID-REDIRECT handshake and the launcher replayed it, making MSAL believe an
 * interaction was already running. Observed on a real box: 4 handshake keys, zero
 * tokens, only `msal.version` in localStorage — while the IdP cookies were present,
 * so the profile could have re-authenticated silently had the poisoned replay not
 * pre-empted it.
 */
test("the real captured shape is recognised as a half-finished login", () => {
  const observed = {
    "msal.19c69581-5071-4a63-a272-613c9f93e947.request.origin": "x",
    "msal.19c69581-5071-4a63-a272-613c9f93e947.code.verifier": "x",
    "msal.interaction.status": "interaction_in_progress",
    "msal.19c69581-5071-4a63-a272-613c9f93e947.request.params": "x",
  };
  assert.equal(authState.classify(observed), "handshake-only");
  assert.equal(authState.isPoisoned(observed), true);
  assert.equal(authState.anyAuthenticated({ "https://app": observed }), false);
});

test("a completed login is authenticated and kept", () => {
  const done = { "msal.token.keys.abc": "x", "msal.account.keys": "y" };
  assert.equal(authState.classify(done), "authenticated");
  assert.equal(authState.isPoisoned(done), false);
  assert.equal(authState.anyAuthenticated({ "https://app": done }), true);
});

test("non-MSAL state is NEVER discarded", () => {
  // The guard exists only for the MSAL trap. Dropping arbitrary app state would
  // break every other app's auth replay, which is a far worse failure.
  const other = { "myapp.session": "abc", token: "def" };
  assert.equal(authState.classify(other), "unknown");
  assert.equal(authState.isPoisoned(other), false);
  const [clean, dropped] = authState.sanitize({ "https://other": other });
  assert.deepEqual(dropped, []);
  assert.deepEqual(clean, { "https://other": other });
});

test("sanitize drops only the poisoned origin", () => {
  const [clean, dropped] = authState.sanitize({
    "https://bad": { "msal.interaction.status": "x" },
    "https://good": { "msal.token.keys.abc": "y" },
    "https://plain": { anything: "z" },
  });
  assert.deepEqual(dropped, ["https://bad"]);
  assert.deepEqual(Object.keys(clean).sort(), ["https://good", "https://plain"]);
});

test("an empty map is empty, not authenticated", () => {
  // The capture now records the transition to empty faithfully; before #618 the
  // "only assign when non-empty" guard retained the stale handshake forever.
  assert.equal(authState.classify({}), "empty");
  assert.equal(authState.anyAuthenticated({ "https://app": {} }), false);
});

/**
 * The scripts exist twice — the agent runs `agent/vendor/*`, the server runs
 * `api/app/services/pw_scripts/*`. #557 already cost a server/device divergence, so
 * pin byte-equality rather than trusting a "keep in sync" comment.
 */
test("the vendored scripts are byte-identical to the API copies", () => {
  for (const name of ["session_auth_state.cjs", "capture_auth.cjs", "authoring_browser.cjs"]) {
    const a = readFileSync(join(REPO, "agent", "vendor", name), "utf-8");
    const b = readFileSync(join(REPO, "api", "app", "services", "pw_scripts", name), "utf-8");
    assert.equal(a, b, `${name} has drifted between agent/vendor and api pw_scripts`);
  }
});

/**
 * #638: the markers hard-coded MSAL v1's key layout — the `msal.` prefix and
 * DASH-delimited entities — so nothing in a v2 cache matched. v2 versions the
 * prefix (`msal.2.token.keys.…`) and delimits entities with PIPES
 * (`msal.2|<home-id>|<authority>|idtoken|<clientId>|…`).
 *
 * The consequence was not a cosmetic mislabel: an authenticated v2 map that
 * still carried the handshake leftovers of the redirect it had just completed
 * (the normal post-login state) matched only HANDSHAKE_MARKERS, so it was
 * classified 'handshake-only' and DISCARDED — the capture threw away a real
 * session. Negative control: revert TOKEN_MARKERS to the dashed v1 forms and
 * this fails.
 */
test("an MSAL v2 cache is recognised as authenticated, pipes and all", () => {
  const clientId = "709f1ef3-17f0-4d83-ab1a-78e660fa6710";
  const v2 = {
    "msal.version": "1.0.0",
    [`msal.2.token.keys.${clientId}`]: "{}",
    "msal.2.account.keys": "[]",
    [`msal.2|home.id|tenant.ciamlogin.com|idtoken|${clientId}|||`]: "eyJ",
    [`msal.2|home.id|tenant.ciamlogin.com|refreshtoken|${clientId}|||`]: "eyJ",
  };
  assert.equal(authState.classify(v2), "authenticated");
  assert.equal(authState.isPoisoned(v2), false);

  // The load-bearing case: real tokens PLUS the handshake leftovers. Tokens win,
  // so the map survives sanitize() instead of being dropped.
  const withLeftovers = { ...v2, "msal.interaction.status": "interaction_in_progress" };
  assert.equal(authState.classify(withLeftovers), "authenticated");
  const [clean, dropped] = authState.sanitize({ "https://app": withLeftovers });
  assert.deepEqual(dropped, []);
  assert.deepEqual(Object.keys(clean), ["https://app"]);
});

/**
 * #638: with `cacheLocation: "localStorage"` a COMPLETED login puts its tokens in
 * localStorage and leaves only handshake keys in sessionStorage. Judging the
 * capture on sessionStorage alone therefore told the operator "NO authenticated
 * session captured" about a login that had plainly succeeded.
 */
test("a localStorage-cache login counts as captured", () => {
  const session = { "https://app": { "msal.interaction.status": "interaction_in_progress" } };
  const local = { "https://app": { "msal.2.token.keys.abc": "{}", "msal.version": "1.0.0" } };

  // The old, sessionStorage-only verdict — still false, correctly.
  assert.equal(authState.anyAuthenticated(session), false);
  // Judged across both stores, the login is recognised.
  assert.equal(authState.anyAuthenticatedAcross(local, session), true);
  // And neither store having tokens still reads as "nothing captured".
  assert.equal(authState.anyAuthenticatedAcross({ "https://app": { "msal.version": "1" } }, session), false);
});

/**
 * #638: the authoring launcher restores auth from the captured storageState
 * because the Chrome profile is mutable state a failed run poisons (measured: a
 * run that hit the login page left MSAL's cache cleared, so every later run
 * inherited a profile holding `msal.version` and nothing else). This pins the
 * reduction the launcher feeds to `addCookies` + the localStorage init script.
 */
test("storageState reduces to cookies + a per-origin localStorage map", () => {
  const reduced = authState.storageStateToMaps({
    cookies: [{ name: "ESTSAUTH", domain: ".ciamlogin.com", path: "/" }],
    origins: [
      { origin: "https://app", localStorage: [{ name: "msal.2.account.keys", value: "[]" }] },
      // No usable entries — must not create an empty origin the replay would
      // then "restore" as nothing.
      { origin: "https://empty", localStorage: [] },
    ],
  });
  assert.equal(reduced.cookies.length, 1);
  assert.deepEqual(Object.keys(reduced.localByOrigin), ["https://app"]);
  assert.equal(reduced.localByOrigin["https://app"]["msal.2.account.keys"], "[]");

  // Nothing to restore ⇒ null, so the launcher can tell "no material" from
  // "material present but Playwright missing".
  assert.equal(authState.storageStateToMaps({ cookies: [], origins: [] }), null);
  assert.equal(authState.storageStateToMaps(null), null);
});

/**
 * #638 regression pin. Dropping a handshake-only sessionStorage map is correct,
 * but it must not disable the RESTORE PATH: gating the Playwright attach on the
 * sessionStorage map alone meant a localStorage-cache app injected nothing at
 * all and Chrome opened the app straight from the (poisoned) profile.
 */
test("the launcher arms its restore from storageState too, not just sessionStorage", () => {
  const src = readFileSync(join(REPO, "agent", "vendor", "authoring_browser.cjs"), "utf-8");
  // Either source of captured auth is enough to take the attach-then-navigate path.
  assert.match(src, /const chromium = byOrigin \|\| state \? tryPlaywright\(\) : null;/);
  // And the captured cookies + localStorage actually reach the context.
  assert.match(src, /ctx\.addCookies\(state\.cookies\)/);
  assert.match(src, /restore\(window\.localStorage/);
  // Never clobber a token the app has since rotated.
  assert.match(src, /store\.getItem\(k\) === null/);
});

/**
 * #638: the runner must hand the launcher the storageState path — the fix is
 * inert if the argument never arrives.
 */
test("the runner passes the captured storageState to the authoring launcher", () => {
  const src = readFileSync(join(REPO, "agent", "src", "runner.ts"), "utf-8");
  const call = src.slice(src.indexOf("vendorAuthoringScript(),"));
  assert.match(call.slice(0, 600), /sess \? sess\.storageStatePath : ""/);
});
