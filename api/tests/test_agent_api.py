"""HTTP tests for the Local Agent feature: device pairing endpoints, the
execution-target routing (server vs local-agent, the 409 no-device guard), and
the job protocol (claim atomicity, events re-emit, result upsert, multipart
evidence upload, and completion).
"""

from __future__ import annotations

from app.models.execution import Evidence, Execution, ExecutionResult
from app.services import agent_device_service, auth_service


def _make_user(db_session, email: str, password: str = "password123"):
    from app.models.user import User

    user = User(
        email=email,
        first_name="Agent",
        last_name="Owner",
        password_hash=auth_service.hash_password(password),
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _login(client, email: str, password: str = "password123") -> str:
    resp = client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["accessToken"]


def _pair_device(db_session, user, name: str = "Test Device"):
    code = agent_device_service.create_pairing_code(db_session, user)
    return agent_device_service.redeem_pairing_code(db_session, code, name)


def _seed_agent_run(db_session, owner_id: int):
    """A run + one approved case + its spec, owned by ``owner_id``."""
    from app.models.run import Run
    from app.models.testcase import AutomationSpec, TestCase

    run = Run(code=f"RUN-AGENT-{owner_id}", name="Agent run", status="automation", workers=2, owner_id=owner_id)
    db_session.add(run)
    db_session.flush()

    case = TestCase(
        run_id=run.id,
        ticket_external_id="SUR-1428",
        code="TC-01",
        title="Login works",
        approval="approved",
        automation="Playwright",
    )
    db_session.add(case)
    db_session.flush()

    spec = AutomationSpec(test_case_id=case.id, filename="1428-TC-01.spec.ts", code="// spec code")
    db_session.add(spec)
    db_session.commit()
    db_session.refresh(run)
    db_session.refresh(case)
    return run, case


def _queued_execution(db_session, run, case, target: str = "local-agent", status: str = "queued") -> Execution:
    execution = Execution(
        run_id=run.id, status=status, target=target, env=run.env, browser=run.browser,
        workers=1, total=1,
    )
    db_session.add(execution)
    db_session.flush()
    db_session.add(
        ExecutionResult(
            execution_id=execution.id, test_case_id=case.id,
            ticket_external_id=case.ticket_external_id, case_code=case.code,
            title=case.title, status="pending",
        )
    )
    db_session.commit()
    db_session.refresh(execution)
    return execution


# --------------------------------------------------------------- device management
def test_pair_code_redeem_list_revoke_http_flow(client, db_session):
    _make_user(db_session, "http-agent@example.com")
    token = _login(client, "http-agent@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    pair_resp = client.post("/agent/devices/pair-code", headers=headers)
    assert pair_resp.status_code == 200
    pair_body = pair_resp.json()
    assert pair_body["expiresIn"] == 300
    code = pair_body["code"]

    # Redemption is NOT authenticated by a user bearer token — the code itself is the auth.
    redeem_resp = client.post("/agent/devices/redeem", json={"code": code, "name": "My Laptop"})
    assert redeem_resp.status_code == 200
    redeem_body = redeem_resp.json()
    device_token = redeem_body["deviceToken"]
    device_id = redeem_body["deviceId"]

    list_resp = client.get("/agent/devices", headers=headers)
    assert list_resp.status_code == 200
    devices = list_resp.json()
    assert len(devices) == 1
    assert devices[0]["id"] == device_id
    assert devices[0]["name"] == "My Laptop"

    revoke_resp = client.delete(f"/agent/devices/{device_id}", headers=headers)
    assert revoke_resp.status_code == 200
    assert client.get("/agent/devices", headers=headers).json() == []

    # The revoked device's token must no longer authenticate against the job protocol.
    job_resp = client.post("/agent/jobs/next", headers={"Authorization": f"Bearer {device_token}"})
    assert job_resp.status_code == 401


def test_disconnect_self_revokes_device_and_removes_from_list(client, db_session):
    user = _make_user(db_session, "disconnect@example.com")
    device, device_token = _pair_device(db_session, user)
    token = _login(client, "disconnect@example.com")
    user_headers = {"Authorization": f"Bearer {token}"}

    assert len(client.get("/agent/devices", headers=user_headers).json()) == 1

    # The device disconnects itself (authenticated by its own device token).
    resp = client.post("/agent/disconnect", headers={"Authorization": f"Bearer {device_token}"})
    assert resp.status_code == 200

    # It drops off the owner's list immediately, and its token no longer works.
    assert client.get("/agent/devices", headers=user_headers).json() == []
    assert client.post("/agent/jobs/next", headers={"Authorization": f"Bearer {device_token}"}).status_code == 401


def test_disconnect_requires_a_device_token(client):
    assert client.post("/agent/disconnect").status_code == 401
    assert client.post("/agent/disconnect", headers={"Authorization": "Bearer bogus"}).status_code == 401


def test_redeem_rejects_garbage_code(client):
    resp = client.post("/agent/devices/redeem", json={"code": "not-a-real-code"})
    assert resp.status_code == 401


def test_revoke_unknown_device_returns_404(client, db_session):
    _make_user(db_session, "revoke-404@example.com")
    token = _login(client, "revoke-404@example.com")
    resp = client.delete("/agent/devices/999999", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404


# ---------------------------------------------------------- execution-target routing
def test_start_execution_local_agent_requires_paired_device_409(client, db_session):
    user = _make_user(db_session, "no-device@example.com")
    run, _case = _seed_agent_run(db_session, user.id)

    resp = client.post(f"/runs/{run.id}/execution", json={"target": "local-agent"})
    assert resp.status_code == 409


def test_start_execution_local_agent_queues_without_spawning_thread(client, db_session, monkeypatch):
    import app.services.playwright_runner as runner_module

    user = _make_user(db_session, "with-device@example.com")
    _pair_device(db_session, user)
    run, _case = _seed_agent_run(db_session, user.id)

    called = []
    monkeypatch.setattr(runner_module, "run_execution", lambda execution_id: called.append(execution_id))

    resp = client.post(f"/runs/{run.id}/execution", json={"target": "local-agent"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    assert body["target"] == "local-agent"
    assert called == []  # the in-process runner thread must never spawn for a local-agent target


def test_start_execution_default_target_is_server(client, db_session, monkeypatch):
    """No 'target' in the body -> falls back to the (default) 'server' setting, unchanged behavior."""
    import app.services.playwright_runner as runner_module

    monkeypatch.setattr(runner_module, "run_execution", lambda execution_id: None)
    user = _make_user(db_session, "default-target@example.com")
    run, _case = _seed_agent_run(db_session, user.id)

    resp = client.post(f"/runs/{run.id}/execution", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["target"] == "server"
    assert body["status"] == "running"


def test_run_single_spec_on_terminal_run_commits_execution(client, db_session):
    """Regression (#247): a manual "Run anyway" on a terminal (failed/blocked)
    run must durably COMMIT the queued local-agent execution.

    ``set_run_status`` short-circuits (returns False, no commit) on a terminal
    run, and ``get_db`` never auto-commits, so ``run_single_spec`` must commit
    itself — otherwise the flushed Execution/ExecutionResult roll back on session
    close: the endpoint returns 200 (from the in-memory object) but nothing is
    queued and the Local Agent has nothing to claim.
    """
    import app.db as db_module

    user = _make_user(db_session, "terminal-run@example.com")
    _pair_device(db_session, user)
    run, case = _seed_agent_run(db_session, user.id)
    run.status = "failed"  # terminal — the "run anyway" case (#245)
    db_session.commit()

    resp = client.post(f"/cases/{case.id}/spec/run", json={"target": "local-agent"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "queued"

    # A SEPARATE session sees only COMMITTED rows. Pre-fix the execution was
    # flushed but never committed, so this query came back empty.
    other = db_module.SessionLocal()
    try:
        execs = other.query(Execution).filter(Execution.run_id == run.id).all()
    finally:
        other.close()
    assert len(execs) == 1
    assert execs[0].status == "queued"
    assert execs[0].target == "local-agent"


# ------------------------------------------------------------------- job protocol
def test_job_endpoints_reject_missing_or_invalid_token(client):
    assert client.post("/agent/jobs/next").status_code == 401
    assert client.post(
        "/agent/jobs/next", headers={"Authorization": "Bearer bogus-token"}
    ).status_code == 401


def test_claim_next_job_atomic_and_wont_double_claim(client, db_session):
    user = _make_user(db_session, "claimer@example.com")
    device, token = _pair_device(db_session, user)
    run, case = _seed_agent_run(db_session, user.id)
    execution = _queued_execution(db_session, run, case)

    headers = {"Authorization": f"Bearer {token}"}
    first = client.post("/agent/jobs/next", headers=headers)
    assert first.status_code == 200
    body = first.json()
    assert body["executionId"] == execution.id
    assert body["runCode"] == run.code
    assert body["specs"] == [
        {
            # #540: the job payload filename comes from `spec_service.spec_filename`,
            # which now carries the full ticket id.
            "filename": "SUR-1428-TC-01.spec.ts",
            "code": "// spec code",
            "ticketExternalId": "SUR-1428",
            "caseCode": "TC-01",
        }
    ]
    assert body["manualAuth"] is False
    # Security requirement: the claim payload must NEVER carry session data.
    assert "storageState" not in first.text
    assert "sessionStorage" not in first.text

    db_session.refresh(execution)
    assert execution.status == "running"
    assert execution.claimed_by_device_id == device.id

    # Nothing else queued -> 204, and a second claim can't re-grab the same job.
    second = client.post("/agent/jobs/next", headers=headers)
    assert second.status_code == 204


def test_claim_next_job_scoped_to_owning_user(client, db_session):
    owner = _make_user(db_session, "job-owner@example.com")
    other = _make_user(db_session, "job-other@example.com")
    _device_owner, _token_owner = _pair_device(db_session, owner)
    _device_other, token_other = _pair_device(db_session, other)

    run, case = _seed_agent_run(db_session, owner.id)
    _queued_execution(db_session, run, case)

    resp = client.post("/agent/jobs/next", headers={"Authorization": f"Bearer {token_other}"})
    assert resp.status_code == 204


def test_push_job_event_reemits_via_hub_publish(client, db_session, monkeypatch):
    from app.ws import hub as hub_singleton

    user = _make_user(db_session, "events@example.com")
    device, token = _pair_device(db_session, user)
    run, case = _seed_agent_run(db_session, user.id)
    execution = _queued_execution(db_session, run, case, status="running")
    execution.claimed_by_device_id = device.id
    db_session.commit()

    captured = []
    monkeypatch.setattr(
        hub_singleton, "publish", lambda run_id, event, payload=None: captured.append((run_id, event, payload))
    )

    resp = client.post(
        f"/agent/jobs/{execution.id}/events",
        json={"event": "exec.case.running", "payload": {"caseCode": "TC-01"}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert captured == [(str(run.id), "exec.case.running", {"caseCode": "TC-01"})]


def test_push_job_result_upserts_matching_execution_result(client, db_session):
    user = _make_user(db_session, "results@example.com")
    _device, token = _pair_device(db_session, user)
    run, case = _seed_agent_run(db_session, user.id)
    execution = _queued_execution(db_session, run, case, status="running")

    resp = client.post(
        f"/agent/jobs/{execution.id}/results",
        json={"file": "1428-TC-01.spec.ts", "status": "pass", "duration_ms": 321, "error_message": ""},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200

    result = db_session.query(ExecutionResult).filter(ExecutionResult.execution_id == execution.id).first()
    db_session.refresh(result)
    assert result.status == "pass"
    assert result.duration_ms == 321
    assert resp.json()["resultId"] == result.id


def test_push_job_result_no_match_returns_400(client, db_session):
    user = _make_user(db_session, "no-match@example.com")
    _device, token = _pair_device(db_session, user)
    run, case = _seed_agent_run(db_session, user.id)
    execution = _queued_execution(db_session, run, case, status="running")

    resp = client.post(
        f"/agent/jobs/{execution.id}/results",
        json={"file": "nope.spec.ts", "status": "pass", "duration_ms": 1, "error_message": ""},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


def test_push_job_evidence_stores_file_and_row(client, db_session):
    from app.services.workspace_scope import scoped_evidence_dir

    user = _make_user(db_session, "evidence@example.com")
    _device, token = _pair_device(db_session, user)
    run, case = _seed_agent_run(db_session, user.id)
    execution = _queued_execution(db_session, run, case, status="running")

    files = {"file": ("failure.png", b"fake-png-bytes", "image/png")}
    data = {"ticket_external_id": case.ticket_external_id, "case_code": case.code, "kind": "screenshot"}
    resp = client.post(
        f"/agent/jobs/{execution.id}/evidence",
        data=data, files=files,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["filename"] == "failure.png"
    assert body["kind"] == "screenshot"

    evidence = db_session.get(Evidence, body["id"])
    assert evidence is not None
    dest = scoped_evidence_dir(run.owner_id) / evidence.path
    assert dest.exists()
    assert dest.read_bytes() == b"fake-png-bytes"


def test_push_job_evidence_no_matching_result_returns_404(client, db_session):
    user = _make_user(db_session, "evidence-404@example.com")
    _device, token = _pair_device(db_session, user)
    run, case = _seed_agent_run(db_session, user.id)
    execution = _queued_execution(db_session, run, case, status="running")

    files = {"file": ("failure.png", b"x", "image/png")}
    data = {"ticket_external_id": "SUR-9999", "case_code": "TC-99", "kind": "screenshot"}
    resp = client.post(
        f"/agent/jobs/{execution.id}/evidence",
        data=data, files=files,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


def test_complete_job_finalizes_execution_and_advances_run(client, db_session):
    from app.models.run import Run

    user = _make_user(db_session, "complete@example.com")
    _device, token = _pair_device(db_session, user)
    run, case = _seed_agent_run(db_session, user.id)
    execution = _queued_execution(db_session, run, case, status="running")

    resp = client.post(
        f"/agent/jobs/{execution.id}/complete",
        json={"passed": 1, "failed": 0, "log": "all good"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200

    db_session.refresh(execution)
    assert execution.status == "done"
    assert execution.passed == 1
    assert execution.failed == 0
    assert execution.log == "all good"

    refreshed_run = db_session.get(Run, run.id)
    assert refreshed_run.status == "evidence"


def test_job_endpoints_404_for_execution_not_owned_by_device_user(client, db_session):
    owner = _make_user(db_session, "scope-owner@example.com")
    other = _make_user(db_session, "scope-other@example.com")
    _device_owner, _token_owner = _pair_device(db_session, owner)
    _device_other, token_other = _pair_device(db_session, other)

    run, case = _seed_agent_run(db_session, owner.id)
    execution = _queued_execution(db_session, run, case, status="running")

    resp = client.post(
        f"/agent/jobs/{execution.id}/complete",
        json={"passed": 0, "failed": 0, "log": ""},
        headers={"Authorization": f"Bearer {token_other}"},
    )
    assert resp.status_code == 404
