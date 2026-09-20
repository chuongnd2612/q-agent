// Long-lived Playwright driver for the DOM Exploration Agent (ADR 0010 §1).
//
// Unlike the rest of the app — which drives Playwright as one-shot
// `npx playwright test` subprocesses — the exploration loop is interactive: the
// Python agent must OBSERVE page state after each action to DECIDE the next one.
// So this process launches ONE chromium browser + page and keeps it open for the
// whole session, reading newline-delimited JSON commands on stdin and writing
// newline-delimited JSON responses on stdout.
//
// Args: baseURL, [storageState], [sessionState]  (storageState = absolute path
// to a saved Playwright session for auth reuse, per ADR 0002; sessionState =
// absolute path to the sibling sessionStorage.json snapshot, replayed for auth
// reuse on MSAL/SPA apps whose token lives in sessionStorage — which Playwright's
// storageState cannot persist).
//
// Protocol (one JSON object per line, each way):
//   {"cmd":"observe"}
//     -> {"ok":true,"url":..,"path":..,"a11y":<ariaSnapshot() role+name tree>,
//         "elements":[<distilled interactive DOM>]}
//   {"cmd":"act","action":"goto|click|fill|expectVisible","args":{...}}
//     -> {"ok":bool,"error":str|null,"changed":bool}
//   {"cmd":"close"}  -> process exits.
//
// Every command is guarded so a bad action returns {ok:false,error} and never
// crashes the process (the session must survive a mis-step, mirroring the
// self-heal loop's tolerance for stale actions).
const { chromium } = require('playwright');
const readline = require('readline');
const fs = require('fs');

const [, , baseURL, storageState, sessionState] = process.argv;

process.on('unhandledRejection', (e) => console.error('explore unhandledRejection:', e && (e.message || e)));
process.on('uncaughtException', (e) => console.error('explore uncaughtException:', e && (e.message || e)));

// Same selector set + shape as the self-heal distilled DOM (playwright_runner
// `_fixtures_ts`), so observations ground on identical real identifiers.
const DISTILL = () => {
  const SEL = 'a,button,input,select,textarea,[role],[data-testid],[data-test],[id]';
  return Array.from(document.querySelectorAll(SEL)).slice(0, 400).map((node) => {
    const el = node;
    const text = (el.innerText || '').trim().slice(0, 80);
    return {
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || undefined,
      testId: el.getAttribute('data-testid') || el.getAttribute('data-test') || undefined,
      id: el.id || undefined,
      name: el.getAttribute('name') || undefined,
      text: text || undefined,
      placeholder: el.getAttribute('placeholder') || undefined,
      type: el.getAttribute('type') || undefined,
    };
  });
};

