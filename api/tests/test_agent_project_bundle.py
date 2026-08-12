"""The layered-project bundle + version guard on the Local Agent claim (#541).

Two things are proved here, because both are silent failures if they regress:

* **The bundle ships wholesale, correctly, and with the right exclusions.** The
  agent is stateless and cannot fetch anything — whatever is missing from the
  claim simply does not exist on the device, and every import that needed it
  fails collection.
* **Version skew is refused, in both directions.** A device below the minimum —
  **or reporting no version at all** — would flatten the nested tree into one
  directory and produce a wall of import errors that reads as a mass test
  failure. The server must fail the execution with one legible reason instead.
"""

from __future__ import annotations

import pytest

from app.models.execution import Execution, ExecutionResult
from app.services import agent_device_service, agent_project_bundle, auth_service


# --------------------------------------------------------------------- unit: the guard
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0.2.0", (0, 2, 0)),
        ("v0.2.1", (0, 2, 1)),
        ("1.10.3", (1, 10, 3)),
        ("0.2.0-rc1", (0, 2, 0)),
        (" 0.2.0 ", (0, 2, 0)),
        ("unknown", None),
        ("", None),
        (None, None),
        ("0.2", None),
        ("garbage", None),
    ],
)
def test_parse_version(raw, expected):
    assert agent_project_bundle.parse_version(raw) == expected


def test_version_ok_treats_absent_and_unparseable_as_below_minimum():
    """The whole point of the guard: no version reported == too old."""
    assert agent_project_bundle.version_ok(None) is False
    assert agent_project_bundle.version_ok("") is False
    assert agent_project_bundle.version_ok("unknown") is False
    assert agent_project_bundle.version_ok("0.1.29") is False
    assert agent_project_bundle.version_ok(agent_project_bundle.MIN_AGENT_VERSION) is True
    assert agent_project_bundle.version_ok("0.2.1") is True
    assert agent_project_bundle.version_ok("1.0.0") is True


# ------------------------------------------------------------------------------ helpers
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
    from app.models.run import Run
    from app.models.testcase import AutomationSpec, TestCase

    run = Run(
        code=f"RUN-BUNDLE-{owner_id}", name="Layered run", status="automation", workers=2,
        owner_id=owner_id,
    )
    db_session.add(run)
    db_session.flush()
    case = TestCase(
        run_id=run.id, ticket_external_id="SUR-1428", code="TC-01", title="Login works",
        approval="approved", automation="Playwright",
    )
    db_session.add(case)
    db_session.flush()
    db_session.add(AutomationSpec(test_case_id=case.id, filename="1428-TC-01.spec.ts", code="// spec code"))
    db_session.commit()
    db_session.refresh(run)
    db_session.refresh(case)
    return run, case


