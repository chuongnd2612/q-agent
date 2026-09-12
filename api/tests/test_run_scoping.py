"""Tests for #92 — scoping the run domain to its owner.

With ``auth_required=True`` two users each get a bearer token; user A's run is
invisible to user B across reads/mutations, the run's ``/artifacts`` files, and
its WS channel. The rest of the suite runs with auth disabled (the
``owned``/``get_owned_or_404`` bridge from #91 is then a no-op), so this file
is the only place these owner checks are exercised end-to-end.
"""

from __future__ import annotations

import pytest

from app.models.run import Run, RunTicket
from app.models.user import User
from app.services import auth_service
from app.services.workspace_scope import scope_for, scoped_evidence_dir


@pytest.fixture
def auth_on(monkeypatch):
    """Turn the global auth guard on for the duration of a test."""
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "auth_required", True)
    yield


def _make_user(db_session, email: str) -> User:
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


def _token(user: User) -> str:
    return auth_service.create_access_token(user, sid=f"sid-{user.id}")


def _make_owned_run(db_session, owner: User, code: str = "RUN-800") -> Run:
    run = Run(code=code, name="Owned run", scope="selected", status="done", owner_id=owner.id)
    db_session.add(run)
    db_session.flush()
    db_session.add(RunTicket(run_id=run.id, ticket_external_id="SUR-1", position=0))
    db_session.commit()
    db_session.refresh(run)
    return run


@pytest.fixture
def two_users(db_session):
    user_a = _make_user(db_session, "owner-a@example.com")
    user_b = _make_user(db_session, "other-b@example.com")
    return user_a, user_b


def test_owner_can_read_their_own_run(client, db_session, auth_on, two_users):
    user_a, _ = two_users
    run = _make_owned_run(db_session, user_a)
    headers = {"Authorization": f"Bearer {_token(user_a)}"}

    assert client.get(f"/runs/{run.id}", headers=headers).status_code == 200
    listed = client.get("/runs", headers=headers).json()
    assert [r["id"] for r in listed] == [run.id]
    assert client.get(f"/runs/{run.id}/tickets", headers=headers).status_code == 200


def test_other_user_gets_404_on_run_reads(client, db_session, auth_on, two_users):
    user_a, user_b = two_users
    run = _make_owned_run(db_session, user_a)
    headers_b = {"Authorization": f"Bearer {_token(user_b)}"}

    assert client.get(f"/runs/{run.id}", headers=headers_b).status_code == 404
    assert client.get(f"/runs/{run.id}/tickets", headers=headers_b).status_code == 404
    # The owner's run is excluded from user B's list entirely, not just 404'd.
    assert client.get("/runs", headers=headers_b).json() == []


def test_other_user_gets_404_on_run_mutations(client, db_session, auth_on, two_users):
    user_a, user_b = two_users
    run = _make_owned_run(db_session, user_a)
    headers_b = {"Authorization": f"Bearer {_token(user_b)}"}

    assert client.post(f"/runs/{run.id}/cancel", headers=headers_b).status_code == 404
    assert client.delete(f"/runs/{run.id}", headers=headers_b).status_code == 404


def test_other_user_gets_404_on_artifacts(client, db_session, auth_on, two_users):
    """The run's evidence files aren't reachable by anyone but its owner.

    Evidence now lives at ``<scope>/evidence/<RUN-CODE>/...`` (ADR 0009 §5), so
    the served URL carries the owner's scope segment ahead of ``evidence/``.
    """
    user_a, user_b = two_users
    run = _make_owned_run(db_session, user_a)

    evidence_dir = scoped_evidence_dir(user_a.id) / run.code
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "shot.png").write_bytes(b"fake-png")

    url = f"/artifacts/{scope_for(user_a.id)}/evidence/{run.code}/shot.png"
    assert client.get(url, params={"token": _token(user_a)}).status_code == 200
    assert client.get(url, params={"token": _token(user_b)}).status_code == 404


