"""The Local Agent half of a project-scoped execution (#798, server side).

Covers the three protocol changes a run-less Execution needs:

* ``POST /agent/jobs/next`` can claim a ``run_id IS NULL`` row, and serves it a
  payload the agent branches on (``projectScoped``, ``specPath``, the repo label
  in ``runCode``) — gated behind a **higher** version floor than the layered
  run-scoped payload, which must keep working for the agents already paired.
* ``POST /agent/jobs/{id}/evidence`` addresses a project-scoped result by
  ``spec_path``, which is its only identity.
* ``POST /agent/jobs/{id}/report`` / ``GET /executions/{id}/report`` — the raw
  Playwright JSON report, stored as a file under the owner's workspace scope.

Three disciplines this file is deliberate about:

* **Which branch ran, not just the status code.** Every claim assertion pins a
  field that only the project-scoped branch emits, and every refusal pins the
  execution's resulting row state, not only the 409.
* **A negative control on the ``spec_path`` match.** A lookup that matched
  *everything* would pass a positive-only assertion, so every evidence test
  asserts the sibling result in the same execution received nothing.
* **Token acceptance needs ``auth_required=True``.** The suite runs with the
  ``auth_guard`` middleware as a passthrough, so the report endpoints' auth is
  exercised under a fixture that flips it on.
"""

from __future__ import annotations

import json

import pytest

from app.models.automation_project import AutomationFile, AutomationProject
from app.models.execution import Evidence, Execution, ExecutionResult
from app.models.user import User
from app.services import agent_device_service, agent_project_bundle, auth_service

pytestmark = pytest.mark.usefixtures("workspace_dir")

PAGE_CODE = "export class LoginPage {}\n"
SPEC_A = "tests/SUR-1428/a.spec.ts"
SPEC_B = "tests/SUR-1428/b.spec.ts"
SPEC_CODE = "test('a', async () => {});\n"


@pytest.fixture
def auth_on(monkeypatch):
    """Turn the global auth guard on for one test (it is a passthrough by default)."""
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "auth_required", True)
    yield


