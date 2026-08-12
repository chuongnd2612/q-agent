/**
 * Materialize a shipped automation project into a job workdir (#541).
 *
 * The agent is fully stateless: every job stages into a fresh
 * `os.tmpdir()/qagent-*` directory that is deleted when the job ends, and there
 * is **no list-dir/read-file capability in either direction**. So a layered
 * project (page objects, fixtures, data) cannot be fetched on demand — the
 * server ships it wholesale with the claim as `project.files[]`, and this module
 * writes it back out at the right nesting.
 *
 * Two invariants worth stating out loud:
 *
 * * **Every write is confined to the workdir.** A path is rejected outright if it
 *   is absolute, drive-qualified, or escapes via `..`. The server is trusted, but
 *   a bug on either side must not be able to write into the operator's home
 *   directory.
 * * **`@q-agent/playwright-base` resolves without a network call.** The agent
 *   carries a built copy in `vendor/playwright-base/` and copies it into the
 *   workdir's `node_modules/`. See `scripts/vendor-playwright-base.mjs` for why
 *   this beats a per-job `npm install`.
 */

import * as fs from "fs";
import * as path from "path";
import { vendorPlaywrightBase } from "./paths";

/** One project file as shipped in the claim payload. */
export interface ProjectFile {
  path: string;
  code: string;
}

/** The `project` block of a claim payload — the whole shared asset library. */
export interface ProjectBundle {
  /** Bundle protocol version ("1" = the whole library, every time). */
  baseVersion?: string;
  files?: ProjectFile[];
}

/** Mirrors the server's `agent_project_bundle.BUNDLE_MAX_BYTES` (~5 MB). Both
 * sides check: the server so it never ships one, the agent so a rogue/older
 * server cannot make it fill the operator's temp disk. */
export const BUNDLE_MAX_BYTES = 5 * 1024 * 1024;

/** Outcome of {@link materializeProject}, for logging on the device. */
export interface MaterializeResult {
  files: number;
  bytes: number;
  skipped: string[];
  /** Set when the bundle exceeded {@link BUNDLE_MAX_BYTES}; nothing was written. */
  overCap?: boolean;
}

/**
 * True when `relative` is a safe, workdir-confined POSIX-ish relative path.
 * Rejects absolute paths, Windows drive letters, UNC paths and any `..` segment.
 */
export function isSafeRelativePath(relative: string): boolean {
  if (!relative || relative.trim() !== relative) return false;
  const normalized = relative.replace(/\\/g, "/");
  if (normalized.startsWith("/") || /^[A-Za-z]:/.test(normalized)) return false;
  const segments = normalized.split("/");
  return segments.every((s) => s !== "" && s !== "." && s !== "..");
}

/** Write one file under `workDir`, creating parent directories. Returns false when
 * the relative path is unsafe (nothing is written). */
export function writeProjectFile(workDir: string, relative: string, code: string): boolean {
  if (!isSafeRelativePath(relative)) return false;
  const target = path.join(workDir, relative.replace(/\\/g, "/"));
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, code, "utf-8");
  return true;
}

/**
 * Write every bundled project file into `workDir` at its relative path.
 *
 * Over-cap bundles write **nothing** and report `overCap` so the caller can fail
 * the job with a clear reason rather than running against a half-staged tree.
 *
 * @param workDir The job's staged workdir.
 * @param bundle The claim payload's `project` block.
 * @returns Counts + the paths that were rejected as unsafe.
 */
export function materializeProject(workDir: string, bundle: ProjectBundle | undefined): MaterializeResult {
  const files = bundle?.files || [];
  const bytes = files.reduce((sum, f) => sum + Buffer.byteLength(f.code || "", "utf-8"), 0);
  if (bytes > BUNDLE_MAX_BYTES) {
    return { files: 0, bytes, skipped: [], overCap: true };
  }
  const skipped: string[] = [];
  let written = 0;
  for (const file of files) {
    if (writeProjectFile(workDir, file.path, file.code ?? "")) written++;
    else skipped.push(file.path);
  }
  return { files: written, bytes, skipped };
}

/** How {@link installBaseFramework} satisfied `@q-agent/playwright-base`. */
export type BaseInstall = "vendored" | "already-present" | "unavailable";

/**
 * Make `@q-agent/playwright-base` resolvable from `workDir` — the `npm ci`
 * equivalent, without npm.
 *
 * Copies the agent's vendored build into `<workDir>/node_modules/@q-agent/playwright-base`.
 * Its own `@playwright/test` peer resolves through the `NODE_PATH` the runner
 * already sets to the agent's `node_modules` (`paths.agentNodeModules`), so no
 * second copy is needed.
 *
 * Never throws: a missing vendor dir returns `"unavailable"` and the caller logs
 * it. A project that doesn't actually import the base package still runs fine.
 */
export function installBaseFramework(workDir: string): BaseInstall {
  const target = path.join(workDir, "node_modules", "@q-agent", "playwright-base");
  if (fs.existsSync(path.join(target, "package.json"))) return "already-present";
  const source = vendorPlaywrightBase();
  if (!source) return "unavailable";
  try {
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.cpSync(source, target, { recursive: true });
    return "vendored";
  } catch {
    return "unavailable";
  }
}
