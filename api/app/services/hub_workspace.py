"""Mirror EmeHub-owned resources into the caller's own workspace (#514).

A user who signs in through EmeHub for the first time gets a freshly provisioned
local account, and everything in Q-Agent is per-user (``owner_id``, ADR 0009) — so
they landed in an empty workspace while the hub held their connection, projects
and every ticket.

**Why mirror rather than read through.** Every downstream feature — run creation,
review, evidence, publish — addresses local rows by primary key. A read-through
list can be *browsed* but not *selected into a run*, which is the only thing a
user actually wants to do with a ticket here. The read-through added in #500
overlays hub values onto local rows and is complementary: it keeps an existing row
fresh, while this creates the row in the first place.

**What a mirrored connection is.** A real ``ProviderConnection`` with
``hub_connection_id`` set and ``secrets`` empty — empty permanently, because the
hub never releases the PAT (``GET /connections`` returns ``hasPat`` only). It
exists so per-connection scoping, the connection picker and the ticket filters
keep working unchanged. It must never be used for a direct provider call; provider
work for these belongs to the hub, which holds the credential.

Everything here is **idempotent** and **additive**: re-running updates in place,
never duplicates, and never deletes or overwrites a row the user owns locally. A
user who already has their own connections is unaffected.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging import logger
from app.models.knowledge import KNOWLEDGE_STATUSES, ProjectKnowledge, compose_key
from app.models.project import Project
from app.models.provider_connection import ProviderConnection
from app.models.run import RunTicket
from app.models.ticket import Ticket
from app.models.user import User
from app.services import hub_client, knowledge_service, project_config_service

# The hub spells Azure DevOps `azure_devops`; we spell it `ado` (#507). Shared
# with the ticket read-through's join key so both sides agree.
_KIND_ALIASES = {"azure_devops": "ado", "azure-devops": "ado", "azuredevops": "ado"}


def _local_kind(hub_kind: Any) -> str:
    kind = str(hub_kind or "").strip().lower()
    return _KIND_ALIASES.get(kind, kind)


def _str(value: Any, default: str = "") -> str:
    return str(value).strip() if value not in (None, "") else default


def ensure_connections(db: Session, user: User, hub_token: str) -> dict[str, ProviderConnection]:
    """Mirror the hub's connections for ``user``; return them keyed by hub id.

    Idempotent: an existing mirror is refreshed in place. Connections the user
    created locally are matched by nothing and left completely alone — we only
    ever touch rows carrying the matching ``hub_connection_id``.
    """
    rows = hub_client.list_connections(hub_token)
    if not isinstance(rows, list):
        return {}

    mirrored: dict[str, ProviderConnection] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        hub_id = _str(row.get("id"))
        kind = _local_kind(row.get("kind"))
        if not hub_id or not kind:
            continue

        existing = db.scalar(
            select(ProviderConnection).where(
                ProviderConnection.hub_connection_id == hub_id,
                ProviderConnection.owner_id == user.id,
            )
        )
        conn = existing or ProviderConnection(
            owner_id=user.id,
            hub_connection_id=hub_id,
            # Empty, and staying that way: the PAT never crosses (#501).
            secrets={},
        )
        conn.kind = kind
        conn.name = _str(row.get("label")) or _str(row.get("baseUrl")) or f"EmeHub {kind}"
        # `connected` reflects the hub's own view. It does NOT mean Q-Agent can
        # call the provider — it cannot, and must not try.
        conn.connected = bool(row.get("connected"))
        config = row.get("config")
        conn.config = dict(config) if isinstance(config, dict) else {}
        if row.get("baseUrl"):
            conn.config.setdefault("baseUrl", row["baseUrl"])
        if existing is None:
            db.add(conn)
        mirrored[hub_id] = conn

    db.commit()
    for conn in mirrored.values():
        db.refresh(conn)
    return mirrored


def ensure_tickets(
    db: Session, user: User, hub_token: str, connections: dict[str, ProviderConnection]
) -> int:
    """Mirror the hub's tickets for ``user``. Returns how many rows were created.

    Matched on ``hub_ticket_id`` — the hub's own id — rather than
    ``(kind, external_id)``, because this owns the row's existence and needs an
    unambiguous identity. Existing rows are refreshed in place.

    Only list-level fields are written. ``description`` and ``acceptanceCriteria``
    are deliberately left to the detail fetch: pulling them for every ticket would
    mean one hub round trip per ticket (200 here) to populate fields the list never
    shows.
    """
    # Every page, not just the first: the hub defaults to 25 per page, so a
    # single call mirrored 25 of 200 tickets and the workspace looked
    # arbitrarily truncated. `complete` says whether the walk was exhaustive —
    # only then may we prune (#522).
    items, complete = hub_client.iter_all_tickets(hub_token)
    if not items and not complete:
        return 0

    existing_rows = db.scalars(
        select(Ticket).where(Ticket.owner_id == user.id, Ticket.hub_ticket_id.is_not(None))
    ).all()
    by_hub_id = {row.hub_ticket_id: row for row in existing_rows}

    created = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        hub_id = _str(item.get("id"))
        external_id = _str(item.get("externalId"))
        if not hub_id or not external_id:
            continue

        row = by_hub_id.get(hub_id)
        if row is None:
            row = Ticket(owner_id=user.id, hub_ticket_id=hub_id)
            db.add(row)
            created += 1

        row.external_id = external_id
        row.provider_kind = _local_kind(item.get("providerKind"))
        row.title = _str(item.get("title"))[:500]
        row.work_item_type = _str(item.get("workItemType"), "User Story")[:32]
        row.status = _str(item.get("status"), "Ready for QA")[:32]
        row.priority = _str(item.get("priority"), "Medium")[:16]
        row.assignee = _str(item.get("assignee"))[:120]
        row.sprint = _str(item.get("sprint"))[:120]
        row.area_path = _str(item.get("areaPath"))[:300]
        row.epic = _str(item.get("epic"))[:300]
        labels = item.get("labels")
        row.labels = [str(x) for x in labels] if isinstance(labels, list) else []

        conn = connections.get(_str(item.get("connectionId")))
        if conn is not None:
            row.connection_id = conn.id

    if complete:
        _prune_vanished(db, user, seen_hub_ids={_str(i.get("id")) for i in items if isinstance(i, dict)})

    db.commit()
    return created


def _prune_vanished(db: Session, user: User, seen_hub_ids: set[str]) -> int:
    """Delete the caller's mirrored tickets the hub no longer has. Returns the count.

    Only ever called after an **exhaustive** hub read: a partial or failed read
    would otherwise look like "the hub is empty now" and wipe the workspace. That
    is why :func:`hub_client.iter_all_tickets` reports completeness.

    Two rows are deliberately spared:

    * anything without ``hub_ticket_id`` — a ticket the user created or synced
      locally was never ours to remove;
    * anything a run references. ``runs.run_tickets.ticket_external_id`` is a
      plain string with **no foreign key**, so deleting would not fail loudly — it
      would silently orphan run history, leaving a run pointing at a ticket that
      no longer exists.
    """
    mirrored = db.scalars(
        select(Ticket).where(Ticket.owner_id == user.id, Ticket.hub_ticket_id.is_not(None))
    ).all()
    vanished = [row for row in mirrored if row.hub_ticket_id not in seen_hub_ids]
    if not vanished:
        return 0

    referenced = {
        ext
        for (ext,) in db.execute(
            select(RunTicket.ticket_external_id).where(
                RunTicket.ticket_external_id.in_([row.external_id for row in vanished])
            )
        ).all()
    }

    removed = 0
    kept = 0
    for row in vanished:
        if row.external_id in referenced:
            kept += 1
            continue
        db.delete(row)
        removed += 1

    if removed or kept:
        logger.info(
            "pruned {} mirrored tickets the hub no longer has for user {} ({} kept, referenced by a run)",
            removed, user.id, kept,
        )
    return removed


def ensure_projects(
    db: Session, user: User, hub_token: str, connections: dict[str, ProviderConnection]
) -> int:
    """Mirror the hub's projects for ``user``. Returns how many rows were created.

    Matched on ``(provider_kind, external_id)`` scoped to the owner, so a project
    the user already had locally is refreshed rather than duplicated.

    Mirrored projects are created **inactive**: activating a project is a
    deliberate user choice here, and silently activating several because the hub
    happens to know about them would change what runs target.
    """
    rows = hub_client.list_projects(hub_token)
    if not isinstance(rows, list):
        return 0

    created = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = _str(row.get("key"))
        name = _str(row.get("name")) or key
        if not key:
            continue

        conn = next(iter(connections.values()), None)
        kind = conn.kind if conn is not None else ""

        existing = db.scalar(
            select(Project).where(
                Project.external_id == key,
                Project.owner_id == user.id,
            )
        )
        project = existing or Project(
            owner_id=user.id, external_id=key, provider_kind=kind, active=False
        )
        project.name = name[:200]
        # The hub's NUMERIC id, kept so the read-only settings tab can deep-link
        # to where the configuration is actually editable (#587). The hub's own
        # project screen routes by id, not by key. Only overwritten when the hub
        # supplies one, so a re-mirror that omits it doesn't blank the link.
        hub_id = _str(row.get("id"))
        if hub_id:
            project.hub_project_id = hub_id[:64]
        if conn is not None:
            project.connection_id = conn.id
            if not project.provider_kind:
                project.provider_kind = conn.kind
        summary = row.get("summary")
        if isinstance(summary, dict):
            project.meta = {**(project.meta or {}), "hub": summary}
        if existing is None:
            db.add(project)
            created += 1

    db.commit()
    return created


def ensure_for_user(db: Session, user: User | None, hub_token: str | None) -> dict[str, int]:
    """Mirror the hub's connections and tickets for ``user``. Never raises.

    Called from read paths, so a hub outage must degrade to "whatever is already
    local" rather than breaking the screen — the #491 rule. Returns a small
    summary for logging; callers ignore it.
    """
    if user is None or not hub_token or not hub_client.enabled():
        return {"connections": 0, "tickets": 0, "projects": 0}

    try:
        connections = ensure_connections(db, user, hub_token)
        created = ensure_tickets(db, user, hub_token, connections)
        projects = ensure_projects(db, user, hub_token, connections)
    except hub_client.HubClientError as exc:
        # Routine: 15-minute tokens expire, the hub is a remote hop.
        logger.info("hub workspace mirror skipped ({}), serving local data", exc)
        db.rollback()
        return {"connections": 0, "tickets": 0, "projects": 0}
    except Exception as exc:  # noqa: BLE001 - a mirror must never break a read
        logger.warning("hub workspace mirror failed unexpectedly: {}", exc)
        db.rollback()
        return {"connections": 0, "tickets": 0, "projects": 0}

    if created or projects:
        logger.info(
            "mirrored {} hub tickets and {} projects for user {}", created, projects, user.id
        )
    return {"connections": len(connections), "tickets": created, "projects": projects}


def fill_ticket_detail(db: Session, ticket: Ticket, hub_token: str | None) -> Ticket:
    """Populate a mirrored ticket's description/AC/comments from the hub, once.

    Deferred to here rather than done during the list mirror: the list shows none
    of these, and fetching them eagerly would cost one hub round trip per ticket.

    Never raises, and only fills what is empty — a user's local edits to a
    mirrored ticket are never clobbered.
    """
    if not ticket.hub_ticket_id or not hub_token or not hub_client.enabled():
        return ticket
    if ticket.description and ticket.acceptance_criteria:
        return ticket

    try:
        detail = hub_client.get_ticket(ticket.external_id, hub_token)
    except hub_client.HubClientError as exc:
        logger.info("hub ticket detail unavailable for {}: {}", ticket.external_id, exc)
        return ticket
    if not isinstance(detail, dict):
        return ticket

    if not ticket.description:
        ticket.description = _str(detail.get("description"))
    if not ticket.acceptance_criteria:
        criteria = detail.get("acceptanceCriteria")
        if isinstance(criteria, list):
            ticket.acceptance_criteria = [str(c) for c in criteria]
        elif criteria:
            ticket.acceptance_criteria = [str(criteria)]
    if not ticket.acceptance_criteria_html:
        ticket.acceptance_criteria_html = _str(detail.get("acceptanceCriteriaHtml"))
    if not ticket.comments:
        comments = detail.get("comments")
        ticket.comments = comments if isinstance(comments, list) else []

    db.commit()
    db.refresh(ticket)
    return ticket


def _local_connection_id(db: Session, user: User, hub_connection_id: Any) -> int | None:
    """Map a **hub** connection id onto the caller's mirrored local connection.

    The hub's config names its own connection ids. Copying one straight into
    ``work_item_connection_id`` would point at whatever local row happens to hold
    that primary key — a different provider, or another user's — so it is
    translated through ``hub_connection_id``, the mapping the connection mirror
    already records. Unmapped means ``None``: no binding is better than a wrong
    one, and this is the same class of mistake as the `azure_devops`/`ado` join
    that silently matched nothing (#507).
    """
    hub_id = _str(hub_connection_id)
    if not hub_id:
        return None
    conn = db.scalar(
        select(ProviderConnection).where(
            ProviderConnection.hub_connection_id == hub_id,
            ProviderConnection.owner_id == user.id,
        )
    )
    return conn.id if conn is not None else None


def ensure_project_config(db: Session, user: User | None, key: str, hub_token: str | None) -> bool:
    """Mirror a hub project's configuration into the caller's own config row.

    Q-Agent showed an empty Settings tab for a hub project — no repos, no
    environments — because the project mirror created a bare ``projects`` row and
    never copied the configuration that hangs off it. The hub had it all along
    (``GET /projects/{key}/config`` is agent-readable); we simply never asked.

    Called from the config/repos reads rather than from the bulk project mirror,
    because the bulk path runs on every ticket-list load and this would make it
    one hub round trip **per project** on a screen that shows none of it.

    Returns True when a mirror happened. Never raises: a hub outage leaves
    whatever is already local, so Settings still renders (#491).

    Only mirrors onto projects that came from the hub — a purely local project's
    config is never overwritten by hub data.
    """
    if user is None or not hub_client.enabled():
        return False
    if not hub_token:
        # A missing token here is a WIRING bug, not an outage: the caller reached a
        # read that mirrors hub config without attaching `X-Hub-Token`, so we ask
        # the hub for nothing and the screen shows the bare mirrored row. That is
        # #592, and it was invisible precisely because this function is silent by
        # design (so a hub outage cannot break Settings). Say something.
        logger.info(
            "hub project config not mirrored for {}: no X-Hub-Token on the request "
            "(the caller must attach one for a hub-owned project)",
            key,
        )
        return False

    project = _hub_project(db, user, key)
    if project is None:
        return False  # not a hub-sourced project — leave local config alone

    try:
        cfg = hub_client.get_project_config(project.external_id or key, hub_token)
    except hub_client.HubClientError as exc:
        logger.info("hub project config unavailable for {}: {}", key, exc)
        return False
    if not isinstance(cfg, dict):
        return False

    repos = cfg.get("repos")
    patch: dict[str, Any] = {
        "name": _str(cfg.get("name")) or key,
        "base_url": _str(cfg.get("baseUrl")),
        # Already our shape (name/repo_url/default_branch/local_repo_path/default).
        "repos": repos if isinstance(repos, list) else [],
        "environments": cfg.get("environments") if isinstance(cfg.get("environments"), list) else [],
        "extra": cfg.get("extra") if isinstance(cfg.get("extra"), dict) else {},
        "manual_auth": bool(cfg.get("manualAuth")),
        "work_item_connection_id": _local_connection_id(db, user, cfg.get("workItemConnectionId")),
        "repository_connection_id": _local_connection_id(db, user, cfg.get("repositoryConnectionId")),
        "test_case_connection_id": _local_connection_id(db, user, cfg.get("testCaseConnectionId")),
    }

    # Test accounts only when the hub actually has some. Passing an empty list
    # would delete locally-held accounts — and their passwords, which the hub
    # never sends — so an absent list means "nothing to say", not "none".
    accounts = cfg.get("testAccounts")
    if isinstance(accounts, list) and accounts:
        patch["test_accounts"] = accounts

    row = project_config_service.upsert_config_for_owner(db, key, patch, user.id)
    row.project_guid = project.guid
    db.commit()
    logger.info("mirrored hub project config for {} ({} repos)", key, len(patch["repos"]))
    return True


def _hub_project(db: Session, user: User, key: str) -> Project | None:
    """The caller's own **hub-sourced** project for ``key``, or None.

    ``key`` may be the project's name (what knowledge/config rows are keyed by) or
    its provider external id. A project without ``hub_project_id`` never came from
    the hub, so nothing hanging off it is ours to overwrite.
    """
    project = db.scalar(
        select(Project).where(Project.name == key, Project.owner_id == user.id)
    ) or db.scalar(
        select(Project).where(Project.external_id == key, Project.owner_id == user.id)
    )
    if project is None or not project.hub_project_id:
        return None
    return project


# ------------------------------------------------------------------ knowledge
# Q-Agent had **no** hub knowledge endpoint and no knowledge mirror at all
# (#598): the hub showed a project as `Knowledge: Indexed` with every section
# populated while Q-Agent showed the same project as having none. Config was
# mirrored (so the repos appeared) and knowledge was not, which is the recurring
# EmeHub failure shape — the call succeeds, nothing errors, and the data is
# quietly incomplete.
#
# Mirror, don't read through. Generation reads the KB off the **local** row
# (`project_config_service.build_context` -> `prompts.render_project_context`), so
# a read-through would make the UI look right while generation still ran blind.


def _parse_hub_dt(value: Any) -> datetime | None:
    """Parse the hub's ISO timestamp into an aware UTC datetime, or None.

    The hub serialises ``lastIndexed`` from its own UTC column, usually with a
    ``+00:00`` offset but not always: a naive value is read as UTC rather than
    dropped, because dropping it would make every hub row look "older than local"
    and the mirror would silently never fire.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = _str(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo:
        return value
    return value.replace(tzinfo=timezone.utc)


def _pick(payload: dict[str, Any], camel: str, snake: str) -> Any:
    """Read a hub field by either casing.

    The hub serialises camelCase (``lastIndexed``) but accepts and, in places,
    echoes snake_case — so read both rather than betting on one and mirroring a
    row full of defaults.
    """
    return payload.get(camel, payload.get(snake))


def _hub_supersedes_local(
    payload: dict[str, Any], existing: ProjectKnowledge, label: str
) -> bool:
    """Whether the hub's row may overwrite ``existing``. Never destroys a newer build.

    Two rules, both learned the hard way and both asserted with a negative control
    in ``tests/test_hub_knowledge.py``:

    * a hub row that is **not indexed** never overwrites a local indexed row — a
      project whose hub build has not run yet must not wipe the KB this machine
      built;
    * otherwise the hub wins only when its ``lastIndexed`` is **strictly newer**
      than the local row's. Equal timestamps mean the same build, so nothing to
      do; a hub row with no timestamp at all can never win against a local one.
    """
    hub_status = _str(_pick(payload, "status", "status"), "not_indexed")
    if hub_status != "indexed" and existing.status == "indexed":
        logger.info(
            "kept the local indexed knowledge for {}: the hub's row is '{}'", label, hub_status
        )
        return False

    hub_at = _parse_hub_dt(_pick(payload, "lastIndexed", "last_indexed"))
    local_at = _aware(existing.last_indexed)
    if hub_at is None:
        return local_at is None and existing.status not in ("indexed", "indexing")
    if local_at is None:
        return True
    if hub_at <= local_at:
        logger.info(
            "kept the newer local knowledge for {} (local {} >= hub {})", label, local_at, hub_at
        )
        return False
    return True


def _mirror_knowledge(
    db: Session, user: User | None, key: str, repo: str, hub_token: str | None
) -> tuple[bool, bool]:
    """Mirror one knowledge row. Returns ``(payload_seen, row_written)``.

    Split from :func:`ensure_knowledge` so the multi-repo caller can tell "the hub
    had nothing" from "the hub answered and we wrote nothing" — the one line that
    separates an empty hub from a mirror that failed to understand it (#598).
    """
    if user is None or not hub_client.enabled():
        return (False, False)
    if not hub_token:
        # A wiring bug, not an outage — the caller reached a hub-mirroring read
        # without attaching `X-Hub-Token` (the #592 shape). Say so; this function
        # is otherwise silent by design so a hub outage cannot break the screen.
        logger.info(
            "hub knowledge not mirrored for {}: no X-Hub-Token on the request", compose_key(key, repo)
        )
        return (False, False)

    project = _hub_project(db, user, key)
    if project is None:
        return (False, False)

    label = compose_key(key, repo)
    hub_key = project.external_id or key
    try:
        payload = (
            hub_client.get_repo_knowledge(hub_key, repo, hub_token)
            if repo
            else hub_client.get_project_knowledge(hub_key, hub_token)
        )
    except hub_client.HubClientError as exc:
        logger.info("hub knowledge unavailable for {}: {}", label, exc)
        return (False, False)
    if payload is None:
        # 404 — the hub has no knowledge for this repo yet. Not an error, and the
        # local row (if any) is left exactly as it was.
        return (False, False)

    blob = _pick(payload, "knowledge", "knowledge")
    blob = dict(blob) if isinstance(blob, dict) else {}
    status = _str(_pick(payload, "status", "status"), "not_indexed")
    if status not in KNOWLEDGE_STATUSES:
        status = "not_indexed"

    existing = db.scalar(
        select(ProjectKnowledge).where(
            ProjectKnowledge.key == label, ProjectKnowledge.owner_id == user.id
        )
    )
    if existing is not None and not _hub_supersedes_local(payload, existing, label):
        return (True, False)
    if existing is None and status != "indexed" and not blob:
        logger.info("hub has no knowledge to mirror for {} (status '{}')", label, status)
        return (True, False)

    row = existing or ProjectKnowledge(key=label, owner_id=user.id)
    row.project_key = key
    row.project_guid = project.guid
    row.name = _str(_pick(payload, "name", "name")) or key
    # The hub says `azure_devops`; we say `ado`. #507 joined on the untranslated
    # value and matched zero rows while every test passed.
    row.provider = _local_kind(_pick(payload, "provider", "provider")) or (
        project.provider_kind or ""
    )
    # The requested repo, NOT the payload's: the hub falls back to its
    # project-level row (`repo: ""`) when a repo has none of its own, and this row
    # is the per-repo slot every downstream lookup addresses.
    row.repo = repo
    row.framework = _str(_pick(payload, "framework", "framework"), "Playwright")
    row.status = status
    confidence = _pick(payload, "confidence", "confidence")
    row.confidence = int(confidence) if isinstance(confidence, (int, float)) else 0
    row.version = _str(_pick(payload, "version", "version"), "v1")
    row.needs_refresh = bool(_pick(payload, "needsRefresh", "needs_refresh"))
    row.last_indexed = _parse_hub_dt(_pick(payload, "lastIndexed", "last_indexed"))
    row.knowledge = blob
    row.last_error = _str(_pick(payload, "lastError", "last_error"))[:1000]
    if existing is None:
        db.add(row)
    db.commit()
    db.refresh(row)

    # `docPath` is deliberately NOT copied. The hub documents it as the
    # *agent-host* directory, "opaque to the hub … which the hub stores and never
    # resolves" — a mirrored value points at a path that may not exist here. We
    # re-render knowledge.md/.json from the blob into THIS owner's own scoped
    # knowledge dir instead, and leave doc_path empty if that cannot be done.
    row.doc_path = ""
    try:
        config = project_config_service.get_config_for_owner(db, key, user.id)
        row.doc_path = knowledge_service.write_knowledge_files(row, config)
    except Exception as exc:  # noqa: BLE001 - artifacts are a convenience, not the mirror
        logger.warning("could not re-render local knowledge artifacts for {}: {}", label, exc)
    db.commit()

    if status == "indexed" and not blob:
        logger.warning(
            "hub reports {} indexed but sent an empty knowledge blob — mirrored a row with "
            "nothing in it, so generation will still run blind",
            label,
        )
    logger.info(
        "mirrored hub knowledge for {} (status {}, {} sections, confidence {})",
        label, row.status, len(blob), row.confidence,
    )
    return (True, True)


def ensure_knowledge(
    db: Session, user: User | None, key: str, repo: str = "", hub_token: str | None = None
) -> bool:
    """Mirror a hub project's knowledge base into the caller's own row.

    Idempotent and additive: re-running updates the one row in place, and a local
    build that is newer (or indexed while the hub's is not) is never destroyed —
    see :func:`_hub_supersedes_local`.

    ``repo`` empty means the project-level row (``compose_key`` yields the bare
    key). Returns True when a row was written. Never raises: a hub outage leaves
    whatever is already local, so the screen still renders (#491).
    """
    try:
        _, written = _mirror_knowledge(db, user, key, repo, hub_token)
    except Exception as exc:  # noqa: BLE001 - a mirror must never break a read
        logger.warning("hub knowledge mirror failed unexpectedly for {}: {}", key, exc)
        db.rollback()
        return False
    return written


def ensure_knowledge_for_repos(
    db: Session, user: User | None, key: str, repos: list[str], hub_token: str | None = None
) -> int:
    """Mirror every configured repo's knowledge (the repos listing). Returns rows written.

    Warns when the hub answered for at least one repo and **nothing** was written:
    that single line is what separates "the hub had nothing" from "we failed to
    understand what it sent", which are indistinguishable from the UI.
    """
    seen = 0
    written = 0
    for repo in repos:
        try:
            payload_seen, row_written = _mirror_knowledge(db, user, key, repo, hub_token)
        except Exception as exc:  # noqa: BLE001 - a mirror must never break a read
            logger.warning("hub knowledge mirror failed unexpectedly for {}: {}", key, exc)
            db.rollback()
            continue
        seen += int(payload_seen)
        written += int(row_written)
    if seen and not written:
        logger.warning(
            "the hub answered with knowledge for {} of {}'s repos but no local row was written "
            "— the project will still show as not indexed",
            seen, key,
        )
    return written


# ------------------------------------------- knowledge STATUS for the grid (#603)
# The Projects grid badge reads `GET /projects/knowledge`, a purely local list, so
# a hub-indexed project read "not indexed" until its detail page had been opened
# once (#603) — the #598 complaint, moved one screen over.
#
# It is fixed **without a single extra hub call**. Mirroring the blob here would
# be `ensure_knowledge_for_repos` per project, i.e. projects x repos hub round
# trips to paint a list, on a token that lives 15 minutes: 10 projects x 3 repos
# is 30 hops. But the grid needs a *status*, not the content — and
# `ensure_projects` already stores `GET /projects`'s `summary` under
# `Project.meta["hub"]`, and that summary carries `knowledgeStatus` /
# `knowledgeConfidence` (EmeHub's `ProjectSummaryOut`). So the badge is served
# from a payload this request already fetched. The blob mirror stays where the
# content is actually used: the detail screen and the Repos tab.


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def hub_knowledge_status_rows(
    db: Session, user: User | None, local: list[ProjectKnowledge]
) -> list[dict[str, Any]]:
    """Status-only knowledge rows for hub projects with no local row yet (#603).

    **Makes no hub calls.** Reads the already-mirrored ``Project.meta["hub"]``
    summary; see the note above for why the grid must not fan out.

    Transient by design: nothing is persisted, and a project that already has any
    local knowledge row is skipped entirely, so a local build (including one still
    ``indexing``) is never shadowed by the hub's view of it. Only rows the caller
    owns and that carry ``hub_project_id`` are considered.

    Warns once when a mirrored hub summary exists but carries no readable
    ``knowledgeStatus``: the call succeeding while the data is quietly empty is
    this integration's signature failure, and from the UI it is indistinguishable
    from "the hub has nothing indexed".
    """
    if user is None or not hub_client.enabled():
        return []

    have = {row.project_key or row.key for row in local}
    rows: list[dict[str, Any]] = []
    unreadable = 0
    for project in db.scalars(select(Project).where(Project.owner_id == user.id)).all():
        if not project.hub_project_id:
            continue
        summary = (project.meta or {}).get("hub")
        if not isinstance(summary, dict) or not summary:
            continue
        raw = _pick(summary, "knowledgeStatus", "knowledge_status")
        status = _str(raw)
        if status not in KNOWLEDGE_STATUSES:
            # A summary we could not read at all is the tell-tale; a hub project
            # that genuinely has nothing indexed is not.
            unreadable += 1
            continue
        if status == "not_indexed" or project.name in have:
            continue
        rows.append(
            {
                "key": compose_key(project.name),
                "project_key": project.name,
                "name": project.name,
                # `azure_devops` -> `ado` (#507): the hub's spelling would match
                # nothing on our side of the join.
                "provider": _local_kind(_pick(summary, "provider", "provider"))
                or project.provider_kind
                or "",
                "repo": "",
                "status": status,
                "confidence": _int(_pick(summary, "knowledgeConfidence", "knowledge_confidence")),
                # Marks the row as a hub summary rather than a mirrored KB, so the
                # UI can label it "Indexed" instead of inventing a repo count it
                # does not have.
                "source": "hub",
            }
        )
    if unreadable and not rows:
        logger.warning(
            "{} mirrored hub project summaries carried no readable knowledgeStatus "
            "— the Projects grid will show them as not indexed",
            unreadable,
        )
    return rows
