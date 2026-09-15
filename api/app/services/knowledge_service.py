"""Project Knowledge Base build — invokes Claude via the project-bootstrap skill.

Given a project's identity (name, provider, repo, framework) and its user-authored
config (base URL, local repo clone path, environments, test-account roles) Claude
produces a structured knowledge base: stack, architecture, domain, locator
strategy, **base URL, application routes, real selector/testid examples, auth flow,
environments, business entities**, and counts of existing Playwright assets.

When a local repo clone path is configured, the Claude CLI runs there so its file
tools traverse the real source — turning inferred structure into discovered fact
and eliminating placeholders downstream. Real Claude only (ADR 0001); errors
propagate to the caller.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from app import db as db_module
from app.config import settings
from app.db import utcnow
from app.logging import logger
from app.models.knowledge import ProjectKnowledge, compose_key
from app.services import audit_service, project_config_service, repo_service, run_context
from app.services.claude_cli import run_json
from app.services.skills import PROJECT_BOOTSTRAP
from app.services.workspace_scope import scoped_knowledge_dir, slug

if TYPE_CHECKING:
    from app.models.project_config import ProjectConfig

# Row keys with a build currently in flight — guards against double-triggering a
# (potentially minutes-long) bootstrap while one is already running in-process.
_building: set[str] = set()


def is_building(row_key: str) -> bool:
    return row_key in _building


def _config_hints(config: "ProjectConfig | None") -> str:
    """Render the user-authored config as grounding facts for the build prompt."""
    if config is None:
        return "No project configuration has been provided yet.\n"
    lines: list[str] = []
    if config.base_url:
        lines.append(f"- Application base URL: {config.base_url}")
    if config.local_repo_path:
        lines.append(
            f"- A local checkout of the application source is available at: "
            f"{config.local_repo_path}. Traverse it with your file tools to discover "
            f"real routes, data-testids/selectors, page objects, fixtures and the auth flow."
        )
    for env in config.environments or []:
        name = env.get("name", "")
        url = env.get("base_url", "")
        if name or url:
            lines.append(f"- Environment '{name}': {url}")
    roles = [a.get("role", "") for a in (config.test_accounts or []) if a.get("role")]
    if roles:
        lines.append(f"- Configured test-account roles: {', '.join(roles)}")
    return "\n".join(lines) + "\n" if lines else "No project configuration has been provided yet.\n"


def _build_prompt(name: str, provider: str, repo: str, framework: str, config) -> str:
    return (
        "Build a Project Knowledge Base for this software project so a QA "
        "automation agent can generate runnable Playwright tests with NO manual "
        "placeholders. Discover concrete, reusable facts.\n\n"
        f"Project name: {name}\n"
        f"Provider: {provider or 'unknown'}\n"
        f"Repository: {repo or 'unknown'}\n"
        f"Automation framework: {framework or 'Playwright'}\n\n"
        "Known project configuration (treat as authoritative):\n"
        f"{_config_hints(config)}\n"
        "Return a JSON object with EXACTLY these keys:\n"
        '{"branch": string, "stack": string[], "architecture": string, '
        '"domain": string, "locator": string, "base_url": string, '
        '"routes": [{"path": string, "description": string, "auth_required": boolean}], '
        '"selectors": [{"screen": string, "element": string, "selector": string}], '
        '"auth": {"login_flow": string, "login_url": string, "storage_state": string}, '
        '"environments": [{"name": string, "base_url": string, "notes": string}], '
        '"business_entities": string[], "assets": number, "pageObjects": number, '
        '"page_object_names": string[], "fixtures": number, "fixture_names": string[], '
        '"utilities": string[], '
        '"test_conventions": {"spec_roots": string[], "spec_naming": string, '
        '"structure": string, "assertion_style": string, "data": string}, '
        '"confidence": number (0-100)}\n'
        "- base_url: the primary application URL (use the configured one if given).\n"
        "- routes: real application routes/URL patterns a test would navigate to.\n"
        "- selectors: real, stable selectors (prefer data-testid / role) found in the code.\n"
        "- auth: how a test logs in — flow summary, the login URL, and any storageState path.\n"
        "- architecture/domain: 1-2 sentences each.\n"
        "- assets/pageObjects/fixtures: best-estimate COUNTS of existing Playwright assets.\n"
        "- page_object_names/fixture_names: the actual names of reusable assets to reuse.\n"
        "- test_conventions: how this team ALREADY writes its e2e tests, from their own "
        "suite — where specs live, how files and test titles are named, how a spec is "
        "structured (describe/test nesting, hooks, step granularity), the assertion "
        "idioms actually used, and how test data/accounts are supplied. One short "
        "sentence each; leave a field empty rather than guessing, and omit the whole "
        "object if the project has no tests yet.\n"
        "- confidence: how confident this knowledge base is (0-100). Lower it for anything guessed."
    )


#: The fields of ``test_conventions``, and the per-field character ceiling.
#: These land in EVERY downstream prompt (spec generation, page-object authoring,
#: the planner), so a chatty model must not be able to grow them without bound.
_CONVENTION_FIELDS = ("spec_naming", "structure", "assertion_style", "data")
_CONVENTION_CHARS = 400
_SPEC_ROOTS_MAX = 6


def _normalise_test_conventions(raw: Any) -> dict[str, Any]:
    """Coerce the model's ``test_conventions`` into a bounded, known shape.

    Unknown keys are dropped, values are stringified and clamped, and empty fields
    are omitted entirely so :func:`prompts.render_project_context` can treat
    "absent" and "blank" identically. A non-dict (the model answering with prose,
    or with null) yields ``{}``.

    :param raw: Whatever came back under ``test_conventions``.
    :returns: A dict with only the known keys, or ``{}``.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    roots = raw.get("spec_roots")
    if isinstance(roots, list):
        cleaned = [str(r).strip() for r in roots if str(r or "").strip()]
        if cleaned:
            out["spec_roots"] = cleaned[:_SPEC_ROOTS_MAX]
    for field in _CONVENTION_FIELDS:
        value = raw.get(field)
        if isinstance(value, (list, tuple)):
            value = "; ".join(str(v).strip() for v in value if str(v or "").strip())
        text = str(value or "").strip()
        if text:
            out[field] = text[:_CONVENTION_CHARS]
    return out


