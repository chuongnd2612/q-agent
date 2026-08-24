"""Project configuration + project-context resolution.

Two responsibilities:

1. **Persist** user-authored project config (base URL, test accounts, environments,
   local repo path, extra values), encrypting test-account passwords at rest and
   masking them on the way out — mirroring provider-credential handling.
2. **Resolve** the full project context for a ticket/test case so downstream AI
   actions (requirement analysis, test-case generation, Playwright generation) get
   a complete, consistent picture instead of missing context or placeholders.

A test case only knows its ``ticket_external_id`` and ``provider_kind``; tickets
are not reliably linked to a Project row, so we resolve the project **key** from
the ticket's **work-item connection** config (its configured project name, which
equals the Project Knowledge key, e.g. ADO ``config.project = "Surency Platform"``),
falling back to the sole configured project when only one exists.
"""

from __future__ import annotations

import re

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy.orm import Session

from app import crypto
from app.models.knowledge import ProjectKnowledge, compose_key
from app.models.project import Project
from app.models.project_config import ProjectConfig
from app.models.user import User
from app.models.provider_connection import ProviderConnection
from app.models.ticket import Ticket
from app.services import connection_service
from app.services.adapters.base import ProviderError
from app.services.workspace_scope import scoped_auth_dir, slug


# --------------------------------------------------------------- persistence
def get_config(db: Session, key: str) -> ProjectConfig | None:
    """Return the ProjectConfig row for a project key, or None."""
    return db.query(ProjectConfig).filter(ProjectConfig.key == key).first()


def upsert_config(db: Session, key: str, patch: dict[str, Any]) -> ProjectConfig:
    """Create or update a project's config from a partial patch (caller commits).

    Test-account passwords in ``patch['test_accounts']`` are plaintext on input and
    encrypted before persistence. A blank/absent password on an account preserves
    the previously stored (encrypted) password for the same role+username so the UI
    can save the masked form without wiping the secret.

    Args:
        db: Active session.
        key: Project key (project name).
        patch: Partial config — any of base_url, local_repo_path, environments,
            test_accounts, extra, name.

    Returns:
        The upserted ProjectConfig row (not yet committed).
    """
    row = get_config(db, key)
    if row is None:
        row = ProjectConfig(key=key, name=patch.get("name", key))
        db.add(row)
    _apply_config_patch(row, patch)
    return row


def get_config_for_owner(db: Session, key: str, owner_id: int | None) -> ProjectConfig | None:
    """Owner-scoped variant of :func:`get_config` (ADR 0009 clone/admin-shared, #120).

    ``get_config`` matches the first row for ``key`` regardless of owner (kept
    as-is for existing call sites, which additionally apply
    ``check_owned_or_404``). This filters by ``owner_id`` explicitly so callers
    that need exactly the shared row (``owner_id=None``) or a specific user's
    row never match a different owner's same-keyed row.
    """
    return db.query(ProjectConfig).filter(ProjectConfig.key == key, ProjectConfig.owner_id == owner_id).first()


_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def looks_like_guid(value: str) -> bool:
    """True when ``value`` has the shape of a project GUID.

    Shape only. A name *could* in principle be a UUID string, so this decides
    which lookup to try first, never whether the project exists.
    """
    return bool(value and _GUID_RE.match(value))


def resolve_project_identifier(db: Session, identifier: str, user: "User | None") -> str:
    """Translate a project identifier — GUID **or** name — into the project name.

    Named ``…_identifier``, not ``…_key``: this module already has a
    :func:`resolve_project_key` that maps a *work-item connection* to a project
    key (ADR 0006). Defining a second one silently shadowed it — Python keeps the
    last definition — and every call site bound to the wrong function.

    The G1 bridge for #585. Storage and routes still key off the name, so this is
    the single place that understands both, letting GUID URLs work before the 288
    call sites move. G4 deletes it, once nothing addresses a project by name.

    Owner-scoped: a GUID resolves only to a project the caller may see, so it
    cannot be used to discover another user's project name. An unknown or
    non-GUID identifier is returned unchanged, which keeps every existing
    name-based caller working exactly as before.
    """
    if not looks_like_guid(identifier):
        return identifier
    query = db.query(Project).filter(Project.guid == identifier)
    if user is not None:
        # Own rows or shared/legacy ones (owner_id NULL), matching
        # `_ownership_mismatch` — never someone else's.
        query = query.filter((Project.owner_id == user.id) | (Project.owner_id.is_(None)))
    project = query.first()
    return project.name if project is not None else identifier


