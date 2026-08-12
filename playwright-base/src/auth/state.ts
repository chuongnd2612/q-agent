/**
 * Authentication state management (doc §9 `auth/`).
 *
 * Playwright's `storageState` persists cookies + `localStorage`, but **not**
 * `sessionStorage` — and that is exactly where MSAL and many SPAs keep their tokens.
 * A restored session therefore bounces straight back to the login page. Q-Agent
 * already worked around this in the fixtures module it injects per run
 * (`playwright_runner._fixtures_ts` / `playwrightConfig.fixturesTs`): snapshot
 * `sessionStorage` per origin, then replay it via an init script before any app code
 * runs. That mechanism lives here now, as typed code instead of a generated string.
 *
 * Nothing in this module knows a login URL, a form, or a credential — an automation
 * project supplies those.
 */

import * as fs from 'fs';
import * as path from 'path';

import type { BrowserContext, Page } from '../runtime';

/**
 * Captured `sessionStorage`, keyed by origin (`https://app.example.com`) then by
 * storage key. Matches the on-disk shape of the `sessionStorage.json` snapshot the
 * server and Local Agent already write.
 */
export type SessionStorageSnapshot = Record<string, Record<string, string>>;

/**
 * Read a {@link SessionStorageSnapshot} from disk.
 *
 * Never throws: a missing or malformed file yields `{}`, matching the injected
 * fixtures module's `try { … } catch {}` around the same read. An absent session is
 * a normal state (no capture has run yet), not an error.
 */
export function loadSessionStorage(file: string): SessionStorageSnapshot {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf-8')) as SessionStorageSnapshot;
  } catch {
    return {};
  }
}

/** Write a {@link SessionStorageSnapshot} to disk, creating parent dirs. */
export function saveSessionStorage(file: string, snapshot: SessionStorageSnapshot): void {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, JSON.stringify(snapshot, null, 2), 'utf-8');
}

/**
 * Install an init script on `context` that restores `snapshot`'s entries for the
 * document's own origin **before any app code runs**.
 *
 * This is the behaviour of the injected fixtures module's `context` override, which
 * is why it is origin-scoped and fully swallowed on error: a page that forbids
 * storage access must not fail the test.
 */
export async function applySessionStorage(
  context: BrowserContext,
  snapshot: SessionStorageSnapshot,
): Promise<void> {
  await context.addInitScript((sessions: SessionStorageSnapshot) => {
    try {
      const entries = sessions[location.origin];
      if (entries) for (const key in entries) window.sessionStorage.setItem(key, entries[key]);
    } catch {
      /* ignore */
    }
  }, snapshot);
}

/**
 * {@link applySessionStorage} reading the snapshot from `file`.
 *
 * @returns Whether any entries were installed (`false` for a missing/empty file).
 */
export async function applySessionStorageFile(context: BrowserContext, file: string): Promise<boolean> {
  const snapshot = loadSessionStorage(file);
  await applySessionStorage(context, snapshot);
  return Object.keys(snapshot).length > 0;
}

/**
 * Snapshot the live page's `sessionStorage` for its current origin.
 *
 * Use after an interactive/programmatic login to persist what `storageState` cannot.
 */
export async function captureSessionStorage(page: Page): Promise<SessionStorageSnapshot> {
  try {
    return await page.evaluate(() => {
      const entries: Record<string, string> = {};
      for (let i = 0; i < window.sessionStorage.length; i++) {
        const key = window.sessionStorage.key(i);
        if (key == null) continue;
        entries[key] = window.sessionStorage.getItem(key) ?? '';
      }
      return { [location.origin]: entries } as Record<string, Record<string, string>>;
    });
  } catch {
    return {};
  }
}

/**
 * Persist both halves of a session after a login: Playwright's `storageState`
 * (cookies + `localStorage`) and the `sessionStorage` snapshot it omits.
 *
 * @param page A logged-in page.
 * @param options `storageStatePath` and/or `sessionStoragePath`; each is skipped
 *   when omitted.
 */
export async function saveAuthState(
  page: Page,
  options: { storageStatePath?: string; sessionStoragePath?: string },
): Promise<void> {
  const { storageStatePath, sessionStoragePath } = options;
  if (storageStatePath) {
    fs.mkdirSync(path.dirname(storageStatePath), { recursive: true });
    await page.context().storageState({ path: storageStatePath });
  }
  if (sessionStoragePath) {
    saveSessionStorage(sessionStoragePath, await captureSessionStorage(page));
  }
}

/** Whether `file` exists and is non-empty — the server's "do we have a session?" test. */
export function hasStorageState(file: string): boolean {
  try {
    return fs.existsSync(file) && fs.statSync(file).size > 0;
  } catch {
    return false;
  }
}
