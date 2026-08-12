"""Server-assist for agent-executed self-heal (issue #260).

The self-heal LOOP runs on the Local Agent (where Playwright + the captured DOM
live). The two steps that physically require the server — asking Claude for a
fix (the agent holds no LLM credentials) and reading/writing the DB + Knowledge
Base — are delegated here, called by the ``/agent/heal/*`` endpoints:

* :func:`plan_fix` — given the current spec code + failure + captured DOM, resolve
  project context/examples/KB from the DB, classify the failure, ask Claude for a
  fix, and run the anti-cheat + placeholder gate. Returns the action the agent
  should take (``fixed`` / ``blocked`` / ``rejected`` / ``product_defect``).
* :func:`finalize_agent_heal` — persist the final spec status/code/heal report and
  feed a passing DOM-grounded heal back into the KB.

Both mirror the decisions the in-process server loop
(:func:`app.services.playwright_runner.heal_spec`) makes per attempt, minus the
Playwright execution (which is the agent's job).

Project-aware since #547, on both counts the server loop is:

* :func:`plan_fix` runs :func:`page_object_healer_service.heal_library` before the
  spec fixer for a layered spec, and its anti-cheat count spans the spec **plus**
  its imported page objects. A repaired library is returned to the agent as
  ``libraryFiles`` so the device's next re-run stages the healed page object — the
  agent is stateless and holds no read-file capability, so the only way it can see
  the edit is for the server to ship it, exactly as the claim ships the bundle.
* :func:`finalize_agent_heal` writes the healed spec to the **project tree** when
  the spec is project-backed. It previously always wrote to the legacy per-run dir,
  so an agent-executed heal's fix never reached the project — the next generation
  and every other target read the stale code.
"""

from __future__ import annotations

import difflib
import json
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.logging import logger
from app.models.automation_project import AutomationProject
from app.models.run import Run, RunTicket
from app.models.testcase import AutomationSpec, TestCase
from app.services import (
    failure_classifier,
    page_object_healer_service,
    placeholder_gate,
    playwright_runner,
    run_context,
    settings_store,
    spec_examples,
    spec_service,
)
from app.services.claude_cli import ClaudeError
from app.services.playwright_runner import _resolve_project_for_run


def _known_with_dom(known: dict, dom_snapshot: dict[str, Any] | None) -> dict:
    """Augment the gate's ``known`` grounding with the live captured DOM (#265).

    A route/selector observed in the actually-rendered page is real, not
    hallucinated — so the placeholder gate must not flag a fix that uses it as an
    "invented reference". This adds the captured pathname (route) and each captured
    element's identifier(s) — as the exact literal forms the gate extracts from code
    (bare test id, ``[data-testid="…"]``, ``#id``) — to ``known``. This is what lets
    a DOM-grounded heal pass the gate (and then enrich the KB on the pass), breaking
    the chicken-and-egg where DOM-derived refs were rejected as unknown.
    """
    if not dom_snapshot:
        return known
    routes = list(known.get("routes") or [])
    selectors = list(known.get("selectors") or [])
    path = (dom_snapshot.get("path") or "").strip()
    if path:
        routes.append({"path": path})
    for el in dom_snapshot.get("elements") or []:
        if not isinstance(el, dict):
            continue
        test_id = el.get("testId")
        if test_id:
            selectors.append({"selector": test_id})
            selectors.append({"selector": f'[data-testid="{test_id}"]'})
        el_id = el.get("id")
        if el_id:
            selectors.append({"selector": f"#{el_id}"})
            selectors.append({"selector": el_id})
    return {**known, "routes": routes, "selectors": selectors}


def _resolve_grounding(db, case: TestCase, run: Run) -> tuple[dict, dict, list[dict]]:
    """Resolve the DB-backed grounding a fix needs: (context, known, examples).

    ``context`` is the full project context (base URL, decrypted creds, routes,
    selectors) from :func:`spec_service.build_case_context`; ``known`` is the
    subset the placeholder gate compares a fix against; ``examples`` are proven
    passing specs for few-shot grounding. Mirrors ``heal_spec``'s setup.
    """
    context = spec_service.build_case_context(db, case, env=run.env)
    known = {
        "routes": context.get("routes", []),
        "selectors": context.get("selectors", []),
        "base_url": context.get("baseUrl", ""),
    }
    project_key, _base_url, _manual_auth, _provider = _resolve_project_for_run(db, run, run.env)
    heal_ticket = (
        db.query(RunTicket)
        .filter(
            RunTicket.run_id == run.id,
            RunTicket.ticket_external_id == case.ticket_external_id,
        )
        .first()
    )
    repo = heal_ticket.repo if heal_ticket else ""
    examples = (
        spec_examples.select_examples(db, project_key, repo, case, limit=1) if project_key else []
    )
    return context, known, examples


