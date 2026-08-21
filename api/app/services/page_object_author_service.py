"""Author and extend the project's shared library from the Automation Plan (#545).

The headline capability of epic #537. #544 decided REUSE > EXTEND > CREATE but
authored nothing, so a ``create`` decision still ended as inline locators. This
module closes that loop: an agentic Claude run, confined to the project dir,
applies the plan's ``create``/``extend`` actions as **real files**, and
:func:`automation_planner_service.refresh_plan` then re-derives ``importable``
from the tree it just wrote — which is the whole mechanism by which a
freshly-authored page object becomes legal to import in the same pass.

Editing shared code that other tickets' specs already import is the scariest
thing in the epic. Three stacked defences make it safe, and all three run on
**every** authoring pass:

1. **Whole-project ``playwright test --list``** — collection covers every spec in
   the project, so an edit that breaks *another case's* spec is rejected, not just
   one that breaks its own. ``tsc --noEmit`` (#546) runs after it, because
   ``--list`` transpiles with esbuild and never checks a signature.
2. **Git rollback** — any rejection is ``git reset --hard`` back to the exact
   pre-authoring commit, so a bad edit leaves no debris and every previously-good
   file is untouched.
3. **:func:`automation_project_service.diff_is_additive`** — the editor may *add*
   exported methods but may not delete one, change its parameter list, or rewrite
   its body. This converts the worst coupling risk into a hard, cheap gate.

Plus a plan-boundary check: a file the plan never marked ``writable`` (or anything
outside the library dirs — ``tests/``, ``package.json``, the configs) is a
rejection. Blast radius never crosses ``(owner, project_key, repo)``.

**Residual risk, accepted:** a semantically-wrong-but-compiling change can still
degrade another case at *runtime*, and no static gate sees that. The mitigation is
diagnosability, so an :func:`audit_service.record` entry is written **per file
touched**, naming the plan action that motivated it.

**Cost.** The editor is skipped entirely when the plan has no ``create``/
``extend`` action — a reuse-only feature must never pay for an agentic call, and
that is the slice's main cost control. It also runs **once per ticket**: the
plan file carries an ``authoredAt`` stamp, so the ticket's second case reuses the
authored library instead of re-running the editor. Spend is bounded by the same
ceilings as live authoring (``authoring_cost_budget_usd`` pre-flight plus the
CLI's native ``--max-budget-usd``).
"""

from __future__ import annotations

from typing import Any, Sequence

from app.db import utcnow
from app.logging import logger
from app.models.automation_project import AutomationProject
from app.services import (
    ai_usage_service,
    audit_service,
    automation_gate,
    automation_planner_service,
    automation_project_service,
    claude_cli,
    settings_store,
)
from app.services.prompts import render_project_context
from app.services.skills import PAGE_OBJECT_AUTHOR

__all__ = ["AUTHORED_ACTIONS", "author_assets", "pending_actions", "skipped"]

# The plan actions this stage acts on. `reuse` and `reuse-base` need no file
# written, which is exactly why a reuse-only plan costs nothing here.
AUTHORED_ACTIONS = ("create", "extend")

_SYSTEM_PROMPT = (
    "You are a senior test-automation engineer maintaining a shared Playwright "
    "page-object library that many existing tests already import. You add exactly "
    "what the Automation Plan asks for and change nothing else. You never edit or "
    "delete an existing method, and you never write a spec file."
)


def skipped(reason: str, **extra: Any) -> dict[str, Any]:
    """A uniform 'the editor did not run' report — ``ran`` is False, always."""
    return {"ran": False, "ok": True, "reason": reason, "files": [], **extra}


def pending_actions(plan: dict | None) -> list[dict]:
    """The plan's ``create``/``extend`` entries, flattened across asset groups.

    Empty means there is nothing to author, which is the signal to skip the
    agentic call entirely (see the module docstring's cost note).
    """
    if not automation_planner_service.is_actionable(plan):
        return []
    out: list[dict] = []
    for group in automation_planner_service.ASSET_GROUPS:
        for entry in (plan or {}).get(group) or []:
            if entry.get("action") in AUTHORED_ACTIONS and entry.get("path"):
                out.append({**entry, "group": group})
    return out


# ---------------------------------------------------------------------------
# Prompting
# ---------------------------------------------------------------------------


