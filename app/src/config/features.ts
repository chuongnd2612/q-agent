/**
 * Feature flags for controls whose backend half does not exist yet (#672).
 *
 * Everything here persists correctly — the setting reaches the database and
 * survives a reload — but **nothing reads it back**, so the control looks like
 * it works and changes nothing about how a run behaves. Rather than delete the
 * plumbing, the control is hidden until the reader exists.
 *
 * Each flag names exactly what is missing. Flip one to `true` only once that
 * thing is true; a flag switched on ahead of its reader restores the original
 * bug, which is a control that lies.
 */
export const FEATURES = {
  /**
   * Settings → "Auto-retry flaky tests".
   *
   * Missing: `api/app/services/playwright_runner.py` builds its config from
   * `_PLAYWRIGHT_CONFIG_TEMPLATE`, which has no `retries:` key at all, so every
   * run uses Playwright's default of 0 retries. `Run.retry_policy`
   * (`api/app/models/run.py`) is written on create and never read either.
   * Switch on when the template emits `retries:` from `settings.retryFlaky`
   * (or from `Run.retry_policy`) and a run actually retries.
   */
  retryFlaky: false,

  /**
   * Settings → "Screenshot on failure".
   *
   * Missing: the same template hardcodes `screenshot: 'on'`, so screenshots are
   * captured on every case regardless. Turning the setting off changes nothing.
   * Switch on when the template derives that value from
   * `settings.screenshotOnFail`.
   */
  screenshotOnFail: false,
} as const;
