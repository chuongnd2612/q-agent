#!/usr/bin/env node
/**
 * Refresh `agent/vendor/playwright-base/` from the committed
 * `playwright-base/vendor/q-agent-playwright-base-<version>.tgz` tarball.
 *
 * Why the agent carries its own copy of `@q-agent/playwright-base` (#541):
 *
 * A layered automation project's specs import from `@q-agent/playwright-base`,
 * so the package must resolve inside the ephemeral `os.tmpdir()` workdir the
 * agent stages every job into. The three ways to make that happen:
 *
 *   1. `npm install` from the registry, per job — needs the network on every
 *      run, adds seconds to each job, and dies on a locked-down device. It also
 *      cannot work today: the package is not published yet.
 *   2. Add it as a real dependency of `@q-agent/agent` — same registry problem,
 *      and it would break `npm ci` for the agent itself until publish day.
 *   3. Vendor the built package and copy it into the workdir's `node_modules/`
 *      — no network, no per-job install, deterministic. This is what we do.
 *
 * The trade-off: the base-framework version is pinned to the agent RELEASE
 * rather than to each project's lockfile, so a project needing a newer base
 * needs an agent update. That is precisely what the server's version guard
 * (`agent_project_bundle.MIN_AGENT_VERSION`) makes visible instead of silent.
 *
 * The extracted directory is a DELIBERATE committed artifact (same precedent as
 * `playwright-base/vendor/*.tgz`). Regenerate it whenever the base package
 * changes:
 *
 *   cd agent && npm run vendor:base
 *
 * `.js.map` / `.d.ts.map` entries are dropped — nothing on the device reads them
 * and they roughly double the size.
 */
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readdirSync, rmSync, statSync, unlinkSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const agentRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = join(agentRoot, "..");
const sourceVendor = join(repoRoot, "playwright-base", "vendor");
const destination = join(agentRoot, "vendor", "playwright-base");

const tarballs = existsSync(sourceVendor)
  ? readdirSync(sourceVendor).filter((n) => n.endsWith(".tgz"))
  : [];
if (tarballs.length !== 1) {
  console.error(
    `expected exactly one tarball in ${sourceVendor}, found ${tarballs.length}. ` +
      `Run \`cd playwright-base && npm run vendor\` first.`
  );
  process.exit(1);
}

rmSync(destination, { recursive: true, force: true });
mkdirSync(destination, { recursive: true });
// `tar` ships with Windows 10+ and every POSIX host; --strip-components=1 drops
// npm pack's `package/` prefix so the result is a directly-usable package dir.
// Paths are RELATIVE to the repo root on purpose: GNU tar (Git Bash on Windows)
// reads an absolute `D:\…` argument as a remote `host:path` and fails.
const rel = (p) => relative(repoRoot, p).split("\\").join("/");
execFileSync(
  "tar",
  ["-xzf", rel(join(sourceVendor, tarballs[0])), "-C", rel(destination), "--strip-components=1"],
  { cwd: repoRoot, stdio: "inherit" }
);

let removed = 0;
let bytes = 0;
const walk = (dir) => {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) {
      walk(path);
    } else if (entry.endsWith(".map")) {
      unlinkSync(path);
      removed++;
    } else {
      bytes += statSync(path).size;
    }
  }
};
walk(destination);

console.log(
  `vendored ${tarballs[0]} -> ${relative(agentRoot, destination)} ` +
    `(${bytes} bytes, ${removed} source maps dropped)`
);
