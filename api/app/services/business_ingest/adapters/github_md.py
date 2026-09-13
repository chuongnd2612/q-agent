"""GitHub markdown source — the docs *in a repository*, as business context (#821).

A great many teams keep the rules a QC needs — the glossary, the refund policy,
the approval matrix — as markdown in the product repo rather than in a wiki. This
adapter turns a GitHub address into those documents.

Three address shapes, one parser (:func:`parse_target`)::

    https://github.com/acme/handbook                       whole repo, default branch
    https://github.com/acme/handbook/tree/main/docs/rules  a directory (recursive **/*.md)
    https://github.com/acme/handbook/blob/main/README.md   one file

**Credential.** ``ProviderConnection(kind="github").secrets["pat"]`` already
exists and is already the bearer token every other GitHub call in this codebase
uses (``app/services/adapters/github.py``); ``contents:read`` is the only scope
needed. It is resolved by
:mod:`app.services.business_ingest.credentials` on the caller's side and arrives
here as :class:`~...base.SourceCredential` — the adapter never touches the
database and never resolves its own secret. A **public** repository needs no
token at all, which is why the token is optional rather than required.

**Staleness is keyed on the commit SHA** (:func:`probe_revision` /
:func:`is_stale`), not on a timestamp and not on re-fetching the whole tree. One
request to ``/commits?path=…&per_page=1`` answers "has this document changed
upstream" for real, which is the signal #830 needs and the only one worth
storing.

**Every failure says what happened, in words.** A 404 on a repo with no token is
"it may be private — connect a GitHub account", not a bare 404. A 403 that is
really a rate limit says when the limit resets (GitHub's unauthenticated limit
is 60 requests/hour, which is genuinely easy to hit). A path that resolves to no
markdown at all is an *error*, never a ``synced`` source containing nothing —
that silent-empty outcome is the failure this whole feature exists to prevent.
A single unreadable file among many is doc-level: the rest land and the source
reports "N documents could not be read".
"""

from __future__ import annotations

import base64
import binascii
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import unquote, urlsplit

import httpx

from app.services.business_ingest.base import FetchedDoc, SourceCredential, SourceFetchError

__all__ = [
    "GitHubMarkdownAdapter",
    "GitHubTarget",
    "parse_target",
    "probe_revision",
    "is_stale",
    "API_BASE",
    "MARKDOWN_EXTENSIONS",
    "MAX_FILES",
    "MAX_DOC_BYTES",
    "MAX_DIRECTORY_REQUESTS",
    "TIMEOUT_SECONDS",
]

#: GitHub REST API root. Same constant as the provider adapter uses; kept local
#: rather than imported so this module has no dependency on the ticket adapters.
API_BASE = "https://api.github.com"
#: What counts as a markdown document when a directory is expanded.
MARKDOWN_EXTENSIONS = (".md", ".markdown")
#: Cap on documents per source. A repository of 500 docs is not a "source" a QC
#: can reason about, and expanding it silently would also burn the rate limit.
MAX_FILES = 100
#: Per-document byte cap. Matches the generic URL adapter's cap on purpose:
#: the ceiling is a property of the pipeline, not of the transport.
MAX_DOC_BYTES = 2 * 1024 * 1024
#: Directory listings allowed per sync. The recursive walk is one request per
#: directory, so a pathological tree is refused with an instruction rather than
#: quietly consuming the caller's whole hourly quota.
MAX_DIRECTORY_REQUESTS = 50
#: Whole-request timeout, seconds.
TIMEOUT_SECONDS = 20.0

_USER_AGENT = "Q-Agent/1.0 (+business-knowledge-ingest)"
_ACCEPT_JSON = "application/vnd.github+json"
_API_VERSION = "2022-11-28"


