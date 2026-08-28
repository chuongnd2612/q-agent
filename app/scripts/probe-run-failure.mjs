/**
 * Runtime probe for #758 — a run that failed at the hidden Analyze stage.
 *
 * Same setup as `probe-project-routes.mjs` (read its header). Needs two seeded
 * runs: one where every ticket's generation errored (`run.status = "failed"`,
 * `failed_stage = "processing"`) and one where only some did (`review`, with
 * surviving cases). Both shapes are required — the bug was that the total
 * failure looked exactly like "nothing generated yet", so a probe that only sees
 * one shape cannot tell the fix from the bug.
 *
 *   PROJECT_GUID=<guid> FAILED_RUN=<id> PARTIAL_RUN=<id> node scripts/probe-run-failure.mjs
 */
import { chromium } from "playwright";
import { writeFileSync } from "node:fs";

const BASE = "http://localhost:5173";
const API = "http://127.0.0.1:8787";
const EMAIL = "probe@example.com";
const PASSWORD = "probe-password-123";
const PROJECT_GUID = process.env.PROJECT_GUID;
const FAILED_RUN = process.env.FAILED_RUN ?? "5";
const PARTIAL_RUN = process.env.PARTIAL_RUN ?? "6";
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
  console.log("FAIL  could not mint an API token — the fixture checks would be vacuous");
  process.exit(1);
}
const getRun = async (id) =>
  (await fetch(`${API}/runs/${id}`, { headers: { Authorization: `Bearer ${token}` } })).json();

// The fixture has to be the shape this probe claims to test.
const failedRun = await getRun(FAILED_RUN);
const partialRun = await getRun(PARTIAL_RUN);
check(
  "the failed fixture really failed at Analyze",
  failedRun.status === "failed" && failedRun.failedStage === "processing",
  `${failedRun.status} / ${failedRun.failedStage}`,
);
check(
  "the API now carries the reason per ticket",
  (failedRun.runTickets ?? []).every((rt) => rt.genStatus !== "error" || rt.analysisError),
  "analysisError present on every errored ticket",
);
check(
  "the partial fixture is still reviewable",
  partialRun.status === "review" &&
    (partialRun.runTickets ?? []).some((rt) => rt.genStatus === "error") &&
    (partialRun.runTickets ?? []).some((rt) => rt.genStatus === "done"),
  partialRun.status,
);

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
  await page.waitForTimeout(800);
};
const runRoot = (id) => `/projects/${PROJECT_GUID}/runs/${id}`;

// ------------------------------------------------------- the total failure
await goto(runRoot(FAILED_RUN));
await page.waitForSelector("[data-testid=run-overlay]", { timeout: 15000 });
check("the failed run lands on Review", path().endsWith("/review"), path());

await page.waitForSelector("[data-testid=run-failure-chip]", { timeout: 10000 });
const chip = (await page.textContent("[data-testid=run-failure-chip]"))?.trim();
check("the overlay names the stage it failed at", /analyze/i.test(chip ?? ""), chip ?? "");

await page.waitForSelector("[data-testid=run-generation-failure]", { timeout: 10000 });
const panel = (await page.textContent("[data-testid=run-generation-failure]")) ?? "";
check("the failure panel replaces the empty state", true);
check(
  "the provider's own message is shown verbatim",
  panel.includes("Invalid API key") && panel.includes("credential expired"),
  panel.replace(/\s+/g, " ").slice(0, 110),
);
check(
  'the misleading "hasn\'t generated any yet" copy is gone',
  !panel.toLowerCase().includes("hasn't generated") &&
    !(await page.evaluate(() =>
      document.body.innerText.toLowerCase().includes("hasn't generated any test cases"),
    )),
);
check(
  "Retry generation is offered",
  (await page.$("[data-testid=run-generation-retry]")) !== null,
);
// A failed run must not tick every stage — that would say it completed the
// pipeline, which is the same false reassurance in a different place.
const pillStates = await page.$$eval("[data-testid^=run-stage-]", (els) =>
  els.map((e) => e.getAttribute("data-state")),
);
check(
  "a failed run does NOT read as all stages complete",
  pillStates.some((state) => state !== "complete"),
  pillStates.join(","),
);

const hint = await page.evaluate(() => {
  const footer = document.querySelector("[data-testid=run-overlay] footer");
  return footer?.innerText ?? "";
});
check(
  "the footer names the failure instead of a gate the user cannot satisfy",
  /failed/i.test(hint) && !/approve at least one/i.test(hint),
  hint.replace(/\s+/g, " ").slice(0, 90),
);
await page.screenshot({ path: `${OUT}/failure-total.png` });

// ----------------------------------------------------- the partial failure
await goto(`${runRoot(PARTIAL_RUN)}/review`);
await page.waitForSelector("[data-testid=run-generation-failure]", { timeout: 10000 });
check("a partial failure also surfaces its reason", true);
check(
  "a partial failure has NO failure chip — the run is still reviewable",
  (await page.$("[data-testid=run-failure-chip]")) === null,
);
const survived = await page.evaluate(
  () => document.querySelectorAll("[data-testid=run-overlay] [data-ticket]").length,
);
check(
  "the surviving cases are still listed below the panel",
  (await page.evaluate(() =>
    document.body.innerText.includes("A surviving case"),
  )) || survived > 0,
);
await page.screenshot({ path: `${OUT}/failure-partial.png` });

writeFileSync(`${OUT}/probe758.json`, JSON.stringify({ results, consoleErrors }, null, 2));
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
if (consoleErrors.length) console.log("console errors:\n" + consoleErrors.slice(0, 10).join("\n"));
await browser.close();
process.exit(failed.length ? 1 : 0);
