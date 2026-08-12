/**
 * Runtime path resolution for the agent's child processes (Playwright CLI +
 * the headed-login capture script), working in three layouts:
 *
 *   1. from source / `npx @q-agent/agent` — `node` on PATH, deps in the package's
 *      own node_modules;
 *   2. a packaged Windows bundle (built by `scripts/package-win.mjs`, run as a
 *      `@yao-pkg/pkg` binary) — a bundled `node.exe`, `node_modules/`, and
 *      `vendor/` sit next to the executable.
 *
 * Everything the agent spawns is invoked as `nodeBin() <script/cli.js> …`, so
 * the same code path works whether `node` comes from PATH or the bundle.
 */
import * as fs from "fs";
import * as path from "path";

export function firstExisting(candidates: string[]): string | null {
  for (const c of candidates) {
    if (fs.existsSync(c)) return c;
  }
  return null;
}

/** True when running inside Electron (the desktop app) rather than plain Node. */
export function isElectron(): boolean {
  return Boolean((process as { versions?: { electron?: string } }).versions?.electron);
}

/**
 * Environment additions for spawned child node processes. Under Electron,
 * `process.execPath` is the Electron binary, so `nodeBin()` returns it — set
 * ELECTRON_RUN_AS_NODE=1 so it runs the given script as Node rather than
 * launching another app window.
 */
export function childNodeEnv(): NodeJS.ProcessEnv {
  return isElectron() ? { ELECTRON_RUN_AS_NODE: "1" } : {};
}

/** True when running as a Node Single Executable Application (the packaged .exe). */
function isSea(): boolean {
  try {
    return (require("node:sea") as { isSea(): boolean }).isSea();
  } catch {
    return false;
  }
}

/**
 * The folder beside the executable in a packaged bundle (holds the bundled
 * node runtime, node_modules, and vendor/). Null when running from source/npx.
 * Set when running as a SEA binary (the Windows bundle) — or a pkg binary.
 */
export function packagedRoot(): string | null {
  return isSea() || (process as { pkg?: unknown }).pkg ? path.dirname(process.execPath) : null;
}

/**
 * Node runtime used to spawn child processes. Prefers the bundled `node.exe`
 * in a packaged install (so no global Node is required), else `node` from PATH.
 */
export function nodeBin(): string {
  const root = packagedRoot();
  if (root) {
    const bundled = path.join(root, process.platform === "win32" ? "node.exe" : "node");
    if (fs.existsSync(bundled)) return bundled;
  }
  // From source / npx, the agent itself runs under a real Node — reuse it. An
  // absolute path lets children spawn without a shell (safe with spaces in the
  // path); `process.execPath` is only the pkg binary when packagedRoot() is set.
  return process.execPath;
}

/** node_modules holding `playwright` + `@playwright/test`. */
export function agentNodeModules(): string {
  const root = packagedRoot();
  if (root) {
    const nm = path.join(root, "node_modules");
    if (fs.existsSync(nm)) return nm;
  }
  // From source / npx / global install, let Node's own resolver find the
  // node_modules that actually holds the deps. npm (and `npx`) HOIST
  // `@playwright/test` and `playwright` to a parent node_modules — the npx
  // cache root or the global root — rather than nesting them under the package,
  // so a hardcoded relative guess misses them (the cause of the
  // "Cannot find module '@playwright/test'" failure on npx/global installs).
  // `.../node_modules/@playwright/test/package.json` → `.../node_modules`.
  const testPkgJson = require.resolve("@playwright/test/package.json");
  return path.dirname(path.dirname(path.dirname(testPkgJson)));
}

/** Playwright's CLI entry — drives both `test` (run specs) and `install` (browsers). */
export function playwrightCli(): string {
  const root = packagedRoot();
  if (root) {
    const bundled = path.join(root, "node_modules", "playwright", "cli.js");
    if (fs.existsSync(bundled)) return bundled;
  }
  // Resolve via Node so it works whether npm nested or hoisted `playwright`.
  return path.join(path.dirname(require.resolve("playwright/package.json")), "cli.js");
}

/** The bundled Claude Code CLI (`@anthropic-ai/claude-code`), so no separate
 * `claude` install / PATH shim is needed (#400 — live-authoring drives it on the
 * agent). NOTE: the package ships a NATIVE binary (`bin/claude[.exe]`), not a JS
 * entry — spawn it DIRECTLY (not via node). Resolves the package's `bin` entry
 * across source/npx and the packaged bundle. */
