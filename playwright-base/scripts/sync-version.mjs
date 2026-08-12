#!/usr/bin/env node
/**
 * Rewrite `VERSION` and `src/version.ts`'s `BASE_VERSION` from `package.json`.
 *
 * Run after any `npm version` bump; `scripts/release.mjs` calls it automatically.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const { version } = JSON.parse(readFileSync(join(root, 'package.json'), 'utf-8'));

writeFileSync(join(root, 'VERSION'), `${version}\n`, 'utf-8');

const sourcePath = join(root, 'src', 'version.ts');
const source = readFileSync(sourcePath, 'utf-8');
const updated = source.replace(/(BASE_VERSION\s*=\s*')[^']+(')/, `$1${version}$2`);
if (updated === source && !source.includes(`BASE_VERSION = '${version}'`)) {
  console.error('sync-version: BASE_VERSION assignment not found in src/version.ts');
  process.exit(1);
}
writeFileSync(sourcePath, updated, 'utf-8');

console.log(`synced version -> ${version}`);
