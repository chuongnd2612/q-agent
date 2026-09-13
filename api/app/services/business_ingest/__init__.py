"""Business Knowledge ingestion (#818, epic #813, ADR 0016).

Turns a link or an uploaded file into a normalized, hashed, on-disk snapshot
that test-case authoring can be grounded in and attributed to.

The package is split along one seam, and only one::

    adapters/<kind>.py   fetch(source, credential) -> list[FetchedDoc]
    normalize.py         raw bytes -> markdown  (+ the SPA-shell check)
    storage.py           persist raw AND normalized; hash the normalized text
    pipeline.py          orchestration, the background thread, the row's status
    uploads.py           the upload entry point (no fetch: the bytes arrived)
    distil.py            corpus -> the brief + the facts (the step after ingest)

A new source is therefore **one new module** plus one line in the adapter
registry: #821 (GitHub markdown) and #822 (Azure DevOps wiki) are written
against this, and Notion (v2, #832) will be.

Two decisions worth knowing before reading further:

- **Raw bytes are kept alongside the normalized markdown.** Re-normalizing must
  never require re-fetching — which, for the credentialed sources, means
  re-authenticating — and keeping the raw bytes is what makes a later parser
  improvement retroactive over everything already ingested.
- **HTML is converted with ``markdownify``** rather than a hand-rolled
  ``html.parser`` stripper. It is pure-Python and its parsing is BeautifulSoup's,
  which matters precisely here: the pages this ingests are real-world and often
  malformed, and the SPA-shell check is a *measurement of extracted text* — a
  stripper that mis-parses a broken page would make that measurement, and
  therefore the check, unreliable. A ~120-line stripper would also be a new
  parser to maintain in a codebase whose standing rule is to reuse rather than
  re-implement.
"""

from __future__ import annotations

from app.services.business_ingest.adapters import get_adapter, register, registered_kinds
from app.services.business_ingest.base import (
    BusinessIngestError,
    FetchedDoc,
    SourceAdapter,
    SourceCredential,
    SourceFetchError,
    UnreadableContentError,
)
from app.services.business_ingest.distil import (
    BRIEF_CHAR_BUDGET,
    BRIEF_STATUSES,
    BRIEF_TOKEN_BUDGET,
    CorpusDocument,
    build_distillation,
    collect_corpus,
    corpus_hash,
    distil_project,
    is_distilling,
    merge_facts,
    start_distil,
)
from app.services.business_ingest.normalize import (
    MIN_READABLE_CHARS,
    SPA_SHELL_MESSAGE,
    normalize,
    readable_length,
)
from app.services.business_ingest.pipeline import (
    ingest_documents,
    is_syncing,
    start_sync,
    sync_source,
)
from app.services.business_ingest.storage import content_hash_for, source_root
from app.services.business_ingest.uploads import (
    ALLOWED_EXTENSIONS,
    MAX_FILES_PER_PROJECT,
    MAX_UPLOAD_BYTES,
    UploadRejectedError,
    ingest_upload,
    validate_upload,
)

__all__ = [
    # contract
    "FetchedDoc",
    "SourceAdapter",
    "SourceCredential",
    "get_adapter",
    "register",
    "registered_kinds",
    # errors
    "BusinessIngestError",
    "SourceFetchError",
    "UnreadableContentError",
    "UploadRejectedError",
    # pipeline
    "sync_source",
    "ingest_documents",
    "start_sync",
    "is_syncing",
    # distillation
    "distil_project",
    "start_distil",
    "is_distilling",
    "collect_corpus",
    "corpus_hash",
    "build_distillation",
    "merge_facts",
    "CorpusDocument",
    "BRIEF_STATUSES",
    "BRIEF_TOKEN_BUDGET",
    "BRIEF_CHAR_BUDGET",
    # normalization
    "normalize",
    "readable_length",
    "MIN_READABLE_CHARS",
    "SPA_SHELL_MESSAGE",
    # storage
    "content_hash_for",
    "source_root",
    # uploads
    "ingest_upload",
    "validate_upload",
    "ALLOWED_EXTENSIONS",
    "MAX_UPLOAD_BYTES",
    "MAX_FILES_PER_PROJECT",
]
