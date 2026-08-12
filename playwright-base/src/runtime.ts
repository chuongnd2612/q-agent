/**
 * The ONE module in `@q-agent/playwright-base` that may import `@playwright/test`.
 *
 * Every other module in this package — and every generated spec — imports Playwright
 * primitives from here (or, transitively, from `../index`). This is a hard
 * architectural constraint, not a style preference:
 *
 * * Q-Agent's execution hosts rewrite `'@playwright/test'` import specifiers when
 *   staging a run (`playwright_runner._apply_fixtures` on the server,
 *   `playwrightConfig.applyFixtures` in the Local Agent). Funnelling the real import
 *   through a single module keeps that rewrite tractable and depth-aware (#540).
 * * `@playwright/test` is a **peer** dependency, provided by the execution host via
 *   `NODE_PATH`. One import site means one resolution site to reason about.
 *
 * CI guard: `grep -rn "@playwright/test" src/` must only ever match this file.
 */

export {
  test as baseTest,
  expect,
  request,
  devices,
  defineConfig,
  mergeTests,
  mergeExpects,
  chromium,
  firefox,
  webkit,
  selectors,
} from '@playwright/test';

export type {
  APIRequestContext,
  APIResponse,
  Browser,
  BrowserContext,
  BrowserContextOptions,
  ConsoleMessage,
  Cookie,
  Dialog,
  Download,
  ElementHandle,
  FileChooser,
  Fixtures,
  Frame,
  FrameLocator,
  JSHandle,
  Locator,
  Page,
  PlaywrightTestArgs,
  PlaywrightTestConfig,
  PlaywrightTestOptions,
  PlaywrightWorkerArgs,
  PlaywrightWorkerOptions,
  Request,
  Response,
  Route,
  TestInfo,
  TestType,
  Worker as PlaywrightWorker,
} from '@playwright/test';
