"""Azure DevOps project wiki — pages out of ``/_apis/wiki`` (#822).

A wiki is the one Business Knowledge source that is *already* the right shape:
its pages are markdown, written by the people who own the rules, and organised
as a tree. So the adapter's real work is not parsing — it is telling four
indistinguishable-looking 4xx responses apart and saying which one happened.

**Why that is the deliverable.** Every failure below answers 4xx and, told
generically, sends the user to the wrong repair:

============================  ==========================================
what actually happened        what a generic message would make them do
============================  ==========================================
the PAT lacks ``vso.wiki``    re-do a connection that is perfectly fine
the org/project is wrong      re-issue a token that is perfectly fine
the project has no wiki       hunt for a permissions problem that does
                              not exist
Azure DevOps rate-limited     conclude the feature is broken
============================  ==========================================

So each gets its own constant, each constant is asserted verbatim in
``tests/test_business_ingest_ado.py``, and none of them is reachable from
another's branch.

**Preflight is a separate, cheap call.** ``GET /{project}/_apis/wiki/wikis``
lists the project's wikis and is the request that distinguishes "no wiki here"
(404 / empty list) from "this token cannot read wikis" (401/403) — before a
single page is fetched, and, through
:mod:`app.routers.business_ado`, before the source is even saved.

**Limits**, both pinned as literals in the tests rather than read back from
here: :data:`MAX_DEPTH` = 4 levels below the root, :data:`MAX_PAGES` = 200
pages. Exceeding the page cap is **not** a silent truncation — it lands as a
readable "only the first 200 pages were ingested" item in the source's partial-
failure detail, because a wiki that quietly lost half of itself is exactly the
failure this epic exists to prevent.

A token never reaches a log line, an exception message or a stored artifact: it
is used to build one ``Authorization`` header and is never interpolated into any
string that leaves this module.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx

from app.services.business_ingest.base import FetchedDoc, SourceCredential, SourceFetchError
from app.services.business_ingest.credentials import NO_CREDENTIAL_MESSAGE

__all__ = [
    "AdoWikiAdapter",
    "WikiTarget",
    "API_VERSION",
    "MAX_DEPTH",
    "MAX_PAGES",
    "TIMEOUT_SECONDS",
    "BAD_TOKEN_MESSAGE",
    "WIKI_SCOPE_MESSAGE",
    "NO_WIKI_MESSAGE",
    "RATE_LIMITED_MESSAGE",
    "list_wikis",
    "parse_wiki_url",
    "preflight",
]

#: Matches ``app.services.adapters.azure_devops.API_VERSION`` — one Azure DevOps
#: REST version across the codebase, not a second one to keep in step.
API_VERSION = "7.1"

#: Levels of sub-pages below the wiki root that are ingested. A project wiki
#: nests shallowly in practice; the cap is here so a pathological tree cannot
#: turn one sync into thousands of requests.
MAX_DEPTH = 4

#: Hard ceiling on pages per source. Reaching it is reported, never silent.
MAX_PAGES = 200

#: Whole-request timeout, seconds. Matches the generic URL adapter's budget.
TIMEOUT_SECONDS = 20.0

#: An invalid or expired PAT. Azure DevOps famously answers this with ``203``
#: and an HTML sign-in page rather than a 401, which is why
#: :func:`_decode_json` treats a non-JSON body as an auth failure and not as a
#: parse bug.
BAD_TOKEN_MESSAGE = (
    "Azure DevOps rejected the access token — it is invalid or has expired. "
    "Issue a new personal access token with Wiki (Read) scope."
)

#: The 401/403 case, and the single most important string in the slice: the
#: token works, the connection is fine, only the *scope* is wrong.
WIKI_SCOPE_MESSAGE = (
    "Your Azure DevOps token can read work items but not wikis. Re-issue it "
    "with Wiki (Read) scope."
)

#: The 404-on-preflight case. Covers both "wikis are not enabled here" and an
#: organisation/project mismatch, because Azure DevOps answers identically for
#: the two and guessing between them would be worse than naming both.
NO_WIKI_MESSAGE = (
    "Azure DevOps has no wiki for project '{project}' — either the project has "
    "no wiki enabled, or the organisation/project in the URL is wrong."
)

RATE_LIMITED_MESSAGE = (
    "Azure DevOps rate-limited this request. Wait a few minutes and sync again."
)

_PAGE_CAP_MESSAGE = (
    "this wiki has more than {cap} pages — only the first {cap} were ingested; "
    "link a sub-tree instead of the whole wiki"
)

_DEPTH_CAP_MESSAGE = (
    "this wiki nests deeper than {cap} levels — pages below that were not "
    "ingested; link the deeper sections as their own sources"
)


@dataclass(frozen=True)
class WikiTarget:
    """The three things an ADO wiki address actually names.

    :param org_url: Everything up to and including the organisation/collection
        segment, e.g. ``https://dev.azure.com/acme``. Used as the client's base
        URL, exactly as ``AzureDevOpsAdapter`` uses its ``baseUrl``.
    :param project: The Azure DevOps project, decoded (``My%20Project`` ->
        ``My Project``).
    :param wiki: The wiki identifier from the URL, or ``""`` to mean "the
        project's only/first wiki".
    :param page_path: Root of the sub-tree to ingest; ``"/"`` for the whole
        wiki. Taken from the ``pagePath`` query parameter, which is what an
        Azure DevOps deep link carries.
    """

    org_url: str
    project: str
    wiki: str = ""
    page_path: str = "/"


def parse_wiki_url(url: str) -> WikiTarget:
    """Split an Azure DevOps wiki address into its organisation/project/wiki.

    Handles every shape a user can copy out of the browser, without a per-host
    special case: ``dev.azure.com/{org}/{project}/_wiki/wikis/{wiki}/...``,
    ``{org}.visualstudio.com/{project}/_wiki/...``, an on-premises collection
    URL, and a bare project URL with no ``_wiki`` segment at all. The rule is
    positional rather than host-based — the project is the segment *before*
    ``_wiki`` and the organisation is everything before that — which is why all
    four fall out of the same six lines.

    :param url: ``BusinessSource.url``.
    :returns: The parsed :class:`WikiTarget`.
    :raises SourceFetchError: when the address is missing, is not http(s), or
        names no project.
    """
    raw = (url or "").strip()
    if not raw:
        raise SourceFetchError("this source has no Azure DevOps wiki URL to fetch")
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise SourceFetchError(
            "an Azure DevOps wiki URL must start with https:// and name a host"
        )

    segments = [segment for segment in parsed.path.split("/") if segment]
    root = f"{parsed.scheme}://{parsed.netloc}"

    wiki = ""
    if "_wiki" in segments:
        index = segments.index("_wiki")
        if index == 0:
            raise SourceFetchError(
                "that Azure DevOps URL names no project — copy the address of a "
                "wiki page from the project you want to ingest"
            )
        project_segments, tail = segments[:index], segments[index + 1 :]
        if len(tail) >= 2 and tail[0] == "wikis":
            wiki = unquote(tail[1])
    else:
        project_segments = segments

    if not project_segments:
        raise SourceFetchError(
            "that Azure DevOps URL names no project — copy the address of a wiki "
            "page from the project you want to ingest"
        )

    project = unquote(project_segments[-1])
    org_url = "/".join([root, *project_segments[:-1]]).rstrip("/")

    page_path = parse_qs(parsed.query).get("pagePath", ["/"])[0] or "/"
    if not page_path.startswith("/"):
        page_path = "/" + page_path

    return WikiTarget(org_url=org_url, project=project, wiki=wiki, page_path=page_path)


def _client(org_url: str, token: str) -> httpx.Client:
    """An httpx client authenticated for ``org_url`` with a PAT.

    Basic auth with an empty username is Azure DevOps' documented PAT scheme and
    is what ``AzureDevOpsAdapter._client`` already does; the header is the only
    place the token appears.
    """
    encoded = base64.b64encode(f":{token}".encode("utf-8")).decode("utf-8")
    return httpx.Client(
        base_url=org_url,
        headers={"Authorization": f"Basic {encoded}", "Accept": "application/json"},
        timeout=TIMEOUT_SECONDS,
    )


def _decode_json(response: httpx.Response) -> Any:
    """The response body as JSON, or the bad-token refusal.

    Azure DevOps answers an unauthenticated *browser-ish* request with ``200``
    or ``203`` and the HTML sign-in page. Parsed naively that is a JSON error
    deep inside the walk; read here it is what it actually is — a rejected
    token — and it gets :data:`BAD_TOKEN_MESSAGE`.
    """
    media_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    if media_type != "application/json":
        raise SourceFetchError(BAD_TOKEN_MESSAGE)
    try:
        return response.json()
    except ValueError as exc:  # pragma: no cover - defensive
        raise SourceFetchError(BAD_TOKEN_MESSAGE) from exc


def _check(response: httpx.Response, *, project: str, missing: str) -> None:
    """Turn a non-2xx Azure DevOps response into the message for *that* failure.

    :param response: The response to classify.
    :param project: Named in the 404 message so the user can see which project
        was actually asked for.
    :param missing: The 404 text — different for "the project has no wiki" and
        "that page path does not exist", which is the whole reason it is a
        parameter.
    :raises SourceFetchError: for every non-2xx status.
    """
    status = response.status_code
    if 200 <= status < 300:
        if status == 203:
            # Not an error status, but Azure DevOps' way of saying "signed out".
            raise SourceFetchError(BAD_TOKEN_MESSAGE)
        return
    if status in (401, 403):
        raise SourceFetchError(WIKI_SCOPE_MESSAGE)
    if status == 404:
        raise SourceFetchError(missing)
    if status == 429:
        raise SourceFetchError(RATE_LIMITED_MESSAGE)
    reason = (response.reason_phrase or "").strip()
    detail = f" ({reason})" if reason else ""
    raise SourceFetchError(f"Azure DevOps returned {status}{detail}")


def list_wikis(client: httpx.Client, project: str) -> list[dict[str, Any]]:
    """Every wiki in ``project``.

    :raises SourceFetchError: with the scope / no-wiki / rate-limit message, as
        classified by :func:`_check`.
    """
    try:
        response = client.get(
            f"/{quote(project)}/_apis/wiki/wikis", params={"api-version": API_VERSION}
        )
    except httpx.TimeoutException as exc:
        raise SourceFetchError(
            f"Azure DevOps did not respond within {int(TIMEOUT_SECONDS)} seconds"
        ) from exc
    except httpx.HTTPError as exc:
        raise SourceFetchError(f"could not reach Azure DevOps ({exc})") from exc

    _check(response, project=project, missing=NO_WIKI_MESSAGE.format(project=project))
    payload = _decode_json(response)
    wikis = payload.get("value") if isinstance(payload, dict) else payload
    return [item for item in (wikis or []) if isinstance(item, dict)]


def _select_wiki(wikis: list[dict[str, Any]], wanted: str, *, project: str) -> dict[str, Any]:
    """The wiki the URL asked for, or the project's only one.

    :raises SourceFetchError: when the project has no wiki at all, or when the
        named wiki is not among them — and in the latter case the message lists
        what *is* there, so the user can fix the URL without leaving the screen.
    """
    if not wikis:
        raise SourceFetchError(NO_WIKI_MESSAGE.format(project=project))
    if not wanted:
        return wikis[0]
    lowered = wanted.strip().lower()
    for wiki in wikis:
        if lowered in {
            str(wiki.get("name") or "").lower(),
            str(wiki.get("id") or "").lower(),
        }:
            return wiki
    available = ", ".join(str(wiki.get("name") or wiki.get("id") or "?") for wiki in wikis)
    raise SourceFetchError(
        f"project '{project}' has no wiki called '{wanted}' — available: {available}"
    )


def _fetch_tree(
    client: httpx.Client, *, project: str, wiki_id: str, page_path: str
) -> dict[str, Any]:
    """The page tree under ``page_path``, with content, in one request.

    :raises SourceFetchError: including a page-path-specific 404, because "the
        wiki exists but that page does not" is a different repair from "there is
        no wiki".
    """
    try:
        response = client.get(
            f"/{quote(project)}/_apis/wiki/wikis/{quote(str(wiki_id))}/pages",
            params={
                "path": page_path,
                "recursionLevel": "full",
                "includeContent": "true",
                "api-version": API_VERSION,
            },
        )
    except httpx.TimeoutException as exc:
        raise SourceFetchError(
            f"Azure DevOps did not respond within {int(TIMEOUT_SECONDS)} seconds"
        ) from exc
    except httpx.HTTPError as exc:
        raise SourceFetchError(f"could not reach Azure DevOps ({exc})") from exc

    _check(
        response,
        project=project,
        missing=(
            f"the wiki page path '{page_path}' does not exist in this wiki — "
            "check the address, or link the wiki root instead"
        ),
    )
    payload = _decode_json(response)
    return payload if isinstance(payload, dict) else {}


def _walk(root: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Flatten the page tree breadth-first, honouring both caps.

    Breadth-first on purpose: when the page cap bites, the pages that survive
    are the shallow ones, which is the half of a wiki a reader would have
    reached for anyway.

    A node that has sub-pages and no content of its own is a *container*, not an
    empty document: it is skipped rather than reported as unreadable, because
    counting folders as failures would bury the pages that genuinely did fail.

    :returns: ``(pages, notes)`` — ``notes`` holds one user-facing sentence per
        cap that actually bit, and is empty when the whole tree fit. Neither cap
        is allowed to be silent.
    """
    pages: list[dict[str, Any]] = []
    hit_page_cap = False
    hit_depth_cap = False
    queue: list[tuple[dict[str, Any], int]] = [(root, 0)]
    while queue:
        node, depth = queue.pop(0)
        if not isinstance(node, dict):
            continue
        path = str(node.get("path") or "")
        sub_pages = [child for child in (node.get("subPages") or []) if isinstance(child, dict)]
        has_content = bool(str(node.get("content") or "").strip())
        if path not in ("", "/") and (has_content or not sub_pages):
            if len(pages) >= MAX_PAGES:
                hit_page_cap = True
            else:
                pages.append(node)
        if not sub_pages:
            continue
        if depth < MAX_DEPTH:
            queue.extend((child, depth + 1) for child in sub_pages)
        else:
            hit_depth_cap = True

    notes: list[str] = []
    if hit_page_cap:
        notes.append(_PAGE_CAP_MESSAGE.format(cap=MAX_PAGES))
    if hit_depth_cap:
        notes.append(_DEPTH_CAP_MESSAGE.format(cap=MAX_DEPTH))
    return pages, notes