def _queued_execution(db_session, run, case) -> Execution:
    execution = Execution(
        run_id=run.id, status="queued", target="local-agent", env=run.env, browser=run.browser,
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


def _make_layered_project(db_session, owner_id: int, case, spec_code: str = "// layered spec"):
    """Attach the run's spec to a REAL on-disk automation project.

    The bundle is read from disk by ``automation_project_service.bundle_for_agent``,
    so the tree has to genuinely exist — including a ``tests/`` dir that must not be
    bundled and a ``.qagent/`` dir that must never leave the server.
    """
    from app.models.testcase import AutomationSpec
    from app.services import automation_project_service, spec_service

    project = automation_project_service.ensure_project(db_session, owner_id, "SUR", "web")
    root = automation_project_service.project_dir(project)
    (root / "pages").mkdir(parents=True, exist_ok=True)
    (root / "pages" / "LoginPage.ts").write_text(
        "import { Page } from '@q-agent/playwright-base';\nexport class LoginPage {}\n",
        encoding="utf-8",
    )
    (root / "components" / "nav").mkdir(parents=True, exist_ok=True)
    (root / "components" / "nav" / "TopNav.ts").write_text("export class TopNav {}\n", encoding="utf-8")
    (root / ".qagent").mkdir(parents=True, exist_ok=True)
    (root / ".qagent" / "inventory.json").write_text("[]", encoding="utf-8")

    written = automation_project_service.write_spec(
        project, case.ticket_external_id, case.code, spec_code
    )
    relative = written.relative_to(root).as_posix()
    expected_name = spec_service.spec_filename(case.ticket_external_id, case.code)
    assert relative == f"tests/{case.ticket_external_id}/{expected_name}"

    spec = db_session.query(AutomationSpec).filter(AutomationSpec.test_case_id == case.id).first()
    spec.project_id = project.id
    spec.filename = relative
    spec.code = spec_code
    db_session.commit()
    return project, spec


# ------------------------------------------------------------------------- the claim
def test_claim_ships_the_project_bundle_with_correct_nesting(client, db_session):
    user = _make_user(db_session, "layered@example.com")
    _device, token = _pair_device(db_session, user)
    run, case = _seed_agent_run(db_session, user.id)
    _make_layered_project(db_session, user.id, case)
    execution = _queued_execution(db_session, run, case)

    resp = client.post(
        "/agent/jobs/next",
        json={"agentVersion": agent_project_bundle.MIN_AGENT_VERSION},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["executionId"] == execution.id

    bundle = body["project"]
    # baseVersion ships from day one so a later delta protocol needs no payload change.
    assert bundle["baseVersion"] == "1"
    paths = {f["path"] for f in bundle["files"]}
    assert "pages/LoginPage.ts" in paths
    assert "components/nav/TopNav.ts" in paths, "nested library dirs keep their nesting"
    assert {"playwright.config.ts", "package.json", "tsconfig.json"} <= paths
    # Exclusions: other runs' specs, server-side plans, installed packages.
    assert not any(p.startswith("tests/") for p in paths), "tests/** is never bundled"
    assert not any(p.startswith(".qagent") for p in paths), ".qagent/** never leaves the server"
    assert not any("node_modules" in p for p in paths)
    # Source actually rides along — this is the point; the agent cannot fetch it.
    code = {f["path"]: f["code"] for f in bundle["files"]}
    assert "export class LoginPage" in code["pages/LoginPage.ts"]

    # The spec's filename is project-relative so the agent nests it under
    # tests/<TICKET>/. The FILE name itself comes from spec_service (#540 changes
    # it from the short form to the full ticket id), so it is not hardcoded here —
    # only the nesting, which is this slice's contract, is asserted literally.
    from app.services import spec_service

    expected_name = spec_service.spec_filename("SUR-1428", "TC-01")
    assert body["specs"][0]["filename"] == f"tests/SUR-1428/{expected_name}"
    assert body["specs"][0]["filename"].startswith("tests/SUR-1428/")
    assert body["specs"][0]["ticketExternalId"] == "SUR-1428"
    # Still no session data in a layered payload.
    assert "storageState" not in resp.text


def test_claim_refuses_a_layered_run_when_the_device_reports_no_version(client, db_session):
    """A pre-#541 agent sends no body at all — treat it as below minimum."""
    user = _make_user(db_session, "noversion@example.com")
    _device, token = _pair_device(db_session, user)
    run, case = _seed_agent_run(db_session, user.id)
    _make_layered_project(db_session, user.id, case)
    execution = _queued_execution(db_session, run, case)

    resp = client.post("/agent/jobs/next", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 409, resp.text
    assert "Update your Local Agent" in resp.json()["detail"]

    # The execution fails CLEANLY instead of being handed over to produce import
    # errors, and it is not left "running" forever.
    db_session.expire_all()
    refreshed = db_session.get(Execution, execution.id)
    assert refreshed.status == "done"
    assert (refreshed.passed, refreshed.failed) == (0, 1)
    assert agent_project_bundle.UPDATE_MESSAGE in (refreshed.log or "")
    result = (
        db_session.query(ExecutionResult)
        .filter(ExecutionResult.execution_id == execution.id)
        .first()
    )
    assert result.status == "fail"
    assert "Update your Local Agent" in result.error_message

    # Nothing left claimable, so the refusal cannot loop.
    assert client.post("/agent/jobs/next", headers={"Authorization": f"Bearer {token}"}).status_code == 204


def test_claim_refuses_a_layered_run_below_the_minimum_version(client, db_session):
    from app.models.agent_device import AgentDevice

    user = _make_user(db_session, "oldversion@example.com")
    device, token = _pair_device(db_session, user)
    run, case = _seed_agent_run(db_session, user.id)
    _make_layered_project(db_session, user.id, case)
    _queued_execution(db_session, run, case)

    resp = client.post(
        "/agent/jobs/next",
        json={"agentVersion": "0.1.29"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409, resp.text
    assert "Update your Local Agent" in resp.json()["detail"]
    # The version is still recorded, so the devices list can show WHY it was refused.
    db_session.expire_all()
    assert db_session.get(AgentDevice, device.id).agent_version == "0.1.29"


def test_claim_records_agent_version_and_exposes_it_on_the_devices_list(client, db_session):
    from app.models.agent_device import AgentDevice

    user = _make_user(db_session, "stamped@example.com")
    device, token = _pair_device(db_session, user)
    run, case = _seed_agent_run(db_session, user.id)
    _queued_execution(db_session, run, case)

    assert device.agent_version == ""
    resp = client.post(
        "/agent/jobs/next",
        json={"agentVersion": "9.9.9"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    assert db_session.get(AgentDevice, device.id).agent_version == "9.9.9"

    user_token = _login(client, "stamped@example.com")
    listed = client.get("/agent/devices", headers={"Authorization": f"Bearer {user_token}"}).json()
    assert listed[0]["agentVersion"] == "9.9.9"


def test_legacy_claim_is_unaffected_by_the_version_guard(client, db_session):
    """Every existing spec is ``project_id IS NULL`` and must run on any agent build."""
    user = _make_user(db_session, "legacy@example.com")
    _device, token = _pair_device(db_session, user)
    run, case = _seed_agent_run(db_session, user.id)
    _queued_execution(db_session, run, case)

    # No version, no body — the pre-#541 wire call.
    resp = client.post("/agent/jobs/next", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "project" not in body, "a legacy payload is unchanged from before"
    # #540 changed `spec_filename` to carry the FULL ticket id, so that two tickets
    # sharing a short id (SUR-1428 / OPS-1428) cannot collide in one persistent
    # project. Legacy claims get the new name too — that is fine and intended:
    # results pushed back in either form still match, because
    # `execution_service.match_result` tries the full form first and falls back to
    # the legacy short form. What this test guards is the *version guard*, i.e. that
    # a `project_id IS NULL` spec ships with no `project` bundle on any agent build.
    assert body["specs"][0]["filename"] == "SUR-1428-TC-01.spec.ts"


def test_claim_fails_fast_when_the_bundle_is_over_cap(client, db_session, monkeypatch):
    user = _make_user(db_session, "overcap@example.com")
    _device, token = _pair_device(db_session, user)
    run, case = _seed_agent_run(db_session, user.id)
    _make_layered_project(db_session, user.id, case)
    execution = _queued_execution(db_session, run, case)

    # A tiny cap makes the real tree over-cap without writing megabytes to disk.
    monkeypatch.setattr(agent_project_bundle, "BUNDLE_MAX_BYTES", 16)

    resp = client.post(
        "/agent/jobs/next",
        json={"agentVersion": agent_project_bundle.MIN_AGENT_VERSION},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 413, resp.text
    assert "too large" in resp.json()["detail"]

    db_session.expire_all()
    refreshed = db_session.get(Execution, execution.id)
    assert refreshed.status == "done" and refreshed.failed == 1
    result = (
        db_session.query(ExecutionResult)
        .filter(ExecutionResult.execution_id == execution.id)
        .first()
    )
    assert result.status == "fail" and "too large" in result.error_message


def test_bundle_payload_logs_and_measures_the_real_tree(client, db_session):
    user = _make_user(db_session, "measured@example.com")
    _run, case = _seed_agent_run(db_session, user.id)
    project, _spec = _make_layered_project(db_session, user.id, case)

    payload, total = agent_project_bundle.bundle_payload(project)
    assert total == sum(len(f["code"].encode("utf-8")) for f in payload["files"])
    assert total > 0
    # Sorted paths keep the payload byte-stable for a future content-hash protocol.
    assert [f["path"] for f in payload["files"]] == sorted(f["path"] for f in payload["files"])
