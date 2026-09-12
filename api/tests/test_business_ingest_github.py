"""The GitHub markdown source: what it fetches, and how it fails out loud (#821).

The adapter under test is
:mod:`app.services.business_ingest.adapters.github_md`; everything after
``fetch`` is #818's pipeline and is only asserted here where this source changes
what lands (partial success, the on-disk snapshot, the hash).

Three conventions from ``CLAUDE.md`` are load-bearing in this file:

* **Assert which branch ran plus an observable effect.** Every outcome is
  ``{"status": …}`` on one row, so a status code proves nothing on its own. The
  observable effects used throughout are the on-disk snapshot (present with the
  right bytes on success, *absent* on a whole-source failure) and the **requests
  actually issued** — which is the only way to tell "it used the token" from "it
  happened to work", or "it resolved the default branch" from "it hard-coded
  main".
* **Pin limits as literals, never as the constant under test.** A test that
  sizes its payload from ``MAX_FILES`` agrees with whatever ``MAX_FILES`` says,
  so widening the cap leaves it green. The numbers are written out here.
* **Negative controls are not optional.** A 404 message that fires on every
  repository, or a staleness probe that reports stale every time, is exactly as
  green as a correct one. Each failure test has the case that must *not* fire.

Every assertion in this file was mutation-proved: the adapter was deliberately
broken (the token header dropped, the 404 branch inverted, the markdown filter
removed, the SHA comparison hard-wired) and each test confirmed red before being
believed.
"""

from __future__ import annotations

import base64
import hashlib

import httpx
import pytest

from app import crypto
from app.models.business import BusinessSource
from app.models.project import Project
from app.models.provider_connection import ProviderConnection
from app.services import workspace_scope
from app.services.business_ingest import adapters, pipeline, storage
from app.services.business_ingest.adapters import github_md
from app.services.business_ingest.adapters.github_md import (
    GitHubMarkdownAdapter,
    SourceFetchError,
    parse_target,
)
from app.services.business_ingest.base import SourceCredential
from app.services.business_ingest.credentials import resolve_credential

OWNER = "acme"
REPO = "handbook"
HEAD_SHA = "9f1c0de4b2a7c5e8d3f6019a2b4c6d8e0f1a2b3c"
PAT = "ghp_a_real_looking_token"

#: A document with enough prose to clear the normalizer's readability floor.
DOC = """# Refund eligibility

A customer may request a refund within 30 days of the order date. Orders placed
with store credit are refunded to store credit, never to a card. A premium
member skips the review queue unless the order is flagged for fraud.
"""


def _blob_sha(data: bytes) -> str:
    """A deterministic per-file sha, standing in for GitHub's blob sha."""
    return hashlib.sha1(data).hexdigest()  # noqa: S324 - identity, not security