def _doc_path(page_path: str) -> str:
    """On-disk filename for a wiki page path.

    ``/Refunds/Store credit`` -> ``Refunds/Store credit.md``. Keyed on the page
    path so the same page keeps the same file across re-syncs, which is what
    makes the content hash a staleness signal rather than noise.
    """
    cleaned = page_path.strip("/") or "Home"
    return f"{cleaned}.md"


def preflight(url: str, token: str) -> dict[str, Any]:
    """Prove, before anything is stored, that this token can read this wiki.

    Called from the credential endpoints in :mod:`app.routers.business_ado` so
    the user learns about a scope problem while they are still looking at the
    token field — not hours later, in a sync that reports ``error``.

    :param url: The wiki address.
    :param token: The PAT to test.
    :returns: ``{"project", "wiki", "wikis"}`` — the resolved project, the wiki
        that would be ingested, and the names of every wiki in the project.
    :raises SourceFetchError: with the specific refusal, which is the point.
    """
    if not (token or "").strip():
        raise SourceFetchError(NO_CREDENTIAL_MESSAGE)
    target = parse_wiki_url(url)
    with _client(target.org_url, token) as client:
        wikis = list_wikis(client, target.project)
        selected = _select_wiki(wikis, target.wiki, project=target.project)
    return {
        "project": target.project,
        "wiki": str(selected.get("name") or selected.get("id") or ""),
        "wikis": [str(wiki.get("name") or wiki.get("id") or "") for wiki in wikis],
    }


