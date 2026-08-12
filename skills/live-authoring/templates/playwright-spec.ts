/**
 * E2E spec — authored live by live-authoring (driven against the real app)
 * ---------------------------------------------------------------------------
 * Source ticket : <ADO/Jira ID>
 * Test Case ID  : <TC-NN>
 *
 * Layered spec, written at the planned path inside the project's automation
 * project: tests/<TICKET-ID>/<TICKET-ID>-<TC-NN>.spec.ts — TWO levels below the
 * project root. The base URL, routes and selectors below were VERIFIED LIVE
 * against the running app while authoring — they are real, not inferred.
 *
 *   - Import test/expect/helpers from '@q-agent/playwright-base' (never
 *     '@playwright/test'). That `test` carries the run's saved session, so the
 *     spec starts AUTHENTICATED — no inline login.
 *   - Import a shared project file (e.g. '../../pages/Foo') ONLY when a
 *     reference spec proves it exists; otherwise keep the live-verified
 *     locators inline here. An unresolvable import fails the project-wide
 *     `playwright test --list` gate and the spec is rejected.
 * ---------------------------------------------------------------------------
 */
import { test, expect } from '@q-agent/playwright-base';

test('<TC-NN> — <test case title>', async ({ page }) => {
  // --- Arrange --- already authenticated: navigate straight to the route the
  // case starts on (real, live-verified route).
  await page.goto('<real route verified live>');

  // --- Setup / test data (only if the case needs data that isn't present) ---
  // Recreate any data you created live so the spec is self-sufficient on re-run.

  // --- Act --- map each step to a page action using the LIVE-VERIFIED selectors
  // (data-testid → getByRole → getByLabel → CSS).
  await page.getByRole('button', { name: '<real action>' }).click();

  // --- Assert --- one web-first assertion per Expected Result. Auto-waiting
  // only — never page.waitForTimeout(...).
  await expect(page.getByText('<expected result>')).toBeVisible();
});
