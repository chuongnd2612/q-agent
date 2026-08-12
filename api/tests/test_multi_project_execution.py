"""An execution spanning two automation projects is refused, not half-shipped (#556).

An ``Execution`` covers **every** approved non-Manual case of a run (``execution.py``
adds one ``ExecutionResult`` per case, with no repo filter), a run's tickets each
carry their own ``repo``, and automation projects are keyed
``(owner_id, project_key, repo)``. So one execution can legitimately span two
projects — and neither run path can serve that:

* the **Local Agent claim** ships exactly one ``project`` bundle, so the other
  project's specs reach the device with unresolvable ``../pages/…`` imports;
* **server staging** merged both libraries into one run dir, where one project's
  ``pages/LoginPage.ts`` silently overwrote the other's.

Both are the silent mass-failure shape the version guard exists to prevent, so both
paths now refuse and report. Nothing in the product produces such a run yet, so the
two-repo fixture is constructed here.

**Mixed executions (some specs project-backed, some legacy) are deliberately
allowed** — a legacy spec is a self-contained flat file that needs no library and
cannot collide with the single project's tree. Only two or more *distinct* projects
are refused. Both halves of that decision are pinned below.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from app.models.execution import Execution, ExecutionResult
from app.services import agent_project_bundle

# Reuse the #541 guard suite's user/device/claim helpers rather than re-deriving them.
from test_agent_project_bundle import _make_user, _pair_device


# ----------------------------------------------------------------------- unit: reason
def test_multi_project_reason_is_none_for_zero_or_one_project():
    assert agent_project_bundle.multi_project_reason([]) is None
    one = SimpleNamespace(id=1, project_key="SUR", repo="web")
    assert agent_project_bundle.multi_project_reason([one]) is None
    # The same project seen once per spec is still one project.
    assert agent_project_bundle.multi_project_reason([one, one, one]) is None


def test_multi_project_reason_names_every_project_it_refuses():
    reason = agent_project_bundle.multi_project_reason(
        [
            SimpleNamespace(id=1, project_key="SUR", repo="web"),
            SimpleNamespace(id=2, project_key="SUR", repo="api"),
            SimpleNamespace(id=2, project_key="SUR", repo="api"),
        ]
    )
    assert reason is not None
    assert "spans 2 automation projects" in reason
    # Legible enough to act on: it names WHICH projects, so the user can split the run.
    assert "SUR (api)" in reason and "SUR (web)" in reason


def test_project_label_falls_back_when_repo_is_the_only_repo():
    """``repo == ""`` means "the project's only repo" — don't print empty parens."""
    assert agent_project_bundle.project_label(SimpleNamespace(id=7, project_key="SUR", repo="")) == "SUR"
    assert (
        agent_project_bundle.project_label(SimpleNamespace(id=7, project_key="", repo=""))
        == "#7"
    )


# ---------------------------------------------------------------------------- fixture
def _seed_two_project_run(db_session, owner_id=None, *, second_project: bool = True):
    """A run with two cases in two repos — two projects, or one project + a legacy spec.

    Returns ``(run, cases, projects)``. The projects are DB rows only: both guards
    run *before* any staging or bundling, so no on-disk tree (or git) is needed.
    """
    from app.models.automation_project import AutomationProject
    from app.models.run import Run, RunTicket
    from app.models.testcase import AutomationSpec, TestCase

    suffix = f"{owner_id or 0}-{'2' if second_project else '1'}"
    run = Run(
        code=f"RUN-MULTI-{suffix}", name="Two repos", status="automation", workers=1,
        owner_id=owner_id,
    )
    db_session.add(run)
    db_session.flush()

    projects: list[AutomationProject] = []
    cases = []
    for index, (ticket, repo) in enumerate((("SUR-1428", "web"), ("OPS-1433", "api"))):
        db_session.add(RunTicket(run_id=run.id, ticket_external_id=ticket, position=index))
        case = TestCase(
            run_id=run.id, ticket_external_id=ticket, code="TC-01", title=f"{ticket} works",
            approval="approved", automation="Playwright",
        )
        db_session.add(case)
        db_session.flush()
        cases.append(case)
        if index == 1 and not second_project:
            # The mixed execution: this one is a pre-#538 legacy spec.
            db_session.add(
                AutomationSpec(test_case_id=case.id, filename="1433-TC-01.spec.ts", code="// legacy")
            )
            continue
        project = AutomationProject(
            owner_id=owner_id, project_key="SUR", repo=repo, slug=f"sur/{repo}"
        )
        db_session.add(project)
        db_session.flush()
        projects.append(project)
        db_session.add(
            AutomationSpec(
                test_case_id=case.id,
                project_id=project.id,
                filename=f"tests/{ticket}/{ticket}-TC-01.spec.ts",
                code="// layered",
            )
        )
    db_session.commit()
    db_session.refresh(run)
    return run, cases, projects


