"""REUSE > EXTEND > CREATE planning, decided BEFORE any code is generated (#544).

Wave 3, step 1 of epic #537. This slice produces and surfaces the plan; it
**authors no page objects** (that is #545), deliberately, so the reuse decisions
can be watched before being trusted.

The plan is the doc's §24 artifact, normalized server-side into an envelope the
rest of the pipeline can trust:

* ``pages`` / ``components`` / ``fixtures`` / ``data`` / ``utils`` — one entry per
  asset, each ``{name, path, action, methods, reason}`` with ``action`` in
  ``reuse | extend | create | reuse-base``.
* ``counts`` — the epic's own success metric (doc's "how little new code is
  generated"), logged per plan and rolled up per generation pass.
* ``importable`` — **the authorization for an ``import`` in a generated spec.**
* ``writable`` — the only paths generation is allowed to create or modify.

Two properties are load-bearing and are why the normalization step exists at all:

1. **``importable`` is computed from disk, never from the model's claim.** It is
   the intersection of the plan's asset paths with
   :func:`automation_project_service.inventory`, so a hallucinated path can never
   become an import. #178 died from the opposite arrangement.
2. **The criterion is "the file is on disk", not "the action was ``reuse``".**
   #544 shipped with ``create`` targets deliberately excluded, because nothing
   authored them yet and authorizing the import would have failed collection for
   every spec. #545 authors them, so the exclusion is gone: the project editor
   runs, and then :func:`refresh_plan` re-normalizes the plan against the tree it
   just wrote, at which point a freshly created page object *is* on disk and
   therefore importable. A ``create`` whose authoring failed is still not on disk
   and still not importable — the safety property survives without a special case.

Planning is **once per ticket**, not once per case — the main cost lever for
Wave 3. The on-disk plan file *is* the cache: a second case on the same ticket in
the same run loads ``<project>/.qagent/plans/<RUN-CODE>/<TICKET>.plan.json``
instead of calling Claude again.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.db import utcnow
from app.logging import logger
from app.models.automation_project import AutomationProject
from app.models.testcase import TestCase
from app.services import automation_project_service, claude_cli
from app.services.prompts import render_project_context
from app.services.skills import AUTOMATION_PLANNER
from app.services.workspace_scope import slug

__all__ = [
    "ACTIONS",
    "ASSET_GROUPS",
    "DUPLICATE_MAX_EXTRA_TOKENS",
    "counts",
    "duplicate_owner",
    "empty_plan",
    "import_violations",
    "is_actionable",
    "load_plan",
    "log_pass_counts",
    "log_plan_counts",
    "normalize",
    "render_inventory",
    "plan_for_ticket",
    "plan_path",
    "refresh_plan",
    "render_plan",
    "unplanned_new_paths",
]

# Every action the planner may emit. ``reuse-base`` is doc §24's own spelling for
# "this comes from @q-agent/playwright-base" — it is a reuse, and never a path in
# this project.
ACTIONS = ("reuse", "extend", "create", "reuse-base")

# The plan's asset buckets, mapped to the library directory each one lives in.
ASSET_GROUPS = {
    "pages": "pages",
    "components": "components",
    "fixtures": "fixtures",
    "data": "data",
    "utils": "utils",
}

_SYSTEM_PROMPT = (
    "You are a senior test-automation architect. You decide, before any code is "
    "written, which existing automation assets a new feature can REUSE, which "
    "must be EXTENDED, and which genuinely have to be CREATED. You never write "
    "test code. You answer with a single JSON object."
)

# Relative imports out of a spec at tests/<TICKET>/<spec>.spec.ts, e.g.
# `from '../../pages/UserListPage'`.
_ASSET_IMPORT_RE = re.compile(r"""from\s+['"](\.\./\.\./([A-Za-z0-9_./-]+))['"]""")


# ---------------------------------------------------------------------------
# Shape helpers
# ---------------------------------------------------------------------------


def empty_plan(feature: str = "", ticket: str = "") -> dict[str, Any]:
    """A normalized, empty plan — what every failure path degrades to.

    Planning is best-effort by contract: a Claude outage must not stop generation,
    it must only cost the reuse. An empty plan has no ``importable`` paths, so the
    generator falls back to exactly the pre-#544 behaviour (inline locators).
    """
    return {
        "feature": feature,
        "ticket": ticket,
        "specGroups": [],
        **{group: [] for group in ASSET_GROUPS},
        "counts": {action: 0 for action in ACTIONS},
        "importable": [],
        "writable": [],
        "duplicates": [],
        "duplicatesDemoted": 0,
        "cases": [],
        "plannedAt": utcnow().isoformat(),
    }


def _clean_path(raw: Any) -> str:
    """A project-relative asset path, or ``""`` when it is not one.

    Rejects absolute paths, ``..`` escapes and anything outside the library dirs —
    a plan must never be able to point generation at the repo root or at
    ``tests/``.
    """
    text = str(raw or "").strip().replace("\\", "/").lstrip("./")
    if not text or ".." in text.split("/") or text.startswith("/"):
        return ""
    if text.split("/", 1)[0] not in automation_project_service.LIBRARY_DIRS:
        return ""
    return text if text.endswith(".ts") else f"{text}.ts"


