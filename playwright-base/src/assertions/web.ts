/**
 * Shared web-first assertion helpers (doc §9 `assertions/`).
 *
 * Every helper delegates to Playwright's own auto-retrying `expect(locator)` matchers
 * — they add naming and a consistent timeout, never a manual poll or sleep. That is
 * the whole point: Q-Agent's generation rules require web-first assertions and ban
 * hard waits, so the base package must make the correct thing the easy thing.
 */

import { expect } from '../runtime';
import type { Locator, Page } from '../runtime';

/** Common option bag: an override timeout in ms. */
export interface AssertOptions {
  timeoutMs?: number;
}

const opts = (options: AssertOptions = {}) => (options.timeoutMs != null ? { timeout: options.timeoutMs } : {});

/** Assert `locator` is visible. */
export async function expectVisible(locator: Locator, options?: AssertOptions): Promise<void> {
  await expect(locator).toBeVisible(opts(options));
}

/** Assert `locator` is hidden or absent. */
export async function expectHidden(locator: Locator, options?: AssertOptions): Promise<void> {
  await expect(locator).toBeHidden(opts(options));
}

/** Assert `locator`'s text equals `text` (string) or matches it (RegExp). */
export async function expectText(locator: Locator, text: string | RegExp, options?: AssertOptions): Promise<void> {
  await expect(locator).toHaveText(text, opts(options));
}

/** Assert `locator`'s text contains `text`. */
export async function expectContainsText(
  locator: Locator,
  text: string | RegExp,
  options?: AssertOptions,
): Promise<void> {
  await expect(locator).toContainText(text, opts(options));
}

/** Assert an input/select/textarea holds `value`. */
export async function expectValue(locator: Locator, value: string | RegExp, options?: AssertOptions): Promise<void> {
  await expect(locator).toHaveValue(value, opts(options));
}

/** Assert `locator` resolves to exactly `count` elements. */
export async function expectCount(locator: Locator, count: number, options?: AssertOptions): Promise<void> {
  await expect(locator).toHaveCount(count, opts(options));
}

/** Assert `locator` is enabled. */
export async function expectEnabled(locator: Locator, options?: AssertOptions): Promise<void> {
  await expect(locator).toBeEnabled(opts(options));
}

/** Assert `locator` is disabled. */
export async function expectDisabled(locator: Locator, options?: AssertOptions): Promise<void> {
  await expect(locator).toBeDisabled(opts(options));
}

/** Assert a checkbox/radio is checked (or unchecked when `checked` is false). */
export async function expectChecked(locator: Locator, checked = true, options?: AssertOptions): Promise<void> {
  await expect(locator).toBeChecked({ ...opts(options), checked });
}

/** Assert `locator` has attribute `name` with `value`. */
export async function expectAttribute(
  locator: Locator,
  name: string,
  value: string | RegExp,
  options?: AssertOptions,
): Promise<void> {
  await expect(locator).toHaveAttribute(name, value, opts(options));
}

/** Assert `locator` carries CSS class `className` (string match or RegExp). */
export async function expectClass(
  locator: Locator,
  className: string | RegExp,
  options?: AssertOptions,
): Promise<void> {
  await expect(locator).toHaveClass(className, opts(options));
}

/** Assert the page URL equals/matches `url`. */
export async function expectUrl(page: Page, url: string | RegExp, options?: AssertOptions): Promise<void> {
  await expect(page).toHaveURL(url, opts(options));
}

/** Assert the page title equals/matches `title`. */
export async function expectTitle(page: Page, title: string | RegExp, options?: AssertOptions): Promise<void> {
  await expect(page).toHaveTitle(title, opts(options));
}

/**
 * Assert a row identified by `rowText` exists in a table-ish container — the one
 * table pattern generic enough to live in the base package (it takes the container
 * locator from the caller and makes no assumption about the app's markup beyond
 * ARIA `row`).
 */
export async function expectRowVisible(
  container: Locator,
  rowText: string | RegExp,
  options?: AssertOptions,
): Promise<void> {
  await expect(container.getByRole('row').filter({ hasText: rowText }).first()).toBeVisible(opts(options));
}

/**
 * Assert every one of `locators` is visible, concurrently.
 *
 * Handy for mapping a test case's "Expected Results" list to one call.
 */
export async function expectAllVisible(locators: Locator[], options?: AssertOptions): Promise<void> {
  await Promise.all(locators.map((locator) => expectVisible(locator, options)));
}

/** Assert `locator` eventually disappears — e.g. a spinner or toast. */
export async function expectEventuallyGone(locator: Locator, options?: AssertOptions): Promise<void> {
  await expect(locator).toHaveCount(0, opts(options));
}
