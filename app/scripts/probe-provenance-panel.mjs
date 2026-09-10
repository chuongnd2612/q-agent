/**
 * Runtime probe for the project Automation tab's ProvenancePanel (#772).
 *
 * Follows the pattern established by `probe-project-automation-tab.mjs` (#770)
 * and handles the same four documented traps, each of which otherwise produces a
 * green-looking run that proved nothing:
 *
 *  1. Interception matches on a **predicate** over the pathname (`/api/` or
 *     `/auth/`), never a `**\/auth/**` glob — that glob also swallows Vite's own
 *     `/src/screens/auth/*.tsx` dev modules and silently blanks the login page.
 *  2. Navigation is **client-side**: the real login form, then real clicks through
 *     Projects → the project → the Automation tab. `page.goto` on the tab URL is a
 *     full reload and the access token is memory-only, so it boots anonymous.
 *  3. `addInitScript` pre-sets `localStorage["qagent.tourSeen"] = "1"` before the
 *     first navigation — the tour blocker is `fixed inset-0 z-[70]` and it also
 *     auto-navigates the shell.
 *  4. Every request is **counted**, because with `staleTime: 15_000` a revisited
 *     screen issues none and a passing assertion can be reading cache. Each of the
 *     three files opened here is opened for the first time.
 *
 * `/file?path=` answers with three different provenance shapes, which is the whole
 * point: an overwritten spec (latest + two history entries, one stale), a
 * single-run spec (no history, so no toggle), and `provenance: null` for a page
 * object (the shared-asset strip).
 *
 * Usage: `npm run dev`, then `node scripts/probe-provenance-panel.mjs`.
 */
import { chromium } from "playwright";
import { mkdirSync, writeFileSync } from "node:fs";

const BASE = process.env.BASE ?? "http://localhost:5173";
const OUT = process.env.OUT ?? "./probe-out/772";
const GUID = "11111111-2222-3333-4444-555555555555";

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
const hoursAgo = (h) => new Date(Date.now() - h * 3600_000).toISOString();

const REPOS = [
  {
    id: 2,
    repo: "surency",
    repoLabel: "surency",
    slug: "demo-surency",
    baseVersion: "1.4.2",
    fileCount: 3,
    specCount: 2,
    updatedAt: NOW,
  },
];

const file = (path, kind, size) => ({ path, kind, size, updatedAt: NOW });
const TREE = [
  file("tests/SUR-14/SUR-14-TC-01.spec.ts", "spec", 1820),
  file("tests/SUR-14/SUR-14-TC-02.spec.ts", "spec", 1610),
  file("pages/LoginPage.ts", "page", 980),
];

const entry = (over) => ({
  specId: 1,
  specStatus: "passed",
  blockReason: null,
  testCaseId: 41,
  caseCode: "SUR-14-TC-01",
  caseTitle: "A signed-in user reaches the dashboard",
  ticketExternalId: "SUR-14",
  runId: 31,
  runCode: "RUN-0031",
  runName: "Nightly regression",
  runStatus: "done",
  runCreatedAt: hoursAgo(3),
  runFinishedAt: hoursAgo(2),
  stale: false,
  ...over,
});

// The headline case: run #31 rewrote a path two earlier runs had also written.
const OVERWRITTEN = {
  kind: "spec",
  overwritten: true,
  latest: entry({}),
  history: [
    entry({
      specId: 2,
      runId: 22,
      runCode: "RUN-0022",
      runName: "SUR-14 rerun",
      runStatus: "failed",
      specStatus: "failed",
      runCreatedAt: hoursAgo(30),
      runFinishedAt: hoursAgo(29),
      stale: true,
    }),
    entry({
      specId: 3,
      runId: 11,
      runCode: "RUN-0011",
      runName: "SUR-14 first pass",
      runStatus: "done",
      specStatus: "blocked",
      blockReason: "Selector for the dashboard heading was not in the DOM snapshot",
      runCreatedAt: hoursAgo(80),
      runFinishedAt: hoursAgo(79),
      stale: true,
    }),
  ],
};

// A spec written by exactly one run: no lineage, so no toggle at all.
const SINGLE = {
  kind: "spec",
  overwritten: false,
  latest: entry({
    specId: 9,
    testCaseId: 42,
    caseCode: "SUR-14-TC-02",
    caseTitle: "An expired session is bounced to the sign-in screen",
    runId: 31,
    runStatus: "done",
    specStatus: "passed",
  }),
  history: [],
};

