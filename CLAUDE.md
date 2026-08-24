# CLAUDE.md — Q-Agent

Project-specific guidelines. Merge with the global `~/.claude/CLAUDE.md`.

## Debugging

- For visual layering/rendering bugs, inspect the live DOM (e.g. `elementFromPoint`, computed styles) to find the actual cause **before** fixing. Don't iterate on opacity/z-index guesses.

## Frontend (React / Tailwind / Framer Motion)

- Render floating overlays (dropdowns, popovers, tooltips, menus) via a portal to `document.body` with fixed positioning anchored to the trigger's bounding rect. Ancestor `backdrop-filter`/`transform`/`filter` create stacking contexts that trap child `z-index`.
- Don't use `backdrop-filter` on panels layered over animated content; use an opaque background. Animated backdrops cause compositing artifacts and the filter itself creates a stacking-context trap.
- When portalling a Framer Motion element, call `createPortal` on the outside and let `AnimatePresence` directly wrap the `motion` element inside — `AnimatePresence` must be the direct parent of the animating child, or it won't mount/animate.

## Routing & navigation (frontend)

Navigation is **URL-driven** via `react-router-dom` (`app/src/router.tsx`, `createBrowserRouter`). See [ADR 0003](docs/adr/0003-client-side-routing.md) for the full route map.

- The URL is the source of truth for navigation — **not** Zustand. `store/ui.ts` holds UI-only state (command palette, modals + form fields, list filters/search/selection, review edit draft, annotation tool). Never reintroduce navigation fields (`screen`, `activeRunId`, `activeProject`, `activeTicket`, `projectTab`) to the store.
- Run-scoped screens live under `/runs/:runId/*` and read `runId` via `useParams`. Intra-screen *selection* (expanded accordion, selected case/ticket, project tab) goes in **query params** (`?ticket=`, `?case=`, `?tab=`) — not the store, not the path.
- The run WebSocket is owned by `RunLayout` via `RunSocketProvider` (one socket per run visit, persists across intra-run navigation). Screens subscribe to transient events with `useRunEvents(handler)` — don't open `useRunSocket` per screen.
- **Run-scoped screens are reachable only from within a run** (workspace mode — see [ADR 0004](docs/adr/0004-run-workspace-navigation.md)). The global sidebar (`GlobalSidebar`) lists **only** global screens; run-scoped nav (Review/Automation/Execution/Evidence/Link/Publish) lives in the run workspace sidebar (`RunSidebar`) + run-context header (`RunContextHeader`), both shown only under `/runs/:runId/*`.
- **Never default to "the latest run."** For shell chrome, read `useRunRouteId()` (URL-only, returns `null` off run routes) — there is no "resolve current run" fallback (the old `useResolvedRunId` is deleted). `RunLayout` guards every run-scoped route: an invalid or nonexistent `:runId` redirects to `/runs`, never auto-selecting a run.

## Build & verify (app/)

- There is **no unit-test harness**. The gate is `npm run typecheck` (`tsc -b --noEmit`) + `npm run build`. Do not run `npm test` — the script doesn't exist.
- Verify UI/behavior at runtime: `npm run dev` (Vite; auto-falls off 5173 if busy) + Playwright (`playwright` is installed; `npx playwright install chromium` once) to drive real routes and screenshot. The API defaults to `127.0.0.1:8787`; filter benign backend fetch/WebSocket errors when asserting on console output.
- **In a fresh worktree, run `npm ci` in `app/` first** — `node_modules` is not shared across worktrees, so `tsc`/`vite` are simply missing until you do.

### Driving the real SPA in Playwright — four traps that silently invalidate a test

Each of these produces a *green-looking* run that proved nothing. Learned the hard way in #482.

