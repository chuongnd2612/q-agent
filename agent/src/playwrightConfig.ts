/**
 * Port of `api/app/services/playwright_runner.py`'s config template
 * (`_PLAYWRIGHT_CONFIG_TEMPLATE` + `_write_config`) and the injected-fixtures
 * shim (`_fixtures_ts` + `_apply_fixtures`), reproduced faithfully so a spec
 * that runs on the server and one that runs via the Local Agent behave
 * identically — including always-on DOM capture (raw + distilled).
 */

import * as fs from "fs";
import * as path from "path";

const PLAYWRIGHT_CONFIG_TEMPLATE = `import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: '.',
  timeout: __TIMEOUT__,
  workers: __WORKERS__,
  reporter: __REPORTERS__,
  use: {
    headless: __HEADLESS__,
    screenshot: 'on',
    video: '__VIDEO__',
    trace: '__TRACE__',
__EXTRA_USE__  },
});
`;

/** Optional heal-tuning knobs for {@link writeConfig} — port of the server's
 * `_write_config` extras (#398). Omitted for normal runs. */
export interface ConfigOptions {
  /** Per-test timeout (ms). Default 30000; heal re-runs pass a shorter value. */
  testTimeoutMs?: number;
  /** When set, injected as `use.actionTimeout` / `use.navigationTimeout` so a
   * broken action fails fast (heal re-runs only). */
  actionTimeoutMs?: number;
  /** When false, `video`/`trace` are `'off'` — intermediate heal attempts. */
  heavyEvidence?: boolean;
  /** Honors the "Capture video" setting (#456). When true (and `heavyEvidence`),
   * video is `'on'` so every case is recorded regardless of pass/fail; else `'off'`. */
  captureVideo?: boolean;
  /** Absolute path to the vendored live reporter (`live_reporter.cjs`). When set,
   * it's added alongside the JSON reporter so per-test results stream out during
   * the run (execution only; heal/regen don't need it). */
  liveReporterPath?: string;
}

/**
 * (Re)write `playwright.config.ts` into `specDir` — same shape as the
 * server's `_write_config`.
 *
 * @param specDir The job's local workdir the config is written into.
 * @param workers Parallel worker count.
 * @param headless Whether the browser runs headless.
 * @param baseUrl When non-empty, injected as `use.baseURL`.
 * @param storageState When non-empty, an absolute path injected as `use.storageState`.
 * @param opts Heal-tuning knobs (timeouts + evidence); defaults match a normal run.
 */
export function writeConfig(
  specDir: string,
  workers: number,
  headless: boolean,
  baseUrl = "",
  storageState = "",
  opts: ConfigOptions = {}
): void {
  const { testTimeoutMs = 30000, actionTimeoutMs, heavyEvidence = true, captureVideo = false, liveReporterPath } = opts;
  // JSON reporter (drives report.json for evidence + reconcile) + optionally the
  // live reporter (streams per-test results). Paths JSON-escaped for Windows.
  const reporters = ["['json', { outputFile: 'report.json' }]"];
  if (liveReporterPath) reporters.push(`[${JSON.stringify(liveReporterPath)}]`);
  const reportersStr = `[${reporters.join(", ")}]`;
  const extraLines: string[] = [];
  if (baseUrl) extraLines.push(`    baseURL: ${JSON.stringify(baseUrl)},`);
  if (storageState) extraLines.push(`    storageState: ${JSON.stringify(storageState)},`);
  if (actionTimeoutMs != null) {
    extraLines.push(`    actionTimeout: ${Math.trunc(actionTimeoutMs)},`);
    extraLines.push(`    navigationTimeout: ${Math.trunc(actionTimeoutMs)},`);
  }
  const extraUse = extraLines.length ? extraLines.join("\n") + "\n" : "";
  const trace = heavyEvidence ? "retain-on-failure" : "off";
  // Video follows the "Capture video" setting (always-on when enabled), not the
  // failure-only trace policy — but intermediate heal attempts still skip it.
  const video = captureVideo && heavyEvidence ? "on" : "off";
  const content = PLAYWRIGHT_CONFIG_TEMPLATE.replace("__TIMEOUT__", String(Math.trunc(testTimeoutMs)))
    .replace("__WORKERS__", String(workers))
    .replace("__HEADLESS__", headless ? "true" : "false")
    .replace("__REPORTERS__", reportersStr)
    .replace("__VIDEO__", video)
    .replace("__TRACE__", trace)
    .replace("__EXTRA_USE__", extraUse);
  fs.writeFileSync(path.join(specDir, "playwright.config.ts"), content, "utf-8");
}

