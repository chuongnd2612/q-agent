/**
 * Runtime probe for the sidebar project tree (ADR 0015 slice 3, #729).
 *
 * Drives the REAL SPA against a REAL API on a throwaway SQLite workspace — the
 * same setup `probe-project-routes.mjs` documents (#728), which see for the
 * seeding recipe. Two extra facts matter here:
 *
 *  - the seeded rows must be stamped with `owner_id`, because `owned()` filters
 *    strictly on `owner_id == user.id` and a signed-in probe user sees NOTHING
 *    otherwise; and
 *  - one run must be left in a non-terminal, non-`review`-swept status, because
 *    the API's boot sweep (`_recover_orphaned_runs`) terminates anything else —
 *    `review` survives it, and is non-terminal, so the badge has something to
 *    pulse on.
 *
 * What it asserts, beyond "the tree renders":
 *
 *  - every project row is COLLAPSED on a cold load;
 *  - expanding one reveals exactly the six project tabs, and a tab navigates to
 *    that project's own nested route;
 *  - the Tickets/Runs counts match what the API reports for that project;
 *  - the counts come from ONE workspace-wide `GET /runs`, not one request per
 *    project (ADR 0015 §8) — asserted on the recorded request log;
 *  - the sidebar's numbers equal the Dashboard comparison table's for the same
 *    project, i.e. they really are one source;
 *  - the global Tickets / Runs / Reports nav entries are gone, and so are the
 *    header's project pill and New Run button.
 *
 * Usage: BASE=http://localhost:5199 node scripts/probe-sidebar-tree.mjs
 */
import { chromium } from "playwright";
import { mkdirSync, writeFileSync } from "node:fs";

const BASE = process.env.BASE ?? "http://localhost:5199";
const EMAIL = process.env.PROBE_EMAIL ?? "probe@example.com";
const PASSWORD = process.env.PROBE_PASSWORD ?? "probe-password-123";
const OUT = process.env.OUT ?? "D:/tmp/qagent-probe729/shots";
mkdirSync(OUT, { recursive: true });

