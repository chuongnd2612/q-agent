/**
 * Runtime probe for the Business tab's per-kind add-source form (#848).
 *
 * Follows `probe-provenance-panel.mjs` and handles the same four documented
 * traps: a **predicate** route match over the pathname (never a `**\/auth/**`
 * glob, which also swallows Vite's own `/src/screens/auth/*.tsx` dev modules);
 * client-side navigation only (the access token is memory-only, so `page.goto`
 * on a deep link boots anonymous); `qagent.tourSeen` pre-set in an
 * `addInitScript` (the tour blocker is `fixed inset-0 z-[70]` and also
 * auto-navigates the shell); and every request counted, because with
 * `staleTime: 15_000` a revisited screen issues none.
 *
 * What it proves, kind by kind — the deliverable of #848:
 *
 *  - `url`      — one address, no credential field at all.
 *  - `github_md`— an address plus an OPTIONAL connection: the placeholder says
 *                 a public repo needs no token, and a hub-backed connection
 *                 warns (anonymous read) without blocking, because a public
 *                 repo still works through it.
 *  - `ado_wiki` — a wiki-scoped PAT as the primary path, a Test button that
 *                 preflights before anything is stored, and the hub refusal:
 *                 picking an EmeHub-managed connection DISABLES the submit and
 *                 says the sentence that names the fix.
 *  - `upload`   — a file picker (.md/.txt), no URL, and a multipart POST to
 *                 `/sources/upload` rather than the JSON create.
 *
 * Screenshots land in `probe-out/848` in BOTH themes (#844's class of bug).
 *
 * Usage: `npm run dev`, then `node scripts/probe-business-source-form.mjs`.
 */
import { chromium } from "playwright";
import { mkdirSync, writeFileSync } from "node:fs";

const BASE = process.env.BASE ?? "http://localhost:5173";
const OUT = process.env.OUT ?? "./probe-out/848";
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
  providerKind: "ado",
  externalId: "DEMO",
  name: "demo",
  active: true,
  hubProjectId: null,
  meta: {},
};

const conn = (id, kind, name, over = {}) => ({
  id,
  kind,
  categories: kind === "ado" ? ["work_item", "repository"] : ["repository"],
  name,
  connected: true,
  config: {},
  secretFields: ["pat"],
  lastSync: null,
  lastTestedAt: null,
  hubBacked: false,
  ...over,
});

// One local + one hub-backed of each kind: the hub row is the whole point.
const GITHUB_LOCAL = conn(11, "github", "GitHub — acme");
const GITHUB_HUB = conn(12, "github", "GitHub via EmeHub", {
  hubBacked: true,
  secretFields: [],
});
const ADO_LOCAL = conn(21, "ado", "Azure DevOps — acme");
const ADO_HUB = conn(22, "ado", "Azure DevOps via EmeHub", {
  hubBacked: true,
  secretFields: [],
});

const PROVIDERS = [
  {
    kind: "ado",
    categories: ["work_item", "repository"],
    name: "Azure DevOps",
    connectionCount: 2,
    connectedCount: 2,
    connections: [ADO_LOCAL, ADO_HUB],
  },
  {
    kind: "github",
    categories: ["repository"],
    name: "GitHub",
    connectionCount: 2,
    connectedCount: 2,
    connections: [GITHUB_LOCAL, GITHUB_HUB],
  },
];

let nextSourceId = 100;
const sources = [];
const makeSource = (over) => ({
  id: nextSourceId++,
  projectGuid: GUID,
  projectKey: "demo",
  kind: "url",
  title: "",
  url: null,
  connectionId: null,
  status: "pending",
  lastError: "",
  fetchedAt: null,
  contentHash: "",
  byteSize: 0,
  docCount: 0,
  excluded: false,
  ...over,
});