def _default_path(group: str, name: str) -> str:
    """Where an asset of this group and name would live, e.g. ``pages/UserPage.ts``."""
    directory = ASSET_GROUPS.get(group, "utils")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "", str(name or "").strip())
    return f"{directory}/{safe}.ts" if safe else ""


def _methods(raw: Any) -> list[str]:
    """The method signatures an entry names, as a de-duplicated list of strings."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            item = item.get("signature") or item.get("name") or ""
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def is_actionable(plan: dict[str, Any] | None) -> bool:
    """True when the plan actually decided something.

    An :func:`empty_plan` — what every planning failure degrades to — decided
    nothing, so it must **not** be enforced: enforcing "you may import nothing"
    off the back of a Claude outage would reject specs for a reason that has
    nothing to do with them. A plan that decided ``create`` for everything (the
    first-feature case) *is* actionable, and correctly forbids asset imports.
    """
    if not plan:
        return False
    return any(plan.get(group) for group in ASSET_GROUPS)


def counts(plan: dict[str, Any]) -> dict[str, int]:
    """``{action: n}`` across every asset group — the epic's success metric."""
    tally = {action: 0 for action in ACTIONS}
    for group in ASSET_GROUPS:
        for entry in plan.get(group) or []:
            action = entry.get("action")
            if action in tally:
                tally[action] += 1
    return tally


# ---------------------------------------------------------------------------
# Duplicate detection (doc §21) — machine-enforced, at the point of authorization
# ---------------------------------------------------------------------------
#
# Doc §21 requires searching for a semantically equivalent existing implementation
# BEFORE creating a new file. Until #571 that was prompt-enforced only: `normalize`
# demoted a *hallucinated* `reuse` and the `writable` boundary blocked
# *unauthorized* paths, but a plan that deliberately asked to `create`
# `pages/CreateUserPage.ts` beside an existing `pages/UserPage.ts` was authorized,
# written, and rejected by nothing — and duplicates degrade the epic's metric
# INVISIBLY, because each one is legitimately "reused" from then on.
#
# The check is deliberately NOT a fuzzy similarity score. A false positive that
# blocks a legitimately distinct page object is worse than a missed duplicate, and
# `UserPage` / `UserListPage` / `UserFormPage` are genuinely distinct screens in
# this codebase's own examples (doc §11), whose token sets differ *exactly as much*
# as `CreateUserPage` differs from `UserPage`. Overlap size therefore cannot
# separate them; only the KIND of the extra token can:
#
#   CreateUserPage vs UserPage  -> extra {create}: a VERB. The difference is a
#                                  capability, which belongs in a method on the
#                                  screen's existing owner (doc §8's `extend`).
#   UserListPage  vs UserPage   -> extra {list}: a NOUN. The difference names a
#                                  different screen, so it is a genuine `create`.
#
# So the rule is: the candidate's core tokens must CONTAIN the existing file's core
# tokens, and every extra token must come from a small closed vocabulary of
# CRUD verbs (pages/components) or wrapper words (everything else). One unknown
# token is enough to leave the `create` alone.

# How many extra qualifier tokens a name may carry and still be judged the same
# asset. The tunable threshold: 0 makes the check exact-name-only, 2+ makes it
# progressively more aggressive. 1 catches doc §21's own two examples.
DUPLICATE_MAX_EXTRA_TOKENS = 1

# Dropped before comparison: they carry no meaning of their own.
_STOPWORDS = frozenset({"a", "an", "and", "for", "in", "of", "on", "the", "to", "with"})

# The asset's own type, not part of its identity: `UserPage` and `User` are the
# same screen. Stripped from both sides.
_TYPE_WORDS = frozenset(
    {
        "page", "component", "fixture", "fixtures", "data", "util", "utils",
        "helper", "helpers", "object", "objects", "po",
    }
)

# CRUD verbs. An extra one of these means the name describes an ACTION on a screen
# that something else already owns — doc §21's `CreateUserPage` case.
_ACTION_WORDS = frozenset(
    {
        "add", "cancel", "create", "delete", "destroy", "edit", "manage", "modify",
        "new", "open", "remove", "save", "show", "submit", "update", "view",
    }
)

# Wrapper words. Only for non-class assets, where the duplicate is usually the same
# capability wrapped differently — doc §21's `waitForDownload` vs `download` case.
_WRAPPER_WORDS = frozenset(
    {
        "common", "do", "ensure", "get", "handle", "perform", "shared", "trigger",
        "until", "wait", "waits",
    }
)

_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _stem_of(path: str) -> str:
    """``"pages/UserPage.ts"`` -> ``"UserPage"``."""
    return path.rsplit("/", 1)[-1].removesuffix(".ts")


