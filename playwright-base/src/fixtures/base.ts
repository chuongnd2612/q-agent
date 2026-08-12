/**
 * The base `test` every generated spec uses (doc §9 `fixtures/`).
 *
 * This is the extraction of the fixtures module Q-Agent generates and injects into
 * every run today — `playwright_runner._fixtures_ts` on the server and its port
 * `playwrightConfig.fixturesTs` in the Local Agent. Same behaviour, now shipped as a
 * package instead of a string template:
 *
 * * **Always-on evidence.** One `{ auto: true }` fixture registers console + network
 *   listeners *before* the test body (#456) and, after it, attaches the distilled DOM
 *   inventory (retried once after a settle — #398), optionally the raw HTML, and the
 *   console/network JSON. Everything is best-effort: evidence capture can never fail
 *   a test.
 * * **`sessionStorage` replay.** When a session snapshot path is configured, the
 *   `context` fixture installs the origin-scoped replay init script so an MSAL/SPA
 *   session survives into the run instead of bouncing to the login page.
 *
 * Both are configurable as Playwright **option fixtures** (`test.use({ … })` or
 * `use: { … }` in `playwright.config.ts`), with defaults read from the environment so
 * an execution host can tune them without rewriting code — e.g. intermediate heal
 * attempts set `QAGENT_CAPTURE_RAW_DOM=0` to skip the large, heal-unused raw HTML.
 */

import { baseTest, expect } from '../runtime';
import { applySessionStorageFile } from '../auth/state';
import { attachDomSnapshot, attachRawDom } from '../evidence/dom';
import { createTelemetryRecorder } from '../evidence/telemetry';
import { env, envBool, envInt } from '../config/environment';

/** Evidence-capture knobs, settable per test/project via `test.use({ qagentEvidence })`. */
export interface EvidenceOptions {
  /** Attach the distilled DOM inventory (`qagent-dom-distilled`). Default true. */
  captureDom?: boolean;
  /** Attach the full page HTML (`qagent-dom-raw`). Default `QAGENT_CAPTURE_RAW_DOM` (true). */
  captureRawDom?: boolean;
  /** Attach console + network JSON (`qagent-console` / `qagent-network`). Default true. */
  captureTelemetry?: boolean;
  /** Distill attempts before giving up. Default 2 (#398). */
  distillAttempts?: number;
  /** Settle delay between distill attempts, ms. Default 400. */
  distillSettleMs?: number;
}

/** `sessionStorage`-replay knobs, settable via `test.use({ qagentSession })`. */
export interface SessionReplayOptions {
  /** Path to a `sessionStorage.json` snapshot. Default `QAGENT_SESSION_FILE` (unset = off). */
  file?: string;
  /** Master switch. Default true — with no `file`, replay is a no-op anyway. */
  replay?: boolean;
}

/** Option + internal fixtures added by {@link test}. */
export interface BaseFixtures {
  qagentEvidence: EvidenceOptions;
  qagentSession: SessionReplayOptions;
  /** Internal auto fixture; never referenced by a spec. */
  qagentAutoEvidence: void;
}

const evidenceDefaults: EvidenceOptions = {
  captureDom: envBool('QAGENT_CAPTURE_DOM', true),
  captureRawDom: envBool('QAGENT_CAPTURE_RAW_DOM', true),
  captureTelemetry: envBool('QAGENT_CAPTURE_TELEMETRY', true),
  distillAttempts: envInt('QAGENT_DISTILL_ATTEMPTS', 2),
  distillSettleMs: envInt('QAGENT_DISTILL_SETTLE_MS', 400),
};

const sessionDefaults: SessionReplayOptions = {
  file: env('QAGENT_SESSION_FILE'),
  replay: envBool('QAGENT_SESSION_REPLAY', true),
};

/**
 * The extended `test` all specs import. A drop-in replacement for
 * `@playwright/test`'s `test`, with evidence capture and session replay wired in.
 */
export const test = baseTest.extend<BaseFixtures>({
  qagentEvidence: [evidenceDefaults, { option: true }],
  qagentSession: [sessionDefaults, { option: true }],

  // sessionStorage replay — the injected fixtures module's `context` override.
  context: async ({ context, qagentSession }, use) => {
    const { file, replay = true } = qagentSession ?? {};
    if (replay && file) await applySessionStorageFile(context, file);
    await use(context);
  },

  // Always-on evidence: listeners registered BEFORE the test body so nothing early
  // is missed; attachments written after it, pass or fail.
  qagentAutoEvidence: [
    async ({ page, qagentEvidence }, use, testInfo) => {
      const options = { ...evidenceDefaults, ...(qagentEvidence ?? {}) };
      const telemetry = options.captureTelemetry ? createTelemetryRecorder(page) : null;

      await use();

      // Distilled inventory FIRST — it is what the self-heal loop grounds on (#398).
      if (options.captureDom) {
        await attachDomSnapshot(page, testInfo, {
          attempts: options.distillAttempts,
          settleMs: options.distillSettleMs,
        });
      }
      if (options.captureRawDom) await attachRawDom(page, testInfo);
      if (telemetry) await telemetry.attach(testInfo);
    },
    { auto: true },
  ],
});

export { expect };