// --------------------------------------------------------------------- setup
const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1500, height: 1100 } });
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

    if (p === "/auth/refresh") return json(route, { detail: "no session" }, 401);
    if (p === "/auth/login")
      return json(route, { accessToken: "probe-access-token", user: USER });
    if (p === "/auth/me") return json(route, USER);
    if (p === "/auth/logout") return json(route, null, 204);

    if (p === "/api/providers") return json(route, PROVIDERS);

    if (/^\/api\/projects\/[^/]+\/business\/sources$/.test(p)) {
      if (method === "GET") return json(route, sources);
      const body = JSON.parse(route.request().postData() ?? "{}");
      const row = makeSource({
        kind: body.kind,
        title: body.title || body.url,
        url: body.url ?? null,
        connectionId: body.connectionId ?? null,
      });
      sources.unshift(row);
      return json(route, row, 201);
    }
    if (/^\/api\/projects\/[^/]+\/business\/sources\/upload$/.test(p)) {
      const row = makeSource({
        kind: "upload",
        title: "glossary.md",
        status: "synced",
        docCount: 1,
        byteSize: 42,
        fetchedAt: new Date().toISOString(),
      });
      sources.unshift(row);
      return json(route, row, 201);
    }
    if (/\/business\/sources\/\d+\/ado-credential$/.test(p))
      return json(route, {
        sourceId: Number(p.match(/sources\/(\d+)/)[1]),
        origin: "source",
        hasToken: true,
        canSync: true,
        message: "This source has its own Azure DevOps token.",
      });
    if (/\/business\/sources\/\d+\/sync$/.test(p)) {
      const id = Number(p.match(/sources\/(\d+)/)[1]);
      const row = sources.find((s) => s.id === id);
      if (row) row.status = "synced";
      return json(route, row ?? {}, 202);
    }
    if (/\/business\/ado\/preflight$/.test(p))
      return json(route, {
        ok: true,
        project: "Payments",
        wiki: "Payments.wiki",
        wikis: ["Payments.wiki"],
      });

    if (p === "/api/projects") return json(route, [PROJECT]);
    if (p === "/api/projects/environments") return json(route, ["staging"]);
    if (p === "/api/projects/knowledge") return json(route, []);
    if (/^\/api\/projects\/[^/]+\/repos$/.test(p)) return json(route, []);
    if (p === "/api/runs") return json(route, []);
    if (p === "/api/tickets") return json(route, { items: [], total: 0 });

    // TRAP: anything not deliberately mocked falls through. Fulfilling it with
    // `{}` is what mints bogus state.
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

// TRAP 2: client-side navigation, through the app's own UI.
await page.getByRole("button", { name: /^all projects$/i }).first().click();
await page.waitForTimeout(900);
await page.getByText("demo", { exact: true }).last().click();
await page.waitForTimeout(800);
const beforeTab = seen.length;
await page.getByRole("button", { name: /^business/i }).first().click();
await page.waitForTimeout(1200);
check(
  "Business tab reached client-side",
  new URL(page.url()).pathname === `/projects/${GUID}/business`,
  page.url(),
);
// TRAP 4: prove the tab's own read actually fired on this first visit.
check(
  "GET .../business/sources fired on arrival",
  seen.slice(beforeTab).some((r) => /GET .*\/business\/sources$/.test(r)),
  seen.slice(beforeTab).join(" | "),
);

// --------------------------------------------------------------- open the form
await page.locator('[data-testid="business-empty-cta"]').click();
await page.waitForSelector('[data-testid="business-add-form"]', { timeout: 5000 });

const form = page.locator('[data-testid="business-add-form"]');
const formText = () => form.innerText();
const kindOf = () => form.getAttribute("data-kind");
const submit = page.locator('[data-testid="business-submit"]');

/** Pick an option in one of the form's Selects, by the field's label. */
const pick = async (label, option) => {
  await page.locator("label", { hasText: label }).first().locator("button").first().click();
  await page.waitForTimeout(250);
  await page.locator(`button:has-text(${JSON.stringify(option)})`).last().click();
  await page.waitForTimeout(350);
};

const shot = async (name) => {
  await page.waitForTimeout(250);
  await form.screenshot({ path: `${OUT}/${name}.png` });
};

// ------------------------------------------------------------------ 1. `url`
check("the form opens on `url`", (await kindOf()) === "url");
let text = await formText();
check(
  "url: one address, no credential field",
  text.includes("Links on it are not followed") &&
    (await form.locator('[data-testid="business-url-input"]').count()) === 1 &&
    (await form.locator('[data-testid="business-pat-input"]').count()) === 0 &&
    (await form.locator('[data-testid="business-file-input"]').count()) === 0,
  text.replace(/\n/g, " / "),
);
await form
  .locator('[data-testid="business-url-input"]')
  .fill("https://wiki.example.com/eligibility");
await shot("dark-1-url");

// ----------------------------------------------------------- 2. `github_md`
await pick("Source type", "GitHub Markdown");
check("switched to github_md", (await kindOf()) === "github_md");
text = await formText();
check(
  "github: says a PUBLIC repo needs no token, and warns about a slashed ref",
  text.includes("needs no token at all") && text.includes("release/2024"),
  text.replace(/\n/g, " / "),
);
await form
  .locator('[data-testid="business-url-input"]')
  .fill("https://github.com/acme/handbook/tree/main/docs");
