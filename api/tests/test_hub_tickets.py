"""Ticket read-through from EmeHub + ``hub_ticket_id`` mapping — C3 of #497 (#500).

Two things are being defended here, and they pull in opposite directions:

* the hub is allowed to **freshen** what the ticket list shows, and
* the hub is never allowed to **break** it.

So most of these tests are about the second: a hub that is down, unauthorised or
talking nonsense must produce exactly the list the user would have seen anyway —
not an error, and above all not an empty list dressed up as "no tickets found"
(#491). The hub is stubbed with ``respx`` throughout; nothing here touches a real
hub.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.config import API_DIR
from app.models.ticket import Ticket

HUB = "https://hub.example.test/api"
HUB_TICKETS = f"{HUB}/tickets"


@pytest.fixture
def hub_on(workspace_dir, monkeypatch):
    """Both flags on plus a base URL — hub data reads are live."""
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "hub_sso_enabled", True)
    monkeypatch.setattr(config_module.settings, "hub_data_enabled", True)
    monkeypatch.setattr(config_module.settings, "hub_base_url", HUB)
    return config_module.settings


def _seed(db, **kwargs) -> Ticket:
    defaults = dict(
        external_id="SUR-1",
        provider_kind="ado",
        title="Local title",
        work_item_type="User Story",
        status="Ready for QA",
        priority="Medium",
        assignee="Local Person",
        sprint="Sprint 1",
        area_path="Surency\\Platform",
        epic="",
    )
    defaults.update(kwargs)
    ticket = Ticket(**defaults)
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


def _hub_ticket(**kwargs) -> dict:
    item = {
        "id": "hub-1",
        "externalId": "SUR-1",
        # The REAL hub vocabulary. This fixture originally said "ado" — our own
        # spelling on both sides of the join — which is exactly why #507 slipped
        # through: every test matched, while the live hub (which says
        # `azure_devops`) matched nothing. Keeping the hub's real spelling here
        # means the whole file exercises the translation.
        "providerKind": "azure_devops",
        "projectId": "hub-project",
        "connectionId": "hub-conn",
        "title": "Hub title",
        "status": "In Progress",
        "assignee": "Hub Person",
        "sprint": "Sprint 9",
        "epic": "",
        "priority": "High",
        "areaPath": "Surency\\Platform",
        "labels": ["hub"],
        "acCount": 3,
        "syncedAt": "2026-08-06T10:00:00Z",
    }
    item.update(kwargs)
    return item


def _page(*items: dict) -> dict:
    return {"items": list(items), "total": len(items)}


# --------------------------------------------------------------- the happy path
@respx.mock
def test_flag_on_with_token_serves_the_hub_list(hub_on, client, db_session):
    """The hub's values are what the user sees; the local row is the anchor."""
    local = _seed(db_session)
    route = respx.get(HUB_TICKETS).mock(return_value=httpx.Response(200, json=_page(_hub_ticket())))

    res = client.get("/tickets", headers={"X-Hub-Token": "fresh-token"})

    assert res.status_code == 200
    assert route.called
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["title"] == "Hub title"
    assert items[0]["status"] == "In Progress"
    assert items[0]["assignee"] == "Hub Person"
    assert items[0]["priority"] == "High"
    assert items[0]["sprint"] == "Sprint 9"
    assert items[0]["labels"] == ["hub"]
    assert items[0]["acCount"] == 3
    # The local primary key survives: everything downstream (detail, delete, runs)
    # addresses this row, and the hub's own id lives in a different namespace.
    assert items[0]["id"] == local.id


@respx.mock
def test_matched_rows_record_the_hub_ticket_id(hub_on, client, db_session):
    ticket = _seed(db_session)
    assert ticket.hub_ticket_id is None
    respx.get(HUB_TICKETS).mock(
        return_value=httpx.Response(200, json=_page(_hub_ticket(id="hub-42")))
    )

    client.get("/tickets", headers={"X-Hub-Token": "fresh-token"})

    db_session.refresh(ticket)
    assert ticket.hub_ticket_id == "hub-42"


