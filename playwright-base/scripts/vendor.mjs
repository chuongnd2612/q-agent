#!/usr/bin/env node
/**
 * Build + `npm pack` the package and refresh the committed vendored tarball at
 * `vendor/q-agent-playwright-base-<version>.tgz`.
 *
 * That tarball is a **deliberate committed artifact**: the API's `ensure_deps`
 * (#538) installs it directly when the npm registry is unreachable, so an
 * automation project can still be scaffolded and executed offline. Regenerate it
 * whenever the package version or sources change:
 *
 *   cd playwright-base && npm run vendor
 *
 * Stale tarballs for other versions are removed so `vendor/` holds exactly one.
 */
import { execSync } from 'node:child_process';
import { existsSync, mkdirSync, readdirSync, readFileSync, renameSync, rmSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const { name, version } = JSON.parse(readFileSync(join(root, 'package.json'), 'utf-8'));
// npm pack's name for a scoped package: @scope/pkg -> scope-pkg-<version>.tgz
const tarball = `${name.replace(/^@/, '').replace('/', '-')}-${version}.tgz`;
const vendorDir = join(root, 'vendor');

const run = (cmd) => execSync(cmd, { cwd: root, stdio: 'inherit' });

run('npm run build');
run(`npm pack --pack-destination "${root}"`);

mkdirSync(vendorDir, { recursive: true });
for (const entry of readdirSync(vendorDir)) {
  if (entry.endsWith('.tgz') && entry !== tarball) {
    rmSync(join(vendorDir, entry));
    console.log(`removed stale vendored tarball: ${entry}`);
  }
}

const packed = join(root, tarball);
if (!existsSync(packed)) {
  console.error(`vendor: expected npm pack to produce ${tarball}`);
  process.exit(1);
}
const target = join(vendorDir, tarball);
if (existsSync(target)) rmSync(target);
renameSync(packed, target);
console.log(`\nvendored -> playwright-base/vendor/${tarball}`);
console.log('Commit it: the API installs this tarball when the npm registry is unreachable.');
