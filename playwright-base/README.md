# `@q-agent/playwright-base`

The shared **Playwright base framework** for Q-Agent's generated automation projects —
Layer 2 of the layered automation architecture
(`docs/QAgent_Playwright_Automation_Architecture_Update.md` §9).

It holds the generic automation infrastructure that Q-Agent must **never regenerate per
feature**: fixtures, evidence capture, authentication state, API helpers, logging, wait
and test-data utilities, and web-first assertion helpers.

```ts
import { test, expect } from '@q-agent/playwright-base';

test('TC-01 — dashboard loads', async ({ page }) => {
  await page.goto('/dashboard');
  await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible();
});
```

`test` is Playwright's `test` extended with always-on evidence capture (and optional
`sessionStorage` replay). It is a drop-in replacement — **specs never import
`@playwright/test` directly.**

## What is NOT in here

Per doc §10, this package must not know how any specific application's UI works. It
contains **no page objects** — no `LoginPage`, `UserPage`, `OrderPage` — no app URLs,
no credentials, and no domain test-data factories. Those live in the application
automation project (Layer 3), which depends on this package.

Generic auth *infrastructure* (storage-state handling, a form-login driver whose
selectors the caller supplies) is in scope. A concrete app's login page object is not.

## Layout

```text
src/
  runtime.ts        the ONLY module that imports '@playwright/test'
  auth/             storage-state + sessionStorage replay, generic form login,
                    authenticated contexts
  fixtures/         the base `test` (evidence + session replay), authenticated test factory
  api/              APIRequestContext-backed API client
  evidence/         distilled + raw DOM capture, console/network telemetry,
                    screenshot / trace / video / file attachments
  logging/          structured JSON-line automation logging
  utils/            wait/retry, files (upload/download), dates, random test data
  assertions/       shared web-first assertion helpers
  config/           environment helpers
  version.ts        BASE_VERSION + compatibility check
  index.ts          the public surface
```

### `runtime.ts` is the only `@playwright/test` import site

This is a hard architectural rule, enforced by `npm run check:runtime` (wired into
`prebuild`):

* Q-Agent's execution hosts rewrite `'@playwright/test'` import specifiers when staging
  a run (`playwright_runner._apply_fixtures` on the server,
  `playwrightConfig.applyFixtures` in the Local Agent), and #540 makes that rewrite
  depth-aware. One import site is what keeps it tractable.
* `@playwright/test` is a **peer dependency**, provided by the execution host via
  `NODE_PATH`. One import site means one resolution site to reason about.

Equivalent manual check: `grep -rn "@playwright/test" src/` must match only
`src/runtime.ts`.

## Provenance — this is an extraction, not a new invention

The evidence fixtures were duplicated in two places before this package existed, and
are ported from both:

* `api/app/services/playwright_runner.py` → `_fixtures_ts` (the server's generated
  `fixtures.ts`)