@dataclass(frozen=True)
class GitHubTarget:
    """A parsed GitHub address.

    :param owner: Repository owner (user or organisation login).
    :param repo: Repository name.
    :param ref: Branch, tag or commit named in the URL; ``""`` means "resolve
        the repository's default branch".
    :param path: Repository-relative path; ``""`` is the repository root.
    :param single_file: True for a ``/blob/`` address — exactly one document,
        never expanded.
    """

    owner: str
    repo: str
    ref: str = ""
    path: str = ""
    single_file: bool = False

    @property
    def slug(self) -> str:
        """``owner/repo``, for error messages."""
        return f"{self.owner}/{self.repo}"


def _is_markdown(path: str) -> bool:
    """Whether ``path`` names a markdown document."""
    return path.lower().endswith(MARKDOWN_EXTENSIONS)


def parse_target(url: str) -> GitHubTarget:
    """Parse a GitHub address into the repository, ref and path it names.

    Accepts the three web URL shapes plus the bare ``owner/repo[/…]`` shorthand,
    because that is what people paste when they are not copying from a browser.

    A ref containing a slash (``release/2024``) is genuinely ambiguous in a
    GitHub web URL — nothing in the path distinguishes it from a directory — so
    the first segment after ``tree``/``blob`` is taken as the ref. When that
    guess is wrong the failure is legible: GitHub answers "no commit found for
    the ref", which :func:`_raise_for_status` turns into "the branch or tag …
    does not exist".

    :param url: The ``BusinessSource.url``.
    :returns: The parsed :class:`GitHubTarget`.
    :raises SourceFetchError: for an empty URL, a non-GitHub host, or an address
        that names no repository.
    """
    raw = (url or "").strip()
    if not raw:
        raise SourceFetchError("this source has no GitHub address to fetch")

    candidate = raw
    if "://" in candidate:
        parts = urlsplit(candidate)
        host = (parts.netloc or "").lower().split("@")[-1].split(":")[0]
        if host not in ("github.com", "www.github.com"):
            raise SourceFetchError(
                f"{host or 'that address'} is not github.com — a GitHub markdown source "
                "must point at a repository on github.com"
            )
        candidate = parts.path
    elif candidate.lower().startswith("github.com/"):
        candidate = candidate[len("github.com") :]

    segments = [unquote(segment) for segment in candidate.split("/") if segment]
    if len(segments) < 2:
        raise SourceFetchError(
            "that address names no repository — link a repository, a folder or a "
            "markdown file, e.g. https://github.com/acme/handbook/tree/main/docs"
        )

    owner, repo = segments[0], segments[1]
    if repo.lower().endswith(".git"):
        repo = repo[: -len(".git")]

    rest = segments[2:]
    if not rest:
        return GitHubTarget(owner=owner, repo=repo)

    marker = rest[0].lower()
    if marker in ("tree", "blob"):
        if len(rest) < 2:
            raise SourceFetchError(
                f"that address names no branch in {owner}/{repo} — link the folder or "
                "file as it appears in the GitHub URL bar"
            )
        ref = rest[1]
        path = "/".join(rest[2:])
        single = marker == "blob"
        if single and not _is_markdown(path):
            raise SourceFetchError(
                f"{path or 'that file'} is not a markdown file — link a .md file, or "
                "link the folder that holds them"
            )
        return GitHubTarget(owner=owner, repo=repo, ref=ref, path=path, single_file=single)

    # Bare `owner/repo/docs/rules` shorthand: no ref, the remainder is the path.
    path = "/".join(rest)
    return GitHubTarget(
        owner=owner, repo=repo, path=path, single_file=_is_markdown(path)
    )


def _client(credential: SourceCredential | None) -> httpx.Client:
    """An API client, bearer-authenticated when a token was resolved.

    Unauthenticated is a supported mode, not a degraded one: a public repository
    needs no token, and requiring one would make the commonest case impossible.
    """
    headers = {
        "Accept": _ACCEPT_JSON,
        "X-GitHub-Api-Version": _API_VERSION,
        "User-Agent": _USER_AGENT,
    }
    token = (credential.token if credential else "") or ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=API_BASE, headers=headers, timeout=TIMEOUT_SECONDS)