function send(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

let browser = null;
let page = null;

/**
 * Resolve a Playwright locator from the action args. Prefers stable strategies:
 * data-testid -> role -> raw selector (CSS/testid). Returns null when the args
 * carry neither a role nor a selector.
 */
function locatorFor(args) {
  if (!args) return null;
  if (args.testId) return page.getByTestId(args.testId);
  if (args.role) return page.getByRole(args.role, args.name != null ? { name: args.name } : undefined);
  if (args.selector) return page.locator(args.selector);
  return null;
}

/** A cheap page signature (url + distilled interactive DOM) used to detect change. */
async function signature() {
  try {
    const elements = await page.evaluate(DISTILL);
    return page.url() + '::' + JSON.stringify(elements);
  } catch {
    return page.url() + '::';
  }
}

async function doAct(action, args) {
  args = args || {};
  const before = await signature();
  switch (action) {
    case 'goto': {
      await page.goto(args.url, { waitUntil: 'domcontentloaded' });
      break;
    }
    case 'click': {
      const loc = locatorFor(args);
      if (!loc) return { ok: false, error: 'click needs role+name or selector', changed: false };
      await loc.first().click();
      break;
    }
    case 'fill': {
      const loc = locatorFor(args);
      if (!loc) return { ok: false, error: 'fill needs role+name or selector', changed: false };
      await loc.first().fill(args.value != null ? String(args.value) : '');
      break;
    }
    case 'expectVisible': {
      // Probe only — report visibility, never throw on absence.
      const loc = locatorFor(args);
      if (!loc) return { ok: false, error: 'expectVisible needs role+name or selector', changed: false };
      let visible = false;
      try { visible = await loc.first().isVisible(); } catch { visible = false; }
      return { ok: visible, error: visible ? null : 'not visible', changed: false };
    }
    default:
      return { ok: false, error: `unknown action: ${action}`, changed: false };
  }
  const after = await signature();
  return { ok: true, error: null, changed: after !== before };
}

async function doObserve() {
  // Playwright removed page.accessibility; the supported role+name tree is
  // locator.ariaSnapshot() (a compact YAML string, ideal for the model and
  // maps directly to getByRole). Best-effort — never fail observe on it.
  let a11y = '';
  try { a11y = await page.locator('body').ariaSnapshot(); } catch { a11y = ''; }
  const elements = await page.evaluate(DISTILL);
  const url = page.url();
  let path = '';
  try { path = new URL(url).pathname; } catch { path = ''; }
  return { ok: true, url, path, a11y, elements };
}

async function handle(line) {
  let msg;
  try {
    msg = JSON.parse(line);
  } catch (e) {
    send({ ok: false, error: 'invalid JSON command' });
    return;
  }
  try {
    if (msg.cmd === 'observe') {
      send(await doObserve());
    } else if (msg.cmd === 'act') {
      send(await doAct(msg.action, msg.args));
    } else if (msg.cmd === 'close') {
      try { await browser.close(); } catch {}
      process.exit(0);
    } else {
      send({ ok: false, error: `unknown cmd: ${msg.cmd}` });
    }
  } catch (e) {
    send({ ok: false, error: (e && (e.message || String(e))) || 'command failed' });
  }
}

(async () => {
  if (!baseURL) { console.error('explore_session: missing baseURL arg'); process.exit(1); }
  // Headed when QAGENT_EXPLORE_HEADED=1 (the Local Agent sets it so the user can
  // watch, and a headed browser trips WAF/bot-protection far less than headless);
  // headless otherwise (e.g. the server, which has no display).
  // `playwright` is installed in the API image for its `cli` subcommand (#875),
  // but the image deliberately ships Debian's chromium (QAGENT_CHROME_BIN) rather
  // than Playwright's ~150MB bundled browsers — `playwright install` is never run.
  // Without an explicit executablePath, launch() looks for the bundle and dies
  // with "Executable doesn't exist at .../ms-playwright/chromium_headless_shell-*"
  // (#885), so server-side exploration could never start a browser. Fall back to
  // the bundle only when QAGENT_CHROME_BIN is unset (e.g. the Local Agent, which
  // does install browsers).
  const chromeBin = process.env.QAGENT_CHROME_BIN;
  browser = await chromium.launch({
    headless: process.env.QAGENT_EXPLORE_HEADED !== '1',
    ...(chromeBin ? { executablePath: chromeBin } : {}),
  });
  const contextOpts = { baseURL };
  if (storageState) contextOpts.storageState = storageState;
  const context = await browser.newContext(contextOpts);
  // Replay the captured sessionStorage (MSAL/SPA auth tokens) for the matching
  // origin BEFORE any app code runs — mirrors playwright_runner._fixtures_ts.
  // storageState persists cookies + localStorage but NOT sessionStorage, so
  // without this an MSAL/SPA app boots unauthenticated and bounces to login even
  // with a valid saved session. The snapshot is {origin: {key: value}}.
  if (sessionState) {
    let sessions = {};
    try { sessions = JSON.parse(fs.readFileSync(sessionState, 'utf-8')); } catch {}
    await context.addInitScript((byOrigin) => {
      try {
        const entries = byOrigin[location.origin];
        if (entries) for (const k in entries) window.sessionStorage.setItem(k, entries[k]);
      } catch {}
    }, sessions);
  }
  page = await context.newPage();

  // Land on the app before the first observe so step 1 sees the real page,
  // not about:blank. Best-effort — the model can still goto elsewhere.
  if (baseURL) {
    try { await page.goto(baseURL, { waitUntil: 'domcontentloaded' }); } catch {}
  }

  // Serialize commands: one line in -> one response out, in order.
  const rl = readline.createInterface({ input: process.stdin });
  let chain = Promise.resolve();
  rl.on('line', (line) => {
    if (!line.trim()) return;
    chain = chain.then(() => handle(line));
  });
  rl.on('close', async () => {
    try { await browser.close(); } catch {}
    process.exit(0);
  });
  // Signal readiness so the Python side knows the browser+page are up.
  send({ ok: true, ready: true });
})().catch((e) => {
  console.error('explore_session fatal:', e && (e.stack || e.message || e));
  process.exit(1);
});