class FakeGitHub:
    """A GitHub Contents API just real enough to fetch markdown out of.

    Serves the three endpoints the adapter uses — the repository (for the
    default branch), ``/commits`` (for the staleness probe) and ``/contents``
    (listing and file) — from a ``{repo path: text}`` dict. It records every
    request, because most of what this file asserts is *which calls were made*
    rather than what came back.
    """

    def __init__(
        self,
        files: dict[str, str] | None = None,
        *,
        default_branch: str = "main",
        head_sha: str = HEAD_SHA,
        private: bool = False,
        token: str = PAT,
    ) -> None:
        self.files: dict[str, bytes] = {
            path: text.encode("utf-8") if isinstance(text, str) else text
            for path, text in (files or {}).items()
        }
        self.default_branch = default_branch
        self.head_sha = head_sha
        self.private = private
        self.token = token
        #: ``{repo path: httpx.Response}`` — injected failures for one file.
        self.file_failures: dict[str, httpx.Response] = {}
        self.requests: list[httpx.Request] = []

    # -- helpers ---------------------------------------------------------
    def authenticated(self, request: httpx.Request) -> bool:
        return request.headers.get("authorization") == f"Bearer {self.token}"

    def _not_found(self, message: str = "Not Found") -> httpx.Response:
        return httpx.Response(404, json={"message": message})

    def _file_payload(self, path: str) -> dict:
        data = self.files[path]
        return {
            "type": "file",
            "path": path,
            "name": path.rsplit("/", 1)[-1],
            "sha": _blob_sha(data),
            "size": len(data),
            "encoding": "base64",
            "content": base64.b64encode(data).decode("ascii"),
        }

    def _listing(self, directory: str) -> list[dict] | None:
        prefix = f"{directory}/" if directory else ""
        children: dict[str, dict] = {}
        for path, data in self.files.items():
            if not path.startswith(prefix):
                continue
            remainder = path[len(prefix) :]
            if "/" in remainder:
                name = remainder.split("/", 1)[0]
                children.setdefault(
                    name, {"type": "dir", "path": f"{prefix}{name}", "name": name, "sha": "", "size": 0}
                )
            else:
                children[remainder] = {
                    "type": "file",
                    "path": path,
                    "name": remainder,
                    "sha": _blob_sha(data),
                    "size": len(data),
                }
        return list(children.values()) or None

    # -- the transport ---------------------------------------------------
    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if self.private and not self.authenticated(request):
            return self._not_found()

        repo_root = f"/repos/{OWNER}/{REPO}"
        if path == repo_root:
            return httpx.Response(200, json={"default_branch": self.default_branch})
        if path == f"{repo_root}/commits":
            return httpx.Response(200, json=[{"sha": self.head_sha}])
        if path.startswith(f"{repo_root}/contents"):
            return self._contents(request, path[len(f"{repo_root}/contents") :].lstrip("/"))
        return self._not_found(f"no route for {path}")

    def _contents(self, request: httpx.Request, relative: str) -> httpx.Response:
        if relative in self.file_failures:
            return self.file_failures[relative]
        if relative in self.files:
            if request.headers.get("accept") == "application/vnd.github.raw":
                return httpx.Response(200, content=self.files[relative])
            return httpx.Response(200, json=self._file_payload(relative))
        listing = self._listing(relative)
        if listing is None:
            return self._not_found()
        return httpx.Response(200, json=listing)


@pytest.fixture
def mock_github(monkeypatch):
    """Point ``httpx.Client`` at a :class:`FakeGitHub` for one test.

    The adapter builds its own client — that is where its headers, timeout and
    base URL live, and the test must exercise *that* client — so the transport
    is injected by wrapping the constructor. ``monkeypatch`` restores it;
    nothing calls ``monkeypatch.undo()``, which would un-redirect the
    session/engine the ``workspace_dir`` fixture patched with the same
    monkeypatch (#641).
    """
    real_client = httpx.Client

    def install(server: FakeGitHub) -> FakeGitHub:
        def factory(**kwargs):
            kwargs["transport"] = httpx.MockTransport(server)
            return real_client(**kwargs)

        monkeypatch.setattr(httpx, "Client", factory)
        return server

    return install