/**
 * TypeScript for a generated `fixtures.ts` that captures the page DOM after
 * every test, and optionally replays a captured sessionStorage — port of
 * `_fixtures_ts`.
 *
 * The module re-exports Playwright's `test` extended with:
 * - an `{auto: true}` fixture that, after each test, best-effort attaches the
 *   live page's raw HTML (`qagent-dom-raw`) and a distilled inventory of the
 *   page's interactable elements (`qagent-dom-distilled`) via `testInfo.attach`,
 *   so self-heal can ground on the real DOM. Wrapped in try/catch so it never
 *   fails a test.
 * - (only when `replaySession`) a `context` override that restores the captured
 *   `sessionStorage` (MSAL/SPA tokens) for the current origin before app code runs.
 *
 * @param sessionFile Absolute path to the captured `sessionStorage.json`;
 *   embedded as a JSON-encoded string literal, read at runtime only when
 *   `replaySession` is set.
 * @param replaySession Whether to inject the sessionStorage-replay `context`
 *   override (DOM capture is always injected regardless).
 */
export function fixturesTs(sessionFile: string, replaySession: boolean, captureRaw = true): string {
  const contextFixture = replaySession
    ? "  context: async ({ context }, use) => {\n" +
      "    await context.addInitScript((sessions: Record<string, Record<string, string>>) => {\n" +
      "      try {\n" +
      "        const entries = sessions[location.origin];\n" +
      "        if (entries) for (const k in entries) window.sessionStorage.setItem(k, entries[k]);\n" +
      "      } catch {}\n" +
      "    }, SESSIONS);\n" +
      "    await use(context);\n" +
      "  },\n"
    : "";
  const rawBlock = captureRaw
    ? "    try {\n" +
      "      const raw = await page.content();\n" +
      "      const rawPath = testInfo.outputPath('qagent-dom-raw.html');\n" +
      "      fs.writeFileSync(rawPath, raw, 'utf-8');\n" +
      "      await testInfo.attach('qagent-dom-raw', { path: rawPath, contentType: 'text/html' });\n" +
      "    } catch {}\n"
    : "";
  return (
    "import { test as base, expect } from '@playwright/test';\n" +
    "import * as fs from 'fs';\n" +
    "\n" +
    `const SESSION_FILE = ${JSON.stringify(sessionFile)};\n` +
    "let SESSIONS: Record<string, Record<string, string>> = {};\n" +
    "try { SESSIONS = JSON.parse(fs.readFileSync(SESSION_FILE, 'utf-8')); } catch {}\n" +
    "\n" +
    "export const test = base.extend<{ _domCapture: void }>({\n" +
    contextFixture +
    "  // Always-on DOM capture: after each test, snapshot the live page so the\n" +
    "  // runner (evidence) and self-heal loop (real selectors) can use it.\n" +
    "  _domCapture: [async ({ page }, use, testInfo) => {\n" +
    "    // Console + network capture (#456): collect for the whole test, pass or\n" +
    "    // fail. Listeners registered BEFORE use() so nothing is missed; the JSON\n" +
    "    // is attached after and parsed into console_logs/network_logs columns.\n" +
    "    const __net: any[] = []; const __con: any[] = []; const __started = new Map();\n" +
    "    page.on('request', (r) => { try { __started.set(r, Date.now()); } catch {} });\n" +
    "    page.on('response', (resp) => { try { if (__net.length < 300) { const req = resp.request(); const t0 = __started.get(req); __net.push({ method: req.method(), url: req.url(), status: resp.status(), durationMs: t0 ? Date.now() - t0 : 0 }); } } catch {} });\n" +
    "    page.on('requestfailed', (r) => { try { if (__net.length < 300) { const t0 = __started.get(r); __net.push({ method: r.method(), url: r.url(), status: 0, durationMs: t0 ? Date.now() - t0 : 0, failed: true }); } } catch {} });\n" +
    "    page.on('console', (msg) => { try { if (__con.length < 500) __con.push({ level: msg.type(), text: String(msg.text()).slice(0, 2000) }); } catch {} });\n" +
    "    page.on('pageerror', (err) => { try { if (__con.length < 500) __con.push({ level: 'error', text: String((err && err.message) || err).slice(0, 2000) }); } catch {} });\n" +
    "    await use();\n" +
    "    // Distilled inventory FIRST (what self-heal needs) — retry once after a\n" +
    "    // short settle so a transiently-busy failure page still yields real\n" +
    "    // elements instead of leaving the fixer with no DOM (#398).\n" +
    "    const runDistill = () => page.evaluate(() => {\n" +
    "      const SEL = 'a,button,input,select,textarea,[role],[data-testid],[data-test],[id]';\n" +
    "      const elements = Array.from(document.querySelectorAll(SEL)).slice(0, 400).map((node) => {\n" +
    "        const el = node as HTMLElement;\n" +
    "        const text = (el.innerText || '').trim().slice(0, 80);\n" +
    "        return {\n" +
    "          tag: el.tagName.toLowerCase(),\n" +
    "          role: el.getAttribute('role') || undefined,\n" +
    "          testId: el.getAttribute('data-testid') || el.getAttribute('data-test') || undefined,\n" +
    "          id: el.id || undefined,\n" +
    "          name: el.getAttribute('name') || undefined,\n" +
    "          text: text || undefined,\n" +
    "          placeholder: el.getAttribute('placeholder') || undefined,\n" +
    "          type: el.getAttribute('type') || undefined,\n" +
    "        };\n" +
    "      });\n" +
    "      return { path: location.pathname, url: location.href, elements };\n" +
    "    });\n" +
    "    let snapshot: { path: string; url: string; elements: unknown[] } | null = null;\n" +
    "    for (let i = 0; i < 2; i++) {\n" +
    "      if (page.isClosed()) break;\n" +
    "      try { snapshot = await runDistill(); } catch { snapshot = null; }\n" +
    "      if (snapshot && Array.isArray(snapshot.elements) && snapshot.elements.length > 0) break;\n" +
    "      try { await page.waitForTimeout(400); } catch { break; }\n" +
    "    }\n" +
    "    if (snapshot) {\n" +
    "      try {\n" +
    "        const distilledPath = testInfo.outputPath('qagent-dom-distilled.json');\n" +
    "        fs.writeFileSync(distilledPath, JSON.stringify(snapshot), 'utf-8');\n" +
    "        await testInfo.attach('qagent-dom-distilled', { path: distilledPath, contentType: 'application/json' });\n" +
    "      } catch {}\n" +
    "    }\n" +
    rawBlock +
    "    try {\n" +
    "      const netPath = testInfo.outputPath('qagent-network.json');\n" +
    "      fs.writeFileSync(netPath, JSON.stringify(__net), 'utf-8');\n" +
    "      await testInfo.attach('qagent-network', { path: netPath, contentType: 'application/json' });\n" +
    "    } catch {}\n" +
    "    try {\n" +
    "      const conPath = testInfo.outputPath('qagent-console.json');\n" +
    "      fs.writeFileSync(conPath, JSON.stringify(__con), 'utf-8');\n" +
    "      await testInfo.attach('qagent-console', { path: conPath, contentType: 'application/json' });\n" +
    "    } catch {}\n" +
    "  }, { auto: true }],\n" +
    "});\n" +
    "\n" +
    "export { expect };\n" +
    "export type * from '@playwright/test';\n"
  );
}