def _await_execution(client, execution_id: int) -> dict:
    for _ in range(80):
        time.sleep(0.05)
        body = client.get(f"/executions/{execution_id}").json()
        if body["status"] == "done":
            return body
    return client.get(f"/executions/{execution_id}").json()


# ------------------------------------------------------------------ the guard's input
def test_execution_projects_lists_each_distinct_project_once(client, db_session):
    from app.services import playwright_runner

    _run, cases, projects = _seed_two_project_run(db_session)
    listed = playwright_runner.execution_projects(
        db_session, [(c.id, c.ticket_external_id, c.code) for c in cases]
    )
    assert {p.id for p in listed} == {p.id for p in projects}
    assert agent_project_bundle.multi_project_reason(listed) is not None

    _run2, cases2, projects2 = _seed_two_project_run(db_session, second_project=False)
    listed2 = playwright_runner.execution_projects(
        db_session, [(c.id, c.ticket_external_id, c.code) for c in cases2]
    )
    assert [p.id for p in listed2] == [projects2[0].id], "a legacy spec adds no project"
    assert agent_project_bundle.multi_project_reason(listed2) is None


# ------------------------------------------------------------- server execution path
def test_run_execution_refuses_an_execution_spanning_two_projects(
    client, db_session, monkeypatch
):
    """Every case fails with ONE legible reason; nothing is staged and nothing runs."""
    import app.services.playwright_runner as runner_module

    run, _cases, projects = _seed_two_project_run(db_session)
    assert len({p.id for p in projects}) == 2

    invocations: list = []
    monkeypatch.setattr(
        runner_module, "_invoke_playwright",
        lambda *a, **k: (invocations.append(a), (0, "", ""))[1],
    )

    def never_stage(*_args, **_kwargs):
        raise AssertionError("a multi-project execution must be refused before staging")

    monkeypatch.setattr(runner_module, "_stage_specs_for_run", never_stage)

    resp = client.post(f"/runs/{run.id}/execution", json={"target": "server"})
    assert resp.status_code == 200, resp.text
    body = _await_execution(client, resp.json()["id"])

    assert body["status"] == "done"
    assert (body["passed"], body["failed"]) == (0, 2)
    for result in body["results"]:
        assert result["status"] == "fail"
        assert "spans 2 automation projects" in result["errorMessage"]
        assert "SUR (api)" in result["errorMessage"] and "SUR (web)" in result["errorMessage"]
    assert invocations == [], "no subprocess for a refused execution"


def test_run_execution_allows_a_mixed_project_backed_and_legacy_execution(
    client, db_session, monkeypatch
):
    """One project + one legacy spec is NOT refused — the documented mixed behaviour."""
    import app.services.playwright_runner as runner_module

    run, _cases, projects = _seed_two_project_run(db_session, second_project=False)
    assert len(projects) == 1

    invoked: list[str] = []

    def fake_invoke(spec_dir_arg, workers, timeout_s, spec_file="", **_kwargs):
        invoked.append(spec_file)
        (spec_dir_arg / "report.json").write_text('{"suites": []}', encoding="utf-8")
        return 0, "ok", ""

    monkeypatch.setattr(runner_module, "_invoke_playwright", fake_invoke)
    # Staging needs no real tree for this assertion — only that the run proceeded.
    monkeypatch.setattr(
        runner_module, "_stage_specs_for_run",
        lambda db, run_arg, cases: _ensure_dir(
            runner_module.scoped_specs_dir(run_arg.owner_id) / run_arg.code
        ),
    )

    resp = client.post(f"/runs/{run.id}/execution", json={"target": "server"})
    body = _await_execution(client, resp.json()["id"])
    assert body["status"] == "done"
    assert invoked, "a mixed execution must still run"
    for result in body["results"]:
        assert "automation projects" not in (result["errorMessage"] or "")


