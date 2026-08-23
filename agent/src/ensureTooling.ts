/**
 * Ensure the `browser-harness` CLI is available for live spec-authoring (#400),
 * provisioning it on first run so the user never installs anything by hand —
 * the same "download once, then it's ready" contract as `ensureChromium`.
 *
 * browser-harness is a Python CLI, so we can't just add an npm dep. Instead we
 * self-provision it with `uv` (a single static binary that also installs its own
 * Python): find `uv` on PATH, else download the platform build once into the
 * agent's config dir, then `uv tool install browser-harness` into a known bin dir
 * we can resolve and prepend to the authoring subprocess PATH.
 *
 * Everything is best-effort and heavily logged: a failure returns `{ok:false}`
 * with a message the caller surfaces, rather than throwing.
 */
import { spawn } from "child_process";
import * as fs from "fs";
import * as path from "path";
import { configDir } from "./config";

const TOOLS_DIR = path.join(configDir(), "tools");
const UV_DIR = path.join(TOOLS_DIR, "uv"); // downloaded uv binary lives here
const TOOL_BIN = path.join(TOOLS_DIR, "bin"); // UV_TOOL_BIN_DIR — browser-harness lands here
const TOOL_DATA = path.join(TOOLS_DIR, "data"); // UV_TOOL_DIR — tool venvs

const isWin = process.platform === "win32";
const exe = (n: string): string => (isWin ? `${n}.exe` : n);

/** Directory holding the installed `browser-harness` executable (prepended to the
 * authoring subprocess PATH so the agent's `claude` can run `browser-harness`). */
export function browserHarnessBinDir(): string {
  return TOOL_BIN;
}

/** Run a command to completion, returning its exit code (null on spawn error).
 * Captures stdio and forwards it to the agent log, so first-run
 * downloads/installs stay visible.
 *
 * Why piped and not `stdio: "inherit"` (#421):
 *
 *   - Measured on Windows 11: with `windowsHide: true` and **piped** stdio the
 *     child gets NO console at all (`GetConsoleWindow() == 0`). With
 *     `stdio: "inherit"` it still gets a console object — window hidden, so no
 *     flash, but a console its own descendants then inherit. No console at all is
 *     strictly the safer state, and it is what makes CREATE_NO_WINDOW propagate
 *     cleanly to `uv`'s own python/tar grandchildren.
 *   - Under Electron the agent is a GUI process with no stdout, so `inherit`
 *     wrote provisioning output into a void — a first-run `uv` failure was
 *     invisible (the #615 class of bug). Piping and logging makes it visible.
 */
function run(cmd: string, args: string[], env?: NodeJS.ProcessEnv): Promise<number | null> {
  return new Promise((resolve) => {
    // windowsHide: don't pop a console window for the uv/tar/browser-harness probe
    // when the agent runs as a GUI (Electron) process with no console of its own.
    const child = spawn(cmd, args, {
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
      env: { ...process.env, ...env },
    });
    const label = path.basename(cmd);
    const forward = (chunk: unknown): void => {
      const text = String(chunk).replace(/\s+$/, "");
      if (text) console.log(`[${label}] ${text}`);
    };
    child.stdout?.on("data", forward);
    child.stderr?.on("data", forward);
    child.on("close", resolve);
    child.on("error", () => resolve(null));
  });
}

/** True if `browser-harness` is already runnable (in our bin dir or on PATH). */
async function harnessPresent(): Promise<boolean> {
  if (fs.existsSync(path.join(TOOL_BIN, exe("browser-harness")))) return true;
  const code = await run(exe("browser-harness"), ["--version"]);
  return code === 0;
}

/** Resolve a usable `uv`: our downloaded copy, then PATH, else download it once. */
async function ensureUv(): Promise<string | null> {
  const local = path.join(UV_DIR, exe("uv"));
  if (fs.existsSync(local)) return local;
  if ((await run(exe("uv"), ["--version"])) === 0) return exe("uv"); // on PATH

  const asset = uvAsset();
  if (!asset) {
    console.error(`ensureUv: unsupported platform ${process.platform}/${process.arch}`);
    return null;
  }
  fs.mkdirSync(UV_DIR, { recursive: true });
  const url = `https://github.com/astral-sh/uv/releases/latest/download/${asset}`;
  const archive = path.join(UV_DIR, asset);
  console.log(`Downloading uv (one-time) from ${url} ...`);
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    fs.writeFileSync(archive, Buffer.from(await res.arrayBuffer()));
  } catch (e) {
    console.error("ensureUv: download failed:", (e as Error).message);
    return null;
  }
  // Extract with the system `tar` (bsdtar on Win10+ handles .zip too; unix uses .tar.gz).
  if ((await run("tar", ["-xf", archive, "-C", UV_DIR])) !== 0) {
    console.error("ensureUv: extract failed (need `tar` on PATH)");
    return null;
  }
  // Archives may nest the binary in a subdir — find it.
  const found = findFile(UV_DIR, exe("uv"));
  if (found && found !== local) {
    try { fs.copyFileSync(found, local); fs.chmodSync(local, 0o755); } catch { /* best-effort */ }
  }
  return fs.existsSync(local) ? local : found;
}

/** The uv release asset name for this platform/arch, or null if unsupported. */
function uvAsset(): string | null {
  const a = process.arch;
  if (isWin) return a === "arm64" ? "uv-aarch64-pc-windows-msvc.zip" : "uv-x86_64-pc-windows-msvc.zip";
  if (process.platform === "darwin")
    return a === "arm64" ? "uv-aarch64-apple-darwin.tar.gz" : "uv-x86_64-apple-darwin.tar.gz";
  if (process.platform === "linux")
    return a === "arm64" ? "uv-aarch64-unknown-linux-gnu.tar.gz" : "uv-x86_64-unknown-linux-gnu.tar.gz";
  return null;
}

/** Shallow recursive search for a file named `name` under `dir`. */
function findFile(dir: string, name: string): string | null {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      const hit = findFile(full, name);
      if (hit) return hit;
    } else if (entry.name === name) {
      return full;
    }
  }
  return null;
}

/**
 * Ensure `browser-harness` is installed. Returns the bin dir to prepend to PATH.
 * Fast no-op once present; on first run downloads uv (if needed) and installs
 * browser-harness (uv fetches its own Python). Never throws.
 */
export async function ensureBrowserHarness(): Promise<{ ok: boolean; binDir: string; error?: string }> {
  try {
    if (await harnessPresent()) return { ok: true, binDir: TOOL_BIN };

    const uv = await ensureUv();
    if (!uv) {
      return { ok: false, binDir: TOOL_BIN, error: "could not obtain `uv` to install browser-harness" };
    }
    fs.mkdirSync(TOOL_BIN, { recursive: true });
    fs.mkdirSync(TOOL_DATA, { recursive: true });
    console.log("Installing browser-harness (one-time) via uv ...");
    const code = await run(uv, ["tool", "install", "--force", "browser-harness"], {
      UV_TOOL_BIN_DIR: TOOL_BIN,
      UV_TOOL_DIR: TOOL_DATA,
    });
    if (code !== 0 || !(await harnessPresent())) {
      return { ok: false, binDir: TOOL_BIN, error: "browser-harness install via uv failed" };
    }
    console.log("browser-harness ready.");
    return { ok: true, binDir: TOOL_BIN };
  } catch (e) {
    return { ok: false, binDir: TOOL_BIN, error: (e as Error).message };
  }
}
