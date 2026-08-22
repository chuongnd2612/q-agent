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
