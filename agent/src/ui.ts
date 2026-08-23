/**
 * Local web UI for the agent, served on 127.0.0.1 (opened in the browser by the
 * `ui` command, or shown in the Electron desktop window). Pairs by entering a
 * server URL + 6-digit code, then shows the live agent status + log.
 *
 * The page implements the client's "Local Agent" design (dark, glass,
 * purple→indigo, custom frameless titlebar, segmented 6-digit code, compact
 * connected view). No framework / no extra deps — Node's built-in http plus the
 * in-process event bus (bus.ts).
 */
import { spawn } from "node:child_process";
import * as http from "node:http";
import * as os from "node:os";
import * as path from "node:path";
import { disconnectDevice, redeemDevice } from "./api";
import { bus, emit, recentEvents } from "./bus";
import { AgentConfig, clearConfig, defaultServerUrl, loadConfig, saveConfig } from "./config";
import { agentNodeModules } from "./paths";
import { killActiveChild, runAgentLoop } from "./runner";

let signal = { aborted: false };
let running = false;

/** Playwright's version, for the environment chips (best-effort). */
function playwrightVersion(): string {
  try {
    return (require(path.join(agentNodeModules(), "playwright", "package.json")) as { version: string }).version;
  } catch {
    return "";
  }
}

/** This agent build's version (from its own package.json), shown bottom-right. */
function appVersion(): string {
  try {
    return (require(path.join(__dirname, "..", "..", "package.json")) as { version: string }).version || "";
  } catch {
    return "";
  }
}

/** Start the claim→run loop for `cfg` (no-op if already running). */
function startLoop(cfg: AgentConfig): void {
  if (running) return;
  running = true;
  signal = { aborted: false };
  emit("agent-status", { running: true, deviceId: cfg.deviceId, deviceName: cfg.deviceName, serverUrl: cfg.serverUrl });
  runAgentLoop(cfg, signal)
    .catch((err) => emit("error", { message: (err as Error).message || String(err) }))
    .finally(() => {
      running = false;
      emit("agent-status", { running: false });
    });
}

function stopLoop(): void {
  signal.aborted = true;
  killActiveChild();
  running = false;
}

function state() {
  const cfg = loadConfig();
  return {
    paired: Boolean(cfg),
    running,
    deviceId: cfg?.deviceId ?? null,
    deviceName: cfg?.deviceName ?? os.hostname(),
    // Pre-fill the server from the paired config, else the value baked into this
    // build — so a downloaded desktop app needs only the 6-digit code.
    serverUrl: cfg?.serverUrl ?? defaultServerUrl(),
    machine: os.hostname(),
    nodeVersion: process.versions.node,
    playwrightVersion: playwrightVersion(),
    appVersion: appVersion(),
    events: recentEvents(),
  };
}

function readBody(req: http.IncomingMessage): Promise<string> {
  return new Promise((resolve) => {
    let data = "";
    req.on("data", (c) => (data += c));
    req.on("end", () => resolve(data));
  });
}

function sendJson(res: http.ServerResponse, code: number, body: unknown): void {
  const s = JSON.stringify(body);
  res.writeHead(code, { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(s) });
  res.end(s);
}

function openBrowser(url: string): void {
  try {
    // windowsHide (#421): `cmd /c start` is a console app, so without the flag a
    // GUI/Electron agent (no console of its own) pops a visible console window
    // for the split second it takes to hand the URL to the shell.
    const opts = { detached: true, stdio: "ignore" as const, windowsHide: true };
    if (process.platform === "win32") spawn("cmd", ["/c", "start", "", url], opts).unref();
    else if (process.platform === "darwin") spawn("open", [url], opts).unref();
    else spawn("xdg-open", [url], opts).unref();
  } catch {
    // Non-fatal: the URL is printed to the console as a fallback.
  }
}

