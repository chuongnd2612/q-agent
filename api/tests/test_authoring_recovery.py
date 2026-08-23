"""Durability + boot recovery of the agent live-authoring queue (#605).

The reported bug was "Regenerate is always empty and never triggers the local
agent". The cause was not the trigger: ``_enqueue_agent_authoring`` committed the
``AutomationSpec`` at ``status="running"`` and queued the session in a
module-level ``list`` inside ``agent_authoring_service`` — process-local. Any API
restart dropped the queue, and nothing recovered the row:
``run_status.recover_orphaned_runs`` only ever looked at ``Run.status``, so the
spec sat at ``running`` with empty ``code`` forever, the panel rendered its empty
state, and the agent — correctly — had nothing to claim.

This file pins both halves of the fix:

* **the queue is durable** — it lives in ``agent_authoring_sessions``, so a fresh
  process (simulated here by reloading the service module, which throws away any
  module state that might be hiding the persistence) still claims it; and
* **the boot sweep recovers what cannot be resumed** — a spec left ``running``
  with no surviving session is reset to a re-triggerable status and the count is
  logged, exactly the way ``_recover_orphaned_runs`` logs its own sweep.

Plus the two things that must not regress: a **terminal** run still authors on
Regenerate (#442), and the per-run **Stop** path still resets stuck specs (#420) —
now through the query shared with the sweep.

Real engines only (ADR 0001): no browser, no ``claude``. Prompts are composed by
the real server-side code; only the HTTP wiring, the durable queue and the sweep
are under test.
"""

from __future__ import annotations

import importlib
import threading

import pytest

from app.models.agent_authoring import AgentAuthoringSession
from app.models.agent_device import AgentDevice
from app.models.run import Run
from app.models.testcase import AutomationSpec, TestCase
from app.models.user import User
from app.services import agent_authoring_service, agent_device_service, auth_service, run_status


