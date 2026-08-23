"""Automation generation router — Claude -> Playwright TypeScript specs.

Endpoints to implement:
  POST /runs/{run_id}/automation/generate   -> list[AutomationSpecOut]  (approved cases only)
  GET  /runs/{run_id}/automation            -> list[AutomationSpecOut]
  GET  /cases/{case_id}/spec                 -> AutomationSpecOut
  POST /cases/{case_id}/spec/regenerate      -> AutomationSpecOut

Generation writes real *.spec.ts files under workspace/specs/{run_code}/ and
persists AutomationSpec rows. Manual cases are skipped. Publishes WS progress.
"""

from __future__ import annotations

import json
import re
import threading
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session

from app import db as db_module
from app.config import settings
from app.db import get_db
from app.deps_auth import current_user
from app.logging import logger
from app.models.agent_device import AgentDevice
from app.models.automation_project import AutomationFile, AutomationProject
from app.models.execution import Execution, ExecutionResult
from app.models.run import Run, RunTicket
from app.models.testcase import AutomationSpec, TestCase
from app.models.ticket import Ticket
from app.models.user import User
from app.schemas import (
    AutomationExportRequest,
    AutomationSpecRegenerate,
    AutomationSpecUpdate,
    SpecChatRequest,
)
from app.services import (
    audit_service,
    automation_export_service,
    automation_gate,
    automation_planner_service,
    automation_project_service,
    live_authoring_service,
    page_object_author_service,
    placeholder_gate,
    playwright_runner,
    project_config_service,
    run_context,
    run_control,
    settings_store,
    spec_examples,
    spec_service,
)
from app.services.claude_cli import ClaudeError, run_json
from app.services.ownership import get_owned_or_404
from app.services.prompts import build_automation_review_prompt
from app.services.run_status import set_run_status
from app.services.skills import AUTOMATION_REVIEWER
from app.ws import hub

router = APIRouter(tags=["automation"])

# Run ids with an in-flight generation pass — lets the UI reflect the running
# state after navigating away/back, and prevents double-triggering generation.
_generating: set[int] = set()

# Case ids with an in-flight single-case regeneration (background thread) — guards
# against double-triggering the same case's regenerate.
_regenerating_cases: set[int] = set()

# Case ids with an in-flight AI-chat spec edit (background thread) — guards against
# double-triggering while Claude edits the same spec.
_chatting_cases: set[int] = set()


def forget_generating(run_id: int) -> None:
    """Clear the in-flight generation marker for a run (#420, on stop)."""
    _generating.discard(run_id)


def is_generating(run_id: int) -> bool:
    return run_id in _generating


