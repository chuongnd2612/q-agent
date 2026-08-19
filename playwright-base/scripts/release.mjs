#!/usr/bin/env node
/**
 * One-shot release for `@q-agent/playwright-base`, mirroring `agent/scripts/release.mjs`.
 *
 * Bumps the version, keeps `VERSION` + `src/version.ts` in step, builds, publishes to
 * npm, and refreshes the committed offline-fallback tarball in `vendor/`.
 *
 * **`npm publish` needs an interactive 2FA OTP, so the MAINTAINER runs this — not the
 * assistant** (same division of labour as the Local Agent; see CLAUDE.md).
 *
 * Usage (from playwright-base/):
 *   npm run release                    # bump patch, sync, build, publish, re-vendor
 *   npm run release -- --minor         # bump minor instead of patch (also --major)
 *   npm run release -- --otp=123456    # non-interactive 2FA OTP (fresh code; TOTP expires ~30s)
 *   npm run release -- --no-bump       # publish the current version as-is
 *   npm run release -- --dry-run       # everything except the actual publish
 *
 * The version bump uses --no-git-tag-version, so it edits package.json only and never
 * requires a clean git tree or creates a tag. Commit the resulting version files AND
 * the refreshed vendor/ tarball afterwards.
 */
import { execSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
const otpArg = args.find((a) => a.startsWith('--otp='));
const level = args.includes('--major') ? 'major' : args.includes('--minor') ? 'minor' : 'patch';
const noBump = args.includes('--no-bump');
const dryRun = args.includes('--dry-run');

const run = (cmd) => execSync(cmd, { cwd: root, stdio: 'inherit' });
const version = () => JSON.parse(readFileSync(join(root, 'package.json'), 'utf-8')).version;

// A release must not depend on someone having run `npm install` here first. Without
// deps, `npm run build` dies as "'tsc' is not recognized" — which points at a missing
// global toolchain rather than at uninstalled local devDependencies, so it reads like
// an environment problem when it is a one-command fix (#599). `prepublishOnly` rebuilds
// during `npm publish` too, so this has to hold before the publish, not just the build.
const hasBin = (name) =>
  ['', '.cmd', '.ps1'].some((ext) =>
    existsSync(join(root, 'node_modules', '.bin', `${name}${ext}`)),
  );

const ensureDeps = () => {
  if (hasBin('tsc')) return; // already installed — keep the common case fast
  const locked = existsSync(join(root, 'package-lock.json'));
  console.log(`node_modules/.bin/tsc not found — installing dependencies (npm ${locked ? 'ci' : 'install'})...`);
  try {
    run(locked ? 'npm ci' : 'npm install');
  } catch {
    console.error('\nCould not install dependencies. Run `npm ci` in playwright-base/ and retry.');
    process.exit(1);
  }
  if (!hasBin('tsc')) {
    console.error('\nDependencies installed but node_modules/.bin/tsc is still missing — is `typescript` still a devDependency?');
    process.exit(1);
  }
};

// 0. Dependencies, before anything that needs the local toolchain.
ensureDeps();

// 1. Bump (package.json only — no git tag, no clean-tree requirement).
if (!noBump) run(`npm version ${level} --no-git-tag-version`);

// 2. Keep VERSION + src/version.ts in step, then build (prebuild re-checks both,
//    plus the single-@playwright/test-import rule).
run('node scripts/sync-version.mjs');
run('npm run build');

// 3. Publish. npm prompts for the OTP when --otp isn't supplied.
if (dryRun) {
  console.log(`\n[dry-run] would publish @q-agent/playwright-base@${version()}`);
} else {
  run(`npm publish${otpArg ? ` ${otpArg}` : ''}`);
}

// 4. Refresh the committed offline-fallback tarball for the API's ensure_deps.
run('node scripts/vendor.mjs');

console.log(`\nReleased @q-agent/playwright-base@${version()}.`);
console.log('Commit package.json, VERSION, src/version.ts and the refreshed vendor/*.tgz.');