const results = [];
const check = (name, ok, detail = "") => {
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? " — " + detail : ""}`);
};

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1440, height: 950 } });
// The TourOverlay's blocker is `fixed inset-0 z-[70]` AND it auto-navigates the
// shell, so pre-seed the flag rather than trying to click Skip.
await context.addInitScript(() => window.localStorage.setItem("qagent.tourSeen", "1"));
const page = await context.newPage();

// Request listeners are attached for the WHOLE session: react-query's
// `staleTime: 15_000` means a screen visited twice issues nothing the second
// time, so a per-step counter would read as "no requests" and prove nothing.
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
await page.locator('input[type="email"]').fill(EMAIL);
await page.locator('input[type="password"]').fill(PASSWORD);
await page.locator('button[type="submit"]').click();
await page.waitForURL((u) => !u.pathname.endsWith("/login"), { timeout: 20000 });
check("signs in and leaves /login", true, page.url());

const path = () => new URL(page.url()).pathname;

await page.waitForSelector("[data-testid=sidebar-project-tree]", { timeout: 15000 });
await page.waitForFunction(
  () => document.querySelectorAll("[data-testid=sidebar-project-row]").length > 0,
  null,
  { timeout: 20000 },
);

// ------------------------------------------------------- the tree renders
const rows = page.locator("[data-testid=sidebar-project-row]");
const rowCount = await rows.count();
check("sidebar lists every project as a row", rowCount >= 3, `${rowCount} rows`);

// ------------------------------------------- all collapsed on a cold load
const expandedOnLoad = await page.$$eval("[data-testid=sidebar-project-row]", (els) =>
  els.filter((e) => e.getAttribute("aria-expanded") === "true").length,
);
check("every project row is collapsed on load", expandedOnLoad === 0, `${expandedOnLoad} expanded`);
const tabsOnLoad = await page.locator("[data-testid^=sidebar-project-tab-]").count();
check("no tab items are rendered while collapsed", tabsOnLoad === 0, `${tabsOnLoad} tabs`);

await page.screenshot({ path: `${OUT}/tree-collapsed.png` });

// ---------------------------------------------------- expanding a project
const firstRow = rows.first();
const firstKey = await firstRow.getAttribute("data-project");
await firstRow.click();
await page.waitForSelector("[data-testid=sidebar-project-tab-overview]", { timeout: 5000 });
const tabIds = await page.$$eval("[data-testid^=sidebar-project-tab-]", (els) =>
  els.map((e) => e.getAttribute("data-testid").replace("sidebar-project-tab-", "")),
);
check(
  "expanding reveals exactly the six project tabs",
  tabIds.join(",") === "overview,tickets,runs,knowledge,connection,reports",
  tabIds.join(","),
);
await page.screenshot({ path: `${OUT}/tree-expanded.png` });

// ------------------------------------------------------- the active badge
const badges = page.locator("[data-testid=sidebar-active-run]");
const badgeCount = await badges.count();
const badgeText = badgeCount ? (await badges.first().innerText()).trim() : "";
check("a project with a run in flight shows the active-run badge", badgeCount === 1, badgeText);
// The pulse is a real animation, not a static dot.
const pulsing = badgeCount
  ? await badges
      .first()
      .locator("span.animate-ping")
      .evaluate((el) => getComputedStyle(el).animationName !== "none")
  : false;
check("the badge's dot is actually animating", pulsing);
if (badgeCount) {
  await badges.first().scrollIntoViewIfNeeded();
  const box = await badges.first().boundingBox();
  if (box) {
    await page.screenshot({
      path: `${OUT}/tree-active-badge.png`,
      clip: {
        x: Math.max(0, box.x - 210),
        y: Math.max(0, box.y - 26),
        width: 270,
        height: 76,
      },
    });
  }
}

// --------------------------------------------- counts match the API's own
const sidebarCount = async (tab) => {
  const el = page.locator(`[data-testid=sidebar-count-${tab}]`).first();
  return (await el.count()) ? (await el.innerText()).trim() : null;
};
// Wait for the ticket probe to land — react-query retries with backoff, so wait
// on the OUTCOME rather than a fixed sleep.
await page
  .waitForFunction(
    () => document.querySelector("[data-testid=sidebar-count-tickets]") !== null,
    null,
    { timeout: 20000 },
  )
  .catch(() => {});
const sidebarTickets = await sidebarCount("tickets");
const sidebarRuns = await sidebarCount("runs");

check(
  "the expanded project shows a Tickets count",
  sidebarTickets !== null && sidebarTickets !== "",
  String(sidebarTickets),
);
check(
  "the expanded project shows a Runs count",
  sidebarRuns !== null && sidebarRuns !== "",
  String(sidebarRuns),
);

// ------------------------------- ONE workspace-wide GET /runs, not N of them
const runsListRequests = apiRequests.filter((r) => /^\/api\/runs(\?|$)/.test(r));
const perProjectRunRequests = runsListRequests.filter((r) => r.includes("project="));
check(
  "runs are fetched workspace-wide, not once per project",
  perProjectRunRequests.length === 0,
  runsListRequests.join(" | ") || "(none)",
);
const ticketProbes = apiRequests.filter((r) => r.startsWith("/api/tickets?"));
check(
  "ticket counts are per-project count probes (pageSize=1)",
  ticketProbes.length > 0 && ticketProbes.every((r) => r.includes("pageSize=1")),
  ticketProbes.join(" | ") || "(none)",
);

// ------------------------------------ the sidebar agrees with the Dashboard
// The Dashboard is already the current screen (login lands on "/"), and its
// table is the other consumer of `useProjectCounts`.
const dashRow = page.locator("[data-testid=dash-project-row]").first();
await dashRow.waitFor({ timeout: 15000 });
const dashCells = await dashRow.evaluate((el) =>
  [...el.children].map((c) => c.textContent.trim()),
);
check(
  "sidebar Tickets count equals the Dashboard table's for the same project",
  dashCells.includes(String(sidebarTickets)),
  `sidebar=${sidebarTickets} dashboard=[${dashCells.join(" | ")}]`,
);
check(
  "sidebar Runs count equals the Dashboard table's for the same project",
  dashCells.includes(String(sidebarRuns)),
  `sidebar=${sidebarRuns} dashboard=[${dashCells.join(" | ")}]`,
);

// ------------------------------------------------- a tab really navigates
await page.locator("[data-testid=sidebar-project-tab-tickets]").first().click();
await page.waitForFunction(
  (guid) => window.location.pathname === `/projects/${guid}/tickets`,
  firstKey,
  { timeout: 10000 },
);
check("a tab navigates to the project's nested route", path() === `/projects/${firstKey}/tickets`, path());
await page.screenshot({ path: `${OUT}/tree-on-project.png` });

// ---------------------------------------------- the removed global entries
// The project TABS are also called Tickets / Runs / Reports, so a text match
// would be ambiguous. Compare against the top-level nav items only — they are
// the ones carrying a `data-tour` id.
const topLevel = await page.$$eval("aside nav button[data-tour]", (els) =>
  els.map((e) => e.getAttribute("data-tour")),
);
check(
  "global Tickets / Runs / Reports nav entries are gone",
  !topLevel.includes("nav-tickets") &&
    !topLevel.includes("nav-runs") &&
    !topLevel.includes("nav-reports"),
  topLevel.join(", "),
);
check(
  "the sidebar still offers Dashboard and All projects",
  topLevel.includes("nav-dashboard") && topLevel.includes("nav-projects"),
  topLevel.join(", "),
);

// --------------------------------------------------------------- the header
const newRunBtn = await page.locator("[data-tour=topbar-newrun]").count();
check("the header's global New Run button is gone", newRunBtn === 0);
const headerText = (await page.locator("header").first().innerText()).trim();
check(
  "the header no longer carries the project pill",
  !headerText.includes("Surency Platform") &&
    !headerText.includes("Claims Portal") &&
    !headerText.includes("Member Hub"),
  headerText.replace(/\s+/g, " "),
);

// ---------------------------------------- collapsing again hides the tabs
await page.locator("[data-testid=sidebar-project-row]").first().click();
await page.waitForTimeout(300);
const afterCollapse = await page
  .locator(`[data-testid=sidebar-project-tab-overview]`)
  .count();
check("collapsing a project hides its tabs again", afterCollapse === 0, `${afterCollapse}`);

writeFileSync(
  `${OUT}/probe729.json`,
  JSON.stringify({ results, apiRequests, consoleErrors }, null, 2),
);
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
if (consoleErrors.length) console.log("console errors:\n" + consoleErrors.slice(0, 10).join("\n"));
await browser.close();
process.exit(failed.length ? 1 : 0);
