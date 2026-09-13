"""The source-agnostic half of Business Knowledge ingestion (#818).

An adapter's only job is ``fetch``. Everything after it is here and is the same
for every source, present and future::

    normalize to markdown -> hash -> persist raw + normalized -> mark synced

**Background work follows the existing convention** and adds nothing new: a
daemon :class:`threading.Thread` plus an in-process guard set, exactly mirroring
``knowledge_service.start_build`` / ``_building``. There is no job scheduler in
this codebase (ADR 0016 records this as a constraint, not an oversight) and this
slice does not introduce one.

**Failure is a first-class outcome here, not an exception path.** Three rules:

- A whole-source failure (``SourceFetchError``) marks the row ``error`` with the
  adapter's own words and leaves the previous snapshot on disk, so a failed
  re-sync never destroys the version an existing test case is attributed to.
- A document-level failure never disappears. If some documents of a multi-
  document source could not be read, the source is ``synced`` — the readable
  ones are genuinely usable — and carries an "N documents could not be read"
  detail. Partial success is a real state; a silent drop is not.
- A source that produced *no* readable document is ``error``, never ``synced``.
  This is what makes the SPA-shell check bite: an empty JavaScript shell answers
  ``200 OK``, and without this rule it would land as a synced source containing
  nothing at all.
"""

from __future__ import annotations

import threading

from loguru import logger

from app import db as db_module
from app.db import utcnow
from app.models.business import BusinessSource
from app.services.business_ingest import adapters, credentials, staleness, storage
from app.services.business_ingest.base import (
    BusinessIngestError,
    FetchedDoc,
    SourceCredential,
    SourceFetchError,
)
from app.services.business_ingest.normalize import normalize

__all__ = ["sync_source", "ingest_documents", "start_sync", "is_syncing"]

#: Source ids with an ingestion in flight in this process. Same shape and same
#: guarantees as ``knowledge_service._building``: it de-duplicates concurrent
#: syncs of one source within a process, and it is what a test waits on rather
#: than polling an endpoint.
_syncing: set[int] = set()

#: ``BusinessSource.last_error`` is ``String(1000)``.
_MAX_ERROR_CHARS = 1000


def is_syncing(source_id: int) -> bool:
    """Whether an ingestion for ``source_id`` is running in this process."""
    return source_id in _syncing


def _failure_detail(failures: list[tuple[str, str]], *, total: int) -> str:
    """User-facing summary of the documents that could not be read.

    A single-document source reports the underlying message verbatim — that is
    the one that carries the instruction (the SPA-shell text says to upload the
    content instead) and wrapping it in a count would bury it. A multi-document
    source leads with the count, because "3 of 40 pages failed" is the fact the
    reader needs first.

    :param failures: ``(document path, message)`` pairs.
    :param total: How many documents the source yielded in all.
    :returns: The string for ``BusinessSource.last_error``; ``""`` when nothing
        failed.
    """
    if not failures:
        return ""
    if total == 1:
        return failures[0][1][:_MAX_ERROR_CHARS]
    plural = "s" if len(failures) != 1 else ""
    listed = "; ".join(f"{path} — {message}" for path, message in failures)
    return f"{len(failures)} document{plural} could not be read: {listed}"[:_MAX_ERROR_CHARS]


