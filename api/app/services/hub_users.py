"""Mapping an EmeHub agent token onto a local Q-Agent user (B2 of #479).

``app.services.hub_tokens`` is a pure decoder — it never touches the database.
This module is the other half: it takes a *verified* set of :class:`HubClaims`
and resolves the local ``users`` row the request should act as, provisioning one
just-in-time on first sight.

Three rules, all of them decisions settled in ``docs/HUB-INTEGRATION.md`` §8 and
issue #479:

- **``hub_user_id`` is the mapping column, never ``sub``-as-a-local-id.** A hub
  token's ``sub`` is a *hub* user id; casting it to a local ``users.id`` resolves
  to the wrong account (or none). Local ids stay exactly as they are so every
  ``owner_id`` row, evidence file and workspace path keeps working (ADR 0009).
- **Email collision → auto-link, with an audit entry.** A hub user whose email
  already exists locally links to that row. The accepted risk is stated plainly
  in §8: whoever controls that address at the hub inherits the local account, so
  every link is written to the audit log.
- **``role`` is a create-time seed only.** The claim supplies a default for
  accounts that did not exist yet and is *ignored* on every subsequent login —
  local ``users.role`` stays authoritative. Deliberately **not** written as a
  find-or-create that refreshes attributes: that would silently make a role
  change at the hub escalate privilege here.

Everything here is gated on ``settings.hub_sso_enabled``. With the flag off,
:func:`resolve_user` and :func:`token_valid` behave as if hub tokens did not
exist, which is what keeps the integration dormant by default.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.logging import logger
from app.models.user import ROLE_MEMBER, USER_ROLES, User
from app.services import audit_service, hub_tokens
from app.services.hub_tokens import HubClaims, HubTokenError


def claims_for(token: str | None) -> HubClaims | None:
    """Verify ``token`` as a hub token, or return ``None``.

    ``None`` covers every "this is not an acceptable hub token" case with no
    exceptions leaking to callers: SSO disabled, no token, a token that isn't
    hub-shaped (no ``iss: emehub``), or one that fails verification. Callers on
    the dual-accept path only need "did this resolve or not".
    """
    if not token or not settings.hub_sso_enabled:
        return None
    if not hub_tokens.looks_like_hub_token(token):
        return None
    try:
        return hub_tokens.decode(token)
    except HubTokenError as exc:
        logger.debug("hub token rejected: {}", exc)
        return None


def token_valid(token: str | None) -> bool:
    """True if ``token`` is an acceptable hub token (guard/WS use).

    The hub-side mirror of ``auth_service.access_token_valid``: signature only,
    no database work, so the global ``auth_guard`` middleware can accept a hub
    token without provisioning anything.
    """
    return claims_for(token) is not None


def resolve_user(db: Session, token: str | None) -> User | None:
    """Resolve the local user for ``token``, JIT-provisioning on first sight.

    Returns ``None`` when the token isn't an acceptable hub token (see
    :func:`claims_for`). The returned user may be inactive — callers apply their
    own ``is_active`` check, exactly as they do for local tokens.
    """
    claims = claims_for(token)
    if claims is None:
        return None
    return provision(db, claims)


def provision(db: Session, claims: HubClaims) -> User:
    """Find (or create) the local user for verified ``claims``.

    Resolution order:

    1. ``users.hub_user_id == claims.hub_user_id`` — the steady state. Returned
       **untouched**: no attribute refresh, so a hub-side role change cannot
       escalate an existing local account.
    2. ``users.email == claims.normalized_email`` — auto-link: stamp
       ``hub_user_id`` onto the existing row and record an audit event.
    3. Otherwise create a new local user, seeding ``role`` from the claim.
    """
    existing = db.query(User).filter(User.hub_user_id == claims.hub_user_id).first()
    if existing is not None:
        return existing

    by_email = db.query(User).filter(User.email == claims.normalized_email).first()
    if by_email is not None:
        return _link(db, by_email, claims)

    return _create(db, claims)


def _link(db: Session, user: User, claims: HubClaims) -> User:
    """Attach ``claims.hub_user_id`` to an existing local account, audibly.

    Role is *not* touched — this account already existed, so Q-Agent's own role
    is authoritative (§8 decision 2).
    """
    user.hub_user_id = claims.hub_user_id
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info(
        "hub SSO: linked local user {} (id={}) to hub user {}", user.email, user.id, claims.hub_user_id
    )
    audit_service.record(
        category="auth",
        actor_type="system",
        action="Linked EmeHub account",
        target=user.email,
        meta=f"hubUserId={claims.hub_user_id} role={user.role} (local role kept)",
    )
    return user


def _create(db: Session, claims: HubClaims) -> User:
    """Provision a brand-new local user from a hub token.

    This is the **only** place ``claims.role`` is read. An unrecognised value
    falls back to ``member`` rather than being trusted verbatim. No password hash
    is set: the account is reachable through the hub (or a local password reset),
    never by guessing an empty password — ``verify_password`` rejects an empty
    hash outright.
    """
    role = claims.role if claims.role in USER_ROLES else ROLE_MEMBER
    local_part = claims.normalized_email.split("@", 1)[0]
    user = User(
        email=claims.normalized_email,
        first_name=local_part[:120],
        last_name="",
        role=role,
        password_hash="",
        is_active=True,
        hub_user_id=claims.hub_user_id,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # Two concurrent first-sight requests for the same hub user (the unique
        # index on hub_user_id is what makes this benign). Roll back and take
        # whichever row won.
        db.rollback()
        winner = db.query(User).filter(User.hub_user_id == claims.hub_user_id).first()
        if winner is None:  # pragma: no cover - the email index lost the race instead
            raise
        return winner
    db.refresh(user)
    logger.info(
        "hub SSO: provisioned local user {} (id={}) for hub user {} with role {}",
        user.email,
        user.id,
        claims.hub_user_id,
        user.role,
    )
    audit_service.record(
        category="auth",
        actor_type="system",
        action="Provisioned user from EmeHub",
        target=user.email,
        meta=f"hubUserId={claims.hub_user_id} role={user.role}",
    )
    return user
