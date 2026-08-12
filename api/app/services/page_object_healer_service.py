"""Project-aware self-heal: repair the shared library, not just the spec (#547).

Before this module the heal loop rewrote **only** ``spec.code``. In a layered
project (#542) that is the wrong file: the spec is a thin sequence of business
steps and the locators live in ``pages/LoginPage.ts``, so a stale locator gave
the fixer exactly one way to reach green — copy the locator back into the spec
and stop calling the page object. That "fix" passed the gate, flattened the
architecture the epic had just built, and left the *next* ticket's spec failing on
the same unrepaired page object. #542 added a prompt guard against it; this module
supplies the missing ability the guard was standing in for.

Two things live here.

**1. The import-spanning assertion scope** (:func:`assertion_scope_count`) — the
hard blocker named in #547. The anti-cheat rejects a fix whose assertion count
dropped, and it counted **one file**. Once a page object may hold page-level UI
assertions (doc §14), *moving* an assertion out of the spec and into the page
object that owns the screen reads as "assertions removed" and is rejected — so a
layered spec could not be healed at all. Counting the spec **plus** every project
library file it imports (transitively) makes the move a no-op for the total while
keeping a genuine deletion a strict decrease. The count itself is still
:func:`placeholder_gate.count_assertions`, so #542's ``\\bexpect[A-Z]\\w*\\(``
widening for the base package's ``expectVisible(`` helpers is inherited, not
re-implemented.

**2. The library heal** (:func:`heal_library`) — an agentic Claude run confined to
the project dir that may **edit** the page objects the failing spec imports, under
the same three stacked defences as authoring (#545), reusing that slice's
machinery rather than a parallel copy:

1. whole-project ``playwright test --list`` (plus ``tsc --noEmit``, #546, after
   it — cheap check first, and it is what catches a signature esbuild erases);
2. ``git reset --hard`` back to the exact pre-heal commit on any rejection;
3. :func:`automation_project_service.diff_is_additive`, called with
   ``allow_body_edits=True``.

That flag is the one deliberate difference from authoring, and it is forced: the
stale locator *is* a method body, so a heal that may not rewrite a body cannot fix
anything — which is precisely what pushed the loop into re-inlining. The half of
the guarantee other specs depend on ("every signature I import still exists, with
the same parameters") is untouched, and the body edit is additionally fenced by
the two static gates above and by the assertion scope in (1), which no authoring
pass has.

Plus a boundary check tighter than authoring's: the only writable paths are the
library files **this failing spec imports**. A new page object, a helper, a spec,
``package.json`` — all rejections. Blast radius never crosses
``(owner, project_key, repo)``.

**Cost.** At most **one** agentic call per heal pass, and only when the spec is
project-backed *and* actually imports library files — a legacy or unlayered spec
never reaches the CLI here. Bounded by ``authoring_cost_budget_usd`` (a
``run_breakdown`` pre-flight) and the CLI's native ``--max-budget-usd``. A product
defect is classified *before* this runs and never reaches it (see
``playwright_runner.heal_spec``): the app being wrong is never healed by editing
the test, and that stays true of the library too.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.logging import logger
from app.models.automation_project import AutomationProject
from app.services import (
    ai_usage_service,
    audit_service,
    automation_gate,
    automation_project_service,
    claude_cli,
    placeholder_gate,
    settings_store,
)
from app.services.prompts import render_dom_snapshot
from app.services.skills import PAGE_OBJECT_HEALER

__all__ = [
    "assertion_scope_count",
    "heal_library",
    "imported_library_paths",
    "library_sources",
    "skipped",
]

# `from '...'` / `export ... from '...'` with a RELATIVE specifier. Only relative
# specifiers can name a file in this project; '@q-agent/playwright-base' and
# '@playwright/test' are packages and are ignored by construction.
_RELATIVE_IMPORT_RE = re.compile(r"""from\s*['"](\.{1,2}/[^'"]+)['"]""")

_SYSTEM_PROMPT = (
    "You are a senior test-automation engineer repairing a shared Playwright "
    "page-object library that many existing tests already import. You fix the "
    "defect that made one spec fail and change nothing else. You may rewrite the "
    "body of an existing method, but never its signature; you never delete an "
    "assertion to make a test pass; and you never write a spec file."
)


def skipped(reason: str, **extra: Any) -> dict[str, Any]:
    """A uniform 'the library healer did not run' report — ``ran`` is False, always."""
    return {"ran": False, "ok": True, "reason": reason, "files": [], **extra}


# ---------------------------------------------------------------------------
# Import resolution — which library files is this spec's failure allowed to blame?
# ---------------------------------------------------------------------------


def _resolve(root: Path, from_relative: str, specifier: str) -> str | None:
    """Resolve one relative import to a project-relative library file, or None.

    ``tests/SUR-1/x.spec.ts`` + ``../../pages/LoginPage`` -> ``pages/LoginPage.ts``.
    Returns None for anything that does not land on an existing ``.ts`` file
    inside :data:`automation_project_service.LIBRARY_DIRS` — so ``tests/``,
    escapes above the root, and missing files all resolve to nothing rather than
    to something the healer would then be allowed to write.
    """
    base = posixpath.dirname(from_relative.replace("\\", "/"))
    target = posixpath.normpath(posixpath.join(base, specifier))
    if target.startswith("..") or posixpath.isabs(target):
        return None
    if target.split("/", 1)[0] not in automation_project_service.LIBRARY_DIRS:
        return None
    candidates = (
        [target] if target.endswith(".ts") else [f"{target}.ts", f"{target}/index.ts"]
    )
    for candidate in candidates:
        if (root / candidate).is_file():
            return candidate
    return None


def imported_library_paths(
    root: "Path | AutomationProject | None", spec_relative: str, code: str
) -> list[str]:
    """Project-relative library files the spec imports, **transitively**.

    Transitive on purpose: a spec imports ``pages/LoginPage``, which imports
    ``components/Header``, and a stale locator in the header is just as much the
    cause of the spec's failure as one in the page. Both the writable set and the
    assertion scope want the same closure, so it is computed once here.

    Args:
        root: Project root ``Path`` / :class:`AutomationProject`, or None for a
            legacy (non-project) spec, which imports no library files by
            definition.
        spec_relative: The spec's project-relative path, e.g.
            ``tests/SUR-1428/SUR-1428-TC-01.spec.ts`` — the anchor every ``../``
            is resolved against.
        code: The spec source. Read from the argument, not from disk, so a
            *proposed* fix's imports can be resolved before it is written.

    Returns:
        Sorted, de-duplicated project-relative paths that exist on disk.
    """
    if root is None or not code:
        return []
    base = (
        automation_project_service.project_dir(root)
        if isinstance(root, AutomationProject)
        else Path(root)
    )
    seen: set[str] = set()
    queue: list[tuple[str, str]] = [(spec_relative, code)]
    while queue:
        origin, text = queue.pop()
        for specifier in _RELATIVE_IMPORT_RE.findall(text or ""):
            resolved = _resolve(base, origin, specifier)
            if resolved is None or resolved in seen:
                continue
            seen.add(resolved)
            try:
                queue.append((resolved, (base / resolved).read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError):  # unreadable -> no further edges
                continue
    return sorted(seen)


def library_sources(
    root: "Path | AutomationProject", paths: Iterable[str]
) -> dict[str, str]:
    """``{project-relative path: source}`` for the paths that can be read."""
    base = (
        automation_project_service.project_dir(root)
        if isinstance(root, AutomationProject)
        else Path(root)
    )
    out: dict[str, str] = {}
    for relative in paths:
        try:
            out[relative] = (base / relative).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return out


# ---------------------------------------------------------------------------
# The anti-cheat, made project-aware
# ---------------------------------------------------------------------------


def library_assertion_count(
    root: "Path | AutomationProject | None", spec_relative: str, code: str
) -> int:
    """Assertions in the library files ``code`` imports — the spec itself excluded.

    Split out from :func:`assertion_scope_count` because the anti-cheat compares a
    **transition**, not one tree state: the spec side moves between two versions
    while the library side moves between two tree states, and conflating them is
    what made the naive widening fail. See :func:`assertion_scope_count`.
    """
    imported = imported_library_paths(root, spec_relative, code)
    if not imported:
        return 0
    return sum(
        placeholder_gate.count_assertions(source)
        for source in library_sources(root, imported).values()  # type: ignore[arg-type]
    )


def assertion_scope_count(
    root: "Path | AutomationProject | None", spec_relative: str, code: str
) -> int:
    """Assertions in the spec **plus** every library file it imports.

    This is the fix for #547's hard blocker. Compared across the *whole*
    transition — the spec before/after **and** the tree before/after — so:

    * **moving** an assertion from the spec into the page object that owns the
      screen leaves the total unchanged (it left one file as it arrived in
      another) and is accepted, so the layered shape doc §14 asks for stops
      reading as sabotage;
    * **deleting** one (or loosening it to nothing countable) still lowers the
      total and is still rejected, wherever in the layers it happened. Widening
      the scope adds files to the sum; it never subtracts a check.

    Callers comparing a spec fix must hold the library side at its **pre-edit**
    value — see ``playwright_runner.heal_spec``'s ``library_floor`` — because a
    library gain measured on both sides of the comparison cancels out and the move
    reads as a loss again. That subtlety is the reason
    :func:`library_assertion_count` exists separately.

    A legacy spec (``root=None``) imports no library files, so this is exactly
    :func:`placeholder_gate.count_assertions` — the pre-#547 behaviour, bit for
    bit.
    """
    return placeholder_gate.count_assertions(code) + library_assertion_count(
        root, spec_relative, code
    )


# ---------------------------------------------------------------------------
# Prompting
# ---------------------------------------------------------------------------


def _build_prompt(
    spec_relative: str,
    spec_code: str,
    error: str,
    output: str,
    sources: dict[str, str],
    dom_snapshot: dict[str, Any] | None,
) -> str:
    files_block = "\n\n".join(
        f"// {path}\n```typescript\n{(text or '').strip()}\n```"
        for path, text in sources.items()
    )
    dom_block_text = render_dom_snapshot(dom_snapshot)
    dom_block = f"{dom_block_text}\n\n" if dom_block_text else ""
    output_block = (
        f"\n\nPlaywright output (tail):\n{output.strip()[-2000:]}" if output.strip() else ""
    )
    return (
        "A Playwright spec in this project FAILED. The spec is a thin sequence of "
        "business steps — the locators, waits and navigation it relies on live in "
        "the shared library files below, which it imports. Find the defect in "
        "those library files and repair it there.\n\n"
        f"{dom_block}"
        f"The failing spec — `{spec_relative}`. READ-ONLY: you must NOT edit it, and "
        "you must NOT move its locators out of the library into it:\n"
        f"```typescript\n{(spec_code or '').strip()}\n```\n\n"
        f"Failure / error:\n{error.strip() or '(no error message captured)'}"
        f"{output_block}\n\n"
        "The ONLY files you may write — the library files this spec imports:\n"
        f"{files_block}\n\n"
        "HARD BOUNDARIES (violating any one of them reverts your entire edit with "
        "`git reset --hard`, and the heal loop falls back to rewriting the spec):\n"
        f"- Write ONLY these paths: {', '.join(sources) or '(none)'}. Any other file — "
        "a new page object, a helper, the spec, anything under `tests/`, "
        "`package.json`, `tsconfig.json`, `playwright.config.ts` — is a rejection.\n"
        "- You MAY rewrite the body of an existing method (that is the point: the "
        "stale locator is inside one). You may NOT delete or rename an exported "
        "method, class or function, and you may NOT change a parameter list — other "
        "tickets' specs call them by name and arity.\n"
        "- The total number of assertions across the spec AND these library files is "
        "counted before and after you run and must NOT go down. Moving an assertion "
        "into the page object that owns the screen is fine; deleting one, or "
        "loosening it to something trivially true, is a rejection. If the only way "
        "to make the test pass is to check less, edit NOTHING and say so — the app is "
        "probably the thing that is wrong.\n"
        "- Fix the existing locator in place. Do not add a duplicate locator for the "
        "same element elsewhere, and do not leave the stale one behind beside the new "
        "one.\n"
        "- Never add `waitForTimeout(...)`; prefer `getByRole`/`getByLabel`/"
        "`getByTestId` and web-first assertions, grounded in the captured DOM above.\n"
        "- The whole project must still collect (`playwright test --list`) and "
        "typecheck (`tsc --noEmit`) afterwards, including every spec written for "
        "other tickets.\n"
        "- Library files sit ONE level below the project root, so a sibling import is "
        "`../pages/Foo` — `../../` is spec depth and must not appear here.\n\n"
        "Finish with a short plain-text summary: one line per file naming the defect "
        "and the change. If nothing in the library was wrong, edit nothing and say "
        "that instead."
    )


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def _record_files(
    files: Sequence[str],
    *,
    ticket_external_id: str,
    case_code: str,
    run_code: str,
    status: str,
    meta: str,
    error: str,
) -> None:
    """One audit entry per library file a heal touched.

    Same reasoning as authoring's trail (#545): the accepted residual risk is a
    change that compiles and collects but is semantically wrong for *another*
    case at runtime, and nothing static catches that — so every touched file has
    to be attributable to the failure that motivated it and to a keep/rollback
    decision.
    """
    for path in files:
        audit_service.record(
            category="ai",
            actor_type="ai",
            action="Automation library heal",
            target=f"{ticket_external_id} {case_code} · {path}",
            status=status,
            run_code=run_code,
            meta=meta,
            detail={"path": path, "caseCode": case_code, "failure": (error or "")[:600]},
        )


def heal_library(
    db,
    project: AutomationProject,
    run_code: str,
    ticket_external_id: str,
    case_code: str,
    spec_relative: str,
    spec_code: str,
    error: str,
    output: str = "",
    dom_snapshot: dict[str, Any] | None = None,
    *,
    run_id: int | None = None,
) -> dict[str, Any]:
    """Repair the library files the failing spec imports, or report why not.

    Args:
        db: Active session (mirrors the tree back to the DB, reads the run spend).
        project: The persistent automation project — the confined workspace.
        run_code: Owning run code, for the audit trail.
        ticket_external_id / case_code: The failing case, for the audit trail.
        spec_relative: The spec's project-relative path (the import anchor).
        spec_code: The spec source that failed. Never written — read-only context.
        error / output: The Playwright failure and output tail.
        dom_snapshot: The failing attempt's distilled live DOM, so the repair is
            grounded in the page that actually rendered.
        run_id: Ambient run, for the budget pre-flight.

    Returns:
        ``{ran, ok, reason, files, ...}``. ``ran`` False means no Claude call was
        made (no imports, or the budget was already spent). ``ok`` False with
        ``ran`` True means the edit was rejected and the tree has been rolled
        back. On success, ``before``/``after`` carry the touched files' sources so
        the caller can feed a corrected selector back to the KB.
    """
    targets = imported_library_paths(project, spec_relative, spec_code)
    if not targets:
        # The cheap exit that keeps a legacy or unlayered spec free: nothing is
        # imported, so there is no library file this failure could blame.
        return skipped("the failing spec imports no project library files")

    budget = settings_store.authoring_cost_budget_usd()
    if run_id is not None:
        try:
            spent = float(ai_usage_service.run_breakdown(db, run_id).get("totalCostUsd") or 0.0)
        except Exception as exc:  # noqa: BLE001 - the budget read is best-effort
            logger.warning("library heal budget check skipped: {}", exc)
        else:
            if spent >= budget:
                logger.warning(
                    "library heal skipped for {} {}: run already spent ${:.2f} of ${:.2f}",
                    ticket_external_id, case_code, spent, budget,
                )
                return skipped(f"authoring budget ${budget:.2f} already spent")

    with automation_project_service.project_lock(project):
        project_root = automation_project_service.project_dir(project)
        # Commit whatever is in the tree first, so the rollback point is exactly
        # "the project as it was before the healer ran" — defence 2's anchor.
        automation_project_service.git_commit(
            project, f"chore: pre-heal state for {ticket_external_id} {case_code}"
        )
        pre_state = automation_project_service.head_commit(project) or "HEAD"
        before_inventory = automation_project_service.inventory(project)
        before_sources = library_sources(project, targets)
        before_assertions = assertion_scope_count(project, spec_relative, spec_code)

        try:
            summary = claude_cli.run_agentic(
                _build_prompt(
                    spec_relative, spec_code, error, output, before_sources, dom_snapshot
                ),
                workspace_dir=project_root,
                system=_SYSTEM_PROMPT,
                skill=PAGE_OBJECT_HEALER,
                allowed_tools=claude_cli._PROJECT_TOOLS,
                max_budget_usd=budget,
                label=f"Heal library: {ticket_external_id} {case_code}",
            )
        except Exception as exc:  # noqa: BLE001 - a library heal must never break the loop
            logger.warning(
                "library heal failed for {} {}: {}", ticket_external_id, case_code, exc
            )
            automation_project_service.git_reset_hard(project, pre_state)
            return {"ran": True, "ok": False, "reason": f"healer failed: {exc}"[:300], "files": []}

        touched = automation_project_service.git_changed_paths(project)
        rejection = _rejection(
            project,
            project_root,
            touched,
            before_inventory,
            set(targets),
            spec_relative,
            spec_code,
            before_assertions,
        )
        if rejection:
            logger.warning(
                "library heal REJECTED for {} {} ({}): rolling back {} file(s) to {}",
                ticket_external_id, case_code, rejection, len(touched), pre_state[:8],
            )
            _record_files(
                touched,
                ticket_external_id=ticket_external_id, case_code=case_code,
                run_code=run_code, status="error",
                meta=f"Rolled back — {rejection}", error=error,
            )
            automation_project_service.git_reset_hard(project, pre_state)
            automation_project_service.sync_files_to_db(db, project)
            return {"ran": True, "ok": False, "reason": rejection, "files": list(touched)}

        automation_project_service.git_commit(
            project, f"fix({ticket_external_id}): heal library for {case_code}"
        )
        automation_project_service.sync_files_to_db(db, project)
        automation_project_service.write_inventory(project)
        _record_files(
            touched,
            ticket_external_id=ticket_external_id, case_code=case_code,
            run_code=run_code, status="success",
            meta="Accepted — project collects, typechecks, signatures intact, assertions preserved.",
            error=error,
        )
        logger.info(
            "library heal accepted for {} {} {}: {}",
            run_code, ticket_external_id, case_code, ", ".join(touched),
        )
        return {
            "ran": True,
            "ok": True,
            "reason": "",
            "files": list(touched),
            "before": before_sources,
            "after": library_sources(project, touched),
            "summary": (summary or "").strip()[:2000],
        }


def _rejection(
    project: AutomationProject,
    project_root,
    touched: Sequence[str],
    before_inventory: Sequence[dict],
    targets: set[str],
    spec_relative: str,
    spec_code: str,
    before_assertions: int,
) -> str:
    """The first defence that rejects this heal, or ``""`` when all of them pass.

    Ordered cheapest-first, exactly like authoring's (#545): the write boundary,
    the signature check and the assertion scope are pure Python and run before any
    subprocess; ``--list`` then catches a broken import anywhere in the project,
    and ``tsc`` catches the wrong *signature* esbuild happily erases.
    """
    if not touched:
        # Not an error: "nothing in the library was wrong" is a legitimate, and
        # cheap, conclusion. The caller falls through to the spec fixer.
        return "the healer found nothing to change in the library"

    outside = [path for path in touched if path not in targets]
    if outside:
        return "wrote files the failing spec does not import: " + ", ".join(sorted(outside)[:6])

    # Signatures frozen, bodies free — see the module docstring for why heal needs
    # `allow_body_edits` where authoring must not have it.
    if not automation_project_service.diff_is_additive(
        project, before_inventory, allow_body_edits=True
    ):
        return "the heal removed or re-signed an existing exported method"

    # THE new defence (#547): the count spans the spec + its imports, so an
    # assertion may move between layers but may not vanish from them.
    after_assertions = assertion_scope_count(project, spec_relative, spec_code)
    if after_assertions < before_assertions:
        return (
            "the heal removed/weakened assertions (anti-cheat: "
            f"{before_assertions} -> {after_assertions} across the spec and its imports)"
        )

    list_ok, detail = automation_gate.list_ok_in_project(project_root, [])
    if not list_ok:
        return f"the heal broke project collection: {detail}"[:600]

    types_ok, type_detail = automation_gate.typecheck_ok(project_root)
    if not types_ok:
        return f"the heal does not typecheck: {type_detail}"[:600]
    return ""