def _ensure_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------- local-agent claim
def _queue_local_agent_execution(db_session, run, cases) -> Execution:
    execution = Execution(
        run_id=run.id, status="queued", target="local-agent", env=run.env, browser=run.browser,
        workers=1, total=len(cases),
    )
    db_session.add(execution)
    db_session.flush()
    for case in cases:
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


def test_claim_refuses_an_execution_spanning_two_projects(client, db_session):
    """The claim ships ONE bundle, so two projects is refused with 409 + a reason."""
    user = _make_user(db_session, "multiproject@example.com")
    _device, token = _pair_device(db_session, user)
    run, cases, _projects = _seed_two_project_run(db_session, owner_id=user.id)
    execution = _queue_local_agent_execution(db_session, run, cases)

    resp = client.post(
        "/agent/jobs/next",
        json={"agentVersion": agent_project_bundle.MIN_AGENT_VERSION},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409, resp.text
    assert "spans 2 automation projects" in resp.json()["detail"]

    # The claimed execution ends cleanly instead of being left "running" forever or
    # handed to a device that would fail half the specs on missing imports.
    db_session.expire_all()
    refreshed = db_session.get(Execution, execution.id)
    assert refreshed.status == "done"
    assert (refreshed.passed, refreshed.failed) == (0, 2)
    assert "automation projects" in (refreshed.log or "")
    results = (
        db_session.query(ExecutionResult)
        .filter(ExecutionResult.execution_id == execution.id)
        .all()
    )
    assert [r.status for r in results] == ["fail", "fail"]
    assert all("SUR (web)" in (r.error_message or "") for r in results)

    # Nothing left claimable, so the refusal cannot loop forever.
    assert (
        client.post("/agent/jobs/next", headers={"Authorization": f"Bearer {token}"}).status_code
        == 204
    )


def test_claim_allows_a_mixed_project_backed_and_legacy_execution(client, db_session):
    """One project + one legacy spec claims fine: one bundle, one nested + one flat spec."""
    user = _make_user(db_session, "mixedclaim@example.com")
    _device, token = _pair_device(db_session, user)
    run, cases, projects = _seed_two_project_run(
        db_session, owner_id=user.id, second_project=False
    )
    _queue_local_agent_execution(db_session, run, cases)

    resp = client.post(
        "/agent/jobs/next",
        json={"agentVersion": agent_project_bundle.MIN_AGENT_VERSION},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "project" in body, "the one project still ships"
    from app.services import spec_service

    # The project-backed spec is project-relative (nested); the legacy one keeps the
    # flat convention name the claim computes for it. Both ride in one payload.
    filenames = sorted(spec["filename"] for spec in body["specs"])
    assert filenames == sorted(
        [
            spec_service.spec_filename("OPS-1433", "TC-01"),
            "tests/SUR-1428/SUR-1428-TC-01.spec.ts",
        ]
    )
    assert len(projects) == 1


def test_single_project_claim_is_unaffected(client, db_session):
    """Two cases in the SAME project is one project and must still ship."""
    from app.models.automation_project import AutomationProject
    from app.models.testcase import AutomationSpec

    user = _make_user(db_session, "oneproject@example.com")
    _device, token = _pair_device(db_session, user)
    run, cases, projects = _seed_two_project_run(db_session, owner_id=user.id)
    # Point both specs at the first project — the "same project, two tickets" run.
    keeper = projects[0]
    for case in cases:
        spec = (
            db_session.query(AutomationSpec)
            .filter(AutomationSpec.test_case_id == case.id)
            .first()
        )
        spec.project_id = keeper.id
    db_session.query(AutomationProject).filter(AutomationProject.id == projects[1].id).delete()
    db_session.commit()
    _queue_local_agent_execution(db_session, run, cases)

    resp = client.post(
        "/agent/jobs/next",
        json={"agentVersion": agent_project_bundle.MIN_AGENT_VERSION},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["specs"]) == 2
