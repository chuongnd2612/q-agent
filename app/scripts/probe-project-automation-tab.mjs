/**
 * Runtime probe for the project Automation tab (#770).
 *
 * The tab's read endpoints (#768) are being built in parallel and are not on
 * `master` yet, so this probe **deliberately** mocks exactly them plus the
 * handful of calls the shell needs to reach the tab. Everything else is
 * `route.fallback()` — never a blanket `{}`, which would mint a bogus session
 * out of a faked `/auth/refresh` and crash the shell.
 *
 * All four documented traps are handled here, each of which otherwise produces a
 * green-looking run that proved nothing:
 *
 *  1. Interception matches on a **predicate** over the pathname (`/api/` or
 *     `/auth/`), never a `**\/auth/**` glob — that glob also swallows Vite's own
 *     `/src/screens/auth/*.tsx` dev modules and silently blanks the login page.
 *  2. Navigation is **client-side**: the real login form, then clicks through
 *     Projects to the project to the Automation tab. `page.goto` on the tab URL is
 *     a full reload and the access token is memory-only, so it boots anonymous and
 *     `RequireAuth` correctly bounces to /login.
 *  3. `addInitScript` pre-sets `localStorage["qagent.tourSeen"] = "1"` before the
 *     first navigation — the tour blocker is `fixed inset-0 z-[70]` and it also
 *     auto-navigates the shell.
 *  4. Every automation request is **counted**, because with `staleTime: 15_000` a
 *     revisited screen issues none and a passing assertion can be reading cache.
 *
 * Usage: `npm run dev`, then `node scripts/probe-project-automation-tab.mjs`.
 * Scenarios: `SCENARIO=multi|scaffold|norepo|many` (default `multi`).
 */
import { chromium } from "playwright";
import { mkdirSync, writeFileSync } from "node:fs";

const BASE = process.env.BASE ?? "http://localhost:5173";
const OUT = process.env.OUT ?? "./probe-out/770";
const SCENARIO = process.env.SCENARIO ?? "multi";
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
const repo = (id, name, fileCount, specCount) => ({
  id,
  repo: name,
  repoLabel: name || "default",
  slug: `demo-${name || "default"}`,
  baseVersion: "1.4.2",
  fileCount,
  specCount,
  updatedAt: NOW,
});

const REPOS = {
  // Two repos -> the segmented control. The one with the most specs is the
  // default selection even though it is not first in the list.
  multi: [repo(2, "surency", 9, 4), repo(7, "checkout", 3, 0)],
  // A scaffolded repo with zero mirrored files.
  scaffold: [repo(3, "", 0, 0)],
  // No AutomationProject row at all.
  norepo: [],
  // Four repos -> the dropdown.
  many: [
    repo(2, "surency", 9, 4),
    repo(7, "checkout", 3, 0),
    repo(8, "billing", 5, 2),
    repo(9, "admin-portal", 4, 1),
  ],
}[SCENARIO];

const file = (path, kind, size) => ({ path, kind, size, updatedAt: NOW });
const TREE = {
  2: [
    file("tests/DEMO-1/DEMO-1-TC-01.spec.ts", "spec", 1820),
    file("tests/DEMO-1/DEMO-1-TC-02.spec.ts", "spec", 2140),
    file("tests/SUR-14/SUR-14-TC-01.spec.ts", "spec", 1610),
    file("tests/SUR-14/SUR-14-TC-02.spec.ts", "spec", 1990),
    file("pages/LoginPage.ts", "page", 980),
    file("pages/CheckoutPage.ts", "page", 1240),
    file("components/NavBar.ts", "component", 460),
    file("fixtures/auth.ts", "fixture", 720),
    file("playwright.config.ts", "config", 610),
  ],
  // "Files but zero specs" — NOT an empty state: the tree renders, the Specs
  // group is simply absent.
  7: [
    file("pages/CartPage.ts", "page", 1100),
    file("fixtures/basket.ts", "fixture", 540),
    file("playwright.config.ts", "config", 610),
  ],
  3: [],
  8: [file("pages/InvoicePage.ts", "page", 800)],
  9: [file("pages/AdminPage.ts", "page", 800)],
};

