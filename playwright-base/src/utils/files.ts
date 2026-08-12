/**
 * File helpers, including upload/download (doc §9 `utils/`).
 */

import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

import type { Download, Locator, Page } from '../runtime';

/** Create `dir` (and parents) if absent; returns `dir`. */
export function ensureDir(dir: string): string {
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

/** Read and parse a JSON file, returning `fallback` on any failure. */
export function readJson<T>(file: string, fallback: T): T {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf-8')) as T;
  } catch {
    return fallback;
  }
}

/** Write `value` as pretty JSON, creating parent dirs. */
export function writeJson(file: string, value: unknown): void {
  ensureDir(path.dirname(file));
  fs.writeFileSync(file, JSON.stringify(value, null, 2), 'utf-8');
}

/** Read a UTF-8 text file, returning `fallback` on any failure. */
export function readText(file: string, fallback = ''): string {
  try {
    return fs.readFileSync(file, 'utf-8');
  } catch {
    return fallback;
  }
}

/** Write a UTF-8 text file, creating parent dirs. */
export function writeText(file: string, content: string): void {
  ensureDir(path.dirname(file));
  fs.writeFileSync(file, content, 'utf-8');
}

/** Create a temp file with `content` and return its path. Useful for upload tests. */
export function createTempFile(content: string | Buffer, fileName?: string): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'qagent-'));
  const target = path.join(dir, fileName ?? `upload-${Date.now()}.txt`);
  fs.writeFileSync(target, content);
  return target;
}

/** Set files on a file input (or any locator resolving to one). */
export async function uploadFiles(input: Locator, files: string | string[]): Promise<void> {
  await input.setInputFiles(files);
}

/**
 * Trigger a download and save it.
 *
 * @param page The page the download originates from.
 * @param trigger The action that starts the download (e.g. clicking a button).
 * @param targetDir Directory to save into; created if absent.
 * @returns The saved absolute path and the Playwright `Download`.
 */
export async function downloadTo(
  page: Page,
  trigger: () => Promise<unknown>,
  targetDir: string,
): Promise<{ path: string; download: Download }> {
  const [download] = await Promise.all([page.waitForEvent('download'), trigger()]);
  ensureDir(targetDir);
  const target = path.join(targetDir, download.suggestedFilename());
  await download.saveAs(target);
  return { path: target, download };
}

/** Whether `file` exists and has non-zero size. */
export function fileHasContent(file: string): boolean {
  try {
    return fs.existsSync(file) && fs.statSync(file).size > 0;
  } catch {
    return false;
  }
}