def _rate_limit_message(response: httpx.Response, *, authenticated: bool) -> str:
    """The rate-limit failure, naming the reset time and the way out."""
    reset = response.headers.get("x-ratelimit-reset", "")
    when = ""
    try:
        when = datetime.fromtimestamp(int(reset), tz=UTC).strftime("%H:%M UTC")
    except (TypeError, ValueError):
        when = ""
    at = f" — it resets at {when}" if when else ""
    if authenticated:
        return f"GitHub's API rate limit is exhausted{at}; try again then"
    return (
        f"GitHub's API rate limit for anonymous requests is exhausted{at} — connect a "
        "GitHub account to raise it, or try again then"
    )


def _api_message(response: httpx.Response) -> str:
    """GitHub's own ``message`` field, when the body is the usual error JSON."""
    try:
        payload = response.json()
    except ValueError:
        return ""
    return str(payload.get("message", "")) if isinstance(payload, dict) else ""


def _raise_for_status(
    response: httpx.Response, target: GitHubTarget, *, authenticated: bool, what: str
) -> None:
    """Turn a non-2xx GitHub response into a message a QC can act on.

    :param response: The response to inspect; a 2xx returns without doing anything.
    :param target: The address being fetched, for the message.
    :param authenticated: Whether a token was sent — it changes the advice for
        both 404 (connect an account) and 403 (raise the limit).
    :param what: What was being fetched, e.g. ``"docs/rules"``.
    :raises SourceFetchError: for every non-2xx status.
    """
    if 200 <= response.status_code < 300:
        return

    message = _api_message(response)
    remaining = response.headers.get("x-ratelimit-remaining", "")

    if response.status_code in (403, 429) and remaining == "0":
        raise SourceFetchError(_rate_limit_message(response, authenticated=authenticated))
    if response.status_code == 401:
        raise SourceFetchError(
            f"GitHub rejected the connected account's token for {target.slug} — it may "
            "be expired, or it may lack the contents:read scope"
        )
    if response.status_code == 403:
        detail = f" ({message})" if message else ""
        raise SourceFetchError(
            f"GitHub refused access to {target.slug}{detail} — the connected account "
            "may not have read access to this repository"
        )
    if response.status_code == 404:
        if "no commit found for the ref" in message.lower():
            raise SourceFetchError(
                f"the branch or tag '{target.ref}' does not exist in {target.slug}"
            )
        if not authenticated:
            raise SourceFetchError(
                f"we could not find {what} in {target.slug} — if the repository is "
                "private, connect a GitHub account with read access (a token with the "
                "contents:read scope)"
            )
        raise SourceFetchError(
            f"we could not find {what} in {target.slug} — check the path and the "
            "branch, or whether the connected GitHub account can read this repository"
        )

    detail = f" ({message})" if message else ""
    raise SourceFetchError(f"GitHub returned {response.status_code} for {target.slug}{detail}")


def _request(
    client: httpx.Client,
    path: str,
    *,
    target: GitHubTarget,
    authenticated: bool,
    what: str,
    params: dict | None = None,
    accept: str | None = None,
) -> httpx.Response:
    """One API call, with the transport failures translated too.

    :raises SourceFetchError: for a timeout, an unreachable host, or any non-2xx.
    """
    headers = {"Accept": accept} if accept else None
    try:
        response = client.get(path, params=params, headers=headers)
    except httpx.TimeoutException as exc:
        raise SourceFetchError(
            f"GitHub did not respond within {int(TIMEOUT_SECONDS)} seconds"
        ) from exc
    except httpx.HTTPError as exc:
        raise SourceFetchError(f"could not reach GitHub ({exc})") from exc
    _raise_for_status(response, target, authenticated=authenticated, what=what)
    return response


def _resolve_ref(client: httpx.Client, target: GitHubTarget, *, authenticated: bool) -> str:
    """The ref to read, resolving the repository's default branch when none was given."""
    if target.ref:
        return target.ref
    response = _request(
        client,
        f"/repos/{target.owner}/{target.repo}",
        target=target,
        authenticated=authenticated,
        what="that repository",
    )
    payload = response.json()
    branch = str(payload.get("default_branch") or "") if isinstance(payload, dict) else ""
    if not branch:
        raise SourceFetchError(f"{target.slug} has no default branch to read")
    return branch