def get_config_visible_to(db: Session, key: str, user: "User | None") -> ProjectConfig | None:
    """The config row for ``key`` that ``user`` may actually see (#583).

    Resolution order — the caller's **own** row, then the unowned/shared one, and
    **never** a row belonging to somebody else:

    1. ``owner_id == user.id`` — their own.
    2. ``owner_id IS NULL`` — shared/legacy, which :func:`ownership._ownership_mismatch`
       deliberately treats as everyone's.
    3. ``None`` — nothing yet, so callers fall through to a default config.

    :func:`get_config` matches the first row for ``key`` *regardless of owner*,
    which is fine where the caller then applies ``check_owned_or_404`` to reject
    it — but that combination means a second user with a same-named project sees a
    permanent 404 rather than their own row, and can never create one. Project
    configs are keyed by project **name**, so the collision is ordinary, not
    exotic: two people with a "Surency" project is the normal case.
    """
    if user is None:
        # BRIDGE (#91): no identity in play, so preserve the pre-ownership
        # behaviour of matching whatever row exists for the key.
        return get_config(db, key)
    own = db.query(ProjectConfig).filter(
        ProjectConfig.key == key, ProjectConfig.owner_id == user.id
    ).first()
    if own is not None:
        return own
    return db.query(ProjectConfig).filter(
        ProjectConfig.key == key, ProjectConfig.owner_id.is_(None)
    ).first()


def upsert_config_for_owner(
    db: Session, key: str, patch: dict[str, Any], owner_id: int | None
) -> ProjectConfig:
    """Owner-scoped variant of :func:`upsert_config` — see :func:`get_config_for_owner`.

    Used by the admin shared-namespace endpoints (``owner_id=None``) so writes
    always target the shared row, never a same-keyed row owned by someone else.
    """
    row = get_config_for_owner(db, key, owner_id)
    if row is None:
        row = ProjectConfig(key=key, name=patch.get("name", key), owner_id=owner_id)
        db.add(row)
    _apply_config_patch(row, patch)
    return row


def _apply_config_patch(row: ProjectConfig, patch: dict[str, Any]) -> None:
    """Apply a partial config patch onto ``row`` in place (shared by both upsert helpers)."""
    if "name" in patch and patch["name"]:
        row.name = patch["name"]
    # Per-project provider bindings (ADR 0006). Present-but-null clears the binding.
    if "work_item_connection_id" in patch:
        row.work_item_connection_id = patch["work_item_connection_id"]
    if "repository_connection_id" in patch:
        row.repository_connection_id = patch["repository_connection_id"]
    if "base_url" in patch and patch["base_url"] is not None:
        row.base_url = patch["base_url"].strip()
    if "local_repo_path" in patch and patch["local_repo_path"] is not None:
        row.local_repo_path = patch["local_repo_path"].strip()
    if "repo_url" in patch and patch["repo_url"] is not None:
        row.repo_url = patch["repo_url"].strip()
    if "repos" in patch and patch["repos"] is not None:
        row.repos = _normalize_repos(patch["repos"])
    if "environments" in patch and patch["environments"] is not None:
        row.environments = patch["environments"]
    if "extra" in patch and patch["extra"] is not None:
        row.extra = patch["extra"]
    if "test_accounts" in patch and patch["test_accounts"] is not None:
        row.test_accounts = _encrypt_accounts(patch["test_accounts"], row.test_accounts or [])
    if "manual_auth" in patch and patch["manual_auth"] is not None:
        row.manual_auth = bool(patch["manual_auth"])


def _normalize_repos(incoming: list[dict]) -> list[dict]:
    """Clean a submitted repo list; ensure at most one repo is flagged default."""
    out: list[dict] = []
    seen_default = False
    for r in incoming:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        is_default = bool(r.get("default")) and not seen_default
        seen_default = seen_default or is_default
        out.append(
            {
                "name": name,
                "repo_url": (r.get("repo_url") or "").strip(),
                "default_branch": (r.get("default_branch") or "").strip(),
                "local_repo_path": (r.get("local_repo_path") or "").strip(),
                "default": is_default,
            }
        )
    # If none was flagged default, the first repo becomes the default target.
    if out and not any(r["default"] for r in out):
        out[0]["default"] = True
    return out


def get_repos(config: ProjectConfig | None) -> list[dict]:
    """Return a project's repos, synthesizing one from legacy fields if needed."""
    if config and config.repos:
        return config.repos
    if config and (config.repo_url or config.local_repo_path):
        name = ""
        if config.repo_url:
            name = config.repo_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        return [
            {
                "name": name or "repo",
                "repo_url": config.repo_url,
                "default_branch": "",
                "local_repo_path": config.local_repo_path,
                "default": True,
            }
        ]
    return []


