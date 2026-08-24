---
name: live-authoring
description: Author a runnable Playwright + TypeScript spec by FIRST driving the real app live with the browser-harness CLI — performing the test case's steps against a real browser, discovering the real selectors on the live DOM, creating any missing test data — and only then emitting a self-contained spec built from what actually worked. Use for the live-authoring execution mode (#400), instead of generating a spec blind and healing it afterwards.
version: 1.1.0
---

# Live Authoring

## Purpose

Produce a single, runnable, self-contained Playwright + TypeScript spec for **one approved manual
test case** — but instead of writing it blind from the Knowledge Base and healing the failures,
**drive the real application first** with the `browser-harness` CLI: actually perform each step
against a live browser, discover the *real* selectors on the *real* DOM, create any test data the
case needs, confirm each expected result — and only then emit the spec, built from exactly what
worked. The result is a clean, deterministic Playwright spec grounded in runtime-verified selectors,
so it should run green with no heal pass.

## How you drive the browser

A dedicated, already-authenticated Chrome is running and `browser-harness` is pre-wired to it (the
`BU_CDP_URL` environment variable points at it — you do **not** configure any connection, open
`chrome://inspect`, start a daemon, or pick a profile). Just run the CLI with a heredoc:

```bash
browser-harness <<'PY'
new_tab("<base URL from context>")
wait_for_load()
print(page_info())
PY
```

- A tab is **already open and signed in** to the app under test (its session was pre-restored). **Attach to it with `ensure_real_tab()` first** and continue from there — do **not** open a fresh `new_tab(url)` for the app's own origin (a new tab may not carry the restored session). Use `new_tab(url)` only for a genuinely different site. After any navigation call `wait_for_load()`.
- **Find elements via the accessibility tree, then verify** — do not guess. `cdp("Accessibility.getFullAXTree")["nodes"]` has every element's `role`, `name`, and `backendDOMNodeId` (filter in Python before printing — it is large). To click: resolve the box center and `click_at_xy(x, y)`, then confirm with a targeted `js(...)` / `page_info()` check. Use `js(...)` for DOM inspection/extraction (e.g. read a `data-testid`, an input's label, the visible text of a result).
- The Chrome is already signed in via its persistent profile. If you unexpectedly hit a login wall, use available SSO if Chrome is already signed in; **never** type passwords/MFA yourself, and **never** run against a production environment.

## Record the REAL selector for every interaction

This is the whole point. As you perform each step, capture the concrete, stable selector that
actually located the element on the live page — you will bake these exact selectors into the spec.
For each element you interact with or assert on, determine and record the **highest-priority stable
selector that exists on the live DOM**, in this order:

1. `data-testid` (or `data-test`) → Playwright `getByTestId('…')` / `[data-testid="…"]` — **strategy `data-testid`**
2. ARIA role + accessible name → `getByRole('button', { name: '…' })` — **strategy `role`**
3. Associated label → `getByLabel('…')` — **strategy `label`**
4. A stable CSS selector (id, unique attribute) — **strategy `css`** (last resort; never `:nth-child`, bare classes, or DOM-structure combinators)

Read the element's real attributes live (via `js(...)` or the AX tree) to choose — do not assume a
`data-testid` exists; confirm it does before using it.

**Verify the SELECTOR, not the pixel.** `click_at_xy(x, y)` lands on whatever element sits at that
coordinate — often an inner `<a>` — while the spec will click the *selector you recorded*, whose
centre may be a container the app ignores. Proving the coordinate works proves nothing about the
locator. So for every interaction, and above all for one that must NAVIGATE, dispatch the click on
the recorded selector itself and confirm the effect:

```
js("document.querySelector('[data-testid=\"employer-row\"]').click()")   # the selector the spec will use
wait_for_load(); page_info()                                              # did the URL actually change?
```

If the recorded selector does nothing, it is the WRONG selector: find the descendant that does
(usually the row's link — `js("...closest('a')")` or the AX tree's `link` node), record THAT, and
re-verify. A step whose navigation you have not seen happen through the emitted selector is not
verified, and the spec's next `toHaveURL` will fail while the click itself silently "passes" —
Playwright's `click()` succeeds whenever it clicks *something*.

## Create test data if it does not exist

