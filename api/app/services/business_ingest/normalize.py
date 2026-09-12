"""Raw bytes -> markdown, source-agnostically (#818).

Every adapter's output funnels through :func:`normalize`, so a parser
improvement here reaches every source at once — and, because the pipeline keeps
the raw bytes, reaches everything already ingested on the next re-normalize
without re-fetching or re-authenticating.

HTML is converted with **markdownify**. See the module-level note in
:mod:`app.services.business_ingest` for why a dependency rather than a
hand-rolled stripper.

The check that earns this module its own file is :func:`_assert_readable` — the
**SPA trap**. A JavaScript-rendered page answers ``200 OK`` with an empty
``<div id="root"></div>`` shell. Ingested naively it produces a source that
reports ``synced``, carries a content hash, and contains nothing whatsoever —
the worst possible failure, because it is invisible until a generated test case
is inexplicably ungrounded. So the normalized text length is asserted
explicitly and the source fails with an instruction the user can act on.
"""

from __future__ import annotations

import re

from markdownify import MarkdownConverter

from app.services.business_ingest.base import FetchedDoc, UnreadableContentError

__all__ = ["normalize", "readable_length", "MIN_READABLE_CHARS", "SPA_SHELL_MESSAGE"]

#: Below this many readable characters an HTML page is treated as an empty
#: shell rather than a document. Calibrated to be well under any real wiki page
#: (a one-paragraph page clears it comfortably) and well over what a SPA shell,
#: a cookie banner or a bare ``<noscript>`` notice produces.
MIN_READABLE_CHARS = 200

#: Verbatim user-facing text for the SPA trap. A constant because the test
#: asserts on it and because the *instruction* ("try uploading the content
#: instead") is the part that makes the failure recoverable.
SPA_SHELL_MESSAGE = (
    "we fetched the page but found almost no readable text — it may need "
    "JavaScript or a login; try uploading the content instead."
)

#: Media types normalized by passing the text through unchanged.
_TEXT_TYPES = ("text/markdown", "text/x-markdown", "text/plain")
#: Media types normalized through the HTML converter.
_HTML_TYPES = ("text/html", "application/xhtml+xml")

#: Markdown syntax that carries no reading content. Stripped before measuring
#: length so that a shell whose only output is ``* * *`` or a nav list of bare
#: links cannot buy its way past :data:`MIN_READABLE_CHARS`.
_LINK_TARGET_RE = re.compile(r"\]\([^)]*\)")
_MD_SYNTAX_RE = re.compile(r"[#*_>`~\[\]|\-]+")
_WHITESPACE_RE = re.compile(r"\s+")


class _DocumentConverter(MarkdownConverter):
    """markdownify, minus the parts of a page that are not the document.

    ``<title>`` is metadata, not body text: markdownify emits it inline by
    default, which would let a SPA shell's one-word title count as readable
    content — precisely the measurement the SPA check depends on. ``<script>``
    and ``<style>`` are already dropped by markdownify itself.
    """

    def convert_title(self, el, text, parent_tags=None):  # noqa: ANN001, ARG002
        return ""


def _charset_of(content_type: str) -> str:
    """Extract the ``charset`` parameter from a media type, defaulting to UTF-8."""
    match = re.search(r"charset=([\w.-]+)", content_type or "", re.IGNORECASE)
    return match.group(1) if match else "utf-8"


def _media_type_of(content_type: str) -> str:
    """The bare media type from a ``Content-Type`` header, lower-cased."""
    return (content_type or "").split(";")[0].strip().lower()


def _decode(raw_bytes: bytes, content_type: str) -> str:
    """Decode ``raw_bytes`` using the declared charset, never raising.

    A wrong-but-declared charset is a far smaller problem than a failed
    ingestion, so an undecodable byte becomes U+FFFD rather than an exception.
    """
    try:
        return raw_bytes.decode(_charset_of(content_type), errors="replace")
    except LookupError:
        return raw_bytes.decode("utf-8", errors="replace")


def readable_length(markdown: str) -> int:
    """Number of reading characters in ``markdown``, ignoring syntax and links.

    Link *targets* are removed but link *text* is kept, so a page of prose with
    citations scores on its prose. Used only as the SPA-shell measurement; it is
    not a quality score.

    :param markdown: Normalized markdown.
    :returns: Character count after stripping markdown syntax, link targets and
        collapsing whitespace.
    """
    text = _LINK_TARGET_RE.sub("", markdown)
    text = _MD_SYNTAX_RE.sub(" ", text)
    return len(_WHITESPACE_RE.sub(" ", text).strip())


def _assert_readable(markdown: str, *, media_type: str) -> None:
    """Raise when the normalized text is too thin to be a document.

    :raises UnreadableContentError: with :data:`SPA_SHELL_MESSAGE` for an HTML
        page that rendered to almost nothing (the SPA trap), or a plain "the
        document is empty" for a text document with no content at all.
    """
    length = readable_length(markdown)
    if media_type in _HTML_TYPES:
        if length < MIN_READABLE_CHARS:
            raise UnreadableContentError(SPA_SHELL_MESSAGE)
        return
    if length == 0:
        raise UnreadableContentError("the document is empty")


def normalize(doc: FetchedDoc) -> str:
    """Normalize one fetched document to markdown.

    :param doc: The document as fetched. ``content_type`` decides the strategy;
        when it is empty the ``path`` extension is consulted, so an adapter that
        only knows a filename still gets the right treatment.
    :returns: Markdown text.
    :raises UnreadableContentError: when the media type is not a readable
        document (a PDF, an image, a zip), or when the result is an empty shell.
    """
    media_type = _media_type_of(doc.content_type)
    if not media_type:
        lowered = doc.path.lower()
        if lowered.endswith(".md"):
            media_type = "text/markdown"
        elif lowered.endswith(".txt"):
            media_type = "text/plain"
        elif lowered.endswith((".html", ".htm")):
            media_type = "text/html"

    if media_type in _TEXT_TYPES:
        markdown = _decode(doc.raw_bytes, doc.content_type).strip()
    elif media_type in _HTML_TYPES:
        html = _decode(doc.raw_bytes, doc.content_type)
        markdown = _DocumentConverter(heading_style="ATX").convert(html).strip()
    else:
        raise UnreadableContentError(
            f"that is a {media_type or 'binary'} document, not a readable page — "
            "only HTML, markdown and plain text can be ingested in this version"
        )

    _assert_readable(markdown, media_type=media_type)
    return markdown
