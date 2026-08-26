// Long-lived, pre-authenticated automation Chrome for live spec-authoring (#400).
// Args: baseUrl, port, profileDir, [sessionStoragePath], [storageStatePath].
//
// Launches a real Chrome/Edge (NOT via Playwright's launcher, so no automation
// fingerprint) on a FIXED --remote-debugging-port using a DEDICATED, persistent
// --user-data-dir. A dedicated non-default profile is deliberate: it lets
// browser-harness attach over CDP (BU_CDP_URL=http://127.0.0.1:<port>) without
// the Chrome "Allow remote debugging" popup / default-profile lockdown (see
// browser_harness/daemon.py:128-131,148).
//
// AUTH COMES FROM THE CAPTURED SESSION, NOT FROM THE PROFILE (#638). The profile
// is reused (its IdP cookies help a silent re-auth), but it must never be the
// only source: it is mutable state that a FAILED authoring run poisons. Measured
// on a real box — a run that landed on the login page left MSAL's cache cleared,
// so every later run inherited a profile with `msal.version` and nothing else and
// went straight back to the login page. `storageState.json` is the captured
// truth, and it is the same material the spec-run path injects successfully
// through `playwright.config.ts`.
//
// So, when Playwright is resolvable (agent side — the API container ships no
// Playwright), we attach over CDP BEFORE the app is ever loaded and:
//
//   * add the captured cookies to the context, and
//   * register an init script that restores the captured localStorage +
//     sessionStorage for the matching origin, writing ONLY keys that are absent
//     so a rotated live token is never clobbered.
//
// Both stores matter because MSAL's `cacheLocation` decides which one holds the
// tokens: sessionStorage for some apps, localStorage for others (and neither is
// persisted-then-trusted here). The Playwright connection is kept alive for the
// whole session so the init-script registration persists for the tabs
// browser-harness opens.
//
// Unlike capture_auth.cjs (a short snapshot loop) this stays ALIVE for the whole
// authoring session and only tears Chrome down when the parent closes our stdin
// (cross-platform cleanup), on SIGTERM/SIGINT, or when Chrome exits on its own.
//
// Vendored copy of api/app/services/pw_scripts/authoring_browser.cjs — keep in
// sync (the agent runs this locally; the server runs the api copy in Docker).
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const authState = require('./session_auth_state.cjs');
const [, , baseUrl, portArg, profileDir, sessionStoragePath, storageStatePath] = process.argv;
const PORT = parseInt(portArg, 10);

process.on('unhandledRejection', (e) => console.error('authoring_browser unhandledRejection:', e && (e.message || e)));
process.on('uncaughtException', (e) => console.error('authoring_browser uncaughtException:', e && (e.message || e)));

function findBrowser() {
  // Explicit override wins (the Docker image sets QAGENT_CHROME_BIN=/usr/bin/chromium).
  const c = [
    process.env.QAGENT_CHROME_BIN,
    // Linux / container (Debian chromium package + common Chrome paths).
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
    '/usr/bin/google-chrome',
    '/usr/bin/google-chrome-stable',
    // Windows host (native dev).
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
    (process.env.LOCALAPPDATA || '') + '/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  ];
  for (const p of c) { try { if (p && fs.existsSync(p)) return p; } catch {} }
  return null;
}

// Headless container flags: a Linux host with no X display can't run headed
// Chrome, and Chrome-as-root in a container needs --no-sandbox; the small
// default /dev/shm makes --disable-dev-shm-usage necessary. On a real desktop
// (Windows, or Linux with DISPLAY) we launch headed so the operator can watch
// and MSAL/federated auth behaves like a normal browser.
function containerFlags() {
  const headless = process.platform !== 'win32' && !process.env.DISPLAY;
  return headless
    ? ['--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
    : [];
}

async function waitForCDP(port, timeoutMs) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    try { const r = await fetch(`http://127.0.0.1:${port}/json/version`); if (r.ok) return true; } catch {}
    await new Promise((r) => setTimeout(r, 300));
  }
  return false;
}