const CODE = `import { test, expect } from "@playwright/test";
import { LoginPage } from "../../pages/LoginPage";

/** DEMO-1-TC-01 — a signed-in user reaches the dashboard. */
test("signed-in user reaches the dashboard", async ({ page }) => {
  const login = new LoginPage(page);
  await login.goto();
  await login.signIn("probe@example.com", "probe-password-123");
  await expect(page.getByRole("heading", { name: /dashboard/i })).toBeVisible();
});
`;

// --------------------------------------------------------------------- setup
const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1500, height: 980 } });
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
    const method = route.request().method();
    seen.push(`${method} ${p}${url.search}`);

    // ---- auth: the only session is the one minted by the real login form.
    if (p === "/auth/refresh") return json(route, { detail: "no session" }, 401);
    if (p === "/auth/login")
      return json(route, { accessToken: "probe-access-token", user: USER });
    if (p === "/auth/me") return json(route, USER);
    if (p === "/auth/logout") return json(route, null, 204);

    // ---- the automation reads under test (#768).
    const repos = /^\/api\/projects\/([^/]+)\/automation\/repos$/.exec(p);
    if (repos) return json(route, REPOS);
    const files = /^\/api\/projects\/([^/]+)\/automation\/repos\/(\d+)\/files$/.exec(p);
    if (files) {
      const id = Number(files[2]);
      const rows = TREE[id] ?? [];
      return json(route, {
        projectId: id,
        repo: REPOS.find((r) => r.id === id)?.repo ?? "",
        slug: `demo-${id}`,
        baseVersion: "1.4.2",
        headCommit: "9f2c1ab7d3e4f5061728394a5b6c7d8e9f001122",
        fileCount: rows.length,
        updatedAt: NOW,
        files: rows,
      });
    }
    const one = /^\/api\/projects\/([^/]+)\/automation\/repos\/(\d+)\/file$/.exec(p);
    if (one) {
      const path = url.searchParams.get("path") ?? "";
      const id = Number(one[2]);
      const row = (TREE[id] ?? []).find((f) => f.path === path);
      if (!row) return json(route, { detail: "not found" }, 404);
      return json(route, {
        path: row.path,
        kind: row.kind,
        code: CODE,
        size: row.size,
        updatedAt: row.updatedAt,
        sha256: "0".repeat(64),
        // Slice #772 renders this; the tab only has to carry it.
        provenance: null,
      });
    }

    // ---- the shell + project chrome needed to REACH the tab.
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