If a step depends on data that is not present (e.g. the case acts on "an existing draft claim" and
there is none, or needs a named record that isn't there), **create it live through the UI first**,
then continue. Record the exact values you created (names, ids, field values). Bake those created
values (and the setup actions) into the emitted spec — via a setup section at the top of the
`test()` (or a `test.beforeAll` / creation steps) — so the spec is **self-sufficient on re-run** and
does not silently depend on data that happens to exist today. Prefer values that are safe to
re-create idempotently (e.g. a unique suffix) where the app allows it. Never mutate or delete data
you did not create.

## Reuse the project's shared library — the AUTOMATION PLAN block

The task prompt may carry a `## Shared library` section holding an **AUTOMATION PLAN for this
feature**. When it is there, it is the **exhaustive authorization** for what this spec may import,
and it is accurate: the plan's `create`/`extend` page objects were already authored **server-side**,
into the real project, *before* you were handed this job — so every path listed as `IMPORTABLE`
really exists on disk.

- **Import every `IMPORTABLE` file that covers a step you perform, and drive the UI through it**
  rather than repeating its locators in the spec. Call **only** the method signatures the block
  lists, at the real spec depth (`../../pages/Foo`, `../../utils/bar` — the spec lands two levels
  down, in `tests/<TICKET-ID>/`).
- **Import nothing else** from `../../pages/`, `../../components/`, `../../fixtures/`, `../../data/`
  or `../../utils/`. An unlisted import either does not resolve (failing the project-wide
  `playwright test --list` gate) or duplicates an asset the plan says another file owns — both
  reject the spec.
- **`NOT ON DISK`** entries were planned but their authoring did not land. For *those* steps only,
  a live-verified inline locator is the accepted exception.
- **Never write an asset file yourself.** The plan's `writable` paths belong to the server-side
  page-object author; your deliverables are exactly the two files the task prompt names. Do not
  create or edit anything under `pages/`, `components/`, `fixtures/`, `data/` or `utils/`.

With no plan block in the prompt (legacy path, or a project with no library yet), fall back to the
rule below: import a shared project file only when a reference spec proves it exists, and otherwise
keep the live-verified locators inline.

## Emit the spec — same layered contract as automation-generator

After every step and expected result has been confirmed live, **write both files at exactly the paths
given to you in the task prompt** — do not invent your own filenames. The spec is then placed in the
project's automation project at its planned path, `tests/<TICKET-ID>/<TICKET-ID>-<CASE>.spec.ts` —
**two levels below the project root** — so write every relative import for *that* location
(`../../pages/Foo`), not for the directory you are writing in:

1. **The spec** — a `*.spec.ts` following `templates/playwright-spec.ts`:
   - `import { test, expect } from '@q-agent/playwright-base';` — the shared base framework, which
     also exports the web-first assertion helpers (`expectVisible`, `expectText`, `expectUrl`, …),
     waits (`waitFor`, `retry`, `withTimeout`) and data generators (`uniqueSuffix`, `randomEmail`, …).
     **Never import `@playwright/test` directly**, and never reimplement what the base package gives
     you.
   - **The AUTOMATION PLAN block decides your project imports** (see the section above): import
     every `IMPORTABLE` asset that covers a step, at the real depth (`../../pages/Foo`), and nothing
     else. With **no** plan block, do not import a Page Object or project fixture unless you can see
     that file — i.e. it appears as an import in a reference spec you were given — because an import
     that does not resolve fails the project-wide `playwright test --list` gate and rejects the spec;
     keep the live-verified locators inline instead.
   - **One `test()`**, its title a plain quoted string (no `${...}`) prefixed with the **Test Case ID**
     (e.g. `test('TC-01 — …', async ({ page }) => { … })`) so results trace back. A file with only a
     `test.describe(...)` and no `test()` inside is rejected.
   - **No inline login.** The `test` you import carries the run's saved manual-login session
     (storageState + `sessionStorage` replay), so the spec starts authenticated — navigate straight to
     the route the case starts on. Only a case whose subject IS authentication drives the login form,
     and then via `formLoginFlow` / `performFormLogin` with the real credentials from the injected
     context. **Never mock or bypass auth** — no route-mocking of identity/session endpoints, no
     `VITE_BYPASS_AUTH`, no fabricated `storageState`, no "Auth note" prose.
   - Use the **real selectors you verified live** (with the strategy priority above). Bake in the
     real base URL, route TEMPLATES, and any test data you created.
   - **Never bake in an id the app generated at runtime.** A record's own id (`/employers/57da884a-…`)
     is not test data you control — it is whatever the app minted, and asserting it couples the spec
     to one row existing today. Derive it instead, or assert the SHAPE:

     ```ts
     // NO — a discovered GUID frozen into a constant
     await expect(page).toHaveURL(`${BASE}/employers/57da884a-751d-c6c3-4718-3a21ee0356f2`);
     // YES — assert you navigated to *a* detail page
     await expect(page).toHaveURL(/\/employers\/[0-9a-f-]{36}$/);
     // …or capture what the app gave you, then use that
     const employerId = new URL(page.url()).pathname.split('/').pop();
     ```

     Same rule for choosing the row: after a search identified a specific record, click the row that
     MATCHES it (`.filter({ hasText: EMPLOYER_NAME })`), never `.nth(0)` — index after a search
     assumes an ordering the app never promised.
   - Every "Expected Result" becomes a **web-first assertion** (`await expect(locator).toBeVisible()`,
     `.toHaveText(…)`, `.toHaveURL(…)`, or a base helper such as `expectVisible(…)`) — rely on
     auto-waiting. **No `page.waitForTimeout(...)`** or any hard sleep. Deterministic and independent —
     no shared mutable state.

2. **The discovery sidecar** — unchanged: a JSON file with exactly this shape, listing the runtime-verified
   routes and selectors you actually used, so they can be merged into the Knowledge Base:

```json
{
  "routes": [{ "path": "/claims/new", "description": "New claim form" }],
  "selectors": [
    { "screen": "New claim", "element": "Submit button", "selector": "getByRole('button', { name: 'Submit' })", "strategy": "role" },
    { "screen": "New claim", "element": "Amount field", "selector": "[data-testid=\"amount\"]", "strategy": "data-testid" }
  ]
}
```

## RUN the spec you just wrote — before you report anything

Driving the app successfully is **not** evidence that the spec passes. You drove it with CDP calls;
the spec runs as Playwright code with the locators you recorded, which is a different execution path
— that gap is where live-authored specs fail on their very first real run (#657). The only thing that
settles it is running the file.

**You do not run it yourself — the agent does, on the real execution path.** As soon as you finish,
your spec is executed with the same Playwright config, authenticated session and CLI that a normal
run uses. Do not try to invoke Playwright by hand: only the agent knows where its bundled CLI and the
staged project live, so a hand-rolled command fails on the ENVIRONMENT and tells you nothing about
your spec.

What that means for you:

- **If it passes**, you are done.
- **If it fails, you will be handed the Playwright output and asked to fix the spec in place.** Read
  the failure literally. A `toHaveURL` failure immediately after a click almost always means the
  click navigated nowhere — so the click TARGET is wrong, not the assertion. Go back to the live
  page, find the element that really navigates (usually a link inside the row), verify it through the
  locator the spec will use, and emit THAT. Never "fix" a failure by loosening an assertion you
  cannot satisfy, and never delete the step that fails.
- **If it genuinely cannot pass** (a real product defect, an impossible step, or the budget runs
  out): say so plainly in the summary. A spec reported as authored but never seen to pass is exactly
  what this section exists to prevent.

## Final output

After writing both files, respond with a short plain-text summary: which steps you performed, any
test data you created, the result of the `playwright test` run above, and the pass/fail of each
expected result. If you could **not** make the test
pass live (e.g. a genuine product defect, or a step is impossible), do not fabricate a passing spec —
say so clearly in the summary and still write the discovery sidecar with whatever you verified.

## Quality rules (carry over from automation-generator)

- Layered spec: import from `@q-agent/playwright-base`, never `@playwright/test`; import a shared
  project file when the AUTOMATION PLAN lists it as `IMPORTABLE` (or, with no plan block, when a
  reference spec proves it exists), at the real depth (`../../pages/…`) — and never write one.
- No inline login — the imported `test` supplies the authenticated session.
- Locator priority `data-testid` → role → label → CSS; never brittle CSS/`:nth-child`.
- Every assertion maps to a specific Expected Result and is web-first (auto-waiting); no hard waits.
- Never mock/bypass auth; no auth-note meta-commentary — keep comments to brief step annotations.
- Reference the Test Case ID in the `test()` title.
- Use the REAL, live-verified selectors and REAL created/known data — no invented selectors, routes,
  or placeholders.
