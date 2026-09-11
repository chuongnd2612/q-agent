"""Seed (or return) a fully-populated DEMO run for the product tour.

Builds one terminal ``RUN-DEMO`` run whose row graph spans every pipeline stage
(analysis, test cases, links, automation specs, execution results, evidence,
report, publish comments) by direct insert — WITHOUT ever invoking the AI
generation pipeline (:func:`app.services.ai_service.run_generation_pipeline`).
The result is a run every run-scoped screen can render straight after a fresh
sign-in, for a guided tour.

Unlike :mod:`app.seed` (the destructive dev CLI), this seeder is idempotent and
non-destructive: it inserts prerequisite rows only when absent and returns the
existing demo run unchanged on a repeat call, so it is safe to call per user on
demand from an HTTP endpoint.
"""

from __future__ import annotations

from io import BytesIO

from PIL import Image
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models.comment import TicketComment
from app.models.execution import Evidence, Execution, ExecutionResult
from app.models.knowledge import ProjectKnowledge, compose_key
from app.models.linked import LinkedTestCase
from app.models.project_config import ProjectConfig
from app.models.report import Report
from app.models.run import Run, RunTicket
from app.models.testcase import AutomationSpec, TestCase
from app.models.ticket import Ticket
from app.models.user import User
from app.services import report_service, sample_data
from app.services.evidence_service import store_uploaded_evidence
from app.services.ownership import owned, stamp_owner
from app.services.spec_service import spec_filename

# The demo run's stable code — one demo run (idempotency key).
DEMO_RUN_CODE = "RUN-DEMO"
DEMO_RUN_NAME = "Sample run — Broker regression (demo)"

# Work items the demo run covers (those with a case bank in sample_data).
DEMO_TICKET_IDS = ["SUR-1428", "SUR-1402", "SUR-1390"]

# Provider kind per demo ticket (mirrors sample_data.TICKETS) — used for the
# publish comments' ``provider_kind``.
_PROVIDER_KIND = {t["external_id"]: t["provider_kind"] for t in sample_data.TICKETS}

# Per-ticket AI analysis, keyed as RunTicket.analysis expects
# {businessRules, functionalRequirements, validationRules, risks, edgeCases,
#  missingInformation, suggestedScope}.
_ANALYSIS: dict[str, dict] = {
    "SUR-1428": {
        "businessRules": [
            "Only a Surency Internal Admin may view the broker agency list.",
            "The expiration column shows the nearest upcoming license expiration.",
        ],
        "functionalRequirements": [
            "Render Broker Agencies + Broker Agents tabs, Agencies active by default.",
            "Each row shows name, number, type, next expiration, status and an actions menu.",
        ],
        "validationRules": [
            "Search filters by agency name or number.",
            "The status filter narrows the list to matching agencies.",
        ],
        "risks": ["Agencies with no licenses could render a broken expiration cell."],
        "edgeCases": ["Zero-license agency (blank expiration)", "Empty list (no agencies)"],
        "missingInformation": [],
        "suggestedScope": "Cover the default tab, row contents, actions menu, search and the empty state.",
    },
    "SUR-1402": {
        "businessRules": ["Deactivation must be explicitly confirmed before it applies."],
        "functionalRequirements": [
            "Choosing Deactivate opens a confirmation dialog.",
            "Confirm sets the agency Inactive and shows a toast; Cancel leaves it unchanged.",
        ],
        "validationRules": ["The dialog blocks interaction with the list until dismissed."],
        "risks": ["A missing confirm step could deactivate an agency accidentally."],
        "edgeCases": ["Cancel after opening the dialog", "Confirm on an already-Inactive agency"],
        "missingInformation": [],
        "suggestedScope": "Cover the confirm/cancel branches of the deactivation dialog.",
    },
    "SUR-1390": {
        "businessRules": ["Reminders fire at 30 and 7 days before a license expires."],
        "functionalRequirements": [
            "The daily job queues a 30-day reminder and a 7-day reminder.",
        ],
        "validationRules": ["Sends respect the account's local timezone."],
        "risks": ["Timezone handling could send reminders at the wrong local hour."],
        "edgeCases": ["Account in a non-UTC timezone"],
        "missingInformation": ["Confirm the exact local send hour."],
        "suggestedScope": "Cover the 30-day and 7-day reminder windows and timezone handling.",
    },
}

# A short, realistic Playwright spec body stored on each automation spec.
_SPEC_TEMPLATE = """import {{ test, expect }} from '@playwright/test';

test('{title}', async ({{ page }}) => {{
  await page.goto('/brokers');
  await expect(page.getByRole('tab', {{ name: 'Broker Agencies' }})).toBeVisible();
  // {code} — {ticket}
}});
"""