def default_repo(config: ProjectConfig | None) -> dict | None:
    """The repo automation targets by default (flagged default, else first)."""
    repos = get_repos(config)
    if not repos:
        return None
    return next((r for r in repos if r.get("default")), repos[0])


def _encrypt_accounts(incoming: list[dict], existing: list[dict]) -> list[dict]:
    """Encrypt plaintext passwords; keep the stored secret when input is blank."""
    prior: dict[tuple[str, str], str] = {
        (a.get("role", ""), a.get("username", "")): a.get("password", "")
        for a in existing
    }
    out: list[dict] = []
    for acct in incoming:
        role = acct.get("role", "")
        username = acct.get("username", "")
        password = acct.get("password", "")
        if not password:
            stored = prior.get((role, username), "")
            encrypted = stored  # already encrypted (or empty)
        else:
            encrypted = crypto.encrypt(password)
        out.append(
            {
                "role": role,
                "username": username,
                "password": encrypted,
                "notes": acct.get("notes", ""),
            }
        )
    return out


def public_config(row: ProjectConfig | None, key: str, name: str = "") -> dict[str, Any]:
    """Serialize a config row for the UI, masking passwords (never plaintext)."""
    if row is None:
        return {
            "key": key,
            "name": name or key,
            "workItemConnectionId": None,
            "repositoryConnectionId": None,
            "baseUrl": "",
            "repos": [],
            "localRepoPath": "",
            "repoUrl": "",
            "environments": [],
            "testAccounts": [],
            "extra": {},
            "manualAuth": False,
        }
    return {
        "key": row.key,
        "name": row.name or row.key,
        "workItemConnectionId": row.work_item_connection_id,
        "repositoryConnectionId": row.repository_connection_id,
        "baseUrl": row.base_url,
        "repos": get_repos(row),
        "localRepoPath": row.local_repo_path,
        "repoUrl": row.repo_url,
        "environments": row.environments or [],
        "testAccounts": [
            {
                "role": a.get("role", ""),
                "username": a.get("username", ""),
                "notes": a.get("notes", ""),
                "hasPassword": bool(a.get("password")),
            }
            for a in (row.test_accounts or [])
        ],
        "extra": row.extra or {},
        "manualAuth": bool(row.manual_auth),
    }


# --------------------------------------------------------------- manual auth
def auth_path(project_key: str, owner_id: int | None = None) -> Path:
    """Absolute path to a project's saved Playwright session file.

    Located at the owner's scoped auth dir (ADR 0009):
    ``scoped_auth_dir(owner_id) / <project-slug> / "storageState.json"``.
    ``owner_id`` defaults to ``None`` (the shared namespace). The file may not
    exist yet (nothing is created here).
    """
    return scoped_auth_dir(owner_id) / slug(project_key) / "storageState.json"


def session_path(project_key: str, owner_id: int | None = None) -> Path:
    """Absolute path to a project's saved sessionStorage snapshot.

    Sibling of the ``storageState.json`` at :func:`auth_path`. Captures the
    MSAL/SPA tokens that live in ``sessionStorage`` (which Playwright's
    ``storageState`` cannot persist). The file may not exist yet.
    """
    return auth_path(project_key, owner_id).parent / "sessionStorage.json"


def auth_state(project_key: str, owner_id: int | None = None) -> dict[str, Any]:
    """Report whether a saved session exists and, if so, when it was captured.

    Returns a dict shaped for :class:`app.schemas.AuthStateOut`::

        {"exists": bool, "capturedAt": datetime | None}

    ``capturedAt`` is derived from the session file's modification time (UTC).
    """
    path = auth_path(project_key, owner_id)
    if not path.exists():
        return {"exists": False, "capturedAt": None}
    captured_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return {"exists": True, "capturedAt": captured_at}


def agent_auth_state(row) -> dict[str, Any] | None:  # noqa: ANN001 - ProjectConfig | None
    """Agent-captured login marker from a config row's ``extra`` (set by the
    Local Agent capture-complete route), or None when the agent hasn't captured.

    The session itself lives on the operator's machine (the agent), never the
    server — this is only a "captured at" marker so the SPA can show it.
    """
    extra = (row.extra or {}) if row is not None else {}
    captured_at = extra.get("agentAuthCapturedAt")
    return {"exists": True, "capturedAt": captured_at} if captured_at else None


def clear_auth(project_key: str, owner_id: int | None = None) -> dict[str, Any]:
    """Delete a project's saved session file (if any) and return the empty state."""
    path = auth_path(project_key, owner_id)
    if path.exists():
        path.unlink()
    return auth_state(project_key, owner_id)