def _head_commit(
    client: httpx.Client, target: GitHubTarget, ref: str, *, authenticated: bool
) -> str:
    """The SHA of the latest commit touching this source's path on ``ref``.

    Scoped to the path on purpose: a repository-wide HEAD would report every
    unrelated code commit as "your handbook changed", which is a staleness signal
    nobody would keep looking at.
    """
    params: dict[str, object] = {"sha": ref, "per_page": 1}
    if target.path:
        params["path"] = target.path
    response = _request(
        client,
        f"/repos/{target.owner}/{target.repo}/commits",
        target=target,
        authenticated=authenticated,
        what=target.path or "that repository",
        params=params,
    )
    payload = response.json()
    if not isinstance(payload, list) or not payload:
        raise SourceFetchError(
            f"no commit touches {target.path or 'this repository'} on '{ref}' in "
            f"{target.slug} — check the path and the branch"
        )
    sha = str(payload[0].get("sha") or "")
    if not sha:
        raise SourceFetchError(f"GitHub returned no commit SHA for {target.slug}")
    return sha


def probe_revision(source, credential: SourceCredential | None = None) -> str:
    """The current upstream commit SHA for ``source`` — the cheap staleness probe.

    One or two API calls (a default-branch lookup only when the URL named no
    ref), and no document bytes at all. This is what makes "has it changed
    upstream" answerable without re-ingesting, which is the whole reason
    staleness here is keyed on a SHA rather than on a timestamp.

    :param source: The ``BusinessSource`` row; only ``url`` is read.
    :param credential: The resolved token, or ``None`` for a public repository.
    :returns: A commit SHA.
    :raises SourceFetchError: for anything that stops the probe answering —
        never a silent ``""``, because an unknown revision compared against a
        stored one would read as "changed" every single time.
    """
    target = parse_target(getattr(source, "url", ""))
    authenticated = bool(credential and credential.token)
    with _client(credential) as client:
        ref = _resolve_ref(client, target, authenticated=authenticated)
        return _head_commit(client, target, ref, authenticated=authenticated)


def is_stale(source, credential: SourceCredential | None, known_revision: str) -> bool:
    """Whether the upstream commit has moved since ``known_revision``.

    Both directions matter and both are asserted in the tests: a probe that
    always reports stale is useless in exactly the same way as one that never
    does.

    :param source: The ``BusinessSource`` row.
    :param credential: The resolved token, or ``None``.
    :param known_revision: The SHA recorded when the snapshot was taken. Empty
        (never synced) is always stale.
    :returns: True when the upstream SHA differs from ``known_revision``.
    :raises SourceFetchError: when the probe cannot answer.
    """
    return probe_revision(source, credential) != (known_revision or "")


@dataclass(frozen=True)
class _Entry:
    """One markdown file found in the repository tree."""

    path: str
    sha: str
    size: int


def _entry_of(item: dict) -> _Entry:
    return _Entry(
        path=str(item.get("path") or ""),
        sha=str(item.get("sha") or ""),
        size=int(item.get("size") or 0),
    )