def build_knowledge_payload(
    name: str,
    provider: str,
    repo: str,
    framework: str,
    *,
    config: "ProjectConfig | None" = None,
    repo_path: str | None = None,
    timeout: int | None = None,
) -> dict[str, Any]:
    """Call Claude (project-bootstrap skill) and normalize the JSON result.

    When ``repo_path`` points at an existing checkout (a configured local path or a
    freshly cloned/pulled remote), the CLI runs there so Claude reads the real
    source instead of inferring it. Falls back to ``config.local_repo_path``.
    ``timeout`` defaults to the (longer) bootstrap budget.
    """
    cwd = repo_path or (config.local_repo_path if (config and config.local_repo_path) else None)
    raw = run_json(
        _build_prompt(name, provider, repo, framework, config),
        skill=PROJECT_BOOTSTRAP,
        include_template=True,
        label=f"Build knowledge: {name}",
        cwd=cwd,
        timeout=timeout or settings.claude_bootstrap_timeout_s,
    )
    data = raw if isinstance(raw, dict) else {}
    confidence = int(data.get("confidence", 80) or 0)
    confidence = max(0, min(100, confidence))
    knowledge = {
        "branch": data.get("branch", "main"),
        "stack": data.get("stack", []) or [],
        "architecture": data.get("architecture", ""),
        "domain": data.get("domain", ""),
        "locator": data.get("locator", ""),
        "base_url": data.get("base_url", ""),
        "routes": data.get("routes", []) or [],
        "selectors": data.get("selectors", []) or [],
        "auth": data.get("auth", {}) or {},
        "environments": data.get("environments", []) or [],
        "business_entities": data.get("business_entities", []) or [],
        "assets": int(data.get("assets", 0) or 0),
        "pageObjects": int(data.get("pageObjects", 0) or 0),
        "page_object_names": data.get("page_object_names", []) or [],
        "fixtures": int(data.get("fixtures", 0) or 0),
        "fixture_names": data.get("fixture_names", []) or [],
        "utilities": data.get("utilities", []) or [],
        # How the team already writes its tests (#872). The checkout-reading paths
        # (#868/#870) are richer, but they need a local clone; this is the channel
        # that survives a remote-only repo or an agent-dispatched run.
        "test_conventions": _normalise_test_conventions(data.get("test_conventions")),
    }
    return {"knowledge": knowledge, "confidence": confidence}


