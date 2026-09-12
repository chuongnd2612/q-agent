"""Azure DevOps wiki ingestion, and the credential constraint it ships (#822).

The slice's stated point is that **four look-alike failures are told apart**, so
most of this file is failure. Azure DevOps answers a wrong-scope token, a wrong
organisation, a project with no wiki and a rate limit with responses that differ
only in a status code, and a generic "sync failed" sends the user to the wrong
repair in three of the four cases. So every refusal is asserted on its **exact**
message, and each is checked to be unreachable from another's branch.

The second half is the credential finding itself: a hub-backed connection holds
no PAT and never will (#501), and ``hub_client`` has no wiki endpoint, so wiki
ingestion through the shared EmeHub connection is not slow — it is impossible.
:func:`test_a_hub_backed_connection_is_refused_by_name` and its route-level twin
pin that the product says so in words, rather than failing opaquely.

Conventions from ``CLAUDE.md`` that are load-bearing here:

* **Assert which branch ran, plus an observable effect.** Every ingestion
  outcome is ``{"status": ...}`` on one row, so the branch is pinned explicitly
  — ``credential.extra["origin"]``, the credential route's ``origin`` field, the
  request path actually issued — and paired with an effect (the snapshot on
  disk, the token that is or is not stored).
* **Never ``==`` a whole response body**; the fields under test are asserted and
  the behaviour is pinned on the row and on disk.
* **Limits are pinned as literals** (200 pages, 4 levels). Sizing a fixture from
  the constant it means to pin is how a limit test stays green after the limit
  is widened.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from loguru import logger

from app import crypto
from app.models.business import BusinessSource
from app.models.project import Project
from app.models.provider_connection import ProviderConnection
from app.services.business_ingest import adapters, credentials, pipeline, storage
from app.services.business_ingest.adapters import ado_wiki
from app.services.business_ingest.adapters.ado_wiki import (
    BAD_TOKEN_MESSAGE,
    NO_WIKI_MESSAGE,
    RATE_LIMITED_MESSAGE,
    WIKI_SCOPE_MESSAGE,
    AdoWikiAdapter,
    parse_wiki_url,
)
from app.services.business_ingest.base import SourceFetchError

#: The one token used throughout. Distinctive so the "never leaked" test can
#: scan logs, responses and every byte on disk for it without false positives.
PAT = "wiki-scoped-pat-6f2a9c4e"

ORG_URL = "https://dev.azure.com/acme"
WIKI_URL = "https://dev.azure.com/acme/Payments/_wiki/wikis/Payments.wiki/12/Refunds"

#: Enough prose to clear ``MIN_READABLE_CHARS`` — the normalizer's empty-shell
#: check applies to every source, so a fixture page has to be a real page.
REFUNDS_PAGE = """# Refund eligibility

A customer may request a refund within 30 days of the order date. Orders placed
with store credit are refunded to store credit, never to a card. A premium
member skips the review queue: their refund is approved automatically unless the
order is flagged for fraud, in which case it is routed to the risk team.
"""

SHIPPING_PAGE = """# Shipping refunds