def ingest_documents(
    db,
    source: BusinessSource,
    documents: list[FetchedDoc],
    credential: SourceCredential | None = None,
) -> BusinessSource:
    """Normalize, hash, persist and mark — the half every source shares.

    Separate from :func:`sync_source` because an upload has already "fetched"
    its bytes by the time it reaches the service (they arrived in the request),
    so it joins the pipeline here rather than through an adapter. Everything
    from this point on is identical for an upload and for a wiki.

    :param db: An open session; committed by this function.
    :param source: The row to update. Must already be persisted (it needs an
        ``id`` for its on-disk directory).
    :param documents: The adapter's output, including any doc carrying
        :attr:`~...base.FetchedDoc.error`.
    :param credential: The credential the fetch used, reused for the staleness
        probe that stamps ``upstream_rev`` (#830). ``None`` for a
        credential-free kind and for an upload, which has no probe at all.
    :returns: ``source``, updated and committed.
    """
    successes: list[tuple[FetchedDoc, str]] = []
    failures: list[tuple[str, str]] = []
    for doc in documents:
        if doc.failed:
            failures.append((doc.path, doc.error))
            continue
        try:
            successes.append((doc, normalize(doc)))
        except BusinessIngestError as exc:
            failures.append((doc.path, str(exc)))

    detail = _failure_detail(failures, total=len(documents))

    if not successes:
        source.status = "error"
        source.last_error = detail or "the source produced no documents"
        db.commit()
        return source

    snapshot = storage.persist_snapshot(
        project_key=source.project_key,
        source_id=source.id,
        owner_id=source.owner_id,
        documents=successes,
    )
    source.raw_path = snapshot.raw_path
    source.normalized_path = snapshot.normalized_path
    source.content_hash = snapshot.content_hash
    source.byte_size = snapshot.byte_size
    source.doc_count = snapshot.doc_count
    source.fetched_at = utcnow()
    source.status = "synced"
    # Record the upstream version marker for the snapshot we just took, so a
    # later probe compares like with like (#830). Best-effort inside: a source
    # whose probe cannot answer is still perfectly ingested, and the failure is
    # what makes the UI fall back to the honest age label.
    staleness.record_revision(source, credential)
    # Cleared on a fully successful sync (the model's contract); on a partial
    # one it carries the count, because a synced source that quietly lost three
    # pages is exactly the silent drop this slice exists to prevent.
    source.last_error = detail
    db.commit()
    return source


def sync_source(
    db, source: BusinessSource, credential: SourceCredential | None = None
) -> BusinessSource:
    """Run the full ingestion for one source, synchronously.

    :param db: An open session; committed by this function.
    :param source: The row to sync. Must already be persisted.
    :param credential: Resolved by the caller — the adapter never resolves its
        own secret. ``None`` for the credential-free sources.
    :returns: ``source``, updated and committed, with ``status`` either
        ``"synced"`` or ``"error"``. Never raises for an ingestion failure:
        the failure *is* the row's new state, which is the only form a user ever
        sees it in.
    """
    source.status = "syncing"
    source.last_error = ""
    db.commit()
    try:
        if credential is None:
            # Resolved here rather than by the caller, because the only caller
            # that *has* a request context (the sync endpoint) is owned by a
            # different slice. The adapter still never resolves its own secret
            # — the contract in `base` is intact — and a caller that already
            # holds one passes it and skips this entirely.
            credential = credentials.resolve_credential(db, source)
        documents = adapters.get_adapter(source.kind).fetch(source, credential)
    except SourceFetchError as exc:
        source.status = "error"
        source.last_error = str(exc)[:_MAX_ERROR_CHARS]
        db.commit()
        return source
    return ingest_documents(db, source, documents, credential)


def start_sync(source_id: int, credential: SourceCredential | None = None) -> bool:
    """Kick off an ingestion on a daemon thread (no-op if one is already running).

    Mirrors ``knowledge_service.start_build``: the caller has already set the
    row's status in its own transaction, the work runs off the request thread
    because a fetch can take up to 20 seconds per document, and the UI polls the
    row. The worker opens its **own** session — it must not borrow the request's.

    :param source_id: ``BusinessSource.id``.
    :param credential: Resolved before the thread starts, by the caller that has
        the request context to resolve it with.
    :returns: ``True`` if a thread was started, ``False`` if one was already in
        flight for this source.
    """
    if source_id in _syncing:
        return False
    _syncing.add(source_id)
    threading.Thread(target=_run_sync, args=(source_id, credential), daemon=True).start()
    return True


def _run_sync(source_id: int, credential: SourceCredential | None) -> None:
    """Thread body for :func:`start_sync`. Never propagates an exception."""
    db = db_module.SessionLocal()
    try:
        source = db.query(BusinessSource).filter(BusinessSource.id == source_id).first()
        if source is None:
            return
        try:
            sync_source(db, source, credential)
        except Exception as exc:  # noqa: BLE001 - surface on the row, don't kill the thread
            db.rollback()
            source = db.query(BusinessSource).filter(BusinessSource.id == source_id).first()
            if source is not None:
                source.status = "error"
                source.last_error = str(exc)[:_MAX_ERROR_CHARS]
                db.commit()
            logger.error("Business source sync failed for {}: {}", source_id, exc)
    finally:
        _syncing.discard(source_id)
        db.close()
