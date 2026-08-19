#!/usr/bin/env node
/**
 * One-shot release for @q-agent/agent: bump the version, publish the npm package
 * (the `npx @q-agent/agent` path), AND build the Windows desktop app + stage its
 * installer/update-feed for the server's `/downloads/` route (the no-Node,
 * self-updating path). Run on Windows — `electron-builder --win` needs it.
 *
 * Usage (from the agent/ dir):
 *   npm run release                    # bump patch, publish npm, build+stage desktop
 *   npm run release -- --otp=123456    # non-interactive npm 2FA OTP (CI/piped shell)
 *   npm run release -- --minor         # bump minor instead of patch (also --major)
 *   npm run release -- --desktop-only  # skip version bump + npm publish; just build+stage the app
 *   npm run release -- --no-desktop    # only bump + npm publish (old behavior)
 *
 * The desktop installer is written with a STABLE name (`qagent-agent-setup.exe`,
 * see package.json build.artifactName) and copied — with `latest.yml` +
 * `.blockmap` — into the downloads dir the deployment serves. Override the
 * update-feed URL with QAGENT_DOWNLOAD_URL and the staging dir with
 * QAGENT_DOWNLOADS_DIR (defaults to ../downloads, bind-mounted into nginx).
 *
 * The version bump uses --no-git-tag-version, so it edits package.json only and
 * never requires a clean git tree or creates a tag.
 */
import { execSync } from "node:child_process";
import { copyFileSync, existsSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const args = process.argv.slice(2);
const otpArg = args.find((a) => a.startsWith("--otp="));
const level = args.includes("--major") ? "major" : args.includes("--minor") ? "minor" : "patch";
const desktopOnly = args.includes("--desktop-only");
const noDesktop = args.includes("--no-desktop");

const run = (cmd) => execSync(cmd, { stdio: "inherit" });

// Same gap as #599 in playwright-base: nothing here installed dependencies, so on a
// fresh clone `npm run build` (and `prepublishOnly` during `npm publish`) died as
// "'tsc' is not recognized", which reads like a missing global toolchain rather than
// uninstalled local devDependencies. `electron-builder` is a devDependency too, so the
// desktop stage needs this just as much as the npm stage.
const pkgRoot = join(dirname(fileURLToPath(import.meta.url)), "..");

const hasBin = (name) =>
  ["", ".cmd", ".ps1"].some((ext) =>
    existsSync(join(pkgRoot, "node_modules", ".bin", `${name}${ext}`)),
  );

const ensureDeps = () => {
  if (hasBin("tsc")) return; // already installed — keep the common case fast
  const locked = existsSync(join(pkgRoot, "package-lock.json"));
  console.log(
    `node_modules/.bin/tsc not found — installing dependencies (npm ${locked ? "ci" : "install"})...`,
  );
  try {
    execSync(locked ? "npm ci" : "npm install", { cwd: pkgRoot, stdio: "inherit" });
  } catch {
    console.error("\nCould not install dependencies. Run `npm ci` in agent/ and retry.");
    process.exit(1);
  }
  if (!hasBin("tsc")) {
    console.error(
      "\nDependencies installed but node_modules/.bin/tsc is still missing — is `typescript` still a devDependency?",
    );
    process.exit(1);
  }
};

// Dependencies first, before the publish (prepublishOnly builds) or the desktop build.
ensureDeps();

// 1. Bump + publish the npm package (skipped for a desktop-only re-stage).
if (!desktopOnly) {
  // Version bump (package.json only — no git tag, no clean-tree requirement).
  run(`npm version ${level} --no-git-tag-version`);
  // Publish (prepublishOnly rebuilds first). npm prompts for the OTP when
  // --otp isn't supplied; passing it through keeps the flow non-interactive.
  run(`npm publish${otpArg ? ` ${otpArg}` : ""}`);
}

// 2. Build the Windows desktop app and stage its installer + update feed.
if (!noDesktop) {
  const urlOverride = process.env.QAGENT_DOWNLOAD_URL;
  // Server baked into the app so users only enter the 6-digit code (no URL).
  // Defaults to build.extraMetadata.qagentServer in package.json; override per
  // deployment with QAGENT_SERVER.
  const serverOverride = process.env.QAGENT_SERVER;
  run("npm run build");
  // electron-builder reads build.publish / build.extraMetadata from package.json;
  // --desktop deployments can override the update feed + baked server here.
  const cfg =
    (urlOverride ? ` -c.publish.url=${urlOverride}` : "") +
    (serverOverride ? ` -c.extraMetadata.qagentServer=${serverOverride}` : "");
  run(`npx electron-builder --win${cfg}`);

  const outDir = "dist-bin/desktop";
  const dest = process.env.QAGENT_DOWNLOADS_DIR || "../downloads";
  mkdirSync(dest, { recursive: true });
  const artifacts = ["qagent-agent-setup.exe", "qagent-agent-setup.exe.blockmap", "latest.yml"];
  for (const name of artifacts) {
    const src = join(outDir, name);
    if (existsSync(src)) {
      copyFileSync(src, join(dest, name));
      console.log(`staged ${name} -> ${dest}`);
    } else {
      console.warn(`WARN: expected artifact missing: ${src}`);
    }
  }
  console.log(`\nDesktop app staged in ${dest}. Serve it by (re)deploying the web container.`);
}