@pytest.fixture
def project(db_session) -> Project:
    row = Project(provider_kind="github", external_id="ACME", name="Acme", active=True)
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _source(db_session, project, url: str, **kwargs) -> BusinessSource:
    row = BusinessSource(
        project_guid=project.guid,
        project_key=project.name,
        owner_id=None,
        kind="github_md",
        title="Handbook",
        url=url,
        status="pending",
        **kwargs,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture
def gh_source(db_session, project) -> BusinessSource:
    """A ``github_md`` source pointing at a folder, as #817's create leaves it."""
    return _source(db_session, project, f"https://github.com/{OWNER}/{REPO}/tree/main/docs")


def _snapshot_dir(source: BusinessSource):
    return storage.source_root(source.project_key, source.id, source.owner_id)


def _contents_requests(server: FakeGitHub) -> list[httpx.Request]:
    return [r for r in server.requests if "/contents" in r.url.path]


# ==========================================================================
# 1. The address parser
# ==========================================================================


def test_parse_target_reads_the_three_address_shapes():
    """Repo, folder and file URLs each resolve to the right ref/path/mode.

    The default branch is deliberately *not* guessed here: a repo URL leaves
    ``ref`` empty so the adapter has to go and ask, which is what
    ``test_the_default_branch_is_resolved_rather_than_assumed`` pins.
    """
    whole = parse_target(f"https://github.com/{OWNER}/{REPO}")
    assert (whole.owner, whole.repo, whole.ref, whole.path) == (OWNER, REPO, "", "")
    assert whole.single_file is False

    folder = parse_target(f"https://github.com/{OWNER}/{REPO}/tree/release-2/docs/rules")
    assert (folder.ref, folder.path, folder.single_file) == ("release-2", "docs/rules", False)

    one = parse_target(f"https://github.com/{OWNER}/{REPO}/blob/main/docs/refunds.md")
    assert (one.ref, one.path, one.single_file) == ("main", "docs/refunds.md", True)

    # Shorthand and a .git suffix — what people paste when not copying a browser URL.
    assert parse_target(f"{OWNER}/{REPO}.git").repo == REPO
    assert parse_target(f"{OWNER}/{REPO}/docs").path == "docs"


def test_a_non_github_address_is_refused_before_any_request(
    db_session, project, mock_github, workspace_dir
):
    """A GitLab link names the host in the refusal, and never opens a client."""
    source = _source(db_session, project, "https://gitlab.com/acme/handbook/-/tree/main/docs")
    server = mock_github(FakeGitHub({"docs/a.md": DOC}))

    pipeline.sync_source(db_session, source)

    assert source.status == "error"
    assert "gitlab.com is not github.com" in source.last_error
    assert server.requests == []
    assert not _snapshot_dir(source).exists()


def test_a_blob_url_that_is_not_markdown_is_refused_with_the_alternative():
    """Linking a ``.pdf`` blob says what to link instead, rather than 404ing later."""
    with pytest.raises(SourceFetchError) as exc:
        parse_target(f"https://github.com/{OWNER}/{REPO}/blob/main/docs/handbook.pdf")
    assert "not a markdown file" in str(exc.value)
    assert "link the folder that holds them" in str(exc.value)


# ==========================================================================
# 2. What a folder, and a single file, actually fetch
# ==========================================================================


def test_a_folder_expands_recursively_to_its_markdown_only(
    db_session, gh_source, mock_github, workspace_dir
):
    """``**/*.md`` under the linked path — nested included, non-markdown excluded.

    The exclusion is asserted on what reached disk, not only on ``doc_count``:
    a walk that fetched ``deploy.py`` and then failed to normalize it would
    satisfy the count and fail this.
    """
    mock_github(
        FakeGitHub(
            {
                "docs/refunds.md": DOC,
                "docs/policies/shipping.markdown": DOC.replace("Refund", "Shipping"),
                "docs/deploy.py": "print('not a document')",
                "docs/logo.png": "binary-ish",
                "README.md": DOC,  # outside the linked folder
            }
        )
    )
    pipeline.sync_source(db_session, gh_source)

    assert gh_source.status == "synced"
    assert gh_source.last_error == ""
    assert gh_source.doc_count == 2

    normalized = _snapshot_dir(gh_source) / "normalized"
    landed = sorted(p.relative_to(normalized).as_posix() for p in normalized.rglob("*.md"))
    assert landed == ["docs/policies/shipping.markdown.md", "docs/refunds.md.md"]


def test_a_blob_url_fetches_exactly_one_document_and_never_walks(
    db_session, project, mock_github, workspace_dir
):
    """A file link reads that file and lists no directories at all."""
    source = _source(
        db_session, project, f"https://github.com/{OWNER}/{REPO}/blob/main/docs/refunds.md"
    )
    server = mock_github(FakeGitHub({"docs/refunds.md": DOC, "docs/other.md": DOC}))

    pipeline.sync_source(db_session, source)

    assert source.status == "synced"
    assert source.doc_count == 1
    # Every contents call named the one file; nothing enumerated the folder.
    assert {r.url.path.split("/contents/")[-1] for r in _contents_requests(server)} == {
        "docs/refunds.md"
    }


def test_the_default_branch_is_resolved_rather_than_assumed(
    db_session, project, mock_github, workspace_dir
):
    """A repo URL with no ref asks GitHub, and uses the answer as ``?ref=``.

    The fake's default branch is deliberately *not* ``main``: a hard-coded
    ``main`` passes a test whose fixture also says main, and this is the test
    that catches it.
    """
    source = _source(db_session, project, f"https://github.com/{OWNER}/{REPO}")
    server = mock_github(FakeGitHub({"rules.md": DOC}, default_branch="trunk"))

    pipeline.sync_source(db_session, source)

    assert source.status == "synced"
    assert any(r.url.path == f"/repos/{OWNER}/{REPO}" for r in server.requests)
    assert {r.url.params.get("ref") for r in _contents_requests(server)} == {"trunk"}


def test_documents_carry_their_repo_path_and_their_blob_sha(db_session, gh_source, mock_github):
    """``FetchedDoc.path``/``upstream_rev`` are the repo path and the file's sha.

    The path is the snapshot's on-disk identity, so it must be stable across
    re-syncs; the blob sha is what #830 compares per document. Asserted on the
    adapter's own return value rather than through the pipeline, because the
    pipeline does not persist ``upstream_rev`` anywhere yet.
    """
    mock_github(FakeGitHub({"docs/refunds.md": DOC}))
    docs = GitHubMarkdownAdapter().fetch(gh_source, None)

    assert [d.path for d in docs] == ["docs/refunds.md"]
    assert docs[0].raw_bytes == DOC.encode("utf-8")
    assert docs[0].upstream_rev == _blob_sha(DOC.encode("utf-8"))
    assert docs[0].content_type.startswith("text/markdown")


# ==========================================================================
# 3. Credentials
# ==========================================================================


def _github_connection(db_session, *, pat: str = PAT, **kwargs) -> ProviderConnection:
    row = ProviderConnection(
        kind="github",
        name="Acme GitHub",
        connected=True,
        config={"org": OWNER},
        secrets={"pat": crypto.encrypt(pat)},
        **kwargs,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def test_a_private_repo_with_no_token_says_to_connect_an_account(
    db_session, gh_source, mock_github, workspace_dir
):
    """404 + no credential reads as "it may be private", with the scope named.

    A bare "GitHub returned 404" is the failure this message exists to replace:
    it sends the user looking for a typo when the real fix is a connection.
    """
    mock_github(FakeGitHub({"docs/refunds.md": DOC}, private=True))

    pipeline.sync_source(db_session, gh_source)

    assert gh_source.status == "error"
    assert "private" in gh_source.last_error
    assert "contents:read" in gh_source.last_error
    assert gh_source.content_hash == ""
    assert not _snapshot_dir(gh_source).exists()


def test_the_connections_pat_is_sent_and_the_private_repo_then_syncs(
    db_session, project, mock_github, workspace_dir
):
    """Negative control for the message above, and the credential wiring itself.

    Both halves are asserted: the source syncs (so the branch that resolves the
    connection ran), **and** every request actually carried the bearer token
    (so it did not sync because the fake went soft).
    """
    connection = _github_connection(db_session)
    source = _source(
        db_session,
        project,
        f"https://github.com/{OWNER}/{REPO}/tree/main/docs",
        connection_id=connection.id,
    )
    server = mock_github(FakeGitHub({"docs/refunds.md": DOC}, private=True))

    pipeline.sync_source(db_session, source)

    assert source.status == "synced"
    assert source.doc_count == 1
    assert server.requests
    assert all(r.headers.get("authorization") == f"Bearer {PAT}" for r in server.requests)


def test_a_hub_backed_connection_resolves_to_no_credential(db_session, project):
    """A hub mirror is refused on the marker, not merely on an empty secret (#514).

    The row is given a real-looking PAT on purpose. A mirror's ``secrets`` are
    empty in practice, so seeding it empty made this test pass with the
    hub check deleted — it was proving ``if not token`` and nothing else
    (caught by mutation). With a token present it can only pass if the hub
    marker itself is what stops the fetch.
    """
    connection = _github_connection(db_session, hub_connection_id="hub-1")
    source = _source(db_session, project, f"https://github.com/{OWNER}/{REPO}", connection_id=connection.id)

    assert resolve_credential(db_session, source) is None
    # Negative control: the same row without the hub marker does yield the token.
    real = _github_connection(db_session)
    source.connection_id = real.id
    db_session.commit()
    resolved = resolve_credential(db_session, source)
    assert resolved is not None and resolved.token == PAT


def test_a_source_never_borrows_another_users_connection(db_session, project):
    """ADR 0009: a per-user source only ever fetches with that user's connection."""
    from app.models.user import User

    owner = User(email="owner@acme.test", password_hash="x")
    other = User(email="other@acme.test", password_hash="x")
    db_session.add_all([owner, other])
    db_session.commit()

    theirs = _github_connection(db_session, owner_id=other.id)
    source = _source(db_session, project, f"https://github.com/{OWNER}/{REPO}", connection_id=theirs.id)
    source.owner_id = owner.id
    db_session.commit()
    assert resolve_credential(db_session, source) is None

    # Negative control: the owner's own connection resolves.
    mine = _github_connection(db_session, owner_id=owner.id)
    source.connection_id = mine.id
    db_session.commit()
    assert resolve_credential(db_session, source).token == PAT


def test_a_404_with_a_token_blames_the_path_not_privacy(
    db_session, project, mock_github, workspace_dir
):
    """With a connection attached, the advice changes — "check the path", not "connect"."""
    connection = _github_connection(db_session)
    source = _source(
        db_session,
        project,
        f"https://github.com/{OWNER}/{REPO}/tree/main/missing",
        connection_id=connection.id,
    )
    mock_github(FakeGitHub({"docs/refunds.md": DOC}))

    pipeline.sync_source(db_session, source)

    assert source.status == "error"
    assert "check the path and the" in source.last_error
    assert "if the repository is private" not in source.last_error


def test_a_revoked_token_is_named_as_a_credential_problem(
    db_session, project, mock_github, workspace_dir
):
    """A 401 says the token is expired or under-scoped — not that the repo is missing."""
    connection = _github_connection(db_session)
    source = _source(
        db_session,
        project,
        f"https://github.com/{OWNER}/{REPO}/tree/main/docs",
        connection_id=connection.id,
    )

    class _Unauthorized(FakeGitHub):
        def __call__(self, request):
            self.requests.append(request)
            return httpx.Response(401, json={"message": "Bad credentials"})

    mock_github(_Unauthorized({}))
    pipeline.sync_source(db_session, source)

    assert source.status == "error"
    assert "contents:read" in source.last_error
    assert "expired" in source.last_error


# ==========================================================================
# 4. Rate limits, refs, timeouts — the rest of the legible failures
# ==========================================================================


def test_a_rate_limited_403_surfaces_the_reset_time(
    db_session, gh_source, mock_github, workspace_dir
):
    """GitHub's aggressive anonymous limit reads as a limit, with the reset clock."""
    reset_epoch = 1_700_000_000  # 2023-11-14 22:13:20 UTC

    class _RateLimited(FakeGitHub):
        def __call__(self, request):
            self.requests.append(request)
            return httpx.Response(
                403,
                json={"message": "API rate limit exceeded for 203.0.113.4."},
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset_epoch)},
            )

    mock_github(_RateLimited({}))
    pipeline.sync_source(db_session, gh_source)

    assert gh_source.status == "error"
    assert "rate limit" in gh_source.last_error
    assert "22:13 UTC" in gh_source.last_error
    assert "connect a GitHub account" in gh_source.last_error
    assert not _snapshot_dir(gh_source).exists()


