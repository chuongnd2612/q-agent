"""The adapter contract every Business Knowledge source implements (#818).

One contract, deliberately narrow, so that adding a source is adding **one
module** and changing nothing else. #821 (GitHub markdown) and #822 (Azure
DevOps wiki) are written against exactly this, and Notion (v2, #832) will be
too::

    fetch(source, credential) -> list[FetchedDoc]

Everything after ``fetch`` is source-agnostic and lives in
:mod:`app.services.business_ingest.pipeline`: normalize to markdown, hash,
persist **raw + normalized**, mark ``synced``. An adapter therefore never
touches the database, never writes to the workspace and never decides a
source's status — it turns an address plus a credential into bytes, or raises.

Why ``raw_bytes`` and not text: re-normalizing must never require re-fetching
(and re-authenticating), and keeping the bytes is what makes a later parser
improvement retroactive over everything already ingested (ADR 0016 / epic #813).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

__all__ = [
    "FetchedDoc",
    "SourceAdapter",
    "SourceCredential",
    "BusinessIngestError",
    "SourceFetchError",
    "UnreadableContentError",
]


class BusinessIngestError(Exception):
    """Base for every ingestion failure whose message is shown to a user.

    Messages are written to be read by a QC, not a developer: they say what the
    remote side did ("the site returned 403") and, where there is one, what to
    do instead. They land verbatim in ``BusinessSource.last_error``.
    """


class SourceFetchError(BusinessIngestError):
    """The **whole** source could not be fetched — nothing was ingested.

    Raised by :meth:`SourceAdapter.fetch`. The pipeline marks the source
    ``error`` and leaves any previous snapshot on disk untouched, so a failed
    re-sync never destroys the version an existing test case is attributed to.
    """


class UnreadableContentError(BusinessIngestError):
    """One document's bytes could not be turned into readable markdown.

    Doc-level, not source-level: in a multi-document source the others still
    land and the source ends ``synced`` with an "N documents could not be read"
    detail. This is the class the SPA-shell check raises.
    """


@dataclass(frozen=True)
class SourceCredential:
    """The secret an adapter needs, resolved by the caller — never by the adapter.

    The two credential-free sources in this slice (upload, generic URL) are
    passed ``None``. #821/#822 populate ``token`` from the source's
    ``connection_id`` (or a per-source PAT) and may use ``extra`` for
    provider-specific identifiers (an organisation, a wiki id). Keeping
    resolution out of the adapter is what lets the same adapter be exercised in
    a test with a literal token.

    :param token: The bearer / PAT value.
    :param extra: Provider-specific non-secret parameters.
    """

    token: str = ""
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class FetchedDoc:
    """One document as it came off the wire, before any normalization.

    :param path: Stable identity of this document *within* its source, used as
        the on-disk filename under both ``raw/`` and ``normalized/``. Must be a
        relative POSIX path with no ``..`` segment. A single-document source
        uses one fixed name (``"index.html"``); a wiki uses its page path, so
        the same page keeps the same file across re-syncs.
    :param title: Human label for the document (the ``<title>``, the page name,
        the uploaded filename).
    :param raw_bytes: Exactly what was fetched. Empty when ``error`` is set.
    :param content_type: The declared media type, e.g.
        ``"text/html; charset=utf-8"``. Drives normalization; an adapter that
        knows the type only from the file extension should still say so
        (``"text/markdown"``).
    :param upstream_rev: The upstream version identifier (a commit sha, a wiki
        page version) when the source has one, else ``""``. Not the content
        hash — that is computed by the pipeline from the *normalized* text.
    :param error: Set when the adapter could not read **this** document. The
        doc is still returned, never dropped: the pipeline counts it into the
        "N documents could not be read" detail. Partial success is a real
        state, and a silently missing page is the failure this field exists to
        make impossible.
    """

    path: str
    title: str = ""
    raw_bytes: bytes = b""
    content_type: str = ""
    upstream_rev: str = ""
    error: str = ""

    @property
    def failed(self) -> bool:
        """True when the adapter could not read this document."""
        return bool(self.error)


class SourceAdapter(Protocol):
    """Turn a :class:`~app.models.business.BusinessSource` into its documents.

    Implementations are stateless and registered by ``kind`` in
    :mod:`app.services.business_ingest.adapters`.
    """

    #: The ``BusinessSource.kind`` this adapter serves.
    kind: str

    def fetch(self, source, credential: SourceCredential | None) -> list[FetchedDoc]:
        """Fetch every document belonging to ``source``.

        :param source: The ``BusinessSource`` row. Adapters read ``url``,
            ``title`` and ``kind`` only — they must not mutate it.
        :param credential: The resolved secret, or ``None`` for a
            credential-free source.
        :returns: One :class:`FetchedDoc` per document, including one with
            ``error`` set for each document that could not be read.
        :raises SourceFetchError: when the source as a whole failed, so that
            nothing at all can be ingested.
        """
        ...
