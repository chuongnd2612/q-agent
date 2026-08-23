"""Durability + boot recovery of the Local-Agent manual-login capture queue (#625).

The same defect #605 closed one service over. ``agent_capture_service`` kept its
queue in a module-level ``list``, so a queued manual-login capture existed only
in one API worker's RAM:

* an API restart (deploy, crash, ``suite.sh up -d --build``) dropped it silently.
  The agent kept polling ``/agent/auth/next`` and kept getting 204 — which reads
  as *"the agent isn't connected"*, not as *"your capture was lost"*; and
* with more than one worker it could be lost with no restart at all: queued in
  worker A's memory, claimed on worker B.

It is not cosmetic. Live authoring **requires** a pre-authenticated
``browser-profile`` per origin — the agent bails immediately without one (#618) —
and this capture is the only thing that creates it, so a silently-dropped
capture presents as "authoring is broken".

This file pins the fix, in the shape #624 established:

* the queue is a table (``agent_capture_requests``), proven by throwing away all
  in-process state with ``importlib.reload`` and claiming afterwards;
* the claim is a conditional ``UPDATE … WHERE status = 'queued'`` with a
  row-count check, so two workers cannot both win it;
* "one live capture per ``(owner, project_key)``" is a UNIQUE index, not
  ``is_capturing`` reading one process's list;
* a capture stranded mid-flight is recovered at boot, with the count logged
  **and** asserted; and
* a claim that never finalizes is re-queued explicitly (``STALE_CLAIM_AFTER``)
  rather than cleared by accident on the next restart.

Plus the thing that must not regress: the capture flow itself still hands the
agent the same ``baseUrl``/``origin`` and still stamps the persistent
"captured on your Local Agent" marker the SPA and the authoring launcher read.

Real engines only (ADR 0001): no browser is opened here — the headed capture runs
on the agent's machine by construction. Only the HTTP wiring, the durable queue
and the boot sweep are under test.
"""

from __future__ import annotations

import importlib
import threading
from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.db import utcnow
from app.models.agent_capture import AgentCaptureRequest, dedupe_key_for
from app.models.project_config import ProjectConfig
from app.models.user import User
from app.services import agent_capture_service, agent_device_service, auth_service, run_status

BASE_URL = "https://hub-qa.surency.com/"
ORIGIN = "https://hub-qa.surency.com"
PROJECT = "Surency"


# --------------------------------------------------------------------- helpers
def _make_user(db_session, email: str = "capture-owner@example.com") -> User:
    user = User(
        email=email,
        first_name="Capture",
        last_name="Owner",
        password_hash=auth_service.hash_password("password123"),
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _pair_device(db_session, user, name: str = "Test Device") -> str:
    code = agent_device_service.create_pairing_code(db_session, user)
    _device, token = agent_device_service.redeem_pairing_code(db_session, code, name)
    return token


def _config(db_session, owner_id: int | None, key: str = PROJECT) -> ProjectConfig:
    row = ProjectConfig(key=key, base_url=BASE_URL, owner_id=owner_id)
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _row(db_session, owner_id: int | None, key: str = PROJECT) -> AgentCaptureRequest | None:
    db_session.expire_all()
    return (
        db_session.query(AgentCaptureRequest)
        .filter(AgentCaptureRequest.dedupe_key == dedupe_key_for(owner_id, key))
        .first()
    )


def _age(db_session, row: AgentCaptureRequest, *, created=None, claimed=None) -> None:
    """Back-date a row's timestamps to simulate time passing."""
    if created is not None:
        row.created_at = created
    if claimed is not None:
        row.claimed_at = claimed
    db_session.add(row)
    db_session.commit()


@pytest.fixture
def captured_logs():
    """Collect loguru messages so a boot-sweep log line can be asserted on."""
    from app.logging import logger

    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(m.record["message"]), level="DEBUG")
    try:
        yield messages
    finally:
        logger.remove(sink_id)


def _boot_sweep(captured_logs: list[str]) -> list[str]:
    """Run the real boot sweep from ``app.main`` and return the lines it emitted."""
    from app.main import _recover_orphaned_captures

    before = len(captured_logs)
    _recover_orphaned_captures()
    lines = captured_logs[before:]
    # The sweep is best-effort and swallows exceptions, which would otherwise let
    # a broken sweep look like "nothing to recover". Never accept that silently.
    assert not [m for m in lines if "recovery failed" in m], lines
    return lines


