/**
 * Runtime probe for the Review Center's QC-voice "technical wording" badge (#829).
 *
 * Same setup and the same four documented traps as `probe-provenance-panel.mjs`
 * — read its header. In short:
 *
 *  1. Interception matches on a **predicate** over the pathname (`/api/` or
 *     `/auth/`), never a `**\/auth/**` glob.
 *  2. Navigation is **client-side** through the app's own UI; `page.goto` on a
 *     run-scoped URL is a full reload and the access token is memory-only.
 *  3. `addInitScript` pre-sets `localStorage["qagent.tourSeen"] = "1"` — the tour
 *     blocker eats every click AND auto-navigates the shell.
 *  4. Requests are **counted**: with `staleTime: 15_000` a revisited screen
 *     issues none, so a passing assertion can be reading cache.
 *
 * The fixture carries THREE cases on purpose — one clean, one with findings, one
 * more clean — because a screen that badged every case would otherwise pass a
 * probe that only ever looked at the badged one.
 *
 * Usage: `npm run dev`, then `node scripts/probe-review-voice-badge.mjs`.
 */
import { chromium } from "playwright";
import { mkdirSync, writeFileSync } from "node:fs";

const BASE = process.env.BASE ?? "http://localhost:5173";
const OUT = process.env.OUT ?? "./probe-out/829";
const GUID = "11111111-2222-3333-4444-555555555555";
const RUN_ID = 7;

mkdirSync(OUT, { recursive: true });

