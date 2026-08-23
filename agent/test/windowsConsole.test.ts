/**
 * Console-window suppression on Windows (#421).
 *
 * The bug: during live authoring the operator saw a terminal window flash over
 * and over. The flashing processes were GRANDCHILDREN — Claude's Bash tool runs
 * `browser-harness` (a console-subsystem Python CLI) once per step — so the
 * original diagnosis was that `windowsHide` "does not propagate" and that a
 * native GUI-subsystem shim or a `conhost --headless` wrapper would be needed.
 *
 * Measured on Windows 11, that diagnosis is wrong in a useful way:
 *
 *   - `CREATE_NO_WINDOW` (what `windowsHide: true` sets) IS inherited by the
 *     whole descendant tree. A child spawned with the flag gets no console at
 *     all, and a grandchild started by a plain `CreateProcess` we do not control
 *     gets none either — even when that intermediate explicitly asks for a
 *     window.
 *   - Without the flag the child gets a VISIBLE console and every grandchild
 *     attaches to that same window. That is the flash.
 *   - `conhost.exe --headless` does hide the window, but it replaces the child's
 *     stdout with terminal escape sequences — which would silently destroy the
 *     `--output-format stream-json` parsing authoring depends on (#615).
 *
 * So the fix is "the flag, on every spawn", and these tests pin both halves:
 * the source-level invariant (cheap, runs everywhere) and the runtime behaviour
 * (Windows only).
 */
import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

// This package compiles to CommonJS (see tsconfig.json), so `__dirname` is the
// right tool here — `import.meta` is not available.
// dist/test/windowsConsole.test.js -> agent/
const AGENT_ROOT = path.resolve(__dirname, "..", "..");
const SRC_DIR = path.join(AGENT_ROOT, "src");

/** Strip line + block comments so prose mentioning `spawn(` is not audited. */
function stripComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^[ \t]*\/\/.*$/gm, "");
}

/** The source text of the argument list of the call whose `(` is at `open`. */
function callArgs(text: string, open: number): string {
  let depth = 0;
  for (let i = open; i < text.length; i++) {
    const ch = text[i];
    if (ch === "(") depth++;
    else if (ch === ")") {
      depth--;
      if (depth === 0) return text.slice(open + 1, i);
    }
  }
  return text.slice(open + 1);
}

interface SpawnSite {
  file: string;
  args: string;
}

/**
 * The options argument is sometimes a shared variable (`spawn(cmd, args, opts)`
 * in ui.ts) rather than an inline literal. Splice that declaration's initialiser
 * in so the audit reads the options that are actually passed, instead of
 * demanding every call site inline them.
 */
function inlineOptionVars(args: string, fileText: string): string {
  const ident = args.trim().match(/(?:^|,)\s*([A-Za-z_$][\w$]*)\s*$/);
  if (!ident) return args;
  const decl = fileText.indexOf(`${ident[1]} = {`);
  if (decl < 0) return args;
  const open = fileText.indexOf("{", decl);
  let depth = 0;
  for (let i = open; i < fileText.length; i++) {
    if (fileText[i] === "{") depth++;
    else if (fileText[i] === "}") {
      depth--;
      if (depth === 0) return `${args}\n/* resolved ${ident[1]} */ ${fileText.slice(open, i + 1)}`;
    }
  }
  return args;
}

