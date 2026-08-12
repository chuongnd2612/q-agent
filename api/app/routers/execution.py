"""Execution router — run the approved Playwright suite for a Run.

Endpoints to implement:
  POST /runs/{run_id}/execution        -> ExecutionOut   (start; body ExecutionStart; async)
  GET  /runs/{run_id}/execution        -> ExecutionOut   (latest execution + results)
  GET  /executions/{execution_id}      -> ExecutionOut

Spawns Playwright (real) against workspace/specs, streams per-case status via WS
(events: exec.case.running / exec.case.result / exec.progress / exec.done),
records ExecutionResult + Evidence rows, advances Run.status executing→evidence.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps_auth import current_user
from app.models.agent_device import AgentDevice
from app.models.execution import EXEC_TARGETS, Evidence, Execution, ExecutionResult
from app.models.run import Run
from app.models.testcase import TestCase
from app.models.user import User
from app.services import settings_store
from app.services.ownership import get_owned_or_404
from app.services.playwright_runner import run_execution
from app.services.run_status import set_run_status
from app.services.workspace_scope import served_evidence_path

router = APIRouter(tags=["execution"])


def _resolve_target(body: dict) -> str:
    """Resolve the execution target ("server" | "local-agent") for a new run.

    ``body["target"]`` wins when it's a valid target; otherwise falls back to
    the workspace-wide ``executionTarget`` setting (Local Agent feature).
    """
    requested = (body.get("target") or "").strip().lower()
    if requested in EXEC_TARGETS:
        return requested
    default = settings_store.load_settings().get("executionTarget", "server")
    return default if default in EXEC_TARGETS else "server"


def _require_paired_device(db: Session, owner_id: int | None) -> None:
    """409s unless ``owner_id`` has at least one non-revoked paired Local Agent."""
    has_device = (
        db.query(AgentDevice)
        .filter(AgentDevice.owner_id == owner_id, AgentDevice.revoked_at.is_(None))
        .first()
        is not None
    )
    if not has_device:
        raise HTTPException(
            status_code=409, detail="No local agent paired — start your local agent"
        )


@router.post("/runs/{run_id}/execution")
def start_execution(
    run_id: int,
    body: dict = Body(default_factory=dict),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> dict:
    """Create an Execution + pending ExecutionResults, then run Playwright in a thread."""
    run = get_owned_or_404(db, Run, run_id, user)

    cases = (
        db.query(TestCase)
        .filter(
            TestCase.run_id == run_id,
            TestCase.approval == "approved",
            TestCase.automation != "Manual",
        )
        .order_by(TestCase.id)
        .all()
    )
    # A spec that failed the placeholder gate (blocked) or was classified a genuine
    # product defect (terminal) must never be promoted to Execution — blocked specs
    # aren't runnable, and product defects route to the report, not a re-run.
    cases = [c for c in cases if not (c.spec and c.spec.status in ("blocked", "product_defect"))]
    if not cases:
        raise HTTPException(status_code=400, detail="No runnable specs to execute (all approved cases are blocked or product defects)")

    workers = body.get("workers") or run.workers
    env = body.get("env") or run.env
    target = _resolve_target(body)
    if target == "local-agent":
        _require_paired_device(db, run.owner_id)

    execution = Execution(
        run_id=run_id,
        status="running" if target == "server" else "queued",
        target=target,
        env=env,
        browser=run.browser,
        workers=workers,
        total=len(cases),
        started_at=datetime.now(timezone.utc) if target == "server" else None,
    )
    db.add(execution)
    db.flush()

    for case in cases:
        db.add(
            ExecutionResult(
                execution_id=execution.id,
                test_case_id=case.id,
                ticket_external_id=case.ticket_external_id,
                case_code=case.code,
                title=case.title,
                status="pending",
            )
        )

    set_run_status(db, run, "executing")
    # set_run_status commits only when the transition applies; it no-ops (no
    # commit) on a terminal run, so commit the execution explicitly or the
    # flushed rows roll back on session close (a "run anyway" on a failed run
    # would 200 but queue nothing — see #247).
    db.commit()
    db.refresh(execution)

    if target == "server":
        thread = threading.Thread(target=run_execution, args=(execution.id,), daemon=True)
        thread.start()

    return _execution_out(db, execution, run.owner_id)


@router.post("/cases/{case_id}/spec/run")
def run_single_spec(
    case_id: int,
    body: dict = Body(default_factory=dict),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> dict:
    """Execute just one test case's spec (the "run this test" action).

    Creates an Execution with a single pending ExecutionResult and runs only that
    spec (run_execution runs one file when the execution has one result). 404 if
    the case/spec/run is missing; 400 if the case isn't automatable; 409 if
    ``target`` resolves to "local-agent" but no device is paired.
    """
    case = db.get(TestCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Test case not found")
    if case.approval != "approved" or case.automation == "Manual":
        raise HTTPException(status_code=400, detail="Case is not an approved, automatable test")
    # A product defect is a triaged APP bug routed to the report — never re-run it
    # (running would just re-observe the same defect). A *blocked* spec, however,
    # can be run on explicit demand ("run anyway" / Self-heal attempt): it's a
    # manual override, so the gate stays authoritative for normal/bulk runs while
    # letting a human try to unblock one spec.
    if case.spec and case.spec.status == "product_defect":
        raise HTTPException(
            status_code=400,
            detail="Spec is a product defect — routed to the report, not re-run.",
        )
    run = get_owned_or_404(db, Run, case.run_id, user)
    # A blocked spec was never written to disk (it's excluded from the runnable
    # set), so materialize its current code so the runner has a file to execute.
    # Project-backed specs (#540) are deliberately excluded: their code is never
    # committed to the project while blocked, and execution staging materializes it
    # at `tests/<TICKET>/…` from the row. Writing it here as well would put a second
    # copy at the staged dir's root, and Playwright would collect and report the
    # same test twice.
    if (
        case.spec
        and case.spec.project_id is None
        and case.spec.status == "blocked"
        and (case.spec.code or "").strip()
    ):
        from app.services import spec_service

        case.spec.path = str(
            spec_service.write_spec_file(
                run.code, case.ticket_external_id, case.code, case.spec.code, run.owner_id
            )
        )
        db.commit()
    target = _resolve_target(body)
    if target == "local-agent":
        _require_paired_device(db, run.owner_id)

    execution = Execution(
        run_id=run.id,
        status="running" if target == "server" else "queued",
        target=target,
        env=run.env,
        browser=run.browser,
        workers=1,
        total=1,
        started_at=datetime.now(timezone.utc) if target == "server" else None,
    )
    db.add(execution)
    db.flush()
    db.add(
        ExecutionResult(
            execution_id=execution.id,
            test_case_id=case.id,
            ticket_external_id=case.ticket_external_id,
            case_code=case.code,
            title=case.title,
            status="pending",
        )
    )
    set_run_status(db, run, "executing")
    # See start_execution: set_run_status no-ops (no commit) on a terminal run,
    # so commit explicitly or the flushed Execution/ExecutionResult roll back —
    # a "run anyway" on a blocked/failed run would 200 but queue nothing (#247).
    db.commit()
    db.refresh(execution)

    if target == "server":
        threading.Thread(target=run_execution, args=(execution.id,), daemon=True).start()
    return _execution_out(db, execution, run.owner_id)


@router.get("/runs/{run_id}/execution")
def get_latest_execution(
    run_id: int, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> dict:
    """Return the most recent Execution for a run, with its results."""
    run = get_owned_or_404(db, Run, run_id, user)
    execution = (
        db.query(Execution)
        .filter(Execution.run_id == run_id)
        .order_by(Execution.id.desc())
        .first()
    )
    if execution is None:
        raise HTTPException(status_code=404, detail="No execution found for this run")
    return _execution_out(db, execution, run.owner_id)


@router.get("/executions/{execution_id}")
def get_execution(
    execution_id: int, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> dict:
    """Return a single Execution by id, with its results."""
    execution = db.get(Execution, execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Execution not found")
    run = get_owned_or_404(db, Run, execution.run_id, user)
    return _execution_out(db, execution, run.owner_id)


def _execution_out(db: Session, execution: Execution, owner_id: int | None) -> dict:
    results = (
        db.query(ExecutionResult)
        .filter(ExecutionResult.execution_id == execution.id)
        .order_by(ExecutionResult.id)
        .all()
    )
    return {
        "id": execution.id,
        "runId": execution.run_id,
        "status": execution.status,
        "target": execution.target,
        "env": execution.env,
        "browser": execution.browser,
        "workers": execution.workers,
        "total": execution.total,
        "passed": execution.passed,
        "failed": execution.failed,
        "progress": execution.progress,
        "log": execution.log,
        "startedAt": execution.started_at,
        "finishedAt": execution.finished_at,
        "results": [_result_out(db, r, owner_id) for r in results],
    }


def _result_out(db: Session, result: ExecutionResult, owner_id: int | None) -> dict:
    """Evidence ``path`` is stored relative to the scoped evidence root; rewrite
    it to the served ``<scope>/evidence/...`` form (ADR 0009 §5)."""
    evidence = (
        db.query(Evidence).filter(Evidence.result_id == result.id).order_by(Evidence.id).all()
    )
    return {
        "id": result.id,
        "testCaseId": result.test_case_id,
        "ticketExternalId": result.ticket_external_id,
        "caseCode": result.case_code,
        "title": result.title,
        "status": result.status,
        "failureClass": result.failure_class,
        "durationMs": result.duration_ms,
        "errorMessage": result.error_message,
        "consoleLogs": result.console_logs,
        "networkLogs": result.network_logs,
        "evidence": [
            {
                "id": e.id,
                "kind": e.kind,
                "filename": e.filename,
                "path": served_evidence_path(owner_id, e.path) if e.path else e.path,
                "sizeBytes": e.size_bytes,
                "annotated": e.annotated,
                "meta": e.meta,
            }
            for e in evidence
        ],
    }
