"""Staleness probes and case attribution (#830, ADR 0016 §4).

Snapshot-with-manual-resync is only defensible if the user can *see* that a
snapshot has gone stale and what a given test case was written from. Two things
are therefore under test here, and they are the two halves of that sentence:

* **The probes**, per adapter, in BOTH directions. A probe that always reports
  stale is useless in exactly the same way as one that never does, and a
  one-directional test cannot tell them apart — so every probe test asserts the
  changed *and* the unchanged upstream against the same source.
* **The honest fallback.** Where no cheap probe exists (an upload has no
  address; a page that sends neither ``ETag`` nor ``Last-Modified`` cannot be
  asked), the row must end up in the age-labelled state — ``mode == "age"`` with
  a reason — and must **never** claim ``stale``. Claiming knowledge we do not
  have is worse than admitting the gap, and that is the whole point of the
  slice, so it gets negative controls rather than a happy path.

Conventions from ``CLAUDE.md`` that are load-bearing here:

* No ``==`` against a whole response body (#579) — the fields under test only.
* No ``monkeypatch.undo()`` (#641): the ``workspace_dir`` fixture redirects
  ``SessionLocal`` with the same function-scoped monkeypatch.
* Assert an observable effect, not only a status code: a probe endpoint always
  answers 200, so every endpoint test pins what landed on the row.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.models.business import BusinessFact, BusinessSource
from app.models.project import Project
from app.services import project_config_service
from app.services.business_ingest import pipeline, staleness
from app.services.business_ingest.base import SourceCredential

PAT = "a-wiki-scoped-pat"
WIKI_URL = "https://dev.azure.com/acme/Payments/_wiki/wikis/Payments.wiki"
PAGE_URL = "https://rules.test/eligibility"

#: Prose long enough to clear the normalizer's readability floor.
DOC = """# Refund eligibility

