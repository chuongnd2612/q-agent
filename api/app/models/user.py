"""User model — an authenticated account (ADR 0007).

Local-first installs may have zero users (the global auth guard is off by
default). For shared deployments, users are seeded via ``QAGENT_ADMIN_EMAIL`` /
``QAGENT_ADMIN_PASSWORD`` or created by an admin through ``/auth/users``.

``email`` is always stored lowercased and is unique. ``password_hash`` holds an
argon2 hash (never plaintext). ``role`` is ``"admin"`` or ``"member"``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UTCDateTime, timestamp_column, utcnow

# Role values.
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
USER_ROLES = (ROLE_ADMIN, ROLE_MEMBER)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)  # stored lowercased
    first_name: Mapped[str] = mapped_column(String(120), default="")
    last_name: Mapped[str] = mapped_column(String(120), default="")
    role: Mapped[str] = mapped_column(String(16), default=ROLE_MEMBER)
    password_hash: Mapped[str] = mapped_column(String(255), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column(onupdate=utcnow)
    # Stamped on successful login and token refresh (never backfilled).
    last_active: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, default=None)
    # EmeHub user id this account maps to (#478). NULL for local-only accounts.
    #
    # This is the mapping column that keeps the hub integration cheap: a hub
    # token's `sub` is a HUB user id and never equals a local `users.id`, so it
    # resolves here instead. Local ids stay exactly as they are, which matters
    # because nearly every table carries `owner_id -> users.id` and the per-user
    # workspace is literally a path built from it (ADR 0009) — re-pointing
    # `owner_id` at hub ids would be a migration across every scoped table.
    # Stored as a string: the hub's `sub` claim is a string, and treating it as
    # opaque avoids assuming both sides number users the same way.
    hub_user_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True, nullable=True, default=None
    )
