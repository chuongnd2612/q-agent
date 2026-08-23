#!/usr/bin/env node
/**
 * i18n key-coverage gate (#606).
 *
 * A missing `t()` key fails *silently* in i18next — the raw key renders as text,
 * which is exactly how `spec.empty.hint` shipped. Worse, that key was actually
 * present in the JSON, but inside a **duplicate** `"empty"` object, so the later
 * sibling silently replaced it at parse time. This script fails the build on all
 * three shapes:
 *
 *   1. a duplicate key anywhere in a locale catalog (silent data loss),
 *   2. a literal `t("…")` / `<Trans i18nKey="…">` key that does not resolve in `en`,
 *   3. a non-`en` locale disagreeing with `en` on any namespace's key set.
 *
 * Only *literal* keys can be checked; dynamic (`t(`x.${y}`)`) keys are counted
 * and reported, not enforced.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const appDir = join(dirname(fileURLToPath(import.meta.url)), "..");
const srcDir = join(appDir, "src");
const localesDir = join(srcDir, "i18n", "locales");
const DEFAULT_NS = "common"; // must match `defaultNS` in src/i18n/index.ts
const errors = [];

/**
 * Walk the raw token stream and flag any object literal that declares the same
 * key twice — `JSON.parse` gives no signal, it just keeps the last one. Locale
 * catalogs are plain nested objects of strings, so a small scanner is enough
 * (and avoids a parser dependency).
 */
function findDuplicateKeys(text) {
  const dups = [];
  const stack = [];
  let i = 0;
  const readString = () => {
    let s = "";
    i++; // opening quote
    while (i < text.length) {
      const c = text[i];
      if (c === "\\") {
        s += text[i + 1];
        i += 2;
        continue;
      }
      if (c === '"') {
        i++;
        return s;
      }
      s += c;
      i++;
    }
    return s;
  };
  while (i < text.length) {
    const c = text[i];
    if (c === "{") {
      stack.push({ keys: new Set(), expectKey: true });
      i++;
      continue;
    }
    if (c === "[") {
      stack.push(null);
      i++;
      continue;
    }
    if (c === "}" || c === "]") {
      stack.pop();
      i++;
      continue;
    }
    if (c === ",") {
      const top = stack.at(-1);
      if (top) top.expectKey = true;
      i++;
      continue;
    }
    if (c === ":") {
      const top = stack.at(-1);
      if (top) top.expectKey = false;
      i++;
      continue;
    }
    if (c === '"') {
      const top = stack.at(-1);
      const isKey = Boolean(top && top.expectKey);
      const s = readString();
      if (isKey) {
        if (top.keys.has(s)) dups.push(s);
        top.keys.add(s);
      }
      continue;
    }
    i++;
  }
  return dups;
}

function loadCatalog(file) {
  const text = readFileSync(file, "utf8");
  for (const d of findDuplicateKeys(text)) {
    errors.push(`${relative(appDir, file)}: duplicate key "${d}" — the later one silently wins`);
  }
  return JSON.parse(text);
}

/**
 * Drop an i18next plural suffix, so `runMeta_one` / `runMeta_other` collapse to
 * the `runMeta` a caller actually passes to `t()`. Locales legitimately differ in
 * how many plural forms they carry (`vi` has one, `en` two), so both resolution
 * and cross-locale parity are compared on these families, not raw keys.
 */
const PLURAL_SUFFIX = /_(zero|one|two|few|many|other|plural)$/;
const pluralBase = (key) => key.replace(PLURAL_SUFFIX, "");

/** All leaf key paths of a catalog, dot-joined, with plural suffixes collapsed. */
function flatten(obj, prefix = "", out = new Set()) {
  for (const [k, v] of Object.entries(obj)) {
    const path = prefix ? `${prefix}.${k}` : k;
    if (v && typeof v === "object" && !Array.isArray(v)) flatten(v, path, out);
    else out.add(pluralBase(path));
  }
  return out;
}

