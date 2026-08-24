/**
 * Pairing failures have to explain themselves (#661).
 *
 * The desktop dialog showed exactly two words — "fetch failed" — because Node's
 * `fetch` throws a bare `TypeError` and hides the real reason in `err.cause`,
 * which both the pairing handler and the CLI dropped. Pairing is the one screen
 * with no logs behind it, so the message it renders is all the operator gets:
 * naming the API that failed instead of what their machine did is unactionable.
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import { describeFetchError } from "../src/api";

const URL_ = "https://qagent.example.com/api/agent/devices/redeem";

test("the underlying cause and its code survive", () => {
  const cause = Object.assign(new Error("getaddrinfo ENOTFOUND qagent.example.com"), {
    code: "ENOTFOUND",
  });
  const out = describeFetchError(Object.assign(new TypeError("fetch failed"), { cause }), URL_);

  assert.match(out, /fetch failed/);
  assert.match(out, /ENOTFOUND/);
  // The URL matters as much as the reason: the usual cause is the wrong one.
  assert.ok(out.includes(URL_), "the attempted URL must be in the message");
  // And it says what to do about it, in the operator's terms.
  assert.match(out, /does not resolve/);
});

test("a nested cause chain is walked, not just the first level", () => {
  const inner = Object.assign(new Error("unable to verify the first certificate"), {
    code: "UNABLE_TO_VERIFY_LEAF_SIGNATURE",
  });
  const middle = Object.assign(new Error("write EPROTO"), { cause: inner });
  const out = describeFetchError(
    Object.assign(new TypeError("fetch failed"), { cause: middle }),
    URL_,
  );

  assert.match(out, /write EPROTO/);
  assert.match(out, /unable to verify the first certificate/);
  // The hint comes from the code that explains it — a corporate proxy
  // intercepting TLS is the classic "it worked on my other machine" cause, and
  // it is invisible without this.
  assert.match(out, /intercepting TLS/);
});

test("an error with no cause still names what failed and where", () => {
  const out = describeFetchError(new TypeError("fetch failed"), URL_);
  assert.match(out, /fetch failed/);
  assert.ok(out.includes(URL_));
  assert.doesNotMatch(out, /cause:/, "no cause => do not print an empty cause line");
});

test("a non-Error is not swallowed", () => {
  assert.match(describeFetchError("boom", URL_), /boom/);
});
