"""Business Knowledge models — the project's *domain*, as opposed to its code.

:class:`app.models.knowledge.ProjectKnowledge` answers *how the product is
built* (routes, selectors, page objects), because it is produced by pointing the
``project-bootstrap`` skill at a repo checkout. It does not answer *what the
product is supposed to do*, which is the question a QC writes test cases from.
Business Knowledge is that second, peer grounding source: wiki links and
uploaded documents carrying the project's own rules and vocabulary (epic #813,
ADR 0016).

Why new tables rather than an extension of ``ProjectKnowledge`` — three
structural reasons, each verified against the code, recorded here so the next
reader does not re-litigate the decision:

1. **Wrong grain.** ``knowledge.compose_key`` keys a row on
   ``"<project>::<repo>"`` and ``project_config_service.build_context`` resolves
   it *per target repo*, falling back to a project-level row. Business knowledge
   is repo-independent, and the cold-start case (#826) has no repo at all — the
   very axis ``ProjectKnowledge`` is keyed on.
2. **Wrong lifecycle.** ``knowledge_service.apply_build`` does
   ``row.knowledge = payload["knowledge"]``: every rebuild replaces the blob
   wholesale. Business knowledge must survive re-sync and carry per-item
   provenance and pinning (#827), which a wholesale-replaced blob cannot.
3. **Wrong failure surface.** ``ProjectKnowledge`` carries exactly one
   ``status`` and one ``last_error`` for the whole row. That cannot express
   "3 of 40 wiki pages failed", and per-source legibility is a requirement of
   the epic, not a nicety — hence ``status``/``last_error`` per
   :class:`BusinessSource`.

Ownership follows ADR 0009 §3 unchanged: a nullable ``owner_id`` (per-user
privacy) with the owner included in every uniqueness constraint, so the same
logical row can exist once per user and once in the admin-managed shared
namespace (``owner_id IS NULL``). ``owner_id`` here is **not** an org tier —
there is deliberately no ``scope`` enum, no org column and no sharing table. A
shared tier, if it arrives, arrives the ADR 0009 way (``owner_id IS NULL`` +
``require_admin`` + copy-on-clone), and nothing in this schema blocks it.

The project identity is ``project_guid`` (ADR 0013 / #585) — the identity that
survives a project rename.

This slice (#815) is schema only: no service, no router, no behaviour.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UTCDateTime, timestamp_column, utcnow

#: Where a source's bytes came from. ``"notion"`` is deliberately absent — it is
#: deferred to v2 (#832) because it needs a new credential kind.
BUSINESS_SOURCE_KINDS = ("upload", "url", "github_md", "ado_wiki")

#: Per-source ingestion state. One row failing leaves every other source usable,
#: which is the whole point of reason (3) above.
BUSINESS_SOURCE_STATUSES = ("pending", "syncing", "synced", "error")

#: The shape of a distilled fact. Chosen to match what a test case is written
#: from: what a word means, what must hold, how a task flows, who does it, what
#: limits it, and what "done" is allowed to look like.
BUSINESS_FACT_CATEGORIES = (
    "glossary",
    "rule",
    "flow",
    "actor",
    "constraint",
    "acceptance-norm",
)

#: ``"ingested"`` facts are derived from a source and are immutable; ``"manual"``
#: facts are human-authored. Corrections are new rows, never mutations of an
#: ingested row (epic #813: override is an overlay, never a mutation).
BUSINESS_FACT_ORIGINS = ("ingested", "manual")


class BusinessSource(Base):
    """One linked or uploaded document that grounds a project's test authoring.

    A snapshot, never a live mirror: ``content_hash`` + ``fetched_at`` pin the
    exact version a generated artifact is attributable to, and a changed
    upstream document surfaces as *stale* rather than silently shifting under an
    existing test case.
    """

    __tablename__ = "business_source"
    # ADR 0009 §3 — the owner is part of the key, so the same link can exist
    # once per user and once in the shared namespace.
    #
    # ``url`` is NULL for an upload (an uploaded file has no address), and both
    # SQLite and PostgreSQL treat NULLs as distinct in a unique index — so this
    # constraint de-duplicates *links* without ever rejecting a second upload.
    # The same NULL rule means the constraint is inert for a shared-namespace row
    # (``owner_id IS NULL``), exactly as ``uq_project_knowledge_key_owner``
    # already is; a shared-tier writer de-duplicates in the service layer.
    __table_args__ = (
        UniqueConstraint(
            "project_guid", "owner_id", "kind", "url", name="uq_business_source_project_kind_url"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    #: The owning project's GUID (ADR 0013 / #585) — survives a rename.
    project_guid: Mapped[str | None] = mapped_column(
        String(36), index=True, nullable=True, default=None
    )
    #: The project NAME, denormalized for display and for the #585 bridge, the
    #: same way ``ProjectKnowledge.project_key`` carries it.
    project_key: Mapped[str] = mapped_column(String(200), default="")
    #: Per-user ownership (#91, ADR 0009 §3). NULL = the shared namespace.
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)

    #: One of :data:`BUSINESS_SOURCE_KINDS`.
    kind: Mapped[str] = mapped_column(String(16), default="upload")
    #: Human label — the uploaded filename, or the page/document title.
    title: Mapped[str] = mapped_column(String(500), default="")
    #: Address for a link-backed source; NULL for an upload (see __table_args__).
    #: Bounded at 500 rather than the 1000 used for free text because it is part
    #: of a composite index, and PostgreSQL caps an index entry at ~2704 bytes.
    url: Mapped[str | None] = mapped_column(String(500), nullable=True, default=None)
    #: The provider connection whose credentials fetched this source, when one
    #: was needed (GitHub / ADO). Nullable: an upload or a public URL needs none.
    connection_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("provider_connections.id"), nullable=True
    )

    #: One of :data:`BUSINESS_SOURCE_STATUSES`. Per source, not per project.
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    #: Last ingestion error for *this* source; cleared on a successful sync.
    last_error: Mapped[str] = mapped_column(String(1000), default="")
    #: When the snapshot was taken. NULL until the first successful fetch.
    fetched_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: Hash of the normalized content — the staleness signal (#830).
    content_hash: Mapped[str] = mapped_column(String(64), default="")

    # ----------------------------------------------- Staleness probe (#830)
    # Snapshot-with-manual-resync is only defensible if a user can SEE that a
    # snapshot has gone stale, so the cheap per-adapter probe (a commit SHA, a
    # digest of wiki page versions, an ETag) is recorded here alongside the
    # snapshot it describes. Four columns rather than a single ``stale`` boolean
    # because the honest answer has three parts — what was true when we fetched,
    # when we last asked, and what the asking said — and a bare flag cannot tell
    # "upstream is unchanged" apart from "we have never looked", which is
    # exactly the claim this slice exists to stop the UI making.
    #: Upstream version identifier as it stood when the snapshot was taken.
    #: Empty means no probe could answer for this source — staleness for it is
    #: then time-based only, and must be *labelled* as time-based.
    upstream_rev: Mapped[str] = mapped_column(String(200), default="")
    #: When staleness was last checked. NULL = never asked, which is NOT the
    #: same as "not stale" and is rendered differently.
    probed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: Result of the last probe: the upstream revision has moved since
    #: ``upstream_rev``. Only ever set by a probe that actually answered.
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Why the last probe could not answer (no ETag on the page, a 403, a
    #: timeout). Non-empty means ``stale`` is meaningless and the UI falls back
    #: to the age label.
    probe_error: Mapped[str] = mapped_column(String(1000), default="")
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    #: How many documents this source expanded into (a wiki link fetches many).
    doc_count: Mapped[int] = mapped_column(Integer, default=0)

    #: Out of context, **not** deleted — the snapshot and its provenance survive
    #: so an artifact generated from it stays attributable.
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)

    #: Encrypted secret values for *this* source, in exactly the shape and with
    #: exactly the helpers ``ProviderConnection.secrets`` already uses
    #: (:mod:`app.crypto`) — one credential mechanism in the codebase, not two.
    #: It exists because a project's Azure DevOps connection frequently *cannot*
    #: supply one: a hub-backed connection holds no PAT and never will (#501),
    #: and a local one is scoped to work items rather than wikis. So a
    #: wiki-scoped token per source is the path that always works (#822). NULL /
    #: empty for every credential-free kind, and never serialized to a client.
    secrets: Mapped[dict] = mapped_column(JSON, default=dict)

    #: Workspace-relative paths under ``scoped_business_dir(owner_id)``: the raw
    #: bytes as fetched, and the normalized markdown derived from them.
    raw_path: Mapped[str] = mapped_column(String(600), default="")
    normalized_path: Mapped[str] = mapped_column(String(600), default="")

    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column(onupdate=utcnow)


class BusinessFact(Base):
    """A retrievable unit of domain knowledge, and the override overlay for it.

    One table serves both roles on purpose. An ingested fact and the human
    correction that beats it are the same shape; making the correction a
    separate row (``origin="manual"``, ``pinned=True``, the ingested row's id in
    ``superseded_by``) is what lets a re-sync rebuild ingested rows without ever
    destroying a correction — reason (2) in the module docstring.
    """

    __tablename__ = "business_fact"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_guid: Mapped[str | None] = mapped_column(
        String(36), index=True, nullable=True, default=None
    )
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)

    #: The source this fact was distilled from; NULL for a human-authored fact.
    #: ``ON DELETE SET NULL``, not CASCADE: deleting a source must not silently
    #: destroy a pinned human correction that references it. Reaping the
    #: *derived* rows is a service-layer decision (#827), where ``origin`` and
    #: ``pinned`` can be read, not a blind database cascade.
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("business_source.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: One of :data:`BUSINESS_FACT_CATEGORIES`.
    category: Mapped[str] = mapped_column(String(32), default="rule", index=True)
    #: The thing being defined or governed (the glossary term, the rule subject).
    term: Mapped[str] = mapped_column(String(300), default="")
    #: The fact itself, in one sentence — what goes into a prompt block.
    statement: Mapped[str] = mapped_column(Text, default="")
    #: Supporting detail, quoted or paraphrased from the source.
    detail: Mapped[str] = mapped_column(Text, default="")

    #: One of :data:`BUSINESS_FACT_ORIGINS`.
    origin: Mapped[str] = mapped_column(String(16), default="ingested")
    #: A human correction: beats everything and survives re-sync (#827).
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Out of context, not deleted — same contract as ``BusinessSource.excluded``.
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    #: The fact this one replaces, when it is a correction overlay.
    superseded_by: Mapped[int | None] = mapped_column(
        ForeignKey("business_fact.id", ondelete="SET NULL"), nullable=True
    )

    #: How many times a human has edited *this* row's content. Deliberately a
    #: counter and not a history table (#827): a diff/restore UI is a real
    #: feature with no stated demand, and this column is the hook to build one
    #: on if it ever arrives. Ingested rows stay at 1 — a re-sync refreshing a
    #: row is not a human revising it.
    revision: Mapped[int] = mapped_column(Integer, default=1)
    #: Who last edited it. Nullable because an ingested row has no human author
    #: and because the #91 ownership bridge admits an anonymous caller.
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, default=None
    )

    #: Denormalized text for keyword scoring. There is no vector store anywhere
    #: in this codebase — retrieval is keyword overlap
    #: (``prompts._rank_by_relevance``) — so the searchable projection is
    #: materialized on the row rather than recomputed per query.
    rank_text: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column(onupdate=utcnow)