def test_a_403_that_is_not_a_rate_limit_is_not_reported_as_one(
    db_session, gh_source, mock_github, workspace_dir
):
    """Negative control: quota left means "no read access", not "rate limit"."""

    class _Forbidden(FakeGitHub):
        def __call__(self, request):
            self.requests.append(request)
            return httpx.Response(
                403,
                json={"message": "Resource not accessible by personal access token"},
                headers={"x-ratelimit-remaining": "4999"},
            )

    mock_github(_Forbidden({}))
    pipeline.sync_source(db_session, gh_source)

    assert gh_source.status == "error"
    assert "rate limit" not in gh_source.last_error
    assert "refused access" in gh_source.last_error
    assert "not accessible by personal access token" in gh_source.last_error


def test_a_branch_that_does_not_exist_is_named(db_session, project, mock_github, workspace_dir):
    """GitHub's "no commit found for the ref" becomes "the branch … does not exist"."""
    source = _source(
        db_session, project, f"https://github.com/{OWNER}/{REPO}/tree/no-such-branch/docs"
    )

    class _BadRef(FakeGitHub):
        def __call__(self, request):
            self.requests.append(request)
            return httpx.Response(404, json={"message": "No commit found for the ref no-such-branch"})

    mock_github(_BadRef({}))
    pipeline.sync_source(db_session, source)

    assert source.status == "error"
    assert "the branch or tag 'no-such-branch' does not exist" in source.last_error


