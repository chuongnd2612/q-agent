/**
 * Console + network telemetry capture (doc §9 `evidence/`) — extracted from the
 * injected fixtures module (#456).
 *
 * Listeners must be registered BEFORE the test body runs or early requests and the
 * app's boot-time console output are lost, so {@link createTelemetryRecorder} is
 * called at fixture setup and {@link TelemetryRecorder.attach} after the test body,
 * pass or fail. The attachments are parsed into the run's
 * `console_logs`/`network_logs` columns.
 */

import type { Page, TestInfo } from '../runtime';
import { ATTACHMENT_NAMES, attachJson } from './dom';

/** One captured request/response pair. */
export interface NetworkEntry {
  method: string;
  url: string;
  /** HTTP status, or `0` for a failed request. */
  status: number;
  durationMs: number;
  /** Present and `true` only for `requestfailed`. */
  failed?: boolean;
}

/** One captured console message or uncaught page error. */
export interface ConsoleEntry {
  /** Playwright console message type, or `'error'` for a `pageerror`. */
  level: string;
  text: string;
}

/** Caps matching the injected fixtures module, so evidence stays bounded. */
export interface TelemetryOptions {
  /** Max network entries retained. Default 300. */
  maxNetworkEntries?: number;
  /** Max console entries retained. Default 500. */
  maxConsoleEntries?: number;
  /** Max characters per console text. Default 2000. */
  maxTextChars?: number;
}

/** A live telemetry recorder attached to one page. */
export interface TelemetryRecorder {
  /** Network entries captured so far (live array). */
  readonly network: NetworkEntry[];
  /** Console entries captured so far (live array). */
  readonly console: ConsoleEntry[];
  /** Attach both as `qagent-network` / `qagent-console`. Best-effort. */
  attach(testInfo: TestInfo): Promise<void>;
}

/**
 * Start capturing console + network telemetry for `page`.
 *
 * Every listener body is wrapped in try/catch — telemetry must never be able to
 * fail a test.
 */
export function createTelemetryRecorder(page: Page, options: TelemetryOptions = {}): TelemetryRecorder {
  const { maxNetworkEntries = 300, maxConsoleEntries = 500, maxTextChars = 2000 } = options;
  const network: NetworkEntry[] = [];
  const consoleEntries: ConsoleEntry[] = [];
  const startedAt = new Map<unknown, number>();

  page.on('request', (request) => {
    try {
      startedAt.set(request, Date.now());
    } catch {
      /* ignore */
    }
  });
  page.on('response', (response) => {
    try {
      if (network.length >= maxNetworkEntries) return;
      const request = response.request();
      const t0 = startedAt.get(request);
      network.push({
        method: request.method(),
        url: request.url(),
        status: response.status(),
        durationMs: t0 ? Date.now() - t0 : 0,
      });
    } catch {
      /* ignore */
    }
  });
  page.on('requestfailed', (request) => {
    try {
      if (network.length >= maxNetworkEntries) return;
      const t0 = startedAt.get(request);
      network.push({
        method: request.method(),
        url: request.url(),
        status: 0,
        durationMs: t0 ? Date.now() - t0 : 0,
        failed: true,
      });
    } catch {
      /* ignore */
    }
  });
  page.on('console', (message) => {
    try {
      if (consoleEntries.length >= maxConsoleEntries) return;
      consoleEntries.push({ level: message.type(), text: String(message.text()).slice(0, maxTextChars) });
    } catch {
      /* ignore */
    }
  });
  page.on('pageerror', (error) => {
    try {
      if (consoleEntries.length >= maxConsoleEntries) return;
      consoleEntries.push({ level: 'error', text: String((error && error.message) || error).slice(0, maxTextChars) });
    } catch {
      /* ignore */
    }
  });

  return {
    network,
    console: consoleEntries,
    async attach(testInfo: TestInfo): Promise<void> {
      await attachJson(testInfo, ATTACHMENT_NAMES.network, network, 'qagent-network.json');
      await attachJson(testInfo, ATTACHMENT_NAMES.console, consoleEntries, 'qagent-console.json');
    },
  };
}