const PROVENANCE = {
  "tests/SUR-14/SUR-14-TC-01.spec.ts": OVERWRITTEN,
  "tests/SUR-14/SUR-14-TC-02.spec.ts": SINGLE,
  // The whole reason this branch exists: no such row, so no such fact.
  "pages/LoginPage.ts": null,
};

const CODE = `import { test, expect } from "@playwright/test";
import { LoginPage } from "../../pages/LoginPage";

/** SUR-14-TC-01 — a signed-in user reaches the dashboard. */
test("signed-in user reaches the dashboard", async ({ page }) => {
  const login = new LoginPage(page);
  await login.goto();
  await login.signIn("probe@example.com", "probe-password-123");
  await expect(page.getByRole("heading", { name: /dashboard/i })).toBeVisible();
});
`;

const SHA = "3f9a1c7e2b4d6058a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718";

// --------------------------------------------------------------------- setup
const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1500, height: 1000 } });
await context.addInitScript(() => {
  window.localStorage.setItem("qagent.tourSeen", "1");
});
// The sha copy button writes to the clipboard.
await context.grantPermissions(["clipboard-read", "clipboard-write"]);

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
    if (p === "/auth/login")
      return json(route, { accessToken: "probe-access-token", user: USER });
    if (p === "/auth/me") return json(route, USER);
    if (p === "/auth/logout") return json(route, null, 204);

    if (/^\/api\/projects\/[^/]+\/automation\/repos$/.test(p)) return json(route, REPOS);
    if (/^\/api\/projects\/[^/]+\/automation\/repos\/\d+\/files$/.test(p))
      return json(route, {
        projectId: 2,
        repo: "surency",
        slug: "demo-surency",
        baseVersion: "1.4.2",
        headCommit: "9f2c1ab7d3e4f5061728394a5b6c7d8e9f001122",
        fileCount: TREE.length,
        updatedAt: NOW,
        files: TREE,
      });
    if (/^\/api\/projects\/[^/]+\/automation\/repos\/\d+\/file$/.test(p)) {
      const path = url.searchParams.get("path") ?? "";
      const row = TREE.find((f) => f.path === path);
      if (!row) return json(route, { detail: "not found" }, 404);
      return json(route, {
        path: row.path,
        kind: row.kind,
        code: CODE,
        size: row.size,
        updatedAt: hoursAgo(2),
        sha256: SHA,
        provenance: PROVENANCE[path] ?? null,
      });
    }

    if (p === "/api/projects") return json(route, [PROJECT]);
    if (p === "/api/projects/environments") return json(route, ["staging"]);
    if (p === "/api/projects/knowledge") return json(route, []);
    if (/^\/api\/projects\/[^/]+\/repos$/.test(p)) return json(route, []);
    if (p === "/api/runs") return json(route, []);
    if (p === "/api/tickets") return json(route, { items: [], total: 0 });

    // TRAP: everything not deliberately mocked falls through. Fulfilling these
    // with `{}` is what mints bogus state.
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
await page.getByRole("button", { name: /^all projects$/i }).first().click();
await page.waitForTimeout(900);
await page.getByText("demo", { exact: true }).last().click();
await page.waitForTimeout(800);
const beforeTab = seen.length;
await page.getByRole("button", { name: /^automation$/i }).first().click();
await page.waitForTimeout(1400);
check(
  "Automation tab reached client-side",
  new URL(page.url()).pathname === `/projects/${GUID}/automation`,
  page.url(),
);
// TRAP 4: prove the tab's own reads actually fired on this first visit.
check(
  "GET .../automation/repos + /files fired on arrival",
  seen.slice(beforeTab).some((r) => /\/automation\/repos(\?|$)/.test(r)) &&
    seen.slice(beforeTab).some((r) => /\/repos\/\d+\/files/.test(r)),
  seen.slice(beforeTab).join(" | "),
);

