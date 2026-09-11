"""Tests for the execution router + playwright_runner.

The real Playwright subprocess is monkeypatched out (``_invoke_playwright``)
so no browser is required; ``parse_playwright_report`` — the pure mapping
function — is exercised directly and via a monkeypatched runner.
"""

from __future__ import annotations

import time

from app.services.playwright_runner import parse_playwright_report

SAMPLE_REPORT = {
    "suites": [
        {
            "title": "1428-TC-01.spec.ts",
            "file": "1428-TC-01.spec.ts",
            "specs": [
                {
                    "title": "Login works",
                    "ok": True,
                    "file": "1428-TC-01.spec.ts",
                    "tests": [
                        {
                            "status": "expected",
                            "results": [
                                {
                                    "status": "passed",
                                    "duration": 123,
                                    "errors": [],
                                    "attachments": [],
                                }
                            ],
                        }
                    ],
                }
            ],
            "suites": [],
        },
        {
            "title": "1428-TC-02.spec.ts",
            "file": "1428-TC-02.spec.ts",
            "specs": [
                {
                    "title": "Logout works",
                    "ok": False,
                    "file": "1428-TC-02.spec.ts",
                    "tests": [
                        {
                            "status": "unexpected",
                            "results": [
                                {
                                    "status": "failed",
                                    "duration": 456,
                                    "error": {"message": "Timed out waiting for selector"},
                                    "attachments": [
                                        {
                                            "name": "screenshot",
                                            "contentType": "image/png",
                                            "path": "/tmp/test-results/foo/test-failed-1.png",
                                        },
                                        {
                                            "name": "trace",
                                            "contentType": "application/zip",
                                            "path": "/tmp/test-results/foo/trace.zip",
                                        },
                                        {
                                            "name": "error-context",
                                            "contentType": "text/markdown",
                                            "path": "/tmp/test-results/foo/error-context.md",
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
            "suites": [],
        },
    ]
}


def test_parse_playwright_report_maps_pass_and_fail():
    results = parse_playwright_report(SAMPLE_REPORT)
    assert len(results) == 2

    passed = next(r for r in results if r["file"] == "1428-TC-01.spec.ts")
    assert passed["status"] == "pass"
    assert passed["duration_ms"] == 123
    assert passed["error_message"] == ""
    assert passed["attachments"] == []

    failed = next(r for r in results if r["file"] == "1428-TC-02.spec.ts")
    assert failed["status"] == "fail"
    assert failed["duration_ms"] == 456
    assert failed["error_message"] == "Timed out waiting for selector"
    # error-context.md is not a known evidence kind and should be dropped.
    kinds = {a["kind"] for a in failed["attachments"]}
    assert kinds == {"screenshot", "trace"}


def test_parse_playwright_report_maps_dom_attachments():
    """Captured DOM attachments map to the 'dom' / 'dom-distilled' evidence kinds."""
    report = {
        "suites": [
            {
                "title": "x.spec.ts",
                "file": "x.spec.ts",
                "specs": [
                    {
                        "title": "captures dom",
                        "file": "x.spec.ts",
                        "tests": [
                            {
                                "status": "expected",
                                "results": [
                                    {
                                        "status": "passed",
                                        "duration": 5,
                                        "attachments": [
                                            {"name": "qagent-dom-raw", "path": "/t/dom.html"},
                                            {"name": "qagent-dom-distilled", "path": "/t/dom.json"},
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
                "suites": [],
            }
        ]
    }
    results = parse_playwright_report(report)
    assert len(results) == 1
    kinds = {a["kind"]: a["path"] for a in results[0]["attachments"]}
    assert kinds == {"dom": "/t/dom.html", "dom-distilled": "/t/dom.json"}


def test_parse_playwright_report_empty_suites_returns_empty_list():
    assert parse_playwright_report({"suites": []}) == []


def test_parse_playwright_report_uses_last_retry_result():
    report = {
        "suites": [
            {
                "title": "x.spec.ts",
                "file": "x.spec.ts",
                "specs": [
                    {
                        "title": "flaky test",
                        "file": "x.spec.ts",
                        "tests": [
                            {
                                "status": "expected",
                                "results": [
                                    {"status": "failed", "duration": 10, "attachments": []},
                                    {"status": "passed", "duration": 20, "attachments": []},
                                ],
                            }
                        ],
                    }
                ],
                "suites": [],
            }
        ]
    }
    results = parse_playwright_report(report)
    assert len(results) == 1
    assert results[0]["status"] == "pass"
    assert results[0]["duration_ms"] == 20


def _seed_run_with_approved_case(db_session):
    from app.models.run import Run
    from app.models.testcase import TestCase

    run = Run(code="RUN-1", name="Test run", status="automation", workers=2)
    db_session.add(run)
    db_session.flush()

    case = TestCase(
        run_id=run.id,
        ticket_external_id="SUR-1428",
        code="TC-01",
        title="Login works",
        approval="approved",
        automation="Playwright",
    )
    db_session.add(case)
    db_session.commit()
    db_session.refresh(run)
    db_session.refresh(case)
    return run, case


def test_start_execution_creates_execution_and_pending_results(client, db_session, monkeypatch):
    import app.services.playwright_runner as runner_module

    # Prevent the background thread from doing real subprocess/file work; we
    # only assert on what POST /runs/{id}/execution creates synchronously.
    monkeypatch.setattr(runner_module, "run_execution", lambda execution_id: None)

    run, case = _seed_run_with_approved_case(db_session)

    resp = client.post(f"/runs/{run.id}/execution", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["runId"] == run.id
    assert body["status"] == "running"
    assert body["total"] == 1
    assert len(body["results"]) == 1
    assert body["results"][0]["testCaseId"] == case.id
    assert body["results"][0]["status"] == "pending"

    from app.db import SessionLocal
    from app.models.run import Run

    db = SessionLocal()
    try:
        refreshed_run = db.get(Run, run.id)
        assert refreshed_run.status == "executing"
    finally:
        db.close()


def test_start_execution_no_eligible_cases_returns_400(client, db_session):
    from app.models.run import Run

    run = Run(code="RUN-2", name="Empty run", status="review")
    db_session.add(run)
    db_session.commit()

    resp = client.post(f"/runs/{run.id}/execution", json={})
    assert resp.status_code == 400


def test_start_execution_missing_run_returns_404(client):
    resp = client.post("/runs/9999/execution", json={})
    assert resp.status_code == 404


def test_get_latest_execution_and_by_id(client, db_session, monkeypatch):
    import app.services.playwright_runner as runner_module

    monkeypatch.setattr(runner_module, "run_execution", lambda execution_id: None)

    run, _case = _seed_run_with_approved_case(db_session)
    created = client.post(f"/runs/{run.id}/execution", json={}).json()

    latest = client.get(f"/runs/{run.id}/execution")
    assert latest.status_code == 200
    assert latest.json()["id"] == created["id"]

    by_id = client.get(f"/executions/{created['id']}")
    assert by_id.status_code == 200
    assert by_id.json()["id"] == created["id"]

    assert client.get("/executions/9999").status_code == 404
    assert client.get("/runs/9999/execution").status_code == 404


def test_run_execution_end_to_end_with_mocked_subprocess(client, db_session, monkeypatch, tmp_path):
    """Full run_execution() pass: mock _invoke_playwright + write a canned report.json."""
    import app.services.playwright_runner as runner_module

    run, case = _seed_run_with_approved_case(db_session)

    from app.services.workspace_scope import scoped_specs_dir

    spec_dir = scoped_specs_dir(run.owner_id) / run.code
    spec_dir.mkdir(parents=True, exist_ok=True)

    # A screenshot file the parser's evidence-copy step can actually find.
    fake_screenshot = tmp_path / "test-failed-1.png"
    fake_screenshot.write_bytes(b"fake-png-bytes")

    report = {
        "suites": [
            {
                "title": "1428-TC-01.spec.ts",
                "file": "1428-TC-01.spec.ts",
                "specs": [
                    {
                        "title": "Login works",
                        "file": "1428-TC-01.spec.ts",
                        "tests": [
                            {
                                "results": [
                                    {
                                        "status": "passed",
                                        "duration": 99,
                                        "attachments": [
                                            {
                                                "name": "screenshot",
                                                "contentType": "image/png",
                                                "path": str(fake_screenshot),
                                            }
                                        ],
                                    }
                                ]
                            }
                        ],
                    }
                ],
                "suites": [],
            }
        ]
    }

    fake_stdout = "Running 1 test using 1 worker\n  1 passed (99ms)"
    fake_stderr = "warning: some deprecation notice"

    def fake_invoke(spec_dir_arg, workers, timeout_s, spec_file="", **_kwargs):
        import json as _json

        (spec_dir_arg / "report.json").write_text(_json.dumps(report), encoding="utf-8")
        return 0, fake_stdout, fake_stderr

    monkeypatch.setattr(runner_module, "_invoke_playwright", fake_invoke)

    resp = client.post(f"/runs/{run.id}/execution", json={})
    assert resp.status_code == 200
    execution_id = resp.json()["id"]

    for _ in range(50):
        time.sleep(0.05)
        current = client.get(f"/executions/{execution_id}").json()
        if current["status"] == "done":
            break
    else:
        current = client.get(f"/executions/{execution_id}").json()

    assert current["status"] == "done"
    assert current["passed"] == 1
    assert current["failed"] == 0
    result = current["results"][0]
    assert result["status"] == "pass"
    assert result["durationMs"] == 99
    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["kind"] == "screenshot"

    # The captured Playwright stdout/stderr is exposed as the run log.
    assert fake_stdout in current["log"]
    assert fake_stderr in current["log"]

    # ...and it's also returned by GET /runs/{id}/execution.
    latest = client.get(f"/runs/{run.id}/execution").json()
    assert fake_stdout in latest["log"]

    from app.db import SessionLocal
    from app.models.execution import Execution
    from app.models.run import Run

    db = SessionLocal()
    try:
        refreshed_run = db.get(Run, run.id)
        assert refreshed_run.status == "evidence"
        refreshed_exec = db.get(Execution, execution_id)
        assert fake_stdout in refreshed_exec.log
        assert fake_stderr in refreshed_exec.log
    finally:
        db.close()


def test_run_single_spec_runs_only_that_spec(client, db_session, monkeypatch):
    """POST /cases/{id}/spec/run creates a 1-case execution and runs only that spec."""
    import json as _json

    import app.services.playwright_runner as runner_module
    from app.services.workspace_scope import scoped_specs_dir

    run, case = _seed_run_with_approved_case(db_session)
    spec_dir = scoped_specs_dir(run.owner_id) / run.code
    spec_dir.mkdir(parents=True, exist_ok=True)

    captured = {}
    report = {
        "suites": [
            {
                "file": "1428-TC-01.spec.ts",
                "specs": [
                    {
                        "title": "Login works",
                        "file": "1428-TC-01.spec.ts",
                        "tests": [{"results": [{"status": "passed", "duration": 55, "attachments": []}]}],
                    }
                ],
                "suites": [],
            }
        ]
    }

    def fake_invoke(spec_dir_arg, workers, timeout_s, spec_file="", **_kwargs):
        captured["spec_file"] = spec_file
        (spec_dir_arg / "report.json").write_text(_json.dumps(report), encoding="utf-8")
        return 0, "1 passed", ""

    monkeypatch.setattr(runner_module, "_invoke_playwright", fake_invoke)

    resp = client.post(f"/cases/{case.id}/spec/run")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1 and len(body["results"]) == 1
    execution_id = body["id"]

    for _ in range(60):
        time.sleep(0.05)
        if client.get(f"/executions/{execution_id}").json()["status"] == "done":
            break
    current = client.get(f"/executions/{execution_id}").json()
    assert current["status"] == "done"
    assert current["passed"] == 1 and current["total"] == 1
    # Only the one case's spec file was targeted, not the whole suite. The
    # filename carries the full ticket id since #540 (spec_service.spec_filename);
    # this assertion had gone stale unnoticed because the test never got this far.
    assert captured["spec_file"] == "SUR-1428-TC-01.spec.ts"


def test_run_single_spec_rejects_non_automatable(client, db_session):
    from app.models.run import Run
    from app.models.testcase import TestCase

    run = Run(code="RUN-9", name="x", status="automation")
    db_session.add(run)
    db_session.flush()
    manual = TestCase(run_id=run.id, ticket_external_id="SUR-1", code="TC-01",
                      title="m", approval="approved", automation="Manual")
    db_session.add(manual)
    db_session.commit()

    assert client.post(f"/cases/{manual.id}/spec/run").status_code == 400
    assert client.post("/cases/999999/spec/run").status_code == 404


def test_write_config_reflects_headless_setting(tmp_path):
    """The generated playwright.config.ts honors the headless toggle and is rewritten."""
    from app.services import playwright_runner as runner

    runner._write_config(tmp_path, workers=4, headless=False)
    content = (tmp_path / "playwright.config.ts").read_text(encoding="utf-8")
    assert "headless: false" in content
    assert "workers: 4" in content

    # Rewritten (not pinned to the first run) so a toggle change takes effect.
    runner._write_config(tmp_path, workers=2, headless=True)
    content = (tmp_path / "playwright.config.ts").read_text(encoding="utf-8")
    assert "headless: true" in content
    assert "workers: 2" in content


def test_write_config_injects_base_url_and_storage_state(tmp_path):
    """baseURL/storageState appear only when provided; omitted otherwise."""
    from app.services import playwright_runner as runner

    # Neither provided → no baseURL/storageState keys.
    runner._write_config(tmp_path, workers=1, headless=True)
    content = (tmp_path / "playwright.config.ts").read_text(encoding="utf-8")
    assert "baseURL" not in content
    assert "storageState" not in content

    # Both provided → both injected (as JSON string literals).
    runner._write_config(
        tmp_path, workers=1, headless=True,
        base_url="https://app.test", storage_state="/abs/storageState.json",
    )
    content = (tmp_path / "playwright.config.ts").read_text(encoding="utf-8")
    assert 'baseURL: "https://app.test"' in content
    assert 'storageState: "/abs/storageState.json"' in content
    # Existing use settings are preserved. Screenshots are always captured now
    # (pass or fail) so passing cases still yield evidence (#456).
    assert "screenshot: 'on'" in content


def _seed_manual_auth_run(db_session):
    """Seed a run whose ticket resolves to a manual_auth project with a base URL."""
    from app.models.project_config import ProjectConfig
    from app.models.provider import Provider
    from app.models.ticket import Ticket

    db_session.add(
        Provider(kind="ado", name="ADO", connected=True,
                 config={"project": "Surency Platform"}, secrets={})
    )
    db_session.add(
        ProjectConfig(key="Surency Platform", name="Surency Platform",
                      base_url="https://app.test", manual_auth=True)
    )
    db_session.add(
        Ticket(external_id="SUR-1428", provider_kind="ado", title="Login")
    )
    db_session.commit()

    run, case = _seed_run_with_approved_case(db_session)
    from app.models.run import RunTicket

    db_session.add(RunTicket(run_id=run.id, ticket_external_id="SUR-1428", position=0))
    db_session.commit()
    return run, case


def test_run_execution_manual_auth_capture_success(client, db_session, monkeypatch):
    """A successful capture writes storageState into the config and the run proceeds."""
    import app.services.playwright_runner as runner_module
    from app.services.workspace_scope import scoped_specs_dir

    run, _case = _seed_manual_auth_run(db_session)

    captured = {}

    def fake_capture(base_url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text('{"cookies": []}', encoding="utf-8")
        captured["base_url"] = base_url
        return True

    def fake_invoke(spec_dir_arg, workers, timeout_s, spec_file="", **_kwargs):
        # A minimal valid (empty) report so the run finishes cleanly.
        (spec_dir_arg / "report.json").write_text('{"suites": []}', encoding="utf-8")
        return 0, "ok", ""

    monkeypatch.setattr(runner_module, "capture_storage_state", fake_capture)
    monkeypatch.setattr(runner_module, "_invoke_playwright", fake_invoke)

    resp = client.post(f"/runs/{run.id}/execution", json={})
    execution_id = resp.json()["id"]

    for _ in range(50):
        time.sleep(0.05)
        if client.get(f"/executions/{execution_id}").json()["status"] == "done":
            break

    assert captured["base_url"] == "https://app.test"
    config = (
        scoped_specs_dir(run.owner_id) / run.code / "playwright.config.ts"
    ).read_text(encoding="utf-8")
    assert "storageState" in config
    assert 'baseURL: "https://app.test"' in config


def test_run_execution_manual_auth_failure_fails_run_without_specs(client, db_session, monkeypatch):
    """A failed capture fails every result with the clear message and never runs specs."""
    import app.services.playwright_runner as runner_module

    run, _case = _seed_manual_auth_run(db_session)

    monkeypatch.setattr(runner_module, "capture_storage_state", lambda base_url, dest: False)

    def boom_invoke(*args, **kwargs):
        raise AssertionError("specs must not run when manual login fails")

    monkeypatch.setattr(runner_module, "_invoke_playwright", boom_invoke)

    resp = client.post(f"/runs/{run.id}/execution", json={})
    execution_id = resp.json()["id"]

    for _ in range(50):
        time.sleep(0.05)
        if client.get(f"/executions/{execution_id}").json()["status"] == "done":
            break

    current = client.get(f"/executions/{execution_id}").json()
    assert current["status"] == "done"
    assert current["failed"] == 1
    assert current["passed"] == 0
    assert "Manual login was not completed" in current["results"][0]["errorMessage"]


def test_fixtures_ts_contents(tmp_path):
    """The generated fixtures.ts always wires DOM capture; sessionStorage replay is gated."""
    from app.services import playwright_runner as runner

    session_file = tmp_path / "sessionStorage.json"
    import json as _json

    # DOM capture is always present; the session path is always embedded.
    replay = runner._fixtures_ts(session_file, replay_session=True)
    assert "export const test" in replay
    assert "testInfo.attach('qagent-dom-raw'" in replay
    assert "testInfo.attach('qagent-dom-distilled'" in replay
    assert _json.dumps(str(session_file)) in replay
    # replay_session=True injects the sessionStorage-replay init script.
    assert "addInitScript" in replay

    # replay_session=False keeps DOM capture but drops the init script.
    no_replay = runner._fixtures_ts(session_file, replay_session=False)
    assert "testInfo.attach('qagent-dom-distilled'" in no_replay
    assert "addInitScript" not in no_replay


def test_write_config_heal_mode_fails_fast_and_skips_heavy_evidence(tmp_path):
    """Heal re-runs get a shorter timeout + action timeouts and no video/trace (#398)."""
    from app.services import playwright_runner as runner

    runner._write_config(
        tmp_path, workers=1, headless=True, capture_video=True,
        test_timeout_ms=12000, action_timeout_ms=8000, heavy_evidence=False,
    )
    content = (tmp_path / "playwright.config.ts").read_text(encoding="utf-8")
    assert "timeout: 12000" in content
    assert "actionTimeout: 8000" in content
    assert "navigationTimeout: 8000" in content
    # Even with capture_video on, an intermediate heal attempt records nothing.
    assert "video: 'off'" in content and "trace: 'off'" in content
    # A normal run keeps the full timeout + failure-retained trace.
    runner._write_config(tmp_path, workers=1, headless=True)
    normal = (tmp_path / "playwright.config.ts").read_text(encoding="utf-8")
    assert "timeout: 30000" in normal
    assert "trace: 'retain-on-failure'" in normal
    assert "actionTimeout" not in normal


def test_write_config_video_honors_capture_setting(tmp_path):
    """Video follows the "Capture video" setting (#456): on = record every run, off = none."""
    from app.services import playwright_runner as runner

    # Setting ON → video 'on' (recorded regardless of pass/fail); screenshot always on.
    runner._write_config(tmp_path, workers=1, headless=True, capture_video=True)
    on = (tmp_path / "playwright.config.ts").read_text(encoding="utf-8")
    assert "video: 'on'" in on
    assert "screenshot: 'on'" in on
    assert "trace: 'retain-on-failure'" in on

    # Setting OFF (default) → no video; trace unchanged.
    runner._write_config(tmp_path, workers=1, headless=True)
    off = (tmp_path / "playwright.config.ts").read_text(encoding="utf-8")
    assert "video: 'off'" in off
    assert "trace: 'retain-on-failure'" in off


def test_fixtures_ts_captures_console_and_network(tmp_path):
    """The generated fixtures.ts captures console + network on every test (#456)."""
    from app.services import playwright_runner as runner

    fx = runner._fixtures_ts(tmp_path / "sessionStorage.json", replay_session=False)
    assert "testInfo.attach('qagent-network'" in fx
    assert "testInfo.attach('qagent-console'" in fx
    assert "page.on('response'" in fx
    assert "page.on('console'" in fx


def test_apply_log_capture_populates_columns(tmp_path):
    """console/network JSON captures land in the result columns, not media rows (#456)."""
    from types import SimpleNamespace

    from app.services import evidence_service

    assert evidence_service.is_log_capture("console") is True
    assert evidence_service.is_log_capture("network") is True
    assert evidence_service.is_log_capture("screenshot") is False

    result = SimpleNamespace(console_logs=[], network_logs=[])

    # From a file (server runner path).
    net_file = tmp_path / "qagent-network.json"
    net_file.write_text('[{"method":"GET","url":"/x","status":200,"durationMs":12}]', encoding="utf-8")
    assert evidence_service.apply_log_capture(result, "network", net_file) is True
    assert result.network_logs[0]["method"] == "GET"

    # From bytes (Local Agent upload path).
    assert evidence_service.apply_log_capture(result, "console", b'[{"level":"error","text":"boom"}]') is True
    assert result.console_logs[0]["level"] == "error"

    # Bad payload is best-effort: returns False, leaves the column untouched.
    result.console_logs = ["kept"]
    assert evidence_service.apply_log_capture(result, "console", b"not json") is False
    assert result.console_logs == ["kept"]


def test_fixtures_ts_capture_raw_toggle_and_robust_distill(tmp_path):
    """capture_raw=False drops the raw-HTML attach but keeps the (retried) distilled one (#398)."""
    from app.services import playwright_runner as runner

    session_file = tmp_path / "sessionStorage.json"
    with_raw = runner._fixtures_ts(session_file, replay_session=False, capture_raw=True)
    assert "testInfo.attach('qagent-dom-raw'" in with_raw

    no_raw = runner._fixtures_ts(session_file, replay_session=False, capture_raw=False)
    assert "testInfo.attach('qagent-dom-raw'" not in no_raw
    # Distilled capture is always present and now retries after a settle.
    assert "testInfo.attach('qagent-dom-distilled'" in no_raw
    assert "runDistill" in no_raw and "page.isClosed()" in no_raw


def test_apply_fixtures_always_injects(tmp_path):
    """_apply_fixtures always rewrites imports to './fixtures' + writes fixtures.ts."""
    from app.services import playwright_runner as runner

    spec = tmp_path / "1428-TC-01.spec.ts"
    original = (
        "import { test, expect } from '@playwright/test';\n"
        "test('x', async ({ page }) => { await page.goto('/'); });\n"
    )
    spec.write_text(original, encoding="utf-8")
    session_file = tmp_path / "sessionStorage.json"

    # Even without session replay, DOM capture means fixtures are injected.
    runner._apply_fixtures(tmp_path, session_file, replay_session=False)
    assert "'./fixtures'" in spec.read_text(encoding="utf-8")
    assert "'@playwright/test'" not in spec.read_text(encoding="utf-8")
    fixtures = (tmp_path / "fixtures.ts").read_text(encoding="utf-8")
    assert "qagent-dom-distilled" in fixtures
    assert "addInitScript" not in fixtures

    # With replay enabled, the init script is added; specs stay pointed at './fixtures'.
    runner._apply_fixtures(tmp_path, session_file, replay_session=True)
    assert "'./fixtures'" in spec.read_text(encoding="utf-8")
    assert "addInitScript" in (tmp_path / "fixtures.ts").read_text(encoding="utf-8")


def test_run_execution_manual_auth_applies_session_fixtures(client, db_session, monkeypatch):
    """Manual-auth run with a sessionStorage snapshot writes fixtures.ts + rewrites specs."""
    import app.services.playwright_runner as runner_module
    from app.services.workspace_scope import scoped_specs_dir

    run, _case = _seed_manual_auth_run(db_session)

    spec_dir = scoped_specs_dir(run.owner_id) / run.code
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec = spec_dir / "1428-TC-01.spec.ts"
    spec.write_text(
        "import { test, expect } from '@playwright/test';\n"
        "test('login', async ({ page }) => { await page.goto('/'); });\n",
        encoding="utf-8",
    )

    def fake_capture(base_url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text('{"cookies": []}', encoding="utf-8")
        (dest.parent / "sessionStorage.json").write_text(
            '{"https://app.test": {"msal.token": "abc"}}', encoding="utf-8"
        )
        return True

    def fake_invoke(spec_dir_arg, workers, timeout_s, spec_file="", **_kwargs):
        (spec_dir_arg / "report.json").write_text('{"suites": []}', encoding="utf-8")
        return 0, "ok", ""

    monkeypatch.setattr(runner_module, "capture_storage_state", fake_capture)
    monkeypatch.setattr(runner_module, "_invoke_playwright", fake_invoke)

    resp = client.post(f"/runs/{run.id}/execution", json={})
    execution_id = resp.json()["id"]

    for _ in range(50):
        time.sleep(0.05)
        if client.get(f"/executions/{execution_id}").json()["status"] == "done":
            break

    assert client.get(f"/executions/{execution_id}").json()["status"] == "done"
    assert (spec_dir / "fixtures.ts").exists()
    assert "'./fixtures'" in spec.read_text(encoding="utf-8")


def test_run_execution_non_auth_still_injects_dom_fixtures(client, db_session, monkeypatch):
    """A normal (non-auth) run still injects fixtures for DOM capture, without session replay."""
    import app.services.playwright_runner as runner_module
    from app.services.workspace_scope import scoped_specs_dir

    run, _case = _seed_run_with_approved_case(db_session)

    spec_dir = scoped_specs_dir(run.owner_id) / run.code
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec = spec_dir / "1428-TC-01.spec.ts"
    spec.write_text(
        "import { test, expect } from '@playwright/test';\n"
        "test('login', async ({ page }) => { await page.goto('/'); });\n",
        encoding="utf-8",
    )

    def fake_invoke(spec_dir_arg, workers, timeout_s, spec_file="", **_kwargs):
        (spec_dir_arg / "report.json").write_text('{"suites": []}', encoding="utf-8")
        return 0, "ok", ""

    monkeypatch.setattr(runner_module, "_invoke_playwright", fake_invoke)

    resp = client.post(f"/runs/{run.id}/execution", json={})
    execution_id = resp.json()["id"]

    for _ in range(50):
        time.sleep(0.05)
        if client.get(f"/executions/{execution_id}").json()["status"] == "done":
            break

    assert client.get(f"/executions/{execution_id}").json()["status"] == "done"
    # DOM capture: fixtures are always injected now; a non-auth run has no session replay.
    assert "'./fixtures'" in spec.read_text(encoding="utf-8")
    fixtures = (spec_dir / "fixtures.ts").read_text(encoding="utf-8")
    assert "qagent-dom-distilled" in fixtures
    assert "addInitScript" not in fixtures


# --- An Execution without a Run (#796, foundation for #795) -------------------


def test_channel_key_keeps_the_run_channel_name_for_a_run_scoped_execution():
    """The run channel name must stay exactly ``str(run_id)``.

    ``/ws/runs/{run_id}`` and every existing SPA subscriber listen on that
    literal string, so routing publishes through ``channel_key`` has to be a
    no-op for a run-scoped execution. Asserting the exact value (not just
    "contains the id") is the point: a prefix would silently orphan every
    existing subscriber while the code still looked correct.
    """
    from app.models.execution import Execution
    from app.services.execution_service import channel_key

    assert channel_key(Execution(run_id=211, automation_project_id=None)) == "211"
    # Even with a repo id also set, the run still wins — a run-scoped execution
    # of a project-backed spec is the normal case, not a project-scoped one.
    assert channel_key(Execution(run_id=211, automation_project_id=7)) == "211"


def test_channel_key_of_a_run_less_execution_is_keyed_by_its_automation_repo():
    from app.models.execution import Execution
    from app.services.execution_service import channel_key

    assert channel_key(Execution(run_id=None, automation_project_id=7)) == "project:7"


def test_a_run_less_execution_persists_with_a_spec_path_only_result(db_session):
    """The schema genuinely accepts an Execution with no Run.

    This is the assertion the model change alone cannot pass: ``executions.run_id``
    was NOT NULL, so without the migration the INSERT fails. It also pins the
    shape a project-scoped result has — identified by ``spec_path``, with no
    ticket or case to name it.
    """
    from app.models.automation_project import AutomationProject
    from app.models.execution import Execution, ExecutionResult

    project = AutomationProject(project_key="demo", repo="surency-admin-hub")
    db_session.add(project)
    db_session.flush()

    execution = Execution(
        run_id=None,
        owner_id=None,
        automation_project_id=project.id,
        status="running",
        target="server",
        total=1,
    )
    db_session.add(execution)
    db_session.flush()
    db_session.add(
        ExecutionResult(
            execution_id=execution.id,
            test_case_id=0,
            ticket_external_id="",
            case_code="",
            spec_path="tests/1377/1377-TC-01.spec.ts",
            status="pending",
        )
    )
    db_session.commit()

    stored = db_session.get(Execution, execution.id)
    assert stored.run_id is None
    assert stored.automation_project_id == project.id
    assert [r.spec_path for r in stored.results] == ["tests/1377/1377-TC-01.spec.ts"]


def test_start_execution_stamps_the_runs_owner_on_the_execution(client, db_session, monkeypatch):
    """Ownership is denormalized onto the Execution, not derived from the Run.

    The agent's job claim scopes by owner, and a run-less execution has no Run to
    join — so the column has to be populated at creation. Seeding a run with a
    real owner is what makes this a live assertion: with the stamp removed,
    ``owner_id`` comes back ``None`` rather than the owner's id.
    """
    from app.models.execution import Execution
    from app.models.user import User

    # Patch the name the ROUTER holds, not `playwright_runner.run_execution`:
    # `routers/execution.py` does `from ... import run_execution`, so patching the
    # service module leaves the router's already-bound reference untouched and the
    # worker thread really does shell out to `npx playwright test`.
    monkeypatch.setattr("app.routers.execution.run_execution", lambda execution_id: None)

    owner = User(email="owner@example.com")
    db_session.add(owner)
    db_session.flush()

    run, _case = _seed_run_with_approved_case(db_session)
    run.owner_id = owner.id
    db_session.commit()

    created = client.post(f"/runs/{run.id}/execution", json={})
    assert created.status_code == 200

    execution = db_session.get(Execution, created.json()["id"])
    assert execution.owner_id == owner.id
    # And the run-scoped columns still say what they always did.
    assert execution.run_id == run.id
    assert execution.automation_project_id is None


def test_a_project_executions_result_evidence_is_readable_and_owner_scoped(
    client, db_session
):
    """``GET /results/{id}/evidence`` serves a **run-less** execution's evidence.

    This is the only route to a project-scoped execution's failure screenshots —
    ``GET /runs/{id}/evidence`` needs a run, and #796's executions have none — so
    the Automation tab's report viewer (#801) reads it directly. The endpoint used
    to scope every result through ``get_owned_or_404(db, Run, result.execution.run_id,
    …)``, which for ``run_id = NULL`` looked up ``Run(None)`` and 404'd: not a
    permission failure, just an unreachable row.

    Both halves are asserted, because a 200 alone would also pass with the owner
    dropped: the served path has to carry the execution's **own** owner scope
    (ADR 0009), or the ``<img>`` the viewer builds from it points at another
    user's tree — or at ``shared/`` and 404s in the browser instead.
    """
    from app.models.automation_project import AutomationProject
    from app.models.execution import Evidence, Execution, ExecutionResult
    from app.models.user import User

    owner = User(email="report-viewer@example.com")
    project = AutomationProject(project_key="demo", repo="surency-admin-hub")
    db_session.add_all([owner, project])
    db_session.flush()

    execution = Execution(
        run_id=None,
        owner_id=owner.id,
        automation_project_id=project.id,
        status="failed",
        target="local-agent",
        total=1,
    )
    db_session.add(execution)
    db_session.flush()
    result = ExecutionResult(
        execution_id=execution.id,
        test_case_id=0,
        ticket_external_id="",
        case_code="",
        spec_path="tests/1377/1377-TC-01.spec.ts",
        status="failed",
    )
    db_session.add(result)
    db_session.flush()
    db_session.add(
        Evidence(
            result_id=result.id,
            kind="screenshot",
            path="EXEC-9/1377-TC-01/failure.png",
            filename="failure.png",
        )
    )
    db_session.commit()

    response = client.get(f"/results/{result.id}/evidence")
    assert response.status_code == 200
    rows = response.json()
    assert [r["filename"] for r in rows] == ["failure.png"]
    assert rows[0]["path"] == f"users/{owner.id}/evidence/EXEC-9/1377-TC-01/failure.png"