# ------------------------------------------------- the queue itself is durable
def test_the_queue_is_a_table_not_process_memory(workspace_dir, db_session):
    """The #625 regression guard: no module-level queue can come back."""
    assert not hasattr(agent_capture_service, "_pending")

    user = _make_user(db_session)
    capture = agent_capture_service.request_capture(user.id, PROJECT, BASE_URL)

    row = db_session.query(AgentCaptureRequest).one()
    assert (row.status, row.owner_id, row.project_key) == ("queued", user.id, PROJECT)
    assert (row.base_url, row.origin) == (BASE_URL, ORIGIN)
    assert capture["id"] == row.id


def test_a_queued_capture_survives_a_restart_and_is_still_claimable(client, db_session):
    """The acceptance criterion: restart mid-queue and the agent still gets 200."""
    user = _make_user(db_session)
    token = _pair_device(db_session, user)
    queued = agent_capture_service.request_capture(user.id, PROJECT, BASE_URL)

    # Simulate the process restart the old code could not survive: reload the
    # service module so every scrap of in-process state is discarded. Before #625
    # this alone lost the capture and the poll below returned 204 forever.
    importlib.reload(agent_capture_service)

    resp = client.post("/agent/auth/next", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["captureId"] == queued["id"]
    assert body["projectKey"] == PROJECT
    assert body["baseUrl"] == BASE_URL
    assert body["origin"] == ORIGIN

    # And it is running now, so a second poll gets nothing (no second browser).
    again = client.post("/agent/auth/next", headers={"Authorization": f"Bearer {token}"})
    assert again.status_code == 204
    assert _row(db_session, user.id).status == "running"


def test_is_capturing_reads_shared_state_not_one_processs_view(workspace_dir, db_session):
    """``is_capturing`` is what the UI polls; it must survive a state discard too."""
    user = _make_user(db_session)
    agent_capture_service.request_capture(user.id, PROJECT, BASE_URL)
    assert agent_capture_service.is_capturing(user.id, PROJECT) is True

    importlib.reload(agent_capture_service)
    assert agent_capture_service.is_capturing(user.id, PROJECT) is True
    # Negative control: a different project of the same owner is not capturing,
    # so the assertion above cannot be passing on a blanket True.
    assert agent_capture_service.is_capturing(user.id, "Other") is False


def test_a_claim_has_exactly_one_winner(workspace_dir, db_session):
    """Multi-worker safety: the conditional UPDATE, not a process lock, arbitrates."""
    user = _make_user(db_session)
    agent_capture_service.request_capture(user.id, PROJECT, BASE_URL)

    claims: list[dict | None] = []
    barrier = threading.Barrier(4)

    def _claim() -> None:
        barrier.wait()
        claims.append(agent_capture_service.claim_next(user.id))

    threads = [threading.Thread(target=_claim) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    won = [c for c in claims if c is not None]
    assert len(won) == 1, claims
    assert won[0]["project_key"] == PROJECT


def test_another_owners_device_cannot_claim_the_capture(client, db_session):
    owner = _make_user(db_session, "owner@example.com")
    other = _make_user(db_session, "other@example.com")
    other_token = _pair_device(db_session, other, "Other Device")
    agent_capture_service.request_capture(owner.id, PROJECT, BASE_URL)

    resp = client.post("/agent/auth/next", headers={"Authorization": f"Bearer {other_token}"})
    assert resp.status_code == 204
    assert _row(db_session, owner.id).status == "queued"


# ------------------------------------- one live capture per (owner, project_key)
def test_one_live_capture_per_owner_and_project_is_enforced_by_the_database(
    workspace_dir, db_session
):
    """The guard is a UNIQUE index, not the in-memory ``is_capturing`` check."""
    user = _make_user(db_session)
    first = agent_capture_service.request_capture(user.id, PROJECT, BASE_URL)
    second = agent_capture_service.request_capture(user.id, PROJECT, BASE_URL)
    assert second["id"] == first["id"]
    assert db_session.query(AgentCaptureRequest).count() == 1

    # Prove it is the DATABASE refusing, not the read-then-insert shortcut: a
    # second worker inserting the same key directly must be rejected.
    dupe = AgentCaptureRequest(
        owner_id=user.id,
        project_key=PROJECT,
        base_url=BASE_URL,
        origin=ORIGIN,
        dedupe_key=dedupe_key_for(user.id, PROJECT),
        status="queued",
    )
    db_session.add(dupe)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_the_guard_is_scoped_to_the_owner_and_the_project(workspace_dir, db_session):
    """Negative control for the dedupe: it must not collapse unrelated captures."""
    one = _make_user(db_session, "one@example.com")
    two = _make_user(db_session, "two@example.com")
    agent_capture_service.request_capture(one.id, PROJECT, BASE_URL)
    agent_capture_service.request_capture(one.id, "Other", "https://other.test/")
    agent_capture_service.request_capture(two.id, PROJECT, BASE_URL)
    # And the auth-disabled (owner_id NULL) install gets its own single row —
    # a composite UNIQUE index over the two columns would not dedupe NULLs at all.
    agent_capture_service.request_capture(None, PROJECT, BASE_URL)
    agent_capture_service.request_capture(None, PROJECT, BASE_URL)

    assert db_session.query(AgentCaptureRequest).count() == 4
    assert agent_capture_service.is_capturing(None, PROJECT) is True


def test_a_queued_capture_is_refreshed_rather_than_duplicated(workspace_dir, db_session):
    """A second click after the base URL changed must not queue a stale URL."""
    user = _make_user(db_session)
    first = agent_capture_service.request_capture(user.id, PROJECT, BASE_URL)
    again = agent_capture_service.request_capture(user.id, PROJECT, "https://hub.surency.com/app")

    assert again["id"] == first["id"]
    assert again["origin"] == "https://hub.surency.com"
    row = _row(db_session, user.id)
    assert (row.base_url, row.origin) == ("https://hub.surency.com/app", "https://hub.surency.com")


def test_a_fresh_claim_is_not_disturbed_by_a_second_request(workspace_dir, db_session):
    """The headed browser is already open on the operator's machine — don't re-queue."""
    user = _make_user(db_session)
    agent_capture_service.request_capture(user.id, PROJECT, BASE_URL)
    claimed = agent_capture_service.claim_next(user.id)
    assert claimed is not None

    agent_capture_service.request_capture(user.id, PROJECT, BASE_URL)
    assert _row(db_session, user.id).status == "running"


def test_an_abandoned_claim_is_requeued_so_a_durable_queue_cannot_wedge_a_project(
    workspace_dir, db_session
):
    """The hazard a durable queue introduces, handled explicitly (#625)."""
    user = _make_user(db_session)
    agent_capture_service.request_capture(user.id, PROJECT, BASE_URL)
    agent_capture_service.claim_next(user.id)
    row = _row(db_session, user.id)
    _age(db_session, row, claimed=utcnow() - agent_capture_service.STALE_CLAIM_AFTER - timedelta(minutes=1))

    # A stale claim is not "capturing" — the UI must stop spinning on it.
    assert agent_capture_service.is_capturing(user.id, PROJECT) is False

    agent_capture_service.request_capture(user.id, PROJECT, BASE_URL)
    refreshed = _row(db_session, user.id)
    assert (refreshed.status, refreshed.claimed_at) == ("queued", None)
    assert agent_capture_service.claim_next(user.id) is not None


# --------------------------------------------------------------- the boot sweep
def test_boot_sweep_requeues_a_capture_stranded_mid_flight(
    workspace_dir, db_session, captured_logs
):
    """The acceptance criterion: recovered at boot, count logged AND asserted."""
    user = _make_user(db_session)
    agent_capture_service.request_capture(user.id, PROJECT, BASE_URL)
    agent_capture_service.claim_next(user.id)
    row = _row(db_session, user.id)
    _age(db_session, row, claimed=utcnow() - timedelta(hours=2))

    lines = _boot_sweep(captured_logs)
    assert [m for m in lines if "Re-queued 1 login capture(s) stranded mid-flight" in m], lines

    recovered = _row(db_session, user.id)
    assert (recovered.status, recovered.claimed_at) == ("queued", None)
    # And the agent can now actually get it.
    assert agent_capture_service.claim_next(user.id) is not None


def test_boot_sweep_leaves_a_recent_claim_alone(workspace_dir, db_session, captured_logs):
    """The agent runs on another machine; its open browser must still finalize."""
    user = _make_user(db_session)
    agent_capture_service.request_capture(user.id, PROJECT, BASE_URL)
    claimed = agent_capture_service.claim_next(user.id)

    lines = _boot_sweep(captured_logs)
    assert not [m for m in lines if "stranded" in m or "abandoned" in m], lines
    assert _row(db_session, user.id).status == "running"
    # The /complete post-back still resolves after the restart.
    assert agent_capture_service.finish(claimed["id"], user.id) is not None


def test_boot_sweep_drops_a_capture_nobody_is_waiting_on(
    workspace_dir, db_session, captured_logs
):
    """A login prompt asked for half a day ago must not ambush the operator."""
    user = _make_user(db_session)
    agent_capture_service.request_capture(user.id, PROJECT, BASE_URL)
    row = _row(db_session, user.id)
    _age(db_session, row, created=utcnow() - agent_capture_service.ABANDON_AFTER - timedelta(hours=1))

    assert agent_capture_service.is_capturing(user.id, PROJECT) is False
    lines = _boot_sweep(captured_logs)
    assert [m for m in lines if "Dropped 1 abandoned login capture(s)" in m], lines
    assert db_session.query(AgentCaptureRequest).count() == 0


def test_boot_sweep_is_quiet_and_idempotent_when_there_is_nothing_to_recover(
    workspace_dir, db_session, captured_logs
):
    user = _make_user(db_session)
    agent_capture_service.request_capture(user.id, PROJECT, BASE_URL)

    for _ in range(3):
        lines = _boot_sweep(captured_logs)
        assert not [m for m in lines if "stranded" in m or "abandoned" in m], lines
    # A queued capture is untouched by the sweep — it is exactly what must survive.
    assert _row(db_session, user.id).status == "queued"


def test_the_sweep_counts_both_outcomes_independently(workspace_dir, db_session):
    """``(requeued, dropped)`` must not be one number wearing two hats."""
    user = _make_user(db_session)
    agent_capture_service.request_capture(user.id, PROJECT, BASE_URL)
    agent_capture_service.claim_next(user.id)
    _age(db_session, _row(db_session, user.id), claimed=utcnow() - timedelta(hours=2))
    agent_capture_service.request_capture(user.id, "Other", "https://other.test/")
    _age(
        db_session,
        _row(db_session, user.id, "Other"),
        created=utcnow() - agent_capture_service.ABANDON_AFTER - timedelta(minutes=1),
    )

    assert run_status.recover_orphaned_captures(db_session) == (1, 1)
    assert _row(db_session, user.id).status == "queued"
    assert _row(db_session, user.id, "Other") is None


# ------------------------------------------------------- the flow must not regress
def test_the_capture_flow_still_reaches_the_agent_and_stamps_the_marker(client, db_session):
    """End to end over the real endpoints: claim -> complete -> persistent marker.

    The headed browser and the files it writes
    (``~/.qagent-agent/sessions/<origin>/browser-profile``, ``storageState.json``,
    ``sessionStorage.json`` — #618) live on the agent's machine, so what the
    server owes the agent is exactly the ``origin`` it keys them on, plus the
    marker the SPA and the authoring launcher read afterwards.
    """
    user = _make_user(db_session)
    token = _pair_device(db_session, user)
    config = _config(db_session, user.id)
    agent_capture_service.request_capture(user.id, PROJECT, BASE_URL)

    claimed = client.post("/agent/auth/next", headers={"Authorization": f"Bearer {token}"})
    assert claimed.status_code == 200, claimed.text
    body = claimed.json()
    assert body["origin"] == ORIGIN == agent_capture_service.origin_of(BASE_URL)

    done = client.post(
        f"/agent/auth/{body['captureId']}/complete",
        json={"ok": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert done.status_code == 200, done.text

    db_session.expire_all()
    extra = db_session.get(ProjectConfig, config.id).extra or {}
    assert extra["agentAuthOrigin"] == ORIGIN
    assert extra["agentAuthCapturedAt"]
    # The queue holds live work only, so the row is gone and the project is idle.
    assert db_session.query(AgentCaptureRequest).count() == 0
    assert agent_capture_service.is_capturing(user.id, PROJECT) is False


def test_completing_someone_elses_capture_is_a_404_and_leaves_the_row(client, db_session):
    """Negative control on ``finish``'s owner scoping."""
    owner = _make_user(db_session, "owner@example.com")
    other = _make_user(db_session, "other@example.com")
    other_token = _pair_device(db_session, other, "Other Device")
    queued = agent_capture_service.request_capture(owner.id, PROJECT, BASE_URL)

    resp = client.post(
        f"/agent/auth/{queued['id']}/complete",
        json={"ok": True},
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert resp.status_code == 404
    assert _row(db_session, owner.id) is not None


# ------------------------------- the deliberate exception: pairing codes stay in RAM
def test_pairing_codes_are_deliberately_still_in_memory(workspace_dir, db_session):
    """#625 asked for a written decision on ``agent_device_service._pending``.

    It stays in memory on purpose (the reasoning is written out at that symbol):
    a lost pairing code fails **loudly** at redemption and costs one click, the
    code is a short-lived bearer secret that persisting would only expose, and
    both halves of pairing are driven by the same human within seconds. This test
    pins the decision so a future sweep does not "fix" it silently — and pins the
    property that makes it safe: single-use.
    """
    assert isinstance(agent_device_service._pending, dict)

    user = _make_user(db_session)
    code = agent_device_service.create_pairing_code(db_session, user)
    assert code in agent_device_service._pending

    agent_device_service.redeem_pairing_code(db_session, code, "Device")
    assert code not in agent_device_service._pending  # single-use
    with pytest.raises(auth_service.AuthError):
        agent_device_service.redeem_pairing_code(db_session, code, "Device")