def plan_fix(
    db,
    case: TestCase,
    run: Run,
    current_code: str,
    error: str,
    output: str,
    dom_snapshot: dict[str, Any] | None,
    attempt: int = 1,
) -> dict[str, Any]:
    """Classify a heal failure and, unless it's a product defect, propose a fix.

    Returns one of::

        {"action": "product_defect", "failureClass": str, "reason": str}
        {"action": "rejected", "reason": str}                       # anti-cheat or gate
        {"action": "blocked", "reason": str, "code": str}           # missing KB grounding
        {"action": "fixed", "code": str, "diff": str}               # apply + re-run

    The agent applies a ``fixed`` result (write the code, re-run) and stops on any
    terminal action, then calls :func:`finalize_agent_heal`.

    Runs under the run's ambient context (:func:`run_context.set_run`) so the Claude
    CLI resolves the **run owner's** credentials — this endpoint is called on a
    request thread with no ambient run, so without this Claude falls back to the
    (logged-out) shared credential and fails with "Not logged in".
    """
    previous_run = run_context.get_run()
    run_context.set_run(run.id)
    try:
        return _plan_fix(db, case, run, current_code, error, output, dom_snapshot, attempt)
    finally:
        run_context.set_run(previous_run)


# case_id -> assertions in the imported library as it stood before this heal pass
# touched it. The agent path is stateless per HTTP call, so the floor the server
# loop keeps in a local variable has to live somewhere across calls; the pass is a
# single ordered sequence of `plan_fix` calls for one case, and attempt 1 reseeds
# it. See ``playwright_runner.heal_spec``'s ``library_floor`` for why the floor
# must be held at its PRE-EDIT value rather than recomputed each time.
_library_floor: dict[int, int] = {}


def _project_scope(db, case: TestCase) -> tuple[AutomationProject | None, str]:
    """``(project, project-relative spec path)`` for the case, or ``(None, "")``.

    The anchor everything project-aware needs: the confined workspace a library
    heal may edit, and the path every ``../../pages/…`` import is resolved
    against. Legacy (``project_id IS NULL``) specs yield ``(None, "")``, which
    makes every project-aware branch below inert for them.
    """
    spec = db.query(AutomationSpec).filter(AutomationSpec.test_case_id == case.id).first()
    if spec is None or spec.project_id is None:
        return None, ""
    project = db.get(AutomationProject, spec.project_id)
    if project is None:
        return None, ""
    return project, playwright_runner._spec_relative_path(spec, case.ticket_external_id, case.code)


def _plan_fix(
    db,
    case: TestCase,
    run: Run,
    current_code: str,
    error: str,
    output: str,
    dom_snapshot: dict[str, Any] | None,
    attempt: int = 1,
) -> dict[str, Any]:
    context, known, examples = _resolve_grounding(db, case, run)

    classification = failure_classifier.classify_failure(case, current_code, error, output, context)
    if classification["suspectedProductDefect"] or classification["failureClass"] == "product_defect":
        return {
            "action": "product_defect",
            "failureClass": classification["failureClass"],
            "reason": classification.get("reason", ""),
        }

    project, spec_relative = _project_scope(db, case)
    if attempt <= 1 or case.id not in _library_floor:
        _library_floor[case.id] = page_object_healer_service.library_assertion_count(
            project, spec_relative, current_code
        )

    # #547 — try the LIBRARY first for a layered spec, exactly as the server loop
    # does: the failure is most often a stale locator inside an imported page
    # object, and the spec fixer's only route to green there is to inline it back
    # into the spec. The repaired files ride back to the agent so its next re-run
    # stages them; the spec itself is unchanged, which is the point.
    if project is not None and attempt <= 1:
        library = page_object_healer_service.heal_library(
            db, project, run.code, case.ticket_external_id, case.code,
            spec_relative, current_code, error, output, dom_snapshot, run_id=run.id,
        )
        if library["ran"] and library["ok"] and library["files"]:
            return {
                "action": "fixed",
                "code": current_code,  # UNCHANGED — the fix is in the page object
                "diff": "",
                "libraryFiles": [
                    {"path": path, "code": (library.get("after") or {}).get(path, "")}
                    for path in library["files"]
                ],
                "librarySummary": library.get("summary", ""),
            }
        logger.info(
            "agent heal: library heal did not apply for case {}: {}", case.id, library["reason"]
        )

    try:
        fixed = spec_service.generate_fixed_spec_code(
            case, current_code, error, output, context, examples, dom_snapshot
        )
    except ClaudeError as exc:
        # Return a clean terminal action (not a 500) so the agent records a failed
        # attempt and stops the loop instead of throwing on the HTTP call.
        return {"action": "rejected", "reason": f"Heal fix generation failed: {exc}"}

    # Anti-cheat: a fix that removes/weakens assertions only "passes" by checking
    # less — reject it and keep the previous spec. The count SPANS THE SPEC PLUS ITS
    # IMPORTED PAGE OBJECTS (#547), same as the server loop: an assertion may move
    # between the layers, it may not vanish from them. `project` is None for a legacy
    # spec, for which this is bit-for-bit the old single-file count.
    if page_object_healer_service.assertion_scope_count(
        project, spec_relative, fixed
    ) < placeholder_gate.count_assertions(current_code) + _library_floor[case.id]:
        return {
            "action": "rejected",
            "reason": "Rejected fix: it removed/weakened assertions (anti-cheat).",
        }

    # Gate against the KB PLUS the live captured DOM — a fix that uses real,
    # observed routes/selectors is grounded, not invented (#265). Bypassed when
    # the global quality gate is off (#gate-toggle) — the fix is then accepted as
    # long as it did not weaken assertions (checked above; that anti-cheat stays).
    gate = (
        placeholder_gate.gate_spec(fixed, _known_with_dom(known, dom_snapshot))
        if settings_store.gate_enabled()
        else placeholder_gate.bypassed_result()
    )
    if gate["outcome"] == "blocked":
        return {
            "action": "blocked",
            "reason": f'{gate["reason"]} {gate["unblock_action"]}'.strip(),
            "gate": json.dumps(gate),
            "code": fixed,
        }
    if gate["outcome"] == "rejected":
        return {"action": "rejected", "reason": gate["reason"], "gate": json.dumps(gate)}

    diff = "\n".join(
        difflib.unified_diff(
            (current_code or "").splitlines(),
            (fixed or "").splitlines(),
            fromfile="spec (before fix)",
            tofile="spec (after fix)",
            lineterm="",
        )
    )
    # The headroom a library gain created has been spent by this fix — rebase so it
    # cannot be spent again on the next attempt.
    _library_floor[case.id] = page_object_healer_service.library_assertion_count(
        project, spec_relative, fixed
    )
    return {"action": "fixed", "code": fixed, "diff": diff}