def _placeholder_png(color: tuple[int, int, int], size: tuple[int, int] = (1280, 720)) -> bytes:
    """Render a small solid-color PNG as raw bytes (a stand-in screenshot).

    Args:
        color: RGB fill for the image.
        size: (width, height) in pixels.

    Returns:
        The encoded PNG file contents.
    """
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _ensure_prerequisites(db: Session, user: User | None) -> None:
    """Insert the demo tickets, knowledge base and project config if missing.

    Each row is inserted only when absent (guarded per row) and owner-stamped, so
    calling this never wipes or duplicates a user's existing data.
    """
    for ticket in sample_data.TICKETS:
        exists = (
            owned(db.query(Ticket), Ticket, user)
            .filter(Ticket.external_id == ticket["external_id"])
            .first()
        )
        if exists is None:
            db.add(stamp_owner(Ticket(**ticket), user))

    knowledge_key = compose_key(sample_data.PROJECT_KEY, sample_data.PRIMARY_REPO)
    kb_exists = (
        owned(db.query(ProjectKnowledge), ProjectKnowledge, user)
        .filter(ProjectKnowledge.key == knowledge_key)
        .first()
    )
    if kb_exists is None:
        db.add(stamp_owner(sample_data.build_project_knowledge(), user))

    cfg_exists = (
        owned(db.query(ProjectConfig), ProjectConfig, user)
        .filter(ProjectConfig.key == sample_data.PROJECT_KEY)
        .first()
    )
    if cfg_exists is None:
        db.add(stamp_owner(sample_data.build_project_config(), user))

    db.flush()


def _build_cases_and_specs(db: Session, run: Run) -> list[TestCase]:
    """Insert the demo run's test cases (+ automation specs for approved,
    automatable ones) and its per-ticket analysis rows. Returns the cases."""
    cases: list[TestCase] = []
    for position, tid in enumerate(DEMO_TICKET_IDS):
        db.add(
            RunTicket(
                run_id=run.id,
                ticket_external_id=tid,
                position=position,
                gen_status="done",
                repo=sample_data.PRIMARY_REPO,
                analysis=_ANALYSIS[tid],
            )
        )
        for code, title, prio, ttype, auto, plat, pre, steps in sample_data.CASE_BANK[tid]:
            # Approve the automatable (non-Manual) cases; leave manual ones pending
            # so the Review screen shows a realistic mix.
            approval = "approved" if auto != "Manual" else "pending"
            case = TestCase(
                run_id=run.id,
                ticket_external_id=tid,
                code=code,
                title=title,
                precondition=pre,
                steps=[{"a": a, "e": e} for a, e in steps],
                priority=prio,
                test_type=ttype,
                automation=auto,
                platform=plat,
                approval=approval,
                source="ai",
            )
            db.add(case)
            cases.append(case)
    db.flush()  # assign case ids for the specs

    for case in cases:
        if case.approval == "approved" and case.automation != "Manual":
            db.add(
                AutomationSpec(
                    test_case_id=case.id,
                    filename=spec_filename(case.ticket_external_id, case.code),
                    language="TypeScript",
                    framework="Playwright",
                    code=_SPEC_TEMPLATE.format(
                        title=case.title, code=case.code, ticket=case.ticket_external_id
                    ),
                    status="passed",
                )
            )
    return cases


def _build_links(db: Session, run: Run) -> None:
    """Insert a few linked test cases (linked=True) for the Link screen."""
    for i, (title, status) in enumerate(
        [
            ("Broker Management loads with two tabs", "Ready"),
            ("Agency row shows name, number, type, status", "Design"),
            ("Ellipsis on an Active agency offers Deactivate", "Design"),
        ]
    ):
        db.add(
            LinkedTestCase(
                run_id=run.id,
                ticket_external_id="SUR-1428",
                provider_kind="ado",
                external_id=str(4200 + i),
                title=title,
                status=status,
                url="https://dev.azure.com/surency/_workitems/edit/" + str(4200 + i),
                linked=True,
            )
        )


def _build_execution(db: Session, run: Run, cases: list[TestCase]) -> Execution:
    """Insert the execution + per-case results for the run's approved, automatable
    cases. Most results pass; exactly one fails (with an error + failure class) so
    the Evidence/Report screens have an interesting failure to show. Attaches
    placeholder screenshots (green for a pass, red for the failure) plus a small
    video/trace blob on the failing case.
    """
    automatable = [c for c in cases if c.approval == "approved" and c.automation != "Manual"]
    total = len(automatable)

    execution = Execution(
        run_id=run.id,
        owner_id=run.owner_id,
        status="done",
        target="server",
        env=run.env,
        browser=run.browser,
        workers=run.workers,
        total=total,
        passed=max(total - 1, 0),
        failed=1 if total else 0,
        progress=100,
        log="Running 12 tests using 4 workers\n  ✓ broker regression suite complete\n",
        started_at=utcnow(),
        finished_at=utcnow(),
    )
    db.add(execution)
    db.flush()

    # The failing case: SUR-1402 / TC-03 (confirm deactivation) if present, else
    # the last automatable case.
    fail_target = next(
        (c for c in automatable if c.ticket_external_id == "SUR-1402" and c.code == "TC-03"),
        automatable[-1] if automatable else None,
    )

    results: list[ExecutionResult] = []
    for idx, case in enumerate(automatable):
        is_fail = case is fail_target
        result = ExecutionResult(
            execution_id=execution.id,
            test_case_id=case.id,
            ticket_external_id=case.ticket_external_id,
            case_code=case.code,
            title=case.title,
            status="fail" if is_fail else "pass",
            failure_class="product_defect" if is_fail else "",
            duration_ms=2400 + idx * 350,
            error_message=(
                "expect(locator).toBeVisible() failed — the confirmation toast never "
                "appeared after Confirm was clicked."
                if is_fail
                else ""
            ),
        )
        db.add(result)
        results.append(result)
    db.flush()  # assign result ids for evidence

    # Evidence — a passing screenshot on the first pass, and a failing screenshot
    # (+ small video/trace blobs) on the failure.
    first_pass = next((r for r in results if r.status == "pass"), None)
    if first_pass is not None:
        store_uploaded_evidence(
            db, run, first_pass, "screenshot",
            _placeholder_png((34, 139, 87)), f"{first_pass.case_code}-pass.png",
        )
    failing = next((r for r in results if r.status == "fail"), None)
    if failing is not None:
        store_uploaded_evidence(
            db, run, failing, "screenshot",
            _placeholder_png((178, 34, 34)), f"{failing.case_code}-fail.png",
        )
        store_uploaded_evidence(
            db, run, failing, "video", b"\x00demo-video-placeholder",
            f"{failing.case_code}.webm",
        )
        store_uploaded_evidence(
            db, run, failing, "trace", b"\x00demo-trace-placeholder",
            f"{failing.case_code}-trace.zip",
        )
    return execution