// Only the Specs group is `defaultOpen` in ProjectFileTree, so a non-spec group
// has to be expanded before its rows exist to click.
const openFile = async (label, groupTitle = null) => {
  if (groupTitle) {
    await page.locator("button", { hasText: groupTitle }).first().click({ force: true });
    await page.waitForTimeout(500);
  }
  const before = seen.length;
  await page.locator("button", { hasText: label }).first().click({ force: true });
  await page.waitForTimeout(1200);
  const calls = seen.slice(before).filter((r) => /\/file\?path=/.test(r));
  return calls;
};

// ------------------------------------------- 1. the overwritten spec (headline)
let calls = await openFile("SUR-14-TC-01.spec.ts");
check("opening the overwritten spec fired GET .../file?path=", calls.length > 0, calls.join(" | "));
await page.waitForSelector('[data-testid="provenance-spec"]', { timeout: 8000 });
check("the spec branch renders the provenance card", true);

const specCard = page.locator('[data-testid="provenance-spec"]');
const cardText = await specCard.innerText();
check(
  "the latest entry names ticket, case code, case title, run code and run name",
  ["SUR-14", "SUR-14-TC-01", "A signed-in user reaches the dashboard", "RUN-0031", "Nightly regression"].every(
    (s) => cardText.includes(s),
  ),
  cardText.replace(/\n/g, " / "),
);
check(
  "the spec status is rendered with the shared vocabulary (a SpecStatusDot + label)",
  (await specCard.locator('[data-testid="provenance-spec-status"]').count()) > 0 &&
    cardText.includes("Passed"),
);
check(
  "the run status uses the shared runBadge vocabulary",
  cardText.includes("Completed"),
  cardText.replace(/\n/g, " / "),
);

// The deep link must be a real route under the SAME :projectGuid, pointing at
// the run overlay's automation stage with the case pre-selected.
const href = await specCard.locator('[data-testid="provenance-run-link"]').first().getAttribute("href");
check(
  "the entry deep-links to the run overlay's automation stage for that case",
  href === `/projects/${GUID}/runs/31/automation?case=41`,
  String(href),
);

// Collapsed by default; only the toggle is visible.
check(
  "history is COLLAPSED by default",
  (await page.locator('[data-testid="provenance-history"]').count()) === 0 &&
    (await page.locator('[data-testid="provenance-history-toggle"]').count()) === 1,
);
check(
  "the collapsed toggle counts the earlier runs",
  (await page.locator('[data-testid="provenance-history-toggle"]').innerText()).includes(
    "2 earlier runs",
  ),
  await page.locator('[data-testid="provenance-history-toggle"]').innerText(),
);
await page.screenshot({ path: `${OUT}/spec-history-collapsed.png` });