await shot("dark-2-github-public");

await pick("GitHub connection", "GitHub via EmeHub");
check(
  "github + hub-backed connection: warns, but does NOT block (a public repo still reads)",
  (await form.locator('[data-testid="business-github-hub"]').count()) === 1 &&
    (await submit.isEnabled()),
  (await form.locator('[data-testid="business-github-hub"]').innerText()).replace(/\n/g, " "),
);
await shot("dark-3-github-hub");

await pick("GitHub connection", "GitHub — acme");
check(
  "github + a local connection: no warning, submit enabled",
  (await form.locator('[data-testid="business-github-hub"]').count()) === 0 &&
    (await submit.isEnabled()),
);

// ------------------------------------------------------------ 3. `ado_wiki`
await pick("Source type", "Azure DevOps wiki");
check("switched to ado_wiki", (await kindOf()) === "ado_wiki");
await form
  .locator('[data-testid="business-url-input"]')
  .fill("https://dev.azure.com/acme/Payments/_wiki/wikis/Payments.wiki");
await page.waitForTimeout(300);
check(
  "ado with no credential at all: submit is BLOCKED and says what is missing",
  (await form.locator('[data-testid="business-ado-missing"]').count()) === 1 &&
    (await submit.isDisabled()),
  (await form.locator('[data-testid="business-ado-missing"]').innerText()).replace(/\n/g, " "),
);
await shot("dark-4-ado-missing");

await pick("Azure DevOps connection (optional)", "Azure DevOps via EmeHub");
const hubNote = form.locator('[data-testid="business-ado-hub"]');
const hubText = (await hubNote.count()) ? await hubNote.innerText() : "";
check(
  "ado + hub-backed connection: REFUSED, in the words that name the fix",
  (await hubNote.count()) === 1 &&
    hubText.includes("never releases its Azure DevOps token") &&
    hubText.includes("Add a wiki-scoped token") &&
    (await submit.isDisabled()),
  hubText.replace(/\n/g, " "),
);
await shot("dark-5-ado-hub");

// A wiki-scoped token is the primary path — and it beats the hub connection.
await form.locator('[data-testid="business-pat-input"]').fill("probe-wiki-pat");
await page.waitForTimeout(300);
check(
  "a per-source token unblocks the submit even with the hub connection picked",
  (await form.locator('[data-testid="business-ado-source"]').count()) === 1 &&
    (await submit.isEnabled()),
);

const beforeTest = seen.length;
await page.locator('[data-testid="business-ado-test"]').click();
await page.waitForSelector('[data-testid="business-ado-tested"]', { timeout: 8000 });
check(
  "Test preflights the wiki BEFORE anything is stored, and reports what it read",
  seen.slice(beforeTest).some((r) => /POST .*\/business\/ado\/preflight$/.test(r)) &&
    (await form.locator('[data-testid="business-ado-tested"]').innerText()).includes(
      "Payments.wiki",
    ),
  seen.slice(beforeTest).join(" | "),
);
await shot("dark-6-ado-token");

// Submitting must create the row, store the token, THEN sync — in that order.
const beforeSubmit = seen.length;
await submit.click();
await page.waitForTimeout(1500);
const submitCalls = seen.slice(beforeSubmit).filter((r) => r.startsWith("P"));
check(
  "ado submit: preflight → create → PUT ado-credential → sync, in order",
  /preflight/.test(submitCalls[0] ?? "") &&
    /POST .*\/business\/sources$/.test(submitCalls[1] ?? "") &&
    /PUT .*ado-credential$/.test(submitCalls[2] ?? "") &&
    /sync$/.test(submitCalls[3] ?? ""),
  submitCalls.join(" | "),
);

// -------------------------------------------------------------- 4. `upload`
await page.locator('[data-testid="business-add-toggle"]').click();
await page.waitForSelector('[data-testid="business-add-form"]', { timeout: 5000 });
await pick("Source type", "Uploaded document");
check("switched to upload", (await kindOf()) === "upload");
text = await formText();
check(
  "upload: a FILE field, the .md/.txt limit, and no URL field at all",
  (await form.locator('[data-testid="business-file-input"]').count()) === 1 &&
    (await form.locator('[data-testid="business-url-input"]').count()) === 0 &&
    text.includes(".md and .txt"),
  text.replace(/\n/g, " / "),
);
check("upload with no file chosen cannot be submitted", await submit.isDisabled());
await shot("dark-7-upload-empty");