class AdoWikiAdapter:
    """Fetch a project wiki's pages as markdown."""

    kind = "ado_wiki"

    def fetch(self, source, credential: SourceCredential | None = None) -> list[FetchedDoc]:
        """Fetch every page of the wiki ``source.url`` addresses.

        :param source: The ``BusinessSource`` row; ``url`` only.
        :param credential: Resolved by
            :func:`app.services.business_ingest.credentials.resolve_credential`
            — never by this adapter.
        :returns: One :class:`~...base.FetchedDoc` per page, plus one carrying
            ``error`` for the page cap when it was hit. Pages are already
            markdown, so ``content_type`` is ``text/markdown``.
        :raises SourceFetchError: when nothing at all could be read — no token,
            a rejected token, a wrong scope, no wiki, a missing page path, a
            rate limit, or an unreachable host.
        """
        token = (credential.token if credential else "") or ""
        if not token.strip():
            raise SourceFetchError(NO_CREDENTIAL_MESSAGE)

        target = parse_wiki_url(source.url)
        with _client(target.org_url, token) as client:
            wikis = list_wikis(client, target.project)
            wiki = _select_wiki(wikis, target.wiki, project=target.project)
            wiki_id = str(wiki.get("id") or wiki.get("name") or "")
            tree = _fetch_tree(
                client,
                project=target.project,
                wiki_id=wiki_id,
                page_path=target.page_path,
            )

        pages, notes = _walk(tree)
        documents = [
            FetchedDoc(
                path=_doc_path(str(page.get("path") or "")),
                title=str(page.get("path") or "").strip("/") or "Home",
                raw_bytes=str(page.get("content") or "").encode("utf-8"),
                content_type="text/markdown",
                # ``eTag`` when the API supplied one, else the page id: both are
                # upstream identity, and neither is the content hash (which the
                # pipeline computes from the normalized text).
                upstream_rev=str(page.get("eTag") or page.get("id") or ""),
            )
            for page in pages
        ]
        # A cap that bit is reported as a document-level failure rather than
        # logged: that is how it reaches the row's "N documents could not be
        # read" detail instead of vanishing into a worker thread's stdout.
        documents.extend(
            FetchedDoc(path=f"_limit-{index}", error=note) for index, note in enumerate(notes)
        )
        return documents