// TRAP 2: client-side navigation only, through the app's own UI. `page.goto`
// would reload and boot anonymous.
const nav = async (to) => {
  await page.evaluate((t) => {
    window.history.pushState({}, "", t);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, to);
  await page.waitForTimeout(500);
};

// Reach the project list through the sidebar (a button, not an <a href>), then
// the project card, then the Automation tab — all real clicks.
await page.getByRole("button", { name: /^all projects$/i }).first().click();
await page.waitForTimeout(900);
check("sidebar click reaches /projects", new URL(page.url()).pathname === "/projects", page.url());

// The card, not the sidebar row — both read "demo", and the card is later in
// document order.
await page.getByText("demo", { exact: true }).last().click();
await page.waitForTimeout(800);
check(
  "project card opens the project layout",
  new URL(page.url()).pathname.startsWith(`/projects/${GUID}`),
  page.url(),
);

const before = seen.length;
await page.getByRole("button", { name: /^automation$/i }).first().click();
await page.waitForTimeout(1400);
const path = () => new URL(page.url()).pathname;
check("Automation tab is a real route", path() === `/projects/${GUID}/automation`, path());

// TRAP 4: the requests must actually have fired on this first visit.
const fresh = seen.slice(before);
const reposCalls = fresh.filter((r) => /\/automation\/repos(\?|$)/.test(r));
check(
  "GET .../automation/repos fired",
  reposCalls.length > 0,
  reposCalls.join(" | ") || "(none)",
);

if (SCENARIO === "norepo") {
  await page.waitForSelector('[data-testid="automation-no-repo"]', { timeout: 8000 });
  check("empty state 1: no automation repo at all", true);
  check(
    "no Bootstrap/Adopt button is offered",
    (await page.getByRole("button", { name: /bootstrap|adopt/i }).count()) === 0,
  );
  await page.screenshot({ path: `${OUT}/norepo.png` });
} else if (SCENARIO === "scaffold") {
  await page.waitForSelector('[data-testid="automation-scaffold-only"]', { timeout: 8000 });
  check("empty state 2: scaffolded, zero files", true);
  check(
    "the ZIP export stays available for a scaffold-only repo",
    (await page.getByText(/export automation project/i).count()) > 0,
  );
  check(
    "the selector is hidden at exactly one repo",
    (await page.locator('[data-testid^="repo-selector-"]').count()) === 0,
  );
  await page.screenshot({ path: `${OUT}/scaffold-only.png`, fullPage: true });
} else {
  const treeCalls = fresh.filter((r) => /\/automation\/repos\/\d+\/files/.test(r));
  check(
    "GET .../repos/{id}/files fired for the auto-selected repo",
    treeCalls.some((r) => r.includes("/repos/2/files")),
    treeCalls.join(" | ") || "(none)",
  );
  check(
    "the repo with the MOST specs is auto-selected, not the first",
    treeCalls.every((r) => !r.includes("/repos/7/files")),
    treeCalls.join(" | "),
  );

  // No file content is fetched until a file is selected — that split is the
  // point of #768.
  check(
    "no file content is fetched before a selection",
    fresh.every((r) => !/\/file\?path=/.test(r)),
    fresh.filter((r) => r.includes("/file?path=")).join(" | ") || "(none, correct)",
  );

  const selector =
    SCENARIO === "many" ? "repo-selector-dropdown" : "repo-selector-segmented";
  check(
    `the selector renders as a ${SCENARIO === "many" ? "dropdown" : "segmented control"}`,
    (await page.locator(`[data-testid="${selector}"]`).count()) === 1,
  );
  await page.screenshot({ path: `${OUT}/repo-header.png` });

  if (SCENARIO === "many") {
    // The dropdown must portal to document.body — the project layout wraps this
    // tab in a `motion.div`, a transform stacking context that traps z-index.
    await page.locator('[data-testid="repo-selector-dropdown"] button').first().click();
    await page.waitForTimeout(400);
    const portalled = await page.evaluate(() => {
      const panels = [...document.body.children].filter(
        (el) => el.querySelector && el.textContent?.includes("admin-portal"),
      );
      return panels.some((el) => !el.closest("[data-testid]"));
    });
    check("the 4+ repo dropdown panel is portalled to document.body", portalled);
    await page.screenshot({ path: `${OUT}/repo-dropdown-open.png` });
    await page.keyboard.press("Escape");
    await page.mouse.click(20, 20);
  }

  // ---- open a spec: the lazy content request must fire now, and only now.
  const beforeFile = seen.length;
  // The Specs group is `defaultOpen` in ProjectFileTree, so it is already
  // expanded — clicking its header would COLLAPSE it. `force` because the
  // group's collapse animation still owns pointer events for a beat.
  await page
    .locator("button", { hasText: "DEMO-1-TC-01.spec.ts" })
    .first()
    .click({ force: true });
  await page.waitForTimeout(1300);
  const fileCalls = seen
    .slice(beforeFile)
    .filter((r) => /\/automation\/repos\/2\/file\?path=/.test(r));
  check(
    "selecting a row fires GET .../file?path= exactly then",
    fileCalls.length > 0,
    fileCalls.join(" | ") || "(none)",
  );
  check(
    "the open file is written to ?file=",
    new URL(page.url()).searchParams.get("file") === "tests/DEMO-1/DEMO-1-TC-01.spec.ts",
    page.url(),
  );
  check(
    "the code panel rendered the fetched content",
    (await page.getByText("signed-in user reaches the dashboard").count()) > 0,
  );
  check(
    "every tree row shows the read-only lock (specPath=\"\")",
    (await page.locator('button[aria-current] svg').count()) > 0,
  );
  await page.screenshot({ path: `${OUT}/file-open.png`, fullPage: true });

  // ---- switch repos: the file must be dropped and the new tree fetched.
  if (SCENARIO !== "norepo" && REPOS.length > 1) {
    const beforeSwitch = seen.length;
    if (SCENARIO === "many") {
      await page.locator('[data-testid="repo-selector-dropdown"] button').first().click();
      await page.waitForTimeout(300);
      await page.getByText("checkout", { exact: true }).first().click();
    } else {
      await page
        .locator('[data-testid="repo-selector-segmented"] button')
        .filter({ hasText: "checkout" })
        .click();
    }
    await page.waitForTimeout(1300);
    const u = new URL(page.url());
    check("switching repos writes ?repo= to the new id", u.searchParams.get("repo") === "7", u.search);
    check("switching repos drops the stale ?file=", u.searchParams.get("file") === null, u.search);
    check(
      "the new repo's tree was actually fetched",
      seen.slice(beforeSwitch).some((r) => r.includes("/repos/7/files")),
      seen.slice(beforeSwitch).join(" | "),
    );
    // Files but zero specs is NOT an empty state.
    check(
      "files-but-no-specs renders the tree plus a note, not an empty state",
      (await page.locator('[data-testid="automation-no-specs-note"]').count()) === 1 &&
        (await page.getByText("CartPage.ts", { exact: false }).count()) > 0 &&
        (await page.locator('[data-testid="automation-scaffold-only"]').count()) === 0,
    );
    await page.screenshot({ path: `${OUT}/no-specs.png`, fullPage: true });

    // A `?file=` from the previous repo must not be re-requested if replayed.
    const beforeStale = seen.length;
    await nav(`/projects/${GUID}/automation?repo=7&file=tests/DEMO-1/DEMO-1-TC-01.spec.ts`);
    await page.waitForTimeout(900);
    check(
      "a ?file= not in the selected repo's tree issues no request",
      seen.slice(beforeStale).every((r) => !/\/repos\/7\/file\?path=/.test(r)),
      seen.slice(beforeStale).join(" | ") || "(none, correct)",
    );
  }

  // Back-stack: every selection was written with { replace: true }.
  const entries = await page.evaluate(() => window.history.length);
  check("selections use replace:true (short back stack)", entries < 12, `history.length=${entries}`);
}

const benign = (m) =>
  /Failed to load resource|net::ERR_|ECONNREFUSED|502|500 \(Internal|WebSocket|Failed to fetch/i.test(m);
const realErrors = consoleErrors.filter((m) => !benign(m));
check("no unexplained console errors", realErrors.length === 0, realErrors.slice(0, 4).join(" | "));

writeFileSync(
  `${OUT}/probe-${SCENARIO}.json`,
  JSON.stringify({ scenario: SCENARIO, results, requests: seen, consoleErrors }, null, 2),
);
const failed = results.filter((r) => !r.ok);
console.log(`\n[${SCENARIO}] ${results.length - failed.length}/${results.length} passed`);
if (consoleErrors.length) console.log("console:\n" + consoleErrors.slice(0, 8).join("\n"));
await browser.close();
process.exit(failed.length ? 1 : 0);
