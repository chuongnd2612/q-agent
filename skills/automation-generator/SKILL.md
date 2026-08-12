---
name: automation-generator
description: Generate a runnable Playwright + TypeScript spec from an approved manual test case, layered on the shared @q-agent/playwright-base framework and the project's accumulating asset library, baking in the real base URL, credentials, routes and selectors discovered in the Project Knowledge Base. Use when the user says "automate these test cases", "generate Playwright specs", "write e2e tests for this ticket", or after test cases have been approved by test-case-reviewer.
version: 2.0.0
author: Andrew
---

# Automation Generator

## Purpose

Convert **one approved** Azure DevOps-style manual test case into a single runnable Playwright +
TypeScript spec file inside the project's **persistent automation project**.

This skill never invents application structure. It reads the **Project Knowledge Base**
(`knowledge.md` / `knowledge.json`) and bakes the real base URL, real routes, and real selectors
directly into the generated spec so it runs with no manual fixups. (Real test-account credentials are
supplied too, but a spec only uses them when the case's subject *is* authentication — see below.)

## The Layered Architecture (read this before generating)

Generated specs are **not** standalone files any more. They live in a living automation project that
accumulates shared assets, and they sit on top of a published base framework:

```text
Layer 2  @q-agent/playwright-base   test, expect, auth plumbing, assertion helpers, waits, data
Layer 3  <automation project>/      pages/ components/ fixtures/ data/ utils/ config/
                                    tests/<TICKET-ID>/<TICKET-ID>-<CASE>.spec.ts   ← you write this
```

Two facts about that layout are load-bearing:

1. **The spec is TWO levels below the project root.** A shared project file is imported as
   `../../pages/Foo`, `../../fixtures/app.fixture`, `../../data/users` — **never** `../pages/Foo`.
2. **The gate collects the whole project** (`playwright test --list`), so an import that does not
   resolve is a hard rejection, and a file with only a `test.describe(...)` and no `test()` inside is
   rejected too.

So every generated spec:

- imports `test`, `expect` and any assertion helper from **`@q-agent/playwright-base`** — never from
  `@playwright/test` directly. That `test` is Playwright's `test` extended with always-on evidence
  capture and replay of the run's saved session, so it is a drop-in replacement;
- contains **exactly one `test()` block** for the case, its title a plain quoted string (never a
  template literal with `${...}` in it) prefixed with the Test Case ID;
- reads as **business steps + web-first assertions** — not a recording of browser mechanics;
- **never inlines a login flow** (see *Auth is not your job*, below).

### Page objects: the AUTOMATION PLAN decides what you may import

Locators and low-level UI mechanics belong in a shared page object — that is the **default**, not an
aspiration. Which page objects you may use is not your judgement call: it is decided before you run, by
`automation-planner`, the assets are then authored or extended by `page-object-author`, and the result
is handed to you as the **AUTOMATION PLAN** block in the prompt. That block was verified against the
project's real tree, so it is the whole truth about what exists.

- **Import exactly what the plan lists under "IMPORTABLE", and nothing else** from `../../pages/`,
  `../../components/`, `../../fixtures/`, `../../data/` or `../../utils/`. Call only the signatures the
  plan shows for each of those files, and drive the UI **through** them instead of repeating their
  locators in the spec.
- **An inline locator is the exception.** Take it only for a step whose asset the plan names but does
  **not** list as importable — the plan puts those under "NOT ON DISK", and importing one fails
  collection and rejects the whole spec. Choose such a locator by the project's locator priority.
- **Never invent** `import { LoginPage } from '../../pages/LoginPage'` on the assumption that it
  exists. If the plan doesn't list it, it isn't there.
- A page object / fixture **name** listed in the Project Knowledge Base is *metadata about the product
  repo*, not a file in this automation project. Never turn a KB name into an import path.
- **When there is no plan block at all**, the base-package import is the only legal one: keep every
  locator inline, as a thin readable sequence of steps.

### What the base framework already gives you

Take these from `@q-agent/playwright-base` instead of writing your own:

- `test`, `expect` — the extended test (evidence capture + saved-session replay) and Playwright's
  `expect`.
- Auth plumbing: `createAuthenticatedTest`, `formLoginFlow`, `performFormLogin`, `ensureLoggedIn`,
  `hasStorageState`, `applySessionStorage`.
- Web-first assertion helpers: `expectVisible`, `expectHidden`, `expectText`, `expectContainsText`,
  `expectValue`, `expectChecked`, `expectEnabled`, `expectDisabled`, `expectCount`, `expectAttribute`,
  `expectClass`, `expectUrl`, `expectTitle`, `expectRowVisible`, `expectAllVisible`,
  `expectEventuallyGone`.
- Waits/retry (never a hard sleep): `waitFor`, `retry`, `withTimeout`.
- Dynamic data: `uniqueId`, `uniqueSuffix`, `randomEmail`, `randomString`, `randomInt`, `isoDate`,
  `today`, `addDays`, `daysFromNow`, `formatDate`.
- Files / API / logging / config: `uploadFiles`, `downloadTo`, `readJson`, `writeJson`,
  `createApiClient`, `logger`, `env`, `resolveUrl`.

### Auth is not your job

The run installs its saved manual-login session (storageState + `sessionStorage` replay) via the base
package's `test` fixture and the project's Playwright config, so **the spec starts authenticated**.
Do not re-generate the `goto('/login')` → fill → fill → click preamble; navigate straight to the route
the case starts on. The one exception is a case whose *subject* is authentication (login, logout,
session expiry, permissions) — then drive the real login form with the real credentials from the
injected context, preferably via `formLoginFlow` / `performFormLogin` from the base package.

## Position in the QA Pipeline

```
project-bootstrap
        ↓ knowledge.md + knowledge.json
requirement-analyst → test-case-generator → test-case-reviewer
        ↓ approved test cases
automation-planner
        ↓ plan.json (reuse/extend/create — what you may import)
[automation-generator]  ← you are here
        ↓ Playwright specs
automation-reviewer → execution-analyzer
        → screenshot-annotator / ticket-comment-generator / report-generator
```

## When to Use

- A manual test case exists and has been reviewed/approved by `test-case-reviewer`.
- The user asks to automate a ticket, a test case, or a coverage area.
- A Project Knowledge Base (`knowledge.md` / `knowledge.json`) already exists.

Do **not** use this to design test scenarios — that is `test-case-generator`'s job. This skill
only implements an already-approved case.

## Inputs / Prerequisites

Required:

- **One approved ADO test case** (from `test-case-generator`, reviewed by `test-case-reviewer`), with
  a stable **Test Case ID**, steps, and expected results.
- **`knowledge.md` + `knowledge.json`** from `project-bootstrap`.

From the Knowledge Base, use directly (do not invent):

- The application **base URL** and per-environment URLs (`environments`).
- The real **application routes / URL patterns** discovered in the code (`routes`).
- The real **selectors / data-testids** discovered in the code (`selectors`: screen/element → selector).
- The **login URL and auth flow** (`auth.login_url`, `auth.login_flow`, `auth.storage_state`) — for an
  authentication-subject case only; every other spec consumes the saved session instead.
- The **test-account credentials supplied at generation time** (username + password from the injected
  project context) — reference them directly, but only in an authentication-subject spec.
- The documented **locator strategy** (selector priority order).
- The names of existing **Page Objects / fixtures / utilities** — informational context only (see
  *Page objects: the AUTOMATION PLAN decides what you may import*); never turn a name into an import.

Optional, and authoritative when present:

- The **AUTOMATION PLAN** for this ticket (from `automation-planner`) — the REUSE/EXTEND/CREATE
  decisions, listing the exact project files you may import and the exact signatures you may call.

Some routes/selectors may be stamped `verified_at_runtime` — discovered live by the DOM exploration
agent rather than inferred from source. These are marked `✓ runtime-verified` in the injected project
context and **must be preferred** over source-inferred entries for the same screen/element, using the
verified selector's `strategy`.

If any prerequisite is missing, stop and request that `project-bootstrap` (for the KB) or
`test-case-generator` / `test-case-reviewer` (for the approved case) be run first.

## Workflow

1. **Load context** — parse the approved test case and the Knowledge Base.
2. **Map case → spec** — one `test()` for the case, its title prefixed with the case's **Test Case
   ID** (e.g. `test('TC-01 — Login with valid credentials', async ({ page }) => { ... })`) so results
   trace back through `execution-analyzer`.
3. **Choose locators by KB priority** — use the project's documented order (typically
   `data-testid` → `getByRole` → `getByLabel` → CSS → XPath). Never hard-code brittle selectors
   (raw CSS classes, DOM-structure-dependent combinators, `:nth-child`) when a higher-priority,
   KB-known option exists.
