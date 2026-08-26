/**
 * `BU_NAME` for the browser-harness daemon (#739).
 *
 * Live authoring used to leave this unset, so the daemon was "default" and the harness
 * attached to `pages[0]` — whichever page happened to be first in that Chrome. The
 * authoring profile is the SAME one the manual-login capture opens for the operator, so
 * a tab they left in it is restored on launch and taken over: Claude drove the
 * operator's own tab, on their own machine.
 *
 * A NAMED daemon gets its own dedicated tab (the harness's own comment says so), which
 * is why setting this correctly is the whole fix. And "correctly" has teeth: the harness
 * turns the name into a socket/pid FILENAME and rejects anything outside
 * `[A-Za-z0-9_-]{1,64}` (`browser_harness/_ipc._check`). An invalid name does not
 * degrade to the old behaviour — it fails authoring outright — so these tests are about
 * never emitting one.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { harnessName } from "../src/runner.js";

/** The harness's own guard, copied so a drift here fails loudly. */
const LEGAL = /^[A-Za-z0-9_-]{1,64}$/;

test("a normal session produces a legal, recognisable name", () => {
  const name = harnessName("sess-abc123", 763);
  assert.match(name, LEGAL);
  assert.ok(name.startsWith("qagent-"), name);
  assert.ok(name.includes("763"), name);
});

test("distinct sessions get distinct names, so concurrent runs do not share a tab", () => {
  // The harness comment: named daemons sharing a name fight over one tab and their
  // navigations clobber each other. A per-session name is what prevents that.
  assert.notEqual(harnessName("sess-a", 1), harnessName("sess-b", 1));
  assert.notEqual(harnessName("sess-a", 1), harnessName("sess-a", 2));
});

test("illegal characters are scrubbed rather than passed through", () => {
  // A session id is server-generated, but "it is always a uuid today" is not a property
  // — and the failure mode is authoring refusing to start, not a cosmetic name.
  for (const dirty of ["a/b", "a b", "a:b", "../etc", "a.b", "sess#1", "tên-việt"]) {
    assert.match(harnessName(dirty, 1), LEGAL, `from ${dirty}`);
  }
});

test("a long session id is capped, not truncated into something illegal", () => {
  const name = harnessName("x".repeat(200), 42);
  assert.match(name, LEGAL);
  assert.equal(name.length <= 64, true);
});

test("an empty session id still yields a usable name", () => {
  // Falling back to "default" would silently restore the exact bug this fixes, so the
  // fallback has to be a NAMED one.
  const name = harnessName("", 0);
  assert.match(name, LEGAL);
  assert.notEqual(name, "default");
});

test("a name that scrubs to nothing does not become empty", () => {
  const name = harnessName("///", Number.NaN);
  assert.match(name, LEGAL);
});
