"""Generic URL source — one public web page, fetched with ``httpx`` (#818).

**Single page only. No crawling in v1.** A crawler is a different feature with
different failure modes (link budgets, robots, loops) and the corpus this
feature is for — a handful of rules pages — does not need one.

Every limit here exists because the remote side is untrusted: three redirects,
a 20-second timeout and 2 MB of body. And every rejection says what happened in
words a QC can act on, because the alternative — a source that reports
``error`` with a stack trace, or worse ``synced`` with nothing in it — is the
failure mode this slice exists to remove.
"""

from __future__ import annotations

import re

import httpx

from app.services.business_ingest.base import FetchedDoc, SourceCredential, SourceFetchError

__all__ = [
    "UrlAdapter",
    "probe_revision",
    "NO_VALIDATOR_MESSAGE",
    "MAX_DOC_BYTES",
    "MAX_REDIRECTS",
    "TIMEOUT_SECONDS",
]

#: Redirect budget. Enough for http->https plus a canonical-host hop; not
#: enough to be walked around a redirect loop.
MAX_REDIRECTS = 3
#: Whole-request timeout, seconds.
TIMEOUT_SECONDS = 20.0
#: Body cap. Enforced while streaming, so an oversized (or endless) response is
#: abandoned rather than buffered.
MAX_DOC_BYTES = 2 * 1024 * 1024

#: Why a URL probe legitimately cannot answer. A great many pages — anything
#: rendered per-request — send neither ``ETag`` nor ``Last-Modified``, and there
#: is no cheap way to know whether such a page has changed short of downloading
#: it again. Saying so is the whole point of #830: the alternative is a badge
#: that claims a change it cannot see, which is worse than admitting the gap.
NO_VALIDATOR_MESSAGE = (
    "this page sends no ETag or Last-Modified header, so it cannot report "
    "whether it has changed — re-sync it to compare the content itself"
)

_ACCEPTED_TYPES = ("text/html", "application/xhtml+xml", "text/markdown", "text/plain")
_USER_AGENT = "Q-Agent/1.0 (+business-knowledge-ingest)"
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _media_type(content_type: str) -> str:
    """The bare media type from a ``Content-Type`` header, lower-cased."""
    return (content_type or "").split(";")[0].strip().lower()


def _doc_filename(media_type: str) -> str:
    """Stable on-disk name for the single document a URL source yields."""
    if media_type == "text/markdown":
        return "index.md"
    if media_type == "text/plain":
        return "index.txt"
    return "index.html"


def _title_from(html: str, fallback: str) -> str:
    """Best-effort ``<title>`` text, used only as a display label.

    A regex is deliberate here: the title is a label, not content — content goes
    through the real HTML parser in
    :mod:`app.services.business_ingest.normalize` — and this must not fail or
    raise on a malformed head.

    :param html: The decoded page.
    :param fallback: Used when there is no usable title.
    :returns: A single-line title, at most 500 characters.
    """
    match = _TITLE_RE.search(html)
    if not match:
        return fallback
    text = _TAG_RE.sub("", match.group(1))
    return " ".join(text.split())[:500] or fallback


