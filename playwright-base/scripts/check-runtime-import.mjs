#!/usr/bin/env node
/**
 * Build gate for the package's central architectural rule (#539):
 * **`src/runtime.ts` is the only module that may import `@playwright/test`.**
 *
 * Q-Agent's execution hosts rewrite `'@playwright/test'` import specifiers when
 * staging a run (`playwright_runner._apply_fixtures`, `playwrightConfig.applyFixtures`),
 * and #540 makes that rewrite depth-aware. One import site is what keeps it tractable.
 *
 * Equivalent to: `grep -rn "@playwright/test" src/` matching only `src/runtime.ts`.
 */
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const srcDir = join(root, 'src');
const ALLOWED = `runtime.ts`;

/** All `.ts` files under `dir`, recursively. */
function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...walk(full));
    else if (entry.endsWith('.ts')) out.push(full);
  }
  return out;
}

const offenders = [];
for (const file of walk(srcDir)) {
  const rel = relative(srcDir, file);
  const text = readFileSync(file, 'utf-8');
  const lines = text.split(/\r?\n/);
  lines.forEach((line, i) => {
    if (!line.includes('@playwright/test')) return;
    if (rel === ALLOWED) return;
    // A mention inside a doc comment is fine; a module specifier is not.
    const trimmed = line.trim();
    if (trimmed.startsWith('*') || trimmed.startsWith('//')) return;
    offenders.push(`  src${sep}${rel}:${i + 1}: ${trimmed}`);
  });
}

if (offenders.length) {
  console.error(`Only src/${ALLOWED} may import '@playwright/test'. Offending lines:`);
  for (const offender of offenders) console.error(offender);
  process.exit(1);
}
console.log(`runtime-import ok: '@playwright/test' imported only by src/${ALLOWED}`);