def _get_case_and_run_or_404(
    db: Session, case_id: int, user: User | None
) -> tuple[TestCase, Run]:
    """Resolve a test case and its owning run.

    404s when the case is missing, or when the case's run is not owned by
    ``user`` (see ``app.services.ownership.get_owned_or_404``).
    """
    case = db.get(TestCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Test case not found")
    run = get_owned_or_404(db, Run, case.run_id, user)
    return case, run


def _eligible_cases_query(db: Session, run_id: int):
    """Approved, non-Manual test cases for a run — the automation-eligible set."""
    return (
        db.query(TestCase)
        .filter(
            TestCase.run_id == run_id,
            TestCase.approval == "approved",
            TestCase.automation != "Manual",
        )
        .order_by(TestCase.id)
    )


def _select_examples_for_case(db: Session, case: TestCase) -> list[dict]:
    """Pick up to 2 proven, already-passing specs from the SAME project + repo.

    Resolves the case's project key (via its ticket's provider) and target repo
    (via its RunTicket), then delegates to ``spec_examples.select_examples``. Purely
    best-effort grounding for generation — returns ``[]`` when nothing resolves.
    """
    ticket = db.query(Ticket).filter(Ticket.external_id == case.ticket_external_id).first()
    if ticket is None:
        return []
    project_key = project_config_service.project_key_for_ticket(db, ticket)
    if not project_key:
        return []
    run_ticket = (
        db.query(RunTicket)
        .filter(
            RunTicket.run_id == case.run_id,
            RunTicket.ticket_external_id == case.ticket_external_id,
        )
        .first()
    )
    repo = run_ticket.repo if run_ticket else ""
    return spec_examples.select_examples(db, project_key, repo, case, limit=2)


def _run_automation_review(code: str, case: TestCase, context: dict) -> dict | None:
    """Best-effort static review of a gate-passed spec via ``automation-reviewer`` (#181).

    Runs only after the deterministic placeholder/flaky-pattern gate has already
    passed a spec — this is the AI review stage that catches what regex
    heuristics can't (correctness against the case, reuse discipline, subtler
    flakiness). Additive and best-effort, matching the ``test-case-reviewer``
    wiring pattern (#173): any failure (Claude error, non-JSON response) is
    logged and skipped rather than blocking generation.

    Returns:
        The parsed ``{"verdict", "findings"}`` dict, or ``None`` if the review
        could not be obtained.
    """
    try:
        review = run_json(
            build_automation_review_prompt(code, case, context),
            skill=AUTOMATION_REVIEWER,
            label=f"Review spec: {case.ticket_external_id} {case.code}",
        )
    except Exception as exc:  # noqa: BLE001 - review is additive, best-effort
        logger.warning("Automation review skipped for case {}: {}", case.id, exc)
        return None
    return review if isinstance(review, dict) else None


def _review_critical_findings(review: dict) -> list[str]:
    """Critical-severity finding messages from an automation-reviewer verdict."""
    findings = review.get("findings")
    if not isinstance(findings, list):
        return []
    return [
        str(f.get("message", "") or "Critical finding")
        for f in findings
        if isinstance(f, dict) and str(f.get("severity", "")).strip().lower() == "critical"
    ]


def _gate_spec_or_bypass(
    code: str,
    known: dict,
    owner_id: int,
    *,
    noun: str,
    fix_verb: str,
    project: AutomationProject | None = None,
) -> tuple[dict, str]:
    """Run the spec quality gate, or bypass it when the global toggle is off.

    Shared by every spec-acceptance path (generation, manual edit, chat edit) so
    they gate identically. When the workspace ``gateEnabled`` setting is off
    (#gate-toggle) gating is skipped entirely — the placeholder/invented-reference
    gate AND the ``playwright --list`` parse check — and the spec is accepted as
    runnable via :func:`placeholder_gate.bypassed_result`. (The AI automation-reviewer
    is skipped by the caller on a bypassed result.) Otherwise runs the deterministic
    gate followed by the parse check.

    Args:
        code: The spec source to gate.
        known: KB view (routes/selectors/base_url) the gate compares against.
        owner_id: Run owner id, for the per-user ``playwright --list`` invocation.
        noun: "generated spec" / "edited spec" — used in the parse-failure reason.
        fix_verb: "Regenerate" / "Fix" — used in the parse-failure unblock action.
        project: When given, the parse check becomes
            ``automation_gate.list_ok_in_project`` over the **whole** project
            (#540) instead of the legacy single-spec-in-an-empty-temp-dir check.
            ``code`` must already have been written into the project tree — the
            project gate collects what is on disk, which is the entire point:
            imports resolve because the files genuinely exist, and a page-object
            edit that breaks *another* case's spec fails collection here.
            ``None`` keeps the legacy path byte-for-byte for
            ``project_id IS NULL`` specs. A project also enables the second static
            gate, ``automation_gate.typecheck_ok`` (#546), run after the cheaper
            collection check.

    Returns:
        ``(gate_report, outcome)`` where outcome is ``passed`` | ``blocked`` | ``rejected``.
    """
    if not settings_store.gate_enabled():
        return placeholder_gate.bypassed_result(), "passed"
    gate = placeholder_gate.gate_spec(code, known)
    outcome = gate["outcome"]
    # A spec Playwright cannot even parse/collect is treated like a rejection
    # (best-effort: an unavailable CLI/timeout skips the check, never blocks).
    if outcome == "passed":
        if project is not None:
            project_root = automation_project_service.project_dir(project)
            list_ok, detail = automation_gate.list_ok_in_project(
                project_root, automation_gate.test_titles(code)
            )
        else:
            project_root = None
            list_ok = spec_service.playwright_list_ok(code, owner_id)
            detail = "playwright --list parse failure"
        if not list_ok:
            outcome = "rejected"
            gate = {
                "outcome": "rejected",
                "findings": [detail],
                "reason": f"Playwright could not parse/collect the {noun}.",
                "unblock_action": f"{fix_verb} the spec so it parses cleanly under Playwright.",
            }
        elif project_root is not None:
            # `--list` transpiles with esbuild, which erases types without checking
            # them — a misspelled page-object method or a wrong argument shape
            # collects cleanly. Only `tsc --noEmit` sees those (#546). Second, after
            # the cheaper collection check, and only for project-backed specs: the
            # legacy single-spec-in-a-temp-dir path has no tsconfig to check against.
            types_ok, type_detail = automation_gate.typecheck_ok(project_root)
            if not types_ok:
                outcome = "rejected"
                gate = {
                    "outcome": "rejected",
                    "findings": [type_detail],
                    "reason": f"TypeScript rejected the {noun}.",
                    "unblock_action": (
                        f"{fix_verb} the spec so it typechecks against the project's "
                        "page objects and fixtures."
                    ),
                }
    return gate, outcome


def _resolve_automation_project(
    db: Session, run: Run, context: dict, existing: AutomationSpec | None
) -> AutomationProject | None:
    """The persistent automation project this case's spec belongs in, or None (#540).

    ``None`` means "take the legacy path" — write to the per-run throwaway dir and
    gate the spec alone in an empty temp dir, exactly as before #540. Three ways
    that happens, in priority order:

    1. **An existing spec with ``project_id IS NULL`` and code stays legacy.**
       Every spec that predates #540 looks like this, and the epic's contract is
       that those keep generating, gating and executing unchanged for the lifetime
       of their runs. A row that exists but is still empty (e.g. a queued
       live-authoring placeholder) is treated as new.
    2. **No project key resolves** (no ticket / no provider / no project config),
       which is the case for most of the existing test suite. There is nothing to
       key a project off, so there is no project.
    3. Otherwise the project is get-or-created for
       ``(run.owner_id, projectKey, repo)``, and a spec already bound to a project
       keeps that binding.

    ``ensure_deps`` is best-effort by contract: ``"unavailable"`` (npm missing,
    registry down, no vendored tarball) is logged and generation continues. The
    project-aware gate is fail-open, so a project with no ``node_modules`` degrades
    to "gate skipped", never to "generation hard-fails".
    """
    if existing is not None and existing.project_id is None and (existing.code or "").strip():
        return None
    if existing is not None and existing.project_id is not None:
        return db.get(AutomationProject, existing.project_id)
    project_key = (context.get("projectKey") or "").strip()
    if not project_key:
        return None
    project = automation_project_service.ensure_project(
        db, run.owner_id, project_key, (context.get("repo") or "").strip()
    )
    deps = automation_project_service.ensure_deps(project)
    if deps == "unavailable":
        logger.warning(
            "Automation project {} has no installed deps — the project gate will skip "
            "(generation continues)",
            project.id,
        )
    return project


def _plan_for_case(
    db: Session,
    run: Run,
    case: TestCase,
    context: dict,
    project: AutomationProject | None,
    *,
    force: bool = False,
) -> dict | None:
    """The Automation Plan governing this case, planned ONCE per ticket (#544).

    Planning is per **feature**, not per case — the main cost lever of Wave 3 — so
    the plan is keyed on ``(run, ticket)`` and every automation-eligible case on the
    ticket is handed to the planner together. The on-disk plan file is the cache, so
    the second case of a ticket costs no Claude call at all.

    Returns ``None`` on the legacy path (no persistent project): with no project
    there is no inventory, so there is nothing to reuse and nothing to authorize.
    Never raises — ``plan_for_ticket`` degrades to an empty plan on any failure, and
    an empty plan authorizes no imports, i.e. exactly the pre-#544 behaviour.
    """
    if project is None:
        return None
    return automation_planner_service.plan_for_ticket(
        project,
        run.code,
        case.ticket_external_id,
        _ticket_cases(db, run, case),
        context,
        force=force,
    )


def _ticket_cases(db: Session, run: Run, case: TestCase) -> list[TestCase]:
    """Every automation-eligible case on ``case``'s ticket, including ``case`` itself.

    The unit of both planning (#544) and asset authoring (#545) is the **feature**,
    not the case, so both need the whole ticket in one call — five cases on one
    screen share one page object, and per-case calls would invent five.
    """
    cases = (
        _eligible_cases_query(db, run.id)
        .filter(TestCase.ticket_external_id == case.ticket_external_id)
        .all()
    )
    if not any(c.id == case.id for c in cases):
        # A regenerate of a case that is no longer "eligible" (e.g. approval
        # changed under us) still needs a plan covering it.
        cases = [*cases, case]
    return cases


def _author_plan_assets(
    db: Session,
    run: Run,
    case: TestCase,
    context: dict,
    project: AutomationProject | None,
    plan: dict | None,
    plans: dict[str, dict] | None,
) -> dict | None:
    """Apply the plan's ``create``/``extend`` actions, then return the REFRESHED plan.

    #545 introduced this on the blind generation branch only; since #569 it runs on
    **every** branch of :func:`_generate_one` — blind, server live-harness,
    local-agent live-harness and the agent post-back — because the asset library must
    grow in whichever authoring mode a workspace happens to be set to, and #548's
    reuse metric is only comparable across modes if it does.

    It stays **server-side** on purpose (issue #569, option (a)): the paired device
    has no persistent project (#541), so the canonical tree keeps a single writer and
    the device simply receives a project whose planned page objects already exist —
    no wire-protocol change, no agent release.

    All three of #545's defences come along unchanged, because this is literally
    :func:`page_object_author_service.author_assets`: whole-project
    ``playwright test --list``, ``diff_is_additive``, and a ``git reset --hard``
    rollback of any rejection. A reuse-only plan still makes **no** agentic authoring
    call (the cost control), and a rejected/failed pass returns the plan unchanged so
    the branch degrades to inline locators exactly as it did before.
    """
    if project is None or plan is None:
        return plan
    plan, authoring_report = page_object_author_service.author_assets(
        db,
        project,
        run.code,
        case.ticket_external_id,
        plan,
        _ticket_cases(db, run, case),
        context,
        run_id=run.id,
    )
    if plans is not None:
        plans[case.ticket_external_id] = plan
    if authoring_report["ran"] and not authoring_report["ok"]:
        logger.warning(
            "Asset authoring for {} did not land ({}) — this spec falls back to "
            "inline locators",
            case.ticket_external_id, authoring_report["reason"],
        )
    return plan


def _plan_rejection(gate: dict, violations: list[str], *, what: str) -> tuple[dict, str]:
    """Turn a plan violation into a gate rejection (#544).

    The plan is not advice: it enumerates the exact files generation may import and
    the exact paths it may write, so ignoring it is a rejection like any other gate
    failure. Recorded inside ``gate_report`` so it surfaces on the Automation screen
    next to every other rejection reason, with no new UI path.
    """
    gate = dict(gate)
    gate["outcome"] = "rejected"
    gate["planViolations"] = violations
    gate["reason"] = f"The generated spec {what}: {', '.join(violations[:6])}."
    gate["unblock_action"] = (
        "Regenerate — the automation plan lists the only assets this spec may use; "
        "anything else must stay inline until a later stage authors it."
    )
    return gate, "rejected"


def _gate_into_project(
    db: Session,
    project: AutomationProject,
    case: TestCase,
    code: str,
    known: dict,
    owner_id: int,
    *,
    noun: str,
    fix_verb: str,
    review=None,
    plan: dict | None = None,
) -> tuple[dict, str, str]:
    """Write ``code`` into the project, gate it there, commit on pass / reset on fail.

    This is the project-backed replacement for "gate the code, then maybe write the
    file". The order has to invert: the project gate collects what is **on disk**,
    so the candidate must be written before it can be listed. Git is what makes
    that safe — it turns "don't write a bad spec" into "write it, then roll the
    whole tree back", which also extends the pre-#540 ``has_previous_good``
    contract from one spec's code to every file in the project.

    The whole write → gate → review → commit/reset section runs under
    ``project_lock``. That deliberately serializes two runs generating into the
    *same* project (including across the automation-reviewer's Claude call, which
    is slow): the gate lists the entire tree, so a concurrent write would both
    corrupt the listing and make the rollback point meaningless.

    Args:
        review: Optional ``callable(gate) -> (gate, outcome)`` applied only when the
            deterministic + list gate passed — the ``automation-reviewer`` stage,
            which can still flip the outcome to ``rejected``.
        plan: The ticket's Automation Plan (#544). When present it **constrains** the
            write: a library file that appeared but the plan never marked ``writable``
            is a rejection, which is the literal form of "a case whose plan says
            ``reuse`` must not produce a new file". ``None`` skips the check.

    Returns:
        ``(gate_report, outcome, path)`` where ``path`` is the absolute written
        path on ``passed`` and ``""`` otherwise (the tree was rolled back).
    """
    with automation_project_service.project_lock(project):
        # Commit whatever is currently in the tree so the rollback point includes
        # any legitimate prior state, then remember it.
        automation_project_service.git_commit(
            project, f"chore: pre-generation state for {case.ticket_external_id} {case.code}"
        )
        pre_state = automation_project_service.head_commit(project) or "HEAD"
        before_paths = [e["path"] for e in automation_project_service.inventory(project)]
        path = automation_project_service.write_spec(
            project, case.ticket_external_id, case.code, code
        )
        gate, outcome = _gate_spec_or_bypass(
            code, known, owner_id, noun=noun, fix_verb=fix_verb, project=project
        )
        if outcome == "passed" and plan is not None:
            unplanned = automation_planner_service.unplanned_new_paths(
                before_paths,
                [e["path"] for e in automation_project_service.inventory(project)],
                plan,
            )
            if unplanned:
                gate, outcome = _plan_rejection(
                    gate, unplanned, what="created shared files the plan did not authorize"
                )
        if outcome == "passed" and review is not None:
            gate, outcome = review(gate)
        if outcome == "passed":
            automation_project_service.git_commit(
                project, f"feat({case.ticket_external_id}): spec for {case.code}"
            )
            automation_project_service.sync_files_to_db(db, project)
            return gate, outcome, str(path)
        # blocked/rejected: roll the WHOLE tree back, so a bad candidate cannot
        # leave debris and previously-good specs are untouched. `git_reset_hard`
        # also runs `git clean -qfd`, which removes the just-written file even
        # though it was never tracked.
        automation_project_service.git_reset_hard(project, pre_state)
        automation_project_service.sync_files_to_db(db, project)
        return gate, outcome, ""


def _project_spec_relpath(project: AutomationProject, case: TestCase) -> str:
    """A case's spec path relative to its project root, e.g.
    ``"tests/SUR-1428/SUR-1428-TC-01.spec.ts"``.

    Derived from ``automation_project_service.spec_dir`` rather than rebuilt, so the
    ``tests/<TICKET>/`` convention lives in exactly one place. This is what lands in
    ``AutomationSpec.filename`` for a project-backed spec — including a blocked one,
    whose code deliberately never reaches the project tree but which execution
    staging can still materialize from the row on an explicit "run anyway".
    """
    root = automation_project_service.project_dir(project)
    directory = automation_project_service.spec_dir(project, case.ticket_external_id)
    filename = spec_service.spec_filename(case.ticket_external_id, case.code)
    return (directory.relative_to(root) / filename).as_posix()


def _gate_edit(
    db: Session,
    run: Run,
    case: TestCase,
    spec: AutomationSpec,
    code: str,
    known: dict,
    *,
    noun: str,
    fix_verb: str,
) -> tuple[dict, str, str]:
    """Gate a hand/chat-edited spec in whichever home it already lives in.

    A project-backed spec **must** be re-gated against its project: gating an edit
    of a layered spec with the legacy single-file gate would reject the very
    imports the project makes legal, so an edit that changed nothing meaningful
    would blow the spec up. Legacy specs keep the legacy gate.

    Returns:
        ``(gate_report, outcome, project_path)`` — ``project_path`` is ``""`` for a
        legacy spec (the caller writes the per-run file itself) or when the edit
        did not pass.
    """
    project = db.get(AutomationProject, spec.project_id) if spec.project_id else None
    if project is None:
        gate, outcome = _gate_spec_or_bypass(
            code, known, run.owner_id, noun=noun, fix_verb=fix_verb
        )
        return gate, outcome, ""
    return _gate_into_project(
        db, project, case, code, known, run.owner_id, noun=noun, fix_verb=fix_verb
    )


def _apply_automation_review(
    gate: dict, code: str, case: TestCase, context: dict
) -> tuple[dict, str]:
    """The ``automation-reviewer`` stage (#181), applied to a gate-passed spec.

    Additive and best-effort: a Critical finding (or a ``reject`` verdict) is
    treated like a gate rejection; the verdict/findings are persisted in
    ``gate_report`` either way so they surface on the automation screen. A
    failed/unparseable review never blocks a spec the deterministic gate already
    passed, and the whole stage is skipped when gating is bypassed (the toggle
    turns off the review too).

    Returns:
        ``(gate_report, outcome)`` — ``("passed")`` unchanged, or a rejection.
    """
    if gate.get("bypassed"):
        return gate, "passed"
    review = _run_automation_review(code, case, context)
    if review is None:
        return gate, "passed"
    gate = dict(gate)
    gate["review"] = review
    critical = _review_critical_findings(review)
    if not critical and str(review.get("verdict", "")).strip().lower() != "reject":
        return gate, "passed"
    gate["outcome"] = "rejected"
    gate["reason"] = (
        "automation-reviewer flagged Critical findings: " + "; ".join(critical[:6])
        if critical
        else "automation-reviewer verdict was reject."
    )
    gate["unblock_action"] = "Address the review findings above and regenerate."
    return gate, "rejected"


def _merge_authored_discovery(context: dict, run: Run, discovered: dict) -> None:
    """Merge an agent-authored run's runtime-verified discovery into the KB (#403).

    Reuses :func:`live_authoring_service.merge_discovery_to_kb` (source
    ``live-authoring``, no-clobber) by wrapping the discovery + resolved
    project/repo/owner in an ``AuthoringResult``.
    """
    from app.services.live_authoring_service import AuthoringResult, merge_discovery_to_kb

    merge_discovery_to_kb(
        AuthoringResult(
            ok=True,
            code="",
            discovered=discovered,
            project_key=context.get("projectKey"),
            repo=context.get("repo", "") or "",
            owner_id=run.owner_id,
        )
    )


def _get_or_create_spec(db: Session, case_id: int, *, filename: str) -> AutomationSpec:
    """Return the case's AutomationSpec row, inserting it if it doesn't exist yet.

    ``AutomationSpec.test_case_id`` is ``unique=True``, and a plain
    query-then-insert is a check-then-act race (#604): two generation passes on
    two sessions can both read "no spec" and both insert, and the loser dies with
    ``UNIQUE constraint failed: automation_specs.test_case_id``. The window is
    wide in practice because :func:`_generate_one` reads ``existing`` *before*
    generation (several Claude calls) and inserts *after* it, and it is reachable:
    the per-run guard (``_generating``) and the per-case one
    (``_regenerating_cases``) don't guard each other, so a run-wide generate and a
    single-case regenerate can overlap on the same case.

    So the DB — not a prior read — is the arbiter: the insert goes in a SAVEPOINT
    and a UNIQUE violation is resolved by adopting the row the winner committed.
    The loser then updates that row, which is the same last-writer-wins outcome as
    two *sequential* regenerations. Leaves the caller's transaction usable either
    way (a bare ``rollback()`` would discard the caller's other pending work).

    ``filename`` is required because the insert is flushed here to provoke the
    constraint, and ``AutomationSpec.filename`` is NOT NULL; callers overwrite it
    (along with the rest of the row) right after.
    """
    spec = db.query(AutomationSpec).filter(AutomationSpec.test_case_id == case_id).first()
    if spec is not None:
        return spec
    savepoint = db.begin_nested()
    spec = AutomationSpec(test_case_id=case_id, filename=filename)
    db.add(spec)
    try:
        db.flush()
    except IntegrityError:
        savepoint.rollback()
        spec = db.query(AutomationSpec).filter(AutomationSpec.test_case_id == case_id).first()
        if spec is None:
            # Not the concurrent-insert case — some other integrity problem.
            raise
        logger.info("Adopted concurrently-created spec row for case {}", case_id)
        return spec
    savepoint.commit()
    return spec


def _enqueue_agent_authoring(
    db: Session,
    run: Run,
    case: TestCase,
    context: dict,
    heal: dict | None = None,
    plan: dict | None = None,
) -> AutomationSpec:
    """Queue a live-authoring session for the paired agent and return a pending spec (#403).

    Composes the skill system prompt + task prompt server-side (the agent has no
    ``skills/`` dir), enqueues via :mod:`agent_authoring_service`, and marks the
    spec ``running``; the agent claims it, authors locally, and the finalize
    endpoint fills in the real spec via :func:`finalize_authored_spec`.

    When ``heal`` (``{"code": <failing spec>, "error": <failure>}``) is given the
    task prompt is framed as a self-heal (#428) — reproduce + fix the failing spec
    live — instead of authoring from scratch. Same job shape, so the agent runs it
    with no changes.

    ``plan`` (#569) is the ticket's Automation Plan, already **acted on** server-side
    by the caller: the paired device therefore receives a project whose planned page
    objects exist, and the rendered plan block rides in on the existing
    ``task_prompt`` field. That is deliberate — nothing about the wire shape changes,
    so no agent release is needed to make live-harness reuse the library.
    """
    from app.services import agent_authoring_service, agent_capture_service, skills

    base_url = (context.get("baseUrl") or "").strip()
    if not base_url:
        raise ValueError("No base URL in the project context — configure it before live authoring.")
    has_device = (
        db.query(AgentDevice)
        .filter(AgentDevice.owner_id == run.owner_id, AgentDevice.revoked_at.is_(None))
        .first()
        is not None
    )
    if not has_device:
        raise ValueError("No local agent paired — start your local agent to author live.")

    spec_filename = spec_service.spec_filename(case.ticket_external_id, case.code)
    system_prompt = skills.load_skill("live-authoring", include_template=True) or ""
    task_prompt = live_authoring_service._build_prompt(
        case, context, spec_filename, "discovered.json", base_url, heal=heal, plan=plan
    )
    model = settings_store.load_settings().get("claudeModel") or settings.claude_model
    agent_authoring_service.request_authoring(
        uuid4().hex,
        owner_id=run.owner_id,
        project_key=context.get("projectKey") or "",
        repo=context.get("repo", "") or "",
        base_url=base_url,
        origin=agent_capture_service.origin_of(base_url),
        case_id=case.id,
        run_id=run.id,
        spec_filename=spec_filename,
        system_prompt=system_prompt,
        task_prompt=task_prompt,
        model=model,
        max_budget_usd=settings_store.authoring_cost_budget_usd(),
        log_verbosity=settings_store.load_settings().get("authoringLogVerbosity", "concise"),
    )

    spec = _get_or_create_spec(db, case.id, filename=spec_filename)
    spec.filename = spec_filename
    spec.language = "TypeScript"
    spec.framework = "Playwright"
    spec.status = "running"
    spec.block_reason = ""
    return spec


def finalize_authored_spec(
    db: Session, run_id: int, case_id: int, code: str, discovered: dict
) -> AutomationSpec | None:
    """Persist an agent-authored spec via the shared gate/write path (#403).

    Called from the ``/agent/authoring/{id}/finalize`` endpoint. Runs the same
    gate → write → persist tail as blind/server-live generation by feeding the
    authored code + discovery through :func:`_generate_one`, then streams the
    result to the run WebSocket. Returns the persisted spec (or None if the run/
    case vanished).
    """
    run = db.get(Run, run_id)
    case = db.get(TestCase, case_id)
    if run is None or case is None:
        return None
    # Note: we intentionally do NOT drop the post-back for a terminal/cancelled run.
    # Persisting the finished spec never advances the run status (authoring doesn't
    # touch run.status), so it can't "resurrect" a stopped run — and dropping it
    # instead left the spec stuck at status="running" forever, keeping the UI trail
    # spinning after the agent had already written the spec (#440-followup). A run
    # that was properly stopped has its authoring session purged, so this endpoint
    # 404s before reaching here; this path only runs for a genuinely finished job.
    run_context.set_run(run_id)
    try:
        spec = _generate_one(db, run, case, authored={"code": code, "discovered": discovered})
        db.commit()
        db.refresh(spec)
    finally:
        run_context.clear()
    hub.publish(str(run_id), "spec.regenerated", {"caseId": case_id, "spec": _spec_out(spec)})
    return spec


def _generate_one(
    db: Session,
    run: Run,
    case: TestCase,
    reviewer_comment: str | None = None,
    authored: dict | None = None,
    plans: dict[str, dict] | None = None,
) -> AutomationSpec:
    """Generate (or regenerate) and persist the AutomationSpec for one case.

    Generation is grounded with few-shot examples (proven passing specs from the
    same project) and gated by the placeholder / invented-reference / flaky-pattern
    gate plus a best-effort ``playwright --list`` parse check before the spec is
    accepted. A gate-passed spec then gets a best-effort static review from
    ``automation-reviewer`` (#181); a Critical finding flips the outcome to
    ``rejected`` just like the deterministic gate would:

    - ``passed``   -> write the file, ``status="draft"`` (runnable).
    - ``blocked``  -> save the row ``status="blocked"`` with ``block_reason``; the
                      file is NOT written, so a blocked spec never enters the
                      runnable file set.
    - ``rejected`` -> keep any previous good spec untouched (code/path/status),
                      recording the rejection in ``gate_report``. With no previous
                      spec, save ``status="blocked"`` (still not runnable).

    Since #540 there are two homes for the accepted file, chosen by
    :func:`_resolve_automation_project`:

    * **Project-backed** — the spec is written to
      ``<project>/tests/<TICKET>/<TICKET>-<TC>.spec.ts``, gated against the whole
      project (so shared page objects and ``@q-agent/playwright-base`` imports
      resolve), and committed; a blocked/rejected candidate rolls the tree back
      with ``git reset --hard``. ``spec.project_id`` is set and ``spec.filename``
      becomes the project-relative path.
    * **Legacy** (``project_id IS NULL``) — the pre-#540 behaviour, unchanged:
      per-run throwaway dir, spec gated alone in an empty temp dir.

    Since #544 a project-backed case is also **planned** before it is generated:
    ``automation_planner_service`` decides REUSE > EXTEND > CREATE once per ticket, the
    plan is persisted to ``plan_report`` (and to ``.qagent/plans/``), and the plan is
    what authorizes the spec's asset imports and constrains the paths it may write.

    Since #545 the plan is also **acted on** before generation: when it contains
    ``create``/``extend`` actions, ``page_object_author_service`` authors those page
    objects/components/fixtures/data in the project (behind three stacked defences and
    a git rollback) and returns a plan whose ``importable`` includes them — so the spec
    imports real files instead of inlining locators. A reuse-only plan makes no
    agentic call, which is the slice's cost control.

    Since #569 that happens on **every** branch, not just the blind one:
    :func:`_author_plan_assets` runs before the mode/target fan-out, so server
    live-harness gets the rendered plan in its live prompt, the local-agent branch
    hands the paired device a project whose planned page objects already exist, and the
    post-back reuses that same cached plan. Authoring stays server-side (the device has
    no persistent project, #541), which is why this needs no wire change and no agent
    release.

    Args:
        db: Active session (caller commits).
        run: The owning Run (provides run.code for the spec path).
        case: The approved, non-Manual TestCase to generate a spec for.
        reviewer_comment: Optional free-text note (from a per-case regenerate)
            injected into the generation prompt as reviewer guidance. The gate
            still runs unchanged, so a comment can never bypass quality gating.
        plans: Optional ``{ticket_external_id: plan}`` accumulator owned by the
            caller (``_run_generation``), so a whole pass's plans can be rolled up
            into one reuse/extend/create log line.

    Returns:
        The created or updated AutomationSpec row (not yet committed).
    """
    context = spec_service.build_case_context(db, case, env=run.env)
    # Resolved up-front (not after generation) because the project must exist and
    # have its deps installed before the candidate can be written into it and gated
    # there. `None` -> the legacy per-run path, unchanged.
    existing = db.query(AutomationSpec).filter(AutomationSpec.test_case_id == case.id).first()
    project = _resolve_automation_project(db, run, context, existing)
    # Authoring mode (#400): "live-harness" drives the real app via browser-harness
    # to author from live-verified selectors; "blind" (default) generates from the
    # KB and relies on the heal loop. The paths differ only in where `code` comes
    # from — the gate/write/persist below is shared.
    stored = settings_store.load_settings()
    mode = stored.get("authoringMode", "blind")
    exec_target = stored.get("executionTarget", "server")
    # Plan BEFORE generating (doc §8/§24) — the plan is an input to generation, not a
    # report on it. Once per ticket; cached on disk for the ticket's other cases.
    plan = _plan_for_case(db, run, case, context, project)
    if plan is not None and plans is not None:
        plans.setdefault(case.ticket_external_id, plan)
    # (#569) Author the plan's create/extend assets HERE — before any branch hands the
    # work off — so all four branches get a project whose planned page objects exist
    # and a refreshed plan whose `importable` names them. Cheap to reach on the
    # local-agent branch: planning and authoring are both cached per ticket, so the
    # post-back's own `_generate_one` pass re-uses this pass's work rather than paying
    # again.
    plan = _author_plan_assets(db, run, case, context, project, plan, plans)
    if mode == "live-harness" and exec_target == "local-agent" and authored is None:
        # Nothing is generated here — the job is handed to the paired agent (with the
        # rendered plan in its task prompt) and comes back through
        # `finalize_authored_spec`, which runs the shared gate/write tail.
        return _enqueue_agent_authoring(db, run, case, context, plan=plan)
    live_discovered: dict | None = None
    if authored is not None:
        # (#403) A paired agent authored this spec live and posted it back; the
        # code + runtime-verified discovery are already produced. Merge discovery
        # into the KB and fold it into the gate's `known` set below.
        code = authored.get("code") or ""
        live_discovered = authored.get("discovered") or {"routes": [], "selectors": []}
        _merge_authored_discovery(context, run, live_discovered)
    elif mode == "live-harness":
        # (#403) On local-agent, browser-harness runs where Claude runs, so this pass
        # was already handed to the paired agent above and never reaches here.
        result = live_authoring_service.author_case(
            db, case, run, owner_id=run.owner_id, run_id=run.id, plan=plan
        )
        code = result.code
        live_authoring_service.merge_discovery_to_kb(result)
        live_discovered = result.discovered
    else:
        # (#545/#569) The plan's create/extend assets were authored above, before this
        # branch — generation is handed the REFRESHED plan, whose `importable` includes
        # the page objects just written, so the spec imports real files instead of
        # inlining locators.
        examples = _select_examples_for_case(db, case)
        code = spec_service.generate_spec_code(
            case, context, examples=examples, reviewer_comment=reviewer_comment, plan=plan
        )
    filename = spec_service.spec_filename(case.ticket_external_id, case.code)

    spec = existing
    if spec is None:
        # Re-resolved against the DB, not the read taken before generation: another
        # pass may have inserted this case's row in the meantime (#604).
        spec = _get_or_create_spec(db, case.id, filename=filename)
    # "Good" means a genuinely runnable prior spec worth protecting from a rejected
    # regeneration — NOT merely "has code". A previously *blocked* spec is not good:
    # freezing it would discard every new attempt, so a rejected regen on a blocked
    # spec should replace it (visible iteration + a diff to review), while a passing
    # spec is still kept when a regen comes back rejected.
    #
    # Read off the *resolved* row, not the pre-generation `existing`: when a
    # concurrent pass created the row (#604) its spec is a previous one too, and
    # judging it "not good" would let a rejected regen clobber it. A row this call
    # just inserted has no code, so it is correctly not good.
    has_previous_good = bool((spec.code or "").strip() and spec.status != "blocked")
    spec.filename = filename
    spec.language = "TypeScript"
    spec.framework = "Playwright"
    # Persisted on every spec of the ticket, mirroring the `gate_report`/`heal_report`
    # convention, so the UI renders it beside them with no new endpoint (#544). Only a
    # plan that decided something is stored — an empty one (planning failed) would
    # render as a husk and, worse, read as "nothing to reuse" when the truth is
    # "nothing was asked".
    if automation_planner_service.is_actionable(plan):
        spec.plan_report = json.dumps(plan)

    # Build the KB view the gate compares against (accepts raw KB shapes directly).
    # In live-harness mode, add the runtime-verified routes/selectors just
    # discovered so the gate doesn't reject the real selectors as invented.
    known = {
        "routes": list(context.get("routes", [])) + (live_discovered or {}).get("routes", []),
        "selectors": list(context.get("selectors", [])) + (live_discovered or {}).get("selectors", []),
        "base_url": context.get("baseUrl", ""),
    }
    project_path = ""
    if project is not None:
        # Project-backed: write into the project first (the gate lists what is on
        # disk), gate the whole tree, then commit or `git reset --hard` back.
        spec.project_id = project.id
        spec.filename = _project_spec_relpath(project, case)

        def _review_then_plan(gate: dict) -> tuple[dict, str]:
            """automation-reviewer, then the plan's import constraint.

            Ordered after the reviewer so a spec that is wrong on the merits is
            reported as such; the plan check is the last word because it is the one
            that decides whether the spec's imports can even exist.
            """
            gate, outcome = _apply_automation_review(gate, code, case, context)
            if outcome != "passed":
                return gate, outcome
            violations = automation_planner_service.import_violations(code, plan)
            if violations:
                return _plan_rejection(
                    gate, violations, what="imports assets the plan does not authorize"
                )
            return gate, outcome

        gate, outcome, project_path = _gate_into_project(
            db,
            project,
            case,
            code,
            known,
            run.owner_id,
            noun="generated spec",
            fix_verb="Regenerate",
            review=_review_then_plan,
            plan=plan,
        )
    else:
        gate, outcome = _gate_spec_or_bypass(
            code, known, run.owner_id, noun="generated spec", fix_verb="Regenerate"
        )
        if outcome == "passed":
            gate, outcome = _apply_automation_review(gate, code, case, context)

    if outcome == "blocked":
        # Missing-input: persist the generated code + reason but never write the
        # file, so a blocked spec is not part of the runnable set.
        spec.code = code
        spec.status = "blocked"
        spec.block_reason = f'{gate["reason"]} {gate["unblock_action"]}'.strip()
        spec.gate_report = json.dumps(gate)
        return spec

    if outcome == "rejected":
        spec.gate_report = json.dumps(gate)
        if has_previous_good:
            # Keep the previous good spec: leave code/path/status untouched.
            return spec
        # No previous spec to fall back on — save non-runnable, noting the rejection.
        spec.code = code
        spec.status = "blocked"
        spec.block_reason = f'Rejected: {gate["reason"]} {gate["unblock_action"]}'.strip()
        return spec

    # passed — accept and write the runnable spec file. Project-backed specs were
    # already written (and committed) inside `_gate_into_project`.
    path = project_path or str(
        spec_service.write_spec_file(
            run.code, case.ticket_external_id, case.code, code, run.owner_id
        )
    )
    spec.code = code
    spec.path = path
    spec.status = "draft"
    spec.block_reason = ""
    spec.gate_report = json.dumps(gate)
    return spec


def _run_generation(run_id: int, force: bool = False) -> None:
    """Background worker: generate specs for eligible cases in a run.

    Args:
        run_id: The run whose approved, non-Manual cases to generate specs for.
        force: When False (default) only cases that don't yet have an
            AutomationSpec are generated, so previously generated — and possibly
            hand-edited — specs are preserved. When True every eligible case is
            (re)generated, overwriting existing specs.
    """
    # Attribute this thread's Claude spend to the run (see run_context).
    run_context.set_run(run_id)
    db = db_module.SessionLocal()
    try:
        run = db.get(Run, run_id)
        if run is None:
            return
        try:
            cases = _eligible_cases_query(db, run_id).all()
            if not force:
                existing_case_ids = {
                    case_id
                    for (case_id,) in db.query(AutomationSpec.test_case_id)
                    .join(TestCase, AutomationSpec.test_case_id == TestCase.id)
                    .filter(TestCase.run_id == run_id)
                    .all()
                }
                cases = [c for c in cases if c.id not in existing_case_ids]
                # Evict any stale queued live-authoring sessions for cases that
                # already have a spec, so the agent can't re-author them (#419).
                from app.services import agent_authoring_service
                agent_authoring_service.drop_queued_cases(existing_case_ids)
            total = len(cases)
            cancelled = False
            # One plan per ticket in this pass, collected so the pass can be logged
            # as a single reuse/extend/create tally — the epic's success metric (#544).
            pass_plans: dict[str, dict] = {}
            for index, case in enumerate(cases, start=1):
                if run_control.is_cancelled(run_id, db):
                    logger.info("Run {} cancelled — stopping automation generation", run.code)
                    cancelled = True
                    break
                try:
                    spec = _generate_one(db, run, case, plans=pass_plans)
                    db.commit()
                    hub.publish(
                        str(run_id),
                        "automation.progress",
                        {"file": spec.filename, "message": "Generated", "done": index, "total": total},
                    )
                except Exception as exc:  # noqa: BLE001 - surface per-case, never abort the pass
                    db.rollback()
                    logger.error("Automation generation failed for case {}: {}", case.id, exc)
                    hub.publish(
                        str(run_id),
                        "automation.progress",
                        {
                            "file": spec_service.spec_filename(case.ticket_external_id, case.code),
                            "message": f"Error: {exc}",
                            "done": index,
                            "total": total,
                        },
                    )
            if pass_plans:
                automation_planner_service.log_pass_counts(run.code, pass_plans.values())
            # Flip the run to 'automation' and announce it — unless cancelled
            # mid-pass, in which case the cancel path's terminal status stands.
            if not cancelled:
                set_run_status(db, run, "automation")
        except Exception as exc:  # noqa: BLE001 - never crash the worker thread silently
            logger.error("Automation generation crashed for run {}: {}", run.code, exc)
            db.rollback()
            run.failed_stage = run.status
            set_run_status(db, run, "failed")
    finally:
        _generating.discard(run_id)
        db.close()
        run_context.clear()


def _run_single_regeneration(run_id: int, case_id: int, reviewer_comment: str | None) -> None:
    """Background worker: regenerate one case's spec and stream the result over WS.

    Runs off-request (see ``regenerate_case_spec``) so a slow, multi-Claude-call
    regeneration can't exceed the fronting proxy/tunnel timeout. Sets the run
    context so the Claude call resolves the run owner's credential (own→shared),
    then publishes ``spec.regenerated`` with either the fresh ``spec`` payload or
    an ``error`` string for the client to react to.
    """
    run_context.set_run(run_id)
    db = db_module.SessionLocal()
    try:
        run = db.get(Run, run_id)
        case = db.get(TestCase, case_id)
        if run is None or case is None:
            return
        try:
            spec = _generate_one(db, run, case, reviewer_comment=reviewer_comment)
            db.commit()
            db.refresh(spec)
            audit_service.record(
                category="ai", actor_type="ai", action="Regenerated spec",
                target=f"{case.ticket_external_id} · {case.code}",
                meta=f"Comment: {reviewer_comment[:500]}" if reviewer_comment else "",
            )
            hub.publish(str(run_id), "spec.regenerated", {"caseId": case_id, "spec": _spec_out(spec)})
        except Exception as exc:  # noqa: BLE001 - surface the failure to the client, never crash the thread
            db.rollback()
            logger.error("Spec regeneration failed for case {}: {}", case_id, exc)
            hub.publish(str(run_id), "spec.regenerated", {"caseId": case_id, "error": str(exc)})
    finally:
        _regenerating_cases.discard(case_id)
        db.close()
        run_context.clear()


@router.post("/runs/{run_id}/automation/generate")
def generate_automation(
    run_id: int,
    force: bool = False,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> list[dict]:
    """Kick off automation spec generation for a run's approved, non-Manual cases.

    Runs generation in a background thread and returns the current specs list
    immediately (per contract). Sets Run.status = 'automation' once the
    background pass completes.

    Args:
        force: When False (default) only cases without an existing spec are
            generated — newly approved cases get specs while previously
            generated/edited specs are left untouched. When True every eligible
            case is regenerated, overwriting existing specs.
    """
    run = get_owned_or_404(db, Run, run_id, user)

    # Guard against double-triggering while a pass is already running.
    if run_id not in _generating:
        _generating.add(run_id)
        threading.Thread(
            target=_run_generation, args=(run_id, force), daemon=True
        ).start()
        audit_service.record(
            category="ai", actor_type="ai",
            action="Regenerated automation" if force else "Generated automation",
            target=run.code,
        )

    specs = (
        db.query(AutomationSpec)
        .join(TestCase, AutomationSpec.test_case_id == TestCase.id)
        .filter(TestCase.run_id == run_id)
        .all()
    )
    files_cache: dict[int, list[dict]] = {}
    return [_spec_out(s, files_cache) for s in specs]


@router.get("/runs/{run_id}/automation")
def list_automation(
    run_id: int, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> list[dict]:
    """List all generated automation specs for a run."""
    get_owned_or_404(db, Run, run_id, user)
    specs = (
        db.query(AutomationSpec)
        .join(TestCase, AutomationSpec.test_case_id == TestCase.id)
        .filter(TestCase.run_id == run_id)
        .all()
    )
    files_cache: dict[int, list[dict]] = {}
    return [_spec_out(s, files_cache) for s in specs]


@router.get("/runs/{run_id}/automation/status")
def automation_status(
    run_id: int, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> dict:
    """Whether a generation pass is currently running for this run.

    Lets the UI restore the 'generating' state after navigating away/back and
    keep the Generate button disabled instead of re-triggering.
    """
    get_owned_or_404(db, Run, run_id, user)
    return {"generating": is_generating(run_id)}


@router.get("/cases/{case_id}/spec")
def get_case_spec(
    case_id: int, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> dict:
    """Get the automation spec for a single test case."""
    _get_case_and_run_or_404(db, case_id, user)
    spec = db.query(AutomationSpec).filter(AutomationSpec.test_case_id == case_id).first()
    if spec is None:
        raise HTTPException(status_code=404, detail="Spec not found")
    return _spec_out(spec)


@router.patch("/cases/{case_id}/spec")
def update_case_spec(
    case_id: int,
    payload: AutomationSpecUpdate,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> dict:
    """Persist manual edits to a case's spec, re-gate it, and sync the .spec.ts file.

    Re-runs the placeholder / invented-reference gate on the edited code (the
    same gate generation uses), so a manual edit that removes the TODO
    placeholders **unblocks** the spec (``status="draft"``, file written, runnable)
    — and, conversely, re-introducing a placeholder re-blocks it. A still-blocked
    edit is persisted (``code``/``block_reason``) but not written to the runnable
    file set, matching :func:`_generate_one`. 404 if the case has no spec.
    """
    case, run = _get_case_and_run_or_404(db, case_id, user)
    spec = db.query(AutomationSpec).filter(AutomationSpec.test_case_id == case_id).first()
    if spec is None:
        raise HTTPException(status_code=404, detail="Spec not found")

    spec.code = payload.code
    context = spec_service.build_case_context(db, case, env=run.env)
    known = {
        "routes": context.get("routes", []),
        "selectors": context.get("selectors", []),
        "base_url": context.get("baseUrl", ""),
    }
    gate, outcome, project_path = _gate_edit(
        db, run, case, spec, payload.code, known, noun="edited spec", fix_verb="Fix"
    )
    spec.gate_report = json.dumps(gate)

    if outcome == "passed":
        path = project_path or str(
            spec_service.write_spec_file(
                run.code, case.ticket_external_id, case.code, payload.code, run.owner_id
            )
        )
        spec.path = path
        spec.status = "draft"
        spec.block_reason = ""
    else:
        # Still not clean — keep it out of the runnable file set (don't write it).
        prefix = "Rejected: " if outcome == "rejected" else ""
        spec.status = "blocked"
        spec.block_reason = f'{prefix}{gate["reason"]} {gate["unblock_action"]}'.strip()

    db.commit()
    db.refresh(spec)
    return _spec_out(spec)


@router.post("/cases/{case_id}/spec/regenerate")
def regenerate_case_spec(
    case_id: int,
    body: AutomationSpecRegenerate = Body(default_factory=AutomationSpecRegenerate),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> dict:
    """Synchronously regenerate the automation spec for a single test case.

    An optional free-text ``comment`` steers this one regeneration (audit-only;
    not persisted on the spec row): it is injected into the generation prompt as
    reviewer guidance, but the placeholder / invented-reference gate still runs
    unchanged — a comment can never bypass quality gating.
    """
    case, run = _get_case_and_run_or_404(db, case_id, user)
    comment = (body.comment or "").strip() or None

    # Run OFF-REQUEST: a regeneration makes multiple sequential Claude calls
    # (generate + static review) and routinely runs well over a minute, which
    # exceeds the fronting proxy/tunnel timeout (Cloudflare → 524) if done inline.
    # Kick off a background thread and stream the result over the run WS as
    # `spec.regenerated`; the client shows a "Regenerating…" state until it lands.
    if case_id not in _regenerating_cases:
        _regenerating_cases.add(case_id)
        threading.Thread(
            target=_run_single_regeneration,
            args=(run.id, case_id, comment),
            daemon=True,
        ).start()
    return {"started": True, "caseId": case_id}


_SPEC_MENTION_RE = re.compile(r"@([\w.\-]+\.spec\.ts)")


def _resolve_spec_mentions(db, run: Run, case: TestCase, message: str) -> list[tuple[str, str]]:
    """Resolve ``@<filename>.spec.ts`` mentions in a chat message to (filename, code)
    pairs for other specs in the same run — the reviewer's embedded context. Skips
    the spec being edited and de-dupes; best-effort (returns [] on no matches).

    Matched on the **basename**: a project-backed spec's ``filename`` is a
    project-relative path (``tests/SUR-1428/SUR-1428-TC-01.spec.ts``), which the
    mention syntax never spells out."""
    names = {n for n in _SPEC_MENTION_RE.findall(message or "")}
    if not names:
        return []
    rows = (
        db.query(AutomationSpec)
        .join(TestCase, AutomationSpec.test_case_id == TestCase.id)
        .filter(TestCase.run_id == run.id)
        .all()
    )
    return [
        (r.filename, r.code or "")
        for r in rows
        if r.test_case_id != case.id and (r.filename or "").rsplit("/", 1)[-1] in names
    ]


def _run_spec_chat(run_id: int, case_id: int, message: str, message_id: str) -> None:
    """Background worker: apply a reviewer's chat instruction to a spec via Claude.

    Mirrors ``_run_single_regeneration`` (off-request so slow Claude calls can't
    hit the proxy timeout) and persists the edit exactly like ``update_case_spec``
    (re-gate + write_spec_file, else blocked). Publishes ``automation.chat.reply``
    (with the pre-edit ``prevCode`` so the client can Undo + diff) or
    ``automation.chat.error`` — both carry ``messageId`` so the client correlates
    the async result to the placeholder message it rendered on send.
    """
    run_context.set_run(run_id)
    db = db_module.SessionLocal()
    try:
        run = db.get(Run, run_id)
        case = db.get(TestCase, case_id)
        if run is None or case is None:
            return
        spec = db.query(AutomationSpec).filter(AutomationSpec.test_case_id == case_id).first()
        if spec is None:
            hub.publish(
                str(run_id), "automation.chat.error",
                {"caseId": case_id, "messageId": message_id, "error": "This case has no spec to edit."},
            )
            return
        prev_code = spec.code or ""
        try:
            references = _resolve_spec_mentions(db, run, case, message)
            explanation, new_code = spec_service.generate_chat_edit(
                db, run, case, prev_code, message, references
            )
            # Persist + re-gate exactly like a manual edit (update_case_spec).
            spec.code = new_code
            context = spec_service.build_case_context(db, case, env=run.env)
            known = {
                "routes": context.get("routes", []),
                "selectors": context.get("selectors", []),
                "base_url": context.get("baseUrl", ""),
            }
            gate, outcome, project_path = _gate_edit(
                db, run, case, spec, new_code, known, noun="edited spec", fix_verb="Fix"
            )
            spec.gate_report = json.dumps(gate)
            if outcome == "passed":
                spec.path = project_path or str(
                    spec_service.write_spec_file(
                        run.code, case.ticket_external_id, case.code, new_code, run.owner_id
                    )
                )
                spec.status = "draft"
                spec.block_reason = ""
            else:
                prefix = "Rejected: " if outcome == "rejected" else ""
                spec.status = "blocked"
                spec.block_reason = f'{prefix}{gate["reason"]} {gate["unblock_action"]}'.strip()
            db.commit()
            db.refresh(spec)
            audit_service.record(
                category="ai", actor_type="ai", action="Edited spec via chat",
                target=f"{case.ticket_external_id} · {case.code}", meta=message[:500],
            )
            hub.publish(
                str(run_id), "automation.chat.reply",
                {
                    "caseId": case_id, "messageId": message_id, "text": explanation,
                    "prevCode": prev_code, "spec": _spec_out(spec),
                },
            )
        except Exception as exc:  # noqa: BLE001 - surface to the client, never crash the thread
            db.rollback()
            logger.error("Spec chat edit failed for case {}: {}", case_id, exc)
            hub.publish(
                str(run_id), "automation.chat.error",
                {"caseId": case_id, "messageId": message_id, "error": str(exc)},
            )
    finally:
        _chatting_cases.discard(case_id)
        db.close()
        run_context.clear()


@router.post("/cases/{case_id}/spec/chat")
def chat_edit_spec(
    case_id: int,
    payload: SpecChatRequest,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> dict:
    """Edit the case's spec via a reviewer chat instruction (Claude, off-request).

    Kicks off a background thread that edits + re-gates the spec and streams the
    result over the run WS as ``automation.chat.reply`` / ``automation.chat.error``
    (both echo ``messageId``). Returns immediately with the ``messageId`` the client
    uses to correlate that async result. 404 if the case has no spec; 400 if the
    message is empty.
    """
    case, run = _get_case_and_run_or_404(db, case_id, user)
    spec = db.query(AutomationSpec).filter(AutomationSpec.test_case_id == case_id).first()
    if spec is None:
        raise HTTPException(status_code=404, detail="Generate a spec for this case first")
    message = (payload.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    message_id = payload.messageId or uuid4().hex
    if case_id not in _chatting_cases:
        _chatting_cases.add(case_id)
        threading.Thread(
            target=_run_spec_chat, args=(run.id, case_id, message, message_id), daemon=True
        ).start()
    return {"started": True, "caseId": case_id, "messageId": message_id}


@router.post("/cases/{case_id}/spec/heal")
def heal_case_spec(
    case_id: int, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> dict:
    """Start a self-heal loop for one case: run its spec and, while it fails,
    feed the failure back to Claude to regenerate + re-run, up to a cap.

    Runs in a background thread (streams ``heal.progress`` WS events) and returns
    immediately. 409 if the run is executing or another case in the run is
    already healing (they share the run's spec dir).
    """
    case, run = _get_case_and_run_or_404(db, case_id, user)
    spec = db.query(AutomationSpec).filter(AutomationSpec.test_case_id == case_id).first()
    if spec is None:
        raise HTTPException(status_code=404, detail="Generate a spec for this case first")
    if run.status == "executing":
        raise HTTPException(status_code=409, detail="Run is executing — wait for it to finish")

    stored = settings_store.load_settings()
    target = stored.get("executionTarget", "server")
    heal_mode = stored.get("healMode", "classic")

    # Live self-heal (#428): reuse the browser-harness live-authoring pipeline —
    # drive the REAL app, reproduce the failure, and emit a corrected spec (seeded
    # with the failing spec + its last failure). Needs the paired agent (that's
    # where browser-harness + claude run), so it only applies on local-agent; any
    # other target falls through to the classic loop below.
    if heal_mode == "live-harness" and target == "local-agent":
        last_fail = (
            db.query(ExecutionResult)
            .filter(ExecutionResult.test_case_id == case_id, ExecutionResult.error_message != "")
            .order_by(ExecutionResult.id.desc())
            .first()
        )
        error = (last_fail.error_message if last_fail else "") or (spec.block_reason or "")
        context = spec_service.build_case_context(db, case, env=run.env)
        try:
            _enqueue_agent_authoring(
                db, run, case, context, heal={"code": spec.code or "", "error": error}
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        db.commit()
        audit_service.record(
            category="ai", actor_type="ai", action="Self-healed spec (live browser-harness)",
            target=f"{case.ticket_external_id} · {case.code}",
        )
        return {"started": True, "maxAttempts": settings.heal_max_attempts, "mode": "live-harness"}

    # Where the heal's Playwright runs. The server image ships no Playwright, so on
    # a local-agent deployment the heal LOOP must run on the paired device: queue a
    # single-case heal Execution the agent claims via /agent/jobs/next (it then
    # drives run→/heal/fix→re-run and posts /heal/finalize). Server-target keeps the
    # in-process loop.
    if target == "local-agent":
        has_device = (
            db.query(AgentDevice)
            .filter(AgentDevice.owner_id == run.owner_id, AgentDevice.revoked_at.is_(None))
            .first()
            is not None
        )
        if not has_device:
            raise HTTPException(status_code=409, detail="No local agent paired — start your local agent")
        execution = Execution(
            run_id=run.id, status="queued", target="local-agent",
            env=run.env, browser=run.browser, workers=1, total=1,
            heal_case_id=case.id,
        )
        db.add(execution)
        db.flush()
        db.add(
            ExecutionResult(
                execution_id=execution.id, test_case_id=case.id,
                ticket_external_id=case.ticket_external_id, case_code=case.code,
                title=case.title, status="pending",
            )
        )
        spec.status = "running"
        db.commit()
        audit_service.record(
            category="ai", actor_type="ai", action="Self-healed spec (local agent)",
            target=f"{case.ticket_external_id} · {case.code}",
        )
        return {"started": True, "maxAttempts": settings.heal_max_attempts, "mode": "local-agent"}

    if not playwright_runner.start_heal(case_id, run.id):
        raise HTTPException(
            status_code=409, detail="Another case in this run is already self-healing"
        )
    audit_service.record(
        category="ai", actor_type="ai", action="Self-healed spec",
        target=f"{case.ticket_external_id} · {case.code}",
    )
    return {"started": True, "maxAttempts": settings.heal_max_attempts, "mode": "server"}


@router.get("/cases/{case_id}/spec/heal/status")
def heal_case_spec_status(
    case_id: int, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> dict:
    """Whether a self-heal pass is running for this case (survives navigation).

    Covers both the in-process server heal (``playwright_runner._healing``) and an
    agent-executed heal — a queued/running Execution flagged ``heal_case_id`` — so
    the "Healing…" button state is correct the moment a local-agent heal is queued,
    not only once the agent starts streaming ``heal.progress``.
    """
    _get_case_and_run_or_404(db, case_id, user)
    state = playwright_runner.heal_state(case_id)
    if not state["healing"]:
        agent_heal = (
            db.query(Execution.id)
            .filter(Execution.heal_case_id == case_id, Execution.status.in_(("queued", "running")))
            .first()
        )
        if agent_heal is not None:
            state = {"healing": True, "attempt": 0, "maxAttempts": settings.heal_max_attempts}
    return state


@router.get("/cases/{case_id}/spec/heal/report")
def heal_case_spec_report(
    case_id: int, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> dict:
    """The last self-heal trail for a case: per-attempt error, diff and outcome.

    Returns ``{}`` if the case has no spec or has never been healed.
    """
    import json as _json

    _get_case_and_run_or_404(db, case_id, user)
    spec = db.query(AutomationSpec).filter(AutomationSpec.test_case_id == case_id).first()
    if spec is None or not spec.heal_report:
        return {}
    try:
        return _json.loads(spec.heal_report)
    except _json.JSONDecodeError:
        return {}


def _project_files_out(spec: AutomationSpec, cache: dict[int, list[dict]] | None = None) -> list[dict]:
    """The spec's project tree as read-only ``[{path, kind, code}]`` (#540, for #543).

    Read from the ``automation_files`` mirror rather than disk: the mirror is
    refreshed by ``sync_files_to_db`` right after every accepted generation, and
    reading rows keeps this a pure DB call inside a request. Empty for a legacy
    (``project_id IS NULL``) spec, and empty — never an error — when the spec is
    detached from a session.

    Args:
        cache: Optional ``{project_id: files}`` memo so a list endpoint serializing
            N specs of the same project builds the tree once instead of N times.
    """
    if spec.project_id is None:
        return []
    if cache is not None and spec.project_id in cache:
        return cache[spec.project_id]
    session = object_session(spec)
    if session is None:  # pragma: no cover - defensive; specs are always attached
        return []
    rows = session.scalars(
        select(AutomationFile)
        .where(AutomationFile.project_id == spec.project_id)
        .order_by(AutomationFile.path)
    ).all()
    files = [{"path": row.path, "kind": row.kind, "code": row.code} for row in rows]
    if cache is not None:
        cache[spec.project_id] = files
    return files


def _spec_out(spec: AutomationSpec, files_cache: dict[int, list[dict]] | None = None) -> dict:
    out = {
        "id": spec.id,
        "testCaseId": spec.test_case_id,
        "filename": spec.filename,
        "language": spec.language,
        "framework": spec.framework,
        "code": spec.code,
        "status": spec.status,
        "blockReason": spec.block_reason,
        "gateReport": spec.gate_report,
        # The ticket's REUSE/EXTEND/CREATE plan (#544), as a JSON string exactly like
        # `gateReport` — so the Automation screen renders it beside the gate report
        # with no new endpoint. `null` when the case was never planned (legacy path).
        "planReport": spec.plan_report,
        # The persistent automation project this spec lives in (#540). `null` for a
        # legacy spec.
        "projectId": spec.project_id,
    }
    # `projectFiles` is OPTIONAL in the UI contract (#543) and is omitted — not sent
    # as `[]` — for a legacy spec, so the client renders it exactly as before with
    # no empty panel. The case's own spec is part of the tree (it is mirrored from
    # `tests/<TICKET>/…` like any other file), so the client shows its real
    # project-relative path rather than synthesizing a bare filename.
    files = _project_files_out(spec, files_cache)
    if files:
        out["projectFiles"] = files
    return out


# ---------------------------------------------------------------------------
# Export the automation project to a customer-owned remote (#549)
# ---------------------------------------------------------------------------


def _export_project_or_404(
    db: Session, run: Run, project_id: int | None, user: User | None
) -> AutomationProject:
    """The automation project this run's specs live in, ownership-checked.

    Two ways in, both of which enforce ADR 0008/0009 ownership:

    * An explicit ``projectId`` goes through ``get_owned_or_404``, so a user asking
      to export **another user's** project gets a 404, not a push.
    * Otherwise it is derived from the run's own specs (already ownership-checked
      via the run). A run whose specs span more than one project is ambiguous and
      asks the client to name one rather than guessing.

    Raises:
        HTTPException: 404 for an unowned/missing project, 400 when the run has no
            persistent automation project (legacy specs) or the choice is ambiguous.
    """
    if project_id is not None:
        return get_owned_or_404(db, AutomationProject, project_id, user)

    project_ids = sorted(
        {
            pid
            for (pid,) in db.query(AutomationSpec.project_id)
            .join(TestCase, AutomationSpec.test_case_id == TestCase.id)
            .filter(TestCase.run_id == run.id, AutomationSpec.project_id.isnot(None))
            .distinct()
            .all()
        }
    )
    if not project_ids:
        raise HTTPException(
            status_code=400,
            detail=(
                "This run has no persistent automation project to export — its specs predate "
                "the git-backed project. Generate automation for a run bound to a project first."
            ),
        )
    if len(project_ids) > 1:
        raise HTTPException(
            status_code=400,
            detail="This run's specs span several automation projects; specify which to export.",
        )
    return get_owned_or_404(db, AutomationProject, project_ids[0], user)


def _suggested_remote(db: Session, project: AutomationProject) -> str:
    """A best-effort prefill for the remote URL: the project's own configured repo.

    Only ever a *suggestion* — the user confirms or replaces it, because the target
    of an export is a decision Q-Agent does not own (#549). Returns ``""`` when the
    project has no configured repo URL.
    """
    config = project_config_service.get_config_for_owner(
        db, project.project_key, project.owner_id
    ) or project_config_service.get_config(db, project.project_key)
    if config is None:
        return ""
    for repo in project_config_service.get_repos(config):
        if project.repo and repo.get("name") == project.repo:
            return (repo.get("repo_url") or "").strip()
    return (config.repo_url or "").strip()


@router.get("/runs/{run_id}/automation/export")
def automation_export_preflight(
    run_id: int,
    projectId: int | None = None,  # noqa: N803 - query param mirrors the JSON body field
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> dict:
    """What the export panel needs to prefill itself. **Pushes nothing.**

    A read-only call by construction: it resolves the project, suggests a branch and
    remote, and reports whether a repository connection with a usable PAT exists.
    A missing connection comes back as ``credentialsError`` (an actionable sentence)
    rather than an HTTP error, so the UI can explain the problem *before* the user
    triggers an action instead of after a failed push.
    """
    run = get_owned_or_404(db, Run, run_id, user)
    project = _export_project_or_404(db, run, projectId, user)
    return automation_export_service.export_preflight(
        db, project, suggested_remote=_suggested_remote(db, project)
    )


@router.post("/runs/{run_id}/automation/export")
def export_automation_project(
    run_id: int,
    payload: AutomationExportRequest,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> dict:
    """Push the run's automation project to a branch on a remote the user names (#549).

    **The only export trigger in the codebase.** Nothing calls it on generate, on
    heal, on execute or from any background thread — the customer owning their
    automation suite is an explicit decision, so it takes an explicit action, and the
    remote and branch both come from the request body.

    Synchronous on purpose: the user is waiting on a yes/no answer and every refusal
    (diverged branch, default branch, missing connection) is only actionable in the
    moment. Refusals are 400s whose ``detail`` is safe to render verbatim — every
    message routes through the PAT scrubbing in
    ``repo_service.run_git_captured`` / ``automation_export_service``, so neither the
    response, the WS frame nor the log line can carry the token.
    """
    run = get_owned_or_404(db, Run, run_id, user)
    project = _export_project_or_404(db, run, payload.projectId, user)
    branch = (payload.branch or "").strip()
    try:
        result = automation_export_service.export_to_remote(
            db,
            project,
            remote_url=payload.remoteUrl,
            branch=branch,
            message=(payload.message or "").strip(),
        )
    except automation_export_service.ExportError as exc:
        hub.publish(
            str(run_id),
            "automation.exported",
            {
                "ok": False,
                "code": exc.code,
                "error": exc.message,
                "branch": branch,
                "remote": automation_export_service.redact_remote(payload.remoteUrl),
                "projectId": project.id,
            },
        )
        audit_service.record(
            category="automation",
            action="Automation project export refused",
            target=f"{run.code} · {project.slug}",
            status="warning",
            detail={"code": exc.code, "branch": branch},
        )
        raise HTTPException(status_code=400, detail=exc.message) from exc

    hub.publish(str(run_id), "automation.exported", {**result, "projectId": project.id})
    audit_service.record(
        category="automation",
        action="Exported automation project",
        target=f"{run.code} · {project.slug}",
        # `remote` is already redacted by the service; `commit`/`branch` carry no secret.
        detail={
            "remote": result["remote"],
            "branch": result["branch"],
            "commit": result["commit"],
            "pushed": result["pushed"],
        },
    )
    return {**result, "projectId": project.id}
