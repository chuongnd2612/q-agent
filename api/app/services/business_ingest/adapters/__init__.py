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
#: Whether :func:`_load_builtin` has run. A separate flag rather than
#: ``if not _REGISTRY``, because "the registry is empty" and "the builtins have
#: not been loaded" are not the same statement: anything that puts an entry in
#: first — a test substituting one kind, a future plugin registering its own —
#: would otherwise suppress the built-in load entirely and make *every other*
#: kind resolve to "cannot be ingested by this version".
_loaded = False


def register(adapter: SourceAdapter) -> None:
    """Register ``adapter`` under its own ``kind``.

    :param adapter: Any object satisfying
        :class:`~app.services.business_ingest.base.SourceAdapter`.
    """
    _REGISTRY[adapter.kind] = adapter


def _load_builtin() -> None:
    """Instantiate the built-in adapters once. Lazy, to avoid import cycles.

    Idempotent, and it never overwrites an entry already registered under the
    same kind — so a deliberate substitution stays substituted.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True

    from app.services.business_ingest.adapters.ado_wiki import AdoWikiAdapter
    from app.services.business_ingest.adapters.github_md import GitHubMarkdownAdapter
    from app.services.business_ingest.adapters.upload import UploadAdapter
    from app.services.business_ingest.adapters.url import UrlAdapter

    for adapter in (UploadAdapter(), UrlAdapter(), GitHubMarkdownAdapter(), AdoWikiAdapter()):
        _REGISTRY.setdefault(adapter.kind, adapter)


def get_adapter(kind: str) -> SourceAdapter:
    """Resolve the adapter for a source ``kind``.

    :param kind: One of ``app.models.business.BUSINESS_SOURCE_KINDS``.
    :returns: The registered adapter instance.
    :raises SourceFetchError: when no adapter is registered — a source kind the
        deployment cannot ingest is a legible error on the row, not a crash in
        the worker thread.
    """
    _load_builtin()
    adapter = _REGISTRY.get(kind)
    if adapter is None:
        raise SourceFetchError(f"'{kind}' sources cannot be ingested by this version")
    return adapter


def registered_kinds() -> tuple[str, ...]:
    """The source kinds this deployment can ingest, sorted."""
    _load_builtin()
    return tuple(sorted(_REGISTRY))
