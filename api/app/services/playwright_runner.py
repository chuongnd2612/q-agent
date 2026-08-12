"""Real Playwright execution — invokes `npx playwright test` as a subprocess.

Per ADR 0001 there is no simulated fallback: the runner genuinely spawns
Playwright against the generated `*.spec.ts` files for a run and parses its
JSON reporter output. Failures (missing binary, non-zero exit with no report,
timeout) are surfaced via WS + ExecutionResult/Execution status rather than
faked.

The parsing step is a pure function, ``parse_playwright_report``, so tests can
exercise the mapping from a Playwright JSON report to per-spec results without
spawning a real browser.

Playwright invocation choice: we run `npx playwright test` with **cwd = the
run's spec directory** (`scoped_specs_dir(run.owner_id)/{run.code}`, ADR 0009 §1),
not the frontend
`app/` dir. The frontend has no Playwright installation or config of its own
(checked: no `playwright.config.*` in `app/`, `@playwright/test` isn't a
frontend dependency), so anchoring execution there would require it to own a
browser-testing setup it doesn't have. Instead we write a minimal
`playwright.config.ts` into the run's spec dir (if one doesn't already exist)
and run `npx playwright test` there; npx resolves `@playwright/test` from
whichever npm-managed location has it installed/cached on the machine. This
keeps the specs, config, and captured artifacts self-contained per run.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app import db as db_module
from app.config import settings
from app.logging import logger
from app.models.automation_project import AutomationProject
from app.models.execution import Evidence, Execution, ExecutionResult
from app.models.run import Run, RunTicket
from app.models.testcase import AutomationSpec, TestCase
from app.models.ticket import Ticket
from app.services import (
    audit_service,
    automation_project_service,
    evidence_analysis,
    evidence_service,
    execution_service,
    failure_classifier,
    knowledge_service,
    placeholder_gate,
    project_config_service,
    run_context,
    run_control,
    settings_store,
    spec_examples,
    spec_service,
)
from app.services.claude_cli import ClaudeError
from app.services.run_status import set_run_status
from app.services.workspace_scope import scoped_specs_dir
from app.ws import hub

_PLAYWRIGHT_CONFIG_TEMPLATE = """\
import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: '.',
  timeout: __TIMEOUT__,
  workers: __WORKERS__,
  reporter: [['json', { outputFile: 'report.json' }]],
  use: {
    headless: __HEADLESS__,
    screenshot: 'on',
    video: '__VIDEO__',
    trace: '__TRACE__',
__EXTRA_USE__  },
});
"""

_STATUS_MAP = {
    "passed": "pass",
    "failed": "fail",
    "timedOut": "fail",
    "interrupted": "fail",
    "skipped": "skipped",
}

# Evidence.kind values a Playwright attachment `name` can map to.
_ATTACHMENT_KIND_MAP = {
    "screenshot": "screenshot",
    "video": "video",
    "trace": "trace",
    # DOM captured by the injected fixtures (see _fixtures_ts): raw page HTML and a
    # distilled interactable-element inventory used to ground self-heal + the KB.
    "qagent-dom-raw": "dom",
    "qagent-dom-distilled": "dom-distilled",
    # Console + network captured by the fixtures (see _fixtures_ts): these are NOT
    # stored as media artifacts — the evidence sink parses their JSON into the
    # result's console_logs / network_logs columns (see evidence_service).
    "qagent-console": "console",
    "qagent-network": "network",
}


def _write_config(
    spec_dir: Path,
    workers: int,
    headless: bool,
    base_url: str = "",
    storage_state: str = "",
    *,
    test_timeout_ms: int = 30000,
    action_timeout_ms: int | None = None,
    heavy_evidence: bool = True,
    capture_video: bool = False,
) -> None:
    """(Re)write playwright.config.ts from current settings.

    Rewritten on every run (not just when absent) so toggles like ``headless``
    take effect on the next execution rather than being pinned to the first run.

    Args:
        spec_dir: The run's spec directory the config is written into.
        workers: Parallel worker count.
        headless: Whether the browser runs headless.
        base_url: When non-empty, injected as ``use.baseURL`` so relative
            ``page.goto('/path')`` calls resolve against the app.
        storage_state: When non-empty, an absolute path injected as
            ``use.storageState`` so tests start from a saved auth session.
        test_timeout_ms: Per-test timeout. Heal re-runs pass a shorter value
            (``settings.heal_test_timeout_ms``) so a broken locator fails fast
            instead of stalling the full 30s every attempt (#398).
        action_timeout_ms: When set, injected as ``use.actionTimeout`` /
            ``use.navigationTimeout`` so individual broken actions fail fast too
            (heal re-runs only).
        heavy_evidence: When False, ``video``/``trace`` are ``'off'`` — used for
            intermediate heal attempts, where trace/video encoding is wasted work
            (kept on the final attempt for evidence). Screenshot stays always-on.
        capture_video: Honors the user's "Capture video" setting (#456). When
            True (and ``heavy_evidence``), video is ``'on'`` so every case is
            recorded regardless of pass/fail; when False, video is ``'off'``.
            ``trace`` is independent — it stays ``retain-on-failure`` on heavy runs.
    """
    extra_lines: list[str] = []
    if base_url:
        extra_lines.append(f"    baseURL: {json.dumps(base_url)},")
    if storage_state:
        extra_lines.append(f"    storageState: {json.dumps(storage_state)},")
    if action_timeout_ms is not None:
        extra_lines.append(f"    actionTimeout: {int(action_timeout_ms)},")
        extra_lines.append(f"    navigationTimeout: {int(action_timeout_ms)},")
    extra_use = ("\n".join(extra_lines) + "\n") if extra_lines else ""
    trace = "retain-on-failure" if heavy_evidence else "off"
    # Video follows the "Capture video" setting (always-on when enabled), not the
    # failure-only trace policy — but intermediate heal attempts still skip it.
    video = "on" if (capture_video and heavy_evidence) else "off"
    content = (
        _PLAYWRIGHT_CONFIG_TEMPLATE.replace("__WORKERS__", str(workers))
        .replace("__TIMEOUT__", str(int(test_timeout_ms)))
        .replace("__HEADLESS__", "true" if headless else "false")
        .replace("__VIDEO__", video)
        .replace("__TRACE__", trace)
        .replace("__EXTRA_USE__", extra_use)
    )
    (spec_dir / "playwright.config.ts").write_text(content, encoding="utf-8")


_CAPTURE_SCRIPT = Path(__file__).resolve().parent / "pw_scripts" / "capture_auth.cjs"


_capture_lock = threading.Lock()
_capture_cooldown_until = 0.0


def capture_storage_state(base_url: str, dest: Path) -> bool:
    """Open ONE login window at a time, with a back-off so it can never storm.

    A global lock guarantees at most one headed capture browser is ever open, so a
    caller (or the UI) firing capture repeatedly can't spawn browser-after-browser.
    If a capture exits in under 5s without producing a session (i.e. the window
    "flashed"), we refuse new captures for 20s so a crash/re-trigger loop can't
    keep reopening the window — and the node error is logged for diagnosis.
    """
    global _capture_cooldown_until
    if time.monotonic() < _capture_cooldown_until:
        logger.warning("Auth capture on cooldown after a fast failure; skipping this trigger.")
        return dest.exists() and dest.stat().st_size > 0
    if not _capture_lock.acquire(blocking=False):
        logger.warning("Auth capture already in progress; ignoring duplicate trigger.")
        return dest.exists() and dest.stat().st_size > 0
    started = time.monotonic()
    try:
        result = _capture_once(base_url, dest)
    finally:
        _capture_lock.release()
    if not result and (time.monotonic() - started) < 5:
        _capture_cooldown_until = time.monotonic() + 20
        logger.warning(
            "Auth capture window exited in <5s with no session — backing off 20s. "
            "See the node log above for the cause."
        )
    return result


def _capture_once(base_url: str, dest: Path) -> bool:
    """Open a real (headed) browser at ``base_url`` for manual login, save session.

    Primary path runs the Node capture script (``capture_auth.cjs``) which, unlike
    Playwright's ``storageState``, ALSO snapshots ``sessionStorage`` (where MSAL/SPA
    tokens live) into a sibling ``sessionStorage.json``. It periodically snapshots
    both so nothing is lost when the tab closes. Falls back to the built-in
    Playwright ``open/codegen --save-storage`` if the Node script fails to produce
    a non-empty ``dest``.

    Uses the same NODE_PATH-pointed env as :func:`_invoke_playwright`, headed
    (NOT headless), bounded by ``settings.auth_capture_timeout_s``. Never raises:
    logs and returns False on any error/timeout/non-completion.

    Args:
        base_url: The application URL to open for login.
        dest: Absolute path the captured ``storageState.json`` is written to.

    Returns:
        True only when ``dest`` exists and is non-empty after the browser closes.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    nm = settings.playwright_node_modules
    nm_str = str(nm)
    env = os.environ.copy()
    env["NODE_PATH"] = nm_str + (os.pathsep + env["NODE_PATH"] if env.get("NODE_PATH") else "")

    session_dest = dest.parent / "sessionStorage.json"
    node_cmd = ["node", str(_CAPTURE_SCRIPT), base_url, str(dest), str(session_dest)]
    # ONE headed browser only — never chain a second interactive capture, which
    # would pop browser-after-browser. The Node script is the sole method (it also
    # snapshots sessionStorage, which the built-in `open --save-storage` cannot).
    try:
        logger.info("Playwright auth capture (node): {}", " ".join(node_cmd))
        proc = subprocess.run(  # noqa: S603
            node_cmd,
            cwd=nm_str,
            capture_output=True,
            text=True,
            timeout=settings.auth_capture_timeout_s,
            env=env,
        )
        if proc.returncode != 0:
            logger.warning(
                "Auth capture node exited {}: {}",
                proc.returncode,
                (proc.stderr or proc.stdout or "").strip()[:800],
            )
    except subprocess.TimeoutExpired:
        logger.warning("Playwright auth capture timed out after {}s", settings.auth_capture_timeout_s)
        return False
    except FileNotFoundError:
        logger.warning("Auth capture failed: 'node' not found on PATH")
        return False
    except Exception as exc:  # noqa: BLE001 - capture must never raise into the run
        logger.warning("Playwright auth capture (node) failed: {}", exc)
        return False

    ok = dest.exists() and dest.stat().st_size > 0
    if not ok:
        logger.warning("Auth capture produced no storageState at {} — check the node log above", dest)
    return ok


