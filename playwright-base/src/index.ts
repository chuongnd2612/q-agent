/**
 * `@q-agent/playwright-base` — the shared Playwright base framework (doc §9).
 *
 * The public surface of Layer 2. Everything an automation project or a generated spec
 * needs is re-exported here, **including `test` and `expect`**, so no generated file
 * ever imports `@playwright/test` directly:
 *
 * ```ts
 * import { test, expect } from '@q-agent/playwright-base';
 * ```
 *
 * `test` is the base test extended with always-on evidence capture and optional
 * `sessionStorage` replay (see `fixtures/base`). It is a drop-in replacement for
 * Playwright's `test`.
 *
 * What is deliberately NOT here (doc §10): application page objects, app-specific
 * fixtures, app URLs, credentials, or domain test-data factories. Those belong to the
 * application automation project (Layer 3).
 */

// ------------------------------------------------------------------ Playwright core
// Re-exported from the single `@playwright/test` import site (`./runtime`).
export {
  baseTest,
  request,
  devices,
  defineConfig,
  mergeTests,
  mergeExpects,
  chromium,
  firefox,
  webkit,
  selectors,
} from './runtime';

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
  PlaywrightWorker,
  Request,
  Response,
  Route,
  TestInfo,
  TestType,
} from './runtime';

// --------------------------------------------------------------- test + expect
export { test, expect } from './fixtures/base';
export type { BaseFixtures, EvidenceOptions, SessionReplayOptions } from './fixtures/base';
export { createAuthenticatedTest } from './fixtures/authenticated';
export type { AuthenticatedFixtures, AuthenticatedTestOptions } from './fixtures/authenticated';

// ------------------------------------------------------------------------ auth
export {
  applySessionStorage,
  applySessionStorageFile,
  captureSessionStorage,
  hasStorageState,
  loadSessionStorage,
  saveAuthState,
  saveSessionStorage,
} from './auth/state';
export type { SessionStorageSnapshot } from './auth/state';
export { ensureLoggedIn, formLoginFlow, performFormLogin } from './auth/login';
export type { Credentials, FormLoginOptions, LoginFlow, LoginFormFields } from './auth/login';
export { createAuthenticatedContext, createAuthenticatedPage } from './auth/context';
export type { AuthenticatedContextOptions } from './auth/context';

// ------------------------------------------------------------------------- api
export { createApiClient, createStandaloneApiClient } from './api/client';
export type { ApiClient, ApiClientOptions, ApiRequestOptions, ApiResult } from './api/client';

// -------------------------------------------------------------------- evidence
export {
  ATTACHMENT_NAMES,
  attachDomSnapshot,
  attachJson,
  attachRawDom,
  attachText,
  captureDomSnapshot,
  distillDom,
} from './evidence/dom';
export type { DistilledElement, DomSnapshot } from './evidence/dom';
export { createTelemetryRecorder } from './evidence/telemetry';
export type { ConsoleEntry, NetworkEntry, TelemetryOptions, TelemetryRecorder } from './evidence/telemetry';
export { attachFile, attachScreenshot, attachVideo, startTrace, stopTraceAndAttach } from './evidence/artifacts';
export type { ScreenshotOptions, TraceOptions } from './evidence/artifacts';

// --------------------------------------------------------------------- logging
export { createLogger, logger } from './logging/logger';
export type { AutomationLogger, LoggerOptions, LogLevel, LogRecord } from './logging/logger';

// ---------------------------------------------------------------------- config
export { env, envBool, envInt, envList, loadEnvironment, requireEnv, resolveUrl } from './config/environment';
export type { EnvironmentConfig, EnvironmentOverrides } from './config/environment';

// ----------------------------------------------------------------------- utils
export { retry, sleep, waitFor, withTimeout } from './utils/wait';
export type { RetryOptions, WaitForOptions } from './utils/wait';
export {
  createTempFile,
  downloadTo,
  ensureDir,
  fileHasContent,
  readJson,
  readText,
  uploadFiles,
  writeJson,
  writeText,
} from './utils/files';
export {
  addDays,
  addMonths,
  daysFromNow,
  formatDate,
  isoDate,
  isoDateTime,
  timestampSlug,
  today,
} from './utils/dates';
export {
  randomBool,
  randomDigits,
  randomEmail,
  randomInt,
  randomPassword,
  randomPhone,
  randomPick,
  randomSample,
  randomString,
  shuffle,
  uniqueId,
  uniqueSuffix,
} from './utils/random';

// ------------------------------------------------------------------ assertions
export {
  expectAllVisible,
  expectAttribute,
  expectChecked,
  expectClass,
  expectContainsText,
  expectCount,
  expectDisabled,
  expectEnabled,
  expectEventuallyGone,
  expectHidden,
  expectRowVisible,
  expectText,
  expectTitle,
  expectUrl,
  expectValue,
  expectVisible,
} from './assertions/web';
export type { AssertOptions } from './assertions/web';

// --------------------------------------------------------------------- version
export { assertCompatibleWith, BASE_PACKAGE_NAME, BASE_VERSION, isCompatibleWith, parseVersion } from './version';
