/**
 * Runtime probe for the run completion stage (ADR 0015 §6, #731).
 *
 * Same setup as `probe-project-routes.mjs` — read its header. Needs TWO finished
 * runs in the fixture: one whose comments all published (success variant) and
 * one with at least one failed comment (needs-attention variant). A probe that
 * only ever sees the green shape cannot tell a two-variant screen from a screen
 * that is always green.
 *
 *   PROJECT_GUID=<guid> OK_RUN=<id> FAIL_RUN=<id> node scripts/probe-run-complete.mjs
 */
import { chromium } from "playwright";
import { writeFileSync } from "node:fs";

const BASE = "http://localhost:5173";
const API = "http://127.0.0.1:8787";
const EMAIL = "probe@example.com";
const PASSWORD = "probe-password-123";
const PROJECT_GUID = process.env.PROJECT_GUID;
const OK_RUN = process.env.OK_RUN ?? "2";
const FAIL_RUN = process.env.FAIL_RUN ?? "3";
const OUT = "D:/tmp/qagent-probe";

const results = [];
const check = (name, ok, detail = "") => {
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? " — " + detail : ""}`);
};

const loginRes = await fetch(`${API}/auth/login`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ email: EMAIL, password: PASSWORD }),
});
const token = (await loginRes.json()).accessToken ?? null;
if (!token) {
  console.log("FAIL  could not mint an API token — status assertions would be vacuous");
  process.exit(1);
}
const runStatus = async (id) => {
  const res = await fetch(`${API}/runs/${id}`, { headers: { Authorization: `Bearer ${token}` } });
  return (await res.json()).status;
};

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
await context.addInitScript(() => {
  window.localStorage.setItem("qagent.tourSeen", "1");
  Object.keys(window.localStorage)
    .filter((k) => k.startsWith("qagent.runStage."))
    .forEach((k) => window.localStorage.removeItem(k));
});
const page = await context.newPage();
const consoleErrors = [];
page.on("console", (m) => {
  if (m.type() === "error") consoleErrors.push(m.text());
});

await page.goto(`${BASE}/login`, { waitUntil: "domcontentloaded" });
await page.locator('input[type="email"]').fill(EMAIL);
await page.locator('input[type="password"]').fill(PASSWORD);
await page.locator('button[type="submit"]').click();
await page.waitForURL((u) => !u.pathname.endsWith("/login"), { timeout: 20000 });

const path = () => new URL(page.url()).pathname;
const goto = async (p) => {
  await page.evaluate((target) => window.history.pushState({}, "", target), p);
  await page.evaluate(() => window.dispatchEvent(new PopStateEvent("popstate")));
  await page.waitForTimeout(700);
};
const runRoot = (id) => `/projects/${PROJECT_GUID}/runs/${id}`;

// Sanity: the fixture is what the probe assumes it is.
check("the success fixture is a finished run", (await runStatus(OK_RUN)) === "done");
check("the failure fixture is a finished run", (await runStatus(FAIL_RUN)) === "done");

// ------------------------------------------- a finished run reopens onto done
await goto(runRoot(OK_RUN));
await page.waitForTimeout(1200);
check(
  "reopening a finished run lands on the completion stage",
  path() === `${runRoot(OK_RUN)}/done`,
  path(),
);

// ------------------------------------------------------------ success variant
await page.waitForSelector("[data-testid=run-complete-success]", { timeout: 15000 });
check("success variant renders", true);
check(
  "the success variant offers NO retry",
  (await page.$("[data-testid=run-complete-retry]")) === null,
);
check(
  "both exits are present",
  (await page.$("[data-testid=run-complete-reports]")) !== null &&
    (await page.$("[data-testid=run-complete-another]")) !== null,
);

// Footer: Back disabled, Next reads "Back to project", every pill complete.
check(
  "Back is disabled — the run is over",
  (await page.getAttribute("[data-testid=run-back]", "disabled")) !== null,
);
const pillStates = await page.$$eval("[data-testid^=run-stage-]", (els) =>
  els.map((e) => e.getAttribute("data-state")),
);
check(
  "all five stage pills read complete",
  pillStates.length === 5 && pillStates.every((s) => s === "complete"),
  pillStates.join(","),
);
const nextText = (await page.textContent("[data-testid=run-next]"))?.trim();
check("Next becomes the way out of the run", /back to/i.test(nextText ?? ""), nextText ?? "");
await page.screenshot({ path: `${OUT}/complete-success.png` });

// --------------------------------------------------- needs-attention variant
await goto(`${runRoot(FAIL_RUN)}/done`);
await page.waitForSelector("[data-testid=run-complete-attention]", { timeout: 15000 });
check("needs-attention variant renders when a ticket failed to publish", true);
check(
  "the attention variant offers Retry failed publish",
  (await page.$("[data-testid=run-complete-retry]")) !== null,
);
// Negative control: the two variants are genuinely exclusive, so a screen that
// always rendered one of them cannot pass both halves of this probe.
check(
  "the two variants are exclusive",
  (await page.$("[data-testid=run-complete-success]")) === null,
);
const attentionText = await page.textContent("[data-testid=run-complete-attention]");
check(
  "it states how many of how many failed",
  /1\D+3/.test(attentionText ?? ""),
  (attentionText ?? "").replace(/\s+/g, " ").slice(0, 120),
);
await page.screenshot({ path: `${OUT}/complete-attention.png` });

// ------------------------------------------------ it survives exit and reopen
await page.click("[data-testid=run-exit]");
await page.waitForTimeout(900);
check(
  "Exit returns to the project's Runs tab",
  path() === `/projects/${PROJECT_GUID}/runs`,
  path(),
);
await goto(runRoot(FAIL_RUN));
await page.waitForTimeout(1200);
check(
  "reopening lands back on the completion stage, not on a stale stage",
  path() === `${runRoot(FAIL_RUN)}/done`,
  path(),
);

// The completion stage must not have moved the run's status.
check(
  "viewing the completion stage did not change run.status",
  (await runStatus(FAIL_RUN)) === "done",
);

writeFileSync(`${OUT}/probe731.json`, JSON.stringify({ results, consoleErrors }, null, 2));
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
if (consoleErrors.length) console.log("console errors:\n" + consoleErrors.slice(0, 10).join("\n"));
await browser.close();
process.exit(failed.length ? 1 : 0);