def base_url_for(db: Session, ticket: Ticket, env: str = "") -> str:
    """Resolve the base URL a run should target for a ticket's project.

    Reuses :func:`build_context` so per-environment URL selection stays consistent
    with the rest of the pipeline. Returns "" when no project/base URL resolves.
    """
    return build_context(db, ticket, env=env).get("baseUrl", "") or ""


# --------------------------------------------------------------- resolution
def resolve_project_key(db: Session, connection: ProviderConnection | None) -> str | None:
    """Best-effort map a **work-item connection** to its project key (ADR 0006).

    Order: (1) the connection's configured project name, matched against the known
    keys **case-insensitively**; (2) the sole configured project when exactly one
    exists; else None. Reads ``connection.config`` directly — no fragile
    ``config.project == key`` scan.

    Matching is by **id first** (#663): a project's config records the work-item
    connection it was configured with (``work_item_connection_id``), so the link is
    already there and needs no string comparison at all.

    Name matching cannot be trusted here, and this install shows why: two
    connections — ``surency`` and ``surency 2`` — both report
    ``config.project == "Surency"``, because they point at the SAME Azure DevOps
    project. No comparison of that string, however careful about case, can tell
    which Q-Agent project a ticket belongs to. Their ids can.

    It also explains the reported failure. The old code compared the provider's
    name (``"Surency"``) to the config key (``"surency"``) EXACTLY, so it never
    matched, and resolution silently rode on step (3) — "there is only one
    project". Configuring a second project took that count to two, the fallback
    returned None, and runs that had worked for months began failing with "No base
    URL in the project context": a message about the wrong thing entirely, and no
    visible connection to the change that caused it.

    The name steps remain as fallbacks, for a project that is only INDEXED
    (a ``ProjectKnowledge`` row with no config, hence no connection link).
    Ambiguity there is refused rather than guessed: when two keys differ only by
    case there is no way to tell which the provider meant, and picking one would
    be a coin flip that reads another project's base URL.
    """
    if connection is not None:
        # (1) The id link the user actually configured. Owner scoping comes for
        # free: a config row pointing at THIS connection belongs to whoever owns
        # the connection.
        linked = (
            db.query(ProjectConfig)
            .filter(ProjectConfig.work_item_connection_id == connection.id)
            .all()
        )
        if len(linked) == 1:
            return linked[0].key
        if len(linked) > 1:
            # Two projects claiming one connection is a data error, not something
            # to silently pick a winner from.
            logger.warning(
                "{} project configs share work-item connection {} ({}) — cannot resolve a project key",
                len(linked),
                connection.id,
                ", ".join(sorted(c.key for c in linked)),
            )
            return None

        cfg = connection.config or {}
        candidate = (cfg.get("project") or cfg.get("repo") or cfg.get("org") or "").strip()
        if candidate:
            known = _known_project_keys(db)
            if candidate in known:
                return candidate
            folded = candidate.casefold()
            matches = sorted(k for k in known if k.strip().casefold() == folded)
            if len(matches) == 1:
                return matches[0]

    keys = _known_project_keys(db)
    if len(keys) == 1:
        return next(iter(keys))
    return None


def _known_project_keys(db: Session) -> set[str]:
    """Every project key the install knows — configured or merely indexed."""
    keys = {c.key for c in db.query(ProjectConfig).all()}
    keys |= {k.project_key for k in db.query(ProjectKnowledge).all() if k.project_key}
    return keys


def project_key_for_ticket(db: Session, ticket: Ticket) -> str | None:
    """Resolve the project key a ticket belongs to via its work-item connection.

    Best-effort: an un-configured work-item connection degrades to the sole-project
    fallback in :func:`resolve_project_key` (never raises).
    """
    try:
        connection = connection_service.resolve_work_item_for_ticket(db, ticket)
    except ProviderError:
        connection = None
    return resolve_project_key(db, connection)


def repo_options(db: Session, project_key: str) -> list[dict[str, Any]]:
    """List a project's repos with their per-repo knowledge status.

    Each entry is ``{name, default, status}`` where ``status`` is the matching
    ``ProjectKnowledge`` row's status (keyed ``compose_key(project_key, name)``),
    defaulting to ``"not_indexed"`` when no knowledge base has been built.

    Args:
        db: Active session.
        project_key: The resolved project key.

    Returns:
        One entry per configured repo (empty list when the project has no repos).
    """
    cfg = get_config(db, project_key)
    out: list[dict[str, Any]] = []
    for repo in get_repos(cfg):
        name = repo.get("name", "")
        kn = (
            db.query(ProjectKnowledge)
            .filter(ProjectKnowledge.key == compose_key(project_key, name))
            .first()
        )
        out.append(
            {
                "name": name,
                "default": bool(repo.get("default")),
                "status": kn.status if kn else "not_indexed",
            }
        )
    return out