def write_knowledge_files(row: ProjectKnowledge, config: "ProjectConfig | None" = None) -> str:
    """Emit the skill's knowledge.json + knowledge.md artifacts under the row owner's
    scoped knowledge dir (ADR 0009 — ``workspace/<scope>/knowledge/<key>/``).

    project-bootstrap's contract is to persist the Project Knowledge Base as files
    (knowledge.md + knowledge.json) that downstream skills read; we mirror the DB
    row into those files and merge the user-authored config (base URL, environments,
    test-account roles) so the artifacts are a single, consistent project context.
    Test-account passwords are NEVER written to these on-disk artifacts. Returns the
    directory path.
    """
    kn = row.knowledge or {}
    # Per-repo artifacts nest under the project: <scope>/knowledge/<project>/<repo>/.
    project_slug = slug(row.project_key or row.key)
    out_dir = scoped_knowledge_dir(row.owner_id) / project_slug
    if row.repo:
        out_dir = out_dir / slug(row.repo)
    out_dir.mkdir(parents=True, exist_ok=True)

    base_url = (config.base_url if config and config.base_url else "") or kn.get("base_url", "")
    environments = (config.environments if config and config.environments else None) or kn.get(
        "environments", []
    )
    test_account_roles = (
        [
            {"role": a.get("role", ""), "username": a.get("username", ""), "notes": a.get("notes", "")}
            for a in (config.test_accounts or [])
        ]
        if config
        else []
    )

    # knowledge.json — shaped after skills/project-bootstrap/templates/knowledge.json.
    doc = {
        "project_name": row.name,
        "repository": row.repo,
        "branch": kn.get("branch", "main"),
        "purpose": "",
        "framework": kn.get("stack", [None])[0] if kn.get("stack") else "",
        "automation": row.framework,
        "language": "TypeScript",
        "stack": kn.get("stack", []),
        "architecture": kn.get("architecture", ""),
        "business_domain": kn.get("domain", ""),
        "business_entities": kn.get("business_entities", []),
        "base_url": base_url,
        "locator_strategy": kn.get("locator", ""),
        "routes": kn.get("routes", []),
        "selectors": kn.get("selectors", []),
        "auth": kn.get("auth", {}),
        "environments": environments,
        "test_accounts": test_account_roles,  # roles/usernames only — no secrets
        "existing_assets": {
            "spec_files": kn.get("assets", 0),
            "page_objects": kn.get("pageObjects", 0),
            "page_object_names": kn.get("page_object_names", []),
            "fixtures": kn.get("fixtures", 0),
            "fixture_names": kn.get("fixture_names", []),
        },
        "reusable_utilities": kn.get("utilities", []),
        "test_conventions": kn.get("test_conventions", {}),
        "confidence": row.confidence,
        "version": row.version,
        "indexed_at": row.last_indexed.isoformat() if row.last_indexed else None,
    }
    (out_dir / "knowledge.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")

    utilities = "\n".join(f"- `{u}`" for u in kn.get("utilities", [])) or "- _none discovered_"
    stack = ", ".join(kn.get("stack", [])) or "—"
    routes_md = (
        "\n".join(
            f"- `{r.get('path', '')}` — {r.get('description', '')}"
            f"{' (auth required)' if r.get('auth_required') else ''}"
            for r in kn.get("routes", [])
        )
        or "- _none discovered_"
    )
    selectors_md = (
        "\n".join(
            f"- {s.get('screen', '')}: {s.get('element', '')} → `{s.get('selector', '')}`"
            for s in kn.get("selectors", [])
        )
        or "- _none discovered_"
    )
    envs_md = (
        "\n".join(
            f"- **{e.get('name', '')}**: {e.get('base_url', '')} {e.get('notes', '')}".rstrip()
            for e in environments
        )
        or "- _none configured_"
    )
    accounts_md = (
        "\n".join(
            f"- **{a['role'] or 'account'}**: `{a['username']}` "
            f"(password stored securely in Q-Agent) {a['notes']}".rstrip()
            for a in test_account_roles
        )
        or "- _none configured_"
    )
    auth = kn.get("auth", {})
    md = f"""# Project Knowledge Base — {row.name}

- **Repository:** {row.repo or "—"}
- **Branch:** {kn.get("branch", "main")}
- **Automation framework:** {row.framework}
- **Base URL:** {base_url or "—"}
- **Confidence:** {row.confidence}%  ·  **Version:** {row.version}

## Technology stack
{stack}

## Application architecture
{kn.get("architecture", "—")}

## Business domain
{kn.get("domain", "—")}

## Business entities
{", ".join(kn.get("business_entities", [])) or "—"}

## Locator strategy
{kn.get("locator", "—")}

## Application routes
{routes_md}

## Known selectors
{selectors_md}

## Authentication
- **Login flow:** {auth.get("login_flow", "—")}
- **Login URL:** {auth.get("login_url", "—")}
- **storageState:** {auth.get("storage_state", "—")}

## Environments
{envs_md}

## Test accounts
{accounts_md}

## Existing Playwright assets
- Spec files: {kn.get("assets", 0)}
- Page objects: {kn.get("pageObjects", 0)} {", ".join(kn.get("page_object_names", []))}
- Shared fixtures: {kn.get("fixtures", 0)} {", ".join(kn.get("fixture_names", []))}

## Reusable test utilities
{utilities}

## AI Context Summary
{row.name} ({stack}) at base URL {base_url or "(unset)"}. {kn.get("architecture", "")}
Domain: {kn.get("domain", "")} Prefer the locator strategy above and the listed routes,
selectors, auth flow and reusable assets. Test-account credentials are supplied to the
automation generator from Q-Agent's secure store — reference them by role.
"""
    (out_dir / "knowledge.md").write_text(md, encoding="utf-8")
    return str(out_dir)


#: Sections of the knowledge blob whose entries a human can pin, and the key
#: each section's entries are identified by. Both are the lists the three
#: machine-merge paths below already key on, so "what can be pinned" and "what a
#: heal or an exploration can rewrite" stay the same set by construction.
PINNABLE_SECTIONS: dict[str, str] = {"routes": "path", "selectors": "selector"}