export function claudeCli(): string {
  const relFromDir = (dir: string): string => {
    const fallback = path.join(dir, "bin", process.platform === "win32" ? "claude.exe" : "claude");
    try {
      const bin = (require(path.join(dir, "package.json")) as { bin?: string | Record<string, string> }).bin;
      const rel = typeof bin === "string" ? bin : bin ? Object.values(bin)[0] : null;
      return rel ? path.join(dir, rel) : fallback;
    } catch {
      return fallback;
    }
  };
  const root = packagedRoot();
  if (root) {
    const dir = path.join(root, "node_modules", "@anthropic-ai", "claude-code");
    if (fs.existsSync(dir)) return relFromDir(dir);
  }
  return relFromDir(path.dirname(require.resolve("@anthropic-ai/claude-code/package.json")));
}

/** The vendored headed-login capture script (`capture_auth.cjs`). */
export function vendorCaptureScript(): string {
  const root = packagedRoot();
  const found = firstExisting([
    ...(root ? [path.join(root, "vendor", "capture_auth.cjs")] : []),
    path.join(__dirname, "..", "..", "vendor", "capture_auth.cjs"),
    path.join(__dirname, "..", "vendor", "capture_auth.cjs"),
  ]);
  if (!found) throw new Error("capture_auth.cjs not found — the agent package is missing vendor/");
  return found;
}

/** The vendored persistent DOM-exploration driver (`explore_session.cjs`). */
export function vendorExploreScript(): string {
  const root = packagedRoot();
  const found = firstExisting([
    ...(root ? [path.join(root, "vendor", "explore_session.cjs")] : []),
    path.join(__dirname, "..", "..", "vendor", "explore_session.cjs"),
    path.join(__dirname, "..", "vendor", "explore_session.cjs"),
  ]);
  if (!found) throw new Error("explore_session.cjs not found — the agent package is missing vendor/");
  return found;
}

/** The vendored Playwright live reporter (`live_reporter.cjs`) — emits per-test
 * results so execution status updates immediately instead of at end-of-run. */
export function vendorLiveReporter(): string {
  const root = packagedRoot();
  const found = firstExisting([
    ...(root ? [path.join(root, "vendor", "live_reporter.cjs")] : []),
    path.join(__dirname, "..", "..", "vendor", "live_reporter.cjs"),
    path.join(__dirname, "..", "vendor", "live_reporter.cjs"),
  ]);
  if (!found) throw new Error("live_reporter.cjs not found — the agent package is missing vendor/");
  return found;
}

/**
 * The vendored `@q-agent/playwright-base` package directory (#541), or null when
 * this build predates it.
 *
 * A layered project's specs import the base framework, so it must resolve inside
 * the ephemeral job workdir. The agent carries a built copy rather than
 * `npm install`-ing per job: no network, no per-job install, deterministic —
 * see `scripts/vendor-playwright-base.mjs` for the full trade-off. Returns a
 * directory (not a file), copied into `<workdir>/node_modules/@q-agent/…` by
 * `projectBundle.installBaseFramework`.
 */
export function vendorPlaywrightBase(): string | null {
  const root = packagedRoot();
  // Probe the manifest (a directory always "exists" once created), then hand back
  // its directory.
  const manifest = firstExisting([
    ...(root ? [path.join(root, "vendor", "playwright-base", "package.json")] : []),
    path.join(__dirname, "..", "..", "vendor", "playwright-base", "package.json"),
    path.join(__dirname, "..", "vendor", "playwright-base", "package.json"),
  ]);
  return manifest ? path.dirname(manifest) : null;
}

/** The vendored live-authoring Chrome launcher (`authoring_browser.cjs`, #403).
 * Uses only Node built-ins (no Playwright), so it runs with a bare `node`. */
export function vendorAuthoringScript(): string {
  const root = packagedRoot();
  const found = firstExisting([
    ...(root ? [path.join(root, "vendor", "authoring_browser.cjs")] : []),
    path.join(__dirname, "..", "..", "vendor", "authoring_browser.cjs"),
    path.join(__dirname, "..", "vendor", "authoring_browser.cjs"),
  ]);
  if (!found) throw new Error("authoring_browser.cjs not found — the agent package is missing vendor/");
  return found;
}