class GitHubMarkdownAdapter:
    """Fetch the markdown documents a GitHub address names."""

    kind = "github_md"

    def probe_revision(self, source, credential: SourceCredential | None = None) -> str:
        """The current upstream commit SHA — the cheap staleness probe (#830).

        The method on the adapter is what the staleness service calls; the
        module-level :func:`probe_revision` is where the logic lives, so a test
        can exercise it without a registry lookup.

        :param source: The ``BusinessSource`` row; only ``url`` is read.
        :param credential: The resolved token, or ``None`` for a public repo.
        :returns: A commit SHA, never ``""``.
        :raises SourceFetchError: when the probe cannot answer.
        """
        return probe_revision(source, credential)

    def fetch(self, source, credential: SourceCredential | None = None) -> list[FetchedDoc]:
        """Fetch every markdown document under ``source.url``.

        :param source: The ``BusinessSource`` row; only ``url`` and ``title`` are read.
        :param credential: The resolved GitHub token, or ``None`` for a public
            repository. Resolution happens in the caller (see
            :mod:`app.services.business_ingest.credentials`).
        :returns: One :class:`~...base.FetchedDoc` per markdown file, each
            carrying its blob SHA as ``upstream_rev``. A file that could not be
            read is returned with ``error`` set rather than dropped.
        :raises SourceFetchError: when nothing at all can be ingested — a bad
            address, a private repository with no token, an exhausted rate
            limit, a missing ref, a path holding no markdown, or a tree too
            large to expand.
        """
        target = parse_target(getattr(source, "url", ""))
        authenticated = bool(credential and credential.token)

        with _client(credential) as client:
            ref = _resolve_ref(client, target, authenticated=authenticated)
            entries = self._collect(client, target, ref, authenticated=authenticated)
            return [
                self._fetch_one(client, target, ref, entry, authenticated=authenticated)
                for entry in entries
            ]

    # -- enumeration ------------------------------------------------------
    def _collect(
        self, client: httpx.Client, target: GitHubTarget, ref: str, *, authenticated: bool
    ) -> list[_Entry]:
        """Every markdown file the address expands to, sorted by path.

        :raises SourceFetchError: when the path holds no markdown, exceeds
            :data:`MAX_FILES`, or needs more than
            :data:`MAX_DIRECTORY_REQUESTS` listings to walk.
        """
        if target.single_file:
            return [self._single(client, target, ref, authenticated=authenticated)]

        found: list[_Entry] = []
        queue: deque[str] = deque([target.path])
        listings = 0
        while queue:
            directory = queue.popleft()
            listings += 1
            if listings > MAX_DIRECTORY_REQUESTS:
                raise SourceFetchError(
                    f"the folder tree under {target.path or 'the repository root'} in "
                    f"{target.slug} is too large to scan — link a specific subfolder"
                )
            payload = _request(
                client,
                f"/repos/{target.owner}/{target.repo}/contents/{directory}",
                target=target,
                authenticated=authenticated,
                what=directory or "that repository",
                params={"ref": ref},
            ).json()
            if isinstance(payload, dict):
                # The address said "folder" but GitHub says it is a file.
                if _is_markdown(str(payload.get("path") or "")):
                    found.append(_entry_of(payload))
                    continue
                raise SourceFetchError(
                    f"{directory} in {target.slug} is not a folder of markdown files"
                )
            for item in payload:
                item_type = str(item.get("type") or "")
                item_path = str(item.get("path") or "")
                if item_type == "dir":
                    queue.append(item_path)
                elif item_type == "file" and _is_markdown(item_path):
                    found.append(_entry_of(item))
            if len(found) > MAX_FILES:
                raise SourceFetchError(
                    f"{target.path or 'this repository'} holds more than {MAX_FILES} "
                    "markdown files — link a specific subfolder instead"
                )

        if not found:
            raise SourceFetchError(
                f"no markdown files were found under {target.path or 'the repository root'} "
                f"on '{ref}' in {target.slug} — check the path and the branch"
            )
        return sorted(found, key=lambda entry: entry.path)

    def _single(
        self, client: httpx.Client, target: GitHubTarget, ref: str, *, authenticated: bool
    ) -> _Entry:
        """Metadata for a ``/blob/`` address."""
        payload = _request(
            client,
            f"/repos/{target.owner}/{target.repo}/contents/{target.path}",
            target=target,
            authenticated=authenticated,
            what=target.path,
            params={"ref": ref},
        ).json()
        if isinstance(payload, list):
            raise SourceFetchError(
                f"{target.path} in {target.slug} is a folder, not a file — link it with "
                "the folder URL instead"
            )
        return _entry_of(payload)

    # -- one document -----------------------------------------------------
    def _fetch_one(
        self,
        client: httpx.Client,
        target: GitHubTarget,
        ref: str,
        entry: _Entry,
        *,
        authenticated: bool,
    ) -> FetchedDoc:
        """Fetch one file's bytes, or return the doc carrying its own error.

        Doc-level failures (too large, undecodable, a file that vanished between
        the listing and the read) never abort the source: the readable documents
        still land and the pipeline reports "N documents could not be read".
        Source-level failures (rate limit, revoked token) still raise, because
        continuing through 99 more of them would help nobody.
        """
        if entry.size > MAX_DOC_BYTES:
            return FetchedDoc(
                path=entry.path,
                title=entry.path,
                upstream_rev=entry.sha,
                error=f"{entry.path} is larger than 2 MB and was not fetched",
            )
        try:
            response = _request(
                client,
                f"/repos/{target.owner}/{target.repo}/contents/{entry.path}",
                target=target,
                authenticated=authenticated,
                what=entry.path,
                params={"ref": ref},
            )
        except SourceFetchError as exc:
            if _is_source_level(exc):
                raise
            return FetchedDoc(
                path=entry.path, title=entry.path, upstream_rev=entry.sha, error=str(exc)
            )

        body = response.json()
        payload = body if isinstance(body, dict) else {}
        raw = _decode_content(payload)
        if raw is None and str(payload.get("encoding") or "") == "none":
            # Over 1 MB: GitHub sends no inline content, only the raw media type
            # does. Still within our 2 MB ceiling, so fetch it rather than
            # reporting a perfectly readable file as unreadable.
            try:
                raw = _request(
                    client,
                    f"/repos/{target.owner}/{target.repo}/contents/{entry.path}",
                    target=target,
                    authenticated=authenticated,
                    what=entry.path,
                    params={"ref": ref},
                    accept="application/vnd.github.raw",
                ).content
            except SourceFetchError as exc:
                if _is_source_level(exc):
                    raise
                raw = None
        if raw is None:
            return FetchedDoc(
                path=entry.path,
                title=entry.path,
                upstream_rev=entry.sha,
                error=f"{entry.path} could not be decoded from GitHub's response",
            )
        if len(raw) > MAX_DOC_BYTES:
            return FetchedDoc(
                path=entry.path,
                title=entry.path,
                upstream_rev=entry.sha,
                error=f"{entry.path} is larger than 2 MB and was not fetched",
            )
        return FetchedDoc(
            path=entry.path,
            title=entry.path,
            raw_bytes=raw,
            content_type="text/markdown; charset=utf-8",
            upstream_rev=str(payload.get("sha") or entry.sha),
        )