4. **Consume the shared session — do not log in** — the spec starts authenticated (see *Auth is not
   your job*). Only a case whose subject IS authentication drives the login form, and then via the
   base package's `formLoginFlow` / `performFormLogin` with the real credentials.
5. **Bake in real project values** — use the REAL base URL, routes, selectors, login URL and
   test-account credentials from the injected project context DIRECTLY in the spec. Do not invent
   selectors or URLs, and do not emit placeholders, when the context provides them. Emit a
   clearly-marked `// TODO` placeholder only for a value that is genuinely absent from that context.
6. **Assert against expected results** — every "Expected Result" in the case becomes a **web-first
   assertion** (`await expect(locator).toBeVisible()`, `.toHaveText(...)`, or a base-package helper
   such as `expectVisible(...)` / `expectText(...)` / `expectUrl(...)`), relying on Playwright's
   built-in auto-waiting.
7. **No hard waits** — never use `page.waitForTimeout(...)` or other arbitrary sleeps. Web-first
   assertions and auto-waiting locators are the only waiting mechanism; for a genuine non-UI wait use
   the base package's `waitFor` / `retry` / `withTimeout`.
8. **Reuse the base framework** — take `test`, `expect`, assertion helpers, waits and dynamic test-data
   generators from `@q-agent/playwright-base` instead of reimplementing them.