def _render_actions(actions: Sequence[dict]) -> str:
    lines: list[str] = []
    for entry in actions:
        methods = ", ".join(entry.get("methods") or []) or "(the plan named none — infer from the cases)"
        line = (
            f"- {entry['action'].upper()} `{entry['path']}` ({entry['group']}, "
            f"{entry['name']})\n  methods to provide: {methods}"
        )
        existing = entry.get("existingMethods") or []
        if existing:
            line += (
                "\n  ALREADY IN THIS FILE (keep every one of these exactly as it is, "
                "signature and body): " + ", ".join(existing)
            )
        if entry.get("reason"):
            line += f"\n  why: {entry['reason']}"
        lines.append(line)
    return "\n".join(lines)


def _build_prompt(
    plan: dict,
    actions: Sequence[dict],
    cases: Sequence[Any],
    context: dict | None,
) -> str:
    case_lines: list[str] = []
    for case in cases:
        steps = "; ".join(
            f"{step.get('a', '')} -> {step.get('e', '')}" for step in (getattr(case, "steps", None) or [])
        )
        case_lines.append(f"- {case.code}: {case.title}\n  steps: {steps or '(none)'}")
    project_block = render_project_context(context, include_secrets=False)
    writable = plan.get("writable") or []
    return (
        "Author this feature's shared automation assets in the project you are "
        "running in, following the AUTOMATION PLAN below. Write library code only — "
        "no `.spec.ts`, and nothing under `tests/`.\n\n"
        f"Feature / ticket: {plan.get('ticket') or ''} — {plan.get('feature') or ''}\n\n"
        "Test cases this library has to support (they are automated by a LATER "
        "stage — you provide the page objects they will call, not the tests):\n"
        + "\n".join(case_lines)
        + "\n\n"
        + (f"{project_block}\n\n" if project_block else "")
        + "AUTOMATION PLAN — the exhaustive list of what to author:\n"
        + _render_actions(actions)
        + "\n\nHARD BOUNDARIES (violating any one of them reverts your entire edit "
        "with `git reset --hard`, and this feature loses its page objects):\n"
        f"- Write ONLY these paths: {', '.join(writable) or '(none)'}. Any other new "
        "or modified file — a spec, anything under `tests/`, `package.json`, "
        "`tsconfig.json`, `playwright.config.ts` — is a rejection.\n"
        "- ADDITIVE ONLY. Never delete or rename an exported method, never change "
        "its parameter list, never rewrite its body. Other tickets' specs already "
        "call them. If one looks wrong, leave it, add a differently-named method, "
        "and say so in your final message.\n"
        "- The whole project must still collect (`playwright test --list`) and "
        "typecheck (`tsc --noEmit`) afterwards, including every spec written for "
        "other tickets. Read the files you edit in full before editing them.\n"
        "- Import `test`/`expect`, assertion helpers, waits and dynamic-data "
        "generators from '@q-agent/playwright-base'; import `Page`/`Locator` types "
        "from '@playwright/test'. Never author auth/session/login plumbing — the "
        "base package owns it.\n"
        "- Library files sit ONE level below the project root, so a sibling import "
        "is `../pages/Foo` — `../../` is spec depth and must not appear here.\n\n"
        "Match the conventions of the files already in the library (class shape, "
        "constructor, locator style, naming) — read a neighbour first. Finish with a "
        "short plain-text summary: one line per file with what you added."
    )


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def _record_files(
    files: Sequence[str],
    actions: Sequence[dict],
    *,
    ticket_external_id: str,
    run_code: str,
    status: str,
    meta: str,
) -> None:
    """One audit entry per file touched, naming the plan action behind it.

    The accepted residual risk of this slice is a change that compiles and
    collects but is semantically wrong for *another* case at runtime. Nothing
    static catches that, so the mitigation is that every touched file is
    attributable: which file, which plan action, which ticket, and whether it was
    kept or rolled back.
    """
    by_path = {entry["path"]: entry for entry in actions}
    for path in files:
        entry = by_path.get(path) or {}
        action = entry.get("action") or "unplanned"
        audit_service.record(
            category="ai",
            actor_type="ai",
            action=f"Automation library {action}",
            target=f"{ticket_external_id} · {path}",
            status=status,
            run_code=run_code,
            meta=meta,
            detail={
                "path": path,
                "planAction": action,
                "asset": entry.get("name") or "",
                "plannedMethods": entry.get("methods") or [],
                "reason": entry.get("reason") or "",
            },
        )


