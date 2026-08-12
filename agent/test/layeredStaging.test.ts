/**
 * The one thing that actually matters about a layered job (#541): after staging,
 * **the real Playwright collects the nested spec**.
 *
 * Every failure mode this slice guards against surfaces as a collection error —
 * a flattened tree, a wrong-depth `fixtures` specifier, an unresolvable
 * `@q-agent/playwright-base`. So this test stages a bundle exactly as
 * `runner.stageJobTree` does and shells out to `playwright test --list`, which
 * type-checks and resolves every import without needing a browser.
 */

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { agentNodeModules, playwrightCli } from "../src/paths";
import { applyFixtures, writeConfig } from "../src/playwrightConfig";
import { installBaseFramework, materializeProject, writeProjectFile } from "../src/projectBundle";

const SPEC_REL = "tests/SUR-1428/SUR-1428-TC-01.spec.ts";

test("a staged layered bundle collects under the real Playwright", () => {
  let cli: string;
  let nodeModules: string;
  try {
    cli = playwrightCli();
    nodeModules = agentNodeModules();
  } catch {
    // A bundle without Playwright resolvable can't run this; the rest of the
    // suite still covers the staging logic itself.
    return;
  }

  const workDir = fs.mkdtempSync(path.join(os.tmpdir(), "qagent-staged-"));
  try {
    // 1) The bundle the server ships (tests/** excluded, per bundle_for_agent).
    const staged = materializeProject(workDir, {
      baseVersion: "1",
      files: [
        {
          path: "pages/LoginPage.ts",
          code:
            "import { Page } from '@playwright/test';\n" +
            "export class LoginPage {\n" +
            "  constructor(private page: Page) {}\n" +
            "  title(): string { return 'login'; }\n" +
            "}\n",
        },
        { path: "utils/wait.ts", code: "export const nudge = (): number => 1;\n" },
        { path: "package.json", code: '{"name":"staged","private":true}\n' },
      ],
    });
    assert.equal(staged.files, 3);
    assert.equal(installBaseFramework(workDir), "vendored");

    // 2) This run's spec, at its project-relative nested path — importing a page
    //    object two levels up AND the base framework.
    writeProjectFile(
      workDir,
      SPEC_REL,
      "import { test, expect } from '@playwright/test';\n" +
        "import { LoginPage } from '../../pages/LoginPage';\n" +
        "import { nudge } from '../../utils/wait';\n" +
        "import { BASE_VERSION } from '@q-agent/playwright-base';\n" +
        "test('layered spec collects', async ({ page }) => {\n" +
        "  expect(new LoginPage(page).title()).toBe('login');\n" +
        "  expect(nudge() + BASE_VERSION.length).toBeGreaterThan(0);\n" +
        "});\n"
    );

    // 3) Config + depth-aware fixtures, exactly as processJob does.
    writeConfig(workDir, 1, true, "", "");
    const rewritten = applyFixtures(workDir, path.join(workDir, "sessionStorage.json"), false).sort();
    assert.deepEqual(rewritten, ["pages/LoginPage.ts", SPEC_REL]);
    assert.ok(fs.readFileSync(path.join(workDir, SPEC_REL), "utf-8").includes("'../../fixtures'"));
    assert.ok(fs.readFileSync(path.join(workDir, "pages/LoginPage.ts"), "utf-8").includes("'../fixtures'"));

    // 4) The proof: Playwright resolves every import and finds the test.
    const out = execFileSync(process.execPath, [cli, "test", "--list", SPEC_REL], {
      cwd: workDir,
      env: { ...process.env, NODE_PATH: nodeModules },
      encoding: "utf-8",
    });
    assert.match(out, /layered spec collects/);
    assert.match(out, /Total: 1 test/);
  } finally {
    fs.rmSync(workDir, { recursive: true, force: true });
  }
});