await page.locator('[data-testid="provenance-history-toggle"]').click();
await page.waitForSelector('[data-testid="provenance-history"]', { timeout: 5000 });
await page.waitForTimeout(500);
const historyText = await page.locator('[data-testid="provenance-history"]').innerText();
check(
  "expanding shows every history entry with its own run identity",
  ["RUN-0022", "SUR-14 rerun", "RUN-0011", "SUR-14 first pass"].every((s) => historyText.includes(s)),
  historyText.replace(/\n/g, " / "),
);
check(
  "each stale entry carries the exact ADR 0014 superseded copy",
  (await page.locator('[data-testid="provenance-superseded"]').count()) === 2 &&
    (await page.locator('[data-testid="provenance-superseded"]').first().innerText()) ===
      "Superseded by RUN-0031 — this run's stored spec is not the code on disk.",
  await page.locator('[data-testid="provenance-superseded"]').first().innerText(),
);
check(
  "a blocked history entry surfaces its blockReason",
  historyText.includes("Selector for the dashboard heading"),
);
check(
  "the panel sits ABOVE the code panel in the right column",
  await page.evaluate(() => {
    const prov = document.querySelector('[data-testid="provenance-spec"]');
    // The code panel, found by a line of the fetched source rather than a tag —
    // `pre`/`code` also match markup in the left tree column.
    const line = [...document.querySelectorAll("span,div")].find(
      (el) => el.childElementCount === 0 && el.textContent === '"@playwright/test"',
    );
    if (!prov || !line) return false;
    return prov.getBoundingClientRect().bottom <= line.getBoundingClientRect().top;
  }),
);
// No backdrop-filter anywhere in the panel's ancestry-of-one: it must be opaque.
check(
  "the panel surface is opaque (no backdrop-filter)",
  await page.evaluate(() => {
    const el = document.querySelector('[data-testid="provenance-spec"]');
    const s = getComputedStyle(el);
    return (
      (s.backdropFilter === "none" || s.backdropFilter === "") &&
      /rgba?\(8, 8, 13/.test(s.backgroundColor)
    );
  }),
);
await page.screenshot({ path: `${OUT}/spec-history-expanded.png` });
await page.locator('[data-testid="provenance-spec"]').screenshot({
  path: `${OUT}/spec-history-expanded-crop.png`,
});

// ------------------------------------------- 2. a spec only one run ever wrote
calls = await openFile("SUR-14-TC-02.spec.ts");
check("opening the single-run spec fired its own request", calls.length > 0, calls.join(" | "));
await page.waitForTimeout(400);
check(
  "overwritten:false renders NO history toggle",
  (await page.locator('[data-testid="provenance-history-toggle"]').count()) === 0 &&
    (await page.locator('[data-testid="provenance-spec"]').count()) === 1,
);

// ------------------------------------------------- 3. the shared (non-spec) asset
calls = await openFile("LoginPage.ts", /Page objects/);
check("opening the page object fired its own request", calls.length > 0, calls.join(" | "));
await page.waitForSelector('[data-testid="provenance-shared"]', { timeout: 8000 });
const shared = page.locator('[data-testid="provenance-shared"]');
const sharedText = await shared.innerText();
check(
  "provenance:null renders the shared-asset strip, not a blank space",
  (await page.locator('[data-testid="provenance-spec"]').count()) === 0 &&
    sharedText.includes(
      "Shared asset — reused and extended across runs. Q-Agent does not record which run last edited it.",
    ),
  sharedText.replace(/\n/g, " / "),
);
check(
  "the strip states the two facts that ARE known: mirror sync + short sha256",
  sharedText.includes("Last mirror sync") && sharedText.includes(SHA.slice(0, 8)),
  sharedText.replace(/\n/g, " / "),
);
check(
  "only the first 8 chars of the digest are displayed",
  !sharedText.includes(SHA.slice(0, 12)),
  sharedText.replace(/\n/g, " / "),
);
check(
  "the strip points at git history and scopes it out of this slice",
  /git log --follow/.test(sharedText) && /follow-up/.test(sharedText),
);
check(
  "no run is named or implied anywhere in the shared-asset strip",
  !/RUN-\d+/.test(sharedText),
  sharedText.replace(/\n/g, " / "),
);

// The digest is copyable in FULL even though it displays short.
await shared.locator('[data-testid="provenance-sha-copy"]').click();
await page.waitForTimeout(400);
const clip = await page.evaluate(() => navigator.clipboard.readText());
check("the copy button copies the full 64-char sha256", clip === SHA, clip);
// The hover tooltip is portalled to document.body (PathTooltip), per CLAUDE.md.
// The pointer is still inside the trigger after the click above (and the click
// hides the tooltip), so it has to leave before re-entering fires pointerenter.
await page.mouse.move(10, 10);
await page.waitForTimeout(200);
await shared.locator('[data-testid="provenance-sha-copy"]').hover();
await page.waitForTimeout(350);
check(
  "the full-digest tooltip is portalled to document.body with position:fixed",
  await page.evaluate(() => {
    const tip = document.querySelector('body > [role="tooltip"]');
    return !!tip && getComputedStyle(tip).position === "fixed";
  }),
);
await page.screenshot({ path: `${OUT}/shared-asset.png` });
await shared.screenshot({ path: `${OUT}/shared-asset-crop.png` });

const benign = (m) =>
  /Failed to load resource|net::ERR_|ECONNREFUSED|502|500 \(Internal|WebSocket|Failed to fetch/i.test(m);
const realErrors = consoleErrors.filter((m) => !benign(m));
check("no unexplained console errors", realErrors.length === 0, realErrors.slice(0, 4).join(" | "));

writeFileSync(
  `${OUT}/probe.json`,
  JSON.stringify({ results, requests: seen, consoleErrors }, null, 2),
);
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
if (consoleErrors.length) console.log("console:\n" + consoleErrors.slice(0, 8).join("\n"));
await browser.close();
process.exit(failed.length ? 1 : 0);
