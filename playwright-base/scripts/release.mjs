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
import { readFileSync } from 'node:fs';
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
