"""Uploaded-file source (#818).

An upload has no address, so there is nothing to re-fetch: re-sync is not
"unsupported yet", it is **meaningless**. Re-uploading replaces the snapshot and
produces a new hash, which is the same staleness signal a link gets.

The adapter exists anyway so that ``kind="upload"`` resolves through the same
registry as every other source and no caller has to special-case it — it
answers with a legible refusal instead of a ``KeyError``. The real entry point
for this kind is :func:`app.services.business_ingest.uploads.ingest_upload`.
"""

from __future__ import annotations

from app.services.business_ingest.base import FetchedDoc, SourceCredential, SourceFetchError

__all__ = ["UploadAdapter"]


class UploadAdapter:
    """Resolvable, and deliberately un-fetchable."""

    kind = "upload"

    def fetch(self, source, credential: SourceCredential | None = None) -> list[FetchedDoc]:
        """Always refuse: an uploaded file cannot be re-fetched.

        :param source: Unused.
        :param credential: Unused.
        :raises SourceFetchError: always, naming the action that does work.
        """
        raise SourceFetchError(
            "an uploaded file has no address to re-sync from — upload the new "
            "version to replace it"
        )