@respx.mock
def test_no_cross_provider_mis_join(hub_on, client, db_session):
    """Same external id, different provider — a different work item entirely."""
    ado = _seed(db_session, external_id="PROJ-1", provider_kind="ado", title="ADO local")
    jira = _seed(db_session, external_id="PROJ-1", provider_kind="jira", title="Jira local")
    respx.get(HUB_TICKETS).mock(
        return_value=httpx.Response(
            200,
            json=_page(
                _hub_ticket(id="hub-ado", externalId="PROJ-1", providerKind="ado", title="ADO hub")
            ),
        )
    )

    res = client.get("/tickets", headers={"X-Hub-Token": "fresh-token"})

    db_session.refresh(ado)
    db_session.refresh(jira)
    assert ado.hub_ticket_id == "hub-ado"
    assert jira.hub_ticket_id is None  # never joined across providers
    by_kind = {item["providerKind"]: item for item in res.json()["items"]}
    assert by_kind["ado"]["title"] == "ADO hub"
    assert by_kind["jira"]["title"] == "Jira local"  # untouched by the hub


@respx.mock
def test_unmatched_hub_tickets_create_no_phantom_rows(hub_on, client, db_session):
    """A hub ticket we have never synced is reconciled against and then dropped."""
    _seed(db_session, external_id="SUR-1")
    respx.get(HUB_TICKETS).mock(
        return_value=httpx.Response(
            200,
            json=_page(
                _hub_ticket(),
                _hub_ticket(id="hub-2", externalId="SUR-999", title="Never synced here"),
            ),
        )
    )

    res = client.get("/tickets", headers={"X-Hub-Token": "fresh-token"})

    assert db_session.query(Ticket).count() == 1
    external_ids = [item["externalId"] for item in res.json()["items"]]
    assert external_ids == ["SUR-1"]
    assert res.json()["total"] == 1


@respx.mock
def test_local_only_tickets_survive_the_read_through(hub_on, client, db_session):
    """The read-through freshens the list; it must never shrink it."""
    _seed(db_session, external_id="SUR-1")
    _seed(db_session, external_id="SUR-2", title="Hub knows nothing of this")
    respx.get(HUB_TICKETS).mock(return_value=httpx.Response(200, json=_page(_hub_ticket())))

    res = client.get("/tickets", headers={"X-Hub-Token": "fresh-token"})

    items = {item["externalId"]: item for item in res.json()["items"]}
    assert set(items) == {"SUR-1", "SUR-2"}
    assert items["SUR-1"]["title"] == "Hub title"
    assert items["SUR-2"]["title"] == "Hub knows nothing of this"


@respx.mock
def test_filters_apply_to_the_merged_values(hub_on, client, db_session):
    """Filtering on the stale local status while showing the fresh hub one would
    render a list that contradicts the filter that produced it."""
    _seed(db_session, external_id="SUR-1", status="Ready for QA")
    respx.get(HUB_TICKETS).mock(
        return_value=httpx.Response(200, json=_page(_hub_ticket(status="In Progress")))
    )

    hit = client.get("/tickets?status=In Progress", headers={"X-Hub-Token": "t"})
    assert [i["externalId"] for i in hit.json()["items"]] == ["SUR-1"]

    miss = client.get("/tickets?status=Ready for QA", headers={"X-Hub-Token": "t"})
    assert miss.json()["items"] == []
    assert miss.json()["total"] == 0