// Load the saved sessionStorage map ({origin: {k:v}}), or null if unusable.
function loadSessionStorage() {
  if (!sessionStoragePath) return null;
  // Guard, belt-and-braces with the capture-side fix: an older or interrupted
  // snapshot may still hold MSAL mid-redirect state. Replaying it makes MSAL think
  // an interaction is already running, so it restarts the redirect or errors — while
  // doing NOTHING would have let the profile's IdP cookies re-authenticate silently
  // (#618). Non-MSAL state is never discarded: only 'handshake-only' is dropped.
  try {
    const raw = JSON.parse(fs.readFileSync(sessionStoragePath, 'utf-8'));
    if (!raw || typeof raw !== 'object') return null;
    const [clean, dropped] = authState.sanitize(raw);
    if (dropped.length) {
      console.error('authoring_browser: refusing to replay mid-login sessionStorage for',
        dropped.join(','), '- letting the profile cookies re-authenticate instead');
    }
    return Object.keys(clean).length ? clean : null;
  } catch { return null; }
}

// Load the captured `storageState.json` (Playwright's own format: `{cookies,
// origins:[{origin, localStorage:[{name,value}]}]}`), reduced to the cookie list
// plus a `{origin: {k: v}}` localStorage map. Null when unreadable/absent (#638).
function loadStorageState() {
  if (!storageStatePath) return null;
  try {
    return authState.storageStateToMaps(JSON.parse(fs.readFileSync(storageStatePath, 'utf-8')));
  } catch { return null; }
}

// Require Playwright if available (agent side); null in the API container.
function tryPlaywright() {
  try { return require('playwright').chromium; } catch { return null; }
}

