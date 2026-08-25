"""Per-request EmeHub token plumbing (C1 of #497).

A hub read needs a hub token, and only the hub can mint one. Agent tokens live 15
minutes **and** are bound to a live hub session, so there is no useful way to
cache one server-side — a stored token is either expired or about to be, and
acting on a stale one produces failures that look like bugs.

So the browser mints a fresh token when it needs a hub read (it holds the hub
cookies, so it can call ``/auth/agent-token`` at any time the hub session lives)
and sends it on that one request in ``X-Hub-Token``. This module reads it and
hands it to :mod:`app.services.hub_client`.

Deliberately **not** stored in the session, the database, or a module global —
see the module docstring of ``hub_client`` for why. It is also never logged.

The token is **optional**: a request without one is normal (SSO off, no live hub
session, or a screen that simply doesn't need hub data). Callers fall back to
local data rather than failing, so this dependency returns ``None`` instead of
raising — the decision of what to do without a hub token belongs to the caller.
"""

from __future__ import annotations

from fastapi import Header, HTTPException

HUB_TOKEN_HEADER = "X-Hub-Token"


def hub_token(
    x_hub_token: str | None = Header(default=None, alias=HUB_TOKEN_HEADER),
) -> str | None:
    """The caller's freshly-minted EmeHub agent token, if it sent one.

    Returns ``None`` when absent or blank. Never raises: "no hub token" is an
    ordinary state, not an error.
    """
    if not x_hub_token:
        return None
    token = x_hub_token.strip()
    return token or None


def use_hub_credential(run_id: int, hub_token: str | None) -> None:
    """Resolve the run's Claude credential from the hub before an action uses it.

    Call this at the top of any **request-triggered** endpoint that goes on to do
    Claude work for an existing run — publishing comments, regenerating a spec,
    healing, auto-annotating. Q-Agent cannot configure a Claude credential of its own
    once it is connected to the hub (the hub is the only source, #607), so an action
    must resolve from the hub exactly like the run's start did rather than inheriting
    material pinned to disk hours earlier, which is routinely expired and which the
    background re-resolve can no longer refresh once the run's grant has died (#689).

    Refusals become **409 on the action**, deliberately: the run itself finished long
    ago, so failing it (as run start does) would rewrite history to describe a
    credential problem in a later, unrelated click.

    A no-op when the hub integration is off — behaviour is then byte-identical to
    before this existed.
    """
    from app.services import hub_credentials

    try:
        hub_credentials.ensure_run_credential(run_id, hub_token)
    except hub_credentials.HubCredentialRefusedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