def test_a_timeout_is_surfaced_in_seconds(db_session, gh_source, mock_github, workspace_dir):
    """A hung API reads as "did not respond within 20 seconds", not as a traceback."""

    class _Hangs(FakeGitHub):
        def __call__(self, request):
            self.requests.append(request)
            raise httpx.ConnectTimeout("timed out", request=request)

    mock_github(_Hangs({}))
    pipeline.sync_source(db_session, gh_source)

    assert gh_source.status == "error"
    assert "did not respond within 20 seconds" in gh_source.last_error


def test_a_path_with_no_markdown_is_an_error_never_an_empty_synced_source(
    db_session, gh_source, mock_github, workspace_dir
):
    """The whole point of the feature: "synced" must never mean "contains nothing".

    A folder of source files answers 200 for every request, so without this rule
    it would land as a healthy source with a content hash and zero documents.
    """
    mock_github(FakeGitHub({"docs/deploy.py": "x = 1", "docs/logo.png": "binary"}))

    pipeline.sync_source(db_session, gh_source)

    assert gh_source.status == "error"
    assert "no markdown files were found" in gh_source.last_error
    assert "docs" in gh_source.last_error
    assert gh_source.doc_count == 0
    assert gh_source.content_hash == ""
    assert not _snapshot_dir(gh_source).exists()