def carry_pinned_forward(previous: dict[str, Any], rebuilt: dict[str, Any]) -> dict[str, Any]:
    """Re-apply the human-pinned entries of ``previous`` onto a rebuilt blob (#827).

    **This is the highest-risk detail in the Business Knowledge epic**, and it is
    a data-loss one rather than a crash: ``apply_build`` replaces
    ``row.knowledge`` wholesale, so without this a ``project-bootstrap`` rebuild
    destroys every manual correction *silently* — no error, no log, the
    corrections are simply gone the next time somebody rebuilds. ADR 0016 §5
    names it as the reason making the code KB editable (#828) is gated on this
    slice.

    Only entries carrying a truthy ``pinned`` are carried, and a carried entry
    **replaces** the rebuilt entry it collides with rather than sitting beside
    it — two entries for one selector would both reach every prompt, with the
    machine's contradicting the human's. Everything else about the rebuilt blob
    is left exactly as the build produced it, so a rebuild still sheds entries
    the code no longer has.

    Args:
        previous: The blob being replaced (``{}`` on a first index).
        rebuilt: The build's fresh blob. Not mutated.

    Returns:
        A new blob: ``rebuilt`` with the previous blob's pinned entries applied.
    """
    merged = dict(rebuilt or {})
    for section, id_key in PINNABLE_SECTIONS.items():
        pinned = [
            entry
            for entry in (previous or {}).get(section) or []
            if isinstance(entry, dict) and entry.get("pinned")
        ]
        if not pinned:
            continue
        entries = [e for e in (merged.get(section) or []) if isinstance(e, dict)]
        by_id = {entry.get(id_key): i for i, entry in enumerate(entries) if entry.get(id_key)}
        for entry in pinned:
            index = by_id.get(entry.get(id_key))
            if index is None:
                entries.append(entry)
            else:
                entries[index] = entry
        merged[section] = entries
    return merged


#: Stamp applied to every entry a human edits through ``PATCH .../knowledge``
#: (#828). ``pinned`` is the flag the no-clobber rule and
#: :func:`carry_pinned_forward` already honour (#827) — a manual edit is an
#: ordinary entry wearing it, not a second kind of record. ``origin`` says who
#: wrote it, which is what the UI reads to badge the entry.
MANUAL_STAMP: dict[str, Any] = {"origin": "manual", "pinned": True}

#: Scalar (non-list) knowledge fields a human can edit. They have no per-entry
#: identity to hang ``pinned`` on, so the blob records *which* of them a human
#: set under :data:`PINNED_FIELDS_KEY` instead.
EDITABLE_FIELDS: tuple[str, ...] = ("domain", "business_entities")

#: Blob key holding the names of the scalar fields a human has overridden.
PINNED_FIELDS_KEY = "pinned_fields"


def _normalise_route(entry: dict[str, Any]) -> dict[str, Any]:
    """Coerce one submitted route to the blob's stored shape.

    The stored blob is snake_cased (``auth_required``) while the SPA's own type
    is camelCase (``authRequired``), and route entries travel in the request body
    as opaque dicts that no alias generator reaches. Accept either spelling and
    store one.

    Args:
        entry: A route as submitted — ``path`` plus any of ``description``,
            ``auth_required`` / ``authRequired``.

    Returns:
        A new dict in stored shape, carrying :data:`MANUAL_STAMP`.
    """
    auth_required = entry.get("auth_required", entry.get("authRequired", False))
    return {
        "path": str(entry.get("path", "") or "").strip(),
        "description": str(entry.get("description", "") or ""),
        "auth_required": bool(auth_required),
        **MANUAL_STAMP,
    }


def _normalise_selector(entry: dict[str, Any]) -> dict[str, Any]:
    """Coerce one submitted selector to the blob's stored shape (see above)."""
    return {
        "screen": str(entry.get("screen", "") or ""),
        "element": str(entry.get("element", "") or ""),
        "selector": str(entry.get("selector", "") or "").strip(),
        **MANUAL_STAMP,
    }


def apply_manual_edits(
    knowledge: dict[str, Any],
    *,
    routes: list[dict[str, Any]] | None = None,
    selectors: list[dict[str, Any]] | None = None,
    domain: str | None = None,
    business_entities: list[str] | None = None,
) -> tuple[dict[str, Any], int]:
    """Merge a human's per-entry edits into a knowledge blob (#828, ADR 0016 §5).

    Additive to the existing JSON blob — no migration, no new table. Each edited
    route/selector is **upserted by its identity key** (``path`` / ``selector``,
    the same keys the machine-merge paths already key on) and stamped
    :data:`MANUAL_STAMP`, so what a human produces here is exactly the kind of
    entry #827's no-clobber rule and :func:`carry_pinned_forward` protect. An
    edit therefore survives both a self-heal and a full rebuild.

    Sections not mentioned in the call are left untouched: this is a PATCH, so
    omitting ``routes`` means "leave the routes alone", never "delete them".

    Args:
        knowledge: The current blob. **Not mutated** — a new dict is returned, so
            SQLAlchemy sees the JSON column change.
        routes: Routes to upsert, keyed on ``path``.
        selectors: Selectors to upsert, keyed on ``selector``.
            An entry may carry ``replaces`` — the identity of the entry it
            supersedes — which is how *correcting* a wrong value works: the
            identity key IS the value being fixed, so without it "``#wrong``
            should be ``#right``" would add ``#right`` and leave ``#wrong``
            standing in every prompt. ``replaces`` is a locator, not stored.
        domain: Replacement business-domain prose, when given.
        business_entities: Replacement business-entity list, when given.

    Returns:
        ``(new_blob, edited_count)`` — the count is every entry and scalar field
        actually written, so a caller can refuse a patch that says nothing.

    Raises:
        ValueError: An entry has a blank identity key (a route with no ``path``,
            a selector with no ``selector``). Such an entry could never be found
            again, so it is refused rather than appended as an orphan.
    """
    merged = dict(knowledge or {})
    edited = 0

    for section, id_key, normalise, submitted in (
        ("routes", "path", _normalise_route, routes),
        ("selectors", "selector", _normalise_selector, selectors),
    ):
        if submitted is None:
            continue
        entries = [e for e in (merged.get(section) or []) if isinstance(e, dict)]
        by_id = {e.get(id_key): i for i, e in enumerate(entries) if e.get(id_key)}
        for raw in submitted:
            raw = raw if isinstance(raw, dict) else {}
            entry = normalise(raw)
            if not entry.get(id_key):
                raise ValueError(f"A {section[:-1]} edit needs a non-empty '{id_key}'")
            replaces = str(raw.get("replaces") or "").strip()
            index = by_id.get(replaces) if replaces else by_id.get(entry[id_key])
            if index is None:
                by_id[entry[id_key]] = len(entries)
                entries.append(entry)
            else:
                # Keep what the machine discovered about this entry that the edit
                # form does not carry (e.g. ``verified_at_runtime``), then let the
                # human's fields win.
                entries[index] = {**entries[index], **entry}
                by_id.pop(replaces, None)
                by_id[entry[id_key]] = index
            edited += 1
        merged[section] = entries

    pinned_fields = [f for f in (merged.get(PINNED_FIELDS_KEY) or []) if isinstance(f, str)]
    for field, value in (("domain", domain), ("business_entities", business_entities)):
        if value is None:
            continue
        merged[field] = value
        if field not in pinned_fields:
            pinned_fields.append(field)
        edited += 1
    if pinned_fields:
        merged[PINNED_FIELDS_KEY] = pinned_fields
    return merged, edited