def author_assets(
    db,
    project: AutomationProject,
    run_code: str,
    ticket_external_id: str,
    plan: dict | None,
    cases: Sequence[Any] = (),
    context: dict | None = None,
    *,
    run_id: int | None = None,
) -> tuple[dict | None, dict[str, Any]]:
    """Apply the plan's ``create``/``extend`` actions to the project's library.

    Args:
        db: Active session (used to mirror the tree and read the run's spend).
        project: The persistent automation project — the confined workspace.
        run_code: Owning run code, for the plan cache path and the audit trail.
        ticket_external_id: The feature's ticket.
        plan: The ticket's Automation Plan. ``None``/empty/reuse-only → no call.
        cases: The ticket's test cases, so the editor knows what the methods are
            for. Descriptive only — it never writes tests.
        context: Resolved project context (routes/selectors/locator strategy),
            secrets excluded: a page object must not bake credentials in.
        run_id: Ambient run, for the budget pre-flight.

    Returns:
        ``(plan, report)``. On a successful authoring pass the plan is the
        **refreshed** one, whose ``importable`` now includes the files just
        written — that is what the caller must feed to generation. On a skip or a
        rejection the plan comes back unchanged (a rejection has already rolled
        the tree back, so the unchanged plan is also the accurate one).
        ``report`` is ``{ran, ok, reason, files, ...}``; ``ran`` False means no
        Claude call was made at all.
    """
    actions = pending_actions(plan)
    if not actions:
        # The cost control. A reuse-only feature (or a failed/empty plan) never
        # reaches the CLI, and this is observable: no usage row, no activity entry.
        logger.info(
            "page-object author skipped for {} {}: no create/extend actions in the plan",
            run_code, ticket_external_id,
        )
        return plan, skipped("no create/extend actions")
    assert plan is not None  # pending_actions() is empty for a None plan
    if plan.get("authoredAt") and not plan.get("authoringError"):
        # Once per ticket, like planning itself: the ticket's second case reuses
        # the library this pass authored instead of paying for the editor again.
        return plan, skipped("already authored for this ticket", authoredAt=plan["authoredAt"])
    if plan.get("authoredAt") and plan.get("authoringError"):
        # A previous pass FAILED and stamped the plan anyway (to stop a paid call
        # being retried once per case). But a stamp that records an error is not
        # "already authored" — it authored nothing, so skipping it forever left the
        # planned page objects permanently absent while every later attempt reported
        # ok=True and nothing warned (#608). Retry, and let the budget pre-flight
        # below be the cost guard rather than a one-shot stamp.
        logger.warning(
            "page-object authoring retrying for {} {} after an earlier failure: {}",
            run_code, ticket_external_id, plan["authoringError"],
        )

    budget = settings_store.authoring_cost_budget_usd()
    if run_id is not None:
        try:
            spent = float(ai_usage_service.run_breakdown(db, run_id).get("totalCostUsd") or 0.0)
        except Exception as exc:  # noqa: BLE001 - the budget read is best-effort
            logger.warning("page-object author budget check skipped: {}", exc)
        else:
            if spent >= budget:
                logger.warning(
                    "page-object author skipped for {}: run already spent ${:.2f} of ${:.2f}",
                    ticket_external_id, spent, budget,
                )
                return plan, skipped(f"authoring budget ${budget:.2f} already spent")

    library_dirs = set(automation_project_service.LIBRARY_DIRS)
    writable = set(plan.get("writable") or [])

    with automation_project_service.project_lock(project):
        project_root = automation_project_service.project_dir(project)
        # Commit whatever is in the tree first, so the rollback point is exactly
        # "the project as it was before the editor ran" — defence 2's anchor.
        automation_project_service.git_commit(
            project, f"chore: pre-authoring state for {ticket_external_id}"
        )
        pre_state = automation_project_service.head_commit(project) or "HEAD"
        before = automation_project_service.inventory(project)

        try:
            result = claude_cli.run_agentic(
                _build_prompt(plan, actions, cases, context),
                workspace_dir=project_root,
                system=_SYSTEM_PROMPT,
                skill=PAGE_OBJECT_AUTHOR,
                allowed_tools=claude_cli._PROJECT_TOOLS,
                max_budget_usd=budget,
                label=f"Author assets: {ticket_external_id}",
            )
        except Exception as exc:  # noqa: BLE001 - authoring must never break generation
            logger.warning("page-object authoring failed for {}: {}", ticket_external_id, exc)
            automation_project_service.git_reset_hard(project, pre_state)
            # Stamped even on failure: retrying a paid agentic call once per case of
            # the ticket is how a cost ceiling gets blown through. The feature falls
            # back to inline locators for this pass, which is the pre-#545 behaviour.
            return (
                automation_planner_service.refresh_plan(
                    project, run_code, ticket_external_id, plan,
                    authoredAt=utcnow().isoformat(), authoringError=str(exc)[:300],
                ),
                {"ran": True, "ok": False, "reason": f"editor failed: {exc}"[:300], "files": []},
            )

        touched = automation_project_service.git_changed_paths(project)
        rejection = _rejection(project, project_root, touched, before, writable, library_dirs)
        if rejection:
            logger.warning(
                "page-object authoring REJECTED for {} ({}): rolling back {} file(s) to {}",
                ticket_external_id, rejection, len(touched), pre_state[:8],
            )
            _record_files(
                touched, actions,
                ticket_external_id=ticket_external_id, run_code=run_code,
                status="error", meta=f"Rolled back — {rejection}",
            )
            automation_project_service.git_reset_hard(project, pre_state)
            automation_project_service.sync_files_to_db(db, project)
            return (
                automation_planner_service.refresh_plan(
                    project, run_code, ticket_external_id, plan,
                    authoredAt=utcnow().isoformat(), authoringError=rejection,
                ),
                {"ran": True, "ok": False, "reason": rejection, "files": list(touched)},
            )

        automation_project_service.git_commit(
            project, f"feat({ticket_external_id}): author shared automation assets"
        )
        automation_project_service.sync_files_to_db(db, project)
        automation_project_service.write_inventory(project)
        _record_files(
            touched, actions,
            ticket_external_id=ticket_external_id, run_code=run_code,
            status="success", meta="Accepted — project collects, typechecks, and the diff is additive.",
        )
        logger.info(
            "page-object authoring accepted for {} {}: {}",
            run_code, ticket_external_id, ", ".join(touched) or "(no files changed)",
        )
        # THE handoff to generation: re-normalizing against the tree just written is
        # what turns a `create` into an importable path, with no prompt rewording.
        refreshed = automation_planner_service.refresh_plan(
            project,
            run_code,
            ticket_external_id,
            plan,
            authoredAt=utcnow().isoformat(),
            # Explicitly clear any error stamped by an earlier failed pass: `**extra`
            # MERGES onto the refreshed plan, so a stale `authoringError` would make
            # every later case retry a pass that has already succeeded (#608).
            authoringError="",
        )
        return refreshed, {
            "ran": True,
            "ok": True,
            "reason": "",
            "files": list(touched),
            "importable": list(refreshed.get("importable") or []),
            "summary": (result or "").strip()[:2000],
        }