const results = [];
const check = (name, ok, detail = "") => {
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? " — " + detail : ""}`);
};

// ------------------------------------------------------------------ fixtures
const USER = {
  id: 1,
  email: "probe@example.com",
  firstName: "Pro",
  lastName: "Be",
  role: "admin",
  isActive: true,
  totpEnabled: false,
  lastActive: new Date().toISOString(),
};

const PROJECT = {
  id: 1,
  guid: GUID,
  providerKind: "jira",
  externalId: "DEMO",
  name: "demo",
  active: true,
  hubProjectId: null,
  meta: {},
};

const NOW = new Date().toISOString();

// Complete per `RunOut` — a fixture missing e.g. `ticketIds` blanks the whole
// shell with an error boundary, which reads as a routing failure and is not.
const RUN = {
  id: RUN_ID,
  code: "RUN-0007",
  name: "Broker regression",
  scope: "tickets",
  scopeLabel: "1 work item",
  framework: "Playwright",
  browser: "Chromium",
  env: "Staging",
  workers: 4,
  retryPolicy: 1,
  status: "review",
  createdAt: NOW,
  projectGuid: GUID,
  linkEnabled: false,
  linkDryRun: false,
  linkTicketIds: [],
  ticketIds: ["SUR-1402"],
  caseCount: 3,
  total: 0,
  passed: 0,
  passRate: null,
  result: "not_run",
  runTickets: [
    {
      id: 1,
      runId: RUN_ID,
      ticketExternalId: "SUR-1402",
      position: 0,
      genStatus: "done",
      analysis: {},
      analysisError: "",
      repo: "surency-web",
    },
  ],
};

const baseCase = (id, code, title, voiceFindings) => ({
  id,
  runId: RUN_ID,
  ticketExternalId: "SUR-1402",
  code,
  title,
  objective: "Prove the confirmation step cannot be skipped.",
  precondition: "Signed in as an internal admin with an active agency in the list.",
  steps: [
    { a: "Open the actions menu on an active agency.", e: "The menu offers Deactivate." },
    { a: "Choose Deactivate.", e: "A confirmation dialog opens." },
  ],
  testData: [{ field: "Agency name", value: "Northgate Benefits" }],
  linkedAc: ["AC1"],
  priority: "High",
  testType: "Functional",
  automation: "Playwright",
  platform: "Web",
  duration: "—",
  approval: "pending",
  source: "ai",
  edited: false,
  voiceFindings,
});

const CASES = [
  baseCase(1, "TC-01", "Deactivating an agency asks for confirmation first", []),
  baseCase(2, "TC-02", "Deactivate an agency from the list", [
    { field: "steps[0].a", rule: "css_xpath_selector", match: '[data-testid="deactivate-btn"]' },
    { field: "steps[1].a", rule: "api_path", match: "/brokers/agencies" },
    { field: "precondition", rule: "code_identifier", match: "userId" },
  ]),
  baseCase(3, "TC-03", "Cancel closes the dialog with no change", []),
];

const SETTINGS = { dryRun: false, maxCasesPerTicket: 8, executionTarget: "server" };

// --------------------------------------------------------------------- setup
const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1500, height: 1050 } });
await context.addInitScript(() => {
  window.localStorage.setItem("qagent.tourSeen", "1");
});

const seen = [];
const json = (route, body, status = 200) =>
  route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

// TRAP 1: a predicate over the pathname, not a glob.
await context.route(
  (url) => /^\/(api|auth)\//.test(url.pathname),
  async (route) => {
    const url = new URL(route.request().url());
    const p = url.pathname;
    seen.push(`${route.request().method()} ${p}${url.search}`);

    if (p === "/auth/refresh") return json(route, { detail: "no session" }, 401);
    if (p === "/auth/login") return json(route, { accessToken: "probe-access-token", user: USER });
    if (p === "/auth/me") return json(route, USER);
    if (p === "/auth/logout") return json(route, null, 204);

    if (p === "/api/projects") return json(route, [PROJECT]);
    if (p === "/api/projects/environments") return json(route, ["Staging"]);
    if (p === "/api/projects/knowledge") return json(route, []);
    if (/^\/api\/projects\/[^/]+\/repos$/.test(p)) return json(route, []);
    if (p === "/api/health") return json(route, { status: "ok" });
    if (p === "/api/settings") return json(route, SETTINGS);
    if (p === "/api/runs") return json(route, [RUN]);
    if (p === `/api/runs/${RUN_ID}`) return json(route, RUN);
    if (p === `/api/runs/${RUN_ID}/cases`) return json(route, CASES);
    if (p === `/api/runs/${RUN_ID}/linked`) return json(route, { items: [], total: 0 });
    if (p === "/api/tickets") return json(route, { items: [], total: 0 });

    // TRAP: anything not deliberately mocked falls through rather than being
    // fulfilled with `{}` — a faked endpoint is what mints bogus state.
    return route.fallback();
  },
);

const page = await context.newPage();
const consoleErrors = [];
page.on("console", (m) => {
  if (m.type() === "error") consoleErrors.push(m.text());
});

// ------------------------------------------------------------------- sign in
await page.goto(`${BASE}/login`, { waitUntil: "domcontentloaded" });
await page.locator('input[type="email"]').fill(USER.email);
await page.locator('input[type="password"]').first().fill("probe-password-123");
await page.locator('button[type="submit"]').click();
await page.waitForURL((u) => !u.pathname.endsWith("/login"), { timeout: 20000 });
check("signs in through the real form", true, page.url());

// TRAP 2: client-side navigation only, through the app's own UI.
await page.waitForTimeout(1500);
await page.locator("[data-tour=nav-projects]").first().click({ force: true });
await page.waitForTimeout(1200);
await page.getByText("demo", { exact: true }).last().click({ force: true });
await page.waitForTimeout(1200);
await page.getByRole("button", { name: /^runs$/i }).first().click({ force: true });
await page.waitForTimeout(1200);
// The run CARD in the list, not the sidebar tree row of the same code.
await page.getByText("Broker regression", { exact: false }).first().click({ force: true });
await page.waitForTimeout(2000);

// Opening a run in `review` status lands directly on the Review Center — the
// run workspace resumes at the run's own stage, so there is no second click.
check(
  "Review Center reached client-side",
  new URL(page.url()).pathname === `/projects/${GUID}/runs/${RUN_ID}/review`,
  page.url(),
);
// TRAP 4: the shell PREFETCHES /runs/7/cases while the run card is on screen,
// so an "on arrival" assertion would be reading react-query cache. What can be
// asserted honestly is that the request genuinely happened against the mock at
// least once — the rendered rows below are therefore the fixture, not a stub.
check(
  "GET /runs/7/cases actually fired",
  seen.some((r) => /\/runs\/7\/cases/.test(r)),
  seen.filter((r) => /\/runs\/7\/cases/.test(r)).join(" | "),
);

// ---------------------------------------------------------------- the badge
await page.getByText("SUR-1402", { exact: false }).first().click();
await page.waitForTimeout(900);
await page.screenshot({ path: `${OUT}/review-list.png`, fullPage: true });

// `exact: true` is the case-SENSITIVE form. Without it the badge
// ("Technical wording") and the panel heading ("TECHNICAL WORDING") match each
// other, and the negative control below silently passes for the wrong reason.
const badged = await page.getByText("Technical wording", { exact: true }).count();
check(
  "exactly one of the three cases carries the badge",
  badged === 1,
  `badge occurrences: ${badged}`,
);

// Expand the badged case and read the findings panel.
await page.getByText("Deactivate an agency from the list").first().click();
await page.waitForTimeout(900);
const panel = page.getByText("TECHNICAL WORDING", { exact: true });
check("the findings panel renders when the case is expanded", (await panel.count()) > 0);

const body = await page.locator("text=/3 phrases read like code/").count();
check("it says how many phrases, in words", body > 0);

for (const [label, match] of [
  ["Selector", '[data-testid="deactivate-btn"]'],
  ["Route or endpoint", "/brokers/agencies"],
  ["Code identifier", "userId"],
]) {
  const labelled = await page.getByText(label, { exact: true }).count();
  const phrase = await page.getByText(match, { exact: false }).count();
  check(`finding shown: ${label} -> ${match}`, labelled > 0 && phrase > 0);
}

const rewrite = page.getByRole("button", { name: /rewrite for qc voice/i });
check("the panel offers a retry action", (await rewrite.count()) > 0);

// The panel sits below the fold of the shell's own scroll container, so bring
// it into view before the screenshot — otherwise the image proves nothing.
await rewrite.first().scrollIntoViewIfNeeded();
await page.waitForTimeout(400);
await page.screenshot({ path: `${OUT}/review-findings.png` });

// Negative control: a clean case must have neither badge nor panel.
await page.getByText("Deactivate an agency from the list").first().click(); // collapse
await page.waitForTimeout(500);
await page.getByText("Cancel closes the dialog with no change").first().click();
await page.waitForTimeout(900);
check(
  "a clean case shows no findings panel and no badge on its own row",
  (await page.getByText("TECHNICAL WORDING", { exact: true }).count()) === 0,
);
await page.screenshot({ path: `${OUT}/review-clean-case.png`, fullPage: true });

// ---------------------------------------------------------------- Vietnamese
// ADR 0011: the badge, the panel and the retry action must exist in vi too. The
// language switch is a topbar button, so this stays client-side.
await page.getByRole("button", { name: /^VI$/ }).first().click();
await page.waitForFunction(
  () => window.localStorage.getItem("qagent.lang") === "vi",
  { timeout: 10000 },
);
await page.waitForTimeout(800);
await page.getByText("Deactivate an agency from the list").first().click({ force: true });
await page.waitForTimeout(800);
check(
  "the badge is translated in vi",
  (await page.getByText("Ngôn ngữ kỹ thuật", { exact: true }).count()) === 1,
);
check(
  "the panel heading is translated in vi",
  (await page.getByText("NGÔN NGỮ KỸ THUẬT", { exact: true }).count()) === 1,
);
const viRewrite = page.getByRole("button", { name: /viết lại theo giọng qc/i });
check("the retry action is translated in vi", (await viRewrite.count()) > 0);
check(
  "the rule labels are translated in vi",
  (await page.getByText("Đường dẫn hoặc endpoint", { exact: true }).count()) === 1,
);
await viRewrite.first().scrollIntoViewIfNeeded();
await page.waitForTimeout(400);
await page.screenshot({ path: `${OUT}/review-findings-vi.png` });

writeFileSync(
  `${OUT}/probe829.json`,
  JSON.stringify({ results, consoleErrors, seen }, null, 2),
);
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
if (consoleErrors.length) console.log("console errors:\n" + consoleErrors.slice(0, 10).join("\n"));
await browser.close();
process.exit(failed.length ? 1 : 0);