// Restore the captured session and navigate the VISIBLE tab to baseUrl WITH the
// auth in place, so MSAL/SPA apps load authenticated. Critical ordering: cookies
// and the init script must land BEFORE the first navigation to the app (that's
// why Chrome is launched at about:blank, not baseUrl). Returns the connected
// Playwright browser (kept alive so the init-script registration + tab survive).
async function armAuthAndNavigate(chromium, port, sessionByOrigin, state) {
  const browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`);
  const ctx = browser.contexts()[0];
  if (!ctx) return browser;

  // Captured cookies first — the profile may hold older ones, and addCookies
  // overwrites by (name, domain, path).
  if (state && state.cookies.length) {
    try {
      await ctx.addCookies(state.cookies);
      console.error('authoring_browser: restored', state.cookies.length, 'captured cookies');
    } catch (e) {
      console.error('authoring_browser: addCookies failed:', e && e.message);
    }
  }

  const localByOrigin = (state && state.localByOrigin) || {};
  await ctx.addInitScript((data) => {
    // Only write keys that are ABSENT: this script runs on every navigation, and
    // clobbering a token MSAL has since rotated would log the session back out
    // (#638). Restoring what is missing is what makes the first load
    // authenticated; after that the app owns its own cache.
    const restore = (store, map) => {
      if (!map) return;
      for (const k of Object.keys(map)) {
        try { if (store.getItem(k) === null) store.setItem(k, map[k]); } catch (e) {}
      }
    };
    try { restore(window.localStorage, data && data.local && data.local[location.origin]); } catch (e) {}
    try { restore(window.sessionStorage, data && data.session && data.session[location.origin]); } catch (e) {}
  }, { local: localByOrigin, session: sessionByOrigin || {} });

  // A page we OWN — never one already showing something else (#739). This used to be
  // `ctx.pages()[0]`, and this profile is the same one the manual-login capture opens
  // for the operator: a tab they left in it is restored on launch, so the very first
  // thing authoring did was navigate the operator's tab away to the app's base URL.
  // Chrome was launched at about:blank precisely so there is a blank page to claim.
  const blank = ctx.pages().find((p) => {
    const u = p.url();
    return !u || u === 'about:blank' || u === 'chrome://newtab/';
  });
  const page = blank || (await ctx.newPage());
  try {
    await page.goto(baseUrl, { waitUntil: 'domcontentloaded', timeout: 45000 });
    await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
  } catch (e) {
    console.error('authoring_browser: navigate after replay failed:', e && e.message);
  }
  const restored = [
    Object.keys(localByOrigin).length ? 'localStorage' : '',
    Object.keys(sessionByOrigin || {}).length ? 'sessionStorage' : '',
  ].filter(Boolean).join(' + ') || 'cookies only';
  console.error('authoring_browser: session restore armed (' + restored + ')');
  return browser;
}

(async () => {
  if (!PORT || Number.isNaN(PORT)) { console.error('authoring_browser: invalid port', portArg); process.exit(1); }
  const exe = findBrowser();
  if (!exe) { console.error('authoring_browser: no Chrome/Edge found on this machine'); process.exit(1); }
  fs.mkdirSync(profileDir, { recursive: true });

  // Same third-party-cookie seed as capture: fresh profiles break the MSAL/Entra
  // federation redirects. No-op when the profile was already seeded by capture.
  try {
    const defDir = path.join(profileDir, 'Default');
    fs.mkdirSync(defDir, { recursive: true });
    const prefsPath = path.join(defDir, 'Preferences');
    if (!fs.existsSync(prefsPath)) {
      fs.writeFileSync(prefsPath, JSON.stringify({
        profile: { cookie_controls_mode: 0, block_third_party_cookies: false,
          default_content_setting_values: { cookies: 1 } },
      }));
    }
  } catch (e) { console.error('pref seed failed:', e && e.message); }

  // If we have ANY captured auth to restore (agent side: Playwright resolvable
  // plus a storageState and/or a replayable sessionStorage map), launch to
  // about:blank and let Playwright navigate AFTER arming the restore — so the app
  // never loads before the session is in place. Otherwise launch straight to
  // baseUrl (profile-only / API container).
  //
  // Gating on the sessionStorage map ALONE was the #638 regression: for an app
  // whose MSAL cache lives in localStorage, the sanitizer legitimately empties
  // that map, `chromium` fell to null, and the launcher quietly skipped the whole
  // restore — injecting nothing and leaving auth to a profile a previous failed
  // run had already emptied.
  const byOrigin = loadSessionStorage();
  const state = loadStorageState();
  const chromium = byOrigin || state ? tryPlaywright() : null;
  const launchUrl = chromium ? 'about:blank' : baseUrl;
  if ((byOrigin || state) && !chromium) {
    console.error('authoring_browser: captured session present but Playwright is not resolvable — launching profile-only');
  }

  const child = spawn(exe, [
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profileDir}`,
    '--no-first-run', '--no-default-browser-check', '--new-window',
    ...containerFlags(),
    launchUrl,
  ], { detached: false, stdio: 'ignore', windowsHide: true });
  console.error('authoring_browser launched:', exe, 'port', PORT, 'replay:', Boolean(chromium));

  if (!(await waitForCDP(PORT, 20000))) {
    console.error('authoring_browser: CDP endpoint never came up on port', PORT);
    try { child.kill(); } catch {}
    process.exit(1);
  }

  // Arm sessionStorage replay + navigate the visible tab authenticated, BEFORE
  // signalling readiness so browser-harness attaches to a logged-in tab.
  let pw = null;
  if (chromium) {
    try { pw = await armAuthAndNavigate(chromium, PORT, byOrigin, state); }
    catch (e) { console.error('authoring_browser: session restore failed:', e && e.message); }
  }

  // Signal readiness on stdout so the parent proceeds. The daemon resolves
  // BU_CDP_URL to the WS.
  console.log(`AUTHORING_BROWSER_READY ${PORT}`);

  let shuttingDown = false;
  const shutdown = (code) => {
    if (shuttingDown) return;
    shuttingDown = true;
    try { if (pw) pw.close(); } catch {}
    try { child.kill(); } catch {}
    process.exit(code || 0);
  };

  // Cleanup triggers: parent closes our stdin (works cross-platform, incl.
  // Windows where a terminate() won't run signal handlers), OS signals, or
  // Chrome exiting on its own.
  child.on('exit', () => shutdown(0));
  process.stdin.on('end', () => shutdown(0));
  process.stdin.on('close', () => shutdown(0));
  process.on('SIGTERM', () => shutdown(0));
  process.on('SIGINT', () => shutdown(0));
  process.stdin.resume();
})().catch((e) => {
  console.error('authoring_browser fatal:', e && (e.stack || e.message || e));
  process.exit(1);
});
