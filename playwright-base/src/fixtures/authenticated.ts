/**
 * Authenticated fixtures (doc §9 `fixtures/`).
 *
 * The base package supplies the *plumbing* for "a page that is already logged in";
 * the automation project supplies the {@link LoginFlow} — because only the project
 * knows its login URL, form and credentials (doc §10).
 */

import type { Page } from '../runtime';
import { ensureLoggedIn, type LoginFlow } from '../auth/login';
import { test as baseFixtureTest } from './base';

/** The fixture {@link createAuthenticatedTest} adds. */
export interface AuthenticatedFixtures {
  /** A `page` guaranteed to be in an authenticated state. */
  authenticatedPage: Page;
}

/** Options for {@link createAuthenticatedTest}. */
export interface AuthenticatedTestOptions {
  /** How to log in. Project-supplied. */
  login: LoginFlow;
  /**
   * Cheap check for "already logged in". When given, the login flow is skipped for a
   * session restored from `storageState`/`sessionStorage`, so only a genuinely
   * expired session pays for a real login.
   */
  isAuthenticated?: (page: Page) => Promise<boolean>;
  /** Navigated to before the authentication check, when set. */
  entryUrl?: string;
}

/**
 * Build a `test` with an `authenticatedPage` fixture on top of the base test (so it
 * keeps evidence capture and session replay).
 *
 * @example
 * ```ts
 * export const test = createAuthenticatedTest({
 *   login: formLoginFlow({ loginUrl: '/login', credentials, fields }),
 *   isAuthenticated: async (page) => page.getByTestId('user-menu').isVisible(),
 * });
 * ```
 */
export function createAuthenticatedTest(options: AuthenticatedTestOptions) {
  const { login, isAuthenticated, entryUrl } = options;
  return baseFixtureTest.extend<AuthenticatedFixtures>({
    authenticatedPage: async ({ page }, use) => {
      if (entryUrl) await page.goto(entryUrl);
      if (isAuthenticated) await ensureLoggedIn(page, login, isAuthenticated);
      else await login(page);
      await use(page);
    },
  });
}