function collectSpawnSites(): SpawnSite[] {
  const sites: SpawnSite[] = [];
  for (const name of fs.readdirSync(SRC_DIR).sort()) {
    if (!name.endsWith(".ts")) continue;
    const raw = stripComments(fs.readFileSync(path.join(SRC_DIR, name), "utf-8"));
    const re = /\bspawn\s*\(/g;
    let m: RegExpExecArray | null;
    while ((m = re.exec(raw))) {
      const open = raw.indexOf("(", m.index);
      sites.push({ file: name, args: inlineOptionVars(callArgs(raw, open), raw) });
    }
  }
  return sites;
}

test("every spawn in agent/src passes windowsHide: true", () => {
  const sites = collectSpawnSites();
  // Guard the guard: if the scan finds nothing the assertions below are vacuous
  // and this test would pass while proving nothing (the #470 failure mode).
  assert.ok(sites.length >= 6, `expected to find several spawn() sites, found ${sites.length}`);

  const missing = sites.filter((s) => !/windowsHide\s*:\s*true/.test(s.args));
  assert.deepEqual(
    missing.map((s) => s.file),
    [],
    "these spawn() sites would pop a console window on Windows — add windowsHide: true",
  );
});

test("no spawn in agent/src uses stdio: inherit", () => {
  // `stdio: "inherit"` is a double regression: measured, the child still gets a
  // console object (only the window is hidden), and under Electron — a GUI
  // process with no stdout — the child's output goes nowhere, so a first-run
  // provisioning failure becomes invisible. Capture it instead.
  const offenders = collectSpawnSites()
    .filter((s) => /stdio\s*:\s*["']inherit["']/.test(s.args))
    .map((s) => s.file);
  assert.deepEqual(offenders, [], 'use piped stdio and forward it to the log, not stdio: "inherit"');
});

test("the authoring spawns keep stdout and stderr piped", () => {
  // Suppressing consoles must never suppress the pipes: the agent parses
  // claude's stream-json stdout and posts it as events (#615). A `stdio:
  // "ignore"` here would make authoring fail silently.
  const runner = stripComments(fs.readFileSync(path.join(SRC_DIR, "runner.ts"), "utf-8"));
  const claudeSite = callArgs(runner, runner.indexOf("(", runner.indexOf("spawn(claudeCli()")));
  assert.match(claudeSite, /stdio:\s*\["ignore",\s*"pipe",\s*"pipe"\]/);
  assert.match(claudeSite, /windowsHide:\s*true/);
});

/* ------------------------------------------------------------------------- */
/* Runtime behaviour (Windows only)                                          */
/* ------------------------------------------------------------------------- */

/**
 * Build the three-level fixture the real flow has:
 *
 *   top   — spawned DETACHED so it has NO console of its own: the packaged
 *           Electron desktop agent.
 *   mid   — spawned by top WITH windowsHide: the `claude` CLI.
 *   probe — spawned by mid WITHOUT windowsHide (explicitly false), reporting its
 *           own console window: `browser-harness`, launched by a Bash tool we do
 *           not control.
 */
function writeFixture(dir: string): { top: string; log: string } {
  const log = path.join(dir, "log.txt");
  const probe = path.join(dir, "probe.ps1");
  const mid = path.join(dir, "mid.js");
  const top = path.join(dir, "top.js");

  fs.writeFileSync(
    probe,
    [
      "param([string]$Log)",
      "Add-Type -Namespace W -Name N -MemberDefinition @'",
      '[DllImport("kernel32.dll")] public static extern System.IntPtr GetConsoleWindow();',
      "'@",
      "$h = [W.N]::GetConsoleWindow()",
      'Add-Content -Path $Log -Value "PROBE hwnd=$h"',
      'Write-Output "PROBE-STDOUT-OK"',
      "",
    ].join("\n"),
    "utf-8",
  );

  fs.writeFileSync(
    mid,
    [
      "const { spawn } = require('child_process');",
      "const fs = require('fs');",
      "const log = process.argv[2];",
      "const ps = process.env.SystemRoot + '\\\\System32\\\\WindowsPowerShell\\\\v1.0\\\\powershell.exe';",
      `const probe = ${JSON.stringify(probe)};`,
      "const c = spawn(ps, ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', probe, '-Log', log],",
      "  { stdio: ['ignore', 'pipe', 'pipe'], windowsHide: false });",
      "let out = '';",
      "c.stdout.on('data', (d) => { out += d; });",
      "c.stderr.on('data', (d) => { out += d; });",
      "c.on('close', () => {",
      "  fs.appendFileSync(log, 'MID captured=' + JSON.stringify(out.trim()) + '\\n');",
      "  fs.appendFileSync(log, 'DONE\\n');",
      "});",
      "",
    ].join("\n"),
    "utf-8",
  );

  fs.writeFileSync(
    top,
    [
      "const { spawn } = require('child_process');",
      "const log = process.argv[2];",
      `const mid = ${JSON.stringify(mid)};`,
      "spawn(process.execPath, [mid, log], { stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });",
      "",
    ].join("\n"),
    "utf-8",
  );

  return { top, log };
}

test(
  "windowsHide on the child suppresses the GRANDCHILD console, pipes intact",
  { skip: process.platform !== "win32" ? "Windows-only console behaviour" : false },
  async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "qagent-console-421-"));
    try {
      const { top, log } = writeFixture(dir);
      fs.writeFileSync(log, "");

      // Detached + stdio ignore => this level has no console, like the GUI agent.
      const child = spawn(process.execPath, [top, log], {
        detached: true,
        stdio: "ignore",
        windowsHide: true,
      });
      child.unref();

      const deadline = Date.now() + 60_000;
      let text = "";
      while (Date.now() < deadline) {
        text = fs.readFileSync(log, "utf-8");
        if (text.includes("DONE")) break;
        await new Promise((r) => setTimeout(r, 250));
      }
      assert.ok(text.includes("DONE"), `fixture never finished; log so far:\n${text}`);

      // The grandchild asked for a window (windowsHide: false) and still got no
      // console at all, because CREATE_NO_WINDOW came down from `mid`.
      assert.match(text, /PROBE hwnd=0\b/, `grandchild allocated a console:\n${text}`);
      // …and its stdout still reached its parent through the pipe.
      assert.match(text, /MID captured="PROBE-STDOUT-OK"/, `grandchild stdout was lost:\n${text}`);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  },
);
