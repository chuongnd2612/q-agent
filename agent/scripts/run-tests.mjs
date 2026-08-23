#!/usr/bin/env node
/**
 * Run the agent's compiled test suite: `node --test` over every `*.test.js`
 * under `dist/test/`.
 *
 * Why this is a script and not just a `node --test <path>` one-liner (#470):
 *
 *   - `node --test dist/test` — the form this package shipped with — is BROKEN
 *     on modern Node. Since Node 21 the test runner resolves a bare positional
 *     argument as a *module to load* rather than a directory to scan, so on
 *     Node 23/24/26 it dies with `Cannot find module .../dist/test`, reports
 *     `pass 0 / fail 1`, and discovers nothing. A trailing slash fails the
 *     same way. That is a red gate for a Node-version reason, which trains
 *     people to ignore the gate — and hides real failures behind the noise.
 *   - `node --test "dist/test/*.test.js"` works on Node >= 21 (the runner does
 *     its own globbing there), but matches NOTHING on Node 18/20, which had no
 *     glob support and would look for a literal filename. And a glob that
 *     matches nothing still EXITS 0 — a green gate that ran zero tests, the
 *     worst possible failure mode for a gate.
 *
 * So: enumerate the files here, pass them to `node --test` as explicit paths
 * (understood by every Node this package supports, `engines.node >= 18`), and
 * hard-fail when the enumeration comes up empty rather than exiting 0 on a
 * suite that never ran. Recursive, so a test file added in a nested directory
 * is picked up instead of being quietly skipped by a single-level glob.
 *
 * Extra arguments are forwarded, so `npm test -- --test-name-pattern=foo`
 * still works.
 */
import { spawnSync } from 'node:child_process';
import { readdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const agentRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const testRoot = path.join(agentRoot, 'dist', 'test');

/** @returns {string[]} every `*.test.js` under `dir`, recursively, sorted. */
function collect(dir) {
  let entries;
  try {
    entries = readdirSync(dir, { withFileTypes: true });
  } catch (err) {
    if (err && err.code === 'ENOENT') return [];
    throw err;
  }
  const found = [];
  for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) found.push(...collect(full));
    else if (entry.isFile() && entry.name.endsWith('.test.js')) found.push(full);
  }
  return found;
}

const files = collect(testRoot);

if (files.length === 0) {
  console.error(
    `No test files found under ${path.relative(agentRoot, testRoot)}/ — ` +
      'did `npm run build` (tsc) run, and is `test/**/*.test.ts` still in the tsconfig `include`?',
  );
  process.exit(1);
}

console.log(`running ${files.length} test file(s) from dist/test/`);

const result = spawnSync(
  process.execPath,
  ['--test', ...process.argv.slice(2), ...files.map((f) => path.relative(agentRoot, f))],
  { cwd: agentRoot, stdio: 'inherit' },
);

if (result.error) throw result.error;
process.exit(result.status === null ? 1 : result.status);
