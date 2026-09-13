"""Business Knowledge ingestion — the pipeline, its two sources, its failures (#818).

The slice's stated point is that **failure is legible**, so most of what is
asserted here is failure: a status that reaches the user in words, a snapshot
that is absent rather than empty, a partial sync that says how many documents it
lost. The single most important test in the file is the SPA-shell pair
(:func:`test_spa_shell_is_rejected` / :func:`test_a_real_page_passes_the_spa_check`):
a JavaScript-rendered page answers ``200 OK`` with an empty ``<div id="root">``,
and ingesting it silently produces a source that reports ``synced``, carries a
content hash and contains nothing at all. The negative control is not optional
— a length check calibrated slightly wrong rejects *every* page, and would look
exactly as green as a correct one without it.

Two conventions from ``CLAUDE.md`` are load-bearing here:

* **Assert which branch ran, plus an observable effect** — never only a status
  code. Every ingestion outcome is ``{"status": ...}`` on the same row, so a
  green-looking response from the wrong branch is the easy mistake. The
  observable effect used throughout is the on-disk snapshot: present with the
  right bytes on success, *absent* on a whole-source failure.
* **Never ``==`` a whole response body**, so the endpoint tests assert the
  fields they are about and pin the behaviour on the row and on disk.
"""

from __future__ import annotations

import time

import httpx
import pytest

from app.models.business import BusinessSource
from app.models.project import Project
from app.models.user import User
from app.services import auth_service, workspace_scope
from app.services.business_ingest import (
    MIN_READABLE_CHARS,
    SPA_SHELL_MESSAGE,
    FetchedDoc,
    UnreadableContentError,
    adapters,
    normalize,
    pipeline,
    readable_length,
    storage,
    uploads,
)
from app.services.business_ingest.adapters.url import (
    MAX_DOC_BYTES,
    MAX_REDIRECTS,
    TIMEOUT_SECONDS,
)

# --------------------------------------------------------------------------
# Fixtures: the two HTML pages the SPA check has to tell apart
# --------------------------------------------------------------------------

#: A realistic JavaScript-rendered shell. Everything a bundler emits and nothing
#: a reader could use: a title, a preload, an empty mount point, a <noscript>
#: line and an inline bootstrap. It answers 200 and it is the trap.
SPA_SHELL_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Acme Ops Handbook</title>
    <link rel="modulepreload" href="/assets/index-a91f2c3d.js" />
    <link rel="stylesheet" href="/assets/index-4f0e1b77.css" />
  </head>
  <body>
    <div id="root"></div>
    <noscript>You need to enable JavaScript to run this app.</noscript>
    <script type="module" src="/assets/index-a91f2c3d.js"></script>
    <script>window.__INITIAL_STATE__={"user":null,"flags":{"beta":true}};</script>
  </body>
</html>
"""

#: The **negative control**: the same shell markup, server-rendered with the
#: page's actual prose inside the mount point. If the length check is calibrated
#: wrong this fails, which is the only way to know the check is discriminating
#: rather than simply always firing.
REAL_PAGE_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Acme Ops Handbook</title>
    <link rel="stylesheet" href="/assets/index-4f0e1b77.css" />
  </head>
  <body>
    <div id="root">
      <h1>Refund eligibility</h1>
      <p>
        A customer may request a refund within 30 days of the order date. Orders
        placed with store credit are refunded to store credit, never to a card.
      </p>
      <p>
        A premium member skips the review queue: their refund is approved
        automatically unless the order is flagged for fraud, in which case it is
        routed to the risk team and the customer is notified by email.
      </p>
      <ul>
        <li>Digital goods are refundable only if never downloaded.</li>
        <li>Shipping is refunded when the fault is ours.</li>
      </ul>
    </div>
    <script type="module" src="/assets/index-a91f2c3d.js"></script>
  </body>
</html>
"""


def _html_doc(html: str, path: str = "index.html") -> FetchedDoc:
    return FetchedDoc(
        path=path,
        title="Acme Ops Handbook",
        raw_bytes=html.encode("utf-8"),
        content_type="text/html; charset=utf-8",
    )


# --------------------------------------------------------------------------
# Fixtures: rows, and a web the URL adapter can be pointed at
# --------------------------------------------------------------------------