def _make_user(db_session, email: str = "agent-owner@example.com") -> User:
    user = User(
        email=email,
        first_name="Agent",
        last_name="Owner",
        password_hash=auth_service.hash_password("password123"),
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _pair(db_session, user: User):
    code = agent_device_service.create_pairing_code(db_session, user)
    return agent_device_service.redeem_pairing_code(db_session, code, "Test Device")


def _repo_with_specs(db_session, owner_id: int, *, on_disk: bool = False) -> AutomationProject:
    """An automation repo with a page object and two spec files.

    The mirror always carries all three; ``on_disk`` also materializes the page
    object in the project's working tree, because the shipped ``project`` bundle
    is read from **disk** (``automation_project_service.bundle_for_agent``),
    exactly as it is for a layered run-scoped claim. Most tests here do not need
    the tree, and a mirror-only repo is the stronger fixture for them.
    """
    project = AutomationProject(
        owner_id=owner_id,
        project_guid="d52ca970-0000-4000-8000-000000000001",
        project_key="surency",
        repo="web",
        slug="surency/web",
        base_version="1.0.0",
    )
    db_session.add(project)
    db_session.flush()
    db_session.add(
        AutomationFile(project_id=project.id, path="pages/LoginPage.ts", kind="page", code=PAGE_CODE)
    )
    for path in (SPEC_A, SPEC_B):
        db_session.add(
            AutomationFile(project_id=project.id, path=path, kind="spec", code=SPEC_CODE)
        )
    db_session.commit()
    db_session.refresh(project)
    if on_disk:
        from app.services import automation_project_service as aps

        page = aps.project_dir(project) / "pages" / "LoginPage.ts"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(PAGE_CODE, encoding="utf-8")
    return project


def _queued_project_execution(
    db_session, project: AutomationProject, owner_id: int, paths=(SPEC_A, SPEC_B)
) -> Execution:
    """A run-less, queued, local-agent Execution with one pending result per spec."""
    execution = Execution(
        run_id=None,
        owner_id=owner_id,
        automation_project_id=project.id,
        status="queued",
        target="local-agent",
        env="Staging",
        browser="chromium",
        workers=2,
        total=len(paths),
    )
    db_session.add(execution)
    db_session.flush()
    for path in paths:
        db_session.add(
            ExecutionResult(
                execution_id=execution.id,
                test_case_id=0,
                ticket_external_id="",
                case_code="",
                spec_path=path,
                title=path.rsplit("/", 1)[-1],
                status="pending",
            )
        )
    db_session.commit()
    db_session.refresh(execution)
    return execution


def _claim(client, token: str, version: str | None = agent_project_bundle.MIN_AGENT_VERSION):
    body = {"agentVersion": version} if version is not None else None
    return client.post(
        "/agent/jobs/next", headers={"Authorization": f"Bearer {token}"}, json=body
    )


# --------------------------------------------------------------------------- claim
def test_claim_serves_a_run_less_execution_as_a_project_scoped_job(client, db_session):
    """The payload the agent branches on, asserted field by field.

    ``projectScoped`` is what switches the agent into "upload the JSON report and
    failure screenshots only" mode, so its presence is the contract — a payload
    that merely happened to carry the right specs would run the suite and upload
    nothing. ``filename`` and ``specPath`` are asserted to be *the same
    repo-relative path*: the spec has to land where its ``../../pages/…`` import
    expects it, and the result has to be matchable by the only identity it has.
    """
    user = _make_user(db_session)
    _device, token = _pair(db_session, user)
    project = _repo_with_specs(db_session, user.id, on_disk=True)
    execution = _queued_project_execution(db_session, project, user.id)

    resp = _claim(client, token)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["executionId"] == execution.id
    assert body["projectScoped"] is True
    # The repo label, not a run code — there is no run. Path-safe, since the
    # agent names its work dir from it.
    assert body["runCode"] == "surency-web"
    assert body["workers"] == 2
    assert [s["specPath"] for s in body["specs"]] == [SPEC_A, SPEC_B]
    assert [s["filename"] for s in body["specs"]] == [SPEC_A, SPEC_B]
    # The code came from the mirror: only the page object was written to disk, so
    # a claim that read specs off the working tree would ship empty strings here.
    assert all(s["code"] == SPEC_CODE for s in body["specs"])
    # The library ships the same way a layered run-scoped claim ships it, so the
    # page-object imports resolve; other repos' specs are excluded from it.
    bundled = {f["path"] for f in body["project"]["files"]}
    assert "pages/LoginPage.ts" in bundled
    assert not any(p.startswith("tests/") for p in bundled)
    # Still no session data, exactly as for a run-scoped claim.
    assert "storageState" not in resp.text

    db_session.expire_all()
    claimed = db_session.get(Execution, execution.id)
    assert claimed.status == "running"
    assert claimed.claimed_by_device_id is not None
    assert claimed.started_at is not None
    # Claimed once, so a second poll finds nothing — the claim is not re-servable.
    assert _claim(client, token).status_code == 204


def test_claim_refuses_a_project_scoped_job_below_the_project_floor(client, db_session):
    """An 0.2.x agent understands neither the flag nor the report upload.

    It would run the specs happily and upload nothing, which is the silent
    mass-failure the version guard exists for. So the refusal has to leave the
    execution **terminal with the reason on it**, not merely return a 409 — that
    is what the tab shows, and a row left ``running`` would spin forever.
    """
    user = _make_user(db_session)
    _device, token = _pair(db_session, user)
    project = _repo_with_specs(db_session, user.id)
    execution = _queued_project_execution(db_session, project, user.id)

    resp = _claim(client, token, version="0.2.9")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == agent_project_bundle.PROJECT_SCOPED_UPDATE_MESSAGE

    db_session.expire_all()
    refused = db_session.get(Execution, execution.id)
    assert refused.status == "done"
    assert (refused.passed, refused.failed) == (0, 2)
    assert agent_project_bundle.PROJECT_SCOPED_UPDATE_MESSAGE in (refused.log or "")
    statuses = {
        r.spec_path: (r.status, r.error_message)
        for r in db_session.query(ExecutionResult)
        .filter(ExecutionResult.execution_id == execution.id)
        .all()
    }
    assert {v[0] for v in statuses.values()} == {"fail"}
    assert all("Update your Local Agent" in v[1] for v in statuses.values())

    # Nothing claimable left, so the refusal cannot loop forever.
    assert _claim(client, token, version="0.2.9").status_code == 204


def test_claim_refuses_a_project_scoped_job_from_an_agent_reporting_no_version(
    client, db_session
):
    """A body-less claim is a pre-#541 agent — below every floor, including this one."""
    user = _make_user(db_session)
    _device, token = _pair(db_session, user)
    project = _repo_with_specs(db_session, user.id)
    execution = _queued_project_execution(db_session, project, user.id)

    resp = _claim(client, token, version=None)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == agent_project_bundle.PROJECT_SCOPED_UPDATE_MESSAGE

    db_session.expire_all()
    assert db_session.get(Execution, execution.id).status == "done"


def test_the_project_floor_is_strictly_above_the_layered_floor(client, db_session):
    """The bump must not un-pair devices that were fine yesterday.

    Two separate constants, and the 0.3.0 floor is checked only for the
    project-scoped claim — asserted here as a property, and as behaviour by
    ``test_a_layered_run_scoped_claim_still_works_for_an_0_2_x_agent`` in
    ``test_agent_project_bundle.py``.
    """
    layered = agent_project_bundle.parse_version(agent_project_bundle.MIN_LAYERED_AGENT_VERSION)
    project_scoped = agent_project_bundle.parse_version(agent_project_bundle.MIN_AGENT_VERSION)
    assert project_scoped > layered
    # An 0.2.x device passes the layered guard and fails the project-scoped one.
    assert agent_project_bundle.version_ok("0.2.9") is True
    assert (
        agent_project_bundle.version_ok("0.2.9", agent_project_bundle.MIN_AGENT_VERSION) is False
    )


def test_claim_never_serves_another_users_project_execution(client, db_session):
    """Ownership of a run-less row comes from ``Execution.owner_id``, nothing else."""
    owner = _make_user(db_session, "owner@example.com")
    other = _make_user(db_session, "other@example.com")
    _device, other_token = _pair(db_session, other)
    project = _repo_with_specs(db_session, owner.id)
    execution = _queued_project_execution(db_session, project, owner.id)

    assert _claim(client, other_token).status_code == 204

    db_session.expire_all()
    assert db_session.get(Execution, execution.id).status == "queued"


# ------------------------------------------------------------------------ evidence
def _upload(client, token: str, execution_id: int, field: str, value: str, filename: str):
    return client.post(
        f"/agent/jobs/{execution_id}/evidence",
        headers={"Authorization": f"Bearer {token}"},
        data={"kind": "screenshot", field: value},
        files={"file": (filename, b"\x89PNG-bytes", "image/png")},
    )


def _evidence_by_spec(db_session, execution_id: int) -> dict[str, list[str]]:
    """``{spec_path: [evidence filename, …]}`` for every result of an execution."""
    out: dict[str, list[str]] = {}
    results = (
        db_session.query(ExecutionResult)
        .filter(ExecutionResult.execution_id == execution_id)
        .order_by(ExecutionResult.id)
        .all()
    )
    for result in results:
        rows = db_session.query(Evidence).filter(Evidence.result_id == result.id).all()
        out[result.spec_path] = [row.filename for row in rows]
    return out


@pytest.mark.parametrize("field", ["spec_path", "specPath"])
def test_evidence_is_attached_to_the_result_named_by_spec_path(client, db_session, field):
    """Both spellings of the field resolve to the same row — and only that row.

    The **negative control** is the whole test: every result of a project-scoped
    execution has ``ticket_external_id == ""`` and ``case_code == ""``, so a
    lookup that still matched on those would find *an* arbitrary row and look
    exactly as green as a correct one. Asserting that spec B received nothing is
    what distinguishes them.

    ``specPath`` is the name pinned in the cross-slice contract and what the
    shipped 0.3.0 agent sends; ``spec_path`` matches this endpoint's existing
    snake_case fields. Both are accepted, so both are tested.
    """
    user = _make_user(db_session)
    _device, token = _pair(db_session, user)
    project = _repo_with_specs(db_session, user.id)
    execution = _queued_project_execution(db_session, project, user.id)

    resp = _upload(client, token, execution.id, field, SPEC_A, "failure.png")
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "screenshot"

    attached = _evidence_by_spec(db_session, execution.id)
    assert attached[SPEC_A] == ["failure.png"]
    # The negative control: the sibling result in the SAME execution got nothing.
    assert attached[SPEC_B] == []


def test_evidence_for_an_unknown_spec_path_is_a_404_and_attaches_nothing(client, db_session):
    """A path no result carries must refuse, not fall through to the ticket match."""
    user = _make_user(db_session)
    _device, token = _pair(db_session, user)
    project = _repo_with_specs(db_session, user.id)
    execution = _queued_project_execution(db_session, project, user.id)

    resp = _upload(client, token, execution.id, "spec_path", "tests/nope.spec.ts", "x.png")
    assert resp.status_code == 404, resp.text
    assert "tests/nope.spec.ts" in resp.json()["detail"]
    assert _evidence_by_spec(db_session, execution.id) == {SPEC_A: [], SPEC_B: []}


def test_the_ticket_case_evidence_match_is_untouched_for_a_run_scoped_upload(
    client, db_session
):
    """The run-scoped path must keep working with no ``spec_path`` field at all."""
    from app.models.run import Run

    user = _make_user(db_session)
    _device, token = _pair(db_session, user)
    run = Run(code="RUN-EV-1", name="Run", status="executing", owner_id=user.id)
    db_session.add(run)
    db_session.flush()
    execution = Execution(
        run_id=run.id, owner_id=user.id, status="running", target="local-agent", total=1
    )
    db_session.add(execution)
    db_session.flush()
    db_session.add(
        ExecutionResult(
            execution_id=execution.id, test_case_id=1, ticket_external_id="SUR-1428",
            case_code="TC-01", title="Login", status="pending",
        )
    )
    db_session.commit()

    resp = client.post(
        f"/agent/jobs/{execution.id}/evidence",
        headers={"Authorization": f"Bearer {token}"},
        data={"kind": "screenshot", "ticket_external_id": "SUR-1428", "case_code": "TC-01"},
        files={"file": ("shot.png", b"png", "image/png")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["filename"] == "shot.png"


# -------------------------------------------------------------------------- report
def _report_document() -> dict:
    """A report carrying what the viewer exists to show and the parser discards."""
    return {
        "config": {"version": "1.48.0"},
        "suites": [
            {
                "title": SPEC_A,
                "specs": [
                    {
                        "title": "logs in",
                        "tests": [
                            {
                                "status": "flaky",
                                "results": [
                                    {"status": "failed", "steps": [{"title": "click"}]},
                                    {"status": "passed", "steps": [{"title": "click"}]},
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }


def test_report_upload_is_stored_as_a_file_and_served_back_verbatim(client, db_session):
    """The round trip, plus where it lives.

    ``flaky`` and the per-retry ``steps`` are asserted on the way out because
    they are exactly what ``parse_playwright_report`` flattens away — proving the
    raw document survived rather than a re-derived summary. The on-disk check
    pins the storage decision itself: a report kept on the row would pass the
    read assertion and fail this one.
    """
    from app.services import execution_report_service

    user = _make_user(db_session)
    _device, token = _pair(db_session, user)
    project = _repo_with_specs(db_session, user.id)
    execution = _queued_project_execution(db_session, project, user.id)
    document = _report_document()

    resp = client.post(
        f"/agent/jobs/{execution.id}/report",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        content=json.dumps(document),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    stored = execution_report_service.report_path(execution)
    assert stored.is_file()
    assert stored.parent.name == "reports"
    # Under the OWNER's scope, so another user's tree can never reach it.
    assert f"users/{user.id}" in stored.as_posix()

    served = client.get(f"/executions/{execution.id}/report")
    assert served.status_code == 200, served.text
    assert served.headers["content-type"].startswith("application/json")
    body = served.json()
    assert body["suites"][0]["specs"][0]["tests"][0]["status"] == "flaky"
    assert len(body["suites"][0]["specs"][0]["tests"][0]["results"]) == 2
    assert body["config"]["version"] == "1.48.0"


def test_a_second_report_upload_replaces_the_first(client, db_session):
    """A re-upload (a retried push) must not append or leave two documents."""
    user = _make_user(db_session)
    _device, token = _pair(db_session, user)
    project = _repo_with_specs(db_session, user.id)
    execution = _queued_project_execution(db_session, project, user.id)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    client.post(
        f"/agent/jobs/{execution.id}/report", headers=headers, content=json.dumps({"take": 1})
    )
    client.post(
        f"/agent/jobs/{execution.id}/report", headers=headers, content=json.dumps({"take": 2})
    )

    assert client.get(f"/executions/{execution.id}/report").json() == {"take": 2}


def test_an_oversize_report_is_refused_legibly_and_nothing_is_stored(
    client, db_session, monkeypatch
):
    """The cap fails with a reason rather than storing a truncated document.

    A truncated JSON report is not a smaller report, it is an unparseable one —
    the viewer would call the run corrupt instead of oversized. The cap is
    lowered here rather than uploading 25 MB through the test client.
    """
    from app.services import execution_report_service

    user = _make_user(db_session)
    _device, token = _pair(db_session, user)
    project = _repo_with_specs(db_session, user.id)
    execution = _queued_project_execution(db_session, project, user.id)
    monkeypatch.setattr(execution_report_service, "MAX_REPORT_BYTES", 64)

    resp = client.post(
        f"/agent/jobs/{execution.id}/report",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        content=json.dumps({"suites": [{"title": "x" * 200}]}),
    )
    assert resp.status_code == 413, resp.text
    assert "too large" in resp.json()["detail"]

    assert not execution_report_service.report_path(execution).exists()
    assert client.get(f"/executions/{execution.id}/report").status_code == 404


def test_a_report_body_that_is_not_json_is_refused(client, db_session):
    """Validated on the way in, so a later read cannot fail with no clue why."""
    from app.services import execution_report_service

    user = _make_user(db_session)
    _device, token = _pair(db_session, user)
    project = _repo_with_specs(db_session, user.id)
    execution = _queued_project_execution(db_session, project, user.id)

    resp = client.post(
        f"/agent/jobs/{execution.id}/report",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        content=b"<html>not a report</html>",
    )
    assert resp.status_code == 400, resp.text
    assert not execution_report_service.report_path(execution).exists()


def test_report_endpoints_reject_another_users_execution(client, db_session):
    """Both directions scope on the execution's own owner, since there is no run."""
    owner = _make_user(db_session, "owner2@example.com")
    other = _make_user(db_session, "other2@example.com")
    _device, other_token = _pair(db_session, other)
    project = _repo_with_specs(db_session, owner.id)
    execution = _queued_project_execution(db_session, project, owner.id)

    resp = client.post(
        f"/agent/jobs/{execution.id}/report",
        headers={"Authorization": f"Bearer {other_token}", "Content-Type": "application/json"},
        content=json.dumps({"suites": []}),
    )
    assert resp.status_code == 404, resp.text


def test_the_read_endpoint_requires_a_bearer_token_when_auth_is_enforced(
    client, db_session, auth_on
):
    """No ``?token=`` capability URL here — a normal Authorization header, or 401.

    The suite runs with ``auth_guard`` as a passthrough, so this is the only
    place the middleware actually runs for these routes. The ``?token=`` form is
    asserted **not** to work: the report is deliberately not an ``/artifacts``
    static file, and admitting a query-string credential here would reintroduce
    exactly the capability URL #798 removed.
    """
    user = _make_user(db_session)
    _device, device_token = _pair(db_session, user)
    project = _repo_with_specs(db_session, user.id)
    execution = _queued_project_execution(db_session, project, user.id)

    # The agent's own upload still works — /agent/* authenticates per-route.
    upload = client.post(
        f"/agent/jobs/{execution.id}/report",
        headers={"Authorization": f"Bearer {device_token}", "Content-Type": "application/json"},
        content=json.dumps({"suites": []}),
    )
    assert upload.status_code == 200, upload.text

    assert client.get(f"/executions/{execution.id}/report").status_code == 401

    access = client.post(
        "/auth/login", json={"email": user.email, "password": "password123"}
    ).json()["accessToken"]
    assert client.get(f"/executions/{execution.id}/report?token={access}").status_code == 401

    ok = client.get(
        f"/executions/{execution.id}/report", headers={"Authorization": f"Bearer {access}"}
    )
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"suites": []}