def test_two_owners_evidence_scoped_and_guarded(client, db_session, auth_on, two_users):
    """Two runs owned by different users each build evidence under their own
    scope; the guard allows each owner and 404s the other owner, at the new
    ``<scope>/evidence/<RUN-CODE>/...`` path shape."""
    user_a, user_b = two_users
    run_a = _make_owned_run(db_session, user_a, code="RUN-801")
    run_b = _make_owned_run(db_session, user_b, code="RUN-802")

    dir_a = scoped_evidence_dir(user_a.id) / run_a.code
    dir_a.mkdir(parents=True, exist_ok=True)
    (dir_a / "shot.png").write_bytes(b"fake-png-a")

    dir_b = scoped_evidence_dir(user_b.id) / run_b.code
    dir_b.mkdir(parents=True, exist_ok=True)
    (dir_b / "shot.png").write_bytes(b"fake-png-b")

    url_a = f"/artifacts/{scope_for(user_a.id)}/evidence/{run_a.code}/shot.png"
    url_b = f"/artifacts/{scope_for(user_b.id)}/evidence/{run_b.code}/shot.png"

    assert client.get(url_a, params={"token": _token(user_a)}).status_code == 200
    assert client.get(url_a, params={"token": _token(user_b)}).status_code == 404
    assert client.get(url_b, params={"token": _token(user_b)}).status_code == 200
    assert client.get(url_b, params={"token": _token(user_a)}).status_code == 404


def test_forged_scope_prefix_on_valid_run_code_is_rejected(client, db_session, auth_on, two_users):
    """Defense in depth: a valid RUN-CODE behind the WRONG scope prefix 404s
    even for that run's real owner — the scope segment must match the run's
    resolved owner, not just be a valid scope string."""
    user_a, user_b = two_users
    run_b = _make_owned_run(db_session, user_b, code="RUN-803")

    dir_b = scoped_evidence_dir(user_b.id) / run_b.code
    dir_b.mkdir(parents=True, exist_ok=True)
    (dir_b / "shot.png").write_bytes(b"fake-png-b")

    # user_a's own (valid) scope prefix in front of user_b's run code + file.
    forged_url = f"/artifacts/{scope_for(user_a.id)}/evidence/{run_b.code}/shot.png"
    assert client.get(forged_url, params={"token": _token(user_a)}).status_code == 404
    assert client.get(forged_url, params={"token": _token(user_b)}).status_code == 404


def test_artifacts_reject_paths_outside_evidence_subtree(client):
    """The /artifacts mount now serves the workspace root, so a structural
    check must block anything that isn't under a `.../evidence/...` subtree —
    this runs even with auth disabled (the default in this test suite)."""
    assert client.get("/artifacts/q-agent.db").status_code == 404
    assert client.get("/artifacts/shared/specs/RUN-1/x.spec.ts").status_code == 404


def test_other_user_rejected_on_run_ws(client, db_session, auth_on, two_users):
    from starlette.websockets import WebSocketDisconnect

    user_a, user_b = two_users
    run = _make_owned_run(db_session, user_a)

    with client.websocket_connect(f"/ws/runs/{run.id}?token={_token(user_a)}"):
        pass  # the owner connects fine

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/runs/{run.id}?token={_token(user_b)}"):
            pass


def _make_owned_automation_project(db_session, owner: User, project_key: str = "PROJ-A"):
    """An automation repo owned by ``owner`` — the subject of the project WS channel."""
    from app.models.automation_project import AutomationProject

    project = AutomationProject(
        owner_id=owner.id,
        project_key=project_key,
        repo="",
        slug=f"{project_key.lower()}/",
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)
    return project


def test_owner_can_subscribe_to_project_execution_ws(client, db_session, auth_on, two_users):
    """The owner reaches ``/ws/projects/{id}`` AND receives what a run-less
    Execution publishes (#808).

    Asserting only "the socket opened" would still pass if the route subscribed
    to the wrong hub channel, which is the exact bug this fixes — so the channel
    key comes from ``execution_service.channel_key`` on a real run-less row, and
    the event has to arrive through it.
    """
    from app.models.execution import Execution
    from app.services import execution_service
    from app.ws import hub

    user_a, _ = two_users
    project = _make_owned_automation_project(db_session, user_a)
    execution = Execution(
        run_id=None, owner_id=user_a.id, automation_project_id=project.id, status="running"
    )
    db_session.add(execution)
    db_session.commit()

    # Published before connecting so the hub's catch-up replay delivers it
    # deterministically, with no dependence on cross-thread broadcast timing.
    hub.publish(execution_service.channel_key(execution), "exec.progress", {"progress": 42})

    with client.websocket_connect(f"/ws/projects/{project.id}?token={_token(user_a)}") as ws:
        message = ws.receive_json()

    assert message["event"] == "exec.progress"
    assert message["payload"]["progress"] == 42
    # The hub stamps the channel it fanned out on, so this pins the route to the
    # key the publisher uses rather than to any channel that happens to deliver.
    assert message["runId"] == f"project:{project.id}"


