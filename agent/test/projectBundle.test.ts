/**
 * Unit tests for materializing a shipped automation project (`src/projectBundle.ts`, #541).
 *
 * The agent is stateless — it gets one shot at writing the tree into a temp
 * workdir. If the nesting is wrong, every relative import fails collection and
 * the whole run reads as a mass test failure, so the shape of what lands on disk
 * is worth asserting precisely.
 */

import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import {
  BUNDLE_MAX_BYTES,
  installBaseFramework,
  isSafeRelativePath,
  materializeProject,
  writeProjectFile,
} from "../src/projectBundle";

const tmp = (prefix: string) => fs.mkdtempSync(path.join(os.tmpdir(), prefix));

test("materializeProject writes a correctly nested tree", () => {
  const dir = tmp("qa-bundle-");
  const result = materializeProject(dir, {
    baseVersion: "1",
    files: [
      { path: "pages/LoginPage.ts", code: "export class LoginPage {}\n" },
      { path: "components/nav/TopNav.ts", code: "export class TopNav {}\n" },
      { path: "package.json", code: '{"name":"x"}\n' },
      { path: "data/users.json", code: "[]\n" },
    ],
  });

  assert.equal(result.files, 4);
  assert.equal(result.skipped.length, 0);
  assert.ok(result.bytes > 0);
  assert.equal(
    fs.readFileSync(path.join(dir, "pages", "LoginPage.ts"), "utf-8"),
    "export class LoginPage {}\n"
  );
  // Two levels deep: the directories must be created recursively.
  assert.ok(fs.existsSync(path.join(dir, "components", "nav", "TopNav.ts")));
  assert.ok(fs.existsSync(path.join(dir, "data", "users.json")));

  fs.rmSync(dir, { recursive: true, force: true });
});

test("materializeProject refuses paths that escape the workdir", () => {
  const dir = tmp("qa-bundle-esc-");
  const result = materializeProject(dir, {
    files: [
      { path: "../escaped.ts", code: "x" },
      { path: "pages/../../escaped2.ts", code: "x" },
      { path: "/abs.ts", code: "x" },
      { path: "C:\\windows\\evil.ts", code: "x" },
      { path: "pages/Ok.ts", code: "ok" },
    ],
  });

  assert.equal(result.files, 1, "only the safe path is written");
  assert.equal(result.skipped.length, 4);
  assert.ok(fs.existsSync(path.join(dir, "pages", "Ok.ts")));
  assert.ok(!fs.existsSync(path.join(path.dirname(dir), "escaped.ts")));

  fs.rmSync(dir, { recursive: true, force: true });
});

test("isSafeRelativePath", () => {
  assert.ok(isSafeRelativePath("pages/LoginPage.ts"));
  assert.ok(isSafeRelativePath("tests/SUR-1428/SUR-1428-TC-01.spec.ts"));
  assert.ok(isSafeRelativePath("tests\\SUR-1428\\x.spec.ts"), "windows separators are normalized");
  assert.ok(!isSafeRelativePath(""));
  assert.ok(!isSafeRelativePath("../x.ts"));
  assert.ok(!isSafeRelativePath("a/../../x.ts"));
  assert.ok(!isSafeRelativePath("./x.ts"));
  assert.ok(!isSafeRelativePath("/etc/passwd"));
  assert.ok(!isSafeRelativePath("D:/x.ts"));
  assert.ok(!isSafeRelativePath("a//b.ts"));
});

test("materializeProject fails fast over the size cap, writing nothing", () => {
  const dir = tmp("qa-bundle-cap-");
  const result = materializeProject(dir, {
    files: [{ path: "big.ts", code: "x".repeat(BUNDLE_MAX_BYTES + 1) }],
  });

  assert.equal(result.overCap, true);
  assert.equal(result.files, 0);
  assert.ok(result.bytes > BUNDLE_MAX_BYTES, "the measured size is reported for the log");
  assert.deepEqual(fs.readdirSync(dir), [], "nothing is staged from an over-cap bundle");

  fs.rmSync(dir, { recursive: true, force: true });
});

test("materializeProject on a legacy (absent) bundle is a no-op", () => {
  const dir = tmp("qa-bundle-none-");
  const result = materializeProject(dir, undefined);
  assert.deepEqual(result, { files: 0, bytes: 0, skipped: [] });
  assert.deepEqual(fs.readdirSync(dir), []);
  fs.rmSync(dir, { recursive: true, force: true });
});

test("writeProjectFile creates parent dirs and reports refusal", () => {
  const dir = tmp("qa-wpf-");
  assert.equal(writeProjectFile(dir, "a/b/c.ts", "hi"), true);
  assert.equal(fs.readFileSync(path.join(dir, "a", "b", "c.ts"), "utf-8"), "hi");
  assert.equal(writeProjectFile(dir, "../nope.ts", "hi"), false);
  fs.rmSync(dir, { recursive: true, force: true });
});

test("installBaseFramework makes @q-agent/playwright-base resolvable offline", () => {
  const dir = tmp("qa-base-");
  const outcome = installBaseFramework(dir);
  // The vendored copy is committed at agent/vendor/playwright-base (regenerate
  // with `npm run vendor:base`), so this must not need the network.
  assert.equal(outcome, "vendored");
  const manifest = path.join(dir, "node_modules", "@q-agent", "playwright-base", "package.json");
  assert.ok(fs.existsSync(manifest));
  assert.equal(JSON.parse(fs.readFileSync(manifest, "utf-8")).name, "@q-agent/playwright-base");
  // The package's `main` entry must actually be there, or a spec importing it
  // resolves the manifest and then fails.
  const main = JSON.parse(fs.readFileSync(manifest, "utf-8")).main;
  assert.ok(fs.existsSync(path.join(path.dirname(manifest), main)));

  // Idempotent: a second call (e.g. a heal re-attempt) doesn't re-copy.
  assert.equal(installBaseFramework(dir), "already-present");

  fs.rmSync(dir, { recursive: true, force: true });
});