def _rejection(
    project: AutomationProject,
    project_root,
    touched: Sequence[str],
    before: Sequence[dict],
    writable: set[str],
    library_dirs: set[str],
) -> str:
    """The first defence that rejects this edit, or ``""`` when all of them pass.

    Ordered cheapest-first, and deliberately *all* of them rather than the two that
    would catch most cases: the plan boundary and the additive check are pure
    Python, so they run before any subprocess; ``--list`` then catches a broken
    import anywhere in the project, and ``tsc`` catches the wrong *signature* that
    esbuild happily erases.
    """
    if not touched:
        return "the editor wrote nothing"

    outside = [
        path
        for path in touched
        if path.split("/", 1)[0] not in library_dirs or path not in writable
    ]
    if outside:
        return "wrote files the plan did not authorize: " + ", ".join(sorted(outside)[:6])

    # Defence 3 — additive only. Cheapest of the three and the one that guards the
    # scariest failure: silently changing what an existing method means.
    if not automation_project_service.diff_is_additive(project, before):
        return "the edit removed or rewrote an existing exported method"

    # Defence 1 — the whole project must still collect. No expected titles: this is
    # not a candidate spec, it is an edit to shared code, and what matters is that
    # every spec already in the project still collects.
    list_ok, detail = automation_gate.list_ok_in_project(project_root, [])
    if not list_ok:
        return f"the edit broke project collection: {detail}"[:600]

    types_ok, type_detail = automation_gate.typecheck_ok(project_root)
    if not types_ok:
        return f"the edit does not typecheck: {type_detail}"[:600]
    return ""