* `agent/src/playwrightConfig.ts` → `fixturesTs` (the Local Agent's port of the same)

Behaviour preserved verbatim:

* **Distilled DOM first, retried once after a 400 ms settle** (#398) — the self-heal
  loop grounds on the real failure-page DOM, so a transiently-busy page must not leave
  the fixer with nothing.
* **Raw HTML optional** — large and unused by heal, so intermediate heal attempts skip
  it (now `QAGENT_CAPTURE_RAW_DOM=0` instead of a code-generation flag).
* **Console + network listeners registered before the test body** (#456), capped at 500
  / 300 entries, console text truncated to 2000 chars, attached pass or fail.
* **Attachment names unchanged** — `qagent-dom-distilled`, `qagent-dom-raw`,
  `qagent-network`, `qagent-console` — so the server's evidence parsing needs no change.
* **All capture is best-effort** and wrapped in try/catch: evidence must never be the
  reason a test fails.
* **`sessionStorage` replay** via an origin-scoped `addInitScript`, because Playwright's
  `storageState` covers cookies + `localStorage` but not `sessionStorage`, where
  MSAL/SPA tokens live.

The generic form-login driver in `auth/login.ts` replaces the inline-login block that
generated specs repeat today (`skills/automation-generator/SKILL.md` step 4).

## Configuration

Evidence and session replay are Playwright **option fixtures**, so a project can set
them in `playwright.config.ts` (`use: { qagentEvidence: { … } }`) or per file
(`test.use({ … })`). Defaults come from the environment, so an execution host can tune
them without touching code:

| Variable | Default | Effect |
| --- | --- | --- |
| `QAGENT_CAPTURE_DOM` | `true` | Attach the distilled DOM inventory |
| `QAGENT_CAPTURE_RAW_DOM` | `true` | Attach the full page HTML |
| `QAGENT_CAPTURE_TELEMETRY` | `true` | Attach console + network JSON |
| `QAGENT_DISTILL_ATTEMPTS` | `2` | Distill attempts before giving up |
| `QAGENT_DISTILL_SETTLE_MS` | `400` | Settle delay between distill attempts |
| `QAGENT_SESSION_FILE` | *(unset)* | Path to a `sessionStorage.json` snapshot to replay |
| `QAGENT_SESSION_REPLAY` | `true` | Master switch for replay |
| `QAGENT_LOG_LEVEL` | `info` | Minimum level for `createLogger` |
| `QAGENT_BASE_URL` / `BASE_URL` | *(unset)* | `loadEnvironment().baseUrl` |
| `QAGENT_API_URL` / `API_URL` | `baseUrl` | `loadEnvironment().apiUrl` |
| `QAGENT_ENV` / `TEST_ENV` | `local` | `loadEnvironment().name` |
| `QAGENT_HEADLESS` | `true` | `loadEnvironment().headless` |
| `QAGENT_TIMEOUT_MS` | `30000` | `loadEnvironment().timeoutMs` |

## Q-Agent ↔ base-package version compatibility

Q-Agent records the base-package version a project was scaffolded against in
`AutomationProject.base_version` (#538). At execution time the host compares that
against what is actually installed.

* The package follows **semver**, and the compatibility rule is **caret**: same major,
  installed version ≥ scaffolded version.
* `BASE_VERSION`, `isCompatibleWith(required, installed?)` and
  `assertCompatibleWith(required)` are exported for that check.
* A **major** bump means generated projects need regeneration (a removed or reshaped
  export). A **minor** bump only adds surface, so existing projects keep working.

| Q-Agent | `@q-agent/playwright-base` | Notes |
| --- | --- | --- |
| ≥ the release that lands #537 wave 1 | `^1.0.0` | First version. `test`/`expect` re-exported; evidence fixtures extracted from the previously-generated `fixtures.ts`. |

`package.json`, the `VERSION` file and `src/version.ts`'s `BASE_VERSION` are three
copies of one fact (npm, the API's offline fallback, runtime checks). `npm run build`
fails on drift; `node scripts/sync-version.mjs` fixes it.

## Build

```bash
cd playwright-base
npm install
npm run build      # prebuild gates: version sync + single @playwright/test import
```

`dist/` ships JavaScript **plus `.d.ts` declarations** (and source/declaration maps).

## The committed vendored tarball — a deliberate artifact

`vendor/q-agent-playwright-base-1.0.0.tgz` is **checked into git on purpose.**

The API's `ensure_deps` (#538) needs a registry-independent install path: when the npm
registry is unreachable, it installs this exact tarball instead of resolving
`@q-agent/playwright-base` from npm. The path is part of that contract — keep the name
`q-agent-playwright-base-<version>.tgz` under `playwright-base/vendor/`.

Regenerate it after any source or version change:

```bash
cd playwright-base
npm run vendor     # build + npm pack + move into vendor/, pruning stale versions
```

Then commit the refreshed tarball.

## Release — the maintainer publishes, not the assistant

`npm publish` requires an **interactive 2FA OTP**, so it cannot be done headlessly.
Same division of labour as the Local Agent (see the repo `CLAUDE.md`).

**The maintainer runs, from a real shell:**

```bash
cd playwright-base && npm run release
```

That bumps the patch version, syncs `VERSION` + `src/version.ts`, builds, publishes
(prompting for the OTP), and refreshes `vendor/`. Then commit `package.json`,
`VERSION`, `src/version.ts` and the new `vendor/*.tgz`.

For the **first** publish of `1.0.0` — the version already set here — skip the bump:

```bash
cd playwright-base && npm run release -- --no-bump
```

Variants:

| Command | Effect |
| --- | --- |
| `npm run release -- --minor` / `--major` | Bump beyond patch |
| `npm run release -- --otp=123456` | Non-interactive OTP (needs a *fresh* code — TOTP expires in ~30 s) |
| `npm run release -- --no-bump` | Publish the current version as-is |
| `npm run release -- --dry-run` | Everything except the publish |