def _build_report(db: Session, run: Run, execution: Execution) -> None:
    """Insert the run's Report row, reusing report_service's per-ticket summary
    aggregation (no Claude call — the AI failure narrative is a canned demo string).
    """
    results = list(execution.results)
    passed = sum(1 for r in results if r.status == "pass")
    failed = sum(1 for r in results if r.status == "fail")
    total = passed + failed
    pass_rate = round((passed / total) * 100, 1) if total else 0.0
    duration_s = round(sum(r.duration_ms for r in results) / 1000) if results else 0

    db.add(
        Report(
            run_id=run.id,
            execution_id=execution.id,
            overall_result="failed" if failed else "passed",
            pass_rate=pass_rate,
            passed=passed,
            failed=failed,
            duration_s=duration_s,
            env=execution.env,
            data={
                "ticketSummary": report_service._per_ticket_summary(
                    results, report_service.approved_case_counts(db, run.id)
                ),
                "aiFailureAnalysis": (
                    "One case failed on SUR-1402: the confirmation toast did not appear "
                    "after clicking Confirm. The failures look isolated to the deactivation "
                    "dialog rather than the list view. Suggested next step: verify the toast "
                    "component mounts on the confirm handler."
                ),
            },
        )
    )


def _build_comments(db: Session, run: Run) -> None:
    """Insert one publish comment per demo ticket for the Publish screen."""
    for tid in DEMO_TICKET_IDS:
        db.add(
            TicketComment(
                run_id=run.id,
                ticket_external_id=tid,
                provider_kind=_PROVIDER_KIND.get(tid, "ado"),
                body=(
                    f"Q-Agent executed the {run.code} regression for {tid}. "
                    "Results and evidence are attached."
                ),
                status="published",
                target_status="Ready for QA",
            )
        )


def ensure_sample_run(db: Session, user: User | None) -> Run:
    """Return the demo run for ``user``, seeding it (row graph) on first call.

    Idempotent: if a ``RUN-DEMO`` run already exists for ``user`` (matched by code
    alone when ``user`` is ``None``) it is returned unchanged. Otherwise this
    inserts the prerequisite rows (tickets, one knowledge base, one project
    config — only if absent) and a single terminal ``done`` run populated across
    every pipeline stage, then commits.

    Never calls the AI generation pipeline — every row is a direct insert.

    Args:
        db: Active session (committed here).
        user: The current user to own the seeded rows, or ``None`` (auth off).

    Returns:
        The demo ``Run`` (refreshed).
    """
    # ``Run.code`` is globally unique, so per-user demo runs need distinct codes
    # (a fixed "RUN-DEMO" would collide the moment a second user seeds one). Scope
    # the code by user id when authenticated; keep the bare code when auth is off
    # (``user is None`` — tests / single-tenant), where there is only ever one.
    demo_code = f"{DEMO_RUN_CODE}-{user.id}" if user is not None else DEMO_RUN_CODE

    existing = owned(db.query(Run), Run, user).filter(Run.code == demo_code).first()
    if existing is not None:
        return existing

    _ensure_prerequisites(db, user)

    run = Run(
        code=demo_code,
        name=DEMO_RUN_NAME,
        scope="selected",
        scope_label="Selected tickets",
        framework="Playwright",
        browser="chromium",
        env="Staging",
        workers=4,
        retry_policy=2,
        status="done",
        finished_at=utcnow(),
    )
    stamp_owner(run, user)
    db.add(run)
    db.flush()

    cases = _build_cases_and_specs(db, run)
    _build_links(db, run)
    execution = _build_execution(db, run, cases)
    _build_report(db, run, execution)
    _build_comments(db, run)

    db.commit()
    db.refresh(run)
    return run