def finalize_agent_heal(db, case: TestCase, run: Run, payload: dict[str, Any]) -> None:
    """Persist an agent heal's outcome and feed a passing DOM-grounded heal into the KB.

    ``payload`` (posted by the agent) carries::

        finalStatus: "pass"|"fail"|"blocked"|"product_defect"
        finalError:  str
        finalCode:   str            # the spec code as it stands after the loop
        blockReason: str            # when finalStatus == "blocked"
        gateReport:  str            # JSON gate dump when blocked
        domDistilled: object|null   # the passing attempt's distilled DOM (KB enrichment)
        lastFixBefore / lastFixAfter: str|null   # most recent accepted fix (selector-swap feedback)
        attempts:    [ {...} ]      # per-attempt trail for the heal report
    """
    _library_floor.pop(case.id, None)  # the pass is over — don't leak the floor
    spec = db.query(AutomationSpec).filter(AutomationSpec.test_case_id == case.id).first()
    if spec is None:
        return

    final_status = payload.get("finalStatus", "fail")
    final_code = payload.get("finalCode") or spec.code or ""

    # Persist the spec code the loop ended on to its AUTHORITATIVE home (#547).
    # This used to always be `spec_service.write_spec_file` — the legacy per-run
    # dir — even for a project-backed spec, so an agent-executed heal's fix never
    # reached the project tree: the project stayed on the pre-heal code, the next
    # generation and any server-side re-run read the stale version, and the heal
    # was effectively lost the moment the run's dir was recycled. Delegating to the
    # server loop's own writer means the project tree is written, committed and
    # mirrored to the DB for BOTH targets, from one implementation. `spec_dir=None`
    # because this runs on a request thread with no staged run dir to mirror into
    # (the agent's copy is on the device and is discarded when the job ends).
    spec.code = final_code
    spec.path = playwright_runner._persist_spec_code(db, run, case, spec, final_code)

    if final_status == "product_defect":
        spec.status = "product_defect"  # terminal — assertion kept intact
    elif final_status == "blocked":
        spec.status = "blocked"
        spec.block_reason = (payload.get("blockReason") or "").strip()
        if payload.get("gateReport"):
            spec.gate_report = payload["gateReport"]
    else:
        spec.status = "passed" if final_status == "pass" else "failed"

    spec.heal_report = json.dumps(
        {
            "caseId": case.id,
            "finalStatus": final_status,
            "maxAttempts": settings.heal_max_attempts,
            "healedAt": datetime.now(timezone.utc).isoformat(),
            "runsOn": "local-agent",
            "attempts": payload.get("attempts") or [],
        }
    )
    db.commit()

    # KB feedback on a pass (#182 single-selector swap + #249 additive DOM merge).
    if final_status == "pass":
        _project_key, _base_url, _manual, _provider = _resolve_project_for_run(db, run, run.env)
        heal_ticket = (
            db.query(RunTicket)
            .filter(
                RunTicket.run_id == run.id,
                RunTicket.ticket_external_id == case.ticket_external_id,
            )
            .first()
        )
        repo = heal_ticket.repo if heal_ticket else ""
        before, after = payload.get("lastFixBefore"), payload.get("lastFixAfter")
        if before and after:
            playwright_runner._propose_healed_selector_to_kb(
                _project_key, repo, before, after, run.owner_id
            )
        playwright_runner._merge_discovered_dom_to_kb(
            _project_key, repo, final_code, payload.get("domDistilled"), run.owner_id
        )
