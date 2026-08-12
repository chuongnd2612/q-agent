"""Live spec-authoring (#400).

Instead of generating a Playwright spec blind from the Knowledge Base and healing
the failures afterwards, this drives the *real* application first: it launches a
dedicated, already-authenticated Chrome, points the ``browser-harness`` CLI at it,
and lets an agentic Claude (see :func:`claude_cli.run_agentic`) perform the test
case's steps live — discovering the real selectors on the real DOM, creating any
missing test data — then write a clean ``*.spec.ts`` built from what actually
worked (layered on ``@q-agent/playwright-base`` per #542, not self-contained:
see ``skills/live-authoring/SKILL.md``), plus a ``discovered.json`` sidecar of runtime-verified
routes/selectors for the KB.

This runs server-side (the API host has ``browser-harness`` + a Chrome), mirroring
the server-branch exploration thread model — no paired-device bridge. It is bounded
on three axes so an autonomous tool-using run can't misbehave: a per-session Claude
cost ceiling, a max agentic-turn cap, and a wall-clock timeout (see
:mod:`app.config` ``authoring_*``). See ADR 0012.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings
from app.logging import logger
from app.services import (
    ai_usage_service,
    audit_service,
    claude_cli,
    project_config_service,
    settings_store,
    spec_service,
)
from app.services.exploration_agent import normalize_discovered
from app.services.knowledge_service import merge_verified_discovery

_LAUNCHER = Path(__file__).resolve().parent / "pw_scripts" / "authoring_browser.cjs"

# How long to wait for the launched Chrome's CDP endpoint to come up.
_CDP_READY_TIMEOUT_S = 25.0


class LiveAuthoringError(RuntimeError):
    """Raised when live authoring cannot start (missing tool/profile/base URL)."""


@dataclass
class AuthoringResult:
    """Outcome of authoring one case.

    Attributes:
        ok: True when a non-empty spec was emitted.
        code: The emitted Playwright spec source (``""`` if none).
        discovered: Runtime-verified ``{"routes": [...], "selectors": [...]}``
            already normalized for :func:`merge_verified_discovery`.
        summary: Claude's final plain-text summary of the live run.
        project_key: Resolved project key (for the KB merge), or None.
        repo: Resolved target repo name (``""`` = project default).
        owner_id: Owning user id (for owner-scoped KB/spec writes).
    """

    ok: bool
    code: str
    discovered: dict = field(default_factory=lambda: {"routes": [], "selectors": []})
    summary: str = ""
    project_key: str | None = None
    repo: str = ""
    owner_id: int | None = None


def _free_port() -> int:
    """Pick a free localhost TCP port for the dedicated Chrome's CDP endpoint."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_cdp(port: int, timeout_s: float) -> bool:
    """Poll the Chrome DevTools ``/json/version`` endpoint until it responds."""
    deadline = time.monotonic() + timeout_s
    url = f"http://127.0.0.1:{port}/json/version"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:  # noqa: S310 - localhost only
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - endpoint not up yet
            time.sleep(0.3)
    return False