def _fixtures_ts(session_file: Path, replay_session: bool, capture_raw: bool = True) -> str:
    """TypeScript for a generated ``fixtures.ts`` that captures the page DOM after
    every test, and optionally replays a captured sessionStorage.

    The module re-exports Playwright's ``test`` extended with:

    * an ``{auto: true}`` fixture that, after each test, best-effort attaches a
      distilled inventory of the page's interactable elements
      (``qagent-dom-distilled``) — and, when ``capture_raw``, the full raw HTML
      (``qagent-dom-raw``) — via ``testInfo.attach``, so the runner and self-heal
      loop ground on the REAL failure-page DOM instead of guessing. The distilled
      capture retries once after a short settle so a transiently-busy page still
      yields elements (#398 — "ensure real DOM, not the spec with no DOM"); all
      capture is wrapped in try/catch so it can never fail a test.
    * (only when ``replay_session``) a ``context`` override adding an init script
      that restores the captured ``sessionStorage`` entries (where MSAL/SPA tokens
      live) for the current origin before any app code runs, so the restored
      session doesn't bounce back to the login page.

    Args:
        session_file: Absolute path to the ``sessionStorage.json`` snapshot; embedded
            as a JSON-encoded string literal and read at runtime only when
            ``replay_session`` is set.
        replay_session: Whether to inject the sessionStorage-replay ``context``
            override (DOM capture is always injected regardless).
        capture_raw: When False, skip the (large, heal-unused) raw-HTML attachment
            — used for intermediate heal attempts; the distilled inventory the
            fixer needs is always captured (#398).

    Returns:
        The fixtures module source.
    """
    raw_block = (
        (
            "    try {\n"
            "      const raw = await page.content();\n"
            "      const rawPath = testInfo.outputPath('qagent-dom-raw.html');\n"
            "      fs.writeFileSync(rawPath, raw, 'utf-8');\n"
            "      await testInfo.attach('qagent-dom-raw', { path: rawPath, contentType: 'text/html' });\n"
            "    } catch {}\n"
        )
        if capture_raw
        else ""
    )
    context_fixture = (
        (
            "  context: async ({ context }, use) => {\n"
            "    await context.addInitScript((sessions: Record<string, Record<string, string>>) => {\n"
            "      try {\n"
            "        const entries = sessions[location.origin];\n"
            "        if (entries) for (const k in entries) window.sessionStorage.setItem(k, entries[k]);\n"
            "      } catch {}\n"
            "    }, SESSIONS);\n"
            "    await use(context);\n"
            "  },\n"
        )
        if replay_session
        else ""
    )
    return (
        "import { test as base, expect } from '@playwright/test';\n"
        "import * as fs from 'fs';\n"
        "\n"
        f"const SESSION_FILE = {json.dumps(str(session_file))};\n"
        "let SESSIONS: Record<string, Record<string, string>> = {};\n"
        "try { SESSIONS = JSON.parse(fs.readFileSync(SESSION_FILE, 'utf-8')); } catch {}\n"
        "\n"
        "export const test = base.extend<{ _domCapture: void }>({\n"
        f"{context_fixture}"
        "  // Always-on DOM capture: after each test, snapshot the live page so the\n"
        "  // runner (evidence) and self-heal loop (real selectors) can use it.\n"
        "  _domCapture: [async ({ page }, use, testInfo) => {\n"
        "    // Console + network capture (#456): collect for the whole test, pass or\n"
        "    // fail. Listeners registered BEFORE use() so nothing is missed; the JSON\n"
        "    // is attached after and parsed into console_logs/network_logs columns.\n"
        "    const __net: any[] = []; const __con: any[] = []; const __started = new Map();\n"
        "    page.on('request', (r) => { try { __started.set(r, Date.now()); } catch {} });\n"
        "    page.on('response', (resp) => { try { if (__net.length < 300) { const req = resp.request(); const t0 = __started.get(req); __net.push({ method: req.method(), url: req.url(), status: resp.status(), durationMs: t0 ? Date.now() - t0 : 0 }); } } catch {} });\n"
        "    page.on('requestfailed', (r) => { try { if (__net.length < 300) { const t0 = __started.get(r); __net.push({ method: r.method(), url: r.url(), status: 0, durationMs: t0 ? Date.now() - t0 : 0, failed: true }); } } catch {} });\n"
        "    page.on('console', (msg) => { try { if (__con.length < 500) __con.push({ level: msg.type(), text: String(msg.text()).slice(0, 2000) }); } catch {} });\n"
        "    page.on('pageerror', (err) => { try { if (__con.length < 500) __con.push({ level: 'error', text: String((err && err.message) || err).slice(0, 2000) }); } catch {} });\n"
        "    await use();\n"
        "    // Distilled inventory FIRST (it's what self-heal needs) — retry once\n"
        "    // after a short settle so a transiently-busy failure page still yields\n"
        "    // real elements instead of leaving the fixer with no DOM (#398).\n"
        "    const runDistill = () => page.evaluate(() => {\n"
        "      const SEL = 'a,button,input,select,textarea,[role],[data-testid],[data-test],[id]';\n"
        "      const elements = Array.from(document.querySelectorAll(SEL)).slice(0, 400).map((node) => {\n"
        "        const el = node as HTMLElement;\n"
        "        const text = (el.innerText || '').trim().slice(0, 80);\n"
        "        return {\n"
        "          tag: el.tagName.toLowerCase(),\n"
        "          role: el.getAttribute('role') || undefined,\n"
        "          testId: el.getAttribute('data-testid') || el.getAttribute('data-test') || undefined,\n"
        "          id: el.id || undefined,\n"
        "          name: el.getAttribute('name') || undefined,\n"
        "          text: text || undefined,\n"
        "          placeholder: el.getAttribute('placeholder') || undefined,\n"
        "          type: el.getAttribute('type') || undefined,\n"
        "        };\n"
        "      });\n"
        "      return { path: location.pathname, url: location.href, elements };\n"
        "    });\n"
        "    let snapshot: { path: string; url: string; elements: unknown[] } | null = null;\n"
        "    for (let i = 0; i < 2; i++) {\n"
        "      if (page.isClosed()) break;\n"
        "      try { snapshot = await runDistill(); } catch { snapshot = null; }\n"
        "      if (snapshot && Array.isArray(snapshot.elements) && snapshot.elements.length > 0) break;\n"
        "      try { await page.waitForTimeout(400); } catch { break; }\n"
        "    }\n"
        "    if (snapshot) {\n"
        "      try {\n"
        "        const distilledPath = testInfo.outputPath('qagent-dom-distilled.json');\n"
        "        fs.writeFileSync(distilledPath, JSON.stringify(snapshot), 'utf-8');\n"
        "        await testInfo.attach('qagent-dom-distilled', { path: distilledPath, contentType: 'application/json' });\n"
        "      } catch {}\n"
        "    }\n"
        f"{raw_block}"
        "    try {\n"
        "      const netPath = testInfo.outputPath('qagent-network.json');\n"
        "      fs.writeFileSync(netPath, JSON.stringify(__net), 'utf-8');\n"
        "      await testInfo.attach('qagent-network', { path: netPath, contentType: 'application/json' });\n"
        "    } catch {}\n"
        "    try {\n"
        "      const conPath = testInfo.outputPath('qagent-console.json');\n"
        "      fs.writeFileSync(conPath, JSON.stringify(__con), 'utf-8');\n"
        "      await testInfo.attach('qagent-console', { path: conPath, contentType: 'application/json' });\n"
        "    } catch {}\n"
        "  }, { auto: true }],\n"
        "});\n"
        "\n"
        "export { expect };\n"
        "export type * from '@playwright/test';\n"
    )