def carry_pinned_fields(previous: dict[str, Any], rebuilt: dict[str, Any]) -> dict[str, Any]:
    """Re-apply human-overridden **scalar** fields onto a rebuilt blob (#828).

    The list sections are handled by :func:`carry_pinned_forward`; this is its
    counterpart for the fields that have no per-entry identity to pin
    (``domain``, ``business_entities``). Without it, making those editable would
    ship a known silent data-loss hole — the exact failure ADR 0016 §5 calls the
    highest-risk detail in the epic, one field over.

    Args:
        previous: The blob being replaced.
        rebuilt: The build's fresh blob. Not mutated.

    Returns:
        A new blob carrying the previous blob's overridden scalar fields.
    """
    fields = [f for f in (previous or {}).get(PINNED_FIELDS_KEY) or [] if f in EDITABLE_FIELDS]
    if not fields:
        return dict(rebuilt or {})
    merged = dict(rebuilt or {})
    for field in fields:
        merged[field] = (previous or {}).get(field)
    merged[PINNED_FIELDS_KEY] = fields
    return merged


def apply_build(
    row: ProjectKnowledge, payload: dict[str, Any], *, config: "ProjectConfig | None" = None
) -> None:
    """Persist a build result onto a ProjectKnowledge row (caller commits).

    First index stays ``v1``; each subsequent (re)build increments the version.

    A **rebuild** carries the previous blob's human-pinned entries forward
    (:func:`carry_pinned_forward`) and its human-overridden scalar fields
    (:func:`carry_pinned_fields`); a first index has nothing to carry.
    """
    # Detect a rebuild by prior success (the status is transiently "indexing" here).
    rebuild = row.last_indexed is not None
    if rebuild:
        try:
            n = int((row.version or "v1").lstrip("v") or "1")
        except ValueError:
            n = 1
        row.version = f"v{n + 1}"
    else:
        row.version = "v1"
    row.knowledge = (
        carry_pinned_fields(
            row.knowledge or {}, carry_pinned_forward(row.knowledge or {}, payload["knowledge"])
        )
        if rebuild
        else payload["knowledge"]
    )
    row.confidence = payload["confidence"]
    row.status = "indexed"
    row.needs_refresh = False
    row.last_indexed = utcnow()
    row.last_error = ""
    # Persist the skill's knowledge.md + knowledge.json artifacts to the workspace.
    row.doc_path = write_knowledge_files(row, config)


def _resolve_path_for_row(db, row: ProjectKnowledge, config) -> str | None:
    """Resolve the checkout to traverse for a knowledge row (per-repo, else legacy).

    The clone PAT lookup is scoped to the row's own ``owner_id`` (#93 — private
    per-user data), so a build only ever clones with that user's own repository
    connection credentials.
    """
    project_key = row.project_key or row.key
    owner_id = row.owner_id
    repos = project_config_service.get_repos(config)
    repo_entry = next((r for r in repos if r.get("name") == row.repo), None) if row.repo else None
    if repo_entry is not None:
        return repo_service.resolve_one_repo(
            db, project_key, repo_entry, provider_display=row.provider, owner_id=owner_id
        )
    return repo_service.resolve_repo_path(
        db, project_key, config, provider_display=row.provider, repo=row.repo, owner_id=owner_id
    )


