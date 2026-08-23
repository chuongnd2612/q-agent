/**
 * Ensure Playwright's Chromium build is present, downloading it once if absent —
 * so the user never has to run `npx playwright install chromium` manually.
 *
 * Both the headed login capture (`vendor/capture_auth.cjs`) and the spec run use
 * Chromium from the agent's own Playwright install; this guarantees it exists
 * before either runs.
 */
import { spawn } from "child_process";
import * as fs from "fs";
import { childNodeEnv, nodeBin, playwrightCli } from "./paths";

/** Path Playwright expects the Chromium build at, or null if it can't be resolved. */
function chromiumExecutable(): string | null {
  try {
    // Resolved from the agent's own node_modules — the same install used to run specs.
    const { chromium } = require("playwright") as typeof import("playwright");
    return chromium.executablePath();
  } catch {
    return null;
  }
}

/**
 * Ensure Chromium is installed, running `playwright install chromium` if it's missing.
 *
 * Fast no-op once the browser exists. Streams the download progress so a first-run
 * fetch (~100 MB) is visible rather than a silent hang. Invokes Playwright's CLI
 * through the resolved node runtime (`node cli.js install chromium`) so it works
 * from source, via npx, and in a packaged bundle alike.
 *
 * Returns:
 *   true when Chromium is available (already present, or installed successfully);
 *   false when the install failed — the caller should abort rather than run.
 */
export async function ensureChromium(): Promise<boolean> {
  const exe = chromiumExecutable();
  if (exe && fs.existsSync(exe)) return true;

  console.log("Chromium not found — installing Playwright's Chromium (one-time download)...");
  const code = await new Promise<number | null>((resolve) => {
    // Piped rather than inherited (#421): with windowsHide + pipes the child gets
    // no console at all (measured), and under Electron — a GUI process with no
    // stdout — `inherit` discarded the download progress and any failure reason.
    const child = spawn(nodeBin(), [playwrightCli(), "install", "chromium"], {
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
      env: { ...process.env, ...childNodeEnv() },
    });
    const forward = (chunk: unknown): void => {
      const text = String(chunk).replace(/\s+$/, "");
      if (text) console.log(`[playwright install] ${text}`);
    };
    child.stdout?.on("data", forward);
    child.stderr?.on("data", forward);
    child.on("close", resolve);
    child.on("error", () => resolve(null));
  });

  if (code !== 0) {
    console.error("Chromium install failed — run `npx playwright install chromium` manually, then retry.");
    return false;
  }
  console.log("Chromium ready.");
  return true;
}