@pytest.fixture
def project(db_session) -> Project:
    """One project to hang sources on."""
    row = Project(provider_kind="ado", external_id="ACME", name="Acme", active=True)
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture
def url_source(db_session, project) -> BusinessSource:
    """A registered ``url`` source in the state #817's create endpoint leaves it."""
    row = BusinessSource(
        project_guid=project.guid,
        project_key=project.name,
        owner_id=None,
        kind="url",
        title="Acme Ops Handbook",
        url="https://handbook.acme.test/refunds",
        status="pending",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture
def mock_web(monkeypatch):
    """Point ``httpx.Client`` at a :class:`httpx.MockTransport` for one test.

    The adapter builds its own client (that is where its redirect, timeout and
    User-Agent policy lives, and the test must exercise *that* client, not a
    substitute), so the transport is injected by wrapping the constructor rather
    than by passing a client in. ``monkeypatch`` restores it; nothing calls
    ``monkeypatch.undo()``, which would un-redirect the session/engine the
    ``workspace_dir`` fixture patched with the same monkeypatch (#641).

    Yields a callable taking the request handler and returning a list that
    records every request actually issued — which is what proves "single page
    only, no crawling".
    """
    real_client = httpx.Client

    def install(handler):
        seen: list[httpx.Request] = []

        def recording(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        def factory(**kwargs):
            kwargs["transport"] = httpx.MockTransport(recording)
            return real_client(**kwargs)

        monkeypatch.setattr(httpx, "Client", factory)
        return seen

    return install


def _serves(html: str, content_type: str = "text/html; charset=utf-8"):
    """A handler answering every request with ``html``."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=html.encode("utf-8"), headers={"content-type": content_type})

    return handler


class _ScriptedAdapter:
    """A ``url`` adapter substitute returning documents the test wrote.

    Used where the assertion is about the *pipeline* (partial success, hashing,
    persistence) rather than about HTTP, so those tests do not have to also mock
    a web server to say something about a count.
    """

    kind = "url"

    def __init__(self, documents: list[FetchedDoc] | Exception):
        self.documents = documents
        self.calls = 0

    def fetch(self, source, credential=None) -> list[FetchedDoc]:
        self.calls += 1
        if isinstance(self.documents, Exception):
            raise self.documents
        return list(self.documents)


@pytest.fixture
def scripted_adapter(monkeypatch):
    """Install a :class:`_ScriptedAdapter` for ``kind="url"``, restored after."""

    def install(documents):
        # Force the lazy built-in load *before* substituting, so this test's
        # entry is not the thing that makes the registry look already-populated.
        # (It no longer would — `_load_builtin` is flag-guarded — but ordering
        # the two this way keeps the fixture correct independently of that.)
        adapters._load_builtin()
        adapter = _ScriptedAdapter(documents)
        monkeypatch.setitem(adapters._REGISTRY, "url", adapter)
        return adapter

    return install


def test_the_builtin_adapters_load_even_when_a_kind_was_registered_first(monkeypatch):
    """A substituted kind must not suppress the built-in load of the others.

    The registry lazy-loads its built-ins, and keying that on "the registry is
    empty" made the *first* registration — a test double, a future plugin —
    hide every other kind behind "cannot be ingested by this version". Asserted
    on a cold registry, because that is the only state in which it can happen.
    """
    monkeypatch.setattr(adapters, "_REGISTRY", {})
    monkeypatch.setattr(adapters, "_loaded", False)
    adapters.register(_ScriptedAdapter([]))  # kind == "url", registered first

    assert adapters.get_adapter("upload").kind == "upload"
    # …and the substitution survived the load rather than being overwritten.
    assert isinstance(adapters.get_adapter("url"), _ScriptedAdapter)
    assert "url" in adapters.registered_kinds()


def _snapshot_dir(source: BusinessSource):
    return storage.source_root(source.project_key, source.id, source.owner_id)


# ==========================================================================
# 1. The SPA trap — the point of the slice, with its negative control
# ==========================================================================


def test_spa_shell_is_rejected():
    """A JavaScript-rendered empty shell is refused, with the actionable text.

    The message matters as much as the refusal: "try uploading the content
    instead" is what makes the failure recoverable by the user who hit it.
    """
    with pytest.raises(UnreadableContentError) as exc:
        normalize(_html_doc(SPA_SHELL_HTML))
    assert str(exc.value) == SPA_SHELL_MESSAGE
    assert "try uploading the content instead" in str(exc.value)


def test_a_real_page_passes_the_spa_check():
    """Negative control: a page with real prose must ingest, not trip the check.

    Without this, a check that rejected *every* HTML page would pass the test
    above and look correct.
    """
    markdown = normalize(_html_doc(REAL_PAGE_HTML))
    assert "Refund eligibility" in markdown
    assert "premium member skips the review queue" in markdown
    assert readable_length(markdown) >= MIN_READABLE_CHARS


def test_the_two_fixtures_differ_only_in_their_body_text():
    """Guard on the controls themselves: the shell is thin, the page is not.

    If someone later trims the prose fixture below the threshold, this fails
    loudly instead of the SPA test quietly passing for the wrong reason.
    """
    from app.services.business_ingest.normalize import _DocumentConverter

    shell = _DocumentConverter(heading_style="ATX").convert(SPA_SHELL_HTML).strip()
    page = _DocumentConverter(heading_style="ATX").convert(REAL_PAGE_HTML).strip()
    assert readable_length(shell) < MIN_READABLE_CHARS <= readable_length(page)


def test_spa_shell_over_http_leaves_the_source_in_error_with_no_snapshot(
    db_session, url_source, mock_web, workspace_dir
):
    """End to end: a 200 OK shell must not produce a ``synced`` source.

    Branch **and** observable effect: the row is ``error`` (never ``synced``),
    it has no content hash to attribute an artifact to, and nothing at all was
    written to disk — the three things a silently-ingested shell would have
    faked.
    """
    mock_web(_serves(SPA_SHELL_HTML))
    pipeline.sync_source(db_session, url_source)

    assert url_source.status == "error"
    assert url_source.last_error == SPA_SHELL_MESSAGE
    assert url_source.content_hash == ""
    assert url_source.doc_count == 0
    assert not _snapshot_dir(url_source).exists()


def test_a_real_page_over_http_syncs(db_session, url_source, mock_web, workspace_dir):
    """Negative control for the end-to-end path: the same route, a real page."""
    mock_web(_serves(REAL_PAGE_HTML))
    pipeline.sync_source(db_session, url_source)

    assert url_source.status == "synced"
    assert url_source.last_error == ""
    assert url_source.content_hash != ""
    assert url_source.doc_count == 1


# ==========================================================================
# 2. Raw AND normalized on disk; a stable hash
# ==========================================================================


def test_raw_and_normalized_both_land_on_disk(db_session, url_source, mock_web, workspace_dir):
    """Both artifacts are persisted, and the raw copy is byte-identical.

    Keeping the raw bytes is what makes a later parser improvement retroactive
    without a re-fetch (and, for the credentialed sources, without a
    re-authenticate), so "the raw file exists" is not enough — it has to be
    exactly what came off the wire.
    """
    mock_web(_serves(REAL_PAGE_HTML))
    pipeline.sync_source(db_session, url_source)

    root = _snapshot_dir(url_source)
    raw = root / "raw" / "index.html"
    normalized = root / "normalized" / "index.html.md"
    assert raw.read_bytes() == REAL_PAGE_HTML.encode("utf-8")
    assert "Refund eligibility" in normalized.read_text(encoding="utf-8")
    # Markdown, not the original HTML: the normalizer actually ran.
    assert "<div id=" not in normalized.read_text(encoding="utf-8")

    # The row points at the directories, relative to the owner's business scope.
    scope = workspace_scope.scoped_business_dir(url_source.owner_id)
    assert (scope / url_source.raw_path) == root / "raw"
    assert (scope / url_source.normalized_path) == root / "normalized"
    assert url_source.byte_size == len(REAL_PAGE_HTML.encode("utf-8"))


def test_hash_is_stable_across_two_identical_fetches(
    db_session, url_source, mock_web, workspace_dir
):
    """Two identical fetches hash the same; a changed page does not.

    The hash is the staleness signal (#830), so both halves matter: a hash that
    drifted on an unchanged page would cry wolf, and one that did not move on a
    changed page would never fire at all.
    """
    mock_web(_serves(REAL_PAGE_HTML))
    pipeline.sync_source(db_session, url_source)
    first = url_source.content_hash
    first_fetched_at = url_source.fetched_at

    pipeline.sync_source(db_session, url_source)
    assert url_source.content_hash == first
    assert url_source.status == "synced"
    # It genuinely re-ran rather than short-circuiting on the existing snapshot.
    assert url_source.fetched_at >= first_fetched_at

    changed = REAL_PAGE_HTML.replace("within 30 days", "within 14 days")
    mock_web(_serves(changed))
    pipeline.sync_source(db_session, url_source)
    assert url_source.content_hash != first


def test_hash_ignores_the_order_documents_were_returned_in(scripted_adapter, db_session, url_source, workspace_dir):
    """The digest is path-ordered, so an adapter's iteration order cannot move it."""
    a = _html_doc(REAL_PAGE_HTML, path="a.html")
    b = _html_doc(REAL_PAGE_HTML.replace("Refund", "Return"), path="b.html")

    scripted_adapter([a, b])
    pipeline.sync_source(db_session, url_source)
    forward = url_source.content_hash

    scripted_adapter([b, a])
    pipeline.sync_source(db_session, url_source)
    assert url_source.content_hash == forward
    assert url_source.doc_count == 2


# ==========================================================================
# 3. The URL adapter's legible failures
# ==========================================================================


def test_the_v1_limits_are_the_numbers_the_spec_names():
    """Pin the v1 ceilings as literals.

    Every other limit test sizes its payload from the constant, which makes it a
    test of the *mechanism* and not of the number — widen the constant and they
    stay green. This is the one place the numbers themselves are asserted, so a
    change to any of them is a deliberate edit here rather than a silent drift.
    """
    assert uploads.MAX_UPLOAD_BYTES == 10 * 1024 * 1024
    assert uploads.MAX_FILES_PER_PROJECT == 200
    assert sorted(uploads.ALLOWED_EXTENSIONS) == [".md", ".txt"]
    assert MAX_DOC_BYTES == 2 * 1024 * 1024
    assert MAX_REDIRECTS == 3
    assert TIMEOUT_SECONDS == 20.0


def test_non_2xx_surfaces_the_status(db_session, url_source, mock_web, workspace_dir):
    """A 403 says "the site returned 403" — not a stack trace, not "sync failed"."""

    def handler(request):
        return httpx.Response(403, content=b"Forbidden", headers={"content-type": "text/html"})

    mock_web(handler)
    pipeline.sync_source(db_session, url_source)

    assert url_source.status == "error"
    assert "the site returned 403" in url_source.last_error
    assert not _snapshot_dir(url_source).exists()


def test_non_html_content_type_says_so_rather_than_ingesting_a_binary(
    db_session, url_source, mock_web, workspace_dir
):
    """A PDF behind a link is named as such, and its bytes are never stored."""

    def handler(request):
        return httpx.Response(
            200, content=b"%PDF-1.7\n%\xe2\xe3\xcf\xd3", headers={"content-type": "application/pdf"}
        )

    mock_web(handler)
    pipeline.sync_source(db_session, url_source)

    assert url_source.status == "error"
    assert "application/pdf" in url_source.last_error
    assert not _snapshot_dir(url_source).exists()


def test_more_than_three_redirects_is_refused(db_session, url_source, mock_web, workspace_dir):
    """The redirect budget is exactly three, and the message names it.

    One hop *over* the budget with a real page waiting at the end, rather than an
    endless redirect loop: a loop is refused by any finite budget, so it would
    pass just as happily if the limit had been widened to twenty — and this test
    exists to pin the limit, not to prove that loops terminate.
    """
    hops = {"n": 0}

    def handler(request):
        if hops["n"] <= MAX_REDIRECTS:
            hops["n"] += 1
            return httpx.Response(302, headers={"location": f"https://acme.test/hop-{hops['n']}"})
        return httpx.Response(
            200,
            content=REAL_PAGE_HTML.encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    mock_web(handler)
    pipeline.sync_source(db_session, url_source)

    assert url_source.status == "error"
    assert f"redirected more than {MAX_REDIRECTS} times" in url_source.last_error


def test_redirects_within_the_budget_are_followed(db_session, url_source, mock_web, workspace_dir):
    """Negative control: three hops must still land, or the budget is a bug."""
    hops = {"n": 0}

    def handler(request):
        if hops["n"] < MAX_REDIRECTS:
            hops["n"] += 1
            return httpx.Response(301, headers={"location": "https://handbook.acme.test/final"})
        return httpx.Response(
            200,
            content=REAL_PAGE_HTML.encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    mock_web(handler)
    pipeline.sync_source(db_session, url_source)

    assert url_source.status == "synced"
    assert hops["n"] == MAX_REDIRECTS


def test_a_timeout_is_surfaced_in_seconds(db_session, url_source, mock_web, workspace_dir):
    """A hung site reads as "did not respond within 20 seconds", not as a traceback."""

    def handler(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    mock_web(handler)
    pipeline.sync_source(db_session, url_source)

    assert url_source.status == "error"
    assert "did not respond within 20 seconds" in url_source.last_error


def test_a_body_over_two_megabytes_is_abandoned(db_session, url_source, mock_web, workspace_dir):
    """The 2 MB cap is enforced while streaming, and nothing partial is stored."""
    # A literal 2 MB + 1 KB, deliberately not ``MAX_DOC_BYTES + 1024``: sizing the
    # payload from the constant makes the test agree with whatever the constant
    # says, so widening the cap would leave it green.
    oversized = b"<html><body><p>" + b"x" * (2 * 1024 * 1024 + 1024) + b"</p></body></html>"

    def handler(request):
        return httpx.Response(200, content=oversized, headers={"content-type": "text/html"})

    mock_web(handler)
    pipeline.sync_source(db_session, url_source)

    assert url_source.status == "error"
    assert "larger than 2 MB" in url_source.last_error
    assert not _snapshot_dir(url_source).exists()


def test_a_url_source_is_a_single_page_and_never_crawls(
    db_session, url_source, mock_web, workspace_dir
):
    """v1 fetches exactly one document however many links the page carries.

    Asserted on the requests that were actually issued, not on ``doc_count``
    alone: a crawler that fetched forty pages and stored one would satisfy the
    count and fail this.

    Counted per **address**, not per request, because #830 added a
    content-free ``HEAD`` to the same URL after a successful sync — the
    staleness probe that records the version this snapshot was taken at. One
    address is the claim; a second address would be a crawl.
    """
    linked = REAL_PAGE_HTML.replace(
        "<ul>",
        "<ul>" + "".join(f'<li><a href="/page-{i}">Page {i}</a></li>' for i in range(40)),
    )
    seen = mock_web(_serves(linked))
    pipeline.sync_source(db_session, url_source)

    assert url_source.status == "synced"
    assert url_source.doc_count == 1
    assert {str(request.url) for request in seen} == {"https://handbook.acme.test/refunds"}
    # Exactly one document was downloaded; the extra call is the probe's HEAD.
    assert [request.method for request in seen].count("GET") == 1


def test_a_non_http_address_is_refused_before_any_request(
    db_session, url_source, mock_web, workspace_dir
):
    """``file://`` never reaches the network stack — checked before the client opens."""
    url_source.url = "file:///etc/passwd"
    db_session.commit()
    seen = mock_web(_serves(REAL_PAGE_HTML))

    pipeline.sync_source(db_session, url_source)
    assert url_source.status == "error"
    assert "http:// and https://" in url_source.last_error
    assert seen == []


# ==========================================================================
# 4. Partial success is a real state
# ==========================================================================


def test_partial_success_leaves_the_source_synced_with_a_count(
    scripted_adapter, db_session, url_source, workspace_dir
):
    """Two of three documents fail: the source syncs and *says* it lost two.

    The silent drop this prevents is the reason ``BusinessSource`` carries a
    per-source ``last_error`` at all, so the assertion is on all three of:
    the status, the count in the message, and the fact that only the readable
    document reached disk.
    """
    scripted_adapter(
        [
            _html_doc(REAL_PAGE_HTML, path="refunds.html"),
            _html_doc(SPA_SHELL_HTML, path="shipping.html"),
            FetchedDoc(path="returns.html", error="the wiki returned 404 for this page"),
        ]
    )
    pipeline.sync_source(db_session, url_source)

    assert url_source.status == "synced"
    assert "2 documents could not be read" in url_source.last_error
    # Each failure is named, so the user knows *which* pages to go and fix.
    assert "shipping.html" in url_source.last_error
    assert "returns.html" in url_source.last_error

    assert url_source.doc_count == 1
    normalized = _snapshot_dir(url_source) / "normalized"
    assert [p.name for p in normalized.iterdir()] == ["refunds.html.md"]


def test_a_fully_successful_sync_clears_the_previous_error(
    scripted_adapter, db_session, url_source, workspace_dir
):
    """Negative control for the partial case: ``last_error`` is not sticky."""
    scripted_adapter([_html_doc(SPA_SHELL_HTML, path="a.html"), _html_doc(REAL_PAGE_HTML, path="b.html")])
    pipeline.sync_source(db_session, url_source)
    assert url_source.last_error

    scripted_adapter([_html_doc(REAL_PAGE_HTML, path="b.html")])
    pipeline.sync_source(db_session, url_source)
    assert url_source.status == "synced"
    assert url_source.last_error == ""


def test_a_source_with_no_readable_document_is_error_never_synced(
    scripted_adapter, db_session, url_source, workspace_dir
):
    """Every document failing is a source failure — the rule that makes the SPA check bite."""
    scripted_adapter(
        [_html_doc(SPA_SHELL_HTML, path="a.html"), _html_doc(SPA_SHELL_HTML, path="b.html")]
    )
    pipeline.sync_source(db_session, url_source)

    assert url_source.status == "error"
    assert url_source.doc_count == 0
    assert not _snapshot_dir(url_source).exists()


def test_a_failed_resync_leaves_the_previous_snapshot_intact(
    scripted_adapter, db_session, url_source, workspace_dir
):
    """A later failure must not destroy the version an existing test case cites."""
    scripted_adapter([_html_doc(REAL_PAGE_HTML)])
    pipeline.sync_source(db_session, url_source)
    good_hash = url_source.content_hash
    normalized = _snapshot_dir(url_source) / "normalized" / "index.html.md"
    before = normalized.read_text(encoding="utf-8")

    from app.services.business_ingest.base import SourceFetchError

    scripted_adapter(SourceFetchError("the site returned 500"))
    pipeline.sync_source(db_session, url_source)

    assert url_source.status == "error"
    assert "the site returned 500" in url_source.last_error
    assert normalized.read_text(encoding="utf-8") == before
    # The hash still names the version on disk, so attribution stays truthful.
    assert url_source.content_hash == good_hash


# ==========================================================================
# 5. Upload: the v1 limits
# ==========================================================================

_MARKDOWN = "# Refund policy\n\nA customer may request a refund within 30 days.\n"


def _upload(db_session, project, filename, data=None):
    return uploads.ingest_upload(
        db_session,
        project_guid=project.guid,
        project_key=project.name,
        owner_id=None,
        filename=filename,
        data=_MARKDOWN.encode("utf-8") if data is None else data,
    )


def test_upload_accepts_markdown_and_text(db_session, project, workspace_dir):
    """``.md`` and ``.txt`` ingest, raw and normalized both landing on disk."""
    for name in ("policy.md", "policy.txt"):
        row = _upload(db_session, project, name)
        assert row.status == "synced", name
        assert row.doc_count == 1
        root = _snapshot_dir(row)
        assert (root / "raw" / name).read_bytes() == _MARKDOWN.encode("utf-8")
        assert (root / "normalized" / f"{name}.md").exists()


@pytest.mark.parametrize("name", ["handbook.pdf", "handbook.docx", "notes", "archive.zip"])
def test_upload_rejects_every_other_extension(db_session, project, workspace_dir, name):
    """v1 is ``.md``/``.txt`` only, and the refusal says what *is* accepted."""
    with pytest.raises(uploads.UploadRejectedError) as exc:
        _upload(db_session, project, name)
    assert ".md" in str(exc.value) and ".txt" in str(exc.value)
    # Refused before any row was written — not a row parked in `error`.
    assert db_session.query(BusinessSource).count() == 0


def test_upload_over_ten_megabytes_is_rejected(db_session, project, workspace_dir):
    """The per-file ceiling, with a one-byte-under control so it is not off by a mile."""
    over = b"#" * (uploads.MAX_UPLOAD_BYTES + 1)
    with pytest.raises(uploads.UploadRejectedError) as exc:
        _upload(db_session, project, "big.md", over)
    assert "larger than 10 MB" in str(exc.value)
    assert db_session.query(BusinessSource).count() == 0

    at_limit = b"# " + b"a" * (uploads.MAX_UPLOAD_BYTES - 2)
    row = _upload(db_session, project, "big.md", at_limit)
    assert row.status == "synced"


def test_two_hundred_files_per_project_is_the_cap(db_session, project, workspace_dir):
    """The 201st *new* upload is refused; replacing an existing one still works.

    The second half is the control: a cap implemented as "count >= 200, refuse"
    without the replace path would lock a project out of correcting a document
    it already holds.
    """
    for index in range(uploads.MAX_FILES_PER_PROJECT):
        db_session.add(
            BusinessSource(
                project_guid=project.guid,
                project_key=project.name,
                owner_id=None,
                kind="upload",
                title=f"doc-{index}.md",
                url=None,
                status="synced",
            )
        )
    db_session.commit()

    with pytest.raises(uploads.UploadRejectedError) as exc:
        _upload(db_session, project, "one-too-many.md")
    assert str(uploads.MAX_FILES_PER_PROJECT) in str(exc.value)

    replaced = _upload(db_session, project, "doc-7.md")
    assert replaced.status == "synced"
    assert db_session.query(BusinessSource).count() == uploads.MAX_FILES_PER_PROJECT

    # The cap counts this project only — another project is unaffected.
    other = Project(provider_kind="ado", external_id="OTHER", name="Other", active=True)
    db_session.add(other)
    db_session.commit()
    assert _upload(db_session, other, "policy.md").status == "synced"


def test_reupload_replaces_the_snapshot_and_rehashes(db_session, project, workspace_dir):
    """The same filename keeps the row id and moves the hash.

    Keeping the id is what keeps the on-disk directory and everything already
    distilled from it; moving the hash is the staleness signal.
    """
    first = _upload(db_session, project, "policy.md")
    first_id, first_hash = first.id, first.content_hash

    second = _upload(db_session, project, "policy.md", b"# Refund policy\n\nNow 14 days, not 30.\n")
    assert second.id == first_id
    assert second.content_hash != first_hash
    assert db_session.query(BusinessSource).count() == 1

    normalized = _snapshot_dir(second) / "normalized" / "policy.md.md"
    assert "14 days" in normalized.read_text(encoding="utf-8")
    assert "30 days" not in normalized.read_text(encoding="utf-8")


def test_an_empty_upload_is_rejected(db_session, project, workspace_dir):
    """A zero-byte file is a mistake, not a document."""
    with pytest.raises(uploads.UploadRejectedError) as exc:
        _upload(db_session, project, "policy.md", b"")
    assert "empty" in str(exc.value)


def test_an_uploaded_source_cannot_be_re_fetched(db_session, project, workspace_dir):
    """Re-syncing an upload is meaningless, and says so instead of raising KeyError."""
    row = _upload(db_session, project, "policy.md")
    pipeline.sync_source(db_session, row)
    assert row.status == "error"
    assert "no address to re-sync from" in row.last_error


# ==========================================================================
# 6. The HTTP surface (app/routers/business_ingest.py)
# ==========================================================================


def test_upload_endpoint_ingests_and_persists(client, db_session, project, workspace_dir):
    """The multipart door: 201, a synced row, and both artifacts on disk.

    Fields are asserted individually rather than against the whole body — an
    additive change to the response must not fail a test about ingestion (#579).
    """
    response = client.post(
        f"/projects/{project.guid}/business/sources/upload",
        files={"file": ("policy.md", _MARKDOWN.encode("utf-8"), "text/markdown")},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "synced"
    assert body["title"] == "policy.md"
    assert body["docCount"] == 1
    assert body["contentHash"]

    row = db_session.get(BusinessSource, body["id"])
    assert row is not None and row.kind == "upload" and row.project_guid == project.guid
    root = _snapshot_dir(row)
    assert (root / "raw" / "policy.md").read_bytes() == _MARKDOWN.encode("utf-8")
    assert (root / "normalized" / "policy.md.md").exists()


def test_upload_endpoint_refuses_a_pdf_with_a_readable_message(client, db_session, project, workspace_dir):
    """400 and the accepted list — and, the control, no row left behind."""
    response = client.post(
        f"/projects/{project.guid}/business/sources/upload",
        files={"file": ("handbook.pdf", b"%PDF-1.7", "application/pdf")},
    )
    assert response.status_code == 400
    assert ".md" in response.json()["detail"]
    assert db_session.query(BusinessSource).count() == 0


def test_upload_endpoint_404s_for_an_unknown_project(client, workspace_dir):
    """A project that is not the caller's is indistinguishable from one that is missing."""
    response = client.post(
        "/projects/2f6f8b30-0000-4000-8000-000000000000/business/sources/upload",
        files={"file": ("policy.md", _MARKDOWN.encode("utf-8"), "text/markdown")},
    )
    assert response.status_code == 404


def test_sync_endpoint_runs_the_ingestion_in_the_background(
    client, db_session, project, url_source, scripted_adapter, workspace_dir
):
    """202 now, ``synced`` shortly after — the knowledge-build convention.

    Waits on the in-process guard rather than polling the endpoint: ``client``
    shares one session with the test, so hammering it would contend with the
    worker's writes on the same SQLite file and starve the very pass being
    awaited (#641).
    """
    scripted_adapter([_html_doc(REAL_PAGE_HTML)])

    response = client.post(
        f"/projects/{project.guid}/business/sources/{url_source.id}/sync"
    )
    assert response.status_code == 202
    assert response.json()["status"] == "syncing"

    deadline = time.time() + 10
    while pipeline.is_syncing(url_source.id) and time.time() < deadline:
        time.sleep(0.02)
    assert not pipeline.is_syncing(url_source.id), "ingestion thread did not finish"

    db_session.expire_all()
    row = db_session.get(BusinessSource, url_source.id)
    assert row.status == "synced"
    assert row.doc_count == 1
    assert (_snapshot_dir(row) / "normalized" / "index.html.md").exists()


def test_sync_endpoint_refuses_an_upload(client, db_session, project, workspace_dir):
    """An upload has no address, so the endpoint says so instead of starting a thread."""
    row = _upload(db_session, project, "policy.md")
    response = client.post(f"/projects/{project.guid}/business/sources/{row.id}/sync")
    assert response.status_code == 400
    assert "no address to re-sync from" in response.json()["detail"]
    assert not pipeline.is_syncing(row.id)


def test_sync_endpoint_404s_for_a_source_of_another_project(
    client, db_session, project, url_source, workspace_dir
):
    """A source id that exists cannot be reached by naming the wrong project."""
    other = Project(provider_kind="ado", external_id="OTHER", name="Other", active=True)
    db_session.add(other)
    db_session.commit()

    response = client.post(f"/projects/{other.guid}/business/sources/{url_source.id}/sync")
    assert response.status_code == 404
    assert url_source.status == "pending"


# ==========================================================================
# 7. The seams #845 closed: one resolver, one response model
# ==========================================================================
#
# Both endpoints below used to carry their own copy of the #585 GUID-or-name
# bridge and their own `IngestedSourceOut`. Folding each into the one shared
# version (`business_source_service.resolve_project` / `schemas.BusinessSourceOut`)
# is only safe if something fails when the fold is wrong — and nothing in this
# file exercised the *name* branch, the owner filter or the response shape, so
# the refactor could have dropped any of the three and stayed green. These are
# that missing control.


def test_upload_endpoint_resolves_a_project_name_to_its_guid(
    client, db_session, project, workspace_dir
):
    """The path may carry the project NAME; the stored column still holds a GUID.

    The #585 bridge. Pinned by the row, not by the 201: a resolver that returned
    the raw path string would answer 201 just the same and poison
    ``project_guid`` with a name, which every GUID-keyed read then misses.
    """
    response = client.post(
        f"/projects/{project.name}/business/sources/upload",
        files={"file": ("policy.md", _MARKDOWN.encode("utf-8"), "text/markdown")},
    )
    assert response.status_code == 201, response.text

    row = db_session.get(BusinessSource, response.json()["id"])
    assert row.project_guid == project.guid
    assert row.project_key == project.name


def test_sync_endpoint_resolves_a_project_name_to_its_guid(
    client, db_session, project, url_source, scripted_adapter, workspace_dir
):
    """Same bridge on the sync path, where getting it wrong is a 404, not a bad row.

    ``source_or_404`` compares ``row.project_guid`` against whatever the resolver
    returned, so a resolver that handed back the name would make every
    name-addressed sync unreachable.
    """
    scripted_adapter([_html_doc(REAL_PAGE_HTML)])

    response = client.post(
        f"/projects/{project.name}/business/sources/{url_source.id}/sync"
    )
    assert response.status_code == 202, response.text

    deadline = time.time() + 10
    while pipeline.is_syncing(url_source.id) and time.time() < deadline:
        time.sleep(0.02)
    assert not pipeline.is_syncing(url_source.id), "ingestion thread did not finish"
    db_session.expire_all()
    assert db_session.get(BusinessSource, url_source.id).status == "synced"


def test_the_ingestion_endpoints_answer_with_the_shared_source_model(
    client, db_session, project, workspace_dir
):
    """The upload response is a full ``BusinessSourceOut``, not the old projection.

    ``IngestedSourceOut`` carried nine fields and omitted ``projectGuid`` /
    ``projectKey`` / ``connectionId`` / ``excluded``; the SPA types every source
    row as one shape, so a response missing those reads as ``undefined`` in the
    list the moment an upload is rendered beside a link. Asserted field by field
    (never ``==`` a whole body, #579) — including the ingestion-specific ones, so
    this cannot pass by having swapped the model and lost the ingestion state.
    """
    response = client.post(
        f"/projects/{project.guid}/business/sources/upload",
        files={"file": ("policy.md", _MARKDOWN.encode("utf-8"), "text/markdown")},
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["projectGuid"] == project.guid
    assert body["projectKey"] == project.name
    assert body["excluded"] is False
    assert body["connectionId"] is None
    # ...and the ingestion half that used to be this endpoint's whole model.
    assert body["status"] == "synced"
    assert body["docCount"] == 1
    assert body["byteSize"] > 0
    assert body["contentHash"] and body["fetchedAt"]
    assert body["lastError"] == ""


def _make_user(db_session, email: str) -> User:
    """One active member, enough to mint a bearer token for."""
    user = User(
        email=email,
        first_name="Test",
        last_name="User",
        role="member",
        password_hash=auth_service.hash_password("password123"),
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _auth_headers(user: User) -> dict:
    return {"Authorization": f"Bearer {auth_service.create_access_token(user, sid='test-sid')}"}


@pytest.fixture
def auth_on(monkeypatch):
    """Turn the global auth guard on for one test.

    The suite runs with ``auth_required = False`` (``tests/conftest.py``), which
    makes ``current_user`` resolve to ``None`` and every ownership helper a
    passthrough — the #91 bridge. An ownership test that did not flip this would
    be asserting against the bridge and would pass however the guard behaved.
    """
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "auth_required", True)
    yield


def test_the_ingestion_endpoints_refuse_another_users_project(
    client, db_session, auth_on, workspace_dir
):
    """Owner scoping survives the shared resolver — with a negative control.

    The whole point of resolving centrally is that the owner filter cannot drift
    between the two routers, and #817's suite is the only place it was ever
    asserted. Both halves are needed: B is refused, and A (the control) is not,
    so a resolver that 404'd for *everyone* could not pass this.
    """
    user_a = _make_user(db_session, "ingest-a@example.com")
    user_b = _make_user(db_session, "ingest-b@example.com")
    owned = Project(
        provider_kind="ado",
        external_id="ext-owned",
        name="A Product",
        active=True,
        owner_id=user_a.id,
    )
    db_session.add(owned)
    db_session.commit()
    db_session.refresh(owned)

    def upload(headers):
        return client.post(
            f"/projects/{owned.guid}/business/sources/upload",
            files={"file": ("policy.md", _MARKDOWN.encode("utf-8"), "text/markdown")},
            headers=headers,
        )

    assert upload(_auth_headers(user_b)).status_code == 404
    assert db_session.query(BusinessSource).count() == 0, "B's refused upload wrote a row"

    assert upload(_auth_headers(user_a)).status_code == 201