#: Fragments of the messages that mean "stop the whole sync", as opposed to the
#: per-file ones. Matched on the message because the transport layer raises a
#: single error type by design — the alternative, an exception subclass per HTTP
#: status, would spread HTTP knowledge across the module for one call site.
_SOURCE_LEVEL_FRAGMENTS = (
    "rate limit",
    "rejected the connected account's token",
    "did not respond within",
    "could not reach GitHub",
)


def _is_source_level(exc: SourceFetchError) -> bool:
    """Whether this failure means the *source* failed, not just one document."""
    message = str(exc).lower()
    return any(fragment in message for fragment in _SOURCE_LEVEL_FRAGMENTS)


def _decode_content(payload: dict) -> bytes | None:
    """The file bytes out of a Contents API payload, or ``None`` if undecodable.

    GitHub base64-encodes files up to 1 MB and, above that, answers with
    ``encoding: "none"`` and an empty ``content`` — which is why the raw media
    type is used as the fallback rather than treated as an empty file.
    """
    encoding = str(payload.get("encoding") or "")
    content = payload.get("content")
    if encoding == "base64" and isinstance(content, str):
        try:
            return base64.b64decode(content)
        except (binascii.Error, ValueError):
            return None
    if isinstance(content, str) and content and encoding in ("", "utf-8"):
        return content.encode("utf-8")
    return None