def start_build(row_key: str) -> None:
    """Kick off a background knowledge build for a row (no-op if already running).

    The row must already exist with ``status='indexing'`` (set by the caller in the
    request transaction). The build — repo clone/pull + Claude traversal — can take
    minutes, so it runs off the request thread; the UI polls the row's status.
    """
    if row_key in _building:
        return
    _building.add(row_key)
    threading.Thread(target=_run_build, args=(row_key,), daemon=True).start()


def _run_build(row_key: str) -> None:
    db = db_module.SessionLocal()
    try:
        row = db.query(ProjectKnowledge).filter(ProjectKnowledge.key == row_key).first()
        if row is None:
            return
        project_key = row.project_key or row.key
        # Shared builds (owner_id is None) must read the shared config, not a
        # same-keyed row a member owns (their clone) — scope the lookup to the
        # row's owner. Normal per-user builds keep the existing behavior.
        config = (
            project_config_service.get_config_for_owner(db, project_key, None)
            if row.owner_id is None
            else project_config_service.get_config(db, project_key)
        )
        try:
            repo_path = _resolve_path_for_row(db, row, config)
            # Attribute the Claude call to the row's owner so it resolves that
            # user's own/preferred credentials (mirrors the clone PAT + output
            # scoping above). A shared build (owner_id is None) still resolves the
            # shared credential. Without this, the build thread has no ambient run,
            # so the Claude call would fall back to the shared credential (#466).
            with run_context.owner_scope(row.owner_id):
                payload = build_knowledge_payload(
                    row.name, row.provider, row.repo, row.framework, config=config, repo_path=repo_path
                )
            apply_build(row, payload, config=config)
            db.commit()
            audit_service.record(
                category="knowledge", actor_type="ai", action="Built project knowledge base",
                target=f"{row.name} · {row.version}", meta=f"{row.confidence}% confidence",
            )
        except Exception as exc:  # noqa: BLE001 - surface on the row, don't crash the thread
            db.rollback()
            row = db.query(ProjectKnowledge).filter(ProjectKnowledge.key == row_key).first()
            if row is not None:
                row.status = "error"
                row.last_error = str(exc)[:1000]
                db.commit()
            logger.error("Knowledge build failed for {}: {}", row_key, exc)
    finally:
        _building.discard(row_key)
        db.close()


def propose_selector_fix(
    project_key: str, repo: str, old_selector: str, new_selector: str, owner_id: int | None = None
) -> bool:
    """Best-effort: write a self-heal's corrected selector back into the KB (#182).

    When a self-heal changes a spec's selector and the fix then passes, this
    corrects the matching ``selectors`` entry (same screen/element, ``old_selector``
    value) in the project's ``ProjectKnowledge`` row, so future generations reuse
    the healed value instead of repeating the same broken selector.

    NO-CLOBBER (#827, ADR 0016 §5): an entry a human has ``pinned`` is never
    rewritten. A correction is the only signal that the machine was wrong, and a
    self-heal that silently undid one would not be made twice.

    Looks up the per-repo row first, falling back to the legacy project-level row
    (mirrors ``project_config_service.build_context``'s KB resolution). Opens its
    own session so it never interferes with the caller's (heal loop) transaction.
    Never raises — any failure is logged and treated as a skipped proposal, since
    heal->KB feedback is additive, not correctness-critical.

    Args:
        project_key: The project the selector belongs to.
        repo: The target repository name ("" for the legacy project-level row).
        old_selector: The selector value the heal replaced.
        new_selector: The selector value the heal replaced it with (now passing).
        owner_id: The knowledge row's owner (ADR 0009) — scopes the lookup to the
            same private/shared namespace the heal's run belongs to.

    Returns:
        True if a matching selector entry was found and updated, False otherwise
        (no matching row/entry, or any error).
    """
    if not project_key or not old_selector or not new_selector or old_selector == new_selector:
        return False
    db = db_module.SessionLocal()
    try:
        row = None
        if repo:
            row = (
                db.query(ProjectKnowledge)
                .filter(
                    ProjectKnowledge.key == compose_key(project_key, repo),
                    ProjectKnowledge.owner_id == owner_id,
                )
                .first()
            )
        if row is None:
            row = (
                db.query(ProjectKnowledge)
                .filter(ProjectKnowledge.key == project_key, ProjectKnowledge.owner_id == owner_id)
                .first()
            )
        if row is None:
            return False

        kn = dict(row.knowledge or {})
        selectors = list(kn.get("selectors") or [])
        updated = False
        for i, sel in enumerate(selectors):
            if not isinstance(sel, dict) or sel.get("selector") != old_selector:
                continue
            if sel.get("pinned"):
                # No-clobber (#827, ADR 0016 §5): a human correction outranks a
                # self-heal's guess. The heal's own spec edit still stands; only
                # the write-back into the KB is declined.
                continue
            selectors[i] = {**sel, "selector": new_selector}
            updated = True
        if not updated:
            return False

        kn["selectors"] = selectors
        row.knowledge = kn  # reassign so SQLAlchemy tracks the JSON change
        db.commit()
        write_knowledge_files(row)
        logger.info(
            "Self-heal proposed KB selector fix for {}: {!r} -> {!r}",
            project_key, old_selector, new_selector,
        )
        return True
    except Exception as exc:  # noqa: BLE001 - heal->KB feedback is best-effort
        db.rollback()
        logger.warning("Self-heal KB selector proposal failed for {}: {}", project_key, exc)
        return False
    finally:
        db.close()


