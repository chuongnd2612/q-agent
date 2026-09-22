/**
 * Tests for the `playwright-cli` capability preflight (#899).
 *
 * The bug this covers: the old guard was `fs.existsSync(playwrightCli())`, and
 * `cli.js` is also the entry for `playwright test`/`install`, so it exists in
 * EVERY version — including the previously pinned 1.61.1, which has no `cli
 * find`. The guard passed and the session died mid-run on `Unknown command:
 * find`. So the interesting assertions here are the NEGATIVE controls:
 *
 *  1. the real, captured `cli --help` listing of Playwright 1.61.1 must be
 *     REJECTED (this is the version the agent used to ship);
 *  2. a stub `cli.js` that prints that listing must be rejected end-to-end,
 *     through the actual spawn path — not just the pure parser;
 *  3. a missing `cli.js` must be rejected with the "not bundled" message.
 *
 * The positive control runs against the Playwright actually installed in this
 * worktree, so the test fails if the dependency is ever downgraded below the
 * version that ships `find`.
 */

import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { playwrightCli } from "../src/paths";
import {
  PLAYWRIGHT_FIND_MIN_VERSION,
  helpAdvertisesFind,
  playwrightCliFindAvailable,
} from "../src/playwrightCliCapability";

/** Verbatim excerpt of `node cli.js cli --help` from a real playwright 1.61.1.
 * Note it DOES have `snapshot`, `eval`, `resize`, `state-load` — everything the
 * agents use except `find`, which is exactly why a version check on "does the
 * `cli` subcommand exist" says yes and is still wrong. */
const HELP_1_61_1 = `playwright-cli - run playwright mcp commands from terminal

Usage: playwright-cli <command> [args] [options]
Usage: playwright-cli -s=<session> <command> [args] [options]

Core:
  open [url]                  open the browser
  attach [name]               attach to a running playwright browser
  close                       close the browser
  goto <url>                  navigate to a url
  click <target> [button]     perform click on a web page
  fill <target> <text>        fill text into editable element
  snapshot [target]           capture page snapshot to obtain element ref
  eval <func> [target]        evaluate javascript expression on page or element
  resize <w> <h>              resize the browser window

Storage:
  state-load <filename>       loads browser storage (authentication) state from a file
  state-save [filename]       saves the current storage (authentication) state to a file
`;

/** Verbatim excerpt from a real playwright 1.63.0 — `find` is listed. */
const HELP_1_63_0 = `playwright-cli - run playwright mcp commands from terminal

Core:
  open [url]                  open the browser
  find [text]                 search the page snapshot for text or a regexp, returning matching nodes with surrounding context (like search snippets)
  snapshot [target]           capture page snapshot to obtain element ref
`;

test("helpAdvertisesFind rejects the real 1.61.1 listing and accepts 1.63.0", () => {
  assert.equal(helpAdvertisesFind(HELP_1_61_1), false);
  assert.equal(helpAdvertisesFind(HELP_1_63_0), true);
});

test("helpAdvertisesFind is not fooled by the word 'find' inside a description", () => {
  // `find` must appear as a COMMAND entry (indented, first token on the line),
  // not merely somewhere in prose — otherwise any help text mentioning "find"
  // would satisfy the guard.
  assert.equal(
    helpAdvertisesFind("Core:\n  snapshot [target]   use this to find elements on the page\n"),
    false
  );
  assert.equal(helpAdvertisesFind("Core:\n  find [text]  search the snapshot\n"), true);
});

test("the Playwright bundled with this agent can `cli find`", async () => {
  const res = await playwrightCliFindAvailable();
  assert.equal(
    res.ok,
    true,
    `bundled playwright must support \`cli find\` (needs >= ${PLAYWRIGHT_FIND_MIN_VERSION}): ${res.error || ""}`
  );
  assert.equal(res.error, undefined);
});

test("a too-old CLI is rejected end-to-end, through the real spawn", async () => {
  // Negative control for the whole guard, not just the parser: a stub `cli.js`
  // that answers `cli --help` exactly the way 1.61.1 does, laid out like the
  // real package (with a package.json next to it so the message can name the
  // version it rejected).
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pw-old-"));
  fs.writeFileSync(path.join(dir, "package.json"), JSON.stringify({ name: "playwright", version: "1.61.1" }));
  fs.writeFileSync(
    path.join(dir, "cli.js"),
    `process.stdout.write(${JSON.stringify(HELP_1_61_1)});\n`
  );
  const cliJs = path.join(dir, "cli.js");
  try {
    const res = await playwrightCliFindAvailable(cliJs);
    assert.equal(res.ok, false);
    assert.match(res.error || "", /playwright-cli unavailable/);
    assert.match(res.error || "", /no `cli find` command/);
    // The message must name the version it found AND the version it needs, or
    // the operator has no way to tell "update the agent" from "install it".
    assert.match(res.error || "", /1\.61\.1/);
    assert.ok((res.error || "").includes(PLAYWRIGHT_FIND_MIN_VERSION));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("a missing CLI is rejected with the 'not bundled' message", async () => {
  const res = await playwrightCliFindAvailable(path.join(os.tmpdir(), "definitely-absent-playwright-cli.js"));
  assert.equal(res.ok, false);
  assert.match(res.error || "", /not bundled with this agent build/);
});

test("playwrightCli() resolves to a file that exists", () => {
  // Guards the default argument of the preflight: if this ever stops resolving,
  // the capability probe degrades to the "not bundled" message rather than
  // silently passing — the assertion documents that dependency.
  assert.equal(fs.existsSync(playwrightCli()), true);
});