- **The API is at `/api` (same-origin, proxied by Vite), not `127.0.0.1:8787`.** Routing/intercepting on the port matches nothing in dev. Intercept `/api/**` **and** `/auth/**` (the latter is same-origin and unprefixed).
- **A raw `fetch` from page context bypasses `lib/api.ts` entirely** — no bearer token, no 401→refresh, no interceptor. To exercise client behaviour you must drive the app's own UI.
- **`page.goto` is a full reload, and the access token is in memory only** (never persisted, by design). A reload therefore boots *anonymous* and `RequireAuth` legitimately redirects to `/login` — which looks exactly like a session bug but isn't. Navigate **client-side** (click the sidebar buttons; the nav uses buttons, not `<a href>`).
- **react-query has `staleTime: 15_000`** (`app/src/app/QueryProvider.tsx`), so revisiting a screen serves cache and issues **no request at all**. Visit a screen not yet loaded, and *assert that requests actually happened* rather than inferring it.

Also: the onboarding `TourOverlay` intercepts clicks on first load. Its blocker is `fixed inset-0 z-[70]`, so it eats *every* click until dismissed; target it by `[data-testid=tour-card]`. **Better than clicking "Skip": pre-set `localStorage["qagent.tourSeen"] = "1"` in an `addInitScript`** — the tour also *auto-navigates the shell*, which silently bounces a run-scoped route to the dashboard before "Skip" can be clicked (learned in #549, after the route looked like it had redirected for auth reasons).

Four more traps, learned in #543 and #549:

- **Route-match on a predicate, not a `**/auth/**` glob.** That glob also swallows Vite's own `/src/screens/auth/*.tsx` dev modules and silently blanks the login page. Use something like `^/(api|auth)/`.
- **`route.fallback()` for anything you are not deliberately mocking.** Fulfilling unknown endpoints with `{}` mints a bogus session (a faked `/auth/refresh` did exactly this) and crashes the shell.
- **Assert requests actually happened.** Combined with `staleTime: 15_000`, a screen you have already visited issues none, so a passing assertion can be reading cache.
- **Translucent `GlassCard` over the animated shell can make text genuinely unreadable** — screenshot and *look* before calling a panel done; use the opaque pattern (`ProjectFilePanel`) for anything text-heavy.

## Build & verify (api/)

- The gate is `uv run pytest -q` from `api/`. **In a fresh worktree (no `.venv`) use `uv run --extra dev pytest -q`** — `pytest` lives in the `dev` **optional-dependency extra** (`[project.optional-dependencies]`), not the default dependency set, so a plain `uv run pytest` fails with `Failed to spawn: pytest / program not found`.
- **The suite is GREEN: 0 failures on `master`** (#469, closed by #579 — 1048 passed, 4 skipped, ~9min). Still **baseline it before you start and compare by name**, not by count: a green baseline means any failure you see is either yours or a fresh regression, and it should be treated as such immediately, never re-normalised into "the suite is a bit red".
- **Treat "known red" as a bug, not a baseline.** The count went 22 → 18 → 1 → 0, and each drop was a real coverage hole that the "permanently red, just baseline it" framing had been hiding:
  - #542: 4 `test_prompts.py` failures whose `SimpleNamespace` stubs lacked `test_data`, so those assertions had never run.
  - #573: **17** failures from one cause — `settings_store.DEFAULTS` ships `executionTarget="local-agent"` (#161, deliberately), and a test's temp workspace has no `settings.json`, so every execution/heal endpoint took the **agent-dispatch branch** and 409'd "No local agent paired". Those tests never entered the in-process code they claimed to cover. #469/#573 had diagnosed this as an *order-dependent leak*; it was not — nothing leaks (the store lives under a per-test `tmp_path`), it was deterministic and reproduced file-by-file in isolation. Fixing the target then exposed three more stubs that had silently gone stale behind it (a `run_id` kwarg, a 7th positional `dom_snapshot`, and the post-#540 `SUR-`-prefixed spec filename).
  - #579: the last one, `test_claude_creds.py::test_upload_and_delete_own_credentials` — a stale exact-dict `==` against a whole response body that had since grown `status` / `accountEmail` / `accountOrg`.
- **Never assert `==` against a whole API response body.** It fails on *correct* additive changes, so it rots instead of being maintained (#579). Assert the fields the test is about, and pin the behaviour with an observable effect — e.g. after a DELETE, check the row is gone from the store *and* that a second user's row is not, so the test can't pass if the delete no-ops or over-deletes. Prove that with a negative control before believing it.
- **`tests/conftest.py` pins the suite's `executionTarget` to `server`** (`TEST_SETTINGS_DEFAULTS`) so endpoints resolve the in-process path. A test that wants agent dispatch opts in via the `local_agent_target` fixture or the `settings_override(...)` context manager — **never** a bare `settings_store.save_settings(...)`, which would leave the store changed for the rest of the test.
- **Assert which branch ran, not just the status code.** A green-looking response from the *wrong* branch is exactly what hid #573 for months: all three heal branches return `{"started": True}`. Pin the branch (`mode == "server"`) **and** an observable effect of the code you mean to test (e.g. the heal report's per-attempt trail, appended only inside the in-process loop). See `_start_in_process_heal` in `tests/test_automation.py`.
- **Never call `monkeypatch.undo()` in an api test.** The `workspace_dir` fixture redirects `db_module.SessionLocal`/`engine` at the per-test temp DB with the *same* function-scoped monkeypatch, so `undo()` silently un-redirects it. The request path keeps working (conftest overrides `get_db` with an already-bound shared session) while any **background thread** that opens its own `SessionLocal()` goes to the real configured DB, finds none of the seeded rows, and returns quietly — so the test fails (or passes) for a reason unrelated to the code. Restore the one attribute you patched instead: keep the original and `monkeypatch.setattr` it back (#641).
- **Don't poll an endpoint in a tight loop to wait for a background pass.** `client` shares ONE session with the test, so hammering it every 50ms contends with the worker's writes on the same SQLite file and starves the very pass being awaited — a wait that makes the thing it measures slower, and reads as a code failure. Wait on the in-process guard (e.g. `automation.is_generating`), then assert through the endpoint once (#641).
- **Verify order-independence both ways** when touching shared state: the full suite *and* each affected file run alone must give the same result.
- The suite runs with `settings.auth_required = False` (see `tests/conftest.py`), which makes the global `auth_guard` middleware in `main.py` a **passthrough**. Anything that touches token acceptance must add a test that flips `auth_required=True`, or the middleware path is never exercised.

## Local Agent (agent/)

- **Every Local Agent fix must ship to BOTH the Electron desktop app AND the `npx @q-agent/agent` version.** Both compile from the same `agent/src` (→ `dist/`), so a source fix already covers both — the discipline is at **release**: ship it to both channels, never just one. Electron-specific behavior (window, auto-update) lives in `agent/electron/`.
- Release does BOTH channels, but the **npx/npm publish step needs an interactive 2FA OTP** — so the assistant CANNOT run a full release headlessly in one line. Division of labor:
  - **npm publish (npx path) — user-run.** The user runs `cd agent && npm run release` from a real shell (in this session: prefix with `!`) and enters the OTP when npm prompts. This also bumps the version and builds+stages the desktop app in the same run. Non-interactive alternative: `npm run release -- --otp=<code>` with a *fresh* code (TOTP expires in ~30s, so it still can't be a pre-baked single line).
  - **Desktop-only stage — assistant-runnable (no 2FA).** `npm run release -- --desktop-only` builds the Windows installer + stages `qagent-agent-setup.exe`/`latest.yml` into `downloads/`. It uses the *current* package.json version (no bump), so only run it standalone AFTER the version has been bumped, to avoid shipping the desktop app at a stale version.
  - Never use `--no-desktop` alone as the "release" (ships only npx). `downloads/` is a live read-only bind mount into nginx — staging files there needs no web rebuild. Bump above the currently-shipped version or electron-updater won't upgrade; `-- --minor|--major` to bump beyond patch.
- Agent gate: `cd agent && npm test` — it builds (tsc) and then runs `scripts/run-tests.mjs`, which enumerates `dist/test/**/*.test.js` and passes explicit paths to `node --test` (69 tests today). Do **not** go back to `node --test dist/test`: since Node 21 a bare directory argument is resolved as a *module*, so it reports `pass 0 / fail 1` on any modern Node (#470); a bare `"dist/test/*.test.js"` glob is no good either — it matches nothing on Node 18/20 and **still exits 0**, i.e. a green gate that ran zero tests. A fresh worktree also needs `npm ci` + `npm run vendor:base` once, or the `installBaseFramework` staging tests fail on the missing vendored `@q-agent/playwright-base`. Agent fixes are **not** delivered by the server `docker compose` rebuild — the agent runs on the paired device and updates via its own installer/npm publish.

## Issue-driven delivery workflow (default for every request)

For any **feature, enhancement, or bug** the user raises, follow this end-to-end by
default — this is a standing directive, so don't ask permission for the process
itself each time:

1. **Clarify first.** If the request is ambiguous, ask before opening issues or writing code.
2. **Open a GitHub issue** capturing it (`gh issue create` — no `--json` here; capture the number from the returned URL; multi-line bodies via `--body-file`).
3. **Slice vertically.** Break it into independently-shippable vertical slices, one issue each (parent + sub-issues as needed). **Maximize parallelism**: file-disjoint slices run concurrently via `general-purpose` sub-agents (see *Parallel multi-slice work*); slices sharing a core file (store, router, shell) are sequenced.
4. **Branch per issue** off the default branch: `feature/<issue-number>` for features/enhancements, `bug/<issue-number>` for fixes (e.g. `feature/152`, `bug/160`).
5. **Implement + verify** against the relevant `CLAUDE.md` gates (typecheck/build; verify UI at runtime where it applies).
6. **PR → self-merge.** Open a PR to the default branch, then squash-merge it yourself: `gh pr merge <n> --squash --admin --delete-branch`. **Auto-merging self-authored PRs is pre-authorized for this project** — don't wait for per-PR confirmation.
7. **Rebuild the Docker image after shipping.** Whenever code is merged/shipped, the running container is stale until rebuilt — always remember (and explicitly confirm in your response) to rebuild it: `docker compose up -d --build` (or `docker compose build`). This is a standing reminder for **every** ship, not just full releases.

Branch target: the repo's default branch is **`master`** — the user's "merge to main" means merge to the default branch.

## Git / PR workflow

- Default branch is **master** (not `main`). Base PRs on `master`.
- Branch names are issue-scoped: `feature/<issue-number>` / `bug/<issue-number>` (`docs/…` for docs-only). PR → `gh pr merge <n> --squash --admin --delete-branch`. Auto-merging self-authored PRs is pre-authorized for this project (see the issue-driven workflow above).
- Worktree caveat: local `--delete-branch` fails with "branch … used by worktree" — harmless; the server-side squash-merge still succeeded (confirm `gh pr view <n> --json state` = `MERGED`). Clean up agent worktrees afterward: `git worktree remove --force <path>` + `git worktree prune`.
- `gh issue create` has no `--json` here (capture the number from the returned URL); pass multi-line issue bodies via `--body-file`, not nested heredocs (they break on apostrophes).
- Secrets stay out of git: `api/.env` and `api/workspace/` are gitignored; only `.env.example` files are tracked.

## Parallel multi-slice work

- For cross-cutting changes, slice into a **solo foundation** + **file-disjoint feature slices run in parallel** (worktree sub-agents), then a **solo cleanup**. Parallelism is bounded by file disjointness, not issue count — slices sharing a core file (store, router, shell) must be sequenced. Pull `master` between waves so each new worktree branches from merged code.
- **Do not use the `de-expert` agent for this project.** Use `general-purpose` for implementation sub-agents.
- When a full migration can't land in one green step, ship a temporary bridge in the foundation so every intermediate slice stays functional and typechecks, then delete the bridge in the cleanup slice.

## Tooling

- In the Bash tool, use bash heredocs for multi-line commit messages; never use PowerShell here-string syntax (`@'...'@`) — it leaks literal characters into the message.
