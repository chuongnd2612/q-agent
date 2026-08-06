"""Read hub-owned data from EmeHub (C1 of #497, docs/HUB-INTEGRATION.md).

The **only** place that calls the hub's data endpoints. Distinct from
:mod:`app.services.hub_tokens`, which merely *verifies* tokens the hub minted for
us — this module spends one, outbound.

What the hub actually serves an agent-audience token (measured against a live
hub, not taken from the handoff):

===================================  ======  =================================
endpoint                             status  notes
===================================  ======  =================================
``GET /tickets``                     200     the hub's ticket store
``GET /tickets/{external_id}``       200
``GET /projects``                    200
``GET /connections``                 200     ``hasPat`` only — never the PAT
``GET /credentials/claude/resolve``  200     real credential material
``GET /credentials/claude``          401     hub audience only
``GET /audit/events``                401     hub audience only
``GET /agents``                      401     hub audience only
===================================  ======  =================================

Three properties this module holds by construction:

1. **We never store a hub token.** It is a parameter of one call. Agent tokens
   live 15 minutes *and* are bound to a live hub session (a signature-valid token
   with an unknown ``sid`` is refused ``401 "Session revoked or expired"``), so a
   cached one is worthless at best and misleading at worst. Callers obtain a fresh
   token per request — see :mod:`app.deps_hub`.
2. **Failure kinds stay distinguishable.** "The hub says no" and "the hub is not
   answering" lead to different behaviour in every caller, so they are different
   exceptions here. Collapsing them is how a broken service comes to look like an
   empty result (the mistake fixed in #491).
3. **Nothing falls open.** Every entry point refuses unless both flags are on.
   Disabled means *no hub data*, never *allow anything*.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import settings
from app.logging import logger

# Bound every call. The hub is a local-network/tunnel hop serving small JSON
# payloads, so a request that hasn't answered in 15s is not going to.
_TIMEOUT_S = 15.0

# Refuse to parse absurd responses rather than loading them into memory. The
# largest legitimate payload here is a ticket page or a credential blob.
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class HubClientError(Exception):
    """Base for every hub-read failure."""


class HubDisabledError(HubClientError):
    """The integration is off, or the hub URL isn't configured.

    Raised *before* any network call, so a disabled deployment makes no outbound
    requests at all.
    """


class HubUnauthorizedError(HubClientError):
    """The hub refused the token — expired, or its session is gone (401).

    Callers should fall back to local data. It says nothing about whether the
    user is authenticated *here*: our own session is independent of the hub's
    (that is the whole point of the bootstrap in #480).
    """


class HubRefusedError(HubClientError):
    """The hub answered, on its own behalf, and declined (403/404/4xx).

    An authoritative answer — do not retry, and do not paper over it with local
    data if the question was "does this exist".
    """

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class HubUnavailableError(HubClientError):
    """The hub could not be reached, or a gateway answered for it (5xx/timeout).

    Never conflate with :class:`HubRefusedError`: "down" is not "no". Behind
    nginx + a Cloudflare tunnel this arrives as 502/503/504 rather than a
    connection error, so both map here (the same distinction as #490).
    """


def _base_url() -> str:
    """Hub API origin, including whatever path prefix it is served under.

    Behind the tunnel the hub's API lives under ``/api``, so
    ``QAGENT_HUB_BASE_URL`` is expected to carry it (a bare ``/auth/agent-token``
    on that host answers 405 — see #495 and the compose comments).
    """
    return settings.hub_base_url.rstrip("/")


def enabled() -> bool:
    """True when hub data reads are configured and switched on.

    Requires **both** flags: identity has to be in play for a hub token to exist
    at all, so data-without-SSO is a misconfiguration rather than a mode.
    """
    return bool(settings.hub_sso_enabled and settings.hub_data_enabled and _base_url())


def _require_enabled() -> None:
    if not enabled():
        raise HubDisabledError("Hub data integration is disabled")


def get_json(path: str, hub_token: str) -> Any:
    """GET ``path`` from the hub with ``hub_token`` and return parsed JSON.

    ``hub_token`` is used for this call only and never persisted or logged.

    Raises :class:`HubDisabledError`, :class:`HubUnauthorizedError`,
    :class:`HubRefusedError` or :class:`HubUnavailableError` — see each for what
    the caller is expected to do.
    """
    _require_enabled()
    if not hub_token:
        # Treat a missing token as "not authorised to read the hub" rather than a
        # crash: callers reach this whenever the browser had no live hub session.
        raise HubUnauthorizedError("No hub token supplied")

    url = f"{_base_url()}{path}"
    try:
        with httpx.Client(timeout=_TIMEOUT_S, follow_redirects=False) as client:
            resp = client.get(
                url,
                headers={
                    "Authorization": f"Bearer {hub_token}",
                    "Accept": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        # Transport-level: refused, DNS, timeout, TLS. The hub is not answering.
        logger.warning("hub read {} failed to reach the hub: {}", path, exc)
        raise HubUnavailableError(f"Could not reach EmeHub: {exc}") from exc

    if resp.status_code == 401:
        # Expired token, or the hub session behind it was revoked. Deliberately
        # not logged as an error — it is the ordinary end of a 15-minute token.
        logger.info("hub read {} unauthorized (token expired or session gone)", path)
        raise HubUnauthorizedError("EmeHub rejected the agent token")

    if 502 <= resp.status_code <= 504:
        logger.warning("hub read {} got gateway {}", path, resp.status_code)
        raise HubUnavailableError(f"EmeHub gateway error {resp.status_code}")

    if resp.status_code >= 400:
        detail = ""
        try:
            body = resp.json()
            if isinstance(body, dict) and isinstance(body.get("detail"), str):
                detail = body["detail"]
        except Exception:  # noqa: BLE001 - a non-JSON error body is not fatal
            pass
        logger.info("hub read {} refused {} {}", path, resp.status_code, detail)
        raise HubRefusedError(resp.status_code, detail or f"EmeHub returned {resp.status_code}")

    if len(resp.content) > _MAX_RESPONSE_BYTES:
        raise HubUnavailableError("EmeHub response was implausibly large; refusing to parse")

    try:
        return resp.json()
    except ValueError as exc:
        # A 200 that isn't JSON means we're talking to something that isn't the
        # hub API — a tunnel error page, or a wrong base URL (a missing /api
        # prefix serves the SPA's index.html with status 200).
        raise HubUnavailableError("EmeHub returned a non-JSON response") from exc


# ---------------------------------------------------------------- typed reads
def list_tickets(hub_token: str, page: int = 1, page_size: int = 200) -> dict[str, Any]:
    """One page of the hub's ticket store. Shape ``{"items": [...], "total": n}``.

    The hub paginates with ``page``/``pageSize`` and defaults to **25**, so a
    caller that ignores them silently mirrors only the first 25 of however many
    tickets exist — which is exactly what happened before this was parameterised.
    Callers wanting everything should use :func:`iter_all_tickets`.
    """
    return get_json(f"/tickets?page={page}&pageSize={page_size}", hub_token)


# Cap the number of pages walked, so a hub that keeps reporting a `total` we never
# reach cannot spin forever. 40 x 200 is far beyond any realistic backlog.
_MAX_TICKET_PAGES = 40


def iter_all_tickets(hub_token: str, page_size: int = 200) -> list[dict[str, Any]]:
    """Every hub ticket, walking pages until the reported ``total`` is covered.

    Stops on a short/empty page as well as on the count, so a hub whose ``total``
    disagrees with what it actually serves terminates rather than looping.
    """
    collected: list[dict[str, Any]] = []
    for page in range(1, _MAX_TICKET_PAGES + 1):
        payload = list_tickets(hub_token, page=page, page_size=page_size)
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not items:
            break
        collected.extend(items)
        total = payload.get("total")
        if not isinstance(total, int) or len(collected) >= total or len(items) < page_size:
            break
    else:
        logger.warning(
            "stopped mirroring hub tickets at the {}-page cap; some may be missing",
            _MAX_TICKET_PAGES,
        )
    return collected


def get_ticket(external_id: str, hub_token: str) -> dict[str, Any]:
    return get_json(f"/tickets/{external_id}", hub_token)


def list_connections(hub_token: str) -> list[dict[str, Any]]:
    """Provider connections the hub holds — **informational only**.

    Carries ``hasPat`` and never the PAT itself, so these cannot be used to make
    provider calls; Q-Agent keeps its own ``provider_connections`` for that. See
    #501 and docs/HUB-INTEGRATION.md §4c.
    """
    return get_json("/connections", hub_token)


def list_projects(hub_token: str) -> list[dict[str, Any]]:
    return get_json("/projects", hub_token)


def resolve_claude_credential(hub_token: str) -> dict[str, Any]:
    """Claude credential material, already resolved own → shared → none.

    Returns ``source``, ``status``, ``expiresAt``, ``daysLeft``, ``scopes``,
    ``subscriptionType`` and ``credentials``.

    **``status`` has four values, and ``refreshable`` is the common one.** A
    Claude OAuth *access* token expires within hours, so a live credential is past
    ``expiresAt`` almost immediately and the hub reports ``refreshable`` rather
    than ``expired`` when a refresh token exists. Code that special-cases
    ``"expired"`` will misread a perfectly usable credential (§4b).

    The returned material is a secret: never log it, never return it to the SPA.
    """
    return get_json("/credentials/claude/resolve", hub_token)