def _singular(token: str) -> str:
    """A crude, symmetric plural fold so ``UsersPage`` and ``UserPage`` collide."""
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def _tokens(text: Any) -> list[str]:
    """``"waitForDownload"`` -> ``["wait", "download"]`` (stopwords dropped)."""
    spaced = _CAMEL_SPLIT_RE.sub(" ", re.sub(r"[^A-Za-z0-9]+", " ", str(text or "")))
    out = []
    for raw in spaced.split():
        token = _singular(raw.lower())
        if token and token not in _STOPWORDS:
            out.append(token)
    return out


def _core(text: Any) -> frozenset[str]:
    """The identity tokens of a name — its own type word is not one of them."""
    return frozenset(t for t in _tokens(text) if t not in _TYPE_WORDS)


def _qualifier_extra(
    candidate: frozenset[str],
    existing: frozenset[str],
    vocabulary: frozenset[str],
    *,
    both_ways: bool,
) -> frozenset[str] | None:
    """The extra qualifier tokens when two names denote the same asset, else None.

    ``candidate`` must contain ``existing`` (or, when ``both_ways``, either may
    contain the other — ``download`` vs ``waitForDownload`` is the wrapped-capability
    direction), the difference must be no larger than
    :data:`DUPLICATE_MAX_EXTRA_TOKENS`, and **every** differing token must be in
    ``vocabulary``. Anything else — a differing noun, a disjoint name, a bigger
    difference — is a genuinely distinct asset.
    """
    if not candidate or not existing:
        return None
    if candidate == existing:
        return frozenset()
    if candidate > existing:
        extra = candidate - existing
    elif both_ways and existing > candidate:
        extra = existing - candidate
    else:
        return None
    if len(extra) > DUPLICATE_MAX_EXTRA_TOKENS or not extra <= vocabulary:
        return None
    return extra


def _capability_names(entry: dict) -> list[str]:
    """The names an on-disk file offers — its exports plus its method names."""
    names = [str(n) for n in (entry.get("exports") or [])]
    names += [str(m).split("(", 1)[0].strip() for m in (entry.get("methods") or [])]
    return [n for n in names if n]


def duplicate_owner(
    path: str, name: str, methods: Sequence[str], entries: Sequence[dict]
) -> tuple[dict, str] | None:
    """The existing asset that already owns what a planned ``create`` describes.

    Deterministic, conservative, and confined to the planned path's own library
    directory (a page never "duplicates" a util). Returns ``(inventory_entry,
    reason)`` — the reason names the suspected duplicate, so a demotion or a
    rejection can say which file it deferred to — or ``None`` when the planned
    asset is genuinely new.

    Args:
        path: The planned project-relative path, e.g. ``pages/CreateUserPage.ts``.
        name: The planned asset name (usually the exported class/function).
        methods: The signatures the plan wants, used for the capability-overlap
            check that catches doc §21's ``waitForDownload`` vs ``download``.
        entries: :func:`automation_project_service.inventory` — what is on disk.
    """
    directory = path.split("/", 1)[0]
    is_class = directory in ("pages", "components")
    vocabulary = _ACTION_WORDS if is_class else (_ACTION_WORDS | _WRAPPER_WORDS)
    planned = _core(_stem_of(path)) | _core(name)
    planned_methods = [str(m).split("(", 1)[0].strip() for m in methods if str(m).strip()]

    found: list[tuple[int, str, dict, str]] = []
    for existing in entries:
        existing_path = str(existing.get("path") or "")
        if existing_path == path or existing_path.split("/", 1)[0] != directory:
            continue
        extra = _qualifier_extra(
            planned, _core(_stem_of(existing_path)), vocabulary, both_ways=not is_class
        )
        if extra is not None:
            found.append((
                len(extra),
                existing_path,
                existing,
                f"`{existing_path}` already owns this asset"
                + (
                    f" (`{_stem_of(existing_path)}` and `{_stem_of(path)}` differ only "
                    f"by `{'`, `'.join(sorted(extra))}`)"
                    if extra
                    else f" (`{_stem_of(existing_path)}` names the same thing)"
                ),
            ))
            continue
        if is_class or not planned_methods:
            # Capability overlap is a utils-shaped duplicate: a page object's
            # methods legitimately repeat across screens (`open`, `search`).
            continue
        offered = _capability_names(existing)
        matched: list[str] = []
        for wanted in planned_methods:
            near = next(
                (
                    have
                    for have in offered
                    if _qualifier_extra(_core(wanted), _core(have), vocabulary, both_ways=True)
                    is not None
                ),
                None,
            )
            if near is None:
                break
            matched.append(f"`{near}` vs `{wanted}`")
        else:
            found.append((
                DUPLICATE_MAX_EXTRA_TOKENS + 1,  # weaker evidence than a name match
                existing_path,
                existing,
                f"`{existing_path}` already provides this capability "
                f"({', '.join(matched)})",
            ))
    if not found:
        return None
    found.sort(key=lambda item: (item[0], item[1]))
    return found[0][2], found[0][3]


