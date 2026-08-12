/**
 * Screenshot / trace / video / file attachment helpers (doc §9 `evidence/`).
 *
 * Thin, generic wrappers so specs and page objects can add evidence without
 * repeating `testInfo.outputPath` + `testInfo.attach` plumbing. All are best-effort:
 * evidence capture must never be the reason a test fails.
 */

import * as fs from 'fs';
import * as path from 'path';

import type { BrowserContext, Page, TestInfo } from '../runtime';

/** Options for {@link attachScreenshot}. */
export interface ScreenshotOptions {
  /** Capture the whole scrollable page rather than the viewport. Default true. */
  fullPage?: boolean;
  /** Image format. Default `'png'`. */
  type?: 'png' | 'jpeg';
  /** JPEG quality (ignored for png). */
  quality?: number;
}

/**
 * Screenshot the page and attach it under `name`.
 *
 * @returns The written file path, or `null` when capture failed.
 */
export async function attachScreenshot(
  page: Page,
  testInfo: TestInfo,
  name: string,
  options: ScreenshotOptions = {},
): Promise<string | null> {
  const { fullPage = true, type = 'png', quality } = options;
  try {
    const target = testInfo.outputPath(`${name}.${type}`);
    await page.screenshot({ path: target, fullPage, type, ...(quality != null ? { quality } : {}) });
    await testInfo.attach(name, { path: target, contentType: type === 'png' ? 'image/png' : 'image/jpeg' });
    return target;
  } catch {
    return null;
  }
}

/**
 * Attach an existing file (download, generated report, fixture output) to the test.
 *
 * @returns The attached path, or `null` when the file was missing or attach failed.
 */
export async function attachFile(
  testInfo: TestInfo,
  name: string,
  filePath: string,
  contentType = 'application/octet-stream',
): Promise<string | null> {
  try {
    if (!fs.existsSync(filePath)) return null;
    await testInfo.attach(name, { path: filePath, contentType });
    return filePath;
  } catch {
    return null;
  }
}

/** Options for {@link startTrace}. */
export interface TraceOptions {
  /** Record screenshots into the trace. Default true. */
  screenshots?: boolean;
  /** Record DOM snapshots into the trace. Default true. */
  snapshots?: boolean;
  /** Include test sources in the trace. Default false. */
  sources?: boolean;
  /** Trace title shown in the viewer. */
  title?: string;
}

/**
 * Start a Playwright trace on `context`, for a project that manages tracing itself
 * rather than through `use.trace`. Best-effort.
 *
 * @returns Whether tracing actually started (it is already running when
 *   `use.trace` is configured, in which case this is a no-op returning `false`).
 */
export async function startTrace(context: BrowserContext, options: TraceOptions = {}): Promise<boolean> {
  const { screenshots = true, snapshots = true, sources = false, title } = options;
  try {
    await context.tracing.start({ screenshots, snapshots, sources, ...(title ? { title } : {}) });
    return true;
  } catch {
    return false;
  }
}

/** Stop the trace and attach the `.zip` under `name`. Best-effort. */
export async function stopTraceAndAttach(
  context: BrowserContext,
  testInfo: TestInfo,
  name = 'trace',
): Promise<string | null> {
  try {
    const target = testInfo.outputPath(`${name}.zip`);
    await context.tracing.stop({ path: target });
    await testInfo.attach(name, { path: target, contentType: 'application/zip' });
    return target;
  } catch {
    return null;
  }
}

/**
 * Attach the page's recorded video, when `use.video` produced one. Best-effort;
 * returns `null` when video recording is off.
 */
export async function attachVideo(page: Page, testInfo: TestInfo, name = 'video'): Promise<string | null> {
  try {
    const video = page.video();
    if (!video) return null;
    const source = await video.path();
    const target = testInfo.outputPath(`${name}${path.extname(source) || '.webm'}`);
    await video.saveAs(target);
    await testInfo.attach(name, { path: target, contentType: 'video/webm' });
    return target;
  } catch {
    return null;
  }
}
