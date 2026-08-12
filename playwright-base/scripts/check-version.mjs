#!/usr/bin/env node
/**
 * Build gate: `package.json` version, the `VERSION` file and `src/version.ts`'s
 * `BASE_VERSION` must all agree.
 *
 * They are three copies of one fact for three different consumers — npm, the
 * server's `ensure_deps` fallback, and runtime compatibility checks — so a drift
 * would silently mis-report what a project was scaffolded against
 * (`AutomationProject.base_version`, #538).
 */
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const pkg = JSON.parse(readFileSync(join(root, 'package.json'), 'utf-8'));
const versionFile = readFileSync(join(root, 'VERSION'), 'utf-8').trim();
const source = readFileSync(join(root, 'src', 'version.ts'), 'utf-8');
const match = /BASE_VERSION\s*=\s*'([^']+)'/.exec(source);

const problems = [];
if (versionFile !== pkg.version) problems.push(`VERSION is ${versionFile}, package.json is ${pkg.version}`);
if (!match) problems.push('src/version.ts: could not find BASE_VERSION');
else if (match[1] !== pkg.version) problems.push(`src/version.ts BASE_VERSION is ${match[1]}, package.json is ${pkg.version}`);

if (problems.length) {
  console.error('Version drift detected:');
  for (const problem of problems) console.error(`  - ${problem}`);
  console.error('\nFix with: node scripts/sync-version.mjs');
  process.exit(1);
}
console.log(`version ok: ${pkg.version}`);