def test_a_folder_tree_too_deep_to_walk_is_refused_with_an_instruction(
    db_session, project, mock_github, workspace_dir
):
    """More than 50 directory listings stops, rather than burning the whole quota."""
    deep = "/".join(f"d{i}" for i in range(60))
    source = _source(db_session, project, f"https://github.com/{OWNER}/{REPO}/tree/main/d0")
    mock_github(FakeGitHub({f"{deep}/a.md": DOC}))

    pipeline.sync_source(db_session, source)

    assert source.status == "error"
    assert "too large to scan" in source.last_error
    assert "link a specific subfolder" in source.last_error


# ==========================================================================
# 5. The caps, pinned as literals
# ==========================================================================


def test_the_caps_are_the_numbers_the_spec_names():
    """100 files, 2 MB each, 20-second timeout — asserted as literals.

    Every other limit test would otherwise size its payload from the constant
    and so agree with whatever the constant says. This is the one place the
    numbers themselves are pinned, so changing one is a deliberate edit here.
    """
    assert github_md.MAX_FILES == 100
    assert github_md.MAX_DOC_BYTES == 2 * 1024 * 1024
    assert github_md.MAX_DIRECTORY_REQUESTS == 50
    assert github_md.TIMEOUT_SECONDS == 20.0
    assert sorted(github_md.MARKDOWN_EXTENSIONS) == [".markdown", ".md"]


def test_more_than_a_hundred_markdown_files_is_refused_with_the_way_out(
    db_session, gh_source, mock_github, workspace_dir
):
    """101 files (a literal 101) is refused, and the message says to narrow the link."""
    mock_github(FakeGitHub({f"docs/doc-{i:03d}.md": DOC for i in range(101)}))

    pipeline.sync_source(db_session, gh_source)

    assert gh_source.status == "error"
    assert "more than 100 markdown files" in gh_source.last_error
    assert not _snapshot_dir(gh_source).exists()


def test_exactly_a_hundred_markdown_files_still_syncs(
    db_session, gh_source, mock_github, workspace_dir
):
    """Negative control: the cap is not off by one and does not reject the limit itself."""
    mock_github(FakeGitHub({f"docs/doc-{i:03d}.md": DOC for i in range(100)}))

    pipeline.sync_source(db_session, gh_source)

    assert gh_source.status == "synced"
    assert gh_source.doc_count == 100


