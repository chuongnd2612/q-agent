"""Agent device model — pairs a Local Agent install with a user (Local Agent feature).

One row per paired device (the Node "Local Agent" CLI running on a user's own
machine). Mirrors the ``Session`` model's hashing pattern (``models/session.py``,
``auth_service.create_session``): only the **sha256 hash** of the device's
long-lived bearer token is stored; the plaintext is returned exactly once, at
pairing redemption time (``agent_device_service.redeem_pairing_code``), and
never persisted.

A device is valid when ``revoked_at`` is NULL. ``last_seen_at`` is stamped on
every authenticated request (job claim, event/result/evidence push — see
``agent_device_service.touch_last_seen``) so the paired-devices list in the UI
can show a live "last seen" status.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UTCDateTime, timestamp_column


class AgentDevice(Base):
    __tablename__ = "agent_devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    token_hash: Mapped[str] = mapped_column(String(64), default="")  # sha256 hex
    # The agent build that last claimed a job, self-reported on
    # ``POST /agent/jobs/next`` (#541). Empty string means "never reported" — a
    # pre-#541 agent, which is treated as BELOW the minimum by
    # ``agent_project_bundle.version_ok``. That default is the whole point of the
    # guard: an old agent flattens a layered project tree into its workdir and
    # every import fails collection, which looks like a mass test failure rather
    # than a version problem.
    agent_version: Mapped[str] = mapped_column(String(32), default="", server_default="")
    created_at: Mapped[datetime] = timestamp_column()
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, default=None)
