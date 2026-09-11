"""Run a selection of specs straight out of a project's automation repo (#797).

The run-scoped path (``playwright_runner.run_execution``) starts from a Run, walks
its approved ``TestCase`` rows to their ``AutomationSpec`` rows, and stages those.
None of that exists here: the Automation tab hands us **explicit repo-relative
paths**, the code comes from the ``automation_files`` mirror, and there is no run
to advance, no ticket to attribute to and no case to name.

So this is a separate worker rather than a branch inside ``run_execution``. It
deliberately reuses the pieces that are genuinely shared — ``_write_config``,
``_apply_fixtures``, ``_invoke_playwright``, ``parse_playwright_report``,
``execution_service.apply_result`` and ``execution_service.finalize`` — so a
change to how Playwright is configured or how a report is parsed cannot drift
between the two paths.

Three things are deliberately **not** carried over from the run path:

* **Cancel.** ``run_control`` is keyed on a run id and every one of its call
  sites is run-scoped (see #796); a project execution passes ``run_id=None`` to
  ``_invoke_playwright``, which is its documented unregistered invocation. There
  is no cancel for a project execution yet.
* **Run status.** There is no run, so nothing advances a pipeline stage. The
  Execution row's own ``status`` is the whole lifecycle.
* **Evidence and the stored JSON report.** Per #795 a project execution uploads
  no per-case evidence, and persisting ``report.json`` for the viewer lands with
  the agent slice (#798) so the server and agent targets gain it together.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app import db as db_module
from app.config import settings
from app.logging import logger
from app.models.automation_project import AutomationFile, AutomationProject
from app.models.execution import Execution, ExecutionResult
from app.services import (
    automation_project_service,
    execution_service,
    project_config_service,
    settings_store,
)
from app.services.playwright_runner import (
    _apply_fixtures,
    _invoke_playwright,
    _write_config,
    capture_storage_state,
    parse_playwright_report,
)
from app.services.workspace_scope import scoped_specs_dir
from app.ws import hub


def staging_label(execution: Execution) -> str:
    """Directory name for this execution's staged copy.

    Keyed on the **execution id**, not on the repo: two executions of the same
    repo must not share a ``playwright.config.ts`` / ``report.json`` / rewritten
    import tree, which is the same reason the run path keys its dir on the run
    code. Nothing parses this string — see ``stage_for_run``'s ``label``.
    """
    return f"projexec-{execution.id}"


def create(
    db: Session,
    project: AutomationProject,
    spec_paths: list[str],
    *,
    workers: int,
    env: str,
    target: str,
    owner_id: int | None,
) -> Execution:
    """Create the Execution and one pending ExecutionResult per selected spec.

    Each result is identified by ``spec_path`` alone: there is no ticket and no
    case behind a spec read out of the repo. ``title`` carries the file's own
    basename so the row says something legible before Playwright reports a test
    title.
    """
    execution = Execution(
        run_id=None,
        owner_id=owner_id,
        automation_project_id=project.id,
        status="running" if target == "server" else "queued",
        target=target,
        env=env,
        browser="chromium",
        workers=workers,
        total=len(spec_paths),
        started_at=datetime.now(timezone.utc) if target == "server" else None,
    )
    db.add(execution)
    db.flush()
    for path in spec_paths:
        db.add(
            ExecutionResult(
                execution_id=execution.id,
                test_case_id=0,
                ticket_external_id="",
                case_code="",
                spec_path=path,
                title=Path(path).name,
                status="pending",
            )
        )
    db.commit()
    db.refresh(execution)
    return execution


def start_in_thread(execution_id: int) -> None:
    """Spawn the worker, mirroring how the run path dispatches its execution."""
    threading.Thread(target=run, args=(execution_id,), daemon=True).start()


def _stage(
    db: Session, project: AutomationProject, execution: Execution, spec_paths: list[str]
) -> Path:
    """Stage the repo's library plus **only the selected specs**, from the mirror.

    ``stage_for_run`` copies the library and the named spec files off the
    project's working tree. Any selected path the working tree does not have is
    then materialized from the ``automation_files`` mirror — the mirror is what
    the Automation tab listed and validated against, so it is what must run. A
    tree/mirror disagreement would otherwise surface as "Playwright produced no
    report" rather than as the missing file it is.
    """
    staged = automation_project_service.stage_for_run(
        project, staging_label(execution), spec_paths, owner_id=execution.owner_id
    )
    missing = [p for p in spec_paths if not (staged / p).is_file()]
    if missing:
        rows = {
            row.path: row.code or ""
            for row in db.query(AutomationFile)
            .filter(AutomationFile.project_id == project.id, AutomationFile.path.in_(missing))
            .all()
        }
        for path in missing:
            code = rows.get(path, "")
            if not code.strip():
                continue
            destination = staged / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(code, encoding="utf-8")
    return staged


def _resolve_auth(
    db: Session, project: AutomationProject, owner_id: int | None, env: str
) -> tuple[str, str, bool, Path]:
    """``(project_key, base_url, manual_auth, session_file)`` for this repo.

    The run path has to walk ``RunTicket -> project`` to find this
    (``_resolve_project_for_run``); a project-scoped execution already knows its
    provider project key, so it reads the config directly. ``base_url`` honours
    the requested environment the same way the run path does.
    """
    project_key = (project.project_key or "").strip()
    base_url = ""
    manual_auth = False
    if project_key:
        base_url = project_config_service.resolve_base_url(db, project_key, env)
        config = project_config_service.get_config(db, project_key)
        manual_auth = bool(config.manual_auth) if config else False
    session_file = (
        project_config_service.session_path(project_key, owner_id) if project_key else Path()
    )
    return project_key, base_url, manual_auth, session_file


def _fail_all(
    db: Session, execution: Execution, results: list[ExecutionResult], message: str
) -> None:
    """Mark every selected spec failed with one legible reason, then finalize.

    Mirrors ``playwright_runner._fail_all_results``: a run that cannot proceed
    (no base URL, manual login not completed) must say so on every row rather
    than leaving them ``pending`` forever.

    Rows that already reached a terminal status are **left alone**, and the final
    counts are recomputed from all of them. That matters for the crash path,
    which can fire after some specs genuinely passed: overwriting those would
    report a clean suite as entirely failed.
    """
    channel = execution_service.channel_key(execution)
    for result in results:
        if result.status in ("pass", "fail", "skipped"):
            continue
        result.status = "fail"
        result.error_message = message
        result.duration_ms = 0
        db.commit()
        hub.publish(
            channel,
            "exec.case.result",
            {"specPath": result.spec_path, "status": "fail", "durationMs": 0},
        )
    execution.passed = sum(1 for r in results if r.status == "pass")
    execution.failed = sum(1 for r in results if r.status == "fail")
    execution.total = len(results)
    execution_service.finalize(db, execution, None, message)


def run(execution_id: int) -> None:
    """Background worker: run the selected specs and record their results.

    Opens its own session (it runs in a daemon thread) and never raises — a crash
    is logged and the execution is left for the UI to show as unfinished, exactly
    as the run path behaves.
    """
    db = db_module.SessionLocal()
    try:
        execution = db.get(Execution, execution_id)
        if execution is None:
            return
        project = (
            db.get(AutomationProject, execution.automation_project_id)
            if execution.automation_project_id
            else None
        )
        if project is None:
            logger.warning("Project execution {} has no automation project", execution_id)
            return

        results = (
            db.query(ExecutionResult)
            .filter(ExecutionResult.execution_id == execution.id)
            .order_by(ExecutionResult.id)
            .all()
        )
        spec_paths = [r.spec_path for r in results]
        channel = execution_service.channel_key(execution)

        try:
            for index, result in enumerate(results, start=1):
                result.status = "running"
                db.commit()
                hub.publish(
                    channel,
                    "exec.case.running",
                    {
                        "specPath": result.spec_path,
                        "index": index,
                        "total": len(results),
                    },
                )

            spec_dir = _stage(db, project, execution, spec_paths)
            stored = settings_store.load_settings()
            headless = bool(stored.get("headless", True))
            project_key, base_url, manual_auth, session_file = _resolve_auth(
                db, project, execution.owner_id, execution.env
            )

            storage_state = ""
            if manual_auth and project_key:
                session_path = project_config_service.auth_path(project_key, execution.owner_id)
                if session_path.exists() and session_path.stat().st_size > 0:
                    storage_state = str(session_path)
                elif base_url:
                    hub.publish(channel, "exec.auth.waiting", {"url": base_url})
                    if capture_storage_state(base_url, session_path):
                        storage_state = str(session_path)
                        hub.publish(channel, "exec.auth.captured", {})
                    else:
                        message = (
                            "Manual login was not completed — enable/redo login capture"
                        )
                        hub.publish(channel, "exec.auth.error", {"message": message})
                        _fail_all(db, execution, results, message)
                        return
                else:
                    message = "Set a base URL for the project first."
                    hub.publish(channel, "exec.auth.error", {"message": message})
                    _fail_all(db, execution, results, message)
                    return

            _write_config(
                spec_dir,
                execution.workers,
                headless,
                base_url,
                storage_state,
                capture_video=bool(stored.get("video", False)),
            )
            replay_session = bool(
                manual_auth and storage_state and session_file and session_file.exists()
            )
            _apply_fixtures(
                spec_dir,
                session_file if project_key else spec_dir / "sessionStorage.json",
                replay_session,
            )

            # One selected spec → run just that file, same as the run path's
            # "run this test". The basename is passed rather than the repo-relative
            # path because Playwright matches its positional filter against the
            # whole path and a basename matches regardless of separator.
            single_spec = Path(spec_paths[0]).name if len(spec_paths) == 1 else ""

            report: dict[str, Any] = {}
            run_error: str | None = None
            proc_output = ""
            started = time.monotonic()
            try:
                returncode, stdout, stderr = _invoke_playwright(
                    spec_dir, execution.workers, settings.exec_timeout_s, spec_file=single_spec
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
            passed = failed = 0
            matched: set[int] = set()
            for entry in parsed:
                entry = dict(entry)
                entry["duration_ms"] = entry["duration_ms"] or elapsed_ms
                result = execution_service.apply_result(db, results, entry)
                if result is None:
                    continue
                matched.add(result.id)
                if result.status == "pass":
                    passed += 1
                elif result.status == "fail":
                    failed += 1
                hub.publish(
                    channel,
                    "exec.case.result",
                    {
                        "specPath": result.spec_path,
                        "status": result.status,
                        "durationMs": result.duration_ms,
                    },
                )
                execution.passed = passed
                execution.failed = failed
                execution.progress = (
                    int(100 * len(matched) / len(results)) if results else 100
                )
                db.commit()
                hub.publish(
                    channel,
                    "exec.progress",
                    {
                        "progress": execution.progress,
                        "passed": passed,
                        "failed": failed,
                        "remaining": len(results) - len(matched),
                    },
                )

            # A spec Playwright never reported on is a failure, not a row left
            # "running" — the same reconcile the run path does.
            for result in results:
                if result.id in matched:
                    continue
                result.status = "fail"
                result.error_message = run_error or "No result reported by Playwright"
                result.duration_ms = 0
                failed += 1
                db.commit()
                hub.publish(
                    channel,
                    "exec.case.result",
                    {"specPath": result.spec_path, "status": "fail", "durationMs": 0},
                )

            execution.passed = passed
            execution.failed = failed
            execution.total = len(results)
            log_text = run_error or proc_output
            execution_service.finalize(db, execution, None, log_text)
        except Exception as exc:  # noqa: BLE001 - never crash the worker thread silently
            # A crash must still leave a TERMINAL status. The run path can afford
            # to bail (a Run has its own `failed_stage` and a retry endpoint); a
            # project execution's only lifecycle is this row, so leaving it
            # "running" would spin the tab's progress bar forever with no way back.
            logger.error("Project execution {} crashed: {}", execution_id, exc)
            db.rollback()
            message = f"The execution crashed: {exc}"
            hub.publish(channel, "exec.error", {"message": message})
            try:
                _fail_all(db, execution, results, message)
            except Exception as inner:  # noqa: BLE001 - the DB itself may be the problem
                logger.error("Could not finalize crashed execution {}: {}", execution_id, inner)
    finally:
        db.close()