function walk(dir, files = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, files);
    else if (/\.(ts|tsx)$/.test(name)) files.push(p);
  }
  return files;
}

// ---- load catalogs -------------------------------------------------------
const langs = readdirSync(localesDir).filter((d) => statSync(join(localesDir, d)).isDirectory());
/** lang -> ns -> Set<keyPath> */
const keys = {};
for (const lang of langs) {
  keys[lang] = {};
  for (const f of readdirSync(join(localesDir, lang)).filter((n) => n.endsWith(".json"))) {
    keys[lang][f.replace(/\.json$/, "")] = flatten(loadCatalog(join(localesDir, lang, f)));
  }
}

if (!keys.en) {
  console.error("check-i18n: no `en` locale directory found");
  process.exit(1);
}

// ---- en / other-locale parity -------------------------------------------
for (const lang of langs.filter((l) => l !== "en")) {
  for (const ns of new Set([...Object.keys(keys.en), ...Object.keys(keys[lang])])) {
    const a = keys.en[ns];
    const b = keys[lang][ns];
    if (!a) {
      errors.push(`namespace "${ns}" exists in ${lang} but not in en`);
      continue;
    }
    if (!b) {
      errors.push(`namespace "${ns}" exists in en but not in ${lang}`);
      continue;
    }
    for (const k of a) if (!b.has(k)) errors.push(`${lang}/${ns}.json: missing key "${k}" (present in en)`);
    for (const k of b) if (!a.has(k)) errors.push(`en/${ns}.json: missing key "${k}" (present in ${lang})`);
  }
}

// ---- literal t() keys resolve in en -------------------------------------
let dynamic = 0;
const T_CALL = /\bt\(\s*(["'`])((?:[^"'`\\]|\\.)*?)\1/g;
const TRANS = /i18nKey=\{?\s*(["'])((?:[^"'\\]|\\.)*?)\1/g;
const USE_NS = /useTranslation\(\s*(?:\[\s*)?((?:["'][^"']+["']\s*,?\s*)+)/g;

for (const file of walk(srcDir)) {
  if (file.includes(join("i18n", "locales"))) continue;
  const text = readFileSync(file, "utf8");
  const declared = [...text.matchAll(USE_NS)].flatMap((m) => [...m[1].matchAll(/["']([^"']+)["']/g)].map((q) => q[1]));
  // A helper module that takes a `TFunction` parameter has no namespace of its
  // own — its `t` is bound by the caller — so any namespace is a valid home.
  const fileNs = declared.length > 0 ? new Set([DEFAULT_NS, ...declared]) : new Set(Object.keys(keys.en));
  const found = [];
  for (const m of text.matchAll(T_CALL)) found.push(m[2]);
  for (const m of text.matchAll(TRANS)) found.push(m[2]);

  for (const raw of found) {
    // `""`, a template hole, or a trailing `.` (a `t("a.b." + x)` prefix) is dynamic.
    if (raw === "" || raw.includes("${") || raw.endsWith(".")) {
      dynamic++;
      continue;
    }
    let ns = null;
    let key = raw;
    const colon = raw.indexOf(":");
    if (colon > 0 && keys.en[raw.slice(0, colon)]) {
      ns = raw.slice(0, colon);
      key = raw.slice(colon + 1);
    }
    const candidates = ns ? [ns] : [...fileNs];
    if (!candidates.some((n) => keys.en[n]?.has(key))) {
      errors.push(
        `${relative(appDir, file)}: t("${raw}") does not resolve in en (looked in: ${candidates.join(", ")})`,
      );
    }
  }
}

if (errors.length > 0) {
  console.error(`check-i18n: ${errors.length} problem(s):`);
  for (const e of errors) console.error(`  - ${e}`);
  process.exit(1);
}
console.log(
  `check-i18n: OK — ${langs.join("/")} in sync, all literal t() keys resolve (${dynamic} dynamic key(s) skipped).`,
);