def _still_missing(methods: Sequence[str], owner: dict) -> list[str]:
    """The planned signatures the owning file does not already provide.

    Exact name matches are dropped for every kind. For non-class assets a
    *near-synonym* is dropped too: if ``download`` was judged a duplicate of
    ``waitForDownload`` because the names are synonymous, then authoring a
    ``download()`` beside it would recreate the very duplicate this check exists to
    stop — one level down, inside the file.
    """
    owner_path = str(owner.get("path") or "")
    is_class = owner_path.split("/", 1)[0] in ("pages", "components")
    vocabulary = _ACTION_WORDS if is_class else (_ACTION_WORDS | _WRAPPER_WORDS)
    offered = _capability_names(owner)
    exact = set(offered)
    out: list[str] = []
    for signature in methods:
        name = str(signature).split("(", 1)[0].strip()
        if not name or name in exact:
            continue
        if not is_class and any(
            _qualifier_extra(_core(name), _core(have), vocabulary, both_ways=True) is not None
            for have in offered
        ):
            continue
        out.append(signature)
    return out


def _enforce_duplicate_detection(
    plan: dict[str, Any], entries: Sequence[dict]
) -> list[dict[str, Any]]:
    """Demote every ``create`` that duplicates an on-disk asset. Returns the findings.

    **Demote, never reject.** The target is unambiguous by construction (the
    heuristic only fires when one existing file contains the planned name), so
    rewriting the entry into an ``extend`` of that file keeps generation moving and
    produces what doc §8's hierarchy wanted anyway. A rejection would fail the whole
    feature over a naming judgement.

    When the plan *also* has an entry for the owning file — the realistic §21 shape,
    "reuse `UserPage` and create `CreateUserPage`" — the demoted entry's new methods
    are merged into that entry instead of appended as a second entry for the same
    path, which would hand the project editor two conflicting instructions for one
    file.
    """
    findings: list[dict[str, Any]] = []
    on_disk = {str(entry.get("path") or ""): entry for entry in entries}
    for group in ASSET_GROUPS:
        group_entries = plan.get(group) or []
        kept: list[dict[str, Any]] = []
        for entry in group_entries:
            path = entry.get("path") or ""
            # A `create` of a path that is already on disk is left alone: it is not a
            # near-duplicate, and the additive-diff guard already covers it.
            if entry.get("action") != "create" or not path or path in on_disk:
                kept.append(entry)
                continue
            match = duplicate_owner(path, entry.get("name") or "", entry.get("methods") or [], entries)
            if match is None:
                kept.append(entry)
                continue
            owner, reason = match
            owner_path = str(owner["path"])
            new_methods = _still_missing(entry.get("methods") or [], owner)
            action = "extend" if new_methods else "reuse"
            sibling = next(
                (
                    other
                    for other in group_entries
                    if other is not entry and other.get("path") == owner_path
                ),
                None,
            )
            finding = {
                "plannedPath": path,
                "plannedName": entry.get("name") or "",
                "existingPath": owner_path,
                "action": action,
                "reason": reason,
                "mergedInto": bool(sibling),
            }
            findings.append(finding)
            logger.info(
                "automation plan: duplicate detected (doc §21) — demoting create {} "
                "-> {} {} ({})",
                path, action, owner_path, reason,
            )
            if sibling is not None:
                merged = list(sibling.get("methods") or [])
                for method in new_methods:
                    if method not in merged:
                        merged.append(method)
                sibling["methods"] = merged
                if new_methods and sibling.get("action") == "reuse":
                    sibling["action"] = "extend"
                continue  # the duplicate entry disappears into the real owner
            entry.update(
                {
                    "name": (owner.get("exports") or [None])[0] or _stem_of(owner_path),
                    "path": owner_path,
                    "action": action,
                    "methods": new_methods if action == "extend" else entry.get("methods") or [],
                    "duplicateOf": owner_path,
                    "plannedPath": path,
                    "plannedName": entry.get("name") or "",
                    "reason": f"Duplicate detection (doc §21): {reason}.",
                }
            )
            kept.append(entry)
        plan[group] = kept
    return findings


# ---------------------------------------------------------------------------
# Normalization — where the model's claims meet the real tree
# ---------------------------------------------------------------------------


