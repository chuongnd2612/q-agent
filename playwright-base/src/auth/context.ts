/**
 * Authenticated browser contexts (doc §9 `auth/`).
 *
 * Creates a context that already carries a session: Playwright's `storageState` for
 * cookies + `localStorage`, plus the `sessionStorage` replay init script that
 * `storageState` cannot cover (see `./state`).
 */

import type { Browser, BrowserContext, BrowserContextOptions, Page } from '../runtime';
import { applySessionStorageFile, hasStorageState } from './state';

/** Options for {@link createAuthenticatedContext}. */
export interface AuthenticatedContextOptions {
  /** Path to a Playwright `storageState` JSON. Ignored when missing/empty. */
  storageStatePath?: string;
  /** Path to a `sessionStorage.json` snapshot to replay per origin. */
  sessionStoragePath?: string;
  /** Extra context options (viewport, locale, `baseURL`, …). */
  contextOptions?: BrowserContextOptions;
}

/**
 * Open a {@link BrowserContext} pre-loaded with a saved session.
 *
 * A missing `storageStatePath` is tolerated (the context is simply anonymous) so a
 * first run before any session capture still works.
 */
export async function createAuthenticatedContext(
  browser: Browser,
  options: AuthenticatedContextOptions = {},
): Promise<BrowserContext> {
  const { storageStatePath, sessionStoragePath, contextOptions = {} } = options;
  const useState = storageStatePath && hasStorageState(storageStatePath);
  const context = await browser.newContext({
    ...contextOptions,
    ...(useState ? { storageState: storageStatePath } : {}),
  });
  if (sessionStoragePath) await applySessionStorageFile(context, sessionStoragePath);
  return context;
}

/** {@link createAuthenticatedContext} plus a first page, for non-fixture callers. */
export async function createAuthenticatedPage(
  browser: Browser,
  options: AuthenticatedContextOptions = {},
): Promise<{ context: BrowserContext; page: Page }> {
  const context = await createAuthenticatedContext(browser, options);
  return { context, page: await context.newPage() };
}
