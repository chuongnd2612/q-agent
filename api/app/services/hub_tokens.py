"""EmeHub agent-token decoding (B1 of the hub SSO integration, #478).

Q-Agent accepts short-lived **agent tokens** minted by EmeHub so a user who
signed in at the hub lands here already authenticated. This module is the only
place that understands those tokens.

The contract is frozen in ``docs/HUB-INTEGRATION.md`` §2 and mirrors
``emehub/api/app/services/auth_service.py::_decode`` deliberately — the two
decoders must not drift.

Claims (HS256)::

    {"sub": "3",            # the HUB's user id, NOT Q-Agent's — see §3.1
     "email": "…", "role": "admin" | "member",
     "sid": "3a7e…",        # hub session id, NOT a Q-Agent auth_sessions id
     "aud": "qagent", "iss": "emehub",
     "iat": …, "exp": …}    # 15 minute lifetime

Two rules this module enforces by construction:

- **We only ever verify.** Q-Agent never issues, refreshes or extends a hub
  token; only the hub does that. There is deliberately no ``encode`` here.
- **``kid`` is logged, never verified against.** The hub moves to RS256 + JWKS
  in Phase 3, and emitting ``kid`` now is what makes that upgrade
  non-breaking, so reading it must not become load-bearing.

``sub`` is a *hub* user id and is never a local ``users.id``. Callers resolve a
local user through ``users.hub_user_id``; casting ``sub`` to a local id would
silently resolve to the wrong account.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import jwt

from app.config import settings
from app.logging import logger

_ALGO = "HS256"

# Claims required on every hub token. `sub` identifies the hub user, `iss`/`aud`
# scope the token to this agent, `iat`/`exp` bound its lifetime. A token missing
# any of these is rejected rather than defaulted.
_REQUIRED_CLAIMS = ("exp", "iat", "iss", "aud", "sub")

HUB_ISSUER = "emehub"

# `kid` values we've already logged, so a steady stream of tokens doesn't emit a
# line each. Bounded by the number of distinct signing keys the hub has used.
_seen_kids: set[str] = set()


class HubTokenError(Exception):
    """Raised when a hub token is missing, malformed, expired or not for us.

    Deliberately distinct from ``auth_service.AuthError`` so callers can tell
    "this wasn't a valid hub token" from "this wasn't a valid local token" while
    walking the dual-accept path.
    """


@dataclass(frozen=True)
class HubClaims:
    """Validated claims from an EmeHub agent token.

    ``hub_user_id`` is the raw ``sub`` — a **hub** user id. It maps to a local
    account through ``users.hub_user_id`` and must never be used as a local
    ``users.id``. ``hub_session_id`` is likewise a hub session id and has no
    counterpart in the local ``auth_sessions`` table.
    """

    hub_user_id: str
    email: str
    role: str
    hub_session_id: str | None
    issued_at: int
    expires_at: int

    @property
    def normalized_email(self) -> str:
        """Lowercased email, matching how ``users.email`` is stored."""
        return self.email.strip().lower()


def _log_kid_once(token: str) -> None:
    """Log the token's ``kid`` header the first time we see each distinct value.

    Informational only — verification never keys on ``kid`` (see module docstring).
    Never raises: an unreadable header must not fail an otherwise valid token.
    """
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except Exception as exc:  # noqa: BLE001 - purely informational
        logger.debug("hub token: could not read kid header: {}", exc)
        return
    if kid and kid not in _seen_kids:
        _seen_kids.add(str(kid))
        logger.info("hub token: signing key id (kid) in use: {}", kid)


def decode(token: str) -> HubClaims:
    """Verify an EmeHub agent token and return its claims.

    Validates the signature against ``settings.hub_jwt_secret``, plus
    ``iss == "emehub"``, ``aud == settings.hub_audience`` and ``exp``.

    Raises :class:`HubTokenError` on anything invalid — no secret configured, a
    bad signature, an expired token, a token minted for a different agent
    (e.g. ``aud: "dagent"``), or a missing required claim.
    """
    if not token:
        raise HubTokenError("Missing hub token")
    if not settings.hub_jwt_secret:
        # Refuse rather than fall back to any other secret: QAGENT_SECRET_KEY
        # signs local JWTs and derives the credential-encryption key, and
        # overloading it here is exactly what emehub ADR 0005 forbids.
        raise HubTokenError("Hub JWT secret is not configured")

    _log_kid_once(token)

    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.hub_jwt_secret,
            algorithms=[_ALGO],
            issuer=HUB_ISSUER,
            audience=settings.hub_audience,
            options={"require": list(_REQUIRED_CLAIMS)},
        )
    except jwt.ExpiredSignatureError as exc:
        raise HubTokenError("Hub token expired") from exc
    except jwt.InvalidAudienceError as exc:
        raise HubTokenError("Hub token was not minted for this agent") from exc
    except jwt.InvalidIssuerError as exc:
        raise HubTokenError("Hub token has an unexpected issuer") from exc
    except jwt.MissingRequiredClaimError as exc:
        raise HubTokenError(f"Hub token is missing a required claim: {exc.claim}") from exc
    except jwt.InvalidTokenError as exc:
        raise HubTokenError("Invalid hub token") from exc

    sub = str(payload.get("sub") or "").strip()
    if not sub:
        raise HubTokenError("Hub token has an empty subject")

    email = str(payload.get("email") or "").strip()
    if not email:
        # The bootstrap needs an email to provision or link a local account, so
        # a token without one is unusable even though it verified.
        raise HubTokenError("Hub token is missing an email")

    return HubClaims(
        hub_user_id=sub,
        email=email,
        role=str(payload.get("role") or ""),
        hub_session_id=(str(payload["sid"]) if payload.get("sid") else None),
        issued_at=int(payload["iat"]),
        expires_at=int(payload["exp"]),
    )


def looks_like_hub_token(token: str) -> bool:
    """Cheap, unverified check for whether ``token`` is a hub token at all.

    Branches on the presence of an ``iss`` claim, which separates the two token
    kinds with no ambiguity (``docs/HUB-INTEGRATION.md`` §2.3): Q-Agent's own
    access tokens carry ``typ`` and no ``iss``/``aud``; hub tokens carry
    ``iss``/``aud`` and no ``typ``.

    **This proves nothing about validity** — it reads unverified claims and is
    only for deciding which decoder to try. Always follow it with :func:`decode`.
    """
    if not token:
        return False
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.InvalidTokenError:
        return False
    return claims.get("iss") == HUB_ISSUER