def _element_label_from_selector(selector: str) -> str:
    """Best-effort human-ish element label derived from a raw selector string.

    Pulls the most identifying token out of common selector shapes so a
    DOM-discovered ``selectors`` entry reads sensibly (e.g. ``#login-submit`` ->
    ``login-submit``, ``[data-testid="email"]`` -> ``email``). Falls back to "".
    """
    import re

    m = re.search(r'data-test(?:id)?\s*=\s*[\'"]([^\'"]+)', selector)
    if m:
        return m.group(1)
    m = re.search(r"#([A-Za-z0-9_-]+)", selector)
    if m:
        return m.group(1)
    return ""


def merge_discovered_dom(
    project_key: str, repo: str, discovered: dict[str, Any], owner_id: int | None = None
) -> int:
    """Best-effort: ADD routes/selectors discovered by a DOM-grounded heal pass to the KB (#249).

    When a self-heal grounded on the live page DOM passes, the route the spec
    exercised and the selectors it used are, by definition, real. This *adds* any
    of them the KB doesn't already know (it never rewrites existing entries — that
    is ``propose_selector_fix``'s job), tagging added entries ``source="dom-heal"``
    for provenance. Because it only ever *adds*, a human-``pinned`` entry is
    already safe here: a colliding discovery dedups against it and is dropped
    (#827 — the explicit ``pinned`` guard belongs on the two paths that rewrite,
    ``propose_selector_fix`` and ``merge_verified_discovery``). Adding grounding to a KB that had none is what lets a future
    generation stop hitting the ``blocked`` gate.

    Looks up the per-repo row first, falling back to the legacy project-level row
    (mirrors ``propose_selector_fix`` / ``build_context``). Opens its own session
    and never raises — heal->KB feedback is additive, not correctness-critical.

    Args:
        project_key: The project the discovery belongs to.
        repo: Target repository name ("" for the legacy project-level row).
        discovered: ``{"route": "/path", "selectors": ["#a", ...], "screen"?: str}``
            — the route/selectors a passing DOM-grounded heal exercised.
        owner_id: The knowledge row's owner (ADR 0009) — scopes the lookup.

    Returns:
        The number of new entries added (0 if nothing new, no row, or on error).
    """
    if not project_key:
        return 0
    route = (discovered.get("route") or "").strip()
    selectors = [s for s in (discovered.get("selectors") or []) if s]
    if not route and not selectors:
        return 0
    screen = (discovered.get("screen") or route.strip("/") or "home") or "home"

    db = db_module.SessionLocal()
    try:
        row = None
        if repo:
            row = (
                db.query(ProjectKnowledge)
                .filter(
                    ProjectKnowledge.key == compose_key(project_key, repo),
                    ProjectKnowledge.owner_id == owner_id,
                )
                .first()
            )
        if row is None:
            row = (
                db.query(ProjectKnowledge)
                .filter(ProjectKnowledge.key == project_key, ProjectKnowledge.owner_id == owner_id)
                .first()
            )
        if row is None:
            return 0

        kn = dict(row.knowledge or {})
        added = 0

        if route:
            routes = list(kn.get("routes") or [])
            known_paths = {r.get("path") for r in routes if isinstance(r, dict)}
            if route not in known_paths:
                routes.append(
                    {
                        "path": route,
                        "description": "Discovered during self-heal",
                        "auth_required": False,
                        "source": "dom-heal",
                    }
                )
                kn["routes"] = routes
                added += 1

        if selectors:
            kb_selectors = list(kn.get("selectors") or [])
            known_selectors = {s.get("selector") for s in kb_selectors if isinstance(s, dict)}
            for sel in selectors:
                if sel in known_selectors:
                    continue
                kb_selectors.append(
                    {
                        "screen": screen,
                        "element": _element_label_from_selector(sel),
                        "selector": sel,
                        "source": "dom-heal",
                    }
                )
                known_selectors.add(sel)
                added += 1
            if added:
                kn["selectors"] = kb_selectors

        if not added:
            return 0

        row.knowledge = kn  # reassign so SQLAlchemy tracks the JSON change
        db.commit()
        write_knowledge_files(row)
        logger.info(
            "Self-heal DOM discovery added {} KB entr{} for {} (route={!r}, {} selectors)",
            added, "y" if added == 1 else "ies", project_key, route, len(selectors),
        )
        return added
    except Exception as exc:  # noqa: BLE001 - heal->KB feedback is best-effort
        db.rollback()
        logger.warning("Self-heal KB DOM merge failed for {}: {}", project_key, exc)
        return 0
    finally:
        db.close()


