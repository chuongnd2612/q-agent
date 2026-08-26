"""AI activity + Claude credentials management (#95)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps_auth import current_user, require_admin
from app.models.user import User
from app.models.claude_credentials import STATUS_ACTIVE, STATUS_EXPIRED
from app.schemas import (
    ClaudeCredentialModeUpdate,
    ClaudeCredentialsStatusOut,
    ClaudeCredentialsTestOut,
    ClaudeCredentialsUpload,
    OkResponse,
)
from app.deps_hub import hub_token
from app.services import activity, ai_usage_service, claude_cli, claude_usage_reader
from app.services import hub_client
from app.services.claude_credentials import ClaudeCredentialsError, delete_own, delete_shared
from app.services.claude_credentials import persist_refreshed, resolve_scoped_config_dir
from app.services.claude_credentials import set_preferred_mode, set_scoped_status
from app.services.claude_credentials import status_for as credentials_status_for
from app.services.claude_credentials import upsert_own, upsert_shared

router = APIRouter(tags=["ai"])


@router.get("/ai/models")
def ai_models() -> list[dict[str, str]]:
    """The Claude models this deployment offers, for the model dropdowns (#715).

    Served rather than hardcoded in the SPA so there is ONE catalog. The SPA used to
    carry its own copy, and a third table meant a third way to be wrong: it offered a
    date-suffixed Haiku id and no Opus 5, while the backend priced Sonnet 5 at $3/$15
    instead of $2/$10. A list that disagrees with what the server bills for is worse
    than no list.
    """
    from app.services import model_catalog

    return model_catalog.options()


@router.get("/ai/activity")
def ai_activity() -> dict:
    """Currently-running + recent Claude CLI calls (see also WS /ws/ai)."""
    return activity.snapshot()


@router.get("/ai/stats")
def ai_stats(refresh: bool = False, user: User | None = Depends(current_user)) -> dict:
    """Real Claude usage read from the local Claude Code session logs (like /usage).

    ``refresh=true`` (manual reload) bypasses the in-process caches and kicks off
    a fresh CLI `/usage` read for the plan-limit %. The machine-wide reading is
    unchanged; ``own`` (#95) additively reports the signed-in user's own
    DB-recorded cost/tokens (scoped via ``owned()``), independent of which
    machine/config-dir the CLI actually ran under.
    """
    base = claude_usage_reader.read_stats(force=refresh)
    own = ai_usage_service.stats(user)
    return {
        **base,
        "own": {
            "costMonth": own["costMonth"],
            "weekTokens": own["weekTokens"],
            "weekBudget": own["weekBudget"],
        },
    }


# ------------------------------------------------------------- credentials
@router.get("/ai/credentials", response_model=ClaudeCredentialsStatusOut)
def get_credentials_status(
    db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> ClaudeCredentialsStatusOut:
    """Whether own/shared Claude credentials are configured, and the effective mode.

    Never returns the token itself — see :func:`app.services.claude_credentials.status_for`.
    """
    owner_id = user.id if user is not None else None
    return ClaudeCredentialsStatusOut.model_validate(credentials_status_for(db, owner_id))


@router.get("/ai/credentials/hub")
def get_hub_credential(hub: str | None = Depends(hub_token)) -> dict:
    """The Claude credential EmeHub would resolve for the caller (#512).

    Settings used to show only local state while runs resolved from the hub, so
    the screen asserted an "Active" credential that no run would use. This is what
    the card needs to tell the truth.

    **Returns a sanitised subset — never the credential material.** The hub's
    ``/credentials/claude/resolve`` payload carries the real token; the whitelist
    below is applied by construction rather than by deleting keys, so a new field
    appearing upstream cannot leak by default.

    Never raises. Settings must render whether or not the hub answers, so every
    failure — flag off, no hub token, expired token, hub down — collapses to
    ``{"available": false}``. That is not an error state: the local credential is
    a genuine fallback and the card still describes it accurately.
    """
    if not hub_client.enabled() or not hub:
        return {"available": False}
    try:
        resolved = hub_client.resolve_claude_credential(hub)
    except hub_client.HubClientError:
        # Expected routinely: 15-minute tokens expire, the hub is a remote hop.
        return {"available": False}
    if not isinstance(resolved, dict):
        return {"available": False}

    return {
        "available": True,
        "source": resolved.get("source"),
        "status": resolved.get("status"),
        "label": resolved.get("label"),
        "expiresAt": resolved.get("expiresAt"),
        "daysLeft": resolved.get("daysLeft"),
        "scopes": resolved.get("scopes") or [],
        "subscriptionType": resolved.get("subscriptionType"),
    }


@router.post("/ai/credentials/test", response_model=ClaudeCredentialsTestOut)
def test_credentials(
    scope: str = "effective",
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> ClaudeCredentialsTestOut:
    """Run a real minimal Claude call under a credential and report whether it
    authenticates.

    ``scope`` selects which credential to test: ``"effective"`` (default — the
    caller's own→shared precedence, used by the header + personal card),
    ``"shared"`` (the workspace shared account, used by the admin Claude-
    credentials page even when the admin has their own on file), or ``"own"``.

    The only authoritative check (OAuth tokens can be expired-but-refreshable),
    and it feeds the outcome back into the stored status so the passive
    header/AI-stats indicator stays accurate afterwards.
    """
    if scope not in ("effective", "shared", "own"):
        scope = "effective"
    owner_id = user.id if user is not None else None
    config_dir = resolve_scoped_config_dir(db, owner_id, scope)
    if config_dir is None:
        return ClaudeCredentialsTestOut(
            ok=False, result="no_credential", message="No Claude credential is configured."
        )
    result, message = claude_cli.verify_credentials(config_dir)
    if result == "ok":
        set_scoped_status(db, owner_id, scope, STATUS_ACTIVE)
        # A successful call may have refreshed the token on disk — capture it.
        persist_refreshed(db, owner_id)
    elif result == "invalid":
        set_scoped_status(db, owner_id, scope, STATUS_EXPIRED)
    return ClaudeCredentialsTestOut(ok=result == "ok", result=result, message=message)


@router.put("/ai/credentials", response_model=OkResponse)
def upload_own_credentials(
    body: ClaudeCredentialsUpload,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> OkResponse:
    """Upload/replace the signed-in user's own Claude CLI credentials.

    ``body.credentials`` must be the raw contents of a Claude CLI
    ``.credentials.json`` file. Requires an authenticated user (own credentials
    have no meaning without one) — errors 401 when auth is required and no user
    is resolved, matching the rest of the per-user (#91) surfaces.
    """
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        upsert_own(db, user.id, body.credentials, body.label or "")
    except ClaudeCredentialsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return OkResponse()


@router.put("/ai/credentials/mode", response_model=OkResponse)
def set_credential_mode(
    body: ClaudeCredentialModeUpdate,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> OkResponse:
    """Switch the signed-in user between their own and the shared credential.

    Unlike delete-own, this is non-destructive: the user's uploaded credential
    stays on file (as ``prefer_shared`` on their own row) so they can flip back
    without re-uploading. Requires an own credential on file; ``mode="shared"``
    also requires a shared account to fall back to (both 400 otherwise).
    """
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        set_preferred_mode(db, user.id, body.mode)
    except ClaudeCredentialsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return OkResponse()


@router.delete("/ai/credentials", response_model=OkResponse)
def delete_own_credentials(
    db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> OkResponse:
    """Delete the signed-in user's own credential (falls back to shared, if any)."""
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    delete_own(db, user.id)
    return OkResponse()


@router.put("/ai/credentials/shared", response_model=OkResponse)
def upload_shared_credentials(
    body: ClaudeCredentialsUpload,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> OkResponse:
    """Admin-only: upload/replace the shared/fallback Claude CLI credentials."""
    try:
        upsert_shared(db, body.credentials, body.label or "")
    except ClaudeCredentialsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return OkResponse()


@router.delete("/ai/credentials/shared", response_model=OkResponse)
def delete_shared_credentials(
    db: Session = Depends(get_db), _admin: User = Depends(require_admin)
) -> OkResponse:
    """Admin-only: delete the shared/fallback Claude CLI credentials."""
    delete_shared(db)
    return OkResponse()