def fixtures_specifier(relative: Path) -> str:
    """The module specifier a file at ``relative`` uses to reach the root ``fixtures``.

    ``fixtures.ts`` is generated at the **root** of the staged run dir, so a file
    nested inside it needs one ``../`` per directory level:
    ``x.spec.ts`` -> ``./fixtures``, ``tests/SUR-1428/x.spec.ts`` ->
    ``../../fixtures``.

    Split out (and public) because the Local Agent must compute this **identically**
    — #541 mirrors this function byte-for-byte, or server and device diverge on
    which specifier a nested spec gets.

    Args:
        relative: The file's path *relative to the staged run dir*.

    Returns:
        A POSIX relative specifier without the ``.ts`` extension.

    Note:
        The staged project also has a ``fixtures/`` **directory** (one of
        ``automation_project_service.LIBRARY_DIRS``), so ``./fixtures`` is
        nominally ambiguous. Node/TS module resolution tries ``fixtures.ts``
        before ``fixtures/index.ts``, so the generated file always wins — but a
        project that ever adds ``fixtures/index.ts`` would be relying on that
        ordering, which is why nothing should be generated into ``fixtures/``.
    """
    depth = len(relative.parts) - 1
    return "./fixtures" if depth == 0 else "../" * depth + "fixtures"


# Files under the staged run dir that must NEVER have their '@playwright/test'
# import rewritten:
#   * the generated fixtures.ts itself — it IS the re-export site;
#   * playwright.config.ts (and any *.config.ts) — `defineConfig` lives only in
#     the real package, and pointing the config at fixtures.ts would make the
#     config import the fixtures it configures;
#   * *.d.ts declaration files.
def _skip_fixture_rewrite(relative: Path) -> bool:
    name = relative.name
    if relative.parts[0] in ("node_modules", ".git"):
        return True
    if len(relative.parts) == 1 and name == "fixtures.ts":
        return True
    return name.endswith(".config.ts") or name.endswith(".d.ts")


def _apply_fixtures(
    spec_dir: Path, session_file: Path, replay_session: bool, capture_raw: bool = True
) -> None:
    """Point every staged TS file's Playwright import at ``fixtures.ts`` and write it.

    DOM capture is always on, so fixtures are ALWAYS injected (unlike the previous
    auth-only behavior): each import of ``'@playwright/test'`` is rewritten to the
    generated root ``fixtures`` module and ``fixtures.ts`` is (re)written every run.
    ``replay_session`` only controls whether the generated module *also* replays the
    captured sessionStorage. Only the import module specifier is touched (generated
    specs import just ``test``/``expect``/types from ``@playwright/test``, and
    ``fixtures.ts`` re-exports ``expect`` plus ``export type * from
    '@playwright/test'``).

    **Depth-aware since #540.** A staged project has files at depth — specs at
    ``tests/<TICKET>/x.spec.ts``, page objects at ``pages/Foo.ts`` — so the flat
    ``'./fixtures'`` specifier the old implementation wrote would not resolve. The
    walk is now recursive over ``**/*.ts`` with the per-file specifier from
    :func:`fixtures_specifier` and the exclusions in :func:`_skip_fixture_rewrite`.
    Imports of ``@q-agent/playwright-base`` are left alone: that package already
    ships an extended ``test`` with the same evidence/session fixtures.

    Args:
        spec_dir: The run's staged spec directory (project library + this run's specs).
        session_file: Absolute path to the ``sessionStorage.json`` snapshot embedded
            in the generated ``fixtures.ts``.
        replay_session: Whether sessionStorage replay is active for this run.
        capture_raw: Whether to also capture the raw-HTML DOM attachment (off for
            intermediate heal attempts — #398).
    """
    for path in sorted(spec_dir.rglob("*.ts")):
        if not path.is_file():
            continue
        relative = path.relative_to(spec_dir)
        if _skip_fixture_rewrite(relative):
            continue
        specifier = fixtures_specifier(relative)
        text = path.read_text(encoding="utf-8")
        new_text = text.replace("'@playwright/test'", f"'{specifier}'").replace(
            '"@playwright/test"', f'"{specifier}"'
        )
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")
    (spec_dir / "fixtures.ts").write_text(
        _fixtures_ts(session_file, replay_session, capture_raw), encoding="utf-8"
    )


# --------------------------------------------------------- standalone auth capture
# Project keys with an in-flight headed-browser capture, so the API can report
# ``capturing`` and refuse to open a second browser for the same project.
_capturing: set[str] = set()


def is_capturing(project_key: str) -> bool:
    """Return True while a standalone login capture is running for ``project_key``."""
    return project_key in _capturing


def start_capture(project_key: str, base_url: str, owner_id: int | None = None) -> None:
    """Kick off a background headed-browser login capture for a project.

    Guards on :data:`_capturing` so at most one browser is open per project. When
    no capture is already in flight, marks the key as capturing and spawns a
    daemon thread that runs :func:`capture_storage_state` against the project's
    :func:`project_config_service.auth_path`, always discarding the key when done.

    Args:
        project_key: The project whose session is being captured.
        base_url: The application URL to open for manual login.
        owner_id: The project config's owner (ADR 0009 — scopes the saved
            session under the owner's workspace, or the shared namespace when
            ``None``).
    """
    if project_key in _capturing:
        return
    _capturing.add(project_key)

    def _worker() -> None:
        try:
            capture_storage_state(base_url, project_config_service.auth_path(project_key, owner_id))
        finally:
            _capturing.discard(project_key)

    threading.Thread(target=_worker, daemon=True).start()