def merge_verified_discovery(
    project_key: str,
    repo: str,
    discovered: dict[str, Any],
    *,
    owner_id: int | None = None,
    source: str = "exploration",
) -> int:
    """Best-effort: record RUNTIME-VERIFIED routes/selectors into the KB (#325, ADR 0010 §5).

    When the DOM Exploration Agent drives the live app and observes real routes and
    selectors, those discoveries are — by definition — runtime facts. This merges
    them into the target repo's ``ProjectKnowledge`` row, stamping each merged entry
    with ``verified_at_runtime`` (ISO-8601 UTC) and ``source``; selector entries
    additionally carry the locator ``strategy`` that actually worked. Runtime-verified
    entries take priority over source-inferred ones during later generation (ADR 0010 §6).

    Merge semantics (extends ``merge_discovered_dom``): dedup by ``path`` (routes) and
    by ``selector`` value (selectors). NO-CLOBBER — an existing entry that already has
    a truthy ``verified_at_runtime`` **or ``pinned``** is never overwritten (the
    colliding discovery is skipped, leaving that entry intact). ``pinned`` joins the
    condition because a runtime observation outranks a source parse (ADR 0010 §6) but
    not a human correction (ADR 0016 §5, #827). A discovery colliding with an existing
    UN-verified (source-inferred) entry UPGRADES it in place to verified, preserving the
    existing entry's other keys. Non-colliding discoveries are appended.

    Looks up the per-repo row first, falling back to the legacy project-level row
    (mirrors ``propose_selector_fix`` / ``merge_discovered_dom``). Opens its own session
    and never raises — KB enrichment is additive, not correctness-critical.

    Args:
        project_key: The project the discovery belongs to.
        repo: Target repository name ("" for the legacy project-level row).
        discovered: ``{"routes": [{"path", "description"?, "auth_required"?}],
            "selectors": [{"screen", "element", "selector", "strategy"?}]}`` — the
            routes/selectors an exploration session observed on the live app.
        owner_id: The knowledge row's owner (ADR 0009) — scopes the lookup.
        source: Provenance stamp for merged entries (default ``"exploration"``).

    Returns:
        The number of entries merged (appended) or upgraded (0 if nothing to do,
        no matching row, every discovery collided with a verified entry, or on error).
    """
    if not project_key:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    routes_in = [
        r
        for r in (discovered.get("routes") or [])
        if isinstance(r, dict) and (r.get("path") or "").strip()
    ]
    selectors_in = [
        s
        for s in (discovered.get("selectors") or [])
        if isinstance(s, dict) and (s.get("selector") or "").strip()
    ]
    if not routes_in and not selectors_in:
        return 0

    db = db_module.SessionLocal()
    try:
        row = None
        if repo:
            row = (
                db.query(ProjectKnowledge)
                .filter(
                    ProjectKnowledge.key == compose_key(project_key, repo),
                    ProjectKnowledge.owner_id == owner_id,
                )
                .first()
            )
        if row is None:
            row = (
                db.query(ProjectKnowledge)
                .filter(ProjectKnowledge.key == project_key, ProjectKnowledge.owner_id == owner_id)
                .first()
            )
        if row is None:
            return 0

        kn = dict(row.knowledge or {})
        merged = 0

        if routes_in:
            routes = list(kn.get("routes") or [])
            index_by_path: dict[str, int] = {}
            for i, r in enumerate(routes):
                if isinstance(r, dict) and r.get("path"):
                    index_by_path.setdefault(r["path"], i)
            for r in routes_in:
                path = r["path"].strip()
                entry = {**r, "path": path, "verified_at_runtime": now, "source": source}
                i = index_by_path.get(path)
                if i is None:
                    routes.append(entry)
                    index_by_path[path] = len(routes) - 1
                    merged += 1
                elif routes[i].get("verified_at_runtime") or routes[i].get("pinned"):
                    continue  # no-clobber: leave the verified/pinned entry intact
                else:
                    routes[i] = {**routes[i], **entry}  # upgrade in place, preserve other keys
                    merged += 1
            kn["routes"] = routes

        if selectors_in:
            sels = list(kn.get("selectors") or [])
            index_by_sel: dict[str, int] = {}
            for i, s in enumerate(sels):
                if isinstance(s, dict) and s.get("selector"):
                    index_by_sel.setdefault(s["selector"], i)
            for s in selectors_in:
                selector = s["selector"].strip()
                entry = {
                    **s,
                    "selector": selector,
                    "strategy": s.get("strategy") or "css",
                    "verified_at_runtime": now,
                    "source": source,
                }
                i = index_by_sel.get(selector)
                if i is None:
                    sels.append(entry)
                    index_by_sel[selector] = len(sels) - 1
                    merged += 1
                elif sels[i].get("verified_at_runtime") or sels[i].get("pinned"):
                    continue  # no-clobber: leave the verified/pinned entry intact
                else:
                    sels[i] = {**sels[i], **entry}  # upgrade in place, preserve other keys
                    merged += 1
            kn["selectors"] = sels

        if not merged:
            return 0

        row.knowledge = kn  # reassign so SQLAlchemy tracks the JSON change
        db.commit()
        write_knowledge_files(row)
        logger.info(
            "Exploration merged {} verified KB entr{} for {} (source={!r}, {} routes, {} selectors)",
            merged, "y" if merged == 1 else "ies", project_key, source,
            len(routes_in), len(selectors_in),
        )
        return merged
    except Exception as exc:  # noqa: BLE001 - KB enrichment is best-effort
        db.rollback()
        logger.warning("Verified-discovery KB merge failed for {}: {}", project_key, exc)
        return 0
    finally:
        db.close()