9. **Emit the spec** — one `*.spec.ts` file following `templates/playwright-spec.ts`.

## Output

- One Playwright spec file (`*.spec.ts`) following `templates/playwright-spec.ts`: a single `test()`
  importing from `@q-agent/playwright-base` (plus any shared project file the AUTOMATION PLAN lists as
  importable), tagged with its source Test Case ID.

## Quality Rules

- **Layered, not standalone.** Import `test`/`expect`/helpers from `@q-agent/playwright-base`; never
  from `@playwright/test` directly. Import a shared project file only when the AUTOMATION PLAN lists it
  as importable, and always at the real depth (`../../pages/…`). Never invent an import.
- **Exactly one `test()`** per spec, plain-string title, Test Case ID prefixed. A `test.describe` with
  no `test()` inside is rejected.
- **No inline login.** The saved manual-login session authenticates the spec; only an
  authentication-subject case touches the login form.
- Follow the Knowledge Base's **locator priority** — prefer `data-testid`/role/label; avoid raw
  CSS/XPath, bare class selectors, and `:nth-child`/`:nth-of-type` combinators.
- Each assertion must map to a specific **Expected Result** in the source case, and must be a
  **web-first assertion** (auto-waiting) — never a manual `waitForTimeout`.
- **No hard-coded waits** (`page.waitForTimeout`).
- The spec must be **deterministic and independent** — no shared mutable state, no ordering
  dependencies with other specs.
- Reference the source **Test Case ID** in the `test()` title so failures are traceable.
- **No unresolved placeholders** — bake in the real base URL, routes, selectors and credentials
  from the project context; a `// TODO` is allowed only for a value truly missing from the context.
- **Never mock or bypass auth.** Authentication is handled outside the spec by the run's saved
  manual-login session (storageState) and the real test-account credentials in the project context.
  Test as a real authenticated user — do NOT route-mock identity/session endpoints (e.g.
  `GET /api/sessions/me`, `/api/sessions/permissions`), assume flags like `VITE_BYPASS_AUTH`, or
  fabricate a `storageState`. And do NOT emit meta-commentary / "Auth note" prose explaining auth
  strategy, mocking decisions, or environment assumptions — keep comments to brief step annotations.

## Handoff / Success Criteria

The generated spec collects cleanly inside the automation project (`playwright test --list` over the
whole project) and runs with no manual fixups. It is consumed next by `automation-reviewer` (static
quality review) and then by `execution-analyzer` (runtime results). Success = the approved test case
has a corresponding, traceable, runnable `test()` that imports from `@q-agent/playwright-base`, has no
inline login, uses web-first assertions and no hard waits.

## Self-heal (when a spec you wrote comes back failing)

Fix the defect, not the design. Keep the `@q-agent/playwright-base` import and every import of a
shared project file. **Inlining a page object's locators, a fixture's setup, or a login flow back into
the spec to route around it is a rejection** — even if the flattened spec would pass.

You are not the only stage that can fix things any more. Before you are asked for anything,
`page-object-healer` has already inspected the page objects this spec imports and repaired them if the
defect was in there. So a failure that reaches you is a failure in the **spec's own use** of the
library: a wrong method, a missing step, a bad wait or route, wrong data. Assume the page objects are
correct and fix your side of the boundary.

The assertion count is checked across the spec **plus** the library files it imports, so an assertion
may move into the page object that owns the screen, but it may never disappear.