def parse_playwright_report(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Map a Playwright JSON-reporter report to per-spec result dicts.

    Args:
        report: The parsed contents of Playwright's `--reporter=json` output
            (top-level keys include ``suites``).

    Returns:
        A list of dicts, one per test spec, each shaped as::

            {
                "file": str,               # spec file name, e.g. "1428-TC-01.spec.ts"
                "title": str,               # test title
                "status": "pass"|"fail"|"skipped",
                "duration_ms": int,
                "error_message": str,
                "attachments": [{"kind": str, "path": str}, ...],
            }

        Nested suites (Playwright groups specs per file, and can nest describe
        blocks) are flattened. If a test was retried, the **last** result is
        used to determine final status/duration/error, matching what the
        Playwright UI reports as the outcome.
    """
    out: list[dict[str, Any]] = []

    def walk(suite: dict[str, Any], file_hint: str) -> None:
        file_name = suite.get("file") or file_hint
        for spec in suite.get("specs", []) or []:
            spec_file = spec.get("file") or file_name
            for test in spec.get("tests", []) or []:
                results = test.get("results", []) or []
                last = results[-1] if results else {}
                status = _STATUS_MAP.get(last.get("status", ""), "fail")
                error = last.get("error", {}) or {}
                error_message = error.get("message", "") if isinstance(error, dict) else str(error)
                attachments = [
                    {"kind": _ATTACHMENT_KIND_MAP.get(a.get("name", ""), a.get("name", "")), "path": a.get("path", "")}
                    for a in last.get("attachments", []) or []
                    if a.get("name") in _ATTACHMENT_KIND_MAP and a.get("path")
                ]
                out.append(
                    {
                        "file": spec_file,
                        "title": spec.get("title", ""),
                        "status": status,
                        "duration_ms": int(last.get("duration", 0) or 0),
                        "error_message": error_message,
                        "attachments": attachments,
                    }
                )
        for child in suite.get("suites", []) or []:
            walk(child, file_name)

    for top_suite in report.get("suites", []) or []:
        walk(top_suite, top_suite.get("file", ""))

    return out


def _playwright_command(workers: int) -> list[str]:
    """Command to run Playwright, preferring the installed binary in the configured
    node_modules (its browsers are installed) over a fresh `npx` fetch.
    """
    nm = settings.playwright_node_modules
    for candidate in (nm / ".bin" / "playwright.cmd", nm / ".bin" / "playwright"):
        if candidate.exists():
            return [str(candidate), "test", f"--workers={workers}"]
    return [settings.playwright_bin, "playwright", "test", f"--workers={workers}"]


def _invoke_playwright(
    spec_dir: Path, workers: int, timeout_s: int, spec_file: str = "", run_id: int | None = None
) -> tuple[int, str, str]:
    """Run Playwright in spec_dir. Returns (returncode, stdout, stderr).

    The spec dir has no node_modules, so we point NODE_PATH at the configured
    install; that's how the specs' and config's `@playwright/test` imports resolve.

    Args:
        spec_file: When non-empty, only this spec file (relative to spec_dir) is
            run — used by the self-heal loop to re-run a single case. Empty runs
            the whole suite.
        run_id: When given, the subprocess is registered with
            ``app.services.run_control`` for the duration of the call, so a
            concurrent ``kill_processes(run_id)`` (mid-run cancel) can terminate
            it. Omitted (default) preserves the plain, unregistered invocation.
    """
    cmd = _playwright_command(workers)
    if spec_file:
        # Insert the target file right after the `test` subcommand.
        insert_at = cmd.index("test") + 1
        cmd.insert(insert_at, spec_file)
    nm = str(settings.playwright_node_modules)
    env = os.environ.copy()
    env["NODE_PATH"] = nm + (os.pathsep + env["NODE_PATH"] if env.get("NODE_PATH") else "")
    logger.info("Playwright: {} (cwd={}, NODE_PATH={})", " ".join(cmd), spec_dir, nm)

    if run_id is None:
        proc = subprocess.run(  # noqa: S603
            cmd,
            cwd=str(spec_dir),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            shell=True,  # noqa: S602 - .cmd resolution on Windows
            env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr

    # Registered path: use Popen (not the blocking subprocess.run) so a
    # concurrent cancel can kill the live process via run_control.
    popen = subprocess.Popen(  # noqa: S603, S602 - .cmd resolution on Windows
        cmd,
        cwd=str(spec_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=True,
        env=env,
    )
    run_control.register_process(run_id, popen)
    try:
        stdout, stderr = popen.communicate(timeout=timeout_s)
    finally:
        run_control.unregister_process(run_id, popen)
    return popen.returncode, stdout, stderr


def _store_evidence(db, run: Run, result: ExecutionResult, attachments: list[dict]) -> None:
    """Copy/record evidence artifacts for a result under the run owner's scoped
    evidence dir (ADR 0009 §1). Thin wrapper over
    :func:`app.services.evidence_service.store_uploaded_evidence` (shared with
    the Local Agent's multipart evidence upload) — each attachment's on-disk
    ``path`` is copied in using its own basename as the destination filename.

    Console/network captures (#456) are JSON data, not media: they're decoded
    into the result's ``console_logs`` / ``network_logs`` columns instead of
    being stored as files.
    """
    for att in attachments:
        if evidence_service.is_log_capture(att["kind"]):
            evidence_service.apply_log_capture(result, att["kind"], att["path"])
        else:
            evidence_service.store_uploaded_evidence(
                db, run, result, att["kind"], att["path"], Path(att["path"]).name
            )


def _resolve_project_for_run(db, run: Run, env: str) -> tuple[str | None, str, bool, str]:
    """Resolve a run's project identity and manual-auth settings.

    Walks the run's first ticket to its provider, resolves the project key, and
    reads that project's config. Returns ``(project_key, base_url, manual_auth,
    provider_kind)`` where base_url respects the execution ``env``. All fields are
    best-effort — a missing project yields ``(None, "", False, "")``.
    """
    run_ticket = (
        db.query(RunTicket)
        .filter(RunTicket.run_id == run.id)
        .order_by(RunTicket.position, RunTicket.id)
        .first()
    )
    if run_ticket is None:
        return None, "", False, ""
    ticket = (
        db.query(Ticket)
        .filter(Ticket.external_id == run_ticket.ticket_external_id)
        .first()
    )
    provider_kind = ticket.provider_kind if ticket else ""
    if ticket is None or not provider_kind:
        return None, "", False, ""
    project_key = project_config_service.project_key_for_ticket(db, ticket)
    base_url = project_config_service.base_url_for(db, ticket, env=env)
    manual_auth = False
    if project_key:
        cfg = project_config_service.get_config(db, project_key)
        manual_auth = bool(cfg.manual_auth) if cfg else False
    return project_key, base_url, manual_auth, provider_kind


def _fail_all_results(
    db, run: Run, execution: Execution, results: list[ExecutionResult], message: str
) -> None:
    """Mark every result failed with ``message`` and finalize the execution.

    Used when a run cannot proceed (e.g. manual login was not completed) so the
    UI shows a clear reason on every case without any specs being run.
    """
    from datetime import datetime, timezone

    run_id_str = str(run.id)
    for result in results:
        result.status = "fail"
        result.error_message = message
        result.duration_ms = 0
        db.commit()
        hub.publish(
            run_id_str,
            "exec.case.result",
            {
                "ticket": result.ticket_external_id,
                "caseCode": result.case_code,
                "status": result.status,
                "durationMs": result.duration_ms,
            },
        )
    total = len(results)
    execution.passed = 0
    execution.failed = total
    execution.total = total
    execution.progress = 100
    execution.status = "done"
    execution.log = message
    execution.finished_at = datetime.now(timezone.utc)
    db.commit()
    hub.publish(run_id_str, "exec.progress", {"progress": 100, "passed": 0, "failed": total, "remaining": 0})
    hub.publish(run_id_str, "exec.done", {"passed": 0, "failed": total})
    set_run_status(db, run, "evidence")
    audit_service.record(
        category="execution", actor_type="ai", action="Executed test run",
        target=f"{run.code} · {total} cases", status="error", meta=message,
    )


# ---------------------------------------------------------------------------
# Staging: an ephemeral per-run copy of the persistent automation project (#540)
# ---------------------------------------------------------------------------


def _spec_relative_path(spec: AutomationSpec | None, ticket_external_id: str, case_code: str) -> str:
    """Where a case's spec lives, relative to the staged run dir.

    The stored ``spec.filename`` is authoritative and is trusted verbatim: it is
    ``"tests/<TICKET>/<TICKET>-<TC>.spec.ts"`` for a project-backed spec and a
    bare filename for a legacy one — including the *pre-#540 short form* for a
    run that was generated before the rename, which is exactly why we must not
    recompute it. Only a row with no filename at all falls back to the current
    convention.
    """
    filename = ((spec.filename if spec is not None else "") or "").strip()
    return filename or spec_service.spec_filename(ticket_external_id, case_code)


def _stage_specs_for_run(db, run: Run, cases: list[tuple[int, str, str]]) -> Path:
    """Prepare ``<scoped specs>/<RUN-CODE>/`` for execution and return it.

    For every project-backed spec among ``cases`` this stages its project via
    ``automation_project_service.stage_for_run``: the **whole** shared library (so
    ``../pages/Foo`` imports resolve) but **only this run's spec files** — staging
    all of ``tests/`` would re-run every test ever generated for that project.
    A run legitimately spanning two projects (``RunTicket`` is many-per-run, each
    with its own repo) stages each of them into the same dir; ``stage_for_run``
    merges with ``dirs_exist_ok``.

    Because the staged dir is per run code, two concurrent runs on the same
    project never share a ``playwright.config.ts`` / ``report.json`` / rewritten
    import tree.

    Legacy (``project_id IS NULL``) specs are untouched: their files were already
    written straight into this same dir by ``spec_service.write_spec_file``, so
    the dir is simply created and left alone — which is exactly today's behaviour.

    Any spec whose file is missing from the staged tree but whose row still holds
    code is materialized from the DB. That keeps the explicit "run a blocked spec
    anyway" override working for project-backed specs, whose blocked code is
    deliberately never committed to the project.

    Args:
        db: Active session.
        run: The owning run (gives the run code + owner scope).
        cases: ``(test_case_id, ticket_external_id, case_code)`` per spec to stage.

    Returns:
        The staged run directory.
    """
    spec_dir = scoped_specs_dir(run.owner_id) / run.code
    spec_dir.mkdir(parents=True, exist_ok=True)

    projects: dict[int, AutomationProject] = {}
    spec_paths: dict[int, list[str]] = {}
    pending_code: list[tuple[str, str]] = []  # (relative path, code)

    for case_id, ticket_external_id, case_code in cases:
        spec = (
            db.query(AutomationSpec).filter(AutomationSpec.test_case_id == case_id).first()
        )
        if spec is None or spec.project_id is None:
            continue
        project = db.get(AutomationProject, spec.project_id)
        if project is None:
            logger.warning("Spec {} references a missing automation project", spec.id)
            continue
        relative = _spec_relative_path(spec, ticket_external_id, case_code)
        projects[project.id] = project
        spec_paths.setdefault(project.id, []).append(relative)
        pending_code.append((relative, spec.code or ""))

    for project_id, project in projects.items():
        automation_project_service.stage_for_run(
            project, run.code, spec_paths[project_id], owner_id=run.owner_id
        )

    for relative, code in pending_code:
        destination = spec_dir / relative
        if destination.exists() or not code.strip():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(code, encoding="utf-8")
    return spec_dir


def _persist_spec_code(
    db, run: Run, case: TestCase, spec: AutomationSpec, code: str, spec_dir: Path
) -> str:
    """Write an accepted spec revision to its authoritative home + the staged dir.

    Project-backed: the project tree is the source of truth, so the file is
    written there under the project lock and committed (git is the epic's
    snapshot/rollback mechanism, and an AI-authored fix deserves history), then
    mirrored into ``spec_dir`` so the next heal attempt runs the new code.
    Legacy: unchanged — ``spec_service.write_spec_file`` into the run's spec dir.

    Returns:
        The authoritative absolute path, for ``AutomationSpec.path``.
    """
    if spec.project_id is None:
        return str(
            spec_service.write_spec_file(
                run.code, case.ticket_external_id, case.code, code, run.owner_id
            )
        )
    project = db.get(AutomationProject, spec.project_id)
    if project is None:  # the project row vanished — fall back rather than lose the fix
        logger.warning("Spec {} references a missing automation project", spec.id)
        return str(
            spec_service.write_spec_file(
                run.code, case.ticket_external_id, case.code, code, run.owner_id
            )
        )
    with automation_project_service.project_lock(project):
        path = automation_project_service.write_spec(
            project, case.ticket_external_id, case.code, code
        )
        automation_project_service.git_commit(
            project, f"fix({case.ticket_external_id}): heal {case.code}"
        )
    automation_project_service.sync_files_to_db(db, project)
    relative = _spec_relative_path(spec, case.ticket_external_id, case.code)
    staged = spec_dir / relative
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(code, encoding="utf-8")
    return str(path)


def run_execution(execution_id: int) -> None:
    """Background worker: run Playwright for an Execution and record results.

    Uses its own SessionLocal() session since this runs in a background thread.
    Publishes WS events: exec.case.running, exec.case.result, exec.progress,
    exec.done. Advances Run.status to 'evidence' when finished.
    """
    db = db_module.SessionLocal()
    try:
        execution = db.get(Execution, execution_id)
        if execution is None:
            return
        # Attribute this thread's Claude spend (auto-annotate) to the run.
        run_context.set_run(execution.run_id)
        run = db.get(Run, execution.run_id)
        if run is None:
            return

        try:
            results = (
                db.query(ExecutionResult)
                .filter(ExecutionResult.execution_id == execution_id)
                .order_by(ExecutionResult.id)
                .all()
            )
            total = len(results)
            run_id_str = str(run.id)

            if run_control.is_cancelled(run.id, db):
                logger.info("Run {} cancelled — skipping execution", run.code)
                return

            for index, result in enumerate(results, start=1):
                result.status = "running"
                db.commit()
                hub.publish(
                    run_id_str,
                    "exec.case.running",
                    {
                        "ticket": result.ticket_external_id,
                        "caseCode": result.case_code,
                        "index": index,
                        "total": total,
                    },
                )

            # Stage an ephemeral per-run copy of every project-backed spec's
            # project (library + this run's specs only). Legacy specs already sit
            # in this dir. NOTE the ordering below: staging copies the project's
            # own playwright.config.ts, and `_write_config` then OVERWRITES it —
            # deliberately. The runner's generated config is the only one that
            # carries this execution's workers/headless/baseURL/storageState and,
            # critically, the `report.json` JSON reporter the result parser reads;
            # the project's config (a `results.json` reporter, testDir './tests')
            # would silently produce no report. The project config's job is to make
            # the tree listable by the generation gate, not to run executions.
            spec_dir = _stage_specs_for_run(
                db,
                run,
                [(r.test_case_id, r.ticket_external_id, r.case_code) for r in results],
            )
            headless = bool(settings_store.load_settings().get("headless", True))

            # Resolve project auth: inject baseURL always (fixes relative goto), and
            # when manual login is enabled ensure a saved storageState exists first —
            # capturing one via a headed browser if needed, else failing cleanly.
            project_key, base_url, manual_auth, _provider = _resolve_project_for_run(
                db, run, execution.env
            )
            storage_state = ""
            if manual_auth and project_key:
                session_path = project_config_service.auth_path(project_key, run.owner_id)
                if session_path.exists() and session_path.stat().st_size > 0:
                    storage_state = str(session_path)
                elif base_url:
                    hub.publish(run_id_str, "exec.auth.waiting", {"url": base_url})
                    captured = capture_storage_state(base_url, session_path)
                    if captured:
                        storage_state = str(session_path)
                        hub.publish(run_id_str, "exec.auth.captured", {})
                    else:
                        message = "Manual login was not completed — enable/redo login capture"
                        hub.publish(run_id_str, "exec.auth.error", {"message": message})
                        _fail_all_results(db, run, execution, results, message)
                        return
                else:
                    message = "Set a base URL for the project first."
                    hub.publish(run_id_str, "exec.auth.error", {"message": message})
                    _fail_all_results(db, run, execution, results, message)
                    return

            _write_config(
                spec_dir, execution.workers, headless, base_url, storage_state,
                capture_video=bool(settings_store.load_settings().get("video", False)),
            )

            # Always inject the generated fixtures.ts (DOM capture on every run) +
            # rewrite spec imports to it. sessionStorage replay (MSAL/SPA tokens) is
            # additionally enabled only when a manual-auth session (storageState +
            # sessionStorage snapshot) actually exists.
            session_file = (
                project_config_service.session_path(project_key, run.owner_id)
                if project_key
                else spec_dir / "sessionStorage.json"
            )
            replay_session = bool(manual_auth and storage_state and session_file.exists())
            _apply_fixtures(spec_dir, session_file, replay_session)

            # A single-result execution targets just that one spec (the "run this
            # test" action); a multi-case run executes the whole suite. Playwright's
            # positional filter is matched against the whole file path, so we pass
            # the BASENAME rather than `tests/<TICKET>/<file>`: a basename matches
            # regardless of the OS path separator, and since #540 puts the full
            # ticket id in the filename, basenames are unique project-wide.
            single_spec = ""
            if len(results) == 1:
                r0 = results[0]
                r0_spec = (
                    db.query(AutomationSpec)
                    .filter(AutomationSpec.test_case_id == r0.test_case_id)
                    .first()
                )
                single_spec = Path(
                    _spec_relative_path(r0_spec, r0.ticket_external_id, r0.case_code)
                ).name

            # Last checkpoint before spawning Playwright — registered with
            # run_control so a concurrent cancel can kill it mid-run.
            if run_control.is_cancelled(run.id, db):
                logger.info("Run {} cancelled — skipping execution", run.code)
                return

            report: dict[str, Any] = {}
            run_error: str | None = None
            proc_output = ""
            started = time.monotonic()
            try:
                returncode, stdout, stderr = _invoke_playwright(
                    spec_dir, execution.workers, settings.exec_timeout_s,
                    spec_file=single_spec, run_id=run.id,
                )
                proc_output = "\n".join(p for p in (stdout, stderr) if p).strip()
                if returncode != 0:
                    logger.warning("Playwright exited {}: {}", returncode, proc_output[:1000])
            except FileNotFoundError as exc:
                run_error = f"Playwright binary not found ('{settings.playwright_bin}'): {exc}"
            except subprocess.TimeoutExpired:
                run_error = f"Playwright run timed out after {settings.exec_timeout_s}s"
            finally:
                elapsed_ms = int((time.monotonic() - started) * 1000)

            # The subprocess may have been killed by a mid-run cancel — bail
            # without finalizing, leaving the 'cancelled' status the cancel
            # endpoint already set.
            if run_control.is_cancelled(run.id, db):
                logger.info("Run {} cancelled — discarding partial execution", run.code)
                return

            report_path = spec_dir / "report.json"
            if run_error is None:
                if report_path.exists():
                    try:
                        report = json.loads(report_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError as exc:
                        run_error = f"Could not parse Playwright report: {exc}"
                else:
                    # No report → surface the real Playwright error, not just a generic message.
                    detail = f" — {proc_output[:600]}" if proc_output else ""
                    run_error = f"Playwright produced no report.json{detail}"

            parsed = parse_playwright_report(report) if report else []

            passed = failed = 0
            matched_ids: set[int] = set()
            for entry in parsed:
                entry = dict(entry)
                entry["duration_ms"] = entry["duration_ms"] or elapsed_ms
                result = execution_service.apply_result(db, results, entry)
                if result is None:
                    continue
                matched_ids.add(result.id)
                if result.status == "pass":
                    passed += 1
                elif result.status == "fail":
                    failed += 1
                _store_evidence(db, run, result, entry["attachments"])
                db.commit()
                hub.publish(
                    run_id_str,
                    "exec.case.result",
                    {
                        "ticket": result.ticket_external_id,
                        "caseCode": result.case_code,
                        "status": result.status,
                        "durationMs": result.duration_ms,
                    },
                )
                progress = int(100 * len(matched_ids) / total) if total else 100
                execution.progress = progress
                execution.passed = passed
                execution.failed = failed
                db.commit()
                hub.publish(
                    run_id_str,
                    "exec.progress",
                    {"progress": progress, "passed": passed, "failed": failed, "remaining": total - len(matched_ids)},
                )

            # Any result Playwright didn't report on (e.g. run_error) is marked failed.
            for result in results:
                if result.id in matched_ids:
                    continue
                result.status = "fail"
                result.error_message = run_error or "No result reported by Playwright"
                result.duration_ms = elapsed_ms
                failed += 1
                db.commit()
                hub.publish(
                    run_id_str,
                    "exec.case.result",
                    {
                        "ticket": result.ticket_external_id,
                        "caseCode": result.case_code,
                        "status": result.status,
                        "durationMs": result.duration_ms,
                    },
                )

            # Reflect each case's run outcome on its spec lifecycle status. On a plain
            # run we do NOT classify — failure_class stays empty; classification only
            # happens in the self-heal path (see heal_spec).
            for result in results:
                spec = (
                    db.query(AutomationSpec)
                    .filter(AutomationSpec.test_case_id == result.test_case_id)
                    .first()
                )
                if spec is None:
                    continue
                if result.status == "pass":
                    spec.status = "passed"
                elif result.status == "fail":
                    spec.status = "failed"
            db.commit()

            # Auto-analyze + annotate failure screenshots so the Evidence step yields
            # high-quality, annotated review evidence (client-requested). Each is a
            # Claude vision call, so it's gated by a setting and fully best-effort.
            if settings_store.load_settings().get("autoAnnotate", True):
                for result in results:
                    if result.status == "fail":
                        evidence_analysis.auto_annotate_result(db, run, result)

            # Persist the real Playwright output so the UI can show the run log. On
            # timeout / missing binary there is no stdout/stderr, so fall back to the
            # run_error text so the log still explains why the run failed. Keep the
            # LAST ~20000 chars so the failing tail survives truncation.
            log_text = proc_output or run_error or ""

            execution.passed = passed
            execution.failed = failed
            execution.total = total
            execution_service.finalize(db, execution, run, log_text)
        except Exception as exc:  # noqa: BLE001 - never crash the worker thread silently
            logger.error("Execution crashed for run {}: {}", run.code, exc)
            db.rollback()
            run.failed_stage = run.status
            set_run_status(db, run, "failed")
    finally:
        db.close()
        run_context.clear()


# ---------------------------------------------------------------------------
# Self-heal loop: re-run a single failing spec, feeding the failure back to
# Claude to regenerate it, until it passes or a max-attempts cap is hit.
# ---------------------------------------------------------------------------

# case_id -> {"attempt": int, "maxAttempts": int, "runId": int} for in-flight
# heals — lets the UI reflect the running state and blocks double-triggering.
_healing: dict[int, dict[str, int]] = {}
_healing_lock = threading.Lock()

# Selector string literals passed to `.locator(...)`/`getByTestId(...)` — used to
# diff a heal's before/after code for heal->KB feedback (#182), below.
_SELECTOR_LITERAL_RE = re.compile(r"(?:\.locator|getByTestId)\(\s*[\"'`]([^\"'`]+)[\"'`]")


def _selector_literals(code: str) -> set[str]:
    """Selector string literals passed to `.locator(...)`/`getByTestId(...)` in a spec."""
    return set(_SELECTOR_LITERAL_RE.findall(code or ""))


def _load_distilled_dom(attachments: list[dict]) -> dict[str, Any] | None:
    """Best-effort: load the ``dom-distilled`` attachment's JSON (the live-DOM snapshot).

    Returns the parsed ``{path, url, elements: [...]}`` payload captured by the
    injected fixtures (see ``_fixtures_ts``), or None if there is no such
    attachment or it can't be read/parsed. Never raises — DOM grounding is
    additive to the heal, not required.
    """
    for att in attachments or []:
        if att.get("kind") == "dom-distilled" and att.get("path"):
            try:
                return json.loads(Path(att["path"]).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Could not read distilled DOM snapshot {}: {}", att["path"], exc)
                return None
    return None


def _propose_healed_selector_to_kb(
    project_key: str | None, repo: str, before_code: str, after_code: str, owner_id: int | None
) -> None:
    """Best-effort: propose a self-heal's selector fix back to the project KB (#182).

    When a self-heal's accepted fix swapped exactly one selector literal and that
    fix then passed, propose the correction to ``knowledge_service`` so future
    generations reuse the healed value instead of repeating the same broken
    selector. A no-op unless the diff resolves to a single, unambiguous old->new
    selector swap (anything more ambiguous is skipped rather than guessed).
    Never raises — heal->KB feedback must never break the heal loop.
    """
    if not project_key:
        return
    try:
        removed = _selector_literals(before_code) - _selector_literals(after_code)
        added = _selector_literals(after_code) - _selector_literals(before_code)
        if len(removed) == 1 and len(added) == 1:
            knowledge_service.propose_selector_fix(
                project_key, repo, next(iter(removed)), next(iter(added)), owner_id
            )
    except Exception as exc:  # noqa: BLE001 - heal->KB feedback must never break the heal loop
        logger.warning("Heal->KB selector feedback skipped: {}", exc)


def _merge_discovered_dom_to_kb(
    project_key: str | None,
    repo: str,
    passing_code: str,
    dom_snapshot: dict[str, Any] | None,
    owner_id: int | None,
) -> None:
    """Best-effort: add the route + selectors a DOM-grounded heal pass exercised to the KB (#249).

    The route comes from the passing attempt's captured DOM; the selectors are the
    literals the passing spec actually used. Adding grounding to a KB that had none
    is what lets a future generation stop hitting the ``blocked`` gate. Never raises.
    """
    if not project_key or not dom_snapshot:
        return
    try:
        route = (dom_snapshot.get("path") or "").strip()
        selectors = sorted(_selector_literals(passing_code))
        if not route and not selectors:
            return
        knowledge_service.merge_discovered_dom(
            project_key, repo, {"route": route, "selectors": selectors}, owner_id
        )
    except Exception as exc:  # noqa: BLE001 - heal->KB feedback must never break the heal loop
        logger.warning("Heal->KB DOM merge skipped: {}", exc)


def is_healing(case_id: int) -> bool:
    return case_id in _healing


def heal_state(case_id: int) -> dict[str, Any]:
    """Current heal status for a case, for the status endpoint."""
    state = _healing.get(case_id)
    return {
        "healing": state is not None,
        "attempt": state["attempt"] if state else 0,
        "maxAttempts": state["maxAttempts"] if state else settings.heal_max_attempts,
    }


def heal_run_busy(run_id: int) -> bool:
    """True if any case in this run is currently self-healing (they share the
    run's spec dir / report.json, so heals must be serialized per run)."""
    return any(state["runId"] == run_id for state in _healing.values())


def purge_run(run_id: int) -> int:
    """Forget in-flight heal tracking for a run's cases (#420); return how many.

    The live heal subprocess itself is killed via ``run_control.kill_processes``;
    this just clears the in-memory heal-busy registry so the run's cases aren't
    reported as still healing after a stop.
    """
    with _healing_lock:
        stale = [cid for cid, state in _healing.items() if state.get("runId") == run_id]
        for cid in stale:
            _healing.pop(cid, None)
        return len(stale)


def start_heal(case_id: int, run_id: int) -> bool:
    """Register + launch a self-heal pass for one case. Returns False if another
    case in the same run is already healing (idempotent True if this exact case
    is already healing)."""
    with _healing_lock:
        if case_id in _healing:
            return True
        if heal_run_busy(run_id):
            return False
        _healing[case_id] = {
            "attempt": 0,
            "maxAttempts": settings.heal_max_attempts,
            "runId": run_id,
        }
    threading.Thread(target=heal_spec, args=(case_id,), daemon=True).start()
    return True


def _apply_heal_result(
    db,
    run: Run,
    case: TestCase,
    status: str,
    error_message: str,
    duration_ms: int,
    attachments: list[dict],
) -> None:
    """Reflect a heal outcome on the case's most recent ExecutionResult.

    Updates status/error/duration, replaces that result's evidence with the
    latest attempt's artifacts, and recomputes the owning Execution's pass/fail
    counts so dashboards stay accurate. No-op if the case was never executed.
    """
    result = (
        db.query(ExecutionResult)
        .filter(ExecutionResult.test_case_id == case.id)
        .order_by(ExecutionResult.id.desc())
        .first()
    )
    if result is None:
        return
    result.status = status
    result.error_message = error_message
    result.duration_ms = duration_ms
    db.query(Evidence).filter(Evidence.result_id == result.id).delete()
    db.commit()
    _store_evidence(db, run, result, attachments)
    db.commit()

    execution = db.get(Execution, result.execution_id)
    if execution is not None:
        siblings = (
            db.query(ExecutionResult)
            .filter(ExecutionResult.execution_id == execution.id)
            .all()
        )
        execution.passed = sum(1 for r in siblings if r.status == "pass")
        execution.failed = sum(1 for r in siblings if r.status == "fail")
        db.commit()

    hub.publish(
        str(run.id),
        "exec.case.result",
        {
            "ticket": result.ticket_external_id,
            "caseCode": result.case_code,
            "status": result.status,
            "durationMs": result.duration_ms,
        },
    )


def _set_latest_failure_class(db, case: TestCase, failure_class: str) -> None:
    """Persist a root-cause class onto the case's most recent ExecutionResult."""
    result = (
        db.query(ExecutionResult)
        .filter(ExecutionResult.test_case_id == case.id)
        .order_by(ExecutionResult.id.desc())
        .first()
    )
    if result is not None:
        result.failure_class = failure_class or ""
        db.commit()


def heal_spec(case_id: int) -> None:
    """Background worker: self-heal one test case's Playwright spec.

    Runs the case's single spec; while it fails, feeds the failure back to Claude
    to regenerate the spec and re-runs, up to ``settings.heal_max_attempts``.
    Persists each improved spec to disk + AutomationSpec, updates the case's
    latest ExecutionResult, and publishes ``heal.progress`` WS events with phase
    running | fixing | passed | failed.
    """
    db = db_module.SessionLocal()
    try:
        case = db.get(TestCase, case_id)
        if case is None:
            return
        # Attribute this thread's Claude spend (spec heal) to the case's run.
        run_context.set_run(case.run_id)
        run = db.get(Run, case.run_id)
        if run is None:
            return
        spec = (
            db.query(AutomationSpec)
            .filter(AutomationSpec.test_case_id == case_id)
            .first()
        )
        if spec is None:
            return

        run_id_str = str(run.id)
        max_attempts = settings.heal_max_attempts
        # Project-backed specs live at `tests/<TICKET>/<file>` inside the staged
        # tree; legacy ones sit at its root. `relative` is used for file writes,
        # `filename` (the basename) for Playwright's path filter and for matching
        # report entries — which the report parser already basenames.
        relative = _spec_relative_path(spec, case.ticket_external_id, case.code)
        filename = Path(relative).name
        spec_dir = _stage_specs_for_run(
            db, run, [(case.id, case.ticket_external_id, case.code)]
        )
        headless = bool(settings_store.load_settings().get("headless", True))

        # Reuse a saved manual-login session if present; never pop a capture
        # browser from the heal loop.
        project_key, base_url, manual_auth, _provider = _resolve_project_for_run(
            db, run, run.env
        )
        storage_state = ""
        session_file = spec_dir / "sessionStorage.json"
        if manual_auth and project_key:
            saved = project_config_service.auth_path(project_key, run.owner_id)
            if saved.exists() and saved.stat().st_size > 0:
                storage_state = str(saved)
            session_file = project_config_service.session_path(project_key, run.owner_id)
        replay_session = bool(manual_auth and storage_state and session_file.exists())

        context = spec_service.build_case_context(db, case, env=run.env)
        # KB view the placeholder gate compares a regenerated fix against.
        known = {
            "routes": context.get("routes", []),
            "selectors": context.get("selectors", []),
            "base_url": context.get("baseUrl", ""),
        }
        # Few-shot grounding for the fixer: proven passing specs, same project+repo.
        heal_ticket = (
            db.query(RunTicket)
            .filter(
                RunTicket.run_id == run.id,
                RunTicket.ticket_external_id == case.ticket_external_id,
            )
            .first()
        )
        heal_repo = heal_ticket.repo if heal_ticket else ""
        examples = (
            # One example is enough grounding for a targeted fix; a second full
            # spec just bloats the (now fast-model) fix prompt (#398).
            spec_examples.select_examples(db, project_key, heal_repo, case, limit=1)
            if project_key
            else []
        )

        # Materialize the current spec to disk so attempt 1 runs the real code.
        # A blocked spec has never been written to the run's spec dir (it's kept
        # out of the runnable set), so without this a self-heal on a blocked spec
        # would find no file to run — writing it lets the heal loop attempt an
        # unblock. Harmless for already-runnable specs (their file matches).
        (spec_dir / relative).parent.mkdir(parents=True, exist_ok=True)
        (spec_dir / relative).write_text(spec.code or "", encoding="utf-8")

        # A heal pass is now underway — reflect it on the spec lifecycle.
        spec.status = "running"
        db.commit()

        def emit(phase: str, attempt: int, message: str, error: str = "") -> None:
            if case_id in _healing:
                _healing[case_id]["attempt"] = attempt
            hub.publish(
                run_id_str,
                "heal.progress",
                {
                    "caseId": case_id,
                    "ticket": case.ticket_external_id,
                    "caseCode": case.code,
                    "attempt": attempt,
                    "maxAttempts": max_attempts,
                    "phase": phase,
                    "message": message,
                    "error": (error or "")[:600],
                },
            )

        final_status = "fail"
        final_error = ""
        attachments: list[dict] = []
        elapsed_ms = 0
        attempts_log: list[dict[str, Any]] = []  # per-attempt trail for the heal report
        # (before, after) code of the most recently accepted fix — used for
        # heal->KB selector feedback (#182) if that fix goes on to pass.
        last_fix_pair: tuple[str, str] | None = None

        for attempt in range(1, max_attempts + 1):
            # A run cancel should stop the heal loop rather than burn further
            # attempts (and their Claude fixer/classifier calls) — bail before
            # spawning the next Playwright/Claude process.
            if run_control.is_cancelled(run.id, db):
                logger.info("Run {} cancelled — stopping self-heal for case {}", run.code, case_id)
                return
            emit("running", attempt, f"Running spec (attempt {attempt}/{max_attempts})")
            # Heal re-runs fail fast (shorter timeouts) and skip heavy trace/video
            # + raw-DOM capture except on the final attempt (#398).
            is_final_attempt = attempt == max_attempts
            _write_config(
                spec_dir, 1, headless, base_url, storage_state,
                test_timeout_ms=settings.heal_test_timeout_ms,
                action_timeout_ms=settings.heal_action_timeout_ms,
                heavy_evidence=is_final_attempt,
                capture_video=bool(settings_store.load_settings().get("video", False)),
            )
            _apply_fixtures(spec_dir, session_file, replay_session, capture_raw=is_final_attempt)

            report: dict[str, Any] = {}
            run_error: str | None = None
            proc_output = ""
            started = time.monotonic()
            try:
                _rc, stdout, stderr = _invoke_playwright(
                    spec_dir, 1, settings.exec_timeout_s, spec_file=filename, run_id=run.id
                )
                proc_output = "\n".join(p for p in (stdout, stderr) if p).strip()
            except FileNotFoundError as exc:
                run_error = f"Playwright binary not found: {exc}"
            except subprocess.TimeoutExpired:
                run_error = f"Playwright run timed out after {settings.exec_timeout_s}s"
            finally:
                elapsed_ms = int((time.monotonic() - started) * 1000)

            report_path = spec_dir / "report.json"
            if run_error is None:
                if report_path.exists():
                    try:
                        report = json.loads(report_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError as exc:
                        run_error = f"Could not parse Playwright report: {exc}"
                else:
                    detail = f" — {proc_output[:600]}" if proc_output else ""
                    run_error = f"Playwright produced no report.json{detail}"

            parsed = parse_playwright_report(report) if report else []
            entry = next(
                (e for e in parsed if Path(e["file"]).name == filename), None
            )

            final_output = proc_output or run_error or ""
            rec: dict[str, Any] = {
                "attempt": attempt,
                "durationMs": elapsed_ms,
                "outputTail": (final_output or "")[-1500:],
                "fixed": False,
                "diff": "",
            }

            if entry and entry["status"] == "pass":
                final_status = "pass"
                final_error = ""
                attachments = entry["attachments"]
                elapsed_ms = entry["duration_ms"] or elapsed_ms
                rec["status"] = "pass"
                rec["error"] = ""
                attempts_log.append(rec)
                emit("passed", attempt, "Spec passed")
                break

            if entry:
                final_status = "fail"
                final_error = entry["error_message"] or run_error or "Test failed"
                attachments = entry["attachments"]
                elapsed_ms = entry["duration_ms"] or elapsed_ms
            else:
                final_status = "fail"
                final_error = run_error or "No result reported by Playwright"
                attachments = []
            rec["status"] = "fail"
            rec["error"] = final_error

            # Classify the failure's root cause BEFORE attempting any regenerate.
            # A product defect means the APP is wrong, not the test — we must not
            # "heal" it by editing the spec (that would just weaken the check);
            # instead mark the spec terminal and route it to the report /
            # ticket-comment stage. product_defect cases never reach the fixer, and
            # must stay EXCLUDED from any future heal/learning loop.
            classification = failure_classifier.classify_failure(
                case, spec.code, final_error, final_output, context
            )
            _set_latest_failure_class(db, case, classification["failureClass"])
            rec["failureClass"] = classification["failureClass"]
            rec["classificationReason"] = classification.get("reason", "")
            if classification["suspectedProductDefect"] or (
                classification["failureClass"] == "product_defect"
            ):
                spec.status = "product_defect"  # TERMINAL — assertion kept intact
                db.commit()
                rec["productDefect"] = True
                attempts_log.append(rec)
                emit(
                    "product_defect",
                    attempt,
                    "Product defect suspected — routing to report, not the fixer",
                    error=final_error,
                )
                break

            if attempt >= max_attempts:
                attempts_log.append(rec)
                emit("failed", attempt, "Still failing after max attempts", error=final_error)
                break

            emit("fixing", attempt, "Asking Claude to fix the spec", error=final_error)
            # Ground the fix on the real page: the distilled DOM captured for this
            # failing attempt gives Claude actual selectors, which is the only
            # grounding a blocked spec (empty KB) has to work with.
            dom_snapshot = _load_distilled_dom(attachments)
            try:
                fixed = spec_service.generate_fixed_spec_code(
                    case, spec.code, final_error, final_output, context, examples, dom_snapshot
                )
            except ClaudeError as exc:
                final_error = f"Heal generation failed: {exc}"
                rec["error"] = final_error
                attempts_log.append(rec)
                emit("failed", attempt, final_error, error=final_error)
                break

            # Anti-cheat: a fix that removes/weakens assertions is NEVER valid — it
            # only "passes" by checking less. Reject it, keep the previous good spec
            # (do not overwrite code/path/file), and stop (mark failed).
            previous_code = spec.code or ""
            if placeholder_gate.count_assertions(fixed) < placeholder_gate.count_assertions(
                previous_code
            ):
                final_status = "fail"
                final_error = "Rejected fix: it removed/weakened assertions (anti-cheat)."
                rec["error"] = final_error
                rec["rejected"] = "assertion-weakening"
                attempts_log.append(rec)
                emit("failed", attempt, "Rejected fix that weakened assertions", error=final_error)
                break

            # Placeholder / invented-reference gate on the regenerated code.
            # Bypassed when the global quality gate is off (#gate-toggle); the
            # assertion-weakening anti-cheat above still applies.
            gate = (
                placeholder_gate.gate_spec(fixed, known)
                if settings_store.gate_enabled()
                else placeholder_gate.bypassed_result()
            )
            if gate["outcome"] == "blocked":
                # Missing-input: keep the previous spec, mark the spec blocked.
                spec.status = "blocked"
                spec.block_reason = f'{gate["reason"]} {gate["unblock_action"]}'.strip()
                spec.gate_report = json.dumps(gate)
                db.commit()
                final_status = "fail"
                final_error = spec.block_reason
                rec["error"] = final_error
                rec["gate"] = "blocked"
                attempts_log.append(rec)
                emit("failed", attempt, "Fix blocked by placeholder gate", error=final_error)
                break
            if gate["outcome"] == "rejected":
                # Keep the previous good spec; record the rejection in gate_report.
                spec.gate_report = json.dumps(gate)
                db.commit()
                final_status = "fail"
                final_error = gate["reason"]
                rec["error"] = final_error
                rec["gate"] = "rejected"
                attempts_log.append(rec)
                emit("failed", attempt, "Fix rejected by placeholder gate", error=final_error)
                break

            # Accepted fix — record what Claude changed (unified diff), write it.
            diff = "\n".join(
                difflib.unified_diff(
                    (spec.code or "").splitlines(),
                    (fixed or "").splitlines(),
                    fromfile=f"spec (before fix {attempt})",
                    tofile=f"spec (after fix {attempt})",
                    lineterm="",
                )
            )
            rec["fixed"] = True
            rec["diff"] = diff
            attempts_log.append(rec)
            last_fix_pair = (previous_code, fixed)

            spec.path = _persist_spec_code(db, run, case, spec, fixed, spec_dir)
            spec.code = fixed
            db.commit()

        # Reflect the heal outcome on the spec lifecycle. Terminal states set
        # inside the loop (product_defect / blocked) win and are left intact; every
        # other exit maps to passed / failed (attempts exhausted, anti-cheat or
        # gate rejection all mean the heal did not produce a good spec).
        if spec.status not in ("product_defect", "blocked"):
            spec.status = "passed" if final_status == "pass" else "failed"
            db.commit()

        # Heal->KB feedback (#182): the fix that led to a pass corrected exactly
        # one selector -> propose it back to the project KB, best-effort.
        if final_status == "pass" and last_fix_pair is not None:
            _propose_healed_selector_to_kb(
                project_key, heal_repo, last_fix_pair[0], last_fix_pair[1], run.owner_id
            )
        # Heal->KB DOM enrichment (#249): a DOM-grounded pass discovered a real
        # route + selectors -> ADD any the KB doesn't know yet. Runs on ANY pass
        # (even attempt 1 with no fix), since enriching an empty KB is exactly what
        # unblocks the blocked class of specs. Composes with the 1->1 swap above.
        if final_status == "pass":
            _merge_discovered_dom_to_kb(
                project_key, heal_repo, spec.code or "", _load_distilled_dom(attachments), run.owner_id
            )

        # Persist the heal trail on the spec so the UI can show the full process.
        try:
            spec.heal_report = json.dumps(
                {
                    "caseId": case_id,
                    "finalStatus": final_status,
                    "maxAttempts": max_attempts,
                    "healedAt": datetime.now(timezone.utc).isoformat(),
                    "attempts": attempts_log,
                }
            )
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.warning("Failed to persist heal report for case {}: {}", case_id, exc)

        _apply_heal_result(
            db, run, case, final_status, final_error, elapsed_ms, attachments
        )
    except Exception as exc:  # noqa: BLE001 - surface, never crash the thread
        logger.error("Self-heal failed for case {}: {}", case_id, exc)
    finally:
        _healing.pop(case_id, None)
        db.close()
        run_context.clear()