const filePath = `${OUT}/glossary.md`;
writeFileSync(filePath, "# Glossary\n\nA **member** is a person enrolled in a plan.\n");
await form.locator('[data-testid="business-file-input"]').setInputFiles(filePath);
await page.waitForTimeout(400);
check(
  "choosing a file names it and enables the submit",
  (await form.locator('[data-testid="business-file-name"]').innerText()).includes("glossary.md") &&
    (await submit.isEnabled()),
);
await shot("dark-8-upload-chosen");

const beforeUpload = seen.length;
await submit.click();
await page.waitForTimeout(1200);
const uploadCalls = seen.slice(beforeUpload);
check(
  "upload posts MULTIPART to /sources/upload and never the JSON create, nor a sync",
  uploadCalls.some((r) => /POST .*\/business\/sources\/upload$/.test(r)) &&
    !uploadCalls.some((r) => /POST .*\/business\/sources$/.test(r)) &&
    !uploadCalls.some((r) => /sync$/.test(r)),
  uploadCalls.join(" | "),
);
await page.waitForTimeout(400);
await page.screenshot({ path: `${OUT}/dark-9-list.png` });

// --------------------------------------------------------------- LIGHT MODE
// #844's class of bug: a panel that only works in one theme. Same four kinds,
// re-shot on the paper-white page.
await page.evaluate(() => {
  const raw = window.localStorage.getItem("qagent.appearance");
  const parsed = raw ? JSON.parse(raw) : { state: {}, version: 0 };
  parsed.state = { ...parsed.state, mode: "light" };
  window.localStorage.setItem("qagent.appearance", JSON.stringify(parsed));
});
await page.reload({ waitUntil: "domcontentloaded" });
// A reload boots anonymous (the token is memory-only), so sign in again and
// walk back client-side.
await page.waitForTimeout(1200);
if (new URL(page.url()).pathname.endsWith("/login")) {
  await page.locator('input[type="email"]').fill(USER.email);
  await page.locator('input[type="password"]').first().fill("probe-password-123");
  await page.locator('button[type="submit"]').click();
  await page.waitForURL((u) => !u.pathname.endsWith("/login"), { timeout: 20000 });
}
await page.getByRole("button", { name: /^all projects$/i }).first().click();
await page.waitForTimeout(900);
await page.getByText("demo", { exact: true }).last().click();
await page.waitForTimeout(800);
await page.getByRole("button", { name: /^business/i }).first().click();
await page.waitForTimeout(1200);
check(
  "light mode is actually applied",
  await page.evaluate(() =>
    document.documentElement.getAttribute("data-mode") === "light" ||
    document.body.getAttribute("data-mode") === "light"),
);
await page.locator('[data-testid="business-add-toggle"]').click();
await page.waitForSelector('[data-testid="business-add-form"]', { timeout: 5000 });
await form.locator('[data-testid="business-url-input"]').fill("https://wiki.example.com/eligibility");
await shot("light-1-url");
await pick("Source type", "GitHub Markdown");
await pick("GitHub connection", "GitHub via EmeHub");
await shot("light-2-github-hub");
await pick("Source type", "Azure DevOps wiki");
await pick("Azure DevOps connection (optional)", "Azure DevOps via EmeHub");
await shot("light-3-ado-hub");
await pick("Source type", "Uploaded document");
await shot("light-4-upload");
await page.screenshot({ path: `${OUT}/light-5-page.png` });

// The panel must be OPAQUE over the animated shell, in both themes.
check(
  "the form's panel surface is opaque (no backdrop-filter, opaque background)",
  await page.evaluate(() => {
    const el = document.querySelector('[data-testid="business-add-form"]').closest("section");
    const s = getComputedStyle(el);
    return (
      (s.backdropFilter === "none" || s.backdropFilter === "") &&
      !/rgba\([^)]+,\s*0?\.\d+\)/.test(s.backgroundColor)
    );
  }),
);

// The 401s are ours: `/auth/refresh` is deliberately mocked as "no session",
// which is what makes the login form the real entry point.
const noisy = consoleErrors.filter(
  (e) => !/favicon|WebSocket|Failed to fetch|ERR_|net::|401 \(Unauthorized\)/i.test(e),
);
check("no unexpected console errors", noisy.length === 0, noisy.slice(0, 3).join(" | "));

writeFileSync(`${OUT}/results.json`, JSON.stringify(results, null, 2));
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
await browser.close();
process.exit(failed.length ? 1 : 0);