@respx.mock
def test_paging_and_ordering_follow_the_hub_sync_time(hub_on, client, db_session):
    _seed(db_session, external_id="SUR-1")
    _seed(db_session, external_id="SUR-2")
    respx.get(HUB_TICKETS).mock(
        return_value=httpx.Response(
            200,
            json=_page(
                _hub_ticket(id="h1", externalId="SUR-1", syncedAt="2026-01-01T00:00:00Z"),
                _hub_ticket(id="h2", externalId="SUR-2", syncedAt="2026-08-01T00:00:00Z"),
            ),
        )
    )

    res = client.get("/tickets?pageSize=1", headers={"X-Hub-Token": "t"})
    body = res.json()
    assert body["total"] == 2
    assert [i["externalId"] for i in body["items"]] == ["SUR-2"]  # newest first

    page2 = client.get("/tickets?pageSize=1&page=2", headers={"X-Hub-Token": "t"})
    assert [i["externalId"] for i in page2.json()["items"]] == ["SUR-1"]


# ------------------------------------------------------------------- fallbacks
@respx.mock
def test_hub_unreachable_falls_back_to_local(hub_on, client, db_session):
    """"The hub is down" must look like an ordinary local list, not an error."""
    _seed(db_session, external_id="SUR-1")
    respx.get(HUB_TICKETS).mock(side_effect=httpx.ConnectError("refused"))

    res = client.get("/tickets", headers={"X-Hub-Token": "t"})

    assert res.status_code == 200
    items = res.json()["items"]
    assert [i["externalId"] for i in items] == ["SUR-1"]
    assert items[0]["title"] == "Local title"
    assert res.json()["total"] == 1  # never "no tickets found"


@respx.mock
def test_hub_gateway_error_falls_back_to_local(hub_on, client, db_session):
    _seed(db_session, external_id="SUR-1")
    respx.get(HUB_TICKETS).mock(return_value=httpx.Response(503))

    res = client.get("/tickets", headers={"X-Hub-Token": "t"})

    assert res.status_code == 200
    assert res.json()["total"] == 1


@respx.mock
def test_hub_401_falls_back_to_local(hub_on, client, db_session):
    """A 15-minute token expiring is routine, not a reason to show an error."""
    ticket = _seed(db_session, external_id="SUR-1")
    respx.get(HUB_TICKETS).mock(return_value=httpx.Response(401, json={"detail": "expired"}))

    res = client.get("/tickets", headers={"X-Hub-Token": "stale"})

    assert res.status_code == 200
    assert [i["externalId"] for i in res.json()["items"]] == ["SUR-1"]
    db_session.refresh(ticket)
    assert ticket.hub_ticket_id is None


@respx.mock
def test_malformed_hub_payload_falls_back_to_local(hub_on, client, db_session):
    _seed(db_session, external_id="SUR-1")
    respx.get(HUB_TICKETS).mock(return_value=httpx.Response(200, json={"unexpected": True}))

    res = client.get("/tickets", headers={"X-Hub-Token": "t"})

    assert res.status_code == 200
    assert res.json()["total"] == 1


@respx.mock
def test_hub_items_without_a_key_are_ignored(hub_on, client, db_session):
    """A hub row missing `providerKind` has no join key — it must not match
    anything by external id alone."""
    ticket = _seed(db_session, external_id="SUR-1")
    respx.get(HUB_TICKETS).mock(
        return_value=httpx.Response(
            200, json=_page({"id": "hub-x", "externalId": "SUR-1", "title": "Keyless"})
        )
    )

    res = client.get("/tickets", headers={"X-Hub-Token": "t"})

    db_session.refresh(ticket)
    assert ticket.hub_ticket_id is None
    assert res.json()["items"][0]["title"] == "Local title"


# ------------------------------------------------------------------ the flag off
@respx.mock
def test_flag_off_makes_no_hub_call_even_with_a_token(workspace_dir, client, db_session):
    """Off means *no outbound request*, not a request whose answer we discard."""
    _seed(db_session, external_id="SUR-1")
    route = respx.get(HUB_TICKETS).mock(return_value=httpx.Response(200, json=_page(_hub_ticket())))

    res = client.get("/tickets", headers={"X-Hub-Token": "t"})

    assert not route.called
    assert res.json()["items"][0]["title"] == "Local title"


