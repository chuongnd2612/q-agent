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
    """Hub API origin **as this container should reach it**.

    Prefers ``QAGENT_HUB_INTERNAL_BASE_URL`` and falls back to the public
    ``QAGENT_HUB_BASE_URL``. The two exist because the browser and this process
    are in different places: the SPA must be given a public origin, and sending
    the server there too means every read leaves the host for the Cloudflare edge
    and comes back to the same machine — ~498 ms measured, against ~2 ms over the
    Docker bridge.

    Either way the value carries the hub's ``/api`` prefix; behind the tunnel a
    bare ``/auth/agent-token`` answers 405 (#495, and the compose comments).
    """
    internal = settings.hub_internal_base_url.strip()
    return (internal or settings.hub_base_url).rstrip("/")


def enabled() -> bool:
    """True when hub data reads are configured and switched on.

    Requires **both** flags: identity has to be in play for a hub token to exist
    at all, so data-without-SSO is a misconfiguration rather than a mode.

    Gated on the **public** URL, not on :func:`_base_url`. The token every read
    spends is minted by the browser against the public origin, so an internal URL
    without a public one is a hub we can reach and can never be authorised to
    read — enabling on it would turn a misconfiguration into 401s at runtime.
    """
    return bool(
        settings.hub_sso_enabled
        and settings.hub_data_enabled
        and settings.hub_base_url.strip()
    )


def _require_enabled() -> None:
    if not enabled():
        raise HubDisabledError("Hub data integration is disabled")