def build_context(
    db: Session, ticket: Ticket, env: str = "", repo: str | None = None
) -> dict[str, Any]:
    """Assemble the full project context downstream AI actions consume.

    Combines the user-authored ProjectConfig (base URL, test accounts,
    environments, extra) with the discovered ProjectKnowledge (domain, routes,
    selectors, auth, locator strategy). Test-account passwords ARE decrypted here
    because the caller injects them into generated automation; this dict must never
    be logged or returned over the API.

    Args:
        db: Active session.
        ticket: The ticket whose work-item connection resolves the project key.
        env: Optional environment name to pick a matching per-env base URL.
        repo: Optional target repository NAME. When it is a non-empty configured
            repo name, that repo's per-repo knowledge base is loaded (instead of
            the project's default repo). Empty/None keeps the default-repo
            behavior.

    Returns:
        A context dict (empty-ish if no project resolves). Keys: projectKey,
        repo, repoOptions, baseUrl, testAccounts (with decrypted passwords),
        environments, extra, plus flattened knowledge fields (domain,
        architecture, locator, routes, selectors, auth, businessEntities, stack,
        pageObjects/fixtures counts).
    """
    key = project_key_for_ticket(db, ticket)
    context: dict[str, Any] = {"projectKey": key or ""}
    if not key:
        return context

    cfg = get_config(db, key)
    context["repoOptions"] = repo_options(db, key)

    # Pick the target repo: an explicit, configured repo name wins; otherwise the
    # project's default repo. ``target_name`` drives which per-repo KB we load.
    configured_names = {opt["name"] for opt in context["repoOptions"]}
    default = default_repo(cfg)
    default_name = default.get("name", "") if default else ""
    target_name = repo if (repo and repo in configured_names) else default_name
    context["repo"] = target_name
    if cfg:
        base_url = cfg.base_url
        # Prefer a per-environment URL when the run's env matches one.
        if env:
            for e in cfg.environments or []:
                if str(e.get("name", "")).lower() == env.lower() and e.get("base_url"):
                    base_url = e["base_url"]
                    break
        context["baseUrl"] = base_url
        context["localRepoPath"] = cfg.local_repo_path
        context["environments"] = cfg.environments or []
        context["extra"] = cfg.extra or {}
        context["testAccounts"] = [
            {
                "role": a.get("role", ""),
                "username": a.get("username", ""),
                "password": crypto.decrypt(a.get("password", "")) or "",
                "notes": a.get("notes", ""),
            }
            for a in (cfg.test_accounts or [])
        ]

    # Per-repo knowledge: prefer the target repo's KB, falling back to a
    # project-level (legacy) row keyed by the bare project key.
    kn_row = None
    if target_name:
        kn_row = (
            db.query(ProjectKnowledge)
            .filter(ProjectKnowledge.key == compose_key(key, target_name))
            .first()
        )
    if kn_row is None:
        kn_row = db.query(ProjectKnowledge).filter(ProjectKnowledge.key == key).first()
    if kn_row:
        kn = kn_row.knowledge or {}
        # Fall back to the KB's base_url when the ProjectConfig didn't supply one.
        # NOTE: must be an emptiness check, not setdefault — the cfg block above
        # always sets context["baseUrl"] (to "" when the top-level field is blank),
        # so setdefault would never fire and the KB value would be masked.
        if not context.get("baseUrl"):
            context["baseUrl"] = kn.get("base_url", "")
        context["domain"] = kn.get("domain", "")
        context["architecture"] = kn.get("architecture", "")
        context["locator"] = kn.get("locator", "")
        context["routes"] = kn.get("routes", [])
        context["selectors"] = kn.get("selectors", [])
        context["auth"] = kn.get("auth", {})
        context["businessEntities"] = kn.get("business_entities", [])
        context["stack"] = kn.get("stack", [])
        # Names of reusable assets (distinct from the integer counts the UI shows).
        context["pageObjectNames"] = kn.get("page_object_names", [])
        context["fixtureNames"] = kn.get("fixture_names", [])
        context["utilities"] = kn.get("utilities", [])
    return context


def context_for_ticket(
    db: Session, ticket: Ticket, env: str = "", repo: str | None = None
) -> dict[str, Any]:
    """Convenience: build the project context for a specific ticket.

    ``repo`` (a target repository name) is threaded through so the context loads
    that repo's per-repo knowledge base rather than the project default repo's.
    """
    return build_context(db, ticket, env=env, repo=repo)