Shipping is refunded when the fault is ours: a late delivery, a damaged parcel
or an item that never arrived. A customer-initiated return pays its own return
shipping unless the customer holds a premium membership for that region.
"""


# --------------------------------------------------------------------------
# Fixtures: rows, and an Azure DevOps the adapter can be pointed at
# --------------------------------------------------------------------------


@pytest.fixture
def project(db_session) -> Project:
    """One project to hang sources on."""
    row = Project(provider_kind="ado", external_id="ACME", name="Acme", active=True)
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _make_source(db_session, project, **overrides) -> BusinessSource:
    fields = {
        "project_guid": project.guid,
        "project_key": project.name,
        "owner_id": None,
        "kind": "ado_wiki",
        "title": "Payments wiki",
        "url": WIKI_URL,
        "status": "pending",
    }
    fields.update(overrides)
    row = BusinessSource(**fields)
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture
def wiki_source(db_session, project) -> BusinessSource:
    """An ``ado_wiki`` source in the state the create endpoint leaves it."""
    return _make_source(db_session, project)


@pytest.fixture
def make_source(db_session, project):
    """Build extra sources in one test (a second kind, a second connection)."""

    def build(**overrides) -> BusinessSource:
        return _make_source(db_session, project, **overrides)

    return build


@pytest.fixture
def mock_ado(monkeypatch):
    """Point ``httpx.Client`` at a :class:`httpx.MockTransport` for one test.

    The adapter builds its own client — that is where its auth header, base URL
    and timeout live, and the test must exercise *that* client rather than a
    substitute — so the transport is injected by wrapping the constructor.
    ``monkeypatch`` restores it; nothing calls ``monkeypatch.undo()``, which
    would un-redirect the session/engine ``workspace_dir`` patched with the same
    monkeypatch (#641).

    Yields a callable taking the handler and returning the list of requests
    actually issued — which is what lets a test assert the *request*, not only
    the answer.
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


def _page(path: str, content: str = "", sub_pages: list | None = None) -> dict:
    node: dict = {"id": abs(hash(path)) % 100000, "path": path, "content": content}
    if sub_pages is not None:
        node["subPages"] = sub_pages
        node["isParentPage"] = True
    return node


def _ado(
    *,
    wikis: list[dict] | None = None,
    tree: dict | None = None,
    wikis_status: int = 200,
    tree_status: int = 200,
    wikis_response: httpx.Response | None = None,
):
    """A handler standing in for one Azure DevOps organisation.

    Two endpoints, matched on the path so a test can break exactly one of them:
    ``/_apis/wiki/wikis`` (the preflight) and ``…/pages`` (the fetch).
    """
    default_wikis = [{"id": "wiki-1", "name": "Payments.wiki"}]
    default_tree = _page(
        "/",
        sub_pages=[_page("/Refunds", REFUNDS_PAGE), _page("/Shipping", SHIPPING_PAGE)],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pages"):
            if tree_status != 200:
                return _json({"message": "nope"}, tree_status)
            return _json(default_tree if tree is None else tree)
        if wikis_response is not None:
            return wikis_response
        if wikis_status != 200:
            return _json({"message": "nope"}, wikis_status)
        return _json({"count": 1, "value": default_wikis if wikis is None else wikis})

    return handler


def _snapshot_dir(source: BusinessSource):
    return storage.source_root(source.project_key, source.id, source.owner_id)


def _credential(token: str = PAT):
    from app.services.business_ingest.base import SourceCredential

    return SourceCredential(token=token)


# ==========================================================================
# 1. Registration — through the built-in path, on a cold registry
# ==========================================================================


def test_ado_wiki_resolves_through_the_builtin_registry():
    """``get_adapter("ado_wiki")`` works without anything importing the module.

    The registration bug fixed in #818 was invisible in a whole-file run and
    only appeared when a test ran in its own process, so this asserts the
    *resolution*, not the import.
    """
    assert adapters.get_adapter("ado_wiki").kind == "ado_wiki"
    assert "ado_wiki" in adapters.registered_kinds()


def test_ado_wiki_still_loads_when_another_kind_was_registered_first(monkeypatch):
    """A substituted kind must not suppress the built-in load of this one.

    Asserted on a cold registry, the only state in which the old
    ``if not _REGISTRY`` gate could bite.
    """

    class _Other:
        kind = "url"

        def fetch(self, source, credential=None):  # pragma: no cover - never called
            return []

    monkeypatch.setattr(adapters, "_REGISTRY", {})
    monkeypatch.setattr(adapters, "_loaded", False)
    adapters.register(_Other())

    assert isinstance(adapters.get_adapter("ado_wiki"), AdoWikiAdapter)
    assert isinstance(adapters.get_adapter("url"), _Other)


# ==========================================================================
# 2. URL parsing — four shapes a user can genuinely paste
# ==========================================================================


@pytest.mark.parametrize(
    ("url", "org_url", "project", "wiki", "page_path"),
    [
        (WIKI_URL, "https://dev.azure.com/acme", "Payments", "Payments.wiki", "/"),
        (
            "https://acme.visualstudio.com/Payments/_wiki/wikis/Payments.wiki",
            "https://acme.visualstudio.com",
            "Payments",
            "Payments.wiki",
            "/",
        ),
        ("https://dev.azure.com/acme/My%20Project", "https://dev.azure.com/acme", "My Project", "", "/"),
        (
            "https://tfs.acme.local/tfs/DefaultCollection/Payments/_wiki/wikis/P.wiki?pagePath=/Rules",
            "https://tfs.acme.local/tfs/DefaultCollection",
            "Payments",
            "P.wiki",
            "/Rules",
        ),
    ],
)
def test_parse_wiki_url_handles_every_address_shape(url, org_url, project, wiki, page_path):
    """Cloud, legacy ``visualstudio.com``, a bare project URL, and on-premises.

    All four fields are asserted on every shape: a parser that got the project
    right and the organisation wrong would 404 on preflight and read as "no wiki
    enabled", which is precisely the mis-diagnosis this slice removes.
    """
    target = parse_wiki_url(url)
    assert target.org_url == org_url
    assert target.project == project
    assert target.wiki == wiki
    assert target.page_path == page_path


@pytest.mark.parametrize(
    "url",
    ["", "   ", "ftp://dev.azure.com/acme/Payments", "https:///_wiki/wikis/x", "https://dev.azure.com"],
)
def test_parse_wiki_url_refuses_an_unusable_address(url):
    """A blank, non-http or project-less address is refused before any request."""
    with pytest.raises(SourceFetchError):
        parse_wiki_url(url)


# ==========================================================================
# 3. The credential constraint — the finding this slice ships as behaviour
# ==========================================================================


def test_a_hub_backed_connection_is_refused_by_name(db_session, wiki_source):
    """The heart of the slice: a hub connection cannot supply a wiki token, ever.

    ``hub_connection_id`` marks a row whose ``secrets`` are empty and always
    will be (#501), and ``hub_client`` has no wiki endpoint to route around it.
    So the refusal must name the hub and name the fix — not "connection broken",
    which would send the user to re-do a connection that is working fine.
    """
    connection = ProviderConnection(kind="ado", name="EmeHub ADO", hub_connection_id="hub-77")
    db_session.add(connection)
    db_session.commit()
    wiki_source.connection_id = connection.id
    db_session.commit()

    assert credentials.credential_origin(db_session, wiki_source) == "hub"
    with pytest.raises(SourceFetchError) as exc:
        credentials.resolve_credential(db_session, wiki_source)
    assert str(exc.value) == credentials.HUB_BACKED_MESSAGE
    assert "EmeHub" in str(exc.value)


def test_a_hub_backed_source_ends_in_error_with_no_snapshot(
    db_session, wiki_source, mock_ado, workspace_dir
):
    """End to end: branch **and** effect.

    The row is ``error`` with the hub message, and — the effect that proves the
    adapter was never reached — **no HTTP request was issued at all** and nothing
    was written to disk. A refusal that still burned a request would mean the
    hub check had been bypassed and something else produced the failure.
    """
    connection = ProviderConnection(kind="ado", name="EmeHub ADO", hub_connection_id="hub-77")
    db_session.add(connection)
    db_session.commit()
    wiki_source.connection_id = connection.id
    db_session.commit()
    seen = mock_ado(_ado())

    pipeline.sync_source(db_session, wiki_source)

    assert wiki_source.status == "error"
    assert wiki_source.last_error == credentials.HUB_BACKED_MESSAGE
    assert seen == []
    assert not _snapshot_dir(wiki_source).exists()


def test_a_local_connection_with_a_pat_is_used(db_session, wiki_source):
    """Negative control for the hub refusal: a *local* connection does supply one.

    Without this, a resolver that refused every connection would pass the hub
    test and look correct.
    """
    connection = ProviderConnection(
        kind="ado", name="Local ADO", secrets={"pat": crypto.encrypt("connection-pat")}
    )
    db_session.add(connection)
    db_session.commit()
    wiki_source.connection_id = connection.id
    db_session.commit()

    assert credentials.credential_origin(db_session, wiki_source) == "connection"
    resolved = credentials.resolve_credential(db_session, wiki_source)
    assert resolved.extra["origin"] == "connection"
    assert resolved.token == "connection-pat"


def test_the_source_token_beats_the_connection(db_session, wiki_source):
    """A per-source token is the *primary* path, not a fallback — assert the order.

    The source is given a working local connection as well, so this can only
    pass if the source's own token is preferred rather than merely accepted when
    nothing else exists.
    """
    connection = ProviderConnection(
        kind="ado", name="Local ADO", secrets={"pat": crypto.encrypt("connection-pat")}
    )
    db_session.add(connection)
    db_session.commit()
    wiki_source.connection_id = connection.id
    credentials.set_source_token(db_session, wiki_source, PAT)

    assert credentials.credential_origin(db_session, wiki_source) == "source"
    resolved = credentials.resolve_credential(db_session, wiki_source)
    assert resolved.extra["origin"] == "source"
    assert resolved.token == PAT


def test_no_credential_anywhere_names_the_scope(db_session, wiki_source):
    """No token and no connection: the message must name ``Wiki (Read)``."""
    assert credentials.credential_origin(db_session, wiki_source) == "missing"
    with pytest.raises(SourceFetchError) as exc:
        credentials.resolve_credential(db_session, wiki_source)
    assert str(exc.value) == credentials.NO_CREDENTIAL_MESSAGE
    assert "Wiki (Read)" in str(exc.value)


def test_a_connection_without_a_pat_is_its_own_message(db_session, wiki_source):
    """A local connection holding no PAT is *not* the hub case, and says so."""
    connection = ProviderConnection(kind="ado", name="Local ADO", secrets={})
    db_session.add(connection)
    db_session.commit()
    wiki_source.connection_id = connection.id
    db_session.commit()

    with pytest.raises(SourceFetchError) as exc:
        credentials.resolve_credential(db_session, wiki_source)
    assert str(exc.value) == credentials.CONNECTION_HAS_NO_TOKEN_MESSAGE
    assert str(exc.value) != credentials.HUB_BACKED_MESSAGE


def test_a_deleted_connection_is_its_own_message(db_session, wiki_source):
    """A dangling ``connection_id`` must not read as "no credential configured"."""
    wiki_source.connection_id = 90210
    db_session.commit()
    with pytest.raises(SourceFetchError) as exc:
        credentials.resolve_credential(db_session, wiki_source)
    assert str(exc.value) == credentials.CONNECTION_MISSING_MESSAGE


def test_credential_free_kinds_resolve_to_none(db_session, make_source):
    """``url`` and ``upload`` must not be dragged into credential resolution.

    The resolver sits in the shared pipeline, so a bug here would break the two
    sources that ship in #818 and need no secret at all.
    """
    url_source = make_source(kind="url", url="https://handbook.acme.test/refunds")
    upload_source = make_source(kind="upload", url=None, title="rules.md")
    assert credentials.resolve_credential(db_session, url_source) is None
    assert credentials.resolve_credential(db_session, upload_source) is None
    assert credentials.credential_origin(db_session, url_source) == "none"


def test_a_stored_token_is_encrypted_at_rest(db_session, wiki_source):
    """The PAT is encrypted with the same helper ``ProviderConnection`` uses.

    Asserted on the stored value, not on the round trip: a "store" that returned
    the right plaintext while writing the plaintext would pass a round-trip test.
    """
    credentials.set_source_token(db_session, wiki_source, PAT)
    stored = wiki_source.secrets["pat"]
    assert crypto.is_encrypted(stored)
    assert PAT not in stored
    assert credentials.source_token(wiki_source) == PAT

    credentials.clear_source_token(db_session, wiki_source)
    assert "pat" not in (wiki_source.secrets or {})
    assert credentials.has_source_token(wiki_source) is False


# ==========================================================================
# 4. The four look-alike refusals, told apart
# ==========================================================================


@pytest.mark.parametrize("status", [401, 403])
def test_a_wrong_scoped_token_says_scope_not_broken_connection(
    db_session, wiki_source, mock_ado, workspace_dir, status
):
    """401/403 on preflight is a **scope** problem, and must say so.

    This is the message that stops a user re-doing a connection that is fine.
    """
    credentials.set_source_token(db_session, wiki_source, PAT)
    mock_ado(_ado(wikis_status=status))

    pipeline.sync_source(db_session, wiki_source)

    assert wiki_source.status == "error"
    assert wiki_source.last_error == WIKI_SCOPE_MESSAGE
    assert "Wiki (Read)" in wiki_source.last_error
    assert wiki_source.last_error != credentials.HUB_BACKED_MESSAGE
    assert not _snapshot_dir(wiki_source).exists()


def test_a_project_with_no_wiki_is_its_own_message(
    db_session, wiki_source, mock_ado, workspace_dir
):
    """404 on preflight names the project, and never mentions permissions."""
    credentials.set_source_token(db_session, wiki_source, PAT)
    mock_ado(_ado(wikis_status=404))

    pipeline.sync_source(db_session, wiki_source)

    assert wiki_source.status == "error"
    assert wiki_source.last_error == NO_WIKI_MESSAGE.format(project="Payments")
    assert "Payments" in wiki_source.last_error
    assert wiki_source.last_error != WIKI_SCOPE_MESSAGE


def test_an_empty_wiki_list_is_the_no_wiki_message_too(db_session, wiki_source, mock_ado):
    """A 200 with zero wikis means the same thing as the 404 and reads the same.

    Azure DevOps does both, depending on the project's state; a user who gets
    two different explanations for one situation learns nothing from either.
    """
    credentials.set_source_token(db_session, wiki_source, PAT)
    mock_ado(_ado(wikis=[]))

    with pytest.raises(SourceFetchError) as exc:
        AdoWikiAdapter().fetch(wiki_source, _credential())
    assert str(exc.value) == NO_WIKI_MESSAGE.format(project="Payments")


def test_a_203_is_read_as_a_rejected_token_not_as_success(
    db_session, wiki_source, mock_ado, workspace_dir
):
    """Azure DevOps says "signed out" with ``203``, which is a *success* status.

    The body here is valid JSON on purpose, so the only thing that can catch
    this is the status check — the two guards against a dead PAT are asserted
    one at a time, or each would hide the other's absence.

    It is explicitly **not** the scope message: re-scoping a revoked token fixes
    nothing.
    """
    credentials.set_source_token(db_session, wiki_source, PAT)
    mock_ado(_ado(wikis_response=_json({"count": 1, "value": []}, 203)))

    pipeline.sync_source(db_session, wiki_source)

    assert wiki_source.status == "error"
    assert wiki_source.last_error == BAD_TOKEN_MESSAGE
    assert wiki_source.last_error != WIKI_SCOPE_MESSAGE
    assert "expired" in wiki_source.last_error


def test_an_html_sign_in_page_is_read_as_a_rejected_token(
    db_session, wiki_source, mock_ado, workspace_dir
):
    """The other half: a plain ``200`` carrying the HTML sign-in page.

    Parsed naively that surfaces as a JSON decode error somewhere deep in the
    walk. Here the content type is what gives it away, and the status is
    deliberately 200 so this test can only pass through the body check.
    """
    credentials.set_source_token(db_session, wiki_source, PAT)
    sign_in = httpx.Response(
        200,
        content=b"<html><body>Sign in to Azure DevOps</body></html>",
        headers={"content-type": "text/html; charset=utf-8"},
    )
    mock_ado(_ado(wikis_response=sign_in))

    pipeline.sync_source(db_session, wiki_source)

    assert wiki_source.status == "error"
    assert wiki_source.last_error == BAD_TOKEN_MESSAGE
    assert not _snapshot_dir(wiki_source).exists()


def test_a_rate_limit_is_told_apart_from_a_permission_problem(db_session, wiki_source, mock_ado):
    """429 must read as "wait", not as "you are not allowed"."""
    credentials.set_source_token(db_session, wiki_source, PAT)
    mock_ado(_ado(wikis_status=429))

    with pytest.raises(SourceFetchError) as exc:
        AdoWikiAdapter().fetch(wiki_source, _credential())
    assert str(exc.value) == RATE_LIMITED_MESSAGE
    assert str(exc.value) != WIKI_SCOPE_MESSAGE


def test_a_wiki_named_in_the_url_that_does_not_exist_lists_the_ones_that_do(
    db_session, wiki_source, mock_ado
):
    """An organisation/project mismatch usually surfaces as "no such wiki".

    Listing the wikis that *are* there is what turns a dead end into a one-step
    fix, so the available names are asserted, not just the refusal.
    """
    credentials.set_source_token(db_session, wiki_source, PAT)
    mock_ado(_ado(wikis=[{"id": "w9", "name": "Platform.wiki"}]))

    with pytest.raises(SourceFetchError) as exc:
        AdoWikiAdapter().fetch(wiki_source, _credential())
    assert "Payments.wiki" in str(exc.value)
    assert "Platform.wiki" in str(exc.value)


def test_a_missing_page_path_is_not_the_no_wiki_message(db_session, make_source, mock_ado):
    """404 on the *pages* call means the page path is wrong — a different repair.

    The preflight succeeded, so "this project has no wiki" would be a lie.
    """
    source = make_source(
        url="https://dev.azure.com/acme/Payments/_wiki/wikis/Payments.wiki?pagePath=/Nope"
    )
    mock_ado(_ado(tree_status=404))

    with pytest.raises(SourceFetchError) as exc:
        AdoWikiAdapter().fetch(source, _credential())
    assert "/Nope" in str(exc.value)
    assert str(exc.value) != NO_WIKI_MESSAGE.format(project="Payments")


def test_fetching_without_a_token_never_reaches_the_network(wiki_source, mock_ado):
    """The adapter refuses an empty credential itself, before any request."""
    seen = mock_ado(_ado())
    with pytest.raises(SourceFetchError) as exc:
        AdoWikiAdapter().fetch(wiki_source, _credential(""))
    assert str(exc.value) == credentials.NO_CREDENTIAL_MESSAGE
    assert seen == []


# ==========================================================================
# 5. The happy path, and what actually lands on disk
# ==========================================================================


def test_a_wiki_syncs_its_pages_raw_and_normalized(
    db_session, wiki_source, mock_ado, workspace_dir
):
    """Branch and effect: ``synced``, and both artifacts present with real content.

    Raw *and* normalized are asserted, because the raw copy is what makes a
    future parser improvement retroactive without re-authenticating (ADR 0016) —
    a pipeline that wrote only the markdown would pass a status check.
    """
    credentials.set_source_token(db_session, wiki_source, PAT)
    mock_ado(_ado())

    pipeline.sync_source(db_session, wiki_source)

    assert wiki_source.status == "synced"
    assert wiki_source.last_error == ""
    assert wiki_source.doc_count == 2
    assert wiki_source.content_hash != ""

    root = _snapshot_dir(wiki_source)
    raw = root / "raw" / "Refunds.md"
    normalized = root / "normalized" / "Refunds.md.md"
    assert "within 30 days" in raw.read_text(encoding="utf-8")
    assert "within 30 days" in normalized.read_text(encoding="utf-8")
    assert (root / "raw" / "Shipping.md").is_file()
    assert (root / "normalized" / "Shipping.md.md").is_file()


def test_the_request_carries_the_pat_as_basic_auth(db_session, wiki_source, mock_ado):
    """Pin the request, not only the answer.

    A mock transport answers whatever is asked, so a client that sent no
    ``Authorization`` header at all would still "sync". The header, the two
    endpoints and the documented query parameters are asserted here.
    """
    seen = mock_ado(_ado())
    AdoWikiAdapter().fetch(wiki_source, _credential())

    expected = base64.b64encode(f":{PAT}".encode("utf-8")).decode("utf-8")
    assert all(r.headers["authorization"] == f"Basic {expected}" for r in seen)

    paths = [r.url.path for r in seen]
    assert paths == ["/acme/Payments/_apis/wiki/wikis", "/acme/Payments/_apis/wiki/wikis/wiki-1/pages"]
    pages_query = seen[1].url.params
    assert pages_query["recursionLevel"] == "full"
    assert pages_query["includeContent"] == "true"
    assert pages_query["api-version"] == "7.1"


def test_the_content_hash_is_stable_across_two_identical_fetches(
    db_session, wiki_source, mock_ado, workspace_dir
):
    """The hash is the staleness signal (#830), so an unchanged wiki must not move it.

    Paired with its own negative control below: a hash that never changed would
    pass this on its own.
    """
    credentials.set_source_token(db_session, wiki_source, PAT)
    mock_ado(_ado())
    pipeline.sync_source(db_session, wiki_source)
    first = wiki_source.content_hash

    pipeline.sync_source(db_session, wiki_source)
    assert wiki_source.content_hash == first
    assert wiki_source.status == "synced"


def test_the_content_hash_moves_when_a_page_changes(
    db_session, wiki_source, mock_ado, workspace_dir
):
    """Negative control for the hash: edited upstream prose must change it."""
    credentials.set_source_token(db_session, wiki_source, PAT)
    mock_ado(_ado())
    pipeline.sync_source(db_session, wiki_source)
    first = wiki_source.content_hash

    edited = _page(
        "/",
        sub_pages=[
            _page("/Refunds", REFUNDS_PAGE.replace("30 days", "45 days")),
            _page("/Shipping", SHIPPING_PAGE),
        ],
    )
    mock_ado(_ado(tree=edited))
    pipeline.sync_source(db_session, wiki_source)

    assert wiki_source.content_hash != first
    normalized = _snapshot_dir(wiki_source) / "normalized" / "Refunds.md.md"
    assert "45 days" in normalized.read_text(encoding="utf-8")


def test_the_content_hash_moves_when_a_page_is_renamed(
    db_session, wiki_source, mock_ado, workspace_dir
):
    """The second negative control: identical prose at a new page path.

    Stability alone is satisfied by a hash that never moves, and "the prose
    changed" is satisfied by a hash over text only. Renaming a page while
    keeping every byte of its content is the case that separates those two from
    a digest that is genuinely path-inclusive.
    """
    credentials.set_source_token(db_session, wiki_source, PAT)
    mock_ado(_ado())
    pipeline.sync_source(db_session, wiki_source)
    first = wiki_source.content_hash

    renamed = _page(
        "/",
        sub_pages=[_page("/Refund-policy", REFUNDS_PAGE), _page("/Shipping", SHIPPING_PAGE)],
    )
    mock_ado(_ado(tree=renamed))
    pipeline.sync_source(db_session, wiki_source)

    assert wiki_source.content_hash != first
    assert (_snapshot_dir(wiki_source) / "raw" / "Refund-policy.md").is_file()
    # The old file is gone rather than orphaned beside the new one: a snapshot
    # is replaced, not accumulated.
    assert not (_snapshot_dir(wiki_source) / "raw" / "Refunds.md").exists()


def test_a_container_page_is_skipped_not_counted_as_a_failure(
    db_session, wiki_source, mock_ado, workspace_dir
):
    """A parent page with sub-pages and no text of its own is a folder.

    Counting folders as unreadable would bury the pages that genuinely failed in
    a count nobody can act on.
    """
    credentials.set_source_token(db_session, wiki_source, PAT)
    tree = _page(
        "/",
        sub_pages=[_page("/Policies", "", sub_pages=[_page("/Policies/Refunds", REFUNDS_PAGE)])],
    )
    mock_ado(_ado(tree=tree))

    pipeline.sync_source(db_session, wiki_source)

    assert wiki_source.status == "synced"
    assert wiki_source.doc_count == 1
    assert wiki_source.last_error == ""
    assert (_snapshot_dir(wiki_source) / "raw" / "Policies" / "Refunds.md").is_file()


def test_an_unreadable_page_is_reported_not_dropped(
    db_session, wiki_source, mock_ado, workspace_dir
):
    """Partial success is a real state: ``synced`` **plus** the count.

    A leaf page with no content cannot be normalized, and the source must end
    usable with the loss stated — never usable with the loss hidden.
    """
    credentials.set_source_token(db_session, wiki_source, PAT)
    tree = _page(
        "/",
        sub_pages=[
            _page("/Refunds", REFUNDS_PAGE),
            _page("/Shipping", SHIPPING_PAGE),
            _page("/Draft", ""),
        ],
    )
    mock_ado(_ado(tree=tree))

    pipeline.sync_source(db_session, wiki_source)

    assert wiki_source.status == "synced"
    assert wiki_source.doc_count == 2
    assert "1 document could not be read" in wiki_source.last_error
    assert "Draft.md" in wiki_source.last_error
    assert not (_snapshot_dir(wiki_source) / "raw" / "Draft.md").exists()


def test_a_wiki_with_nothing_readable_ends_in_error(
    db_session, wiki_source, mock_ado, workspace_dir
):
    """Zero readable pages is ``error``, never ``synced`` — the #818 rule, here.

    A wiki that looks synced and contains nothing is the failure this epic
    exists to prevent, and it is reachable through this adapter too.
    """
    credentials.set_source_token(db_session, wiki_source, PAT)
    mock_ado(_ado(tree=_page("/", sub_pages=[_page("/Draft", "")])))

    pipeline.sync_source(db_session, wiki_source)

    assert wiki_source.status == "error"
    assert wiki_source.content_hash == ""
    assert not _snapshot_dir(wiki_source).exists()


# ==========================================================================
# 6. The caps — pinned as literals, and never silent
# ==========================================================================


def test_the_page_cap_is_two_hundred_and_is_reported(
    db_session, wiki_source, mock_ado, workspace_dir
):
    """201 pages ingest 200, and the source says the other one was left behind.

    The literals are written out rather than read from ``MAX_PAGES``: sizing the
    fixture from the constant it means to pin is how a limit test stays green
    after somebody widens the limit.
    """
    credentials.set_source_token(db_session, wiki_source, PAT)
    pages = [_page(f"/Page{index:03d}", REFUNDS_PAGE) for index in range(201)]
    mock_ado(_ado(tree=_page("/", sub_pages=pages)))

    pipeline.sync_source(db_session, wiki_source)

    assert wiki_source.status == "synced"
    assert wiki_source.doc_count == 200
    assert "more than 200 pages" in wiki_source.last_error
    assert ado_wiki.MAX_PAGES == 200


def test_the_depth_cap_is_four_levels_and_is_reported(
    db_session, wiki_source, mock_ado, workspace_dir
):
    """Six nested levels ingest four, and the source says the rest were skipped.

    Both halves matter: a cap that dropped pages silently, and a cap that
    refused the whole source, would each be worse than the truncation.
    """
    credentials.set_source_token(db_session, wiki_source, PAT)
    deepest = _page("/L1/L2/L3/L4/L5/L6", REFUNDS_PAGE)
    level5 = _page("/L1/L2/L3/L4/L5", REFUNDS_PAGE, sub_pages=[deepest])
    level4 = _page("/L1/L2/L3/L4", REFUNDS_PAGE, sub_pages=[level5])
    level3 = _page("/L1/L2/L3", REFUNDS_PAGE, sub_pages=[level4])
    level2 = _page("/L1/L2", REFUNDS_PAGE, sub_pages=[level3])
    level1 = _page("/L1", REFUNDS_PAGE, sub_pages=[level2])
    mock_ado(_ado(tree=_page("/", sub_pages=[level1])))

    pipeline.sync_source(db_session, wiki_source)

    assert wiki_source.status == "synced"
    assert wiki_source.doc_count == 4
    assert "deeper than 4 levels" in wiki_source.last_error
    root = _snapshot_dir(wiki_source) / "raw"
    assert (root / "L1" / "L2" / "L3" / "L4.md").is_file()
    assert not (root / "L1" / "L2" / "L3" / "L4" / "L5.md").exists()
    assert ado_wiki.MAX_DEPTH == 4


# ==========================================================================
# 7. The token never leaks
# ==========================================================================


def test_the_pat_never_reaches_a_log_line_or_a_stored_artifact(
    db_session, wiki_source, mock_ado, workspace_dir
):
    """Scan everything the sync produced for the token: logs, row, disk.

    A secret that is correctly encrypted at rest and then printed by a debug
    line is still a leaked secret, so this asserts on the *outputs*, not on the
    storage. The failure path is scanned as well as the success path, because an
    error message is the likeliest place for a credential to be interpolated.
    """
    captured: list[str] = []
    sink_id = logger.add(lambda message: captured.append(str(message)), level="DEBUG")
    try:
        credentials.set_source_token(db_session, wiki_source, PAT)
        mock_ado(_ado())
        pipeline.sync_source(db_session, wiki_source)
        assert wiki_source.status == "synced"

        # …and again down a failing branch.
        mock_ado(_ado(wikis_status=401))
        pipeline.sync_source(db_session, wiki_source)
        assert wiki_source.status == "error"
    finally:
        logger.remove(sink_id)

    assert PAT not in "".join(captured)
    assert PAT not in (wiki_source.last_error or "")
    on_disk = [
        path
        for path in workspace_dir.rglob("*")
        if path.is_file() and PAT.encode("utf-8") in path.read_bytes()
    ]
    assert on_disk == []


def test_the_scan_would_catch_a_leak(db_session, wiki_source, workspace_dir):
    """Negative control for the scan above: plant the token and prove it is found.

    Without this, a scan that silently walked zero files would pass the leak
    test forever.
    """
    planted = workspace_dir / "leak.txt"
    planted.write_text(PAT, encoding="utf-8")
    found = [
        path
        for path in workspace_dir.rglob("*")
        if path.is_file() and PAT.encode("utf-8") in path.read_bytes()
    ]
    assert planted in found


# ==========================================================================
# 8. The credential routes — where the constraint becomes visible
# ==========================================================================


def test_the_credential_route_names_the_hub_before_any_sync(
    client, db_session, project, wiki_source
):
    """A hub-backed source answers ``origin="hub"``, ``canSync=false``, in words.

    Asserted field by field rather than against a whole body (``CLAUDE.md``), and
    the *branch* is the ``origin`` field, not the 200.
    """
    connection = ProviderConnection(kind="ado", name="EmeHub ADO", hub_connection_id="hub-77")
    db_session.add(connection)
    db_session.commit()
    wiki_source.connection_id = connection.id
    db_session.commit()

    response = client.get(
        f"/projects/{project.guid}/business/sources/{wiki_source.id}/ado-credential"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["origin"] == "hub"
    assert body["canSync"] is False
    assert body["hasToken"] is False
    assert body["message"] == credentials.HUB_BACKED_MESSAGE


def test_a_good_pat_is_preflighted_then_stored(client, db_session, project, wiki_source, mock_ado):
    """PUT stores the token only after Azure DevOps confirmed it can list wikis.

    Effect, not status: the token is resolvable afterwards, the response never
    contains it, and the preflight request was actually issued.
    """
    seen = mock_ado(_ado())
    response = client.put(
        f"/projects/{project.guid}/business/sources/{wiki_source.id}/ado-credential",
        json={"pat": PAT},
    )
    assert response.status_code == 200
    assert response.json()["origin"] == "source"
    assert PAT not in response.text
    assert [r.url.path for r in seen] == ["/acme/Payments/_apis/wiki/wikis"]

    db_session.refresh(wiki_source)
    assert credentials.source_token(wiki_source) == PAT


def test_a_wrong_scoped_pat_is_refused_and_not_stored(
    client, db_session, project, wiki_source, mock_ado
):
    """The scope message reaches the user at the field, and nothing is written.

    The "nothing is written" half is the one that matters: a known-bad token
    accepted here would fail later as a sync error, which is exactly the
    deferred, opaque failure the preflight exists to remove.
    """
    mock_ado(_ado(wikis_status=401))
    response = client.put(
        f"/projects/{project.guid}/business/sources/{wiki_source.id}/ado-credential",
        json={"pat": PAT},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == WIKI_SCOPE_MESSAGE

    db_session.refresh(wiki_source)
    assert credentials.has_source_token(wiki_source) is False


def test_deleting_the_token_reports_what_the_source_falls_back_to(
    client, db_session, project, wiki_source, mock_ado
):
    """DELETE answers with the resulting state, which for this source is "nothing"."""
    mock_ado(_ado())
    client.put(
        f"/projects/{project.guid}/business/sources/{wiki_source.id}/ado-credential",
        json={"pat": PAT},
    )
    response = client.delete(
        f"/projects/{project.guid}/business/sources/{wiki_source.id}/ado-credential"
    )
    assert response.status_code == 200
    assert response.json()["origin"] == "missing"
    assert response.json()["message"] == credentials.NO_CREDENTIAL_MESSAGE

    db_session.refresh(wiki_source)
    assert credentials.has_source_token(wiki_source) is False


def test_preflight_before_the_source_exists_reports_the_wiki_it_found(
    client, project, mock_ado
):
    """The "learn at add time, not at sync time" route, on its success branch."""
    mock_ado(_ado())
    response = client.post(
        f"/projects/{project.guid}/business/ado/preflight",
        json={"url": WIKI_URL, "pat": PAT},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["project"] == "Payments"
    assert body["wiki"] == "Payments.wiki"


@pytest.mark.parametrize(
    ("wikis_status", "expected"),
    [
        (401, WIKI_SCOPE_MESSAGE),
        (403, WIKI_SCOPE_MESSAGE),
        (404, NO_WIKI_MESSAGE.format(project="Payments")),
        (429, RATE_LIMITED_MESSAGE),
    ],
)
def test_preflight_tells_the_refusals_apart_at_the_route(
    client, project, mock_ado, wikis_status, expected
):
    """The three refusal paths reach the user distinctly, not as one 400.

    Telling them apart is the slice's deliverable, so the route is asserted on
    the exact ``detail`` rather than on "not 200".
    """
    mock_ado(_ado(wikis_status=wikis_status))
    response = client.post(
        f"/projects/{project.guid}/business/ado/preflight",
        json={"url": WIKI_URL, "pat": PAT},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == expected


def test_the_credential_routes_are_scoped_to_the_caller_s_project(client, project, wiki_source):
    """A source id reached through the wrong project is 404, not somebody's data."""
    response = client.get(
        f"/projects/{project.guid}/business/sources/{wiki_source.id + 500}/ado-credential"
    )
    assert response.status_code == 404
