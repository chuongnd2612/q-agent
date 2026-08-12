/**
 * Unit tests for the injected-fixtures port (`src/playwrightConfig.ts`).
 * Mirrors the server's `test_fixtures_ts_contents` / `test_apply_fixtures_always_injects`
 * (api/tests/test_execution.py) so the agent's DOM capture stays in lock-step
 * with the server runner.
 */

import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import {
  applyFixtures,
  fixtureTargets,
  fixturesSpecifier,
  fixturesTs,
  writeConfig,
} from "../src/playwrightConfig";

test("fixturesTs always wires DOM capture; sessionStorage replay is gated", () => {
  const sessionFile = "/tmp/sessionStorage.json";

  const replay = fixturesTs(sessionFile, true);
  assert.ok(replay.includes("export const test"));
  assert.ok(replay.includes("testInfo.attach('qagent-dom-raw'"));
  assert.ok(replay.includes("testInfo.attach('qagent-dom-distilled'"));
  assert.ok(replay.includes(JSON.stringify(sessionFile)));
  assert.ok(replay.includes("addInitScript"), "replay=true injects the session init script");

  const noReplay = fixturesTs(sessionFile, false);
  assert.ok(noReplay.includes("testInfo.attach('qagent-dom-distilled'"));
  assert.ok(!noReplay.includes("addInitScript"), "replay=false drops the session init script");
});

test("fixturesTs captures console + network on every test (#456)", () => {
  const fx = fixturesTs("/tmp/sessionStorage.json", false);
  assert.ok(fx.includes("testInfo.attach('qagent-network'"));
  assert.ok(fx.includes("testInfo.attach('qagent-console'"));
  assert.ok(fx.includes("page.on('response'"), "network responses are collected");
  assert.ok(fx.includes("page.on('console'"), "console messages are collected");
});

test("writeConfig: screenshot always-on; video honors the setting (#456)", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "qa-cfg-"));
  const read = () => fs.readFileSync(path.join(dir, "playwright.config.ts"), "utf-8");

  // Screenshot is always 'on' now (captured pass or fail).
  writeConfig(dir, 1, true);
  assert.ok(read().includes("screenshot: 'on'"));

  // Capture-video ON → video 'on' regardless of pass/fail; trace stays failure-only.
  writeConfig(dir, 1, true, "", "", { captureVideo: true });
  assert.ok(read().includes("video: 'on'"));
  assert.ok(read().includes("trace: 'retain-on-failure'"));

  // Capture-video OFF → no video.
  writeConfig(dir, 1, true, "", "", { captureVideo: false });
  assert.ok(read().includes("video: 'off'"));

  // Intermediate heal attempts (heavyEvidence=false) never record video even when enabled.
  writeConfig(dir, 1, true, "", "", { captureVideo: true, heavyEvidence: false });
  assert.ok(read().includes("video: 'off'"));
  assert.ok(read().includes("trace: 'off'"));

  fs.rmSync(dir, { recursive: true, force: true });
});

test("applyFixtures always rewrites imports to './fixtures' + writes fixtures.ts", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "qa-fx-"));
  const specName = "1428-TC-01.spec.ts";
  const specPath = path.join(dir, specName);
  const original =
    "import { test, expect } from '@playwright/test';\n" +
    "test('x', async ({ page }) => { await page.goto('/'); });\n";
  fs.writeFileSync(specPath, original, "utf-8");
  const sessionFile = path.join(dir, "sessionStorage.json");

  // Even without session replay, DOM capture means fixtures are injected.
  applyFixtures(dir, sessionFile, false);
  const rewritten = fs.readFileSync(specPath, "utf-8");
  assert.ok(rewritten.includes("'./fixtures'"));
  assert.ok(!rewritten.includes("'@playwright/test'"));
  const fixtures = fs.readFileSync(path.join(dir, "fixtures.ts"), "utf-8");
  assert.ok(fixtures.includes("qagent-dom-distilled"));
  assert.ok(!fixtures.includes("addInitScript"));

  // With replay enabled, the init script is added; specs stay pointed at './fixtures'.
  applyFixtures(dir, sessionFile, true);
  assert.ok(fs.readFileSync(specPath, "utf-8").includes("'./fixtures'"));
  assert.ok(fs.readFileSync(path.join(dir, "fixtures.ts"), "utf-8").includes("addInitScript"));

  fs.rmSync(dir, { recursive: true, force: true });
});