async function handle(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
  const url = req.url || "/";

  if (req.method === "GET" && (url === "/" || url.startsWith("/?"))) {
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    res.end(PAGE);
    return;
  }

  if (req.method === "GET" && url === "/api/state") {
    sendJson(res, 200, state());
    return;
  }

  if (req.method === "POST" && url === "/api/pair") {
    try {
      const { code, server } = JSON.parse((await readBody(req)) || "{}");
      const serverUrl = String(server || "").replace(/\/+$/, "");
      if (!code || !serverUrl) return sendJson(res, 400, { error: "Pair code and server URL are required." });
      const name = os.hostname();
      const { deviceToken, deviceId } = await redeemDevice(serverUrl, String(code).trim(), name);
      const cfg: AgentConfig = { serverUrl, deviceToken, deviceId, deviceName: name };
      saveConfig(cfg);
      emit("log", { message: `paired as device #${deviceId} (${name})` });
      startLoop(cfg);
      sendJson(res, 200, { ok: true, deviceId });
    } catch (err) {
      sendJson(res, 400, { error: (err as Error).message || "Pairing failed" });
    }
    return;
  }

  if (req.method === "POST" && url === "/api/disconnect") {
    // Tell the server first (token still valid) so the device drops off the
    // owner's SPA list immediately, then wipe the local token + stop the loop.
    const cfg = loadConfig();
    if (cfg) {
      await disconnectDevice(cfg).catch((err) =>
        emit("log", { message: `server disconnect failed: ${(err as Error).message}` })
      );
    }
    stopLoop();
    clearConfig();
    emit("agent-status", { running: false });
    sendJson(res, 200, { ok: true });
    return;
  }

  if (req.method === "GET" && url === "/api/events") {
    res.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
    });
    res.write(": connected\n\n");
    for (const ev of recentEvents()) res.write(`data: ${JSON.stringify(ev)}\n\n`);
    const onEvent = (ev: unknown) => res.write(`data: ${JSON.stringify(ev)}\n\n`);
    bus.on("event", onEvent);
    const ka = setInterval(() => res.write(": keep-alive\n\n"), 25_000);
    req.on("close", () => {
      clearInterval(ka);
      bus.off("event", onEvent);
    });
    return;
  }

  res.writeHead(404, { "Content-Type": "text/plain" });
  res.end("Not found");
}

/** Start the UI server, begin the loop if already paired, and open the browser.
 * `onListening` receives the actual URL (the port may bump if 7420 is taken) —
 * the Electron shell uses it to load the window. */
export function startUi(opts: { port?: number; open?: boolean; onListening?: (url: string) => void } = {}): void {
  let port = opts.port ?? 7420;
  const server = http.createServer((req, res) => {
    handle(req, res).catch(() => {
      if (!res.headersSent) res.writeHead(500);
      res.end();
    });
  });
  server.on("error", (err: NodeJS.ErrnoException) => {
    if (err.code === "EADDRINUSE" && port < 7440) {
      port += 1;
      server.listen(port, "127.0.0.1");
    } else {
      console.error("UI server error:", err.message);
    }
  });
  server.listen(port, "127.0.0.1", () => {
    const addr = `http://127.0.0.1:${port}`;
    console.log(`Local Agent UI → ${addr}`);
    const cfg = loadConfig();
    if (cfg) startLoop(cfg);
    opts.onListening?.(addr);
    if (opts.open !== false) openBrowser(addr);
  });
}

