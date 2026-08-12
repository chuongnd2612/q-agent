/**
 * DOM evidence capture (doc §9 `evidence/`).
 *
 * Extracted verbatim in behaviour from the generated fixtures module that Q-Agent
 * injects today — `playwright_runner._fixtures_ts` (server) and
 * `playwrightConfig.fixturesTs` (Local Agent). Both wrote the same inline
 * `page.evaluate` distiller; this is that logic, once, as real typed code.
 *
 * Two artefacts, always best-effort and never able to fail a test:
 *
 * * **distilled** (`qagent-dom-distilled`) — a compact inventory of interactable
 *   elements. This is what the self-heal loop grounds on, so it is captured FIRST
 *   and retried once after a short settle: a transiently-busy failure page would
 *   otherwise leave the fixer with no DOM at all (#398).
 * * **raw** (`qagent-dom-raw`) — the full page HTML. Large and unused by heal, so
 *   intermediate heal attempts switch it off.
 */

import * as fs from 'fs';

import type { Page, TestInfo } from '../runtime';

/** One interactable element in a {@link DomSnapshot}. */
export interface DistilledElement {
  tag: string;
  role?: string;
  testId?: string;
  id?: string;
  name?: string;
  text?: string;
  placeholder?: string;
  type?: string;
}

/** A distilled snapshot of the live page. */
export interface DomSnapshot {
  path: string;
  url: string;
  elements: DistilledElement[];
}

/** Attachment names Q-Agent's evidence pipeline recognises. */
export const ATTACHMENT_NAMES = {
  domDistilled: 'qagent-dom-distilled',
  domRaw: 'qagent-dom-raw',
  network: 'qagent-network',
  console: 'qagent-console',
} as const;

/** Selector for "things a test could interact with or target". */
const INTERACTABLE_SELECTOR = 'a,button,input,select,textarea,[role],[data-testid],[data-test],[id]';

/** Hard caps so a huge page cannot blow up the attachment. */
const MAX_ELEMENTS = 400;
const MAX_TEXT_CHARS = 80;

/**
 * Distil the live page into a {@link DomSnapshot} in a single `page.evaluate`.
 *
 * Returns `null` instead of throwing when the page is closed or navigating.
 */
export async function distillDom(page: Page): Promise<DomSnapshot | null> {
  if (page.isClosed()) return null;
  try {
    return await page.evaluate(
      ({ selector, maxElements, maxTextChars }) => {
        const elements = Array.from(document.querySelectorAll(selector))
          .slice(0, maxElements)
          .map((node) => {
            const el = node as HTMLElement;
            const text = (el.innerText || '').trim().slice(0, maxTextChars);
            return {
              tag: el.tagName.toLowerCase(),
              role: el.getAttribute('role') || undefined,
              testId: el.getAttribute('data-testid') || el.getAttribute('data-test') || undefined,
              id: el.id || undefined,
              name: el.getAttribute('name') || undefined,
              text: text || undefined,
              placeholder: el.getAttribute('placeholder') || undefined,
              type: el.getAttribute('type') || undefined,
            };
          });
        return { path: location.pathname, url: location.href, elements };
      },
      { selector: INTERACTABLE_SELECTOR, maxElements: MAX_ELEMENTS, maxTextChars: MAX_TEXT_CHARS },
    );
  } catch {
    return null;
  }
}

/**
 * {@link distillDom} with the retry that self-heal depends on: up to `attempts`
 * tries, settling `settleMs` between them, stopping as soon as a snapshot has at
 * least one element (#398).
 */
export async function captureDomSnapshot(
  page: Page,
  options: { attempts?: number; settleMs?: number } = {},
): Promise<DomSnapshot | null> {
  const { attempts = 2, settleMs = 400 } = options;
  let snapshot: DomSnapshot | null = null;
  for (let i = 0; i < attempts; i++) {
    if (page.isClosed()) break;
    snapshot = await distillDom(page);
    if (snapshot && Array.isArray(snapshot.elements) && snapshot.elements.length > 0) break;
    try {
      await page.waitForTimeout(settleMs);
    } catch {
      break;
    }
  }
  return snapshot;
}

/**
 * Write `data` as JSON into the test's output dir and attach it. Best-effort: any
 * failure is swallowed so evidence capture can never fail a test.
 *
 * @returns The written path, or `null` when the write/attach failed.
 */
export async function attachJson(
  testInfo: TestInfo,
  name: string,
  data: unknown,
  fileName = `${name}.json`,
): Promise<string | null> {
  try {
    const target = testInfo.outputPath(fileName);
    fs.writeFileSync(target, JSON.stringify(data), 'utf-8');
    await testInfo.attach(name, { path: target, contentType: 'application/json' });
    return target;
  } catch {
    return null;
  }
}

/** Write `text` into the test's output dir and attach it. Best-effort. */
export async function attachText(
  testInfo: TestInfo,
  name: string,
  text: string,
  fileName = `${name}.txt`,
  contentType = 'text/plain',
): Promise<string | null> {
  try {
    const target = testInfo.outputPath(fileName);
    fs.writeFileSync(target, text, 'utf-8');
    await testInfo.attach(name, { path: target, contentType });
    return target;
  } catch {
    return null;
  }
}

/** Capture + attach the distilled DOM inventory as `qagent-dom-distilled`. */
export async function attachDomSnapshot(
  page: Page,
  testInfo: TestInfo,
  options: { attempts?: number; settleMs?: number } = {},
): Promise<DomSnapshot | null> {
  const snapshot = await captureDomSnapshot(page, options);
  if (!snapshot) return null;
  await attachJson(testInfo, ATTACHMENT_NAMES.domDistilled, snapshot, 'qagent-dom-distilled.json');
  return snapshot;
}

/** Capture + attach the full page HTML as `qagent-dom-raw`. Best-effort. */
export async function attachRawDom(page: Page, testInfo: TestInfo): Promise<string | null> {
  try {
    const raw = await page.content();
    return await attachText(testInfo, ATTACHMENT_NAMES.domRaw, raw, 'qagent-dom-raw.html', 'text/html');
  } catch {
    return null;
  }
}
