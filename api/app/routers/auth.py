"""Auth router — login, refresh, profile, 2FA, sessions, admin users (ADR 0007).

All request/response bodies use ``ApiModel`` schemas (camelCase on the wire).
Login sets an HttpOnly ``qagent_refresh`` cookie (Path=/auth) plus a readable
``qagent_csrf`` cookie; refresh reads them and validates the CSRF header. The
global auth guard (``main.py``) enforces bearer access tokens app-wide when
``QAGENT_AUTH_REQUIRED`` is on; this router's endpoints work regardless.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db, utcnow
from app.deps_auth import (
    CSRF_HEADER,
    clear_auth_cookies,
    hub_authed,
    read_csrf_cookie,
    read_refresh_cookie,
    require_admin,
    require_user,
    set_auth_cookies,
)
from app.logging import logger
from app.models.user import ROLE_ADMIN, USER_ROLES, User
from app.schemas import (
    AdminCreateUserRequest,
    AdminInviteUserRequest,
    AdminInviteUserResponse,
    AdminUpdateUserRequest,
    AdminUserOut,
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    MfaLoginRequest,
    OkResponse,
    RefreshResponse,
    RequestResetRequest,
    RequestResetResponse,
    ResetRequest,
    SessionOut,
    TotpCodeRequest,
    TotpDisableRequest,
    TotpSetupResponse,
    UpdateMeRequest,
    UserOut,
)
from app.services import audit_service, auth_service

router = APIRouter(tags=["auth"])


# ---------------------------------------------------------------- helpers
def _client_meta(request: Request) -> tuple[str, str]:
    ua = request.headers.get("user-agent", "")
    ip = request.client.host if request.client else ""
    return ua, ip


def _issue_login(db: Session, user: User, request: Request, response: Response, remember: bool) -> LoginResponse:
    """Create a session, set cookies, and return the access token + user."""
    ua, ip = _client_meta(request)
    session, refresh_token = auth_service.create_session(
        db, user, remember=remember, user_agent=ua, ip=ip
    )
    user.last_active = utcnow()
    db.add(user)
    db.commit()
    csrf = auth_service.generate_csrf_token()
    set_auth_cookies(response, refresh_token=refresh_token, csrf_token=csrf, remember=remember)
    access = auth_service.create_access_token(user, session.id)
    return LoginResponse(access_token=access, user=UserOut.model_validate(user))


def _is_prod() -> bool:
    """Prod = secure cookies (HTTPS). Governs whether the reset token is echoed."""
    return settings.cookie_secure


# ---------------------------------------------------------------- public
@router.post("/auth/login", response_model=LoginResponse)
def login(body: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)) -> LoginResponse:
    user = auth_service.authenticate(db, body.email, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if user.totp_enabled:
        return LoginResponse(mfa_required=True, mfa_token=auth_service.create_mfa_token(user))
    result = _issue_login(db, user, request, response, body.remember)
    audit_service.record(category="auth", actor_type="user", action="Signed in", target=user.email, ip=user_ip(request))
    return result


@router.post("/auth/login/mfa", response_model=LoginResponse)
def login_mfa(body: MfaLoginRequest, request: Request, response: Response, db: Session = Depends(get_db)) -> LoginResponse:
    try:
        payload = auth_service.decode_mfa_token(body.mfa_token)
    except auth_service.AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    user = db.get(User, int(payload.get("sub", 0) or 0))
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    if not (user.totp_enabled and auth_service.verify_totp(user.totp_secret or "", body.code)):
        raise HTTPException(status_code=401, detail="Invalid verification code")
    # remember is not carried through the MFA step; default to a standard session.
    result = _issue_login(db, user, request, response, remember=False)
    audit_service.record(category="auth", actor_type="user", action="Signed in (2FA)", target=user.email, ip=user_ip(request))
    return result


@router.post("/auth/refresh", response_model=RefreshResponse)
def refresh(request: Request, response: Response, db: Session = Depends(get_db)) -> RefreshResponse:
    token = read_refresh_cookie(request)
    if not token:
        raise HTTPException(status_code=401, detail="Missing refresh token")
    if not auth_service.verify_csrf(read_csrf_cookie(request), request.headers.get(CSRF_HEADER)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    # Find the matching, still-valid session by verifying the token hash.
    from app.models.session import Session as AuthSession

    candidates = (
        db.query(AuthSession).filter(AuthSession.revoked_at.is_(None)).all()
    )
    session = next(
        (s for s in candidates if auth_service.verify_refresh(s, token) and auth_service.get_valid_session(db, s.id)),
        None,
    )
    if session is None:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    user = db.get(User, session.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    new_token = auth_service.rotate(db, session)
    user.last_active = utcnow()
    db.add(user)
    db.commit()
    csrf = auth_service.generate_csrf_token()
    # Preserve the cookie lifetime bucket (remember) by reusing the session's ttl.
    remember = (session.expires_at - session.created_at).days >= 1 if session.expires_at and session.created_at else False
    set_auth_cookies(response, refresh_token=new_token, csrf_token=csrf, remember=remember)
    access = auth_service.create_access_token(user, session.id)
    return RefreshResponse(access_token=access, user=UserOut.model_validate(user))


@router.post("/auth/request-reset", response_model=RequestResetResponse)
def request_reset(body: RequestResetRequest, db: Session = Depends(get_db)) -> RequestResetResponse:
    user = auth_service.get_user_by_email(db, body.email)
    if user is None:
        # Don't leak which emails exist.
        return RequestResetResponse(ok=True, token=None)
    token = auth_service.create_reset_token(user)
    # DEV STUB: email delivery is not wired. Log the link and (in non-prod) return the token.
    logger.info("Password reset requested for {} — reset token: {}", user.email, token)
    return RequestResetResponse(ok=True, token=None if _is_prod() else token)


@router.post("/auth/reset", response_model=OkResponse)
def reset_password(body: ResetRequest, db: Session = Depends(get_db)) -> OkResponse:
    try:
        payload = auth_service.decode_reset_token(body.token)
    except auth_service.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    user = db.get(User, int(payload.get("sub", 0) or 0))
    if user is None:
        raise HTTPException(status_code=400, detail="Invalid reset token")
    user.password_hash = auth_service.hash_password(body.password)
    db.add(user)
    # Revoke all sessions on password reset.
    auth_service.revoke_others(db, user.id, keep_sid="")
    db.commit()
    audit_service.record(category="auth", actor_type="user", action="Reset password", target=user.email)
    return OkResponse()


# ---------------------------------------------------------------- authenticated
@router.get("/auth/me", response_model=UserOut)
def get_me(user: User = Depends(require_user)) -> UserOut:
    return UserOut.model_validate(user)


@router.patch("/auth/me", response_model=UserOut)
def update_me(body: UpdateMeRequest, user: User = Depends(require_user), db: Session = Depends(get_db)) -> UserOut:
    if body.first_name is not None:
        user.first_name = body.first_name
    if body.last_name is not None:
        user.last_name = body.last_name
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserOut.model_validate(user)


@router.post("/auth/change-password", response_model=OkResponse)
def change_password(body: ChangePasswordRequest, user: User = Depends(require_user), db: Session = Depends(get_db)) -> OkResponse:
    if not auth_service.verify_password(user.password_hash, body.current_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    user.password_hash = auth_service.hash_password(body.new_password)
    db.add(user)
    db.commit()
    audit_service.record(category="auth", actor_type="user", action="Changed password", target=user.email)
    return OkResponse()


@router.post("/auth/logout", response_model=OkResponse)
def logout(response: Response, user: User = Depends(require_user), db: Session = Depends(get_db)) -> OkResponse:
    # A hub-authenticated request has no local session to revoke: its `sid` is a
    # HUB session id and revoking it against `auth_sessions` is meaningless
    # (#479). Clearing the cookies below is still correct.
    sid = None if hub_authed(user) else getattr(user, "_sid", None)
    if sid:
        auth_service.revoke(db, sid)
    clear_auth_cookies(response)
    audit_service.record(category="auth", actor_type="user", action="Signed out", target=user.email)
    return OkResponse()


@router.post("/auth/2fa/setup", response_model=TotpSetupResponse)
def totp_setup(user: User = Depends(require_user), db: Session = Depends(get_db)) -> TotpSetupResponse:
    secret = auth_service.generate_totp_secret()
    user.totp_secret = secret
    user.totp_enabled = False  # not enabled until a code is verified
    db.add(user)
    db.commit()
    return TotpSetupResponse(secret=secret, otpauth_uri=auth_service.totp_provisioning_uri(secret, user.email))


@router.post("/auth/2fa/enable", response_model=OkResponse)
def totp_enable(body: TotpCodeRequest, user: User = Depends(require_user), db: Session = Depends(get_db)) -> OkResponse:
    if not user.totp_secret:
        raise HTTPException(status_code=400, detail="Run 2FA setup first")
    if not auth_service.verify_totp(user.totp_secret, body.code):
        raise HTTPException(status_code=400, detail="Invalid verification code")
    user.totp_enabled = True
    db.add(user)
    db.commit()
    audit_service.record(category="auth", actor_type="user", action="Enabled 2FA", target=user.email)
    return OkResponse()


@router.post("/auth/2fa/disable", response_model=OkResponse)
def totp_disable(body: TotpDisableRequest, user: User = Depends(require_user), db: Session = Depends(get_db)) -> OkResponse:
    ok = False
    if body.code and user.totp_secret:
        ok = auth_service.verify_totp(user.totp_secret, body.code)
    if not ok and body.password:
        ok = auth_service.verify_password(user.password_hash, body.password)
    if not ok:
        raise HTTPException(status_code=400, detail="Provide a valid 2FA code or your password")
    user.totp_enabled = False
    user.totp_secret = None
    db.add(user)
    db.commit()
    audit_service.record(category="auth", actor_type="user", action="Disabled 2FA", target=user.email)
    return OkResponse()


@router.get("/auth/sessions", response_model=list[SessionOut])
def list_sessions(user: User = Depends(require_user), db: Session = Depends(get_db)) -> list[SessionOut]:
    # None for a hub-authenticated request: no listed local session is "current"
    # because the hub `sid` isn't one of them (#479).
    current_sid = None if hub_authed(user) else getattr(user, "_sid", None)
    out: list[SessionOut] = []
    for s in auth_service.list_sessions(db, user.id):
        so = SessionOut.model_validate(s)
        so.current = s.id == current_sid
        out.append(so)
    return out


@router.delete("/auth/sessions/{session_id}", response_model=OkResponse)
def revoke_session(session_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)) -> OkResponse:
    from app.models.session import Session as AuthSession

    session = db.get(AuthSession, session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(status_code=404, detail="Session not found")
    auth_service.revoke(db, session_id)
    return OkResponse()


@router.post("/auth/sessions/revoke-others", response_model=OkResponse)
def revoke_other_sessions(user: User = Depends(require_user), db: Session = Depends(get_db)) -> OkResponse:
    if hub_authed(user):
        # Refuse rather than pass keep_sid="" (which would revoke *every* local
        # session, including this user's other tabs) or the hub `sid` (which
        # matches no row at all). Sign in with a Q-Agent session to manage
        # Q-Agent sessions (#479).
        raise HTTPException(status_code=400, detail="Session management requires a Q-Agent session")
    sid = getattr(user, "_sid", None) or ""
    auth_service.revoke_others(db, user.id, keep_sid=sid)
    return OkResponse()


@router.delete("/auth/me", response_model=OkResponse)
def delete_me(response: Response, user: User = Depends(require_user), db: Session = Depends(get_db)) -> OkResponse:
    from app.models.session import Session as AuthSession

    db.query(AuthSession).filter(AuthSession.user_id == user.id).delete(synchronize_session=False)
    email = user.email
    db.delete(user)
    db.commit()
    clear_auth_cookies(response)
    audit_service.record(category="auth", actor_type="user", action="Deleted account", target=email)
    return OkResponse()


# ---------------------------------------------------------------- admin
def _active_admin_count(db: Session, *, exclude_id: int | None = None) -> int:
    """Count active admins, optionally excluding one user id (for lockout checks)."""
    query = db.query(User).filter(User.role == ROLE_ADMIN, User.is_active.is_(True))
    if exclude_id is not None:
        query = query.filter(User.id != exclude_id)
    return query.count()


@router.get("/auth/users", response_model=list[AdminUserOut])
def list_users(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> list[AdminUserOut]:
    """List all users with each one's ``credentialSource`` (#95): "personal" if
    they've uploaded their own Claude credential, else "shared" if a
    workspace-shared credential exists to fall back to, else "none"."""
    from app.models.claude_credentials import ClaudeCredentials

    rows = db.query(User).order_by(User.created_at.asc()).all()
    owned_ids = {
        r[0]
        for r in db.query(ClaudeCredentials.owner_id).filter(ClaudeCredentials.owner_id.isnot(None)).all()
    }
    has_shared = db.query(ClaudeCredentials).filter(ClaudeCredentials.owner_id.is_(None)).first() is not None

    out: list[AdminUserOut] = []
    for u in rows:
        item = AdminUserOut.model_validate(u)
        item.credential_source = "personal" if u.id in owned_ids else "shared" if has_shared else "none"
        out.append(item)
    return out


@router.post("/auth/users/invite", response_model=AdminInviteUserResponse, status_code=201)
def invite_user(
    body: AdminInviteUserRequest, admin: User = Depends(require_admin), db: Session = Depends(get_db)
) -> AdminInviteUserResponse:
    """Admin-only: create a user with no password and issue a reset token so
    they can set one via the existing ``/auth/reset`` flow (dev-stub email,
    same as ``/auth/request-reset``)."""
    email = (body.email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email is required")
    if body.role not in USER_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role '{body.role}'")
    if auth_service.get_user_by_email(db, email) is not None:
        raise HTTPException(status_code=409, detail="A user with that email already exists")
    user = User(
        email=email,
        first_name=body.first_name,
        last_name=body.last_name,
        role=body.role,
        password_hash="",  # unusable until the invite's reset token is redeemed
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    token = auth_service.create_reset_token(user)
    logger.info("User {} invited — set-password token: {}", user.email, token)
    audit_service.record(
        category="auth", actor_type="user", action="Invited user", target=email, meta=f"role={body.role}"
    )
    return AdminInviteUserResponse(user=UserOut.model_validate(user), reset_token=None if _is_prod() else token)


@router.post("/auth/users", response_model=UserOut, status_code=201)
def create_user(body: AdminCreateUserRequest, admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> UserOut:
    email = (body.email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email is required")
    if body.role not in USER_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role '{body.role}'")
    if auth_service.get_user_by_email(db, email) is not None:
        raise HTTPException(status_code=409, detail="A user with that email already exists")
    user = User(
        email=email,
        first_name=body.first_name,
        last_name=body.last_name,
        role=body.role,
        password_hash=auth_service.hash_password(body.password),
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    audit_service.record(category="auth", actor_type="user", action="Created user", target=email, meta=f"role={body.role}")
    return UserOut.model_validate(user)


@router.patch("/auth/users/{user_id}", response_model=UserOut)
def update_user(user_id: int, body: AdminUpdateUserRequest, admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> UserOut:
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if body.role is not None and body.role not in USER_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role '{body.role}'")
    new_role = body.role if body.role is not None else target.role
    new_is_active = body.is_active if body.is_active is not None else target.is_active
    # Lockout guard: block a role/status change that would leave zero active
    # admins (covers an admin demoting/deactivating themselves or another).
    was_active_admin = target.role == ROLE_ADMIN and target.is_active
    will_be_active_admin = new_role == ROLE_ADMIN and new_is_active
    if was_active_admin and not will_be_active_admin and _active_admin_count(db, exclude_id=target.id) == 0:
        raise HTTPException(status_code=400, detail="Cannot leave the workspace with zero active admins")
    target.role = new_role
    target.is_active = new_is_active
    db.add(target)
    db.commit()
    db.refresh(target)
    audit_service.record(category="auth", actor_type="user", action="Updated user", target=target.email)
    return UserOut.model_validate(target)


@router.delete("/auth/users/{user_id}", response_model=OkResponse)
def delete_user(user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> OkResponse:
    from app.models.session import Session as AuthSession

    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    # Lockout guard: removing the last active admin (self or otherwise) is blocked.
    if target.role == ROLE_ADMIN and target.is_active and _active_admin_count(db, exclude_id=target.id) == 0:
        raise HTTPException(status_code=400, detail="Cannot remove the last active admin")
    db.query(AuthSession).filter(AuthSession.user_id == target.id).delete(synchronize_session=False)
    email = target.email
    db.delete(target)
    db.commit()
    audit_service.record(category="auth", actor_type="user", action="Deleted user", target=email)
    return OkResponse()


def user_ip(request: Request) -> str:
    return request.client.host if request.client else ""
