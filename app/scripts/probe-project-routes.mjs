/**
 * Runtime probe for the project-scoped route map (ADR 0015, #728).
 *
 * Drives the REAL SPA against a REAL API on a throwaway SQLite workspace, so
 * nothing here is mocked: every assertion is about what the router actually
 * resolves. That matters more than usual for this change, because the four traps
 * in CLAUDE.md all produce a green-looking run that proved nothing.
 *
 * Setup (see the PR for #728):
 *   1. Seed a throwaway workspace:
 *        QAGENT_WORKSPACE_DIR=<dir> QAGENT_DATABASE_URL=sqlite:///<dir>/probe.db  *          uv run python -m app.seed
 *      then add a `projects` row + a user, and stamp `owner_id` on the seeded
 *      rows — `owned()` filters strictly on `owner_id == user.id`, so the dev
 *      seed's unowned rows are invisible to a signed-in user.
 *   2. Start the API on 8787 against that workspace, and `npm run dev`.
 *   3. PROJECT_GUID=<guid> node scripts/probe-project-routes.mjs
 *
 * Signs in through the real login form on purpose: the access token lives in
 * memory only, so a `page.goto` after boot boots anonymous and `RequireAuth`
 * legitimately redirects to /login — which looks exactly like a session bug.
 * Navigation is therefore client-side (history + popstate), never a reload.
 */
import { chromium } from "playwright";
import { writeFileSync } from "node:fs";

const BASE = "http://localhost:5173";
const EMAIL = "probe@example.com";
const PASSWORD = "probe-password-123";
const PROJECT_GUID = process.env.PROJECT_GUID;
const OUT = "D:/tmp/qagent-probe";