def test_other_user_rejected_on_project_execution_ws(client, db_session, auth_on, two_users):
    """A non-owner is refused the repo's channel, exactly as on the run channel."""
    from starlette.websockets import WebSocketDisconnect

    user_a, user_b = two_users
    project = _make_owned_automation_project(db_session, user_a, project_key="PROJ-B")

    with client.websocket_connect(f"/ws/projects/{project.id}?token={_token(user_a)}"):
        pass  # negative control: the owner really does get in

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/projects/{project.id}?token={_token(user_b)}"):
            pass


def test_unknown_project_rejected_on_project_execution_ws(client, auth_on, two_users):
    """An id that resolves to no repo is refused rather than silently subscribed."""
    from starlette.websockets import WebSocketDisconnect

    user_a, _ = two_users
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/projects/987654?token={_token(user_a)}"):
            pass


def _make_project_execution(db_session, owner, spec_path="tests/1377/1377-TC-01.spec.ts"):
    """A run-less, project-scoped Execution (#795) owned by ``owner``.

    ``run_id`` is NULL — the whole point of #796 — so ownership can only come
    from ``Execution.owner_id``.
    """
    from app.models.execution import Execution, ExecutionResult

    execution = Execution(run_id=None, owner_id=owner.id, status="done")
    db_session.add(execution)
    db_session.flush()
    db_session.add(
        ExecutionResult(
            execution_id=execution.id,
            test_case_id=0,  # a project-scoped result has no TestCase; see project_execution.create
            spec_path=spec_path,
            ticket_external_id="",
            case_code="",
            status="fail",
        )
    )
    db_session.commit()
    return execution


def _write_project_evidence(owner, execution, name="test-failed-1.png"):
    """Put a file exactly where ``evidence_service`` puts a project execution's
    screenshot: ``<scope>/evidence/projexec-<id>/<spec path parts>/<name>``."""
    evidence_dir = (
        scoped_evidence_dir(owner.id)
        / f"projexec-{execution.id}"
        / "tests"
        / "1377"
        / "1377-TC-01.spec.ts"
    )
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / name).write_bytes(b"fake-png")
    return (
        f"/artifacts/{scope_for(owner.id)}/evidence/projexec-{execution.id}"
        f"/tests/1377/1377-TC-01.spec.ts/{name}"
    )


def test_owner_can_read_a_project_execution_screenshot(
    client, db_session, auth_on, two_users
):
    """The regression this fixes: the guard resolved the segment after
    ``evidence/`` as a ``Run.code``, so ``projexec-<id>`` matched no row and
    EVERY project-scoped artifact 404'd — the report viewer's attachment
    rendered as a broken image."""
    user_a, _ = two_users
    execution = _make_project_execution(db_session, user_a)
    url = _write_project_evidence(user_a, execution)

    assert client.get(url, params={"token": _token(user_a)}).status_code == 200


def test_other_user_rejected_on_a_project_execution_screenshot(
    client, db_session, auth_on, two_users
):
    """Ownership still holds without a Run behind it — it comes from
    ``Execution.owner_id``. The owner's 200 in the same test is the control, so
    a guard that refused everything could not pass this."""
    user_a, user_b = two_users
    execution = _make_project_execution(db_session, user_a)
    url = _write_project_evidence(user_a, execution)

    assert client.get(url, params={"token": _token(user_a)}).status_code == 200
    assert client.get(url, params={"token": _token(user_b)}).status_code == 404


def test_forged_scope_prefix_on_a_project_execution_is_rejected(
    client, db_session, auth_on, two_users
):
    """Defense in depth, same rule as the RUN-CODE case: a valid
    ``projexec-<id>`` behind the wrong scope prefix 404s even for its real
    owner."""
    user_a, user_b = two_users
    execution = _make_project_execution(db_session, user_b)
    _write_project_evidence(user_b, execution)

    forged_url = (
        f"/artifacts/{scope_for(user_a.id)}/evidence/projexec-{execution.id}"
        f"/tests/1377/1377-TC-01.spec.ts/test-failed-1.png"
    )
    assert client.get(forged_url, params={"token": _token(user_a)}).status_code == 404
    assert client.get(forged_url, params={"token": _token(user_b)}).status_code == 404


def test_unknown_project_execution_id_is_rejected(client, db_session, auth_on, two_users):
    """A well-formed ``projexec-<id>`` that resolves to no Execution is refused,
    rather than falling through to the RUN-CODE branch and being treated as an
    unowned (pre-ownership, #91) run."""
    user_a, _ = two_users
    evidence_dir = scoped_evidence_dir(user_a.id) / "projexec-999999"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "shot.png").write_bytes(b"fake-png")

    url = f"/artifacts/{scope_for(user_a.id)}/evidence/projexec-999999/shot.png"
    assert client.get(url, params={"token": _token(user_a)}).status_code == 404