def _request(method: str, path: str, hub_token: str, json_body: Any | None = None) -> Any:
    """Call the hub and return parsed JSON, mapping every failure to our taxonomy.

    Shared by :func:`get_json` and :func:`post_json` so a POST cannot drift into
    treating "the hub is down" as "the hub said no" — the distinction every caller
    branches on.

    ``hub_token`` is used for this call only and never persisted or logged.
    """
    _require_enabled()
    if not hub_token:
        # Treat a missing token as "not authorised to read the hub" rather than a
        # crash: callers reach this whenever the browser had no live hub session.
        raise HubUnauthorizedError("No hub token supplied")

    url = f"{_base_url()}{path}"
    try:
        with httpx.Client(timeout=_TIMEOUT_S, follow_redirects=False) as client:
            resp = client.request(
                method,
                url,
                json=json_body,
                headers={
                    "Authorization": f"Bearer {hub_token}",
                    "Accept": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        # Transport-level: refused, DNS, timeout, TLS. The hub is not answering.
        logger.warning("hub {} {} failed to reach the hub: {}", method, path, exc)
        raise HubUnavailableError(f"Could not reach EmeHub: {exc}") from exc

    if resp.status_code == 401:
        # Expired token, or the hub session behind it was revoked. Deliberately
        # not logged as an error — it is the ordinary end of a 15-minute token.
        logger.info("hub {} {} unauthorized (token expired or session gone)", method, path)
        raise HubUnauthorizedError("EmeHub rejected the agent token")

    if 502 <= resp.status_code <= 504:
        logger.warning("hub {} {} got gateway {}", method, path, resp.status_code)
        raise HubUnavailableError(f"EmeHub gateway error {resp.status_code}")

    if resp.status_code >= 400:
        detail = ""
        try:
            body = resp.json()
            if isinstance(body, dict):
                if isinstance(body.get("detail"), str):
                    detail = body["detail"]
                # A rejected clause query answers 422 {problems: [{message,
                # clauseIndex}]} — surface the first message rather than a bare
                # status, because the whole point of the hub refusing (instead of
                # dropping) a clause is that the user can be told which one.
                problems = body.get("problems")
                if not detail and isinstance(problems, list) and problems:
                    first = problems[0]
                    if isinstance(first, dict) and isinstance(first.get("message"), str):
                        idx = first.get("clauseIndex")
                        detail = (
                            f"condition {idx + 1}: {first['message']}"
                            if isinstance(idx, int)
                            else first["message"]
                        )
        except Exception:  # noqa: BLE001 - a non-JSON error body is not fatal
            pass
        logger.info("hub {} {} refused {} {}", method, path, resp.status_code, detail)
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


def get_json(path: str, hub_token: str) -> Any:
    """GET ``path`` from the hub. See :func:`_request` for the failure taxonomy."""
    return _request("GET", path, hub_token)


def post_json(path: str, hub_token: str, body: Any) -> Any:
    """POST ``body`` to ``path`` on the hub. See :func:`_request`."""
    return _request("POST", path, hub_token, json_body=body)


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


def iter_all_tickets(hub_token: str, page_size: int = 200) -> tuple[list[dict[str, Any]], bool]:
    """Every hub ticket, plus whether the walk actually covered the whole set.

    Returns ``(items, complete)``. Stops on a short/empty page as well as on the
    reported ``total``, so a hub whose ``total`` disagrees with what it serves
    terminates rather than looping.

    **``complete`` is load-bearing** and is why this returns a tuple rather than a
    bare list. Callers that reconcile deletions (#522) may only remove rows when
    the read was exhaustive: a page-capped walk looks exactly like a short one, so
    without this signal a truncated read is indistinguishable from "the hub has
    fewer tickets now" — and the difference is whether we delete the user's
    mirrored workspace.
    """
    collected: list[dict[str, Any]] = []
    for page in range(1, _MAX_TICKET_PAGES + 1):
        payload = list_tickets(hub_token, page=page, page_size=page_size)
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            # A malformed page is not an exhaustive read.
            return collected, False
        if not items:
            return collected, True
        collected.extend(items)
        total = payload.get("total")
        if not isinstance(total, int) or len(collected) >= total or len(items) < page_size:
            return collected, True
    logger.warning(
        "stopped mirroring hub tickets at the {}-page cap; not pruning, some may be missing",
        _MAX_TICKET_PAGES,
    )
    return collected, False


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


# ------------------------------------------------------- clause-query surface
# The hub's clause model (emehub#130, our #519):
#   {"clauses": [{"field", "operator", "values"}], "match": "all"|"any",
#    "sort": {"field", "direction"}}
#
# It is the same shape the Tickets query builder already produces (#517), which
# is why these take the query through untouched rather than re-encoding it.
#
# An unrunnable clause is REFUSED, not dropped — 422 {problems:[{message,
# clauseIndex}]}, surfaced by `_request` as a HubRefusedError naming the
# condition. That is deliberate on the hub's side and worth preserving here: a
# dropped filter returns MORE work items than were asked for, which is the
# failure a caller is least likely to notice.


def search_tickets(
    hub_token: str,
    query: dict[str, Any],
    *,
    page: int = 1,
    page_size: int = 50,
    provider_kind: str | None = None,
) -> dict[str, Any]:
    """Run a clause query against the hub's own mirror. Paged, reads only.

    Shape: ``{"items": [...], "total": n, "page": n, "pageSize": n}``.
    """
    body: dict[str, Any] = {"query": query, "page": page, "pageSize": page_size}
    if provider_kind:
        body["providerKind"] = provider_kind
    return post_json("/tickets/search", hub_token, body)


def preview_query(
    hub_token: str,
    query: dict[str, Any],
    *,
    provider_kind: str | None = None,
    connection_id: int | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    """What a query *would* pull from the provider. Writes nothing.

    Note this asks the **provider**, not the hub's mirror — measured against the
    live hub, a preview returned 51 items while the mirror held 0. So it answers
    "what is out there", which is what makes it worth showing before a sync.

    Shape: ``{"total": n, "sample": [...]}`` — the total is uncapped and honest.
    """
    body: dict[str, Any] = {"query": query}
    if provider_kind:
        body["providerKind"] = provider_kind
    if connection_id is not None:
        body["connectionId"] = connection_id
    if project:
        body["project"] = project
    return post_json("/tickets/query/preview", hub_token, body)


def sync_tickets(
    hub_token: str,
    *,
    query: dict[str, Any] | None = None,
    ticket_ids: list[str] | None = None,
    provider_kind: str | None = None,
    connection_id: int | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    """Ask the hub to pull work items from the provider into its store.

    **This writes.** The hub performs the provider call with its *own* PAT — which
    is why ticket sync is possible at all without a credential ever crossing the
    boundary (#503). It must be user-initiated and preview-confirmed, never fired
    implicitly on a page load.

    The body names either a clause ``query`` or explicit ``ticket_ids``; the old
    ``mode``/``sprint``/``states`` fields were removed and are now a 422 naming
    the field rather than a silent whole-project pull.
    """
    body: dict[str, Any] = {}
    if query is not None:
        body["query"] = query
    if ticket_ids:
        body["ticketIds"] = ticket_ids
    if provider_kind:
        body["providerKind"] = provider_kind
    if connection_id is not None:
        body["connectionId"] = connection_id
    if project:
        body["project"] = project
    return post_json("/tickets/sync", hub_token, body)


def list_saved_queries(hub_token: str) -> list[dict[str, Any]]:
    """The hub's saved ticket queries — built-in presets and the user's own.

    Each carries ``id``, ``name``, ``destination``, ``query``, ``description``,
    ``builtIn`` and ``shared``. Reading these means the two apps offer the *same*
    saved queries instead of Q-Agent keeping a private browser-local copy.
    """
    return get_json("/ticket-queries", hub_token)


def get_project_config(project_key: str, hub_token: str) -> dict[str, Any]:
    """A hub project's configuration — repos, environments, connection bindings.

    Agent-readable (measured): ``baseUrl``, ``environments``, ``repos``,
    ``testAccounts``, ``manualAuth``, ``workItemConnectionId``,
    ``repositoryConnectionId``, ``extra``.

    ``repos`` already arrives in our own shape — ``name``, ``repo_url``,
    ``default_branch``, ``local_repo_path``, ``default`` — so it needs no
    translation. The two connection ids are **the hub's**, and do need it:
    see :func:`app.services.hub_workspace.ensure_project_config`.
    """
    return get_json(f"/projects/{project_key}/config", hub_token)


def get_project_knowledge(project_key: str, hub_token: str) -> dict[str, Any] | None:
    """A hub project's project-level knowledge base, or ``None`` when it has none.

    ``None`` rather than an exception for **404**: "this project has no knowledge
    yet" is an ordinary answer, and the mirror must leave local state untouched
    when it arrives (#598). Every other failure still raises, so "the hub is
    down" stays distinguishable from "the hub has nothing" — the #491 rule.
    """
    return _knowledge_or_none(f"/projects/{project_key}/knowledge", hub_token)


def get_repo_knowledge(project_key: str, repo: str, hub_token: str) -> dict[str, Any] | None:
    """One repo's knowledge base on the hub, or ``None`` when there is none.

    The hub **falls back to the project-level row** when a repo has none of its
    own, so a payload here may carry ``repo: ""``.

    Two translations are the caller's job, not this module's: field names arrive
    camelCase (``lastIndexed``, ``needsRefresh``, ``docPath``, ``projectKey``)
    and ``provider`` speaks the hub's vocabulary (``azure_devops``, not ``ado``).
    See :func:`app.services.hub_workspace.ensure_knowledge`.
    """
    return _knowledge_or_none(f"/projects/{project_key}/repos/{repo}/knowledge", hub_token)


def _knowledge_or_none(path: str, hub_token: str) -> dict[str, Any] | None:
    try:
        payload = get_json(path, hub_token)
    except HubRefusedError as exc:
        if exc.status_code == 404:
            return None
        raise
    return payload if isinstance(payload, dict) else None