// ---------------------------------------------------------------- the page
// Client's "Local Agent" design, ported to vanilla HTML/CSS/JS and wired to the
// /api/* endpoints. The client script avoids template literals / ${} so this
// server-side template literal needs no escaping.
const PAGE = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Q-Agent · Local Agent</title>
<link rel="preconnect" href="https://api.fontshare.com" />
<link href="https://api.fontshare.com/v2/css?f[]=satoshi@400,500,600,700,900&display=swap" rel="stylesheet" />
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet" />
<style>
  *{box-sizing:border-box}
  html,body{margin:0;height:100%}
  body{background:#0a0a0f;color:#ECECF1;font-family:'Satoshi',system-ui,sans-serif;-webkit-font-smoothing:antialiased;overflow:hidden}
  ::selection{background:rgba(139,92,246,.4);color:#fff}
  input::placeholder{color:#5c5c6e}
  ::-webkit-scrollbar{width:9px}::-webkit-scrollbar-thumb{background:rgba(255,255,255,.1);border-radius:8px}
  @keyframes fadeUp{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
  @keyframes glowPulse{0%,100%{opacity:.5}50%{opacity:.85}}
  @keyframes spin{to{transform:rotate(360deg)}}
  @keyframes livePulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(.7)}}
  @keyframes logoHalo{0%,100%{opacity:.5;transform:scale(.92)}50%{opacity:1;transform:scale(1.12)}}
  @keyframes logIn{from{opacity:0;transform:translateX(-8px)}to{opacity:1;transform:none}}
  @keyframes barPulse{0%,100%{transform:scaleY(.35)}50%{transform:scaleY(1)}}
  @keyframes caret{0%,49%{opacity:1}50%,100%{opacity:0}}
  .ctl{width:44px;height:32px;display:flex;align-items:center;justify-content:center;background:none;border:none;cursor:pointer;color:#8b8b9e;border-radius:7px}
  .ctl:hover{background:rgba(255,255,255,.07);color:#ececf1}
  .ctl.close:hover{background:#e5484d;color:#fff}
  input:focus{outline:none}
  .cell{position:relative;display:flex;align-items:center;justify-content:center;height:54px;border-radius:12px;background:#16161f;font-family:'JetBrains Mono',monospace;font-size:24px;font-weight:600;color:#ECECF1;border:1.5px solid rgba(255,255,255,.09);transition:border-color .16s,box-shadow .16s}
</style>
</head>
<body>
<div style="position:fixed;inset:0;display:flex;flex-direction:column;background:#0a0a0f;overflow:hidden;border:1px solid rgba(255,255,255,.08)">
  <div style="position:absolute;top:-16%;left:-10%;width:520px;height:520px;border-radius:50%;background:radial-gradient(circle,rgba(139,92,246,.26),transparent 62%);filter:blur(30px);z-index:0;pointer-events:none;animation:glowPulse 9s ease-in-out infinite"></div>
  <div style="position:absolute;bottom:-22%;right:-12%;width:600px;height:600px;border-radius:50%;background:radial-gradient(circle,rgba(99,102,241,.22),transparent 62%);filter:blur(30px);z-index:0;pointer-events:none;animation:glowPulse 11s ease-in-out infinite 1s"></div>

  <!-- titlebar (frameless electron chrome; draggable) -->
  <div style="position:relative;z-index:5;flex-shrink:0;height:40px;display:flex;align-items:center;justify-content:space-between;padding:0 4px 0 13px;background:rgba(14,14,20,.72);backdrop-filter:blur(24px);border-bottom:1px solid rgba(255,255,255,.06);-webkit-app-region:drag">
    <div style="display:flex;align-items:center;gap:9px">
      <div style="width:18px;height:18px;border-radius:6px;background:linear-gradient(135deg,#8b5cf6,#6366f1);display:flex;align-items:center;justify-content:center;box-shadow:0 3px 9px -3px rgba(139,92,246,.8)"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 5.3L19 10l-5.1 1.7L12 17l-1.9-5.3L5 10l5.1-1.7z"/></svg></div>
      <span style="font-size:11.5px;font-weight:600;color:#a0a0b2">Q‑Agent · Local Agent</span>
    </div>
    <div id="winctl" style="display:none;align-items:center;-webkit-app-region:no-drag">
      <button class="ctl" onclick="qa.win('minimize')"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14"/></svg></button>
      <button class="ctl" onclick="qa.win('maximize')"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="4" width="16" height="16" rx="2"/></svg></button>
      <button class="ctl close" onclick="qa.win('close')"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
    </div>
  </div>

  <!-- version tag (bottom-right) + update banner (desktop only; shown on demand) -->
  <div id="versionTag" style="position:fixed;bottom:7px;right:11px;z-index:6;font-size:10px;font-weight:600;letter-spacing:.02em;color:#4e4e5e;font-family:'JetBrains Mono',ui-monospace,monospace;pointer-events:none">v&#8212;</div>
  <div id="updateBar" style="display:none;position:fixed;left:14px;right:14px;bottom:16px;z-index:60;align-items:center;gap:11px;padding:11px 13px;border-radius:13px;background:rgba(24,24,32,.97);backdrop-filter:blur(22px);border:1px solid rgba(139,92,246,.4);box-shadow:0 20px 55px -18px rgba(0,0,0,.95);animation:fadeUp .4s ease both">
    <span style="flex-shrink:0;width:30px;height:30px;border-radius:9px;background:rgba(139,92,246,.16);border:1px solid rgba(139,92,246,.32);display:flex;align-items:center;justify-content:center;color:#c4b5fd"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12M7 10l5 5 5-5M5 21h14"/></svg></span>
    <div style="flex:1;min-width:0">
      <div id="updTitle" style="font-size:12.5px;font-weight:700;color:#ECECF1;line-height:1.25">Update available</div>
      <div id="updSub" style="font-size:11px;color:#9494a6;line-height:1.3;margin-top:1px"></div>
    </div>
    <button id="updBtn" style="flex-shrink:0;height:32px;padding:0 14px;border:none;border-radius:9px;font-family:inherit;font-size:12.5px;font-weight:700;color:#fff;background:linear-gradient(135deg,#8b5cf6,#6366f1);cursor:pointer;box-shadow:0 8px 22px -8px rgba(139,92,246,.8)">Install</button>
  </div>

  <!-- body -->
  <div style="position:relative;z-index:2;flex:1;overflow-y:auto;padding:22px 22px 18px">
   <div style="max-width:420px;margin:0 auto">

    <!-- header -->
    <div style="display:flex;align-items:flex-start;gap:14px;margin-bottom:26px">
      <div style="position:relative;width:46px;height:46px;flex-shrink:0">
        <span style="position:absolute;inset:-6px;border-radius:16px;background:radial-gradient(circle,rgba(139,92,246,.5),transparent 70%);filter:blur(8px);animation:logoHalo 3s ease-in-out infinite"></span>
        <div style="position:relative;width:46px;height:46px;border-radius:15px;background:linear-gradient(135deg,#8b5cf6,#6366f1);display:flex;align-items:center;justify-content:center;box-shadow:0 10px 26px -8px rgba(139,92,246,.85)"><svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 5.3L19 10l-5.1 1.7L12 17l-1.9-5.3L5 10l5.1-1.7z"/></svg></div>
      </div>
      <div style="flex:1;min-width:0;padding-top:1px">
        <div style="font-size:20px;font-weight:900;letter-spacing:-.02em;line-height:1.1">Local Agent</div>
        <div style="font-size:12.5px;color:#8b8b9e;margin-top:4px;line-height:1.45">Runs Playwright on this machine — your session stays local.</div>
      </div>
      <div style="display:flex;align-items:center;gap:8px;flex-shrink:0">
        <div id="pill" style="display:flex;align-items:center;gap:7px;padding:6px 12px;border-radius:20px">
          <span id="pillDot" style="width:7px;height:7px;border-radius:50%"></span>
          <span id="pillLabel" style="font-size:11.5px;font-weight:700;white-space:nowrap"></span>
        </div>
        <button id="hdrDisconnect" title="Disconnect" style="display:none;align-items:center;justify-content:center;width:32px;height:32px;border-radius:10px;background:rgba(229,72,77,.12);border:1px solid rgba(229,72,77,.3);cursor:pointer;color:#ff8589"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M18.36 6.64A9 9 0 1 1 5.64 6.64M12 2v10"/></svg></button>
      </div>
    </div>

    <!-- CONNECT form -->
    <div id="formView" style="display:none">
      <div style="animation:fadeUp .5s ease both;border-radius:22px;background:rgba(20,20,28,.55);backdrop-filter:blur(28px);border:1px solid rgba(255,255,255,.08);padding:24px;box-shadow:0 24px 60px -22px rgba(0,0,0,.7)">
        <div style="display:flex;align-items:center;gap:9px;margin-bottom:6px">
          <span style="font-size:11px;font-weight:700;letter-spacing:.14em;color:#a78bfa">CONNECT</span>
          <span style="flex:1;height:1px;background:linear-gradient(90deg,rgba(167,139,250,.35),transparent)"></span>
        </div>
        <p style="margin:0 0 20px;font-size:12.5px;color:#9494a6;line-height:1.5">Generate a pairing code on the Q‑Agent app's <span style="color:#c3c3d0;font-weight:600">Local Agent</span> screen, then enter it here.</p>

        <!-- Compact "known server" line, shown when the server is baked in/paired
             so the user only enters the 6-digit code. A "Change" link reveals the
             full input (#serverBlock) for a different server. -->
        <div id="serverKnown" style="display:none;align-items:center;gap:8px;margin-bottom:16px;padding:9px 12px;border-radius:11px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.07);font-size:12px;color:#9494a6">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#7a7a8c" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a15 15 0 0 1 0 18M12 3a15 15 0 0 0 0 18"/></svg>
          <span style="color:#7a7a8c">Server</span>
          <span id="serverHost" style="color:#c3c3d0;font-weight:600;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span>
          <a id="serverChange" href="#" style="margin-left:auto;flex-shrink:0;color:#a78bfa;text-decoration:none;font-weight:600">Change</a>
        </div>

        <div id="serverBlock" style="margin-bottom:16px">
          <label style="display:block;font-size:12px;font-weight:600;color:#9494a6;margin-bottom:8px">Server URL</label>
          <div id="urlBox" style="display:flex;align-items:center;gap:10px;height:46px;padding:0 14px;border-radius:12px;background:#16161f;border:1px solid rgba(255,255,255,.1);transition:border-color .18s">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#7a7a8c" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a15 15 0 0 1 0 18M12 3a15 15 0 0 0 0 18"/></svg>
            <input id="server" placeholder="https://your-qagent-server/api" style="flex:1;min-width:0;background:none;border:none;color:#ECECF1;font-size:13.5px;font-family:inherit" />
          </div>
        </div>

        <div style="margin-bottom:22px">
          <label style="display:flex;justify-content:space-between;align-items:center;font-size:12px;font-weight:600;color:#9494a6;margin-bottom:8px"><span>Pair code</span><span style="font-size:10.5px;font-weight:600;color:#6c6c7e">one-time · 6 digits</span></label>
          <div style="position:relative">
            <input id="code" inputmode="numeric" maxlength="6" style="position:absolute;inset:0;width:100%;height:100%;opacity:0;cursor:text;font-size:16px;z-index:2" />
            <div style="display:flex;align-items:center;gap:11px;pointer-events:none">
              <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:9px;flex:1">
                <div class="cell" id="cell0"><span class="d"></span><span class="cr" style="display:none;width:2px;height:24px;background:#a78bfa;animation:caret 1.1s step-end infinite"></span></div>
                <div class="cell" id="cell1"><span class="d"></span><span class="cr" style="display:none;width:2px;height:24px;background:#a78bfa;animation:caret 1.1s step-end infinite"></span></div>
                <div class="cell" id="cell2"><span class="d"></span><span class="cr" style="display:none;width:2px;height:24px;background:#a78bfa;animation:caret 1.1s step-end infinite"></span></div>
              </div>
              <span style="width:13px;height:2.5px;border-radius:2px;background:#3a3a48;flex-shrink:0"></span>
              <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:9px;flex:1">
                <div class="cell" id="cell3"><span class="d"></span><span class="cr" style="display:none;width:2px;height:24px;background:#a78bfa;animation:caret 1.1s step-end infinite"></span></div>
                <div class="cell" id="cell4"><span class="d"></span><span class="cr" style="display:none;width:2px;height:24px;background:#a78bfa;animation:caret 1.1s step-end infinite"></span></div>
                <div class="cell" id="cell5"><span class="d"></span><span class="cr" style="display:none;width:2px;height:24px;background:#a78bfa;animation:caret 1.1s step-end infinite"></span></div>
              </div>
            </div>
          </div>
        </div>

        <button id="connect" style="display:flex;align-items:center;justify-content:center;gap:9px;width:100%;height:47px;border-radius:13px;border:none;font-family:inherit;font-weight:700;font-size:14.5px;transition:transform .15s,box-shadow .2s">
          <span id="connSpin" style="display:none;width:16px;height:16px;border:2px solid rgba(255,255,255,.35);border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite"></span>
          <svg id="connBolt" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 3 14h7l-1 8 10-12h-7z"/></svg>
          <span id="connLabel">Connect</span>
        </button>
        <div id="pairErr" style="margin-top:12px;font-size:12px;color:#ff9ea1;min-height:15px"></div>
      </div>

      <div style="display:flex;align-items:center;gap:8px;margin:16px 2px 14px;font-size:11.5px;color:#7a7a8c">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#6ee7b7" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0"><rect x="3" y="11" width="18" height="10" rx="2"/><path d="M7 11V8a5 5 0 0 1 10 0v3"/></svg>
        <span>Your session and credentials never leave this machine.</span>
      </div>
      <div id="envChips" style="display:flex;flex-wrap:wrap;gap:8px;padding:0 2px"></div>
    </div>

    <!-- CONNECTED -->
    <div id="connView" style="display:none">
      <div style="animation:fadeUp .5s ease both">
        <div style="position:relative;overflow:hidden;border-radius:22px;background:linear-gradient(135deg,rgba(16,185,129,.12),rgba(20,20,28,.55));backdrop-filter:blur(28px);border:1px solid rgba(52,211,153,.28);padding:22px;box-shadow:0 24px 60px -22px rgba(0,0,0,.7);margin-bottom:14px">
          <div style="display:flex;align-items:center;gap:13px">
            <div style="width:42px;height:42px;border-radius:12px;background:rgba(52,211,153,.13);border:1px solid rgba(52,211,153,.28);display:flex;align-items:center;justify-content:center;flex-shrink:0"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="#34d399" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/></svg></div>
            <div style="flex:1;min-width:0">
              <div id="machineName" style="font-size:15px;font-weight:700;color:#eafff6;letter-spacing:-.01em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis"></div>
              <div style="display:flex;align-items:center;gap:6px;margin-top:3px">
                <span style="font-size:10px;font-weight:700;letter-spacing:.09em;color:#6c8c7e">SESSION</span>
                <span id="sessionId" style="font-size:11.5px;color:#8fc7b3;font-family:'JetBrains Mono',monospace"></span>
              </div>
            </div>
          </div>
        </div>

        <div style="border-radius:18px;background:rgba(8,8,13,.72);border:1px solid rgba(255,255,255,.08);overflow:hidden">
          <div style="display:flex;align-items:center;gap:8px;padding:11px 15px;border-bottom:1px solid rgba(255,255,255,.06)">
            <span style="width:7px;height:7px;border-radius:50%;background:#34d399;box-shadow:0 0 8px #34d399;animation:livePulse 1.6s ease-in-out infinite"></span>
            <span style="font-size:11px;font-weight:700;letter-spacing:.06em;color:#9494a6">AGENT LOG</span>
            <span style="margin-left:auto;display:flex;align-items:flex-end;gap:2px;height:12px">
              <span style="width:2.5px;height:100%;border-radius:2px;background:#34d399;transform-origin:bottom;animation:barPulse 1s ease-in-out infinite"></span>
              <span style="width:2.5px;height:100%;border-radius:2px;background:#34d399;transform-origin:bottom;animation:barPulse 1s ease-in-out infinite .2s"></span>
              <span style="width:2.5px;height:100%;border-radius:2px;background:#34d399;transform-origin:bottom;animation:barPulse 1s ease-in-out infinite .4s"></span>
            </span>
          </div>
          <div id="log" style="padding:13px 15px;font-family:'JetBrains Mono',monospace;font-size:11px;line-height:1.9;max-height:210px;overflow-y:auto"></div>
        </div>
      </div>
    </div>

   </div>
  </div>
</div>

<script>
var qa = {};
var el = function(id){ return document.getElementById(id); };
var es = null;
var codeFocused = false;

if (window.qagentDesktop) {
  el("winctl").style.display = "flex";
  qa.win = function(a){ try { window.qagentDesktop[a](); } catch(e){} };
} else {
  qa.win = function(){};
}

// Explicit, user-driven update flow (desktop only). We ANNOUNCE a new version
// and only download/install when the user clicks — nothing happens silently.
(function(){
  if (!window.qagentUpdate) return;
  var bar = el("updateBar"), title = el("updTitle"), sub = el("updSub"), btn = el("updBtn");
  var stage = null; // "download" (offer to download) | "install" (offer to restart)
  function show(){ bar.style.display = "flex"; }
  btn.addEventListener("click", function(){
    if (stage === "install"){ window.qagentUpdate.install(); return; }
    if (stage === "download"){
      title.textContent = "Downloading update\\u2026"; sub.textContent = "0%";
      btn.style.display = "none"; window.qagentUpdate.download();
    }
  });
  window.qagentUpdate.on(function(ch, d){
    d = d || {};
    if (ch === "update:available"){
      stage = "download";
      title.textContent = "Version " + d.version + " available";
      sub.textContent = "A newer Local Agent is ready to install.";
      btn.style.display = ""; btn.textContent = "Install"; show();
    } else if (ch === "update:progress"){
      sub.textContent = (d.percent || 0) + "% downloaded";
    } else if (ch === "update:downloaded"){
      stage = "install";
      title.textContent = "Update ready";
      sub.textContent = "Restart to finish installing v" + d.version + ".";
      btn.style.display = ""; btn.textContent = "Restart & install"; show();
    }
    // update:none / update:error → leave the bar as-is (hidden or mid-flow).
  });
  // Check now (the renderer is already subscribed) and every 30 minutes after.
  try { window.qagentUpdate.check(); } catch(e){}
  setInterval(function(){ try { window.qagentUpdate.check(); } catch(e){} }, 1800000);
})();

var PILL = {
  idle:       { label:"Not connected",   dot:"#7a7a8c", color:"#a0a0b2", bg:"rgba(255,255,255,.05)", border:"rgba(255,255,255,.1)",  anim:"none" },
  connecting: { label:"Connecting\\u2026", dot:"#a78bfa", color:"#c4b5fd", bg:"rgba(139,92,246,.16)", border:"rgba(139,92,246,.35)", anim:"livePulse 1.4s ease-in-out infinite" },
  connected:  { label:"Connected \\u00b7 Live", dot:"#34d399", color:"#6ee7b7", bg:"rgba(16,185,129,.14)", border:"rgba(52,211,153,.35)", anim:"livePulse 1.6s ease-in-out infinite" }
};
function setPill(k){
  var p = PILL[k]; var box = el("pill");
  box.style.background = p.bg; box.style.border = "1px solid " + p.border;
  el("pillDot").style.background = p.dot; el("pillDot").style.boxShadow = "0 0 8px " + p.dot; el("pillDot").style.animation = p.anim;
  el("pillLabel").textContent = p.label; el("pillLabel").style.color = p.color;
}

function code(){ return el("code").value; }
function canConnect(){ return el("server").value.trim() && code().length === 6; }
function paintConnectBtn(){
  var b = el("connect"), ok = canConnect();
  if (ok) { b.style.background = "linear-gradient(135deg,#8b5cf6,#6366f1)"; b.style.color = "#fff"; b.style.cursor = "pointer"; b.style.boxShadow = "0 12px 30px -10px rgba(139,92,246,.8)"; }
  else { b.style.background = "rgba(255,255,255,.06)"; b.style.color = "#6c6c7e"; b.style.cursor = "not-allowed"; b.style.boxShadow = "none"; }
}
function renderCells(){
  var v = code();
  for (var i=0;i<6;i++){
    var cell = el("cell"+i);
    var filled = i < v.length;
    var active = codeFocused && i === v.length && v.length < 6;
    cell.style.border = active ? "1.5px solid #8b5cf6" : (filled ? "1.5px solid rgba(255,255,255,.16)" : "1.5px solid rgba(255,255,255,.09)");
    cell.style.boxShadow = active ? "0 0 0 3px rgba(139,92,246,.18)" : "none";
    cell.querySelector(".d").textContent = v[i] || "";
    cell.querySelector(".cr").style.display = active ? "inline-block" : "none";
  }
}

function timeStr(ts){ return new Date(ts).toTimeString().slice(0,8); }
var LOG_EMPTY_HTML = '<div id="logEmptyPh" style="color:#5c5c6e;padding:2px 0;line-height:1.7">Ready \\u00b7 waiting for activity\\u2026<br><span style="color:#4c4c5a;font-size:10.5px">Runs, live-authoring and self-heal will stream here.</span></div>';
function logColor(t){
  if (t === "error" || t === "auth-error") return "#ff9ea1";
  if (t === "auth-waiting") return "#fbbf24";
  if (t === "auth-captured") return "#6ee7b7";
  if (t === "job-claimed") return "#c4b5fd";
  if (t === "job-complete") return "#6ee7b7";
  if (t === "authoring-step") return "#c3c3d0";
  return "#8b8b9e";
}
function logMsg(ev){
  switch (ev.type){
    case "job-claimed": return "claimed execution #" + ev.executionId + " \\u00b7 run " + ev.runCode + " (" + ev.total + " spec" + (ev.total===1?"":"s") + ")";
    case "auth-waiting": return "waiting for manual login\\u2026" + (ev.url?" ("+ev.url+")":"");
    case "auth-captured": return "login captured \\u00b7 session saved locally";
    case "case-running": return "running " + ev.index + "/" + ev.total + " \\u00b7 " + (ev.ticket||"") + " " + (ev.caseCode||"");
    case "case-result": return (ev.status==="pass"?"\\u2713":"\\u2717") + " " + (ev.ticket||"") + " " + (ev.caseCode||"");
    case "progress": return null;
    case "job-complete": return "execution #" + ev.executionId + " complete \\u00b7 " + ev.passed + " passed, " + ev.failed + " failed";
    case "error": return ev.message || "error";
    case "log": return ev.message || "";
    case "authoring-step": return ev.line || null; // live authoring / self-heal steps (#400/#428)
    default: return null;
  }
}
function addLog(ev){
  if (ev.type === "agent-status") return; // status events aren't log lines; pill is driven by view state
  var msg = logMsg(ev); if (!msg) return;
  var ph = document.getElementById("logEmptyPh"); if (ph) ph.remove(); // first real line clears the placeholder
  var row = document.createElement("div");
  row.style.cssText = "display:flex;gap:10px;animation:logIn .4s ease both";
  var t = document.createElement("span"); t.style.cssText = "color:#5c5c6e;flex-shrink:0"; t.textContent = timeStr(ev.ts);
  var m = document.createElement("span"); m.style.color = logColor(ev.type); m.textContent = msg;
  row.appendChild(t); row.appendChild(m);
  var log = el("log"); log.appendChild(row); log.scrollTop = log.scrollHeight;
}
function openStream(){ if (es) return; es = new EventSource("/api/events"); es.onmessage = function(e){ try { addLog(JSON.parse(e.data)); } catch(x){} }; }
function closeStream(){ if (es){ es.close(); es = null; } }

function renderChips(s){
  var chips = [
    { name:"Playwright", ver:s.playwrightVersion || "\\u2014", dot:"#34d399" },
    { name:"Node", ver:(s.nodeVersion||"").replace(/^v/,"") || "\\u2014", dot:"#6ee7b7" },
    { name:"Chromium", ver:"auto", dot:"#60a5fa" }
  ];
  var html = "";
  for (var i=0;i<chips.length;i++){
    var c = chips[i];
    html += '<span style="display:inline-flex;align-items:center;gap:6px;padding:5px 10px;border-radius:9px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.07);font-size:10.5px;color:#9494a6">'
      + '<span style="width:6px;height:6px;border-radius:50%;background:' + c.dot + '"></span>'
      + '<span style="color:#c3c3d0;font-weight:600">' + c.name + '</span>'
      + '<span style="font-family:\\'JetBrains Mono\\',monospace;color:#7a7a8c">' + c.ver + '</span></span>';
  }
  el("envChips").innerHTML = html;
}

function hostOf(u){ try { return new URL(u).host; } catch(e) { return u; } }
function applyServerView(){
  // A known server (baked into the build, or from a prior pairing) collapses to
  // a compact line so the user only deals with the 6-digit code; empty shows the
  // full URL input.
  var known = el("server").value.trim();
  if (known){
    el("serverKnown").style.display = "flex";
    el("serverBlock").style.display = "none";
    el("serverHost").textContent = hostOf(known);
  } else {
    el("serverKnown").style.display = "none";
    el("serverBlock").style.display = "block";
  }
}
function showForm(s){
  el("formView").style.display = "block"; el("connView").style.display = "none";
  el("hdrDisconnect").style.display = "none";
  closeStream(); setPill("idle"); renderChips(s);
  if (s.serverUrl) el("server").value = s.serverUrl;
  applyServerView();
  renderCells(); paintConnectBtn();
}
function showConnected(s){
  el("formView").style.display = "none"; el("connView").style.display = "block";
  el("hdrDisconnect").style.display = "flex";
  setPill("connected");
  el("machineName").textContent = s.machine || "this machine";
  el("sessionId").textContent = "device #" + (s.deviceId==null?"?":s.deviceId);
  // The SSE stream replays the recent-events buffer on connect, so seed the log
  // ONLY from the stream (seeding from /api/state too would double every line).
  // Show a placeholder until the first line arrives so an idle/just-reconnected
  // agent reads as "ready & waiting", not a broken empty panel (#agent-log-empty).
  el("log").innerHTML = LOG_EMPTY_HTML; openStream();
}
function refresh(){
  fetch("/api/state").then(function(r){ return r.json(); }).then(function(s){
    if (s.appVersion) el("versionTag").textContent = "v" + s.appVersion;
    if (s.paired) showConnected(s); else showForm(s);
  });
}

el("server").addEventListener("input", paintConnectBtn);
el("server").addEventListener("focus", function(){ el("urlBox").style.borderColor = "rgba(139,92,246,.55)"; });
el("server").addEventListener("blur", function(){ el("urlBox").style.borderColor = "rgba(255,255,255,.1)"; });
el("serverChange").addEventListener("click", function(e){
  e.preventDefault();
  el("serverKnown").style.display = "none";
  el("serverBlock").style.display = "block";
  el("server").focus();
});
el("code").addEventListener("input", function(){
  el("code").value = el("code").value.replace(/[^0-9]/g,"").slice(0,6);
  renderCells(); paintConnectBtn();
});
el("code").addEventListener("focus", function(){ codeFocused = true; renderCells(); });
el("code").addEventListener("blur", function(){ codeFocused = false; renderCells(); });

el("connect").addEventListener("click", function(){
  if (!canConnect()) return;
  el("pairErr").textContent = "";
  el("connSpin").style.display = "block"; el("connBolt").style.display = "none";
  el("connLabel").textContent = "Connecting\\u2026"; setPill("connecting");
  fetch("/api/pair", { method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({ code: code().trim(), server: el("server").value.trim() }) })
    .then(function(r){ return r.json().then(function(j){ return { ok:r.ok, j:j }; }); })
    .then(function(res){ if (!res.ok) throw new Error(res.j.error || "Pairing failed"); refresh(); })
    .catch(function(e){ el("pairErr").textContent = e.message; setPill("idle"); })
    .then(function(){
      el("connSpin").style.display = "none"; el("connBolt").style.display = "block"; el("connLabel").textContent = "Connect";
    });
});
el("hdrDisconnect").addEventListener("click", function(){
  fetch("/api/disconnect", { method:"POST" }).then(function(){ closeStream(); refresh(); });
});

refresh();
</script>
</body>
</html>`;
