/**
 * Agent half of the shared fixtures-rewrite parity test (#557).
 *
 * The server (`api/app/services/playwright_runner.py` — `fixture_targets` /
 * `_skip_fixture_rewrite`) and this module are two independent ports of the same
 * rule, and in #557 they had already drifted on `*.config.ts` at depth: the same
 * project passed on server-target and failed collection on local-agent target,
 * pointing at a config file nobody edited.
 *
 * Both sides now build the SAME declared tree from
 * `contracts/fixture-rewrite-tree.json` and assert the identical rewritten-path
 * set with the identical specifier per path. The server's half lives in
 * `api/tests/test_fixture_rewrite_parity.py`. Change the rule on one side only
 * and one of the two gates goes red.
 */

import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { applyFixtures, fixtureTargets, fixturesSpecifier } from "../src/playwrightConfig";

const IMPORT_LINE = "import { test, expect } from '@playwright/test';\n";

type Contract = { files: string[]; rewritten: Record<string, string> };

/** The shared declared tree, found by walking up from this file to the repo root.
 * Walking (rather than a fixed `../../..`) keeps it working from both `test/` and
 * the compiled `dist/test/`. */
function contract(): Contract {
  let dir = __dirname;
  for (let i = 0; i < 8; i += 1) {
    const candidate = path.join(dir, "contracts", "fixture-rewrite-tree.json");
    if (fs.existsSync(candidate)) return JSON.parse(fs.readFileSync(candidate, "utf-8"));
    dir = path.dirname(dir);
  }
  throw new Error("contracts/fixture-rewrite-tree.json not found above " + __dirname);
}

/** Materialize the declared tree; every file carries a Playwright import, so the
 * "was it rewritten" answer is purely the skip rule's. */
function declaredTree(): { dir: string; contract: Contract } {
  const c = contract();
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "qa-parity-"));
  for (const relative of c.files) {
    const p = path.join(dir, relative);
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.writeFileSync(p, IMPORT_LINE, "utf-8");
  }
  return { dir, contract: c };
}

test("fixtureTargets matches the shared contract's rewritten set", () => {
  const { dir, contract: c } = declaredTree();
  assert.deepEqual(fixtureTargets(dir).sort(), Object.keys(c.rewritten).sort());
  fs.rmSync(dir, { recursive: true, force: true });
});

test("each path gets the same specifier the server computes", () => {
  const { dir, contract: c } = declaredTree();
  const computed: Record<string, string> = {};
  for (const target of fixtureTargets(dir)) computed[target] = fixturesSpecifier(target);
  assert.deepEqual(computed, c.rewritten);
  fs.rmSync(dir, { recursive: true, force: true });
});

test("applyFixtures rewrites exactly the contract's files on disk", () => {
  const { dir, contract: c } = declaredTree();
  const rewritten = applyFixtures(dir, path.join(dir, "sessionStorage.json"), false).sort();
  assert.deepEqual(rewritten, Object.keys(c.rewritten).sort());

  for (const relative of c.files) {
    const text = fs.readFileSync(path.join(dir, relative), "utf-8");
    const expected = c.rewritten[relative];
    if (expected === undefined) {
      // Skipped files keep the real package. (The root fixtures.ts is rewritten
      // wholesale by the generator and legitimately imports it too.)
      assert.ok(text.includes("'@playwright/test'"), `${relative} must keep the real package`);
    } else {
      assert.ok(text.includes(`'${expected}'`), `${relative} -> ${expected}`);
      assert.ok(!text.includes("'@playwright/test'"), `${relative} must not keep the real package`);
    }
  }
  fs.rmSync(dir, { recursive: true, force: true });
});

test("the load-bearing pair stays split: root fixtures.ts skipped, nested rewritten", () => {
  const { dir } = declaredTree();
  const targets = fixtureTargets(dir);
  assert.ok(!targets.includes("fixtures.ts"));
  assert.ok(targets.includes("fixtures/authenticated.ts"));
  assert.equal(fixturesSpecifier("fixtures/authenticated.ts"), "../fixtures");
  fs.rmSync(dir, { recursive: true, force: true });
});

test("configs are spared at every depth (the #557 divergence)", () => {
  const { dir } = declaredTree();
  const targets = fixtureTargets(dir);
  assert.deepEqual(targets.filter((t) => t.endsWith(".config.ts")), []);
  fs.rmSync(dir, { recursive: true, force: true });
});

test("output dirs are never walked, at any depth", () => {
  const { dir } = declaredTree();
  const targets = fixtureTargets(dir);
  for (const noise of ["node_modules", "test-results", "playwright-report", "blob-report", ".git"]) {
    assert.deepEqual(targets.filter((t) => t.startsWith(`${noise}/`)), [], noise);
  }
  assert.ok(!targets.includes("pages/node_modules/vendored.ts"));
  fs.rmSync(dir, { recursive: true, force: true });
});