def _launch_browser(base_url: str, port: int, profile_dir: Path) -> subprocess.Popen[str]:
    """Start the long-lived pre-authenticated Chrome launcher (Node subprocess).

    The launcher (``authoring_browser.cjs``) uses only Node built-ins — no
    Playwright/node_modules — so it runs from its own dir and needs no NODE_PATH
    (the API image ships chromium + node but no Playwright). It finds Chrome via
    ``QAGENT_CHROME_BIN`` (set in the image) or platform defaults. stdin is a
    pipe: closing it (in :func:`_teardown`) tells the launcher to kill Chrome —
    cross-platform cleanup that works on Windows where ``terminate()`` won't run
    signal handlers.
    """
    cmd = ["node", str(_LAUNCHER), base_url, str(port), str(profile_dir)]
    logger.info("Live authoring: launching browser {}", " ".join(cmd))
    return subprocess.Popen(  # noqa: S603
        cmd,
        cwd=str(_LAUNCHER.parent),
        env=os.environ.copy(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


def _teardown(proc: subprocess.Popen[str] | None) -> None:
    """Stop the launcher (and thus Chrome). Best-effort, never raises."""
    if proc is None:
        return
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()  # triggers the launcher's stdin-end → kills Chrome
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _steps_lines(case) -> str:
    """Render a case's ``steps`` (``[{a, e}]``) as numbered action → expected lines."""
    lines = []
    for i, step in enumerate(case.steps or [], start=1):
        action = (step.get("a") or "").strip()
        expected = (step.get("e") or "").strip()
        lines.append(f"{i}. {action}" + (f"  → expected: {expected}" if expected else ""))
    return "\n".join(lines) or "(no steps recorded)"


def _test_data_lines(case) -> str:
    data = case.test_data or []
    if not data:
        return "(none provided)"
    return "\n".join(f"- {d.get('field', '')}: {d.get('value', '')}" for d in data)


def _build_prompt(
    case,
    context: dict,
    spec_filename: str,
    sidecar: str,
    base_url: str,
    heal: dict | None = None,
    plan: dict | None = None,
) -> str:
    """Build the live task prompt (the skill supplies the methodology).

    When ``heal`` is given (``{"code": <failing spec>, "error": <failure>}``) this
    frames the task as a self-heal (#428): reproduce the failure live, find the
    real cause, and emit a CORRECTED spec — instead of authoring from scratch.
    Reuses the same browser-harness/skill machinery either way.

    When ``plan`` is given (#569) the ticket's Automation Plan is rendered into the
    prompt by the SAME :func:`automation_planner_service.render_plan` the blind path
    uses, so a live-authored spec imports the project's existing page objects
    instead of re-inlining their locators. The plan's ``create``/``extend`` assets
    have already been authored **server-side** before this prompt is built, so every
    path the block lists as importable really is on disk. ``skills/live-authoring/
    SKILL.md`` documents how to consume the block — change one and change the other
    in the same commit (#178).
    """
    # Local import: `automation_planner_service` pulls in the project services, and
    # importing it at module scope makes live_authoring <-> planner a cycle.
    from app.services.automation_planner_service import render_plan

    plan_block = render_plan(plan)
    accounts = context.get("testAccounts") or []
    cred_lines = "\n".join(
        f"- role={a.get('role', '')} username={a.get('username', '')} password={a.get('password', '')}"
        for a in accounts
    ) or "(no test accounts in context — reuse the browser's existing session)"
    routes = json.dumps(context.get("routes", []), ensure_ascii=False)[:4000]
    selectors = json.dumps(context.get("selectors", []), ensure_ascii=False)[:4000]
    auth = json.dumps(context.get("auth", {}), ensure_ascii=False)[:1500]
    if heal is not None:
        failing_code = (heal.get("code") or "").strip()[:8000]
        error = (heal.get("error") or "").strip()[:3000] or "(no error text captured)"
        intro = (
            f"A Playwright spec for this test case FAILED. Repair it by driving the REAL app live "
            f"with browser-harness (already wired to a signed-in Chrome via BU_CDP_URL — just run it): "
            f"reproduce the failing step, find the REAL cause (wrong/stale selector, changed flow, "
            f"missing test data, timing), and emit a CORRECTED spec built from what "
            f"actually works on the live DOM. Keep the test intent + assertions, and keep the spec's "
            f"existing imports/layering (never re-inline a page object or a login flow); do NOT weaken them "
            f"to make it pass.\n\n"
            f"## Failing spec (repair this)\n```ts\n{failing_code}\n```\n\n"
            f"## Failure\n{error}\n\n"
        )
    else:
        intro = (
            f"Author a Playwright spec for this test case by driving the REAL app live with "
            f"browser-harness (it is already wired to a signed-in Chrome via BU_CDP_URL — just run it).\n\n"
        )
    return (
        f"{intro}"
        f"## Test case\n"
        f"Ticket: {case.ticket_external_id}\n"
        f"Test Case ID: {case.code}\n"
        f"Title: {case.title}\n"
        f"Precondition: {case.precondition or '(none)'}\n"
        f"Steps (action → expected):\n{_steps_lines(case)}\n\n"
        f"Test data:\n{_test_data_lines(case)}\n\n"
        f"## Project context (real values — bake these in)\n"
        f"Base URL: {base_url}\n"
        f"Test accounts:\n{cred_lines}\n"
        f"Auth: {auth}\n"
        f"Known routes: {routes}\n"
        f"Known selectors: {selectors}\n\n"
        + (f"## Shared library\n{plan_block}\n\n" if plan_block else "")
        + f"## Deliverables — write BOTH files into the current working directory\n"
        f"1. `{spec_filename}` — the Playwright spec (layered contract in the skill: import from "
        f"`@q-agent/playwright-base`, no inline login"
        + (
            ", and drive the UI through the IMPORTABLE assets above instead of "
            "re-inlining their locators"
            if plan_block
            else ""
        )
        + ").\n"
        f"2. `{sidecar}` — the runtime-verified routes/selectors sidecar (shape in the skill).\n\n"
        f"Perform every step live and confirm every expected result before writing the spec. "
        f"Create any missing test data through the UI first and bake it into the spec. "
        f"Use ONLY selectors you verified on the live DOM."
    )


def author_case(
    db, case, run, *, owner_id: int | None, run_id: int | None, plan: dict | None = None
) -> AuthoringResult:
    """Author one case's spec by driving the real app live via browser-harness (#400).

    ``plan`` (#569) is the ticket's Automation Plan, whose ``create``/``extend``
    assets the caller has **already authored server-side** — it is rendered into the
    task prompt so the live session reuses the project's page objects instead of
    inlining locators. ``None`` (legacy path, no persistent project, planning
    failed) keeps the pre-#569 prompt exactly.

    Returns an :class:`AuthoringResult` with the emitted spec code and the
    runtime-verified discovery (already normalized for the KB). Raises
    :class:`LiveAuthoringError` on a hard precondition failure (no browser-harness,
    no base URL, no authenticated profile). Progress is streamed as
    ``authoring.progress`` on the run WebSocket. The launched Chrome is always torn
    down in ``finally``.
    """
    if not claude_cli.browser_harness_available():
        raise LiveAuthoringError(
            "browser-harness CLI not found on the API host. Install it "
            "(`uv tool install browser-harness`) to use live-authoring mode."
        )

    context = spec_service.build_case_context(db, case, env=run.env)
    base_url = (context.get("baseUrl") or "").strip()
    if not base_url:
        raise LiveAuthoringError(
            "No base URL in the project context — configure the project's base URL first."
        )
    project_key = context.get("projectKey")
    repo = context.get("repo", "") or ""

    # Reuse the persistent, already-authenticated capture profile (non-default →
    # no Chrome "Allow remote debugging" popup). Live authoring requires it.
    profile_dir = project_config_service.auth_path(project_key or "", owner_id).parent / "browser-profile"
    if not profile_dir.is_dir() or not any(profile_dir.iterdir()):
        raise LiveAuthoringError(
            "No authenticated browser profile for this project. Complete a manual "
            "login capture first so live-authoring can reuse the signed-in session."
        )

    # Per-session cost ceiling: don't start if the run has already spent the budget.
    budget = settings_store.authoring_cost_budget_usd()
    if run_id is not None:
        try:
            spent = float(ai_usage_service.run_breakdown(db, run_id).get("totalCostUsd") or 0.0)
            if spent >= budget:
                raise LiveAuthoringError(
                    f"Authoring cost budget (${budget:.2f}) "
                    f"already reached for this run (${spent:.2f} spent)."
                )
        except LiveAuthoringError:
            raise
        except Exception as exc:  # noqa: BLE001 - budget read is best-effort
            logger.warning("Authoring budget check skipped: {}", exc)

    workspace = settings.workspace_dir / "authoring" / run.code / (case.code or str(case.id))
    workspace.mkdir(parents=True, exist_ok=True)
    spec_filename = spec_service.spec_filename(case.ticket_external_id, case.code)
    spec_path = workspace / spec_filename
    sidecar_path = workspace / "discovered.json"
    for stale in (spec_path, sidecar_path):
        try:
            stale.unlink()
        except FileNotFoundError:
            pass

    def _publish(phase: str, **payload) -> None:
        if run_id is None:
            return
        try:
            from app.ws import hub

            # Key by the numeric test-case id (what the UI matches on), not case.code.
            hub.publish(str(run_id), "authoring.progress", {"case": case.id, "phase": phase, **payload})
        except Exception as exc:  # noqa: BLE001 - progress is best-effort
            logger.warning("authoring.progress publish skipped: {}", exc)

    port = _free_port()
    proc: subprocess.Popen[str] | None = None
    try:
        _publish("launching", message="Starting authenticated browser")
        proc = _launch_browser(base_url, port, profile_dir)
        if not _wait_cdp(port, _CDP_READY_TIMEOUT_S):
            raise LiveAuthoringError(f"Chrome CDP endpoint did not come up on port {port}.")
        _publish("driving", message="Driving the app live with browser-harness")

        prompt = _build_prompt(
            case, context, spec_filename, sidecar_path.name, base_url, plan=plan
        )
        summary = claude_cli.run_agentic(
            prompt,
            workspace_dir=workspace,
            skill="live-authoring",
            include_template=True,
            label=f"Live authoring: {case.ticket_external_id} · {case.code}",
            extra_env={"BU_CDP_URL": f"http://127.0.0.1:{port}"},
            max_budget_usd=budget,
        )
    finally:
        _teardown(proc)

    code = ""
    if spec_path.exists():
        code = spec_service._extract_code(spec_path.read_text(encoding="utf-8"))
    discovered = {"routes": [], "selectors": []}
    if sidecar_path.exists():
        try:
            raw = json.loads(sidecar_path.read_text(encoding="utf-8"))
            discovered = normalize_discovered(raw, screen=case.title or "")
        except Exception as exc:  # noqa: BLE001 - a bad sidecar must not fail authoring
            logger.warning("Live authoring: could not parse discovered.json: {}", exc)

    ok = bool(code.strip())
    _publish("done" if ok else "failed", message=summary[:400])
    audit_service.record(
        category="automation",
        actor_type="ai",
        action="Authored spec live" if ok else "Live authoring produced no spec",
        target=f"{case.ticket_external_id} · {case.code}",
        status="ok" if ok else "error",
        meta=summary[:800],
        run_code=run.code,
    )
    return AuthoringResult(
        ok=ok,
        code=code,
        discovered=discovered,
        summary=summary,
        project_key=project_key,
        repo=repo,
        owner_id=owner_id,
    )


def merge_discovery_to_kb(result: AuthoringResult) -> int:
    """Merge a completed authoring run's runtime-verified discovery into the KB.

    Thin wrapper over :func:`merge_verified_discovery` stamping ``source`` as
    ``"live-authoring"``. Returns the count merged (0 on nothing / no project key).
    """
    if not result.project_key or not result.discovered:
        return 0
    try:
        return merge_verified_discovery(
            result.project_key,
            result.repo,
            result.discovered,
            owner_id=result.owner_id,
            source="live-authoring",
        )
    except Exception as exc:  # noqa: BLE001 - KB enrichment is additive/best-effort
        logger.warning("Live authoring KB merge skipped: {}", exc)
        return 0
