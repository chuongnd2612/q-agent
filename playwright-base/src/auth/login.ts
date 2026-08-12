/**
 * Generic login *infrastructure* (doc §9 `auth/`).
 *
 * This replaces the inline-login block that every generated spec repeats today (see
 * `skills/automation-generator/SKILL.md` step 4, "Inline auth"): navigate to a login
 * URL, fill two fields, submit, wait for a post-login signal.
 *
 * **This is deliberately NOT a page object** (doc §10). It holds no selectors, no
 * URLs and no credentials of its own — the caller passes a {@link LoginFormFields}
 * descriptor resolved from its own project config. A concrete `LoginPage` belongs in
 * the application automation project, and may be implemented *on top of* this.
 */

import type { Locator, Page } from '../runtime';
import { createLogger } from '../logging/logger';

const log = createLogger('auth');

/** A username/password pair. */
export interface Credentials {
  username: string;
  password: string;
  /** Optional TOTP/OTP code, when the flow needs a second factor field. */
  otp?: string;
}

/**
 * How to find the login form's controls. Each entry is either a Playwright selector
 * string or a factory resolving a {@link Locator} from the page — so a project can
 * use `getByRole`/`getByLabel` (its KB's preferred priority) without this module
 * knowing anything about the markup.
 */
export interface LoginFormFields {
  username: string | ((page: Page) => Locator);
  password: string | ((page: Page) => Locator);
  submit: string | ((page: Page) => Locator);
  /** Second-factor field, used only when {@link Credentials.otp} is supplied. */
  otp?: string | ((page: Page) => Locator);
}

/** Resolve a {@link LoginFormFields} entry to a locator. */
function resolve(page: Page, field: string | ((page: Page) => Locator)): Locator {
  return typeof field === 'string' ? page.locator(field) : field(page);
}

/** Options for {@link performFormLogin}. */
export interface FormLoginOptions {
  /** Login page URL (absolute, or relative to `use.baseURL`). */
  loginUrl: string;
  credentials: Credentials;
  fields: LoginFormFields;
  /**
   * A signal that login succeeded, awaited after submit. Either a URL/glob/RegExp
   * passed to `page.waitForURL`, or a callback for anything richer (e.g. awaiting a
   * web-first assertion on a post-login element).
   */
  expectAfterLogin?: string | RegExp | ((page: Page) => Promise<void>);
  /** Timeout in ms for the post-login wait. Default 30000. */
  timeoutMs?: number;
  /** Skip the `page.goto(loginUrl)` when the caller is already on the login page. */
  skipNavigation?: boolean;
}

/**
 * Drive a standard username/password form login on `page`.
 *
 * Uses only auto-waiting locator actions and web-first waits — no `waitForTimeout`,
 * per the project's generation rules.
 */
export async function performFormLogin(page: Page, options: FormLoginOptions): Promise<void> {
  const { loginUrl, credentials, fields, expectAfterLogin, timeoutMs = 30_000, skipNavigation = false } = options;

  if (!skipNavigation) await page.goto(loginUrl, { waitUntil: 'load' });

  await resolve(page, fields.username).fill(credentials.username);
  await resolve(page, fields.password).fill(credentials.password);
  await resolve(page, fields.submit).click();

  if (credentials.otp && fields.otp) {
    await resolve(page, fields.otp).fill(credentials.otp);
    await resolve(page, fields.submit).click();
  }

  if (typeof expectAfterLogin === 'function') {
    await expectAfterLogin(page);
  } else if (expectAfterLogin) {
    await page.waitForURL(expectAfterLogin, { timeout: timeoutMs });
  }

  log.info('form login complete', { url: page.url() });
}

/**
 * A login strategy: anything that can get `page` into an authenticated state.
 *
 * An automation project implements this once (form login, SSO, API-token seeding,
 * storage-state replay) and hands it to {@link createAuthenticatedTest}.
 */
export type LoginFlow = (page: Page) => Promise<void>;

/** Build a {@link LoginFlow} from {@link FormLoginOptions}. */
export function formLoginFlow(options: FormLoginOptions): LoginFlow {
  return (page: Page) => performFormLogin(page, options);
}

/**
 * Run `login` only when `isAuthenticated` reports the page is not already logged in
 * — so a replayed `storageState`/`sessionStorage` session skips the form entirely
 * and only a genuinely-expired session pays for a real login.
 *
 * @returns Whether `login` actually ran.
 */
export async function ensureLoggedIn(
  page: Page,
  login: LoginFlow,
  isAuthenticated: (page: Page) => Promise<boolean>,
): Promise<boolean> {
  let authenticated = false;
  try {
    authenticated = await isAuthenticated(page);
  } catch {
    authenticated = false;
  }
  if (authenticated) {
    log.debug('session already authenticated — skipping login');
    return false;
  }
  await login(page);
  return true;
}
