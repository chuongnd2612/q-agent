"""Provider connection model — a named account for a provider kind.

Supersedes the singleton :class:`app.models.provider.Provider` for credential
routing (see ADR 0006 revision 2). A provider *kind* (ado/jira/github) may now
hold **many** named ``ProviderConnection`` rows, and each kind has one or more
**capabilities**:

- **work_item** (``ado``, ``jira``) — the source of tickets / work items.
- **repository** (``ado``, ``github``) — the source of code repositories.

A kind can have **both** capabilities — Azure DevOps serves work items *and*
Git repos, so an ADO connection is eligible for either role.

``config`` holds non-secret connection fields (org URL, project, org, repo,
baseUrl…); secret fields (PAT / API token) are stored encrypted in ``secrets``
(see :mod:`app.crypto`) and are never serialized back to clients in plaintext.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UTCDateTime, timestamp_column, utcnow
from app.models.provider import ADO, GITHUB, JIRA

# Provider capability categories.
WORK_ITEM = "work_item"
REPOSITORY = "repository"

# Code-level classification of each kind (no per-kind DB row). A kind may carry
# more than one capability — Azure DevOps provides both work items and repos.
PROVIDER_CAPABILITIES: dict[str, tuple[str, ...]] = {
    ADO: (WORK_ITEM, REPOSITORY),
    JIRA: (WORK_ITEM,),
    GITHUB: (REPOSITORY,),
}

# Human-readable default names per kind (used when creating a connection).
PROVIDER_DISPLAY_NAMES: dict[str, str] = {
    ADO: "Azure DevOps",
    JIRA: "Jira",
    GITHUB: "GitHub",
}


def categories_for(kind: str) -> tuple[str, ...]:
    """Return the capabilities ('work_item', 'repository', …) for a provider kind."""
    return PROVIDER_CAPABILITIES.get(kind, (WORK_ITEM,))


class ProviderConnection(Base):
    """A named connection to an external provider account."""

    __tablename__ = "provider_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)  # ado/jira/github — NOT unique
    name: Mapped[str] = mapped_column(String(120), default="")
    connected: Mapped[bool] = mapped_column(Boolean, default=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    secrets: Mapped[dict] = mapped_column(JSON, default=dict)  # encrypted values
    last_sync: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_tested_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column(onupdate=utcnow)
    # Per-user ownership (#91) — data is per-user private. Nullable until the
    # cleanup issue (#98) backfills every row and enforces non-null.
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    # Set when this row MIRRORS a connection EmeHub owns (#514). Such a row is a
    # real local connection so per-connection scoping, the picker and ticket
    # filters keep working unchanged — but its ``secrets`` are empty and always
    # will be: the hub never releases the PAT (`GET /connections` returns
    # ``hasPat`` only, #501). Nothing may attempt a direct provider call with it;
    # provider work for these belongs to the hub.
    hub_connection_id: Mapped[str | None] = mapped_column(
        String(64), index=True, nullable=True, default=None
    )

    @property
    def is_hub_backed(self) -> bool:
        """True when this mirrors a hub-owned connection and holds no credential."""
        return bool(self.hub_connection_id)

    @property
    def categories(self) -> tuple[str, ...]:
        return categories_for(self.kind)
