// Manual-login capture that survives Microsoft/Entra's anti-automation checks.
// Args: baseUrl, storageDest, sessionDest.
//
// Approach: launch a NORMAL Chrome/Edge via a plain command (with a remote
// debugging port) — NOT via Playwright's launcher — so the page sees a real,
// non-automated browser and federated sign-in (Microsoft) works without looping.
// Playwright only ATTACHES read-only over CDP to snapshot cookies+localStorage
// (storageState) and sessionStorage (where MSAL/SPA tokens live). We finish when
// the operator closes the browser.
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const authState = require('./session_auth_state.cjs');
const [, , baseUrl, storageDest, sessionDest] = process.argv;

process.on('unhandledRejection', (e) => console.error('capture unhandledRejection:', e && (e.message || e)));
process.on('uncaughtException', (e) => console.error('capture uncaughtException:', e && (e.message || e)));

const profileDir = path.join(path.dirname(storageDest), 'browser-profile');
const PORT = 9222 + Math.floor(Math.random() * 400);

function findBrowser() {
  const c = [
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
    (process.env.LOCALAPPDATA || '') + '/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  ];
  for (const p of c) { try { if (p && fs.existsSync(p)) return p; } catch {} }
  return null;
}

async function waitForCDP(port, timeoutMs) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    try { const r = await fetch(`http://127.0.0.1:${port}/json/version`); if (r.ok) return true; } catch {}
    await new Promise((r) => setTimeout(r, 300));
  }
  return false;
}

(async () => {
  const exe = findBrowser();
  if (!exe) { console.error('capture_auth: no Chrome/Edge found on this machine'); process.exit(1); }
  fs.mkdirSync(profileDir, { recursive: true });

  // Fresh profiles block third-party cookies by default, which breaks the
  // MSAL/Entra federation redirects (app <-> ciamlogin.com <-> login.microsoftonline.com)
  // and makes the Microsoft sign-in loop. Pre-seed the profile to allow cookies.
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

  // Launch a real, non-automated browser. No Playwright launch flags => no
  // automation fingerprint => Microsoft login behaves normally.
  const child = spawn(exe, [
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profileDir}`,
    '--no-first-run', '--no-default-browser-check', '--new-window',
    baseUrl,
  ], { detached: false, stdio: 'ignore' });
  console.error('capture launched real browser:', exe, 'port', PORT);

  if (!(await waitForCDP(PORT, 20000))) {
    console.error('capture_auth: CDP endpoint never came up on port', PORT);
    try { child.kill(); } catch {}
    process.exit(1);
  }

  const browser = await chromium.connectOverCDP(`http://127.0.0.1:${PORT}`);
  const context = browser.contexts()[0];

  // IMPORTANT: never call context.storageState() here. For origins whose tab has
  // navigated away, Playwright reads their localStorage by OPENING A TEMP TAB to
  // that origin — every snapshot — which the operator sees as a tab rapidly
  // flashing open/closed during the federated (Microsoft) login. Instead we
  // compose the state ourselves: cookies via context.cookies() (no page churn),
  // and local/sessionStorage read only from tabs that are already open, merged
  // across snapshots so nothing is lost when the flow navigates between origins.
  const localByOrigin = {};   // origin -> { key: value }
  const sessionByOrigin = {}; // origin -> { key: value }

  async function snapshot() {
    if (!context) return;
    for (const p of context.pages()) {
      try {
        const dump = await p.evaluate(() => {
          const read = (s) => {
            const out = {};
            for (let i = 0; i < s.length; i++) { const k = s.key(i); out[k] = s.getItem(k); }
            return out;
          };
          return { origin: location.origin, local: read(localStorage), session: read(sessionStorage) };
        });
        if (!dump.origin || !dump.origin.startsWith('http')) continue;
        if (Object.keys(dump.local).length) localByOrigin[dump.origin] = dump.local;
        // Record faithfully, INCLUDING the transition to empty. The old guard only
        // assigned when non-empty, so once MSAL cleared its handshake keys the stale
        // mid-redirect map was retained forever and later replayed (#618).
        sessionByOrigin[dump.origin] = dump.session;
      } catch {}
    }
    try {
      const cookies = await context.cookies();
      const origins = Object.entries(localByOrigin).map(([origin, kv]) => ({
        origin,
        localStorage: Object.entries(kv).map(([name, value]) => ({ name, value })),
      }));
      fs.writeFileSync(storageDest, JSON.stringify({ cookies, origins }, null, 2));
    } catch {}
    try {
      // A handshake-only map is not a session; persisting it is what made the
      // authoring browser land unauthenticated (#618).
      const [clean, dropped] = authState.sanitize(sessionByOrigin);
      if (dropped.length) console.error('capture: ignoring mid-login sessionStorage for', dropped.join(','));
      fs.writeFileSync(sessionDest, JSON.stringify(clean, null, 2));
    } catch {}
  }

  const timer = setInterval(() => { snapshot().catch(() => {}); }, 1500);

  // Finish when the operator closes the browser (child exits) or CDP drops.
  await new Promise((resolve) => {
    child.on('exit', resolve);
    browser.on('disconnected', resolve);
  });

  clearInterval(timer);
  await snapshot().catch(() => {});
  // Say plainly whether a login was actually captured. Saving whatever happened to
  // be there let the operator believe a half-finished login had been stored (#618).
  if (authState.anyAuthenticated(sessionByOrigin)) {
    console.error('capture: captured an authenticated session');
  } else {
    console.error('capture: NO authenticated session captured — the login did not complete before the browser closed. Sign in fully, wait for the app to load, then close the window.');
  }
  try { await browser.close(); } catch {}
  try { child.kill(); } catch {}
  process.exit(0);
})().catch((e) => {
  console.error('capture_auth fatal:', e && (e.stack || e.message || e));
  process.exit(1);
});