/** Directories never walked when rewriting imports: installed packages (huge,
 * and not ours to rewrite) and Playwright's own output. Matched at ANY depth.
 *
 * **Shared rule** with the server's `FIXTURE_SKIP_DIRS`
 * (`api/app/services/playwright_runner.py`) — see `contracts/fixture-rewrite-tree.json`. */
const FIXTURE_SKIP_DIRS = new Set(["node_modules", "test-results", "playwright-report", "blob-report", ".git"]);

/** Files at the workdir **root** that must keep importing the real `@playwright/test`:
 * only the injected shim itself, which re-exports Playwright's `test`, so rewriting
 * it to point at itself is a cycle.
 *
 * Root-only is load-bearing: a *nested* `fixtures/authenticated.ts` is a genuine
 * library file and MUST be rewritten. Configs are handled separately, at any depth. */
const FIXTURE_SKIP_ROOT_FILES = new Set(["fixtures.ts"]);

/**
 * Every `.ts` file under `specDir` that should have its Playwright import
 * rewritten, as workdir-relative POSIX paths. Mirrors the server's
 * `**​/*.ts` glob (`fixture_targets` / `_skip_fixture_rewrite`).
 *
 * Skipped, identically to the server (#557):
 * - anything under `FIXTURE_SKIP_DIRS`, at any depth;
 * - `*.d.ts` — nothing to rewrite;
 * - **any** `*.config.ts`, at any depth. `defineConfig` lives only in the real
 *   package, and `config/` is one of the project's scaffolded library dirs, so
 *   `config/environments.config.ts` ships in every bundle and must be spared —
 *   this is exactly where the two ports had drifted;
 * - the ROOT `fixtures.ts`.
 */
