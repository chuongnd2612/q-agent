/**
 * Runtime probe for slice 6 (ADR 0015 §3 and §5, #732).
 *
 * Drives the REAL SPA against a REAL API on a throwaway SQLite workspace — see
 * `probe-project-routes.mjs`'s header for the full setup and for the four traps
 * in CLAUDE.md that otherwise produce a green-looking run that proved nothing.
 * Two of them matter here specifically:
 *
 *   * react-query's `staleTime: 15_000` means an already-visited screen issues NO
 *     request, so requests are captured for the whole session and asserted on at
 *     the end rather than per navigation;
 *   * the access token is in memory only, so navigation is client-side (history +
 *     popstate), never a reload.
 *
 * Setup:
 *   1. Seed a throwaway workspace with TWO projects (see the PR for #732) —
 *      one with an ADO ticket source, one with Jira and its own tickets, so
 *      "the other project's rows must not appear" is a real assertion.
 *   2. Start the API on 8787 against it, and `npm run dev`.
 *   3. PLATFORM_GUID=<guid> CLAIMS_GUID=<guid> node scripts/probe-connection-and-run-options.mjs
 */
import { chromium } from "playwright";
import { writeFileSync } from "node:fs";

const BASE = process.env.PROBE_BASE ?? "http://localhost:5173";
const EMAIL = "probe@example.com";
const PASSWORD = "probe-password-123";
const PLATFORM = process.env.PLATFORM_GUID;
const CLAIMS = process.env.CLAIMS_GUID;
const OUT = process.env.PROBE_OUT ?? "D:/tmp/qagent-probe732";

const results = [];
const check = (name, ok, detail = "") => {
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? " — " + detail : ""}`);
};

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
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
await page.locator('input[type="email"]').fill(EMAIL);
await page.locator('input[type="password"]').fill(PASSWORD);
await page.locator('button[type="submit"]').click();
await page.waitForURL((u) => !u.pathname.endsWith("/login"), { timeout: 20000 });
check("signs in and leaves /login", true, page.url());

const goto = async (p) => {
  await page.evaluate((target) => window.history.pushState({}, "", target), p);
  await page.evaluate(() => window.dispatchEvent(new PopStateEvent("popstate")));
  await page.waitForTimeout(900);
};

// ------------------------------------------------------------ Connection tab
await goto(`/projects/${PLATFORM}/connection`);
await page
  .waitForSelector('[data-testid="connection-role-ticketSource"]', { timeout: 20000 })
  .catch(() => {});

for (const role of ["ticketSource", "codeKnowledge", "testCase"]) {
  const card = page.locator(`[data-testid="connection-role-${role}"]`);
  check(`Connection tab renders the ${role} role card`, (await card.count()) === 1);
}
check(
  "the ticket-source card names the bound connection",
  (await page.locator('[data-testid="connection-role-ticketSource"]').innerText()).includes(
    "Azure DevOps",
  ),
  await page.locator('[data-testid="connection-role-ticketSource"]').innerText(),
);
// Surency Platform has no explicit TEST CASE TARGET, so it must show the ticket
// source and say so — an unset target is a working default, not a gap.
const testCaseText = await page.locator('[data-testid="connection-role-testCase"]').innerText();
check(
  "an unset TEST CASE TARGET shows the ticket source, marked inherited",
  /Same as the ticket source/i.test(testCaseText),
  testCaseText.replace(/\n/g, " | "),
);
await page.screenshot({ path: `${OUT}/connection-tab-platform.png`, fullPage: false });

// The other project binds one explicitly, so the inherited label must be ABSENT
// there — the negative control that stops the check above passing on a constant.
await goto(`/projects/${CLAIMS}/connection`);
await page
  .waitForSelector('[data-testid="connection-role-testCase"]', { timeout: 20000 })
  .catch(() => {});
const claimsTestCase = await page.locator('[data-testid="connection-role-testCase"]').innerText();
check(
  "an explicit TEST CASE TARGET is not marked inherited",
  !/Same as the ticket source/i.test(claimsTestCase),
  claimsTestCase.replace(/\n/g, " | "),
);
await page.screenshot({ path: `${OUT}/connection-tab-claims.png`, fullPage: false });

// ------------------------------------------- provider switcher is really gone
await goto(`/projects/${PLATFORM}/tickets`);
await page.waitForTimeout(1500);
const ticketsBody = await page.locator("body").innerText();
check(
  "the Tickets tab has no provider/connection switcher",
  !/Select connection/i.test(ticketsBody),
  "",
);
check(
  "the Tickets tab says where the project pulls from",
  /the project.s ticket source/i.test(ticketsBody),
  ticketsBody.split("\n").slice(0, 3).join(" | "),
);
// Negative control: the OTHER project's ticket must not appear here.
check(
  "another project's ticket does not appear",
  !ticketsBody.includes("CLM-9001"),
  "",
);
await page.screenshot({ path: `${OUT}/tickets-platform.png`, fullPage: false });

await goto(`/projects/${CLAIMS}/tickets`);
await page.waitForTimeout(1500);
const claimsBody = await page.locator("body").innerText();
check(
  "the other project DOES show its own ticket",
  claimsBody.includes("CLM-9001"),
  "",
);
check(
  "Surency Platform's tickets do not leak into Claims Portal",
  !claimsBody.includes("SUR-1428"),
  "",
);
await page.screenshot({ path: `${OUT}/tickets-claims.png`, fullPage: false });

// -------------------------------------------------- link options in Create Run
await goto(`/projects/${PLATFORM}/tickets`);
await page.waitForTimeout(1200);
// Select a ticket so the run has a scope, then open the modal.
const firstRow = page.locator('input[type="checkbox"]').first();
if (await firstRow.count()) await firstRow.click().catch(() => {});
await page
  .getByRole("button", { name: /create run/i })
  .first()
  .click()
  .catch(() => {});
await page.waitForSelector('[data-testid="create-run-link"]', { timeout: 15000 }).catch(() => {});
const modal = page.locator('[data-testid="create-run-link"]');
check("Create Run offers the link options", (await modal.count()) === 1);
const modalText = (await modal.count()) ? await modal.innerText() : "";
check("…including a dry-run control", /Dry run/i.test(modalText), modalText.replace(/\n/g, " | "));
check(
  "…and the create & link toggle",
  /Create & link test cases/i.test(modalText),
  "",
);
await page.screenshot({ path: `${OUT}/create-run-link-options.png`, fullPage: false });

// ------------------------------------------------------------ request scoping
const ticketReqs = apiRequests.filter((r) => r.startsWith("/api/tickets?"));
check(
  "every /api/tickets request is project-scoped",
  ticketReqs.length > 0 && ticketReqs.every((r) => /[?&]project=/.test(r)),
  ticketReqs.join(" | ") || "(no /api/tickets requests at all)",
);
check(
  "no /api/tickets request carries a connectionId filter any more",
  !ticketReqs.some((r) => /connectionId=/.test(r)),
  ticketReqs.filter((r) => /connectionId=/.test(r)).join(" | "),
);

writeFileSync(`${OUT}/probe732.json`, JSON.stringify({ results, consoleErrors }, null, 2));
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
if (consoleErrors.length) console.log("console errors:\n" + consoleErrors.slice(0, 12).join("\n"));
await browser.close();
process.exit(failed.length ? 1 : 0);
