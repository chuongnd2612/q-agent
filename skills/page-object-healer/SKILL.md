---
name: page-object-healer
description: Repair the shared automation library when a spec fails because of a defect inside a page object, component object or fixture it imports — a stale locator, a wrong wait, a drifted route. Use in the self-heal loop, before regenerating the spec, whenever the failing spec imports project library files.
version: 1.0.0
author: Andrew
---

# Page Object Healer

## Purpose

A layered spec is a thin sequence of business steps; the locators live in the page objects it imports.
So when such a spec fails on "locator not found", **the defect is usually not in the spec** — it is
inside `pages/LoginPage.ts`. Your job is to fix it *there*.

This exists because the alternative is much worse. A fixer that can only rewrite the spec has exactly
one way to make a stale page-object locator pass: copy the locator back into the spec and stop calling
the page object. That "fix" passes, and it destroys the architecture — and the next ticket's spec, still
importing the unrepaired page object, fails the same way. You are the stage that makes the correct fix
possible.

## What you may change

You run with the automation project root as your working directory. The prompt names the **exact files
you may write**: the library files the failing spec imports, and nothing else.

```text
<project root>/
+-- pages/          Page Objects       ← fixable, if the prompt lists the file
+-- components/     Component Objects  ← fixable, if the prompt lists the file
+-- fixtures/       Fixtures           ← fixable, if the prompt lists the file
+-- data/  utils/  config/             ← fixable, if the prompt lists the file
+-- tests/          Specs              ← NEVER. Not yours, in any circumstance.
+-- package.json  tsconfig.json  playwright.config.ts   ← NEVER.
```

Writing any path the prompt did not list — a new page object, a helper, a spec, a config — reverts your
entire edit with `git reset --hard`, and the heal loop falls back to rewriting the spec. Same for
`tests/`: **you never edit the failing spec.** A different stage does that.

Read every file you intend to change, in full, before changing it.

## What a repair looks like

You are fixing a *defect*, not redesigning. Change the smallest thing that explains the failure:

- **A stale locator.** The page changed; the selector no longer matches. Replace it with a robust one —
  `getByRole` / `getByLabel` / `getByTestId` over raw CSS — grounded in the captured DOM the prompt
  gives you. That DOM is the real rendered page: prefer a value you can actually see in it over a
  guess.
- **A wrong or missing wait.** Replace it with a web-first assertion or an explicit state wait. Never
  add `waitForTimeout(...)`; a hard-coded sleep is not a repair.
- **A drifted route.** Correct the path the page object navigates to.
- **A wrong assertion inside the page object** — but only when the *page* legitimately changed, never
  to make a failing check pass. See below.

## The two hard limits

### 1. Signatures are frozen

You may rewrite the **body** of an existing method — that is the whole point of this stage. You may
**not** change its surface:

- never delete or rename an exported method, class or function;
- never change a method's parameter list;
- adding a new method is fine, if a repair genuinely needs one.

Other tickets' specs already call these methods by name and arity. A removed or re-signed method reverts
your entire edit. This is checked mechanically.

### 2. Assertions may move, never disappear

The total number of assertions across the failing spec **plus** every library file it imports must not
go down. That total is counted before and after you run.

- Moving an assertion out of the spec and into the page object it belongs to is **fine** — the total is
  unchanged, and that is good design (doc §14: UI assertions belong with the page).
- Deleting an assertion, loosening `toHaveText('Welcome, Ana')` to `toBeVisible()`, commenting a check
  out, or swapping a real expectation for a trivially-true one is **not** a repair. It is the failure
  mode this gate exists to catch, and it reverts your edit.

If the only way you can see to make the test pass is to check less, then **do not edit anything**. Say
so in your final message: the app is probably wrong, not the test, and a different stage handles that.

## Prefer updating over duplicating (doc §21, §22)

When the corrected locator belongs to a page object that already exists, **fix that page object**. Do
not add a second locator for the same element somewhere else, do not create a parallel
`LoginPageV2.ts`, and do not leave the stale locator behind next to the new one. One element, one
locator, in the page object that owns the screen. The next generation then inherits the fix for free —
that inheritance is the reason this stage is worth its cost.

## Design rules still apply (doc §14, §15, §16)

A repair must not smuggle in a design regression:

- Page objects hold locators, interactions, navigation and page-level UI assertions — **not** whole
  business cases, environment config, base URLs, or generic logging/screenshot/wait/retry
  infrastructure.
- UI reused across pages belongs in a component object, not copied into a second page object.
- Never author auth/session/login plumbing — `@q-agent/playwright-base` owns it. If the failure is a
  login failure, it is not yours to fix.
- Import `test`/`expect`, assertion helpers, waits and dynamic-data generators from
  `'@q-agent/playwright-base'`; import `Page`/`Locator` types from `'@playwright/test'`.
- Library files sit one level below the project root, so a sibling import is `../pages/Foo`. `../../`
  is spec depth and must not appear in a library file.

## After you finish

The whole project is re-checked, not just the file you touched: every spec in `tests/` must still
collect (`playwright test --list`), the project must still typecheck (`tsc --noEmit`), every
pre-existing signature must still exist unchanged, and the import-spanning assertion count must not have
dropped. Any one of those failing rolls back everything you wrote.

Finish with a short plain-text summary: one line per file, naming the defect you found and what you
changed. If you concluded that nothing in the library was wrong, write that instead of editing — an
honest "the page object looks correct, the app appears to have changed behaviour" is a useful result and
costs the loop nothing.
