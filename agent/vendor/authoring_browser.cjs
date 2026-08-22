// Long-lived, pre-authenticated automation Chrome for live spec-authoring (#400).
// Args: baseUrl, port, profileDir, [sessionStoragePath].
//
// Launches a real Chrome/Edge (NOT via Playwright's launcher, so no automation
// fingerprint) on a FIXED --remote-debugging-port using a DEDICATED, persistent
// --user-data-dir. A dedicated non-default profile is deliberate: it lets
// browser-harness attach over CDP (BU_CDP_URL=http://127.0.0.1:<port>) without
// the Chrome "Allow remote debugging" popup / default-profile lockdown (see
// browser_harness/daemon.py:128-131,148). Auth is inherited from the persistent
// profile — reuse the capture `browser-profile` dir (already logged in via the
// manual-login capture flow), so cookies + localStorage are present.
//
// sessionStorage (where MSAL/SPA auth tokens live) is NEVER persisted to a Chrome
// profile on disk, so a profile-only relaunch lands unauthenticated for such apps.
// When a `sessionStoragePath` is given AND Playwright is resolvable (agent side —
// the API container ships no Playwright and passes no path), we attach over CDP
// and register an init script that replays the saved sessionStorage for the
// matching origin before app code runs — the same trick the run/explore paths use.
// The Playwright connection is kept alive for the whole session so the init-script
// registration persists for the tabs browser-harness opens.
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
const [, , baseUrl, portArg, profileDir, sessionStoragePath] = process.argv;
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

// Require Playwright if available (agent side); null in the API container.
function tryPlaywright() {
  try { return require('playwright').chromium; } catch { return null; }
}

// Arm sessionStorage replay and navigate the VISIBLE tab to baseUrl WITH the
// token restored, so MSAL/SPA apps load authenticated. Critical ordering: the
// init script must be registered BEFORE the first navigation to the app (that's
// why Chrome is launched at about:blank, not baseUrl). Returns the connected
// Playwright browser (kept alive so the init-script registration + tab survive).
async function armAuthAndNavigate(chromium, port, byOrigin) {
  const browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`);
  const ctx = browser.contexts()[0];
  if (!ctx) return browser;
  await ctx.addInitScript((data) => {
    try {
      const o = data && data[location.origin];
      if (o) for (const k of Object.keys(o)) window.sessionStorage.setItem(k, o[k]);
    } catch (e) {}
  }, byOrigin);
  const page = ctx.pages()[0] || (await ctx.newPage());
  try {
    await page.goto(baseUrl, { waitUntil: 'domcontentloaded', timeout: 45000 });
    await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
  } catch (e) {
    console.error('authoring_browser: navigate after replay failed:', e && e.message);
  }
  console.error('authoring_browser: sessionStorage replay armed for', Object.keys(byOrigin).join(','));
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

  // If we can replay sessionStorage (agent side: Playwright resolvable + a saved
  // map), launch to about:blank and let Playwright navigate AFTER arming the init
  // script — so the app never loads before the token is restored. Otherwise launch
  // straight to baseUrl (profile-only / API container).
  const byOrigin = loadSessionStorage();
  const chromium = byOrigin ? tryPlaywright() : null;
  const launchUrl = chromium ? 'about:blank' : baseUrl;

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
    try { pw = await armAuthAndNavigate(chromium, PORT, byOrigin); }
    catch (e) { console.error('authoring_browser: replay failed:', e && e.message); }
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