def normalize(
    raw: Any,
    entries: Sequence[dict],
    *,
    feature: str = "",
    ticket: str = "",
    cases: Sequence[str] = (),
) -> dict[str, Any]:
    """Turn Claude's plan into the trusted envelope, checked against ``entries``.

    ``entries`` is :func:`automation_project_service.inventory` — the ground truth
    of what is on disk right now. Everything the model says is filtered through
    it:

    * an asset whose path is not in the inventory cannot be ``reuse``/``extend``;
      it is demoted to ``create`` (that is the honest decision, and it keeps the
      plan internally consistent);
    * **a path that is on disk is ``importable``, whatever the action says** —
      including a ``create`` the project editor has just authored (#545). Called
      again through :func:`refresh_plan` after authoring, this is what promotes a
      brand-new page object into the generator's import allowlist with no second
      opinion from the model;
    * a ``create`` that near-duplicates an on-disk asset (doc §21) is demoted to an
      ``extend`` of that asset — see :func:`duplicate_owner`. This happens *before*
      ``writable`` is computed, so the duplicate path is never authorized at all;
    * ``extend``/``create`` paths are ``writable``, i.e. the only paths the
      project editor and generation may touch.

    Args:
        raw: Whatever ``claude_cli.run_json`` returned (any shape; never trusted).
        entries: The project inventory.
        feature/ticket/cases: Server-known facts that override the model's.

    Returns:
        A fully normalized plan dict, safe to persist and to render.
    """
    plan = empty_plan(feature, ticket)
    on_disk = {entry["path"]: entry for entry in entries}
    if not isinstance(raw, dict):
        raw = {}

    plan["feature"] = feature or str(raw.get("feature") or "").strip()
    plan["cases"] = [str(c) for c in cases]

    groups_raw = raw.get("specGroups") or raw.get("spec_groups") or []
    if isinstance(groups_raw, list):
        for group in groups_raw:
            if not isinstance(group, dict):
                continue
            test_cases = group.get("testCases") or group.get("test_cases") or []
            plan["specGroups"].append(
                {
                    "name": str(group.get("name") or "").strip() or "feature",
                    "testCases": [str(c).strip() for c in test_cases if str(c).strip()],
                }
            )

    for group in ASSET_GROUPS:
        raw_entries = raw.get(group) or []
        if not isinstance(raw_entries, list):
            continue
        for item in raw_entries:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            action = str(item.get("action") or "").strip().lower()
            if action not in ACTIONS:
                action = "create"
            path = "" if action == "reuse-base" else (
                _clean_path(item.get("path")) or _default_path(group, name)
            )
            if action in ("reuse", "extend") and path not in on_disk:
                # The model claimed an asset that is not there. Demoting keeps the
                # plan truthful instead of authorizing an import that cannot resolve.
                logger.info(
                    "automation plan: demoting {} {} -> create ({} is not on disk)",
                    action, name, path or "<no path>",
                )
                action = "create"
            entry = {
                "name": name,
                "path": path,
                "action": action,
                "methods": _methods(item.get("methods") or item.get("method")),
                "reason": str(item.get("reason") or "").strip()[:400],
            }
            # Provenance of a §21 demotion survives re-normalization (`refresh_plan`
            # feeds a plan back through here), so the plan on the spec row still says
            # which duplicate this entry replaced.
            for key in ("duplicateOf", "plannedPath", "plannedName"):
                if item.get(key):
                    entry[key] = str(item[key])
            plan[group].append(entry)

    # Doc §21, machine-enforced (#571): a `create` that near-duplicates an existing
    # asset becomes an `extend` of that asset, BEFORE `writable` is computed — so the
    # duplicate path is never authorized for writing in the first place.
    duplicates = _enforce_duplicate_detection(plan, entries)
    plan["duplicates"] = duplicates
    plan["duplicatesDemoted"] = len(duplicates)

    importable: list[str] = []
    writable: list[str] = []
    for group in ASSET_GROUPS:
        for entry in plan[group]:
            path = entry.get("path") or ""
            if path and path in on_disk:
                # On disk == importable, whatever the action claims. Signatures come
                # from the file itself, so an `extend`'s planned-but-unwritten method
                # is never presented to the generator as if it existed; once the
                # project editor has actually written it, the refreshed inventory
                # carries it here automatically.
                entry["existingMethods"] = list(on_disk[path].get("methods") or [])
                if path not in importable:
                    importable.append(path)
            if entry.get("action") in ("extend", "create") and path and path not in writable:
                writable.append(path)

    plan["importable"] = sorted(importable)
    plan["writable"] = sorted(writable)
    plan["counts"] = counts(plan)
    return plan


# ---------------------------------------------------------------------------
# Persistence — the on-disk plan file IS the once-per-ticket cache
# ---------------------------------------------------------------------------


def plan_path(project: AutomationProject, run_code: str, ticket_external_id: str) -> Path:
    """``<project>/.qagent/plans/<RUN-CODE>/<TICKET>.plan.json``.

    Per **ticket**, not per case: planning once per ticket/feature is the slice's
    main cost lever, and a per-case filename would quietly invite per-case
    planning. ``.qagent/`` is excluded from agent bundles and from the DB mirror,
    so this is server-side only.
    """
    root = automation_project_service.project_dir(project)
    return (
        root
        / ".qagent"
        / "plans"
        / (slug(run_code) or "unknown")
        / f"{slug(ticket_external_id) or 'unknown'}.plan.json"
    )