def test_a_file_over_two_megabytes_is_reported_while_the_others_still_land(
    db_session, gh_source, mock_github, workspace_dir
):
    """Partial success is a real state: the big file is *named*, not dropped.

    The payload is a literal 2 MB + 1 KB rather than ``MAX_DOC_BYTES + 1024``,
    so widening the cap makes this fail instead of silently agreeing with it.
    """
    oversized = "# Big\n\n" + "x" * (2 * 1024 * 1024 + 1024)
    mock_github(FakeGitHub({"docs/refunds.md": DOC, "docs/dump.md": oversized}))

    pipeline.sync_source(db_session, gh_source)

    assert gh_source.status == "synced"
    assert "1 document could not be read" in gh_source.last_error
    assert "docs/dump.md" in gh_source.last_error
    assert "larger than 2 MB" in gh_source.last_error
    assert gh_source.doc_count == 1
    assert not (_snapshot_dir(gh_source) / "raw" / "docs" / "dump.md").exists()
    assert (_snapshot_dir(gh_source) / "raw" / "docs" / "refunds.md").exists()


def test_a_file_that_cannot_be_read_does_not_fail_its_siblings(
    db_session, gh_source, mock_github, workspace_dir
):
    """A file that vanished between listing and read is doc-level, never fatal."""
    server = FakeGitHub({"docs/refunds.md": DOC, "docs/gone.md": DOC})
    server.file_failures["docs/gone.md"] = httpx.Response(404, json={"message": "Not Found"})
    mock_github(server)

    pipeline.sync_source(db_session, gh_source)

    assert gh_source.status == "synced"
    assert gh_source.doc_count == 1
    assert "1 document could not be read" in gh_source.last_error
    assert "docs/gone.md" in gh_source.last_error


def test_a_rate_limit_part_way_through_fails_the_whole_source(
    db_session, gh_source, mock_github, workspace_dir
):
    """Escalation, not 99 per-file notes: a quota failure ends the sync.

    The distinction matters — a rate limit reported as "1 document could not be
    read" would leave a source ``synced`` and missing most of its content, which
    is the silent-drop outcome dressed up as partial success.
    """
    server = FakeGitHub({"docs/a.md": DOC, "docs/b.md": DOC})
    server.file_failures["docs/b.md"] = httpx.Response(
        403,
        json={"message": "API rate limit exceeded"},
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1700000000"},
    )
    mock_github(server)

    pipeline.sync_source(db_session, gh_source)

    assert gh_source.status == "error"
    assert "rate limit" in gh_source.last_error
    assert gh_source.doc_count == 0
    assert not _snapshot_dir(gh_source).exists()


# ==========================================================================
# 6. The snapshot and the hash
# ==========================================================================


def test_raw_and_normalized_both_land_on_disk(db_session, gh_source, mock_github, workspace_dir):
    """Both artifacts persist, and the raw copy is byte-identical to the file.

    Keeping the raw bytes is what makes a later parser improvement retroactive
    without a re-fetch — which here means without a re-authenticate — so "the
    raw file exists" is not enough; it has to be exactly what GitHub sent.
    """
    mock_github(FakeGitHub({"docs/refunds.md": DOC}))
    pipeline.sync_source(db_session, gh_source)

    root = _snapshot_dir(gh_source)
    raw = root / "raw" / "docs" / "refunds.md"
    normalized = root / "normalized" / "docs" / "refunds.md.md"
    assert raw.read_bytes() == DOC.encode("utf-8")
    assert "Refund eligibility" in normalized.read_text(encoding="utf-8")

    scope = workspace_scope.scoped_business_dir(gh_source.owner_id)
    assert (scope / gh_source.raw_path) == root / "raw"
    assert (scope / gh_source.normalized_path) == root / "normalized"
    assert gh_source.byte_size == len(DOC.encode("utf-8"))


def test_the_hash_is_stable_across_two_identical_fetches(
    db_session, gh_source, mock_github, workspace_dir
):
    """Same content twice hashes the same; changed content does not.

    Both halves matter: a hash that drifted on an unchanged repository would cry
    wolf, and one that did not move on an edited document would never fire.
    """
    mock_github(FakeGitHub({"docs/refunds.md": DOC}))
    pipeline.sync_source(db_session, gh_source)
    first = gh_source.content_hash
    assert first != ""

    mock_github(FakeGitHub({"docs/refunds.md": DOC}))
    pipeline.sync_source(db_session, gh_source)
    assert gh_source.content_hash == first
    assert gh_source.status == "synced"

    mock_github(FakeGitHub({"docs/refunds.md": DOC.replace("30 days", "14 days")}))
    pipeline.sync_source(db_session, gh_source)
    assert gh_source.content_hash != first