# --------------------------------------------------------------------- helpers
def _make_user(db_session, email: str = "authoring-owner@example.com") -> User:
    user = User(
        email=email,
        first_name="Authoring",
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


def _seed_case(
    db_session,
    *,
    owner_id: int | None = None,
    run_status_value: str = "automation",
    spec_status: str | None = "running",
    code: str = "",
    block_reason: str = "",
) -> tuple[Run, TestCase, AutomationSpec | None]:
    """One run + one case, optionally with a spec in ``spec_status``."""
    run = Run(
        code=f"RUN-{9000 + (db_session.query(Run).count())}",
        name="Authoring recovery",
        status=run_status_value,
        owner_id=owner_id,
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    case = TestCase(
        run_id=run.id,
        ticket_external_id="SUR-1709",
        code="TC-01",
        title="Reset password from the login screen",
        objective="Prove the reset link is sent",
        steps=[{"a": "Open login", "e": "Login renders"}],
        test_data=[],
        linked_ac=["AC-1"],
        approval="approved",
    )
    db_session.add(case)
    db_session.commit()
    db_session.refresh(case)
    spec = None
    if spec_status is not None:
        spec = AutomationSpec(
            test_case_id=case.id,
            filename="tests/SUR-1709/SUR-1709-TC-01.spec.ts",
            status=spec_status,
            code=code,
            block_reason=block_reason,
        )
        db_session.add(spec)
        db_session.commit()
        db_session.refresh(spec)
    return run, case, spec


def _enqueue(
    *,
    owner_id: int | None,
    case_id: int,
    run_id: int | None,
    session_id: str = "sess-authoring-1",
    task_prompt: str = "author it",
) -> None:
    agent_authoring_service.request_authoring(
        session_id,
        owner_id=owner_id,
        project_key="Surency",
        repo="web",
        base_url="https://hub-qa.surency.com/",
        origin="https://hub-qa.surency.com",
        case_id=case_id,
        run_id=run_id,
        spec_filename="tests/SUR-1709/SUR-1709-TC-01.spec.ts",
        system_prompt="the live-authoring skill",
        task_prompt=task_prompt,
        model="claude-sonnet-4-5",
        max_budget_usd=3.5,
    )


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
    """Run the real boot sweep from ``app.main`` and return the log lines it emitted."""
    from app.main import _recover_orphaned_authoring

    before = len(captured_logs)
    _recover_orphaned_authoring()
    lines = captured_logs[before:]
    # The sweep is best-effort and swallows exceptions, which would otherwise let a
    # broken sweep look like "nothing to recover". Never accept that silently.
    assert not [m for m in lines if "recovery failed" in m], lines
    return lines


# ------------------------------------------------- the queue itself is durable
def test_the_queue_is_a_table_not_process_memory(workspace_dir, db_session):
    """The #605 regression guard: no module-level queue can come back."""
    assert not hasattr(agent_authoring_service, "_pending")
    assert not hasattr(agent_authoring_service, "_results")

    user = _make_user(db_session)
    _run, case, _spec = _seed_case(db_session, owner_id=user.id)
    _enqueue(owner_id=user.id, case_id=case.id, run_id=_run.id)

    row = db_session.query(AgentAuthoringSession).one()
    assert (row.session_id, row.status, row.case_id) == ("sess-authoring-1", "queued", case.id)


def test_a_queued_session_survives_a_restart_and_is_still_claimable(client, db_session):
    """The acceptance criterion: restart mid-queue, and the agent still gets the job."""
    user = _make_user(db_session)
    token = _pair_device(db_session, user)
    run, case, _spec = _seed_case(db_session, owner_id=user.id)
    _enqueue(owner_id=user.id, case_id=case.id, run_id=run.id, task_prompt="author SUR-1709 TC-01")

    # Simulate the process restart the old code could not survive: reload the
    # service module so every scrap of in-process state is discarded. Before #605
    # this alone lost the session and the claim below returned 204.
    importlib.reload(agent_authoring_service)

    resp = client.post("/agent/authoring/next", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sessionId"] == "sess-authoring-1"
    assert body["caseId"] == case.id
    assert body["runId"] == run.id
    assert body["taskPrompt"] == "author SUR-1709 TC-01"
    assert body["specFilename"] == "tests/SUR-1709/SUR-1709-TC-01.spec.ts"

    # And it is now running, so a second poll gets nothing.
    again = client.post("/agent/authoring/next", headers={"Authorization": f"Bearer {token}"})
    assert again.status_code == 204
    db_session.expire_all()
    assert db_session.query(AgentAuthoringSession).one().status == "running"


def test_a_claim_has_exactly_one_winner(workspace_dir, db_session):
    """Multi-worker safety: the conditional UPDATE, not a process lock, arbitrates."""
    user = _make_user(db_session)
    run, case, _spec = _seed_case(db_session, owner_id=user.id)
    _enqueue(owner_id=user.id, case_id=case.id, run_id=run.id)

    claims: list[dict | None] = []
    barrier = threading.Barrier(4)

    def _claim() -> None:
        barrier.wait()
        claims.append(agent_authoring_service.claim_next(user.id))

    threads = [threading.Thread(target=_claim) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    won = [c for c in claims if c is not None]
    assert len(won) == 1, claims
    assert won[0]["case_id"] == case.id


def test_another_owners_device_cannot_claim_the_session(client, db_session):
    owner = _make_user(db_session, "owner@example.com")
    other = _make_user(db_session, "other@example.com")
    other_token = _pair_device(db_session, other, "Other Device")
    run, case, _spec = _seed_case(db_session, owner_id=owner.id)
    _enqueue(owner_id=owner.id, case_id=case.id, run_id=run.id)

    resp = client.post("/agent/authoring/next", headers={"Authorization": f"Bearer {other_token}"})
    assert resp.status_code == 204
    assert db_session.query(AgentAuthoringSession).one().status == "queued"


def test_finalize_drops_the_row_so_the_queue_holds_live_work_only(workspace_dir, db_session):
    user = _make_user(db_session)
    run, case, _spec = _seed_case(db_session, owner_id=user.id)
    _enqueue(owner_id=user.id, case_id=case.id, run_id=run.id)
    assert agent_authoring_service.claim_next(user.id) is not None

    agent_authoring_service.set_result("sess-authoring-1", {"status": "done", "summary": "ok"})

    db_session.expire_all()
    assert db_session.query(AgentAuthoringSession).count() == 0
    assert agent_authoring_service.get_session("sess-authoring-1") is None


# ----------------------------------------------- idempotency / stale-claim recovery
def test_a_second_enqueue_refreshes_a_queued_session_instead_of_duplicating(
    workspace_dir, db_session
):
    """#419's guard, now enforced by the case_id UNIQUE index."""
    user = _make_user(db_session)
    run, case, _spec = _seed_case(db_session, owner_id=user.id)
    _enqueue(owner_id=user.id, case_id=case.id, run_id=run.id, task_prompt="first")
    _enqueue(
        owner_id=user.id,
        case_id=case.id,
        run_id=run.id,
        session_id="sess-authoring-2",
        task_prompt="second, with the reviewer comment",
    )

    db_session.expire_all()
    row = db_session.query(AgentAuthoringSession).one()
    assert row.session_id == "sess-authoring-1"  # same session, refreshed in place
    assert row.task_prompt == "second, with the reviewer comment"


def test_a_running_session_is_not_disturbed_by_a_second_enqueue(workspace_dir, db_session):
    user = _make_user(db_session)
    run, case, _spec = _seed_case(db_session, owner_id=user.id)
    _enqueue(owner_id=user.id, case_id=case.id, run_id=run.id, task_prompt="first")
    assert agent_authoring_service.claim_next(user.id) is not None

    _enqueue(owner_id=user.id, case_id=case.id, run_id=run.id, task_prompt="second")

    db_session.expire_all()
    row = db_session.query(AgentAuthoringSession).one()
    assert row.status == "running"
    assert row.task_prompt == "first"  # the device is authoring this one


def test_an_abandoned_claim_is_requeued_so_a_durable_queue_cannot_wedge_a_case(
    workspace_dir, db_session, monkeypatch
):
    """A restart used to clear a dead claim by accident; now it is explicit."""
    from datetime import timedelta

    user = _make_user(db_session)
    run, case, _spec = _seed_case(db_session, owner_id=user.id)
    _enqueue(owner_id=user.id, case_id=case.id, run_id=run.id, task_prompt="first")
    assert agent_authoring_service.claim_next(user.id) is not None

    monkeypatch.setattr(agent_authoring_service, "STALE_CLAIM_AFTER", timedelta(seconds=-1))
    _enqueue(owner_id=user.id, case_id=case.id, run_id=run.id, task_prompt="after the device died")

    db_session.expire_all()
    row = db_session.query(AgentAuthoringSession).one()
    assert row.status == "queued"
    assert row.claimed_at is None
    assert row.task_prompt == "after the device died"
    assert agent_authoring_service.claim_next(user.id) is not None


# ------------------------------------------------------------------ boot sweep
def test_boot_sweep_recovers_a_spec_stuck_running_with_no_session(
    workspace_dir, db_session, captured_logs
):
    """The bug itself: an empty spec at ``running`` whose queued session was lost."""
    user = _make_user(db_session)
    _run, _case, spec = _seed_case(db_session, owner_id=user.id, spec_status="running", code="")
    assert db_session.query(AgentAuthoringSession).count() == 0  # the queue died with the process

    lines = _boot_sweep(captured_logs)

    db_session.refresh(spec)
    assert spec.status == "blocked"
    assert "Regenerate" in spec.block_reason
    assert any("Recovered 1 orphaned authoring spec(s) from a prior process" in m for m in lines), (
        lines
    )


def test_boot_sweep_keeps_partially_authored_code_as_a_draft(
    workspace_dir, db_session, captured_logs
):
    user = _make_user(db_session)
    _run, _case, spec = _seed_case(
        db_session, owner_id=user.id, spec_status="running", code="test('half written', () => {})"
    )

    _boot_sweep(captured_logs)

    db_session.refresh(spec)
    assert spec.status == "draft"
    assert spec.code == "test('half written', () => {})"


def test_boot_sweep_leaves_a_spec_whose_session_survived_alone(
    workspace_dir, db_session, captured_logs
):
    """A durable session means the agent is still authoring — do not reset it."""
    user = _make_user(db_session)
    run, case, spec = _seed_case(db_session, owner_id=user.id, spec_status="running")
    _enqueue(owner_id=user.id, case_id=case.id, run_id=run.id)

    lines = _boot_sweep(captured_logs)

    db_session.refresh(spec)
    assert spec.status == "running"
    db_session.expire_all()
    assert db_session.query(AgentAuthoringSession).count() == 1
    assert not [m for m in lines if "orphaned authoring spec" in m], lines
    assert any("Kept 1 live authoring session(s)" in m for m in lines), lines


def test_boot_sweep_drops_a_session_no_spec_is_waiting_on(
    workspace_dir, db_session, captured_logs
):
    """The mirror hazard of a durable queue: a job for work already finished."""
    user = _make_user(db_session)
    run, case, spec = _seed_case(
        db_session, owner_id=user.id, spec_status="draft", code="test('done', () => {})"
    )
    _enqueue(owner_id=user.id, case_id=case.id, run_id=run.id)

    lines = _boot_sweep(captured_logs)

    db_session.expire_all()
    assert db_session.query(AgentAuthoringSession).count() == 0
    assert any("Dropped 1 stale authoring session(s)" in m for m in lines), lines
    db_session.refresh(spec)
    assert spec.status == "draft"


def test_boot_sweep_is_quiet_and_idempotent_when_there_is_nothing_to_recover(
    workspace_dir, db_session, captured_logs
):
    user = _make_user(db_session)
    _run, _case, spec = _seed_case(db_session, owner_id=user.id, spec_status="draft")

    first = _boot_sweep(captured_logs)
    second = _boot_sweep(captured_logs)

    assert not [m for m in first + second if "orphaned authoring spec" in m]
    db_session.refresh(spec)
    assert spec.status == "draft"


def test_recovery_is_audited_so_the_reset_is_never_silent(workspace_dir, db_session):
    from app.models.audit import AuditLog

    user = _make_user(db_session)
    _seed_case(db_session, owner_id=user.id, spec_status="running")

    recovered, _dropped = run_status.recover_orphaned_authoring(db_session)

    assert recovered == 1
    rows = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "Recovered orphaned authoring spec")
        .all()
    )
    assert len(rows) == 1
    assert rows[0].target == "SUR-1709 · TC-01"
    assert rows[0].status == "warning"


# ------------------------------- after recovery the whole path works end to end
def _context() -> dict:
    return {
        "projectKey": "Surency",
        "repo": "web",
        "baseUrl": "https://hub-qa.surency.com/",
        "routes": [],
        "selectors": [],
        "auth": {},
        "testAccounts": [],
    }


@pytest.mark.parametrize("run_state", ["automation", "failed", "cancelled"])
def test_a_recovered_spec_regenerates_and_the_paired_agent_claims_it(
    client, db_session, captured_logs, run_state
):
    """The end-to-end acceptance criterion, including the #442 terminal-run case.

    A terminal (failed/cancelled) run must still author on Regenerate — that is
    why ``finalize_authored_spec`` deliberately keeps post-backs for terminal runs
    — so the recovery + re-enqueue path is asserted for a terminal run too.
    """
    from app.routers.automation import _enqueue_agent_authoring

    user = _make_user(db_session)
    token = _pair_device(db_session, user)
    db_session.add(AgentDevice(owner_id=user.id, name="Paired", token_hash="x" * 64))
    run, case, spec = _seed_case(
        db_session, owner_id=user.id, run_status_value=run_state, spec_status="running"
    )

    # 1) The restart orphaned it: the spec is stuck at running with no session.
    _boot_sweep(captured_logs)
    db_session.refresh(spec)
    assert spec.status == "blocked"

    # 2) Regenerate: the real enqueue path runs and re-arms the spec.
    respec = _enqueue_agent_authoring(db_session, run, case, _context())
    db_session.commit()
    assert respec.status == "running"
    assert respec.block_reason == ""

    # 3) The agent's poll now returns 200 with a real job — not 204.
    resp = client.post("/agent/authoring/next", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["caseId"] == case.id
    assert body["runId"] == run.id
    assert body["taskPrompt"].strip()
    assert body["systemPrompt"].strip()


# ------------------------------------------------------------ Stop must still work
def test_stop_still_resets_stuck_specs_through_the_shared_query(workspace_dir, db_session):
    """#420's Stop path, now sharing ``reset_stuck_specs`` with the boot sweep."""
    from app.routers.runs import _stop_run_work

    user = _make_user(db_session)
    run, case, spec = _seed_case(db_session, owner_id=user.id, spec_status="running", code="")
    _enqueue(owner_id=user.id, case_id=case.id, run_id=run.id)
    other_run, other_case, other_spec = _seed_case(
        db_session, owner_id=user.id, spec_status="running", code=""
    )

    _stop_run_work(db_session, run)

    db_session.refresh(spec)
    assert spec.status == "blocked"
    assert spec.block_reason == "Stopped before authoring finished."
    # Negative control: the other run's stuck spec is untouched, so a stop that
    # over-reached (missing run filter) could not pass this test.
    db_session.refresh(other_spec)
    assert other_spec.status == "running"
    # And the run's queued session is gone, so nothing can claim it later.
    db_session.expire_all()
    assert db_session.query(AgentAuthoringSession).count() == 0


def test_purge_run_and_drop_queued_cases_operate_on_the_table(workspace_dir, db_session):
    user = _make_user(db_session)
    run_a, case_a, _ = _seed_case(db_session, owner_id=user.id, spec_status=None)
    run_b, case_b, _ = _seed_case(db_session, owner_id=user.id, spec_status=None)
    _enqueue(owner_id=user.id, case_id=case_a.id, run_id=run_a.id, session_id="sess-a")
    _enqueue(owner_id=user.id, case_id=case_b.id, run_id=run_b.id, session_id="sess-b")

    assert agent_authoring_service.drop_queued_cases({case_a.id}) == 1
    db_session.expire_all()
    assert {r.session_id for r in db_session.query(AgentAuthoringSession).all()} == {"sess-b"}

    assert agent_authoring_service.purge_run(run_b.id) == 1
    db_session.expire_all()
    assert db_session.query(AgentAuthoringSession).count() == 0


def test_drop_queued_cases_spares_a_claimed_session(workspace_dir, db_session):
    user = _make_user(db_session)
    run, case, _ = _seed_case(db_session, owner_id=user.id, spec_status=None)
    _enqueue(owner_id=user.id, case_id=case.id, run_id=run.id)
    assert agent_authoring_service.claim_next(user.id) is not None

    assert agent_authoring_service.drop_queued_cases({case.id}) == 0
    db_session.expire_all()
    assert db_session.query(AgentAuthoringSession).one().status == "running"