def probe_revision(source, credential: SourceCredential | None = None) -> str:
    """The page's HTTP validator — the cheap staleness probe for a URL (#830).

    ``ETag`` first, then ``Last-Modified``: both are exactly the value the
    server itself uses to answer "has this changed", so a comparison between two
    of them is the server's own verdict rather than our guess.

    A ``HEAD`` is tried first because it downloads nothing at all. Servers that
    refuse ``HEAD`` (405/501, and a surprising number answer 403) are retried
    with a ``GET`` whose body is never read — the connection is closed as soon
    as the headers are in, so the cost is the same in practice.

    :param source: The ``BusinessSource`` row; only ``url`` is read.
    :param credential: Ignored — a generic URL is credential-free.
    :returns: The validator, prefixed to say which one it is, so an ``ETag``
        that happens to equal a date string can never be mistaken for one.
    :raises SourceFetchError: when the page cannot be reached, **or** when it
        reports no validator at all (:data:`NO_VALIDATOR_MESSAGE`). Never a
        silent ``""``: an unknown revision compared against a stored one would
        read as "changed" every single time, which is the failure mode a
        staleness badge must not have.
    """
    url = (getattr(source, "url", "") or "").strip()
    if not url:
        raise SourceFetchError("this source has no URL to probe")
    if not url.lower().startswith(("http://", "https://")):
        raise SourceFetchError("only http:// and https:// addresses can be probed")

    try:
        with httpx.Client(
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            timeout=TIMEOUT_SECONDS,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            response = client.head(url)
            if response.status_code in (403, 405, 501) or response.status_code >= 500:
                with client.stream("GET", url) as streamed:
                    return _validator_of(streamed, url)
            return _validator_of(response, url)
    except httpx.TooManyRedirects as exc:
        raise SourceFetchError(f"the site redirected more than {MAX_REDIRECTS} times") from exc
    except httpx.TimeoutException as exc:
        raise SourceFetchError(
            f"the site did not respond within {int(TIMEOUT_SECONDS)} seconds"
        ) from exc
    except httpx.HTTPError as exc:
        raise SourceFetchError(f"could not reach the site ({exc})") from exc


def _validator_of(response: httpx.Response, url: str) -> str:
    """``ETag``/``Last-Modified`` from a response, or raise saying there is none.

    :param response: A response whose headers have arrived (the body may not
        have been read, and deliberately is not).
    :param url: Only for the error message.
    :returns: ``"etag:<value>"`` or ``"last-modified:<value>"``.
    :raises SourceFetchError: on a non-2xx status, or when neither header is
        present.
    """
    if not 200 <= response.status_code < 300:
        reason = (response.reason_phrase or "").strip()
        detail = f" ({reason})" if reason else ""
        raise SourceFetchError(f"the site returned {response.status_code}{detail}")
    etag = (response.headers.get("etag") or "").strip()
    if etag:
        return f"etag:{etag}"[:200]
    modified = (response.headers.get("last-modified") or "").strip()
    if modified:
        return f"last-modified:{modified}"[:200]
    raise SourceFetchError(NO_VALIDATOR_MESSAGE)


class UrlAdapter:
    """Fetch one page from a public URL. No credential, no crawling."""

    kind = "url"

    def probe_revision(self, source, credential: SourceCredential | None = None) -> str:
        """The page's ``ETag``/``Last-Modified`` — see :func:`probe_revision`."""
        return probe_revision(source, credential)

    def fetch(self, source, credential: SourceCredential | None = None) -> list[FetchedDoc]:
        """Fetch ``source.url`` as a single document.

        :param source: The ``BusinessSource`` row; only ``url`` and ``title``
            are read.
        :param credential: Ignored — a generic URL is credential-free by
            definition. A page that needs a login returns its login shell, which
            the normalizer rejects with an instruction to upload instead.
        :returns: A one-element list.
        :raises SourceFetchError: for a missing/unsupported URL, a non-2xx
            status, a non-document content type, an oversized body, too many
            redirects, a timeout, or an unreachable host.
        """
        url = (source.url or "").strip()
        if not url:
            raise SourceFetchError("this source has no URL to fetch")
        if not url.lower().startswith(("http://", "https://")):
            raise SourceFetchError("only http:// and https:// addresses can be fetched")

        try:
            with httpx.Client(
                follow_redirects=True,
                max_redirects=MAX_REDIRECTS,
                timeout=TIMEOUT_SECONDS,
                headers={"User-Agent": _USER_AGENT},
            ) as client:
                with client.stream("GET", url) as response:
                    if not 200 <= response.status_code < 300:
                        reason = (response.reason_phrase or "").strip()
                        detail = f" ({reason})" if reason else ""
                        raise SourceFetchError(f"the site returned {response.status_code}{detail}")

                    content_type = response.headers.get("content-type", "")
                    media_type = _media_type(content_type)
                    if media_type not in _ACCEPTED_TYPES:
                        raise SourceFetchError(
                            f"the URL returned {media_type or 'an unknown content type'}, "
                            "not a readable page — link to a web page, or upload the "
                            "document as a file"
                        )

                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > MAX_DOC_BYTES:
                            raise SourceFetchError(
                                "the page is larger than 2 MB — link to a specific page "
                                "rather than a whole export"
                            )
                        chunks.append(chunk)
        except httpx.TooManyRedirects as exc:
            raise SourceFetchError(f"the site redirected more than {MAX_REDIRECTS} times") from exc
        except httpx.TimeoutException as exc:
            raise SourceFetchError(
                f"the site did not respond within {int(TIMEOUT_SECONDS)} seconds"
            ) from exc
        except httpx.HTTPError as exc:
            raise SourceFetchError(f"could not reach the site ({exc})") from exc

        raw_bytes = b"".join(chunks)
        title = source.title or url
        if media_type in ("text/html", "application/xhtml+xml"):
            title = _title_from(raw_bytes.decode("utf-8", errors="replace"), title)

        return [
            FetchedDoc(
                path=_doc_filename(media_type),
                title=title,
                raw_bytes=raw_bytes,
                content_type=content_type,
            )
        ]