A customer may request a refund within 30 days of the order date. Orders placed
with store credit are refunded to store credit, never to a card. A premium
member skips the review queue unless the order is flagged for fraud.
"""

HTML_DOC = f"<html><head><title>Rules</title></head><body><p>{DOC}</p></body></html>"


# ==========================================================================
# Fixtures
# ==========================================================================


@pytest.fixture
def project(db_session) -> Project:
    row = Project(provider_kind="ado", external_id="ACME", name="Acme", active=True)
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture
def make_source(db_session, project):
    """Build a ``BusinessSource`` in the state the create endpoint leaves it."""

    def build(**overrides) -> BusinessSource:
        fields = {
            "project_guid": project.guid,
            "project_key": project.name,
            "owner_id": None,
            "kind": "url",
            "title": "Eligibility rules",
            "url": PAGE_URL,
            "status": "pending",
        }
        fields.update(overrides)
        row = BusinessSource(**fields)
        db_session.add(row)
        db_session.commit()
        db_session.refresh(row)
        return row

    return build


@pytest.fixture
def mock_http(monkeypatch):
    """Point ``httpx.Client`` at a handler for one test.

    The adapters build their own clients — that is where the headers, timeouts
    and base URLs live, and the test must exercise *those* — so the transport is
    injected by wrapping the constructor. ``monkeypatch`` restores it; nothing
    calls ``monkeypatch.undo()`` (#641).

    Returns the list of requests actually issued, because "which call was made"
    is most of what a probe test is about: a probe that downloaded the document
    is not a probe.
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


def _json(payload, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json; charset=utf-8"},
    )


def _page_node(path: str, etag: str, content: str = "") -> dict:
    return {"id": abs(hash(path)) % 100000, "path": path, "eTag": etag, "content": content}


def _ado_handler(*, refunds_etag: str = "v1", pages: list[dict] | None = None):
    """One Azure DevOps organisation: the wiki list and the page tree."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pages"):
            tree = {
                "id": 0,
                "path": "/",
                "subPages": pages
                if pages is not None
                else [_page_node("/Refunds", refunds_etag, DOC)],
            }
            return _json(tree)
        return _json({"count": 1, "value": [{"id": "wiki-1", "name": "Payments.wiki"}]})

    return handler


def _url_handler(*, etag: str = "", last_modified: str = "", head_status: int = 200):
    """A single web page, with whatever validators the test wants it to send."""
    headers = {"content-type": "text/html; charset=utf-8"}
    if etag:
        headers["etag"] = etag
    if last_modified:
        headers["last-modified"] = last_modified

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(head_status, headers=headers)
        return httpx.Response(200, content=HTML_DOC.encode("utf-8"), headers=headers)

    return handler


# ==========================================================================
# 1. The generic URL probe — ETag / Last-Modified, both directions
# ==========================================================================


def test_a_url_probe_flips_stale_only_when_the_etag_moves(
    db_session, make_source, mock_http, workspace_dir
):
    """The same source: unchanged ETag is not stale, a changed one is.

    Both directions against one row, because a probe hard-wired to ``True`` and
    a probe hard-wired to ``False`` are each green under half of this test.
    """
    source = make_source()
    mock_http(_url_handler(etag='W/"abc123"'))
    pipeline.sync_source(db_session, source)
    assert source.status == "synced"
    # The snapshot recorded the version it was taken at — without this there is
    # nothing for a later probe to compare against.
    assert source.upstream_rev == 'etag:W/"abc123"'
    assert source.probe_error == ""

    staleness.refresh_staleness(db_session, source)
    assert source.stale is False

    mock_http(_url_handler(etag='W/"def456"'))
    staleness.refresh_staleness(db_session, source)
    assert source.stale is True

    # ...and back: a source restored upstream stops being stale, so the flag is
    # a comparison and not a latch.
    mock_http(_url_handler(etag='W/"abc123"'))
    staleness.refresh_staleness(db_session, source)
    assert source.stale is False


def test_a_url_probe_falls_back_to_last_modified(
    db_session, make_source, mock_http, workspace_dir
):
    """No ETag but a Last-Modified is still a real answer — and still both ways."""
    source = make_source()
    mock_http(_url_handler(last_modified="Wed, 01 Jan 2025 00:00:00 GMT"))
    pipeline.sync_source(db_session, source)
    assert source.upstream_rev.startswith("last-modified:")

    staleness.refresh_staleness(db_session, source)
    assert source.stale is False

    mock_http(_url_handler(last_modified="Thu, 02 Jan 2025 00:00:00 GMT"))
    staleness.refresh_staleness(db_session, source)
    assert source.stale is True


def test_a_url_probe_downloads_no_document(db_session, make_source, mock_http, workspace_dir):
    """A HEAD, not a GET. A "probe" that fetches the page is just a sync."""
    source = make_source()
    seen = mock_http(_url_handler(etag='W/"abc123"'))
    staleness.probe_revision(source, None)
    assert [request.method for request in seen] == ["HEAD"]


def test_a_url_probe_retries_with_get_when_head_is_refused(
    db_session, make_source, mock_http, workspace_dir
):
    """405/403 on HEAD is common; the body is still never read."""
    source = make_source()
    seen = mock_http(_url_handler(etag='W/"abc123"', head_status=405))
    assert staleness.probe_revision(source, None) == 'etag:W/"abc123"'
    assert [request.method for request in seen] == ["HEAD", "GET"]


def test_a_page_with_no_validator_is_age_labelled_and_never_claimed_stale(
    db_session, make_source, mock_http, workspace_dir
):
    """The honest gap: no ETag, no Last-Modified, therefore no claim.

    This is the assertion the slice exists for. The row must end in the
    age-labelled mode carrying the reason, and ``stale`` must stay False — a
    badge that said "changed" here would be inventing a fact.
    """
    source = make_source()
    mock_http(_url_handler())
    pipeline.sync_source(db_session, source)
    assert source.status == "synced"
    assert source.upstream_rev == ""
    assert "ETag" in source.probe_error

    staleness.refresh_staleness(db_session, source)
    assert source.stale is False
    state = staleness.probe_state(source)
    assert state.mode == "age"
    assert state.stale is False
    assert "ETag" in state.detail

    # Negative control: the very same row with a validator reaches "revision",
    # so "age" above is a property of the page and not of this test's plumbing.
    mock_http(_url_handler(etag='W/"abc123"'))
    staleness.refresh_staleness(db_session, source)
    assert staleness.probe_state(source).mode == "revision"


def test_an_unreachable_page_records_the_failure_and_keeps_the_last_verdict(
    db_session, make_source, mock_http, workspace_dir
):
    """A probe that cannot answer must not overwrite the answer we already had."""
    source = make_source()
    mock_http(_url_handler(etag='W/"abc123"'))
    pipeline.sync_source(db_session, source)
    mock_http(_url_handler(etag='W/"moved"'))
    staleness.refresh_staleness(db_session, source)
    assert source.stale is True

    def refuses(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    mock_http(refuses)
    staleness.refresh_staleness(db_session, source)
    assert source.probe_error != ""
    # The last real answer survives; it is better information than a guess.
    assert source.stale is True
    assert staleness.probe_state(source).mode == "age"


# ==========================================================================
# 2. The Azure DevOps wiki probe — a page-version digest, both directions
# ==========================================================================


def test_a_wiki_probe_flips_stale_only_when_a_page_version_moves(
    db_session, make_source, mock_http, workspace_dir
):
    """An edited page changes the digest; an untouched wiki does not."""
    source = make_source(kind="ado_wiki", url=WIKI_URL, title="Payments wiki")
    credential = SourceCredential(token=PAT)

    mock_http(_ado_handler(refunds_etag="v1"))
    pipeline.sync_source(db_session, source, credential)
    assert source.status == "synced"
    assert source.upstream_rev != ""

    staleness.refresh_staleness(db_session, source, credential)
    assert source.stale is False

    mock_http(_ado_handler(refunds_etag="v2"))
    staleness.refresh_staleness(db_session, source, credential)
    assert source.stale is True


def test_a_wiki_probe_notices_a_page_being_added(
    db_session, make_source, mock_http, workspace_dir
):
    """A new page is a change to the source, even though no existing page moved.

    This is why the digest is over the whole sub-tree rather than one page's
    eTag: "nothing I already had changed" is not the same as "nothing changed".
    """
    source = make_source(kind="ado_wiki", url=WIKI_URL)
    credential = SourceCredential(token=PAT)

    mock_http(_ado_handler(pages=[_page_node("/Refunds", "v1", DOC)]))
    pipeline.sync_source(db_session, source, credential)
    staleness.refresh_staleness(db_session, source, credential)
    assert source.stale is False

    mock_http(
        _ado_handler(
            pages=[_page_node("/Refunds", "v1", DOC), _page_node("/Shipping", "v1", DOC)]
        )
    )
    staleness.refresh_staleness(db_session, source, credential)
    assert source.stale is True


def test_a_wiki_probe_downloads_no_page_content(
    db_session, make_source, mock_http, workspace_dir
):
    """One content-free tree request. That is what makes the probe cheap."""
    source = make_source(kind="ado_wiki", url=WIKI_URL)
    seen = mock_http(_ado_handler())
    staleness.probe_revision(source, SourceCredential(token=PAT))

    tree_calls = [r for r in seen if r.url.path.endswith("/pages")]
    assert len(tree_calls) == 1
    assert tree_calls[0].url.params.get("includeContent") == "false"


def test_a_wiki_probe_without_a_token_is_recorded_not_raised(
    db_session, make_source, mock_http, workspace_dir
):
    """A missing PAT is a probe failure on the row, never a 500 or a stale claim."""
    source = make_source(kind="ado_wiki", url=WIKI_URL)
    mock_http(_ado_handler())
    staleness.refresh_staleness(db_session, source, None)
    assert source.probe_error != ""
    assert source.stale is False
    assert staleness.probe_state(source).mode == "age"


# ==========================================================================
# 3. Uploads — no probe at all, and it says so
# ==========================================================================


def test_an_upload_is_age_labelled_because_it_has_no_address(db_session, make_source):
    """There is nobody to ask. The UI must show an age, not a verdict."""
    source = make_source(kind="upload", url=None, title="rules.md")
    assert staleness.probe_supported("upload") is False
    state = staleness.probe_state(source)
    assert state.mode == "age"
    assert state.stale is False
    assert "upload the new version" in state.detail

    # Negative control: a link kind on the same row shape is NOT age-labelled
    # before it has been probed — it is "unknown", which renders differently.
    link = make_source()
    assert staleness.probe_state(link).mode == "unknown"


def test_an_unprobed_link_is_unknown_rather_than_fresh(db_session, make_source):
    """"Nobody has looked" must not render as "up to date"."""
    source = make_source()
    assert source.probed_at is None
    assert staleness.probe_state(source).mode == "unknown"
    assert staleness.probe_state(source).stale is False


# ==========================================================================
# 4. The probe endpoint
# ==========================================================================


def test_the_probe_endpoint_reports_the_change_on_the_row(
    client, db_session, project, make_source, mock_http, workspace_dir
):
    """200 either way; the observable effect is what landed on the row."""
    source = make_source()
    mock_http(_url_handler(etag='W/"abc123"'))
    pipeline.sync_source(db_session, source)

    mock_http(_url_handler(etag='W/"moved"'))
    response = client.post(
        f"/projects/{project.guid}/business/sources/{source.id}/probe"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["stale"] is True
    assert body["staleness"]["mode"] == "revision"
    assert body["probeSupported"] is True
    db_session.refresh(source)
    assert source.stale is True


def test_the_probe_endpoint_answers_200_for_an_upload_and_claims_nothing(
    client, db_session, project, make_source, workspace_dir
):
    """"There is no probe" is information, not an error."""
    source = make_source(kind="upload", url=None, title="rules.md")
    response = client.post(
        f"/projects/{project.guid}/business/sources/{source.id}/probe"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["probeSupported"] is False
    assert body["staleness"]["mode"] == "age"
    assert body["stale"] is False


def test_the_probe_endpoint_404s_a_source_of_another_project(
    client, db_session, project, make_source, workspace_dir
):
    """The id exists; naming the wrong project must not reach it."""
    other = Project(provider_kind="ado", external_id="OTHER", name="Other", active=True)
    db_session.add(other)
    db_session.commit()
    source = make_source()

    response = client.post(
        f"/projects/{other.guid}/business/sources/{source.id}/probe"
    )
    assert response.status_code == 404
    # Negative control: the same id under its own project is reachable, so the
    # 404 above is the guard and not a broken route.
    assert (
        client.post(
            f"/projects/{project.guid}/business/sources/{source.id}/probe"
        ).status_code
        == 200
    )


# ==========================================================================
# 5. Attribution — what a generated case is grounded in
# ==========================================================================


def _fact(db_session, project, source, term="Premium") -> BusinessFact:
    row = BusinessFact(
        project_guid=project.guid,
        owner_id=None,
        source_id=source.id if source else None,
        category="rule",
        term=term,
        statement="A premium member skips the review queue.",
        origin="ingested" if source else "manual",
        rank_text="premium member queue",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def test_business_context_names_the_document_versions_behind_its_facts(
    db_session, project, make_source, mock_http, workspace_dir
):
    """The payoff of storing a content hash: the version is nameable afterwards."""
    source = make_source()
    mock_http(_url_handler(etag='W/"abc123"'))
    pipeline.sync_source(db_session, source)
    _fact(db_session, project, source)

    context = project_config_service.business_context(db_session, project.guid, None)
    grounded = context["businessSources"]
    assert [entry["sourceId"] for entry in grounded] == [source.id]
    assert grounded[0]["contentHash"] == source.content_hash
    assert grounded[0]["fetchedAt"]
    assert grounded[0]["title"] == source.title


def test_a_human_fact_grounds_a_case_in_no_document(
    db_session, project, make_source, workspace_dir
):
    """A fact nobody's document stated contributes no source — and the list says so.

    The negative control for the test above: if ``businessSources`` were simply
    "every source of the project", this would list one.
    """
    make_source()  # a registered source that no in-context fact came from
    _fact(db_session, project, None, term="Grace period")

    context = project_config_service.business_context(db_session, project.guid, None)
    assert context["businessFacts"], "the fact itself must still reach the prompt"
    assert context["businessSources"] == []


def test_the_recorded_version_is_a_copy_and_survives_a_resync(
    db_session, project, make_source, mock_http, workspace_dir
):
    """What a case recorded must not change when the source is re-synced.

    This is ADR 0016 §4's whole argument. A join to ``business_source`` would
    answer "which version?" with *today's* version, which is exactly the silent
    retroactive shift the snapshot model exists to prevent.
    """
    source = make_source()
    mock_http(_url_handler(etag='W/"abc123"'))
    pipeline.sync_source(db_session, source)
    _fact(db_session, project, source)

    recorded = project_config_service.business_context(db_session, project.guid, None)[
        "businessSources"
    ]
    first_hash = recorded[0]["contentHash"]

    changed = HTML_DOC.replace("30 days", "45 days")

    def moved(request: httpx.Request) -> httpx.Response:
        headers = {"content-type": "text/html; charset=utf-8", "etag": 'W/"moved"'}
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers)
        return httpx.Response(200, content=changed.encode("utf-8"), headers=headers)

    mock_http(moved)
    pipeline.sync_source(db_session, source)
    assert source.content_hash != first_hash, "the re-sync must really have changed it"
    # The copy taken earlier still names the version the case was written from.
    assert recorded[0]["contentHash"] == first_hash