// ── Depth-aware fixtures (#541) ────────────────────────────────────────────
// A layered project puts specs at `tests/<TICKET>/x.spec.ts` and page objects at
// `pages/Foo.ts`, so a flat `'./fixtures'` rewrite resolves to nothing and every
// spec fails collection. These mirror the server's `_apply_fixtures` change —
// divergence means specs pass on the server and fail on the device.

test("fixturesSpecifier: one specifier per depth", () => {
  assert.equal(fixturesSpecifier("1428-TC-01.spec.ts"), "./fixtures");
  assert.equal(fixturesSpecifier("pages/LoginPage.ts"), "../fixtures");
  assert.equal(fixturesSpecifier("tests/SUR-1428/SUR-1428-TC-01.spec.ts"), "../../fixtures");
  assert.equal(fixturesSpecifier("a/b/c/d.ts"), "../../../fixtures");
  // Windows separators resolve the same way.
  assert.equal(fixturesSpecifier("tests\\SUR-1428\\x.spec.ts"), "../../fixtures");
});

test("fixtureTargets: globs **/*.ts, skips the shim, config, node_modules and .d.ts", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "qa-tgt-"));
  const write = (rel: string, body = "// x\n") => {
    const p = path.join(dir, rel);
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.writeFileSync(p, body, "utf-8");
  };
  write("fixtures.ts");
  write("playwright.config.ts");
  write("types.d.ts");
  write("pages/LoginPage.ts");
  write("components/Nav.ts");
  write("tests/SUR-1428/SUR-1428-TC-01.spec.ts");
  write("node_modules/@q-agent/playwright-base/dist/index.ts");
  write("test-results/leftover.ts");
  // A nested `fixtures/` LIBRARY dir is a real project directory and must be
  // rewritten — only the injected ROOT `fixtures.ts` is exempt.
  write("fixtures/authenticated.ts");

  const targets = fixtureTargets(dir).sort();
  assert.deepEqual(targets, [
    "components/Nav.ts",
    "fixtures/authenticated.ts",
    "pages/LoginPage.ts",
    "tests/SUR-1428/SUR-1428-TC-01.spec.ts",
  ]);

  fs.rmSync(dir, { recursive: true, force: true });
});

test("applyFixtures rewrites each file to its own depth and never touches the config", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "qa-fxd-"));
  const write = (rel: string, body: string) => {
    const p = path.join(dir, rel);
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.writeFileSync(p, body, "utf-8");
  };
  const read = (rel: string) => fs.readFileSync(path.join(dir, rel), "utf-8");

  write("playwright.config.ts", "import { defineConfig } from '@playwright/test';\n");
  write("pages/LoginPage.ts", "import { Page } from '@playwright/test';\nexport class LoginPage {}\n");
  write(
    "tests/SUR-1428/SUR-1428-TC-01.spec.ts",
    'import { test, expect } from "@playwright/test";\nimport { LoginPage } from "../../pages/LoginPage";\n'
  );
  write("utils/wait.ts", "// no playwright import here\n");

  const rewritten = applyFixtures(dir, path.join(dir, "sessionStorage.json"), false).sort();
  assert.deepEqual(rewritten, ["pages/LoginPage.ts", "tests/SUR-1428/SUR-1428-TC-01.spec.ts"]);

  assert.ok(read("pages/LoginPage.ts").includes("'../fixtures'"));
  assert.ok(read("tests/SUR-1428/SUR-1428-TC-01.spec.ts").includes('"../../fixtures"'));
  // The relative page-object import is untouched — only the module specifier for
  // Playwright itself is rewritten.
  assert.ok(read("tests/SUR-1428/SUR-1428-TC-01.spec.ts").includes('"../../pages/LoginPage"'));
  // The config MUST keep the real package: fixtures.ts exports no defineConfig.
  assert.ok(read("playwright.config.ts").includes("'@playwright/test'"));
  assert.ok(fs.existsSync(path.join(dir, "fixtures.ts")));
  // No stray '@playwright/test' left in any rewritten file.
  assert.ok(!read("pages/LoginPage.ts").includes("@playwright/test"));

  fs.rmSync(dir, { recursive: true, force: true });
});
