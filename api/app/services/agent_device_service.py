"""Local Agent device pairing + authentication (Local Agent feature).

A device (the Node "Local Agent" CLI running on a user's own machine)
authenticates with a long-lived opaque bearer token, mirroring the
refresh-session pattern in :mod:`app.services.auth_service`
(``create_session`` — see ``auth_service.py:129-158``): only the sha256 hash of
the device token is ever persisted; the plaintext is returned exactly once, at
pairing redemption.

Pairing flow:

1. The SPA (an already-authenticated user) requests a short-lived pairing code
   (:func:`create_pairing_code`) — a 6-digit code held in an in-memory pending
   store (~5 min TTL) mapped to the user's id. Kept safe by being single-use,
   short-lived, and rate-limited at redemption (see :func:`redeem_pairing_code`).
2. The user types the code into the Local Agent, which redeems it
   (:func:`redeem_pairing_code`) to create an :class:`AgentDevice` row and
   receive the one-time plaintext device token.
3. The agent authenticates subsequent requests with ``Authorization: Bearer
   <device-token>``, resolved via :func:`authenticate_token`
   (``app.deps_auth.require_agent``).

The pending-code store stays **in memory on purpose** — the reasoning is written
out at :data:`_pending` (#625), where the sibling agent queues were made durable.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from datetime import timedelta

from sqlalchemy.orm import Session as DbSession

from app.db import utcnow
from app.models.agent_device import AgentDevice
from app.models.user import User
from app.services.auth_service import AuthError

# Short-lived — long enough to type the 6-digit code into the agent, short
# enough that a leaked code can't be redeemed later.
PAIR_TTL = timedelta(minutes=5)
_CODE_SPACE = 1_000_000  # 6-digit codes: "000000".."999999"

# In-memory pending pairing codes (single-process, like the run_control
# registry): code -> (owner_id, expiry as time.monotonic()). A 6-digit code is
# only 1M combinations, so pairing is kept safe by three properties: it is
# single-use (popped on redeem), short-lived (PAIR_TTL), and redemption is
# throttled below — brute-forcing 1M codes within 5 min is infeasible at the
# allowed attempt rate.
#
# DELIBERATELY MEMORY, not a table (#625, reviewed alongside the capture queue
# and the #605 authoring queue, both of which were made durable). Three reasons,
# so this is a decision and not an accident of history:
#
# 1. **Losing one is cheap and self-announcing.** The user is sitting in front of
#    the pairing dialog with the code on screen; a lost code fails loudly at
#    redemption ("Invalid or expired pairing code") and the fix is one click —
#    request another. Contrast the capture/authoring queues, where the loss was
#    *silent*: the agent just kept getting 204 and the operator concluded the
#    product was broken.
# 2. **Persisting it would be a net security loss.** The code is a bearer secret
#    with a 5-minute life whose entire safety argument is single-use +
#    short-lived + rate-limited. Writing 1M-space plaintext secrets to the
#    database (or hashing them, which then needs a per-code lookup) widens the
#    blast radius of a DB read for no operator benefit.
# 3. **Multi-worker is not a real hazard here.** Unlike a *queue*, both halves of
#    pairing are driven by the same human within seconds: the code is minted for
#    the SPA and typed straight into the agent. A redeem landing on a worker that
#    did not mint the code fails closed (invalid code) and retrying pairs
#    successfully; nothing is left half-done, and no row anywhere claims a device
#    exists when it doesn't.
#
# The one thing that must NOT regress into memory is anything a background poller
# waits on — that is exactly what #605/#625 were.
_pending: dict[str, tuple[int, float]] = {}
# Sliding window of failed-redeem timestamps, to throttle guessing.
_failures: list[float] = []
_lock = threading.Lock()
_FAIL_WINDOW_S = 60.0
_FAIL_MAX = 10


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _purge_expired(now: float) -> None:
    """Drop expired pending codes (caller holds ``_lock``)."""
    for code in [c for c, (_owner, exp) in _pending.items() if exp <= now]:
        _pending.pop(code, None)


def create_pairing_code(db: DbSession, user: User) -> str:
    """Issue a short-lived 6-digit pairing code for ``user``.

    The code is held in the in-memory pending store until redeemed or expired.
    ``db`` is accepted for signature symmetry (no row is created until redemption).

    Returns:
        A zero-padded 6-digit code (e.g. ``"048213"``), valid for :data:`PAIR_TTL`.
    """
    now = time.monotonic()
    with _lock:
        _purge_expired(now)
        code = f"{secrets.randbelow(_CODE_SPACE):06d}"
        # Avoid clobbering another live code (extremely rare); retry a few times.
        for _ in range(20):
            if code not in _pending:
                break
            code = f"{secrets.randbelow(_CODE_SPACE):06d}"
        _pending[code] = (int(user.id), now + PAIR_TTL.total_seconds())
    return code


def redeem_pairing_code(db: DbSession, code: str, name: str = "") -> tuple[AgentDevice, str]:
    """Validate a 6-digit pairing code and create a new paired device.

    Args:
        db: Active session.
        code: The 6-digit code minted by :func:`create_pairing_code`.
        name: Optional human-friendly device name (e.g. hostname), shown in the
            paired-devices list. Defaults to "Local Agent" when blank.

    Returns:
        ``(device, plaintext_token)`` — the plaintext is returned ONLY here; the
        stored row holds just its sha256 hash.

    Raises:
        AuthError: if the code is invalid/expired, or redemption is throttled
            after too many recent failed attempts.
    """
    code = (code or "").strip()
    now = time.monotonic()
    with _lock:
        _purge_expired(now)
        _failures[:] = [t for t in _failures if now - t < _FAIL_WINDOW_S]
        if len(_failures) >= _FAIL_MAX:
            raise AuthError("Too many pairing attempts — wait a minute and try again.")
        entry = _pending.pop(code, None)  # single-use: remove on redeem
        if entry is None or entry[1] <= now:
            _failures.append(now)
            raise AuthError("Invalid or expired pairing code.")
        owner_id = entry[0]

    token = secrets.token_urlsafe(48)
    device = AgentDevice(
        owner_id=owner_id,
        name=(name or "").strip()[:200] or "Local Agent",
        token_hash=_hash_token(token),
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    return device, token


def authenticate_token(db: DbSession, raw: str) -> AgentDevice | None:
    """Resolve a device from its raw bearer token. None if missing/invalid/revoked."""
    if not raw:
        return None
    return (
        db.query(AgentDevice)
        .filter(AgentDevice.token_hash == _hash_token(raw), AgentDevice.revoked_at.is_(None))
        .first()
    )


def touch_last_seen(db: DbSession, device: AgentDevice) -> None:
    """Stamp ``device.last_seen_at`` = now and persist."""
    device.last_seen_at = utcnow()
    db.add(device)
    db.commit()


def list_devices(db: DbSession, user: User) -> list[AgentDevice]:
    """Non-revoked devices paired to ``user``, newest first."""
    return (
        db.query(AgentDevice)
        .filter(AgentDevice.owner_id == user.id, AgentDevice.revoked_at.is_(None))
        .order_by(AgentDevice.created_at.desc())
        .all()
    )


def revoke_self(db: DbSession, device: AgentDevice) -> None:
    """Revoke a device on the device's own behalf (Local Agent "Disconnect").

    Called by the paired agent itself (``POST /agent/disconnect``) when the user
    clicks Disconnect — which also wipes the agent's locally-stored token — so
    the device drops out of the owner's paired-devices list immediately instead
    of lingering as "online" until its ``last_seen_at`` goes stale. Idempotent.
    """
    if device.revoked_at is None:
        device.revoked_at = utcnow()
        db.add(device)
        db.commit()


def revoke(db: DbSession, user: User, device_id: int) -> AgentDevice | None:
    """Revoke a device owned by ``user``.

    Returns:
        The revoked device, or ``None`` if no such device exists / it isn't
        owned by ``user`` (idempotent if it was already revoked).
    """
    device = db.get(AgentDevice, device_id)
    if device is None or device.owner_id != user.id:
        return None
    if device.revoked_at is None:
        device.revoked_at = utcnow()
        db.add(device)
        db.commit()
    return device
