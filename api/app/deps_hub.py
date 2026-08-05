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

from fastapi import Header

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
