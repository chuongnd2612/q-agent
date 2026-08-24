"""One answer to "what does this user still need before a run can work?" (#642).

The blockers were scattered — devices, project config, credentials, connections —
so every screen either re-derived them or, more often, said nothing at all and
let the user discover the gap as a failure. #641 made a failed generation pass
explain itself *after* the fact; this is the same information *before* the click.

Two rules shape the output:

**Relevance is settings-dependent, and a missing-but-irrelevant thing is not a
failure.** With ``executionTarget=server`` an unpaired Local Agent blocks nothing,
so reporting it as unmet would train users to ignore the checklist — the fastest
way to make a warning useless. Each item therefore carries ``required``, computed
from the settings actually in force.

**Point at whoever owns the setting.** Under ``QAGENT_HUB_DATA_ENABLED`` EmeHub
owns projects and connections and Q-Agent's own screens for them are hidden, so
telling the user to "open Settings" would send them somewhere that cannot fix it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models import User
from app.models.agent_device import AgentDevice
from app.models.project_config import ProjectConfig
from app.models.provider_connection import ProviderConnection
from app.services import claude_credentials, project_config_service, settings_store

# Where a given item is fixed. The SPA maps these to a route/deep link; keeping
# them as stable keys (not URLs) means the frontend owns its own routing.
FIX_SETTINGS = "settings"
FIX_PROJECT = "project"
FIX_HUB = "hub"
FIX_INSTALL_AGENT = "install-agent"


def _item(
    key: str,
    *,
    ready: bool,
    required: bool,
    fix: str,
    detail: str = "",
    managed: bool = False,
) -> dict[str, Any]:
    """One checklist row.

    ``managed`` marks a setting whose authority is somewhere Q-Agent cannot see
    (today: EmeHub). Such an item is never a blocker — asserting "missing" about
    something we did not check is how #651 happened, and a false alarm on the
    first row of the list is the fastest way to teach users to ignore the rest.
    """
    return {
        "key": key,
        "ready": ready,
        "required": required,
        "fix": fix,
        "detail": detail,
        "managed": managed,
    }


def _owned_configs(db: Session, user: User | None) -> list[ProjectConfig]:
    """Project config rows this user can see — own rows, plus shared (NULL owner).

    Mirrors ``project_config_service.get_config_visible_to``'s rule rather than
    filtering strictly by owner: a shared row IS usable by this user, so treating
    it as absent would report a blocker they cannot act on and do not have.
    """
    owner_id = user.id if user else None
    rows = db.query(ProjectConfig).all()
    return [r for r in rows if r.owner_id == owner_id or r.owner_id is None]


def check(db: Session, user: User | None) -> dict[str, Any]:
    """Build the setup checklist for ``user`` under the settings in force.

    Returns ``{"ready": bool, "items": [...], "hubManaged": bool}`` where ``ready``
    means every **required** item is met — the question the UI actually asks
    before enabling an action.
    """
    owner_id = user.id if user else None
    stored = settings_store.load_settings()
    exec_target = stored.get("executionTarget", "server")
    authoring_mode = stored.get("authoringMode", "blind")
    heal_mode = stored.get("healMode", "classic")
    hub_managed = bool(settings.hub_data_enabled)

    # An agent is needed to RUN on the paired device, and to author or heal live —
    # browser-harness runs where the agent runs.
    needs_agent = (
        exec_target == "local-agent"
        or authoring_mode == "live-harness"
        or heal_mode == "live-harness"
    )
    # Live authoring/healing drives a real URL, so the project must have one.
    needs_base_url = authoring_mode == "live-harness" or heal_mode == "live-harness"

    has_device = (
        db.query(AgentDevice)
        .filter(AgentDevice.owner_id == owner_id, AgentDevice.revoked_at.is_(None))
        .first()
        is not None
    )
    has_credential = claude_credentials.resolve_effective_config_dir(db, owner_id) is not None

    connections = [
        c
        for c in db.query(ProviderConnection).all()
        if c.owner_id == owner_id or c.owner_id is None
    ]
    # A hub-mirrored connection carries no secret by design (#514), so "connected"
    # is never set on it — its existence is the readiness signal instead.
    has_connection = any(
        c.connected or (hub_managed and getattr(c, "hub_connection_id", None))
        for c in connections
    )

    configs = _owned_configs(db, user)
    with_base_url = [c for c in configs if (c.base_url or "").strip()]
    manual_auth = [c for c in configs if getattr(c, "manual_auth", False)]
    captured = [
        c
        for c in manual_auth
        if project_config_service.auth_state(c.key, c.owner_id)["exists"]
        or project_config_service.agent_auth_state(c) is not None
    ]

    items = [
        # Under hub management the local store is NOT where the credential lives:
        # EmeHub is authoritative (#610) and Q-Agent materialises it per run, at
        # run start, from a browser-minted hub token
        # (`hub_credentials.prepare_run_credential`) — because hub agent tokens are
        # 15-minute and session-bound (#497 §4b). A local miss therefore says
        # nothing, and #651 was exactly that: this row claimed "no credential
        # resolves" while the same screen showed live plan usage from the one that
        # did. The server cannot verify the hub's answer without a hub token, so it
        # reports who owns the setting instead of guessing at its state. A hub with
        # no credential still fails at run start, where #641 now names the reason.
        _item(
            "claudeCredential",
            ready=has_credential,
            required=not hub_managed,
            managed=hub_managed,
            fix=FIX_HUB if hub_managed else FIX_SETTINGS,
            detail=""
            if (has_credential or hub_managed)
            else "No Claude credential resolves for this account.",
        ),
        _item(
            "providerConnection",
            ready=has_connection,
            required=True,
            fix=FIX_HUB if hub_managed else FIX_SETTINGS,
            detail="" if has_connection else "No connected ticket provider.",
        ),
        _item(
            "localAgent",
            ready=has_device,
            required=needs_agent,
            fix=FIX_INSTALL_AGENT,
            detail=""
            if has_device
            else _agent_detail(exec_target, authoring_mode, heal_mode, needs_agent),
        ),
        _item(
            "projectBaseUrl",
            ready=bool(with_base_url),
            required=needs_base_url,
            fix=FIX_HUB if hub_managed else FIX_PROJECT,
            detail=""
            if with_base_url
            else "No project has a base URL, so live authoring has nothing to drive.",
        ),
        _item(
            "capturedLogin",
            # Vacuously ready when no project asks for a manual login: there is
            # nothing to capture, and reporting "not ready" would be a permanent
            # unmeetable item.
            ready=not manual_auth or bool(captured),
            required=needs_base_url and bool(manual_auth),
            fix=FIX_PROJECT,
            detail=""
            if (not manual_auth or captured)
            else "A project needs a manual login, but no session has been captured.",
        ),
    ]
    return {
        "ready": all(i["ready"] for i in items if i["required"]),
        "hubManaged": hub_managed,
        "items": items,
    }


def _agent_detail(exec_target: str, authoring_mode: str, heal_mode: str, needed: bool) -> str:
    """Name the setting that makes the agent necessary, not just "it's needed".

    Knowing *why* is what makes the item actionable: the user can either pair a
    device or change that one setting, and the message should make both obvious.
    """
    if not needed:
        return ""
    reasons = []
    if exec_target == "local-agent":
        reasons.append("runs execute on your machine")
    if authoring_mode == "live-harness":
        reasons.append("live authoring")
    if heal_mode == "live-harness":
        reasons.append("live self-heal")
    return "No paired Local Agent — required by: " + ", ".join(reasons) + "."