export function fixtureTargets(specDir: string, relative = ""): string[] {
  const absolute = relative ? path.join(specDir, relative) : specDir;
  let entries: fs.Dirent[];
  try {
    entries = fs.readdirSync(absolute, { withFileTypes: true });
  } catch {
    return [];
  }
  const out: string[] = [];
  for (const entry of entries) {
    const child = relative ? `${relative}/${entry.name}` : entry.name;
    if (entry.isDirectory()) {
      if (FIXTURE_SKIP_DIRS.has(entry.name)) continue;
      out.push(...fixtureTargets(specDir, child));
    } else if (entry.name.endsWith(".ts") && !entry.name.endsWith(".d.ts")) {
      // Any depth — `config/environments.config.ts` is as load-bearing as the
      // root `playwright.config.ts` (server parity, #557).
      if (entry.name.endsWith(".config.ts")) continue;
      if (!relative && FIXTURE_SKIP_ROOT_FILES.has(entry.name)) continue;
      out.push(child);
    }
  }
  return out;
}

/**
 * The module specifier that reaches the workdir-root `fixtures` module from a
 * file at `relative` — `'./fixtures'` at the root, `'../fixtures'` one level
 * deep, `'../../fixtures'` two, and so on.
 *
 * This is the whole point of the layered layout: `tests/SUR-1428/x.spec.ts` and
 * `pages/LoginPage.ts` sit at different depths, so a flat `'./fixtures'` rewrite
 * (what the pre-#541 code did) resolves to a file that does not exist and the
 * spec fails collection.
 */
export function fixturesSpecifier(relative: string): string {
  const depth = relative.replace(/\\/g, "/").split("/").length - 1;
  return depth === 0 ? "./fixtures" : `${"../".repeat(depth)}fixtures`;
}

/**
 * Point every `.ts` file's Playwright import at the generated `fixtures.ts` and
 * write it — port of `_apply_fixtures`, kept **behaviourally identical to the
 * server's** so a spec that passes there passes on the device. Only the module
 * specifier is touched.
 *
 * DOM capture is always on, so fixtures are ALWAYS injected (unlike the previous
 * auth-only behavior): each `'@playwright/test'` import is rewritten to the
 * depth-correct relative specifier and `fixtures.ts` is (re)written every run.
 * `replaySession` only controls whether the generated module also replays the
 * captured sessionStorage.
 *
 * Depth-aware since #541: the workdir is no longer flat. It holds a whole
 * automation project (`pages/`, `components/`, `tests/<TICKET>/…`), so the tree
 * is walked rather than a caller-supplied filename list being trusted — that list
 * cannot describe the library files, which sit at their own depths. The root
 * `fixtures.ts` and every `*.config.ts` at any depth are left alone (a config
 * genuinely needs `defineConfig` from the real package); `node_modules/` and
 * Playwright's output dirs are never walked. See `fixtureTargets` for the full
 * rule, which is shared with the server.
 *
 * @param specDir The job's local workdir containing the staged project.
 * @param sessionFile Absolute path to the `sessionStorage.json` snapshot embedded
 *   in the generated `fixtures.ts`.
 * @param replaySession Whether sessionStorage replay is active for this run.
 * @param captureRaw Whether to also capture the raw-HTML DOM attachment (off for
 *   intermediate heal attempts — #398).
 * @returns The workdir-relative paths that were actually rewritten.
 */
export function applyFixtures(
  specDir: string,
  sessionFile: string,
  replaySession: boolean,
  captureRaw = true
): string[] {
  // Write the shim FIRST so it exists no matter what the walk finds, then skip it
  // in the walk (it lives at the root, in FIXTURE_SKIP_ROOT_FILES).
  fs.writeFileSync(
    path.join(specDir, "fixtures.ts"),
    fixturesTs(sessionFile, replaySession, captureRaw),
    "utf-8"
  );
  const rewritten: string[] = [];
  for (const relative of fixtureTargets(specDir)) {
    const specifier = fixturesSpecifier(relative);
    const filePath = path.join(specDir, relative);
    let text: string;
    try {
      text = fs.readFileSync(filePath, "utf-8");
    } catch {
      continue;
    }
    const newText = text
      .split("'@playwright/test'")
      .join(`'${specifier}'`)
      .split('"@playwright/test"')
      .join(`"${specifier}"`);
    if (newText !== text) {
      fs.writeFileSync(filePath, newText, "utf-8");
      rewritten.push(relative);
    }
  }
  return rewritten;
}
