"""AI analysis + test-case generation pipeline.

Runs (per Run) over each of its RunTicket rows: analyzes the ticket with Claude,
then generates ADO-style manual test cases with Claude, persisting TestCase rows.
Publishes WS progress events throughout. Per ADR 0001 there is no simulated
fallback — Claude errors are surfaced on the RunTicket (`gen_status='error'`,
`analysis_error=...`) and published as a WS event.

Runs in a background thread (kicked off by the runs router), so it opens its
OWN DB session via ``SessionLocal`` rather than reusing a request-scoped session.
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy.orm import Session

from app import db as db_module
from app.logging import logger
from app.models.run import Run, RunTicket
from app.models.testcase import TestCase
from app.models.ticket import Ticket
from app.services import (
    audit_service,
    connection_service,
    project_config_service,
    run_context,
    run_control,
    settings_store,
)
from app.services.claude_cli import ClaudeError, run_json
from app.services.run_status import set_run_status
from app.services.skills import (
    REQUIREMENT_ANALYST,
    TEST_CASE_GENERATOR,
    TEST_CASE_REVIEWER,
    load_skill,
)
from app.services.prompts import (
    build_case_regenerate_prompt,
    build_combined_prompt,
    build_review_prompt,
)
from app.ws import hub

PHASE_READING = "reading"
PHASE_UNDERSTANDING_AC = "understanding acceptance criteria"
PHASE_BUSINESS_RULES = "identifying business rules"
PHASE_GENERATING = "generating test cases"
PHASE_REVIEWING = "reviewing coverage"


def _publish_phase(run_id: int, ticket_external_id: str, phase: str, message: str) -> None:
    hub.publish(
        str(run_id),
        "analysis.phase",
        {"ticket": ticket_external_id, "phase": phase, "message": message},
    )


def next_case_code(db: Session, run_id: int, ticket_external_id: str) -> str:
    """Compute the next TC-NN code for a ticket within a run."""
    existing = (
        db.query(TestCase)
        .filter(TestCase.run_id == run_id, TestCase.ticket_external_id == ticket_external_id)
        .all()
    )
    max_n = 0
    for case in existing:
        suffix = case.code.rsplit("-", 1)[-1]
        if suffix.isdigit():
            max_n = max(max_n, int(suffix))
    return f"TC-{max_n + 1:02d}"


def provider_case_offset(db: Session, ticket: Ticket) -> int:
    """Highest existing 'TC-NN' number among the ticket's provider test cases.

    Pulls existing test cases from the provider (ADO/Jira) so generated codes
    continue the existing numbering/naming instead of restarting at TC-01.
    Best-effort: returns 0 if the provider is unavailable or has none.
    """
    try:
        connection = connection_service.resolve_work_item_for_ticket(db, ticket)
        adapter = connection_service.adapter_for(db, connection)
        existing = adapter.list_test_cases(ticket.external_id)
    except Exception as exc:  # noqa: BLE001 - never block generation on a provider hiccup
        logger.warning("Could not pull existing test cases for {}: {}", ticket.external_id, exc)
        return 0
    max_n = 0
    for tc in existing or []:
        for field in (str(tc.get("code", "")), str(tc.get("title", ""))):
            match = re.search(r"TC-(\d+)", field)
            if match:
                max_n = max(max_n, int(match.group(1)))
    return max_n


def _validated_repo_guess(analysis: dict, context: dict) -> str:
    """Resolve a work item's target repo from Claude's guess and the project repos.

    Uses ``analysis['suggestedRepo']`` when it matches a configured repo name;
    otherwise falls back to the project's default repo name, else "".

    Args:
        analysis: The requirement-analysis JSON returned by Claude.
        context: The project context (provides ``repoOptions`` and the resolved
            default in ``repo``).

    Returns:
        A validated repo name, or "" when the project has no repos.
    """
    options = context.get("repoOptions") or []
    names = {opt.get("name", "") for opt in options}
    suggested = str(analysis.get("suggestedRepo", "") or "").strip()
    if suggested and suggested in names:
        return suggested
    default_name = next((opt["name"] for opt in options if opt.get("default")), "")
    return default_name or context.get("repo", "") or ""


def _case_kwargs_from_raw(raw_case: dict) -> dict:
    """Map a raw Claude case dict to ``TestCase`` column kwargs.

    Shared by the generation and review stages so the JSON→columns mapping —
    including the #177 fields (objective, testData, linkedAc) — lives in one
    place. ``run_id``/``ticket_external_id``/``code``/``source`` are supplied by
    the caller.
    """
    steps = [
        {"a": s.get("a", ""), "e": s.get("e", "")}
        for s in (raw_case.get("steps") or [])
        if isinstance(s, dict)
    ]
    test_data = [
        {"field": d.get("field", ""), "value": d.get("value", "")}
        for d in (raw_case.get("testData") or [])
        if isinstance(d, dict)
    ]
    linked_ac = [str(a) for a in (raw_case.get("linkedAc") or []) if a]
    return {
        "title": raw_case.get("title", ""),
        "objective": raw_case.get("objective", ""),
        "precondition": raw_case.get("precondition", ""),
        "steps": steps,
        "test_data": test_data,
        "linked_ac": linked_ac,
        "priority": raw_case.get("priority", "Medium"),
        "test_type": raw_case.get("testType", "Functional"),
        "automation": raw_case.get("automation", "Playwright"),
        "platform": raw_case.get("platform", "Web"),
    }


def _review_and_expand(
    db: Session,
    run: Run,
    ticket: Ticket,
    run_ticket: RunTicket,
    analysis: dict,
    context: dict,
    *,
    existing_cases: list,
    start_offset: int,
    max_cases: int,
) -> int:
    """Second-stage coverage expansion (#173).

    Asks the ``test-case-reviewer`` skill to audit the happy-path set and
    generate the deferred edge/negative/boundary/permission cases. Persists them
    as ``source='ai-review'`` TestCase rows continuing the TC-NN numbering, and
    records the verdict + coverage gaps on ``run_ticket.analysis['review']``.

    Best-effort: never raises. The happy-path cases are already committed, so any
    failure here (Claude error, bad JSON) is logged and skipped — the ticket
    still completes with its happy-path coverage.

    Args:
        existing_cases: The happy-path cases just generated (given to the reviewer
            so it doesn't duplicate them).
        start_offset: The TC-NN number to continue from (last happy-path number).
        max_cases: Cap on how many additional cases to add.

    Returns:
        The number of additional cases persisted.
    """
    _publish_phase(
        run.id, ticket.external_id, PHASE_REVIEWING, "Reviewing coverage and adding edge cases..."
    )
    try:
        review = run_json(
            build_review_prompt(ticket, analysis, existing_cases, max_cases=max_cases, context=context),
            skill=TEST_CASE_REVIEWER,
            label=f"Review cases: {ticket.external_id}",
        )
    except Exception as exc:  # noqa: BLE001 - review expansion is additive + best-effort
        logger.warning("Test-case review skipped for {}: {}", ticket.external_id, exc)
        return 0
    if not isinstance(review, dict):
        logger.warning("Test-case review response for {} was not a JSON object", ticket.external_id)
        return 0

    additional = review.get("additionalCases") or []
    if not isinstance(additional, list):
        additional = []
    additional = additional[:max_cases]  # cap the expansion like generation

    added = 0
    for i, raw_case in enumerate(additional, start=1):
        if not isinstance(raw_case, dict):
            continue
        db.add(
            TestCase(
                run_id=run.id,
                ticket_external_id=ticket.external_id,
                code=f"TC-{start_offset + i:02d}",
                source="ai-review",
                **_case_kwargs_from_raw(raw_case),
            )
        )
        added += 1

    # Record the verdict + coverage gaps alongside the analysis (reassign the
    # dict so SQLAlchemy tracks the JSON change). No migration needed — a
    # dedicated coverage-matrix column is #177.
    verdict = str(review.get("verdict", "") or "")
    gaps = review.get("coverageGaps") or []
    run_ticket.analysis = {**(run_ticket.analysis or {}), "review": {"verdict": verdict, "coverageGaps": gaps}}
    db.add(run_ticket)
    db.commit()
    logger.info("Test-case review for {}: verdict={!r}, +{} cases", ticket.external_id, verdict, added)
    return added


def _process_run_ticket(db: Session, run: Run, run_ticket: RunTicket) -> None:
    """Analyze + generate test cases for a single RunTicket. Commits as it goes."""
    ticket = db.query(Ticket).filter(Ticket.external_id == run_ticket.ticket_external_id).first()
    if ticket is None:
        run_ticket.gen_status = "error"
        run_ticket.analysis_error = f"Ticket {run_ticket.ticket_external_id} not found"
        db.add(run_ticket)
        db.commit()
        hub.publish(
            str(run.id),
            "analysis.phase",
            {
                "ticket": run_ticket.ticket_external_id,
                "phase": "error",
                "message": run_ticket.analysis_error,
            },
        )
        return

    try:
        run_ticket.gen_status = "analyzing"
        db.add(run_ticket)
        db.commit()

        # Resolve the full Project Knowledge Base + config so analysis and
        # generation reuse real domain terms, routes and account roles.
        context = project_config_service.context_for_ticket(db, ticket, env=run.env)

        _publish_phase(run.id, ticket.external_id, PHASE_READING, "Reading ticket details...")
        _publish_phase(
            run.id, ticket.external_id, PHASE_UNDERSTANDING_AC, "Understanding acceptance criteria..."
        )
        _publish_phase(
            run.id, ticket.external_id, PHASE_BUSINESS_RULES, "Identifying business rules..."
        )

        _publish_phase(run.id, ticket.external_id, PHASE_GENERATING, "Generating test cases...")

        max_cases = int(settings_store.load_settings().get("maxCasesPerTicket", 8) or 8)
        # Continue numbering from existing provider test cases (match convention).
        offset = provider_case_offset(db, ticket)

        # One combined call does analysis + happy-path generation (#174), cutting
        # per-ticket CLI/overhead cost. Compose BOTH skills as the system prompt so
        # neither the analysis nor the generation loses its methodology.
        combined = run_json(
            build_combined_prompt(ticket, max_cases=max_cases, context=context),
            skill=TEST_CASE_GENERATOR,
            system=load_skill(REQUIREMENT_ANALYST),
            label=f"Generate test cases: {ticket.external_id}",
        )
        if not isinstance(combined, dict):
            raise ClaudeError("Claude analyze+generate response was not a JSON object")
        analysis = combined.get("analysis")
        if not isinstance(analysis, dict):
            raise ClaudeError("Claude analyze+generate response had no 'analysis' object")
        cases = combined.get("cases")
        if not isinstance(cases, list):
            raise ClaudeError("Claude analyze+generate 'cases' was not a JSON array")
        cases = cases[:max_cases]  # enforce the per-ticket cap

        run_ticket.analysis = analysis
        run_ticket.repo = _validated_repo_guess(analysis, context)
        run_ticket.gen_status = "generating"
        db.add(run_ticket)
        db.commit()

        # A cancel may have landed during the call; bail before persisting rather
        # than relying solely on run_control killing the subprocess.
        if run_control.is_cancelled(run.id, db):
            logger.info("Run {} cancelled mid-ticket {} — skipping persistence", run.id, ticket.external_id)
            return

        case_count = 0
        for i, raw_case in enumerate(cases, start=1):
            if not isinstance(raw_case, dict):
                continue
            db.add(
                TestCase(
                    run_id=run.id,
                    ticket_external_id=ticket.external_id,
                    code=f"TC-{offset + i:02d}",
                    source="ai",
                    **_case_kwargs_from_raw(raw_case),
                )
            )
            case_count += 1
        db.commit()

        # Stage 3 (two-stage design, #173): the reviewer audits the happy-path
        # set and generates the deferred edge/negative/boundary/permission
        # coverage. Best-effort — the happy-path cases are already committed, so
        # a reviewer failure logs and continues rather than failing the ticket.
        review_count = 0
        if not run_control.is_cancelled(run.id, db):
            review_count = _review_and_expand(
                db, run, ticket, run_ticket, analysis, context,
                existing_cases=cases,
                start_offset=offset + case_count,
                max_cases=max_cases,
            )

        run_ticket.gen_status = "done"
        db.add(run_ticket)
        db.commit()

        hub.publish(
            str(run.id),
            "analysis.ticketDone",
            {"ticket": ticket.external_id, "caseCount": case_count + review_count},
        )
    except ClaudeError as exc:
        # A run cancel kills the in-flight Claude CLI (run_control.kill_processes),
        # which surfaces here as a ClaudeError. Don't mark the ticket "failed" for a
        # deliberate cancel — leave its status for the cancel flow to finalize.
        if run_control.is_cancelled(run.id, db):
            logger.info(
                "Run {} cancelled mid-ticket {} — skipping error mark",
                run.id,
                ticket.external_id,
            )
            return
        logger.error("AI pipeline error for run={} ticket={}: {}", run.id, ticket.external_id, exc)
        run_ticket.gen_status = "error"
        run_ticket.analysis_error = str(exc)
        db.add(run_ticket)
        db.commit()
        hub.publish(
            str(run.id),
            "analysis.phase",
            {"ticket": ticket.external_id, "phase": "error", "message": str(exc)},
        )


def _resolve_worker_count() -> int:
    """How many tickets to analyze+generate concurrently (#179).

    Reads the ``aiPipelineWorkers`` setting; when unset, defaults to a small pool
    on Postgres and stays sequential (1) on SQLite — SQLite's single-writer model
    makes concurrent writers contend even with WAL, so parallelism is opt-in
    there. Always clamped to [1, 4] to stay within one user's Claude rate window.
    """
    from app.services import settings_store

    configured = settings_store.load_settings().get("aiPipelineWorkers")
    if configured:
        try:
            return max(1, min(4, int(configured)))
        except (TypeError, ValueError):
            pass
    return 1 if db_module.engine.dialect.name == "sqlite" else 3


def _process_ticket_worker(run_id: int, run_ticket_id: int, ticket_external_id: str) -> None:
    """Process one RunTicket in an isolated session + run context (parallel path).

    Each :class:`ThreadPoolExecutor` worker needs its OWN session (SQLAlchemy
    sessions are not thread-safe) and must set the ambient run/ticket context
    *inside* the worker thread (ContextVars don't cross threads — see
    :mod:`run_context`). Never raises: :func:`_process_run_ticket` already records
    per-ticket errors on the row; anything unexpected here is logged.
    """
    db = db_module.SessionLocal()
    try:
        with run_context.run_scope(run_id), run_context.ticket_scope(ticket_external_id):
            run = db.query(Run).filter(Run.id == run_id).first()
            run_ticket = db.query(RunTicket).filter(RunTicket.id == run_ticket_id).first()
            if run is None or run_ticket is None:
                return
            if run_control.is_cancelled(run_id, db):
                return
            _process_run_ticket(db, run, run_ticket)
    except Exception as exc:  # noqa: BLE001 - defensive; per-ticket errors handled within
        logger.error("AI pipeline worker error run={} ticket={}: {}", run_id, ticket_external_id, exc)
    finally:
        db.close()


def _run_pipeline(run_id: int) -> None:
    """The actual pipeline body; opens its own session. Safe to call from any thread."""
    # Attribute this thread's Claude spend to the run (see run_context).
    run_context.set_run(run_id)
    db = db_module.SessionLocal()
    try:
        run = db.query(Run).filter(Run.id == run_id).first()
        if run is None:
            logger.warning("run_generation_pipeline: run {} not found", run_id)
            return

        try:
            run_tickets = (
                db.query(RunTicket)
                .filter(RunTicket.run_id == run.id)
                .order_by(RunTicket.position)
                .all()
            )
            workers = _resolve_worker_count()
            if workers <= 1:
                for run_ticket in run_tickets:
                    if run_control.is_cancelled(run.id, db):
                        logger.info("Run {} cancelled — stopping AI pipeline", run.code)
                        return
                    # Attribute this ticket's Claude spend to it so the per-run
                    # cost card can group by ticket (see ai_usage_service).
                    with run_context.ticket_scope(run_ticket.ticket_external_id):
                        _process_run_ticket(db, run, run_ticket)
            else:
                # Bounded parallelism: file-disjoint tickets (each writes only its
                # own rows) processed by a small pool, each worker isolated (own
                # session + context). In-flight Claude calls are still killed on
                # cancel via run_control.register_process.
                logger.info(
                    "AI pipeline: {} tickets across {} workers (run {})",
                    len(run_tickets), workers, run.code,
                )
                targets = [(rt.id, rt.ticket_external_id) for rt in run_tickets]
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [
                        pool.submit(_process_ticket_worker, run.id, rid, ext)
                        for rid, ext in targets
                    ]
                    for future in as_completed(futures):
                        future.result()  # workers swallow their own errors; surface any escapee
                db.expire_all()  # workers committed on their own sessions — refresh ours
                if run_control.is_cancelled(run.id, db):
                    logger.info("Run {} cancelled during parallel generation", run.code)
                    return

            # A run whose generation failed for EVERY ticket has not reached
            # "needs your review" (#758). It used to advance to `review`
            # regardless, so an expired Claude credential produced a run labelled
            # "In review · needs you" whose Review Center said the AI had not
            # generated anything yet — true of the output, wrong about the cause,
            # and a dead end for the user.
            #
            # `failed_stage="processing"` is deliberate: ADR 0005's retry
            # dispatch already resumes that stage by re-running generation, so
            # "fix the credential, press Retry" becomes the obvious path.
            #
            # A PARTIAL failure still goes to `review` — there is real work to
            # review — and the per-ticket `analysis_error` carries the rest.
            db.expire_all()
            errored = [rt for rt in run_tickets if rt.gen_status == "error"]
            if errored and len(errored) == len(run_tickets):
                first = errored[0].analysis_error or "generation failed"
                logger.error(
                    "AI pipeline: all {} ticket(s) failed for run {}: {}",
                    len(errored), run.code, first,
                )
                run.failed_stage = "processing"
                set_run_status(db, run, "failed")
                audit_service.record(
                    category="ai", actor_type="ai", action="Test generation failed",
                    target=f"{run.code} · all {len(errored)} ticket(s)",
                    status="error", meta=str(first)[:400], run_code=run.code,
                )
                return

            if not set_run_status(db, run, "review"):
                return  # already terminal (e.g. cancelled) — don't overwrite it

            case_total = db.query(TestCase).filter(TestCase.run_id == run.id).count()
            audit_service.record(
                category="ai", actor_type="ai", action="Generated test cases",
                target=f"{run.code} · {case_total} cases",
                meta=f"{len(run_tickets)} tickets analyzed",
            )
        except Exception as exc:  # noqa: BLE001 - never crash the worker thread silently
            logger.error("AI pipeline crashed for run {}: {}", run.code, exc)
            db.rollback()
            run.failed_stage = run.status
            set_run_status(db, run, "failed")
            # Surface the failure in the run's activity timeline (#394) — otherwise
            # a crashed pipeline is only visible in the container logs.
            audit_service.record(
                category="ai", actor_type="ai", action="Test generation failed",
                target=f"{run.code} · {run.failed_stage or 'generation'}",
                status="error", meta=str(exc)[:400], run_code=run.code,
            )
    finally:
        db.close()
        run_context.clear()


def run_generation_pipeline(run_id: int, *, blocking: bool = False) -> threading.Thread | None:
    """Kick off the analyze+generate pipeline for a run.

    If ``blocking`` is True (used by tests), runs synchronously in the calling
    thread and returns None. Otherwise starts a background daemon thread and
    returns it immediately so the caller (the request handler) can respond
    without waiting.
    """
    if blocking:
        _run_pipeline(run_id)
        return None
    thread = threading.Thread(target=_run_pipeline, args=(run_id,), daemon=True)
    thread.start()
    return thread


def regenerate_case(db: Session, test_case: TestCase) -> TestCase:
    """Ask Claude to regenerate a single test case in place, keeping its code.

    Uses the parent RunTicket's stored analysis (if any) for context. Raises
    ClaudeError if the CLI call fails; caller is responsible for surfacing it.
    """
    ticket = (
        db.query(Ticket).filter(Ticket.external_id == test_case.ticket_external_id).first()
    )
    if ticket is None:
        raise ClaudeError(f"Ticket {test_case.ticket_external_id} not found")

    run_ticket = (
        db.query(RunTicket)
        .filter(
            RunTicket.run_id == test_case.run_id,
            RunTicket.ticket_external_id == test_case.ticket_external_id,
        )
        .first()
    )
    analysis = run_ticket.analysis if run_ticket else {}

    # Resolve the Project Knowledge Base so the rewrite reuses real routes,
    # roles and selectors instead of inventing them (#183).
    run = db.query(Run).filter(Run.id == test_case.run_id).first()
    context = project_config_service.context_for_ticket(
        db, ticket, env=run.env if run else "Staging"
    )

    existing_case = {
        "title": test_case.title,
        "objective": test_case.objective,
        "precondition": test_case.precondition,
        "testData": test_case.test_data,
        "steps": test_case.steps,
        "linkedAc": test_case.linked_ac,
        "priority": test_case.priority,
        "testType": test_case.test_type,
        "automation": test_case.automation,
        "platform": test_case.platform,
    }

    result = run_json(
        build_case_regenerate_prompt(ticket, analysis, existing_case, context),
        skill=TEST_CASE_GENERATOR,
    )
    if not isinstance(result, dict):
        raise ClaudeError("Claude case-regenerate response was not a JSON object")

    fields = _case_kwargs_from_raw(result)

    test_case.title = result.get("title", test_case.title)
    test_case.objective = result.get("objective", test_case.objective)
    test_case.precondition = result.get("precondition", test_case.precondition)
    test_case.steps = fields["steps"] or test_case.steps
    test_case.test_data = fields["test_data"] or test_case.test_data
    test_case.linked_ac = fields["linked_ac"] or test_case.linked_ac
    test_case.priority = result.get("priority", test_case.priority)
    test_case.test_type = result.get("testType", test_case.test_type)
    test_case.automation = result.get("automation", test_case.automation)
    test_case.platform = result.get("platform", test_case.platform)
    test_case.edited = True

    db.add(test_case)
    db.commit()
    db.refresh(test_case)
    return test_case