@respx.mock
def test_no_token_makes_no_hub_call(hub_on, client, db_session):
    """No hub session in the browser is an ordinary state, not a failure."""
    _seed(db_session, external_id="SUR-1")
    route = respx.get(HUB_TICKETS).mock(return_value=httpx.Response(200, json=_page(_hub_ticket())))

    res = client.get("/tickets")

    assert not route.called
    assert res.json()["items"][0]["title"] == "Local title"


@respx.mock
def test_sync_never_calls_the_hub(hub_on, client, db_session):
    """Sync needs a provider PAT, which never crosses the hub boundary (#497 §4c),
    so the local sync path stays local even with the flag on."""
    route = respx.get(HUB_TICKETS).mock(return_value=httpx.Response(200, json=_page()))

    res = client.post(
        "/tickets/sync", json={"providerKind": "ado"}, headers={"X-Hub-Token": "t"}
    )

    # No work-item connection is configured in this DB, so sync 404s — the point
    # is that it decided that locally, without asking the hub.
    assert res.status_code == 404
    assert not route.called


# -------------------------------------------------------------------- migration
def test_hub_ticket_id_migration_up_and_down(tmp_path, monkeypatch):
    """The column applies and rolls back cleanly, and is indexed but not unique."""
    import app.config as config_module

    url = f"sqlite:///{(tmp_path / 'mig.db').as_posix()}"
    monkeypatch.setenv("QAGENT_DATABASE_URL", url)
    monkeypatch.setattr(config_module.settings, "database_url", url)
    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "migrations"))

    command.upgrade(cfg, "head")
    engine = create_engine(url, connect_args={"check_same_thread": False})
    insp = inspect(engine)
    assert any(c["name"] == "hub_ticket_id" for c in insp.get_columns("tickets"))
    index = next(i for i in insp.get_indexes("tickets") if i["name"] == "ix_tickets_hub_ticket_id")
    # Not unique: tickets are per-user private data, so one hub work item maps to
    # one row *per owner*.
    assert not index["unique"]
    engine.dispose()

    command.downgrade(cfg, "-1")
    engine = create_engine(url, connect_args={"check_same_thread": False})
    insp = inspect(engine)
    assert not any(c["name"] == "hub_ticket_id" for c in insp.get_columns("tickets"))
    assert all(i["name"] != "ix_tickets_hub_ticket_id" for i in insp.get_indexes("tickets"))
    engine.dispose()


# ---------------------------------------------------------------- #507
# The hub names Azure DevOps `azure_devops`; we name it `ado`. Reconciling
# without translating matched nothing for our most-used provider, and did so
# silently — a read that matches nothing satisfies every other rule.
def test_hub_key_translates_azure_devops_to_ado():
    from app.routers.tickets import _hub_key

    assert _hub_key("azure_devops", "1442") == _hub_key("ado", "1442")
    assert _hub_key("azure_devops", "1442") == ("ado", "1442")


@pytest.mark.parametrize("spelling", ["azure_devops", "azure-devops", "AZURE_DEVOPS", "AzureDevOps"])
def test_hub_key_tolerates_azure_devops_spellings(spelling):
    from app.routers.tickets import _hub_key

    assert _hub_key(spelling, "1442") == ("ado", "1442")


def test_hub_key_leaves_matching_vocabularies_alone():
    """`jira` and `github` are spelled the same on both sides."""
    from app.routers.tickets import _hub_key

    assert _hub_key("jira", "PROJ-1") == ("jira", "PROJ-1")
    assert _hub_key("github", "77") == ("github", "77")


def test_hub_key_keeps_unknown_kinds_verbatim():
    """An unrecognised kind must fail to match, never join the wrong rows."""
    from app.routers.tickets import _hub_key

    assert _hub_key("gitlab", "5") == ("gitlab", "5")
    assert _hub_key("ado", "5") != _hub_key("gitlab", "5")


def test_hub_key_still_requires_both_halves():
    from app.routers.tickets import _hub_key

    assert _hub_key("", "1442") is None
    assert _hub_key("azure_devops", "") is None
