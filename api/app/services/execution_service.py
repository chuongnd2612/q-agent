"""Shared Execution/ExecutionResult mutation — extracted from
``playwright_runner`` (``_match_result`` + the ``run_execution`` finalize tail,
Local Agent feature, #DRY) so the server runner and the Local Agent's job-push
endpoints (``routers/agent.py``) update rows and emit WS events identically.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.models.execution import Execution, ExecutionResult
from app.models.run import Run
from app.services import audit_service
from app.services.run_status import set_run_status
from app.ws import hub


def channel_key(execution: Execution) -> str:
    """The WS channel an Execution's progress events belong on.

    A run-scoped execution keeps the channel name it has always had —
    ``str(run_id)``, which is what ``/ws/runs/{run_id}`` and every existing SPA
    subscriber listen on, so routing publishes through here changes nothing for
    them. A project-scoped execution (#796) has no run, so it is keyed by its
    automation repo instead.

    The project key uses ``automation_project_id`` rather than the project GUID
    deliberately: the hub is keyed by an arbitrary string (see
    ``hub.connect("ai", …)``), the repo id is already what the Automation tab
    holds as ``selectedRepo.id``, and it needs no DB lookup to derive.
    """
    if execution.run_id is not None:
        return str(execution.run_id)
    return f"project:{execution.automation_project_id}"


def match_result(results: list[ExecutionResult], filename: str) -> ExecutionResult | None:
    """Find the ExecutionResult whose spec filename convention matches ``filename``.

    Two conventions are accepted, **in this order**:

    1. ``{ticketExternalId}-{caseCode}.spec.ts`` — the #540 form
       (``spec_service.spec_filename``), e.g. ``"SUR-1428-TC-01.spec.ts"``.
    2. ``{shortTicket}-{caseCode}.spec.ts`` — the pre-#540 form
       (``spec_service.legacy_spec_filename``), e.g. ``"1428-TC-01.spec.ts"``.

    The order matters and is the whole point of the fallback being a *second
    pass* rather than an ``or``: a run can legitimately span ``SUR-1428`` and
    ``OPS-1428`` (``RunTicket`` is many-per-run, each with its own repo — see
    ``app/models/run.py:84``), and both collapse to the same legacy short form.
    Matching every row's full form first guarantees the correct attribution, and
    only a filename that matches no full form at all can fall through to the
    ambiguous legacy comparison — which is exactly the in-flight-legacy-run case
    the fallback exists for.

    ``filename`` is basenamed, so the project-relative
    ``tests/SUR-1428/SUR-1428-TC-01.spec.ts`` that Playwright now reports matches
    without any change here. Shared by the server runner (matching a Playwright
    JSON report entry) and the Local Agent's job-results endpoint (matching a
    pushed result payload), so both paths are fixed at once.

    A **project-scoped** result (#796) is matched before either convention, on
    ``spec_path``. It has no ticket and no case code, so both conventions below
    would build ``"-.spec.ts"`` for it and match nothing — and a spec run straight
    out of an automation repo is under no obligation to be named
    ``{ticket}-{case}.spec.ts`` in the first place. The full path is compared
    first and the basename only as a fallback, because Playwright reports paths
    relative to its own ``testDir``, while ``spec_path`` is relative to the repo
    root.
    """
    name = Path(filename).name
    for result in results:
        if result.spec_path and result.spec_path == filename:
            return result
    for result in results:
        if result.spec_path and Path(result.spec_path).name == name:
            return result
    for result in results:
        if f"{result.ticket_external_id}-{result.case_code}.spec.ts" == name:
            return result
    for result in results:
        legacy = f"{(result.ticket_external_id or '').rsplit('-', 1)[-1]}-{result.case_code}.spec.ts"
        if legacy == name:
            return result
    return None


def apply_result(
    db: Session, results: list[ExecutionResult], entry: dict[str, Any]
) -> ExecutionResult | None:
    """Match ``entry`` to its ExecutionResult and update status/duration/error.

    Commits the update but does NOT publish ``exec.case.result`` — callers
    decide when/whether to emit it (the server runner publishes only after
    evidence has also been stored, preserving today's event order; the Local
    Agent's events endpoint re-emits explicitly).

    Args:
        db: Active session.
        results: Candidate ExecutionResult rows for the owning Execution.
        entry: A dict shaped like ``parse_playwright_report``'s output — at
            least ``file`` (or ``filename``), ``status``, ``duration_ms``,
            ``error_message``.

    Returns:
        The matched, updated ExecutionResult, or ``None`` if no row's filename
        convention matches ``entry``'s file name.
    """
    filename = entry.get("file") or entry.get("filename") or ""
    result = match_result(results, filename)
    if result is None:
        return None
    result.status = entry.get("status", result.status)
    result.duration_ms = entry.get("duration_ms") or 0
    result.error_message = entry.get("error_message", "")
    db.commit()
    return result


def finalize(
    db: Session,
    execution: Execution,
    run: Run | None,
    log: str,
    advance_run: bool = True,
) -> None:
    """Finalize an Execution: stamp the log, mark done, advance the run, notify.

    Expects ``execution.passed``/``execution.failed``/``execution.total`` to
    already reflect the final counts (the caller sets these first). Commits,
    publishes ``exec.progress`` (100%) + ``exec.done``, advances ``run.status``
    to ``"evidence"``, and records the execution audit entry. Shared by the
    server runner's normal completion path and the Local Agent's
    ``POST /agent/jobs/{id}/complete`` endpoint.

    Args:
        run: The owning run, or ``None`` for a project-scoped execution (#796),
            which has no run to advance and no run code to name in the audit
            entry. A project-scoped execution's lifecycle **is** the Execution
            row's own status.
        advance_run: When False, the run's lifecycle status is left untouched —
            used for agent-executed self-heal (#260), which re-runs one case's
            spec and must not push the whole run into the ``evidence`` stage
            (matching the server heal loop, which never advances the run).
    """
    execution.log = (log or "")[-20000:]
    execution.progress = 100
    execution.status = "done"
    execution.finished_at = datetime.now(timezone.utc)
    db.commit()

    channel = channel_key(execution)
    hub.publish(
        channel,
        "exec.progress",
        {"progress": 100, "passed": execution.passed, "failed": execution.failed, "remaining": 0},
    )
    hub.publish(channel, "exec.done", {"passed": execution.passed, "failed": execution.failed})
    if advance_run and run is not None:
        set_run_status(db, run, "evidence")

    audit_service.record(
        category="execution", actor_type="ai", action="Executed test run",
        target=(
            f"{run.code} · {execution.total} cases"
            if run is not None
            else f"automation repo #{execution.automation_project_id} · {execution.total} specs"
        ),
        status="warning" if execution.failed else "success",
        meta=f"{execution.passed} passed · {execution.failed} failed",
    )