def load_plan(project: AutomationProject, run_code: str, ticket_external_id: str) -> dict | None:
    """The persisted plan for this ticket in this run, or None. Never raises."""
    path = plan_path(project, run_code, ticket_external_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _save_plan(
    project: AutomationProject, run_code: str, ticket_external_id: str, plan: dict
) -> Path:
    path = plan_path(project, run_code, ticket_external_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return path


def refresh_plan(
    project: AutomationProject,
    run_code: str,
    ticket_external_id: str,
    plan: dict,
    **extra: Any,
) -> dict:
    """Re-derive the plan's ``importable``/``writable`` from the tree as it is NOW (#545).

    The single line that connects the project editor to the spec generator. The
    editor authors ``create``/``extend`` assets as **real files**; re-running
    :func:`normalize` against a fresh :func:`automation_project_service.inventory`
    then promotes each of those paths into ``importable`` and refreshes its
    ``existingMethods`` from the file itself — no prompt rewording and no second
    opinion from the model. A ``create`` the editor failed to write is simply still
    absent from the inventory and still not importable.

    The plan dict is its own input shape (``normalize`` reads ``name``/``path``/
    ``action``/``methods``/``reason``/``specGroups``), so this is a genuine
    re-normalization rather than a parallel derivation that could drift.

    Args:
        project/run_code/ticket_external_id: Identify the cached plan file to rewrite.
        plan: The plan to refresh (typically the one just acted on).
        **extra: Bookkeeping keys to merge onto the refreshed plan and persist —
            :mod:`page_object_author_service` stamps ``authoredAt`` here so a
            ticket's second case never re-runs the (paid) editor.

    Returns:
        The refreshed plan. Persisted over the cached plan file, best-effort.
    """
    entries = automation_project_service.inventory(project)
    refreshed = normalize(
        plan,
        entries,
        feature=str(plan.get("feature") or ""),
        ticket=str(plan.get("ticket") or ticket_external_id),
        cases=[str(c) for c in (plan.get("cases") or [])],
    )
    # A duplicate demoted on the first pass is an `extend` by now, so re-normalizing
    # finds nothing to demote — carry the findings forward so the persisted plan keeps
    # the record of what was caught.
    previous = [d for d in (plan.get("duplicates") or []) if isinstance(d, dict)]
    seen = {(d.get("plannedPath"), d.get("existingPath")) for d in refreshed["duplicates"]}
    refreshed["duplicates"] += [
        d for d in previous if (d.get("plannedPath"), d.get("existingPath")) not in seen
    ]
    refreshed["duplicatesDemoted"] = len(refreshed["duplicates"])
    refreshed.update(extra)
    try:
        _save_plan(project, run_code, ticket_external_id, refreshed)
    except OSError as exc:  # pragma: no cover - defensive
        logger.warning("could not persist refreshed plan for {}: {}", ticket_external_id, exc)
    return refreshed


# ---------------------------------------------------------------------------
# Prompting
# ---------------------------------------------------------------------------


def render_inventory(entries: Sequence[dict]) -> str:
    """The project's real reusable surface, as a prompt block.

    This is the correct resolution of the ``docs/ARCHITECTURE-REVIEW.md:266-274``
    fork that #178 chose the other side of: signatures come from **Q-Agent's own
    automation project**, so they are always in sync with what a generated spec
    can actually import.
    """
    if not entries:
        return (
            "PROJECT INVENTORY — this automation project's shared library is EMPTY. "
            "This is the project's first feature, so every asset it needs must be "
            "`create`."
        )
    lines = [
        "PROJECT INVENTORY — the real files in this automation project's shared "
        "library, with the method signatures they actually export. These exist on "
        "disk; they are the ONLY assets that can be reused:"
    ]
    for entry in entries:
        methods = ", ".join(entry.get("methods") or []) or "(no methods)"
        exports = ", ".join(entry.get("exports") or []) or "-"
        lines.append(f"- `{entry['path']}` ({entry.get('kind', 'util')}) exports {exports} — {methods}")
    return "\n".join(lines)


def _build_prompt(
    feature: str,
    ticket_external_id: str,
    cases: Sequence[TestCase],
    entries: Sequence[dict],
    context: dict | None,
) -> str:
    case_lines: list[str] = []
    for case in cases:
        steps = "; ".join(
            f"{step.get('a', '')} -> {step.get('e', '')}" for step in (case.steps or [])
        )
        case_lines.append(f"- {case.code}: {case.title}\n  steps: {steps or '(none)'}")
    project_block = render_project_context(context, include_secrets=False)
    return (
        "Produce the AUTOMATION PLAN for this feature — the REUSE/EXTEND/CREATE "
        "decisions, made BEFORE any code is generated. Emit the plan only; do NOT "
        "write test code, page objects or locators.\n\n"
        f"Feature / ticket: {ticket_external_id} — {feature}\n\n"
        f"Test cases in this feature:\n" + "\n".join(case_lines) + "\n\n"
        + (f"{project_block}\n\n" if project_block else "")
        + f"{render_inventory(entries)}\n\n"
        "Decision order is strict (doc §8): REUSE an existing asset if it can "
        "satisfy the requirement; EXTEND it with a named new method if it is the "
        "right owner but is missing a capability; CREATE only when no existing "
        "asset is a suitable owner.\n"
        "Duplicate detection (doc §21): do NOT plan `pages/CreateUserPage.ts` when "
        "`pages/UserPage.ts` already owns user interactions, and do NOT plan a "
        "second download helper when `utils/waitForDownload.ts` exists. Check the "
        "inventory above for a semantically equivalent owner first. This is checked "
        "server-side: a `create` whose name is an existing inventory entry's name "
        "plus an action word is demoted to an `extend` of that entry.\n"
        "Locator reuse (doc §22): prefer updating the existing page object over "
        "adding a duplicate locator elsewhere — that is an `extend`, not a "
        "`create`.\n"
        "Anything `@q-agent/playwright-base` already provides (the extended `test`, "
        "auth/session plumbing, assertion helpers, waits, dynamic data helpers) is "
        "`reuse-base` and needs no file in this project.\n\n"
        "Respond with this exact JSON shape. `action` is one of "
        '"reuse" | "extend" | "create" | "reuse-base". `path` is project-relative '
        "(e.g. `pages/UserListPage.ts`) and must match an inventory path for "
        "`reuse`/`extend`. `methods` names the signatures the feature needs — for "
        "`extend`, the NEW ones to add; for `reuse`, the existing ones it will "
        "call. `reason` is one short sentence.\n"
        "{\n"
        '  "feature": "User Management",\n'
        '  "specGroups": [{"name": "user-creation", "testCases": ["TC-01"]}],\n'
        '  "pages": [{"name": "UserListPage", "path": "pages/UserListPage.ts", '
        '"action": "reuse", "methods": ["openCreateUser()"], "reason": "..."}],\n'
        '  "components": [], "fixtures": [], "data": [], "utils": []\n'
        "}"
    )


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def plan_for_ticket(
    project: AutomationProject,
    run_code: str,
    ticket_external_id: str,
    cases: Sequence[TestCase],
    context: dict | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """The plan for one ticket/feature, produced once and cached on disk.

    Non-agentic ``claude_cli.run_json`` — cheap and deterministic, no tools, no
    file access. Best-effort by contract: any failure returns
    :func:`empty_plan`, which authorizes nothing and therefore leaves generation
    behaving exactly as it did before this slice.

    Args:
        project: The persistent automation project (supplies the inventory).
        run_code: The owning run's code — plans are scoped per run so a re-run
            replans against the tree as it is then.
        ticket_external_id: The feature's ticket, e.g. ``"SUR-1428"``.
        cases: Every automation-eligible case on this ticket. **All of them**, so
            one plan covers the feature rather than one plan per case.
        context: Resolved project context (KB routes/selectors), secrets excluded.
        force: Replan even when a cached plan exists (used by a forced regen).

    Returns:
        A normalized plan dict (see :func:`normalize`).
    """
    if not force:
        cached = load_plan(project, run_code, ticket_external_id)
        if cached is not None:
            logger.info(
                "automation plan reused from disk for {} {} — {}",
                run_code, ticket_external_id, cached.get("counts") or {},
            )
            return cached

    entries = automation_project_service.inventory(project)
    feature = (cases[0].title if cases else "") or ticket_external_id
    try:
        raw = claude_cli.run_json(
            _build_prompt(feature, ticket_external_id, cases, entries, context),
            system=_SYSTEM_PROMPT,
            skill=AUTOMATION_PLANNER,
            label=f"Plan: {ticket_external_id}",
        )
    except Exception as exc:  # noqa: BLE001 - planning must never break generation
        logger.warning("automation planning failed for {}: {}", ticket_external_id, exc)
        return empty_plan(feature, ticket_external_id)

    plan = normalize(
        raw,
        entries,
        feature=feature,
        ticket=ticket_external_id,
        cases=[c.code for c in cases],
    )
    if is_actionable(plan):
        # Only a real plan is cached: caching an empty one would freeze a transient
        # planning failure in for the rest of the run.
        try:
            _save_plan(project, run_code, ticket_external_id, plan)
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("could not persist automation plan for {}: {}", ticket_external_id, exc)
    log_plan_counts(run_code, ticket_external_id, plan)
    return plan


# ---------------------------------------------------------------------------
# Observability — the epic's success metric, from day one
# ---------------------------------------------------------------------------


def _demoted(plan: dict) -> int:
    """How many ``create``s this plan had demoted as duplicates (doc §21, #571).

    Logged beside reuse/extend/create because it is the real signal on whether the
    planner prompt is working: the headline reuse rate cannot show it (once a
    near-duplicate exists it is legitimately reused from then on), so a rising
    demotion rate is the only visible warning that the model has stopped reading §21.
    """
    value = plan.get("duplicatesDemoted")
    if isinstance(value, int):
        return value
    return len(plan.get("duplicates") or [])


def log_plan_counts(run_code: str, ticket_external_id: str, plan: dict) -> dict[str, int]:
    """Log one plan's reuse/extend/create tally and return it."""
    tally = plan.get("counts") or counts(plan)
    logger.info(
        "automation plan {} {}: reuse={} extend={} create={} reuse-base={} "
        "duplicates-demoted={} (importable={} writable={})",
        run_code,
        ticket_external_id,
        tally.get("reuse", 0),
        tally.get("extend", 0),
        tally.get("create", 0),
        tally.get("reuse-base", 0),
        _demoted(plan),
        len(plan.get("importable") or []),
        len(plan.get("writable") or []),
    )
    return tally


def log_pass_counts(run_code: str, plans: Iterable[dict]) -> dict[str, int]:
    """Roll up every plan in one generation pass into a single log line.

    This is the epic's success metric (*"how little new code is generated while
    still fully covering the new test cases"*) and it has to be observable from
    day one, not after #545 makes it look good.
    """
    total = {action: 0 for action in ACTIONS}
    tickets = 0
    demoted = 0
    for plan in plans:
        tickets += 1
        demoted += _demoted(plan)
        for action, value in (plan.get("counts") or counts(plan)).items():
            if action in total:
                total[action] += value
    logger.info(
        "automation generation pass {} planned {} ticket(s): reuse={} extend={} "
        "create={} reuse-base={} duplicates-demoted={}",
        run_code,
        tickets,
        total["reuse"],
        total["extend"],
        total["create"],
        total["reuse-base"],
        demoted,
    )
    return total


# ---------------------------------------------------------------------------
# Enforcement — the plan constrains what generation may import and write
# ---------------------------------------------------------------------------


def import_violations(code: str, plan: dict | None) -> list[str]:
    """Asset imports in ``code`` that the plan does not authorize.

    The plan's ``importable`` list is the whole authorization: it holds exactly
    the ``reuse``/``extend`` targets that :func:`normalize` confirmed are on disk.
    An import of anything else is a violation — either the file does not exist (so
    the spec would fail collection anyway) or the plan decided a *different* asset
    owns this behaviour, which is precisely the duplicate-creation the epic exists
    to stop.

    Returns ``[]`` when there is no *actionable* plan, so the pre-#544 path — and a
    pass whose planning call failed — is unchanged.
    """
    if not is_actionable(plan):
        return []
    allowed = {p.removesuffix(".ts") for p in (plan.get("importable") or [])}
    violations: list[str] = []
    for _full, relative in _ASSET_IMPORT_RE.findall(code or ""):
        target = relative.removesuffix(".ts").rstrip("/")
        if target.split("/", 1)[0] not in automation_project_service.LIBRARY_DIRS:
            continue
        if target not in allowed and target not in violations:
            violations.append(target)
    return violations


def unplanned_new_paths(before: Sequence[str], after: Sequence[str], plan: dict | None) -> list[str]:
    """Library files that appeared but the plan never authorized writing.

    The literal form of the slice's constraint: *a case whose plan says ``reuse``
    must not produce a new file.* ``reuse`` puts nothing in ``writable``, so any
    new asset file under a ``reuse``-only plan shows up here.
    """
    if not is_actionable(plan):
        return []
    allowed = set(plan.get("writable") or [])
    return sorted(set(after) - set(before) - allowed)


def render_plan(plan: dict | None) -> str:
    """The plan as a generation-prompt block: what may be imported, what may not.

    Since #545 there is no asymmetry left to explain. The project editor has
    already authored every ``create`` and ``extend`` the plan asked for, and
    :func:`refresh_plan` re-derived ``importable`` from the resulting tree — so this
    block simply reports which files are on disk, with the signatures they really
    export. **Locators belong in a page object; an inline locator in a spec is the
    exception**, taken only for an asset the plan named but that is not in the
    importable list (i.e. its authoring did not land).
    """
    if not is_actionable(plan):
        return ""
    lines = ["AUTOMATION PLAN for this feature (decided before generation, doc §24):"]
    allowed = set(plan.get("importable") or [])
    importable = [
        entry
        for group in ASSET_GROUPS
        for entry in (plan.get(group) or [])
        if entry.get("path") in allowed
    ]
    missing = [
        entry
        for group in ASSET_GROUPS
        for entry in (plan.get(group) or [])
        if entry.get("path") and entry.get("path") not in allowed
    ]
    if importable:
        lines.append(
            "- IMPORTABLE — these files exist in this project and were authored/"
            "extended for exactly this feature. Import them at the real spec depth "
            "(`../../pages/Foo`) and call ONLY the signatures listed. Drive the UI "
            "THROUGH them rather than repeating their locators here:"
        )
        for entry in importable:
            methods = ", ".join(entry.get("existingMethods") or []) or "(no methods yet)"
            lines.append(f"  - `{entry['path']}` ({entry['action']}) — {methods}")
    if missing:
        lines.append(
            "- NOT ON DISK — these were planned but are not in the project, so "
            "importing one FAILS collection and the spec is rejected. For those "
            "steps only, an inline locator is the accepted exception: "
            + "; ".join(f"{entry['name']} (`{entry['path']}`)" for entry in missing)
        )
    lines.append(
        "- Import NOTHING else from `../../pages/`, `../../components/`, "
        "`../../fixtures/`, `../../data/` or `../../utils/`. The list above is "
        "exhaustive and was verified against the project's real tree."
    )
    return "\n".join(lines)
