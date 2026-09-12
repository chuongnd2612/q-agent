"""Registry of Business Knowledge source adapters, keyed by ``BusinessSource.kind``.

Mirrors :mod:`app.services.adapters` (the provider adapters) so there is one
registration idiom in the codebase rather than two. Adding a source is adding a
module here plus one line in :func:`_load_builtin` — #821 (``github_md``) and
#822 (``ado_wiki``) land exactly that way, and nothing outside this package
changes.
"""

from __future__ import annotations

from app.services.business_ingest.base import SourceAdapter, SourceFetchError

__all__ = ["get_adapter", "register", "registered_kinds"]

_REGISTRY: dict[str, SourceAdapter] = {}


def register(adapter: SourceAdapter) -> None:
    """Register ``adapter`` under its own ``kind``.

    :param adapter: Any object satisfying
        :class:`~app.services.business_ingest.base.SourceAdapter`.
    """
    _REGISTRY[adapter.kind] = adapter


def _load_builtin() -> None:
    """Instantiate the built-in adapters. Lazy, to avoid import cycles."""
    from app.services.business_ingest.adapters.upload import UploadAdapter
    from app.services.business_ingest.adapters.url import UrlAdapter

    for adapter in (UploadAdapter(), UrlAdapter()):
        register(adapter)


def get_adapter(kind: str) -> SourceAdapter:
    """Resolve the adapter for a source ``kind``.

    :param kind: One of ``app.models.business.BUSINESS_SOURCE_KINDS``.
    :returns: The registered adapter instance.
    :raises SourceFetchError: when no adapter is registered — a source kind the
        deployment cannot ingest is a legible error on the row, not a crash in
        the worker thread.
    """
    if not _REGISTRY:
        _load_builtin()
    adapter = _REGISTRY.get(kind)
    if adapter is None:
        raise SourceFetchError(f"'{kind}' sources cannot be ingested by this version")
    return adapter


def registered_kinds() -> tuple[str, ...]:
    """The source kinds this deployment can ingest, sorted."""
    if not _REGISTRY:
        _load_builtin()
    return tuple(sorted(_REGISTRY))