# ==========================================================================
# 7. The SHA staleness probe — both directions
# ==========================================================================


def test_the_probe_reports_stale_only_when_the_commit_sha_moves(
    db_session, gh_source, mock_github, workspace_dir
):
    """An unchanged SHA is not stale; a moved one is.

    A probe that always reports stale is useless in exactly the same way as one
    that never does, so both directions are asserted against the same source.
    """
    mock_github(FakeGitHub({"docs/refunds.md": DOC}, head_sha=HEAD_SHA))
    assert github_md.probe_revision(gh_source, None) == HEAD_SHA
    assert github_md.is_stale(gh_source, None, HEAD_SHA) is False

    moved = "1234567890abcdef1234567890abcdef12345678"
    mock_github(FakeGitHub({"docs/refunds.md": DOC}, head_sha=moved))
    assert github_md.is_stale(gh_source, None, HEAD_SHA) is True
    # Never synced is stale too, rather than "unknown".
    assert github_md.is_stale(gh_source, None, "") is True


def test_the_probe_is_cheap_and_scoped_to_the_source_path(
    db_session, gh_source, mock_github, workspace_dir
):
    """One commits call, scoped to the path, and not a single document byte read.

    The scoping is the difference between a usable signal and noise: a
    repository-wide HEAD would report every unrelated code commit as "your
    handbook changed".
    """
    server = mock_github(FakeGitHub({"docs/refunds.md": DOC}))
    github_md.probe_revision(gh_source, SourceCredential(token=PAT))

    commits = [r for r in server.requests if r.url.path.endswith("/commits")]
    assert len(commits) == 1
    assert commits[0].url.params.get("path") == "docs"
    assert commits[0].url.params.get("sha") == "main"
    assert _contents_requests(server) == []


def test_a_probe_that_cannot_answer_raises_rather_than_returning_empty(
    db_session, gh_source, mock_github, workspace_dir
):
    """No commit for the path is an error, not a ``""`` that would read as stale forever."""

    class _NoCommits(FakeGitHub):
        def __call__(self, request):
            if request.url.path.endswith("/commits"):
                self.requests.append(request)
                return httpx.Response(200, json=[])
            return super().__call__(request)

    mock_github(_NoCommits({"docs/refunds.md": DOC}))
    with pytest.raises(SourceFetchError) as exc:
        github_md.probe_revision(gh_source, None)
    assert "no commit touches docs" in str(exc.value)


# ==========================================================================
# 8. Registration — through the built-in path, on a cold registry
# ==========================================================================


def test_github_md_resolves_through_the_builtin_registry(monkeypatch):
    """The kind is registered by ``_load_builtin``, not by importing this test.

    Asserted on a **cold** registry with a foreign kind registered first, which
    is the state that exposed the original ``if not _REGISTRY`` gate: the first
    registration of any kind suppressed the built-in load and made every other
    kind resolve to "cannot be ingested by this version". That bug was invisible
    in a whole-file run and only surfaced per-process, which is why this asserts
    the cold state explicitly instead of trusting module import order.
    """

    class _Foreign:
        kind = "url"

        def fetch(self, source, credential=None):  # pragma: no cover - never called
            return []

    monkeypatch.setattr(adapters, "_REGISTRY", {})
    monkeypatch.setattr(adapters, "_loaded", False)
    adapters.register(_Foreign())

    assert isinstance(adapters.get_adapter("github_md"), GitHubMarkdownAdapter)
    assert "github_md" in adapters.registered_kinds()
    # …and the substitution survived the load rather than being overwritten.
    assert isinstance(adapters.get_adapter("url"), _Foreign)


def test_the_kind_matches_the_model():
    """``github_md`` is one of the model's declared kinds — no drift between them."""
    from app.models.business import BUSINESS_SOURCE_KINDS

    assert GitHubMarkdownAdapter.kind in BUSINESS_SOURCE_KINDS
    assert adapters.registered_kinds().count("github_md") == 1
