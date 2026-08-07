"""Auth FastAPI dependencies + cookie helpers (ADR 0007).

- :func:`require_user` — decode the bearer access token, load the active user.
  401s when no/invalid token — for routes that must be authenticated. Dual-accept
  (#479): a local Q-Agent access token first, then an EmeHub agent token when
  ``hub_sso_enabled`` is on.
- :func:`require_role` — factory that additionally enforces a role.
- :func:`require_admin` — admin-only shortcut (401 unauthenticated, 403 non-admin).
- :func:`current_user` — best-effort variant used by ownership scoping (#91):
  never raises, so routers not yet migrated to per-user filtering stay usable.
- Cookie helpers set/clear the refresh (HttpOnly) + csrf (readable) cookies. The
  ``Secure`` flag is gated on ``settings.cookie_secure`` so http-localhost dev
  works while production behind HTTPS stays secure.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models.user import ROLE_ADMIN, User
from app.services import agent_device_service, auth_service, hub_users

REFRESH_COOKIE = "qagent_refresh"
CSRF_COOKIE = "qagent_csrf"
CSRF_HEADER = "X-CSRF-Token"

_bearer = HTTPBearer(auto_error=False)


def _unauthorized(detail: str = "Not authenticated") -> HTTPException:
    return HTTPException(status_code=401, detail=detail)


def hub_authed(user: User) -> bool:
    """True when this request authenticated with an **EmeHub** token, not a local one.

    Session management (``/auth/logout``, ``/auth/sessions/*``) revokes rows in the
    local ``auth_sessions`` table keyed by ``user._sid``. A hub token's ``sid`` is
    a *hub* session id with no counterpart there, so the hub path deliberately
    never populates ``_sid`` and those routes consult this flag before touching
    sessions (#479 trap 5).
    """
    return bool(getattr(user, "_hub_authed", False))


def _hub_user(db: Session, token: str) -> User | None:
    """Resolve ``token`` as a hub token, marking the user as hub-authenticated.

    Returns ``None`` when it isn't an acceptable hub token or the account is
    inactive. ``_sid`` is pinned to ``None`` rather than to the hub ``sid``: see
    :func:`hub_authed`.
    """
    user = hub_users.resolve_user(db, token)
    if user is None or not user.is_active:
        return None
    user._hub_authed = True  # type: ignore[attr-defined]
    user._sid = None  # type: ignore[attr-defined]
    return user


def require_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    """Resolve the current active user from the Authorization: Bearer token.

    Dual-accept (#479): a local Q-Agent access token first, then — when
    ``hub_sso_enabled`` is on and the token carries ``iss: emehub`` — an EmeHub
    agent token, which resolves the local user via ``users.hub_user_id`` and
    JIT-provisions one on first sight.
    """
    if credentials is None or not credentials.credentials:
        raise _unauthorized()
    try:
        payload = auth_service.decode_access_token(credentials.credentials)
    except auth_service.AuthError as exc:
        hub_user = _hub_user(db, credentials.credentials)
        if hub_user is None:
            raise _unauthorized(str(exc)) from exc
        return hub_user
    user = db.get(User, int(payload.get("sub", 0) or 0))
    if user is None or not user.is_active:
        raise _unauthorized("User not found or inactive")
    # Stash the sid so handlers (logout, sessions) can reach the current session.
    user._sid = payload.get("sid")  # type: ignore[attr-defined]
    return user


def require_agent(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    """Resolve the owning User from a Local Agent device's Bearer token.

    Distinct from :func:`require_user`: this decodes no JWT — it hashes the raw
    bearer token and looks up a non-revoked ``AgentDevice`` via
    ``agent_device_service.authenticate_token``. 401s when missing/invalid/
    revoked. Touches the device's ``last_seen_at`` on success and returns the
    device's owning ``User`` (stashing the device on ``user._device``) so
    downstream ownership checks and job-claim handlers work unchanged.
    """
    if credentials is None or not credentials.credentials:
        raise _unauthorized()
    device = agent_device_service.authenticate_token(db, credentials.credentials)
    if device is None:
        raise _unauthorized("Invalid device token")
    agent_device_service.touch_last_seen(db, device)
    user = db.get(User, device.owner_id)
    if user is None or not user.is_active:
        raise _unauthorized("User not found or inactive")
    user._device = device  # type: ignore[attr-defined]
    return user


def require_role(role: str):
    """Dependency factory: 403 unless the current user has ``role``."""

    def _dep(user: User = Depends(require_user)) -> User:
        if user.role != role:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user

    return _dep


def require_admin(user: User = Depends(require_user)) -> User:
    """Dependency for admin-only routes (member management, #94).

    401s (via :func:`require_user`) when there's no/invalid bearer token, 403s
    when the authenticated user isn't an admin. Equivalent to
    ``require_role(ROLE_ADMIN)`` but named for call-site clarity.
    """
    if user.role != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    return user


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User | None:
    """Best-effort resolve the current user for ownership scoping (#91).

    Unlike :func:`require_user`, this **never raises** — it returns ``None``
    when there's no bearer token, the token is invalid/expired, or the user
    can't be found/is inactive.

    # BRIDGE (#91): callers (``app.services.ownership``) treat ``None`` as
    "no scoping" so routes not yet migrated to per-user filtering (#92/#93),
    and the whole test suite (which runs with ``auth_required`` off), keep
    working unchanged. Remove this bridge in the cleanup issue (#98) once
    every route requires an authenticated user.
    """
    if credentials is None or not credentials.credentials:
        return None
    try:
        payload = auth_service.decode_access_token(credentials.credentials)
    except auth_service.AuthError:
        # Dual-accept (#479): ownership scoping must work for hub users too, or a
        # hub-authenticated request would read/write as "no owner".
        return _hub_user(db, credentials.credentials)
    user = db.get(User, int(payload.get("sub", 0) or 0))
    if user is None or not user.is_active:
        return None
    return user


# ---------------------------------------------------------------- cookies
#
# Cookie `Path` is what the BROWSER sees, not what this app sees. Behind the
# suite's shared front door the SPA lives at `/qagent/` and the prefix is
# stripped before requests arrive here — so these paths have to be written from
# the browser's point of view or the refresh cookie is set on a path that is
# never requested, and every session silently ends at the next reload.
def _refresh_cookie_path() -> str:
    """Where the refresh cookie is scoped — `/auth`, under the mount point.

    Narrow on purpose (ADR 0007): the refresh token is only ever presented to
    `/auth/*`, so scoping it there keeps it off every other request, including
    the whole of `/api`.
    """
    return f"{settings.mount_path}/auth"


def set_auth_cookies(response: Response, *, refresh_token: str, csrf_token: str, remember: bool) -> None:
    max_age = int(
        (auth_service.REFRESH_TTL_REMEMBER if remember else auth_service.REFRESH_TTL_DEFAULT).total_seconds()
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path=_refresh_cookie_path(),
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        max_age=max_age,
        httponly=False,  # readable by the SPA so it can echo it in X-CSRF-Token
        secure=settings.cookie_secure,
        samesite="lax",
        # The whole app, not just `/auth`: every mutating request echoes it.
        path=settings.mount_path or "/",
    )


def clear_auth_cookies(response: Response) -> None:
    # Must match the paths above exactly — a delete with a different Path is a
    # no-op the browser accepts silently, leaving the cookie in place.
    response.delete_cookie(REFRESH_COOKIE, path=_refresh_cookie_path())
    response.delete_cookie(CSRF_COOKIE, path=settings.mount_path or "/")


def read_refresh_cookie(request: Request) -> str | None:
    return request.cookies.get(REFRESH_COOKIE)


def read_csrf_cookie(request: Request) -> str | None:
    return request.cookies.get(CSRF_COOKIE)