const results = [];
const check = (name, ok, detail = "") => {
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? " — " + detail : ""}`);
};

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
// The onboarding TourOverlay's blocker is `fixed inset-0 z-[70]` and it also
// auto-navigates the shell, which silently bounces a run-scoped route before a
// "Skip" click can land. Pre-seed the flag instead.
await context.addInitScript(() => {
  window.localStorage.setItem("qagent.tourSeen", "1");
});
const page = await context.newPage();
const apiRequests = [];
page.on("request", (r) => {
  const u = new URL(r.url());
  if (u.pathname.startsWith("/api/")) apiRequests.push(u.pathname + u.search);
});
const consoleErrors = [];
page.on("console", (m) => {
  if (m.type() === "error") consoleErrors.push(m.text());
});

// ---------------------------------------------------------------- sign in
await page.goto(`${BASE}/login`, { waitUntil: "domcontentloaded" });
await page.getByLabel(/email/i).fill(EMAIL).catch(async () => {
  await page.locator('input[type="email"]').fill(EMAIL);
});
await page.locator('input[type="password"]').fill(PASSWORD);
await page.locator('button[type="submit"]').click();
await page.waitForURL((u) => !u.pathname.endsWith("/login"), { timeout: 15000 });
check("signs in and leaves /login", true, page.url());

const path = () => new URL(page.url()).pathname;
const goto = async (p) => {
  // A full reload boots anonymous (token is in memory), so navigate through the
  // router's own history API instead.
  await page.evaluate((target) => window.history.pushState({}, "", target), p);
  await page.evaluate(() => window.dispatchEvent(new PopStateEvent("popstate")));
  await page.waitForTimeout(700);
};

// ------------------------------------------------- nested project tab routes
for (const tab of ["overview", "tickets", "runs", "knowledge", "connection", "reports"]) {
  const target = `/projects/${PROJECT_GUID}/${tab}`;
  await goto(target);
  check(`renders /projects/:guid/${tab}`, path() === target, path());
}

// The index route must canonicalise to the overview tab rather than rendering a
// tab the URL does not name.
await goto(`/projects/${PROJECT_GUID}`);
check(
  "project index redirects to /overview",
  path() === `/projects/${PROJECT_GUID}/overview`,
  path(),
);

// Pre-#728 `?tab=` bookmarks translate to the path. `settings` folds into the
// new Connection tab.
await goto(`/projects/${PROJECT_GUID}?tab=settings`);
check(
  "?tab=settings redirects to /connection",
  path() === `/projects/${PROJECT_GUID}/connection`,
  path(),
);
await goto(`/projects/${PROJECT_GUID}?tab=knowledge`);
check(
  "?tab=knowledge redirects to /knowledge",
  path() === `/projects/${PROJECT_GUID}/knowledge`,
  path(),
);

// ------------------------------------------------------- legacy flat routes
for (const flat of ["/tickets", "/runs", "/reports"]) {
  await goto(flat);
  check(`legacy ${flat} redirects to /projects`, path() === "/projects", path());
}

// A flat RUN url resolves exactly, because the run knows its project (#727).
await goto("/runs/1/review");
check(
  "legacy /runs/1/review nests under its project",
  path() === `/projects/${PROJECT_GUID}/runs/1/review`,
  path(),
);
await goto("/runs/1");
// The run ROOT then resolves to a stage — since #730 the overlay lands on the
// resumed stage, else on how far the run has got. So the assertion is that the
// URL ends up inside this project's run, not that it stops at the bare root.
check(
  "legacy /runs/1 nests under its project",
  path().startsWith(`/projects/${PROJECT_GUID}/runs/1`),
  path(),
);

// --------------------------------------------------------- run stage routes
// The five HUMAN stages only. `sync` (Link) is a hidden automatic stage since
// #730 — it has no stepper entry, and addressing it directly resolves to the
// stage the wizard is actually on, which is the intended behaviour rather than a
// broken route.
for (const seg of ["review", "automation", "execution", "evidence", "comment"]) {
  const target = `/projects/${PROJECT_GUID}/runs/1/${seg}`;
  await goto(target);
  check(`renders run stage /${seg}`, path() === target, path());
}

// ------------------------------------------------------------- the guard
// A nonexistent run is still refused — unreachable-by-design is not the same as
// unauthorised — and now lands on the project's Runs tab.
await goto(`/projects/${PROJECT_GUID}/runs/99999/review`);
// react-query retries a failed query with backoff before `isError` flips, so the
// guard legitimately takes seconds to fire — this waits for the outcome instead
// of racing it.
await page
  .waitForFunction(
    (guid) => window.location.pathname === `/projects/${guid}/runs`,
    PROJECT_GUID,
    { timeout: 20000 },
  )
  .catch(() => {});
check(
  "nonexistent run redirects to the project's Runs tab",
  path() === `/projects/${PROJECT_GUID}/runs`,
  path(),
);

// A real run addressed under the wrong project is re-pointed at its own.
await goto(`/projects/00000000-0000-0000-0000-000000000000/runs/1/review`);
check(
  "run under the wrong project is re-pointed at its own",
  path().startsWith(`/projects/${PROJECT_GUID}/runs/1`),
  path(),
);

// ------------------------------------------------ project scoping of the list
const ticketReqs = apiRequests.filter((r) => r.startsWith("/api/tickets?"));
const runReqs = apiRequests.filter((r) => r.startsWith("/api/runs?"));
check(
  "the tickets tab asks the API for THIS project only",
  ticketReqs.some((r) => r.includes(`project=${PROJECT_GUID}`)),
  ticketReqs.join(" | ") || "(no /api/tickets requests at all)",
);
check(
  "the runs tab asks the API for THIS project only",
  runReqs.some((r) => r.includes(`project=${PROJECT_GUID}`)),
  runReqs.join(" | ") || "(no /api/runs requests at all)",
);

// ------------------------------------------------------------- screenshots
for (const [name, target] of [
  ["project-overview", `/projects/${PROJECT_GUID}/overview`],
  ["project-tickets", `/projects/${PROJECT_GUID}/tickets`],
  ["project-runs", `/projects/${PROJECT_GUID}/runs`],
  ["project-connection", `/projects/${PROJECT_GUID}/connection`],
  ["project-reports", `/projects/${PROJECT_GUID}/reports`],
]) {
  await goto(target);
  await page.waitForTimeout(900);
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: false });
}

writeFileSync(`${OUT}/probe728.json`, JSON.stringify({ results, consoleErrors }, null, 2));
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
if (consoleErrors.length) {
  console.log("console errors:\n" + consoleErrors.slice(0, 12).join("\n"));
}
await browser.close();
process.exit(failed.length ? 1 : 0);
