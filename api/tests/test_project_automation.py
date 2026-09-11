"""Project-keyed automation read endpoints (#768) and spec provenance (#769).

Covers ``routers/automation_projects.py``: the repo selector's aggregates, the
code-free file tree, lazy per-file content, path validation, the ZIP export and
the run/ticket/case provenance on a spec — all reached by **project GUID** rather
than by run.

Four things this file is deliberately careful about:

* **It asserts which resolution leg ran**, not just the status code.
  ``projects_for_guid`` is a two-leg union, and a test that seeds both legs would
  pass even if one were deleted. Every resolution test therefore seeds exactly
  one leg: a stamped ``project_guid`` with no specs at all, or a row whose
  ``project_guid`` is NULL that is only findable through
  ``automation_specs -> test_cases -> runs.project_guid``.
* **It asserts the tree carries no ``code`` key.** That negative is the entire
  point of the slice — a tree that quietly regrew ``code`` would still pass every
  positive assertion.
* **The "provenance is null" cases are guarded against a dead join.** A join that
  matched nothing would return ``None`` for *everything*, so every null case
  changes exactly **one** thing from the resolving case: the mirror row's ``kind``,
  the spec's ``project_id``, its ``filename``, or which repo it points at — and
  the shared-asset test asserts the resolving request in the same body.
* **Ownership is exercised with a real user.** The suite runs with
  ``auth_required=False``, so ``current_user`` resolves to ``None`` and the #91
  ownership bridge lets everything through; the ownership tests override the
  dependency so the check actually runs.
"""

from __future__ import annotations

import hashlib
import io
import json as _json
import shutil
import zipfile
from datetime import datetime, timezone

import pytest

from app.models.automation_project import AutomationFile, AutomationProject
from app.services import automation_project_service as aps

pytestmark = pytest.mark.usefixtures("workspace_dir")

GUID = "d52ca970-0000-4000-8000-000000000001"
OTHER_GUID = "9058863f-0000-4000-8000-000000000002"
#: ``None`` on purpose. The suite runs with ``auth_required=False``, so
#: ``current_user`` resolves to ``None`` and the endpoints resolve the
#: shared/un-owned namespace — which is exactly what ``owner_id IS NULL`` rows
#: are (``workspace_scope.scope_for``). Tests that need a *real* owner (the
#: ownership refusals) override the dependency via :func:`_as_me` instead.
OWNER_ID: int | None = None
PROJECT_KEY = "surency"

PAGE_CODE = "export class LoginPage {}\n"
# A multi-byte character on purpose: `size` is a UTF-8 **byte** count, and
# SQL `length()` would have counted characters instead.
SPEC_CODE = "test('café', async () => {});\n"


requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _repo(
    db_session,
    *,
    owner_id: int | None = OWNER_ID,
    project_guid: str | None = GUID,
    project_key: str = PROJECT_KEY,
    repo: str = "web",
    base_version: str = "1.0.0",
) -> AutomationProject:
    """An ``AutomationProject`` row, created directly — no disk, no git.

    Every endpoint except the ZIP export reads the mirror only, so seeding the
    row is both faster and a stronger test: a handler that reached for disk would
    fail here rather than silently agree with a materialized tree.
    """
    project = AutomationProject(
        owner_id=owner_id,
        project_guid=project_guid,
        project_key=project_key,
        repo=repo,
        slug=f"{project_key}/{repo or 'default'}",
        base_version=base_version,
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)
    return project


def _file(db_session, project, path: str, kind: str = "page", code: str = PAGE_CODE):
    row = AutomationFile(
        project_id=project.id,
        path=path,
        kind=kind,
        code=code,
        sha256=hashlib.sha256(code.encode("utf-8")).hexdigest(),
    )
    db_session.add(row)
    db_session.commit()
    return row


def _spec_written_by_a_run(
    db_session,
    project,
    *,
    project_guid: str,
    owner_id: int | None = OWNER_ID,
    run_code: str = "RUN-901",
):
    """A run of ``project_guid`` that wrote a spec into ``project``.

    This is leg (b) of ``projects_for_guid`` and the *only* way a pre-#766 row —
    or a repo shared by two projects, which cannot have one scalar GUID — is
    discoverable.
    """
    from app.models.run import Run
    from app.models.testcase import AutomationSpec, TestCase

    run = Run(
        code=run_code,
        name="Automation run",
        status="complete",
        owner_id=owner_id,
        project_guid=project_guid,
    )
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
    db_session.add(
        AutomationSpec(
            test_case_id=case.id,
            filename="SUR-1428-TC-01.spec.ts",
            code=SPEC_CODE,
            status="passed",
            project_id=project.id,
        )
    )
    db_session.commit()
    return run


def _spec_row(
    db_session,
    project,
    *,
    filename: str,
    project_guid: str | None = GUID,
    project_id: int | None = -1,
    owner_id: int | None = OWNER_ID,
    run_code: str = "RUN-0031",
    run_name: str = "Sprint 42 regression",
    run_status: str = "done",
    created_at=None,
    finished_at=None,
    ticket: str = "SUR-1428",
    case_code: str = "TC-01",
    case_title: str = "Login with valid credentials",
    spec_status: str = "passed",
    block_reason: str = "",
):
    """A ``run -> case -> spec`` chain claiming ``filename`` — the provenance join.

    ``filename`` is the parameter that matters: for a project-backed spec it holds
    the **project-relative POSIX** path (``tests/SUR-1428/SUR-1428-TC-01.spec.ts``),
    which is what ``AutomationFile.path`` looks like too. Passing a bare basename
    plus ``project_id=None`` reproduces a legacy, pre-#538 row.

    ``created_at`` is set explicitly because ordering is on ``Run.created_at`` and
    the default clock has second-level granularity — two rows inserted in the same
    second would otherwise make "which one is ``latest``" a coin flip.

    Args:
        project_id: The ``AutomationProject`` id to stamp on the spec. The
            sentinel ``-1`` means "this project"; pass ``None`` for a legacy row.
    """
    from app.models.run import Run
    from app.models.testcase import AutomationSpec, TestCase

    run = Run(
        code=run_code,
        name=run_name,
        status=run_status,
        owner_id=owner_id,
        project_guid=project_guid,
        finished_at=finished_at,
        **({"created_at": created_at} if created_at is not None else {}),
    )
    db_session.add(run)
    db_session.flush()
    case = TestCase(
        run_id=run.id,
        ticket_external_id=ticket,
        code=case_code,
        title=case_title,
        approval="approved",
        automation="Playwright",
    )
    db_session.add(case)
    db_session.flush()
    spec = AutomationSpec(
        test_case_id=case.id,
        filename=filename,
        code=SPEC_CODE,
        status=spec_status,
        block_reason=block_reason,
        # The absolute on-disk path, deliberately *wrong* for the join: a handler
        # that joined on `.path` instead of `.filename` would find nothing here.
        path=f"/somewhere/else/{filename}",
        project_id=project.id if project_id == -1 else project_id,
    )
    db_session.add(spec)
    db_session.commit()
    return run, case, spec


def _as_me(app, db_session, email: str = "me@test"):
    """Override ``current_user`` with a real user so ownership is enforced."""
    from app.deps_auth import current_user
    from app.models.user import User

    me = User(email=email, first_name="Me", password_hash="x")
    db_session.add(me)
    db_session.commit()
    app.dependency_overrides[current_user] = lambda: me
    return me


# ---------------------------------------------------------------------------
# /repos — aggregates, no disk, no git
# ---------------------------------------------------------------------------


def test_repos_aggregates_a_mixed_repo_and_omits_head_commit(client, db_session):
    project = _repo(db_session)
    _file(db_session, project, "pages/LoginPage.ts", kind="page")
    _file(db_session, project, "tests/SUR-1428/a.spec.ts", kind="spec", code=SPEC_CODE)
    _file(db_session, project, "tests/SUR-1428/b.spec.ts", kind="spec", code=SPEC_CODE)

    response = client.get(f"/projects/{GUID}/automation/repos")

    assert response.status_code == 200
    body = response.json()
    assert [entry["id"] for entry in body] == [project.id]
    entry = body[0]
    assert entry["fileCount"] == 3
    assert entry["specCount"] == 2
    assert entry["repo"] == "web"
    assert entry["repoLabel"] == "web"
    assert entry["baseVersion"] == "1.0.0"
    assert entry["updatedAt"]
    # Explicitly NOT here: resolving it costs a `git rev-parse` spawn per repo,
    # and only the selected repo's commit is ever displayed.
    assert "headCommit" not in entry


def test_repos_reports_zero_for_a_scaffolded_repo_with_no_files(client, db_session):
    """A repo with no mirror rows is still real — it is the empty state's case.

    ``MAX(updated_at)`` over no rows is NULL, so ``updatedAt`` falls back to the
    repo row's own timestamp rather than coming back null on a non-nullable field.
    """
    project = _repo(db_session)

    body = client.get(f"/projects/{GUID}/automation/repos").json()

    assert len(body) == 1
    assert body[0]["fileCount"] == 0
    assert body[0]["specCount"] == 0
    assert body[0]["updatedAt"].startswith(project.updated_at.isoformat()[:19])


def test_repos_counts_a_spec_only_repo_as_all_specs(client, db_session):
    project = _repo(db_session)
    _file(db_session, project, "tests/SUR-1/a.spec.ts", kind="spec", code=SPEC_CODE)
    _file(db_session, project, "tests/SUR-1/b.spec.ts", kind="spec", code=SPEC_CODE)

    entry = client.get(f"/projects/{GUID}/automation/repos").json()[0]

    assert (entry["fileCount"], entry["specCount"]) == (2, 2)
    assert entry["id"] == project.id


def test_repos_labels_the_default_repo_when_repo_is_blank(client, db_session):
    _repo(db_session, repo="")

    entry = client.get(f"/projects/{GUID}/automation/repos").json()[0]

    assert entry["repo"] == ""
    assert entry["repoLabel"] == "default"


def test_repos_orders_by_repo_so_the_default_repo_is_first(client, db_session):
    _repo(db_session, repo="web")
    _repo(db_session, repo="")
    _repo(db_session, repo="admin")

    labels = [e["repoLabel"] for e in client.get(f"/projects/{GUID}/automation/repos").json()]

    assert labels == ["default", "admin", "web"]


def test_repos_is_empty_not_404_for_a_project_with_no_automation(client, db_session):
    """ "No automation yet" is a state, not an error — the tab renders an empty view."""
    _repo(db_session, project_guid=OTHER_GUID)

    response = client.get(f"/projects/{GUID}/automation/repos")

    assert response.status_code == 200
    assert response.json() == []


def test_repos_resolves_a_repo_only_reachable_through_the_spec_join(client, db_session):
    """Leg (b) alone: the row's ``project_guid`` is NULL, exactly like pre-#766 data.

    This is the reported bug — a project with a completed run showing "no
    automation repo". Nothing but the ``specs -> cases -> runs`` join can find it,
    so if that leg were dropped this test would return `[]`.
    """
    project = _repo(db_session, project_guid=None)
    _file(db_session, project, "pages/LoginPage.ts")
    _spec_written_by_a_run(db_session, project, project_guid=GUID)

    body = client.get(f"/projects/{GUID}/automation/repos").json()

    assert [entry["id"] for entry in body] == [project.id]
    assert project.project_guid is None, "the stamped-column leg must not be what resolved this"
    assert body[0]["fileCount"] == 1


def test_a_repo_shared_by_two_projects_is_reachable_from_both_guids(client, db_session):
    """One repo, two owning projects — both must see it (#768's correction).

    The row is keyed on ``(owner_id, provider project_key, repo)``, so two q-agent
    projects targeting the same provider project legitimately write into it. A
    scalar ``project_guid`` cannot express that, and "the latest wins" would hide
    the repo from one of the two projects entirely.
    """
    project = _repo(db_session, project_guid=None)
    _file(db_session, project, "pages/LoginPage.ts")
    _spec_written_by_a_run(db_session, project, project_guid=GUID, run_code="RUN-A")
    _spec_written_by_a_run(db_session, project, project_guid=OTHER_GUID, run_code="RUN-B")

    for guid in (GUID, OTHER_GUID):
        listed = client.get(f"/projects/{guid}/automation/repos").json()
        assert [entry["id"] for entry in listed] == [project.id], guid
        # Reachable, not merely listed: the tree resolves from both guids too.
        tree = client.get(f"/projects/{guid}/automation/repos/{project.id}/files")
        assert tree.status_code == 200, guid
        assert tree.json()["projectId"] == project.id


def test_repos_never_returns_another_users_repo(app, client, db_session):
    me = _as_me(app, db_session)
    mine = _repo(db_session, owner_id=me.id)
    theirs = _repo(db_session, owner_id=me.id + 1000, repo="admin")

    ids = [entry["id"] for entry in client.get(f"/projects/{GUID}/automation/repos").json()]

    assert ids == [mine.id]
    assert theirs.id not in ids


# ---------------------------------------------------------------------------
# /repos/{id}/files — the tree, and the missing `code` key
# ---------------------------------------------------------------------------


def test_tree_carries_no_code_key(client, db_session):
    """The whole slice in one assertion: metadata only, never the source.

    A 200-file project answers in ~25KB instead of megabytes precisely because
    this key is absent; the content is fetched one file at a time.
    """
    project = _repo(db_session)
    _file(db_session, project, "tests/SUR-1/a.spec.ts", kind="spec", code=SPEC_CODE)
    _file(db_session, project, "pages/LoginPage.ts", kind="page")

    body = client.get(f"/projects/{GUID}/automation/repos/{project.id}/files").json()

    assert body["fileCount"] == 2
    for entry in body["files"]:
        assert "code" not in entry, entry
    # And the payload as a whole is free of it, not merely each row.
    assert "code" not in body
    assert (
        PAGE_CODE.strip()
        not in client.get(f"/projects/{GUID}/automation/repos/{project.id}/files").text
    )


def test_tree_reports_mirror_byte_size_and_orders_by_path(client, db_session):
    project = _repo(db_session)
    _file(db_session, project, "tests/SUR-1/a.spec.ts", kind="spec", code=SPEC_CODE)
    _file(db_session, project, "pages/LoginPage.ts", kind="page")

    body = client.get(f"/projects/{GUID}/automation/repos/{project.id}/files").json()

    assert [entry["path"] for entry in body["files"]] == [
        "pages/LoginPage.ts",
        "tests/SUR-1/a.spec.ts",
    ]
    by_path = {entry["path"]: entry for entry in body["files"]}
    assert by_path["pages/LoginPage.ts"]["kind"] == "page"
    assert by_path["tests/SUR-1/a.spec.ts"]["kind"] == "spec"
    # UTF-8 bytes, not characters: "café" is 5 characters' worth of bytes.
    assert by_path["tests/SUR-1/a.spec.ts"]["size"] == len(SPEC_CODE.encode("utf-8"))
    assert by_path["tests/SUR-1/a.spec.ts"]["size"] > len(SPEC_CODE)


def test_tree_returns_head_commit_and_repo_metadata(client, db_session):
    project = _repo(db_session)

    body = client.get(f"/projects/{GUID}/automation/repos/{project.id}/files").json()

    # Present here — one git spawn for the one selected repo — even though the
    # seeded row has no tree on disk, in which case it is "".
    assert "headCommit" in body
    assert body["headCommit"] == ""
    assert body["projectId"] == project.id
    assert body["repo"] == "web"
    assert body["baseVersion"] == "1.0.0"
    assert body["files"] == []


def test_tree_404s_for_a_repo_not_reachable_from_this_guid(client, db_session):
    """A ``project_id`` from another project 404s — it never hops."""
    theirs = _repo(db_session, project_guid=OTHER_GUID)
    _file(db_session, theirs, "pages/Secret.ts", code="export class Secret {}\n")

    response = client.get(f"/projects/{GUID}/automation/repos/{theirs.id}/files")

    assert response.status_code == 404
    assert "Secret" not in response.text


def test_tree_404s_for_a_repo_that_does_not_exist(client, db_session):
    _repo(db_session)

    assert client.get(f"/projects/{GUID}/automation/repos/999999/files").status_code == 404


def test_tree_404s_not_403_for_another_users_repo(app, client, db_session):
    """Ownership is a 404, per ADR 0008/0009 — a 403 would confirm it exists."""
    me = _as_me(app, db_session)
    # Stamped with *my* project's guid but owned by someone else: only the
    # owner_id check can refuse this one.
    theirs = _repo(db_session, owner_id=me.id + 1000)

    response = client.get(f"/projects/{GUID}/automation/repos/{theirs.id}/files")

    assert response.status_code == 404
    assert response.json()["detail"] == "AutomationProject not found"


# ---------------------------------------------------------------------------
# /repos/{id}/file — lazy content, served from the mirror
# ---------------------------------------------------------------------------


def test_file_serves_the_mirror_content_and_omits_provenance_with_no_spec_row(client, db_session):
    project = _repo(db_session)
    row = _file(db_session, project, "tests/SUR-1/a.spec.ts", kind="spec", code=SPEC_CODE)

    response = client.get(
        f"/projects/{GUID}/automation/repos/{project.id}/file",
        params={"path": "tests/SUR-1/a.spec.ts"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == SPEC_CODE
    assert body["path"] == "tests/SUR-1/a.spec.ts"
    assert body["kind"] == "spec"
    assert body["size"] == len(SPEC_CODE.encode("utf-8"))
    assert body["sha256"] == row.sha256
    # A spec *file* with no `AutomationSpec` row claiming it — nothing to attribute
    # it to, so the field stays null rather than inventing a run (#769).
    assert body["provenance"] is None


def test_file_reads_the_mirror_not_disk(client, db_session):
    """Nothing was ever materialized on disk, yet the content resolves.

    That is the property the tree and viewer agreeing depends on — and it is what
    makes traversal structurally impossible, since the only path that resolves is
    one matching a mirror row exactly.
    """
    project = _repo(db_session)
    _file(db_session, project, "pages/LoginPage.ts")

    assert not aps.project_dir(project).exists()
    body = client.get(
        f"/projects/{GUID}/automation/repos/{project.id}/file",
        params={"path": "pages/LoginPage.ts"},
    ).json()

    assert body["code"] == PAGE_CODE


@pytest.mark.parametrize(
    "path",
    [
        "../../etc/passwd",
        "pages/../../../etc/passwd",
        "/etc/passwd",
        "C:/Windows/win.ini",
        "pages\\LoginPage.ts",
        "..",
        "",
    ],
)
def test_file_400s_on_a_malformed_path_before_the_query(client, db_session, path):
    """400, not a silent 404: "that is not a path this API accepts" is the truth.

    And never a body — the response carries only the refusal.
    """
    project = _repo(db_session)
    _file(db_session, project, "pages/LoginPage.ts")

    response = client.get(
        f"/projects/{GUID}/automation/repos/{project.id}/file", params={"path": path}
    )

    # An empty `path` is refused by FastAPI's own required-query validation (422)
    # before the handler runs; every other shape is our 400.
    assert response.status_code in (400, 422), path
    assert "LoginPage" not in response.text
    assert "passwd" not in response.text
    assert "Traceback" not in response.text


def test_file_404s_for_a_well_formed_path_with_no_mirror_row(client, db_session):
    project = _repo(db_session)
    _file(db_session, project, "pages/LoginPage.ts")

    response = client.get(
        f"/projects/{GUID}/automation/repos/{project.id}/file",
        params={"path": "pages/NoSuchPage.ts"},
    )

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_file_404s_for_a_repo_not_reachable_from_this_guid(client, db_session):
    theirs = _repo(db_session, project_guid=OTHER_GUID)
    _file(db_session, theirs, "pages/Secret.ts", code="export class Secret {}\n")

    response = client.get(
        f"/projects/{GUID}/automation/repos/{theirs.id}/file",
        params={"path": "pages/Secret.ts"},
    )

    assert response.status_code == 404
    assert "Secret" not in response.text


def test_file_404s_not_403_for_another_users_repo(app, client, db_session):
    me = _as_me(app, db_session)
    theirs = _repo(db_session, owner_id=me.id + 1000)
    _file(db_session, theirs, "pages/Secret.ts", code="export class Secret {}\n")

    response = client.get(
        f"/projects/{GUID}/automation/repos/{theirs.id}/file",
        params={"path": "pages/Secret.ts"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "AutomationProject not found"
    assert "Secret" not in response.text


# ---------------------------------------------------------------------------
# /repos/{id}/file — provenance (#769)
# ---------------------------------------------------------------------------

SPEC_PATH = "tests/SUR-1428/SUR-1428-TC-01.spec.ts"


def _file_body(client, project, path: str = SPEC_PATH, guid: str = GUID):
    response = client.get(
        f"/projects/{guid}/automation/repos/{project.id}/file", params={"path": path}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_provenance_names_the_run_ticket_and_case_that_produced_the_spec(client, db_session):
    """The whole point of the slice: one spec, one run, fully attributed.

    Every field is asserted individually rather than by comparing the object — an
    additive change to the entry shape must not fail a test about attribution.
    """
    project = _repo(db_session)
    _file(db_session, project, SPEC_PATH, kind="spec", code=SPEC_CODE)
    run, case, spec = _spec_row(
        db_session,
        project,
        filename=SPEC_PATH,
        created_at=datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 3, 1, 9, 30, tzinfo=timezone.utc),
    )

    provenance = _file_body(client, project)["provenance"]

    assert provenance is not None, "a project-backed spec must carry provenance"
    assert provenance["kind"] == "spec"
    assert provenance["overwritten"] is False
    assert provenance["history"] == []
    latest = provenance["latest"]
    assert latest["specId"] == spec.id
    assert latest["specStatus"] == "passed"
    assert latest["blockReason"] is None
    assert latest["testCaseId"] == case.id
    assert latest["caseCode"] == "TC-01"
    assert latest["caseTitle"] == "Login with valid credentials"
    assert latest["ticketExternalId"] == "SUR-1428"
    assert latest["runId"] == run.id
    assert latest["runCode"] == "RUN-0031"
    assert latest["runName"] == "Sprint 42 regression"
    assert latest["runStatus"] == "done"
    assert latest["runCreatedAt"].startswith("2026-03-01T09:00")
    assert latest["runFinishedAt"].startswith("2026-03-01T09:30")
    assert latest["stale"] is False


def test_provenance_joins_on_filename_not_the_absolute_on_disk_path(client, db_session):
    """The join key, pinned. ``AutomationSpec.path`` is absolute and goes stale.

    The fixture writes a deliberately unrelated ``/somewhere/else/…`` into
    ``.path``, so a handler joining on that column resolves nothing and this test
    is the one that fails.
    """
    project = _repo(db_session)
    _file(db_session, project, SPEC_PATH, kind="spec", code=SPEC_CODE)
    _run, _case, spec = _spec_row(db_session, project, filename=SPEC_PATH)

    provenance = _file_body(client, project)["provenance"]

    assert spec.filename == SPEC_PATH
    assert spec.path != SPEC_PATH, "the fixture must not make .path a viable join key"
    assert provenance["latest"]["specId"] == spec.id


def test_provenance_marks_the_overwritten_run_as_history(client, db_session):
    """Two runs on one ticket wrote the same file — ADR 0014's overwrite rule.

    ``latest`` is the newer run (it produced the bytes on screen); the older row
    keeps its own stale copy of the code and is reported as ``history``, with the
    **full** entry shape so the client renders it with the same component.
    """
    project = _repo(db_session)
    _file(db_session, project, SPEC_PATH, kind="spec", code=SPEC_CODE)
    _old_run, old_case, old_spec = _spec_row(
        db_session,
        project,
        filename=SPEC_PATH,
        run_code="RUN-0017",
        run_name="Sprint 41 regression",
        spec_status="failed",
        block_reason="placeholder selector",
        created_at=datetime(2026, 2, 1, 9, 0, tzinfo=timezone.utc),
    )
    new_run, _new_case, new_spec = _spec_row(
        db_session,
        project,
        filename=SPEC_PATH,
        run_code="RUN-0031",
        created_at=datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc),
    )

    provenance = _file_body(client, project)["provenance"]

    assert provenance["overwritten"] is True
    assert provenance["latest"]["runId"] == new_run.id
    assert provenance["latest"]["specId"] == new_spec.id
    assert provenance["latest"]["runCode"] == "RUN-0031"
    assert provenance["latest"]["stale"] is False

    assert len(provenance["history"]) == 1
    stale = provenance["history"][0]
    assert stale["stale"] is True
    assert stale["specId"] == old_spec.id
    assert stale["runCode"] == "RUN-0017"
    assert stale["specStatus"] == "failed"
    assert stale["blockReason"] == "placeholder selector"
    # The full entry shape, identical to `latest` — #767's types render both with
    # one component, so a narrower history object would break the client.
    assert set(stale) == set(provenance["latest"])
    assert stale["testCaseId"] == old_case.id
    assert stale["caseTitle"] == "Login with valid credentials"
    assert stale["runName"] == "Sprint 41 regression"
    assert stale["runCreatedAt"].startswith("2026-02-01T09:00")


def test_provenance_orders_three_claimants_newest_first(client, db_session):
    project = _repo(db_session)
    _file(db_session, project, SPEC_PATH, kind="spec", code=SPEC_CODE)
    for day, code in ((1, "RUN-0001"), (3, "RUN-0003"), (2, "RUN-0002")):
        _spec_row(
            db_session,
            project,
            filename=SPEC_PATH,
            run_code=code,
            created_at=datetime(2026, 4, day, 9, 0, tzinfo=timezone.utc),
        )

    provenance = _file_body(client, project)["provenance"]

    assert provenance["latest"]["runCode"] == "RUN-0003"
    assert [entry["runCode"] for entry in provenance["history"]] == ["RUN-0002", "RUN-0001"]
    assert all(entry["stale"] is True for entry in provenance["history"])


def test_provenance_is_null_for_a_shared_asset(client, db_session):
    """A page is edited across many runs, so "the run that made it" is not a fact.

    Seeded next to a spec whose run *would* match if the handler fell back to
    ``updated_at`` proximity — inferring one there is the #178 failure mode.
    """
    project = _repo(db_session)
    _file(db_session, project, "pages/LoginPage.ts", kind="page")
    _file(db_session, project, SPEC_PATH, kind="spec", code=SPEC_CODE)
    _spec_row(db_session, project, filename=SPEC_PATH)

    page = _file_body(client, project, path="pages/LoginPage.ts")
    spec = _file_body(client, project)

    assert page["kind"] == "page"
    assert page["provenance"] is None
    # Negative control: the same request against the spec in the same repo *does*
    # carry provenance, so the null above is the non-spec rule and not a dead join.
    assert spec["provenance"] is not None


@pytest.mark.parametrize("kind", ["component", "fixture", "util"])
def test_provenance_is_null_for_every_non_spec_kind(client, db_session, kind):
    project = _repo(db_session)
    _file(db_session, project, SPEC_PATH, kind=kind, code=SPEC_CODE)
    _spec_row(db_session, project, filename=SPEC_PATH)

    body = _file_body(client, project)

    # Same path, same spec row, *only* the mirror row's `kind` differs — the
    # predicate really is `kind == "spec"` and nothing else.
    assert body["kind"] == kind
    assert body["provenance"] is None


def test_a_legacy_bare_basename_spec_never_leaks_into_this_projects_provenance(client, db_session):
    """Pre-#538 rows carry ``project_id IS NULL`` and a bare basename filename.

    Both predicates have to hold: the basename cannot match this project's
    ``tests/<TICKET>/…`` path, and the ``project_id`` filter refuses the row even
    if some other project's file were named identically.
    """
    project = _repo(db_session)
    _file(db_session, project, SPEC_PATH, kind="spec", code=SPEC_CODE)
    _spec_row(
        db_session,
        project,
        filename="SUR-1428-TC-01.spec.ts",
        project_id=None,
        run_code="RUN-LEGACY",
        run_name="A run from before #538",
    )

    body = _file_body(client, project)

    assert body["provenance"] is None, "a legacy per-run spec must not be attributed here"
    assert (
        "RUN-LEGACY"
        not in client.get(
            f"/projects/{GUID}/automation/repos/{project.id}/file", params={"path": SPEC_PATH}
        ).text
    )


def test_a_legacy_spec_whose_basename_equals_the_path_still_needs_project_id(client, db_session):
    """The ``project_id`` predicate alone, isolated.

    A legacy row is given the *full* project-relative filename, so the
    ``filename`` match succeeds and only ``project_id == project.id`` can refuse
    it. Without that predicate a legacy row from an unrelated project would be
    rendered as this file's origin.
    """
    project = _repo(db_session)
    _file(db_session, project, SPEC_PATH, kind="spec", code=SPEC_CODE)
    _spec_row(
        db_session,
        project,
        filename=SPEC_PATH,
        project_id=None,
        project_guid=OTHER_GUID,
        run_code="RUN-OTHER",
    )

    assert _file_body(client, project)["provenance"] is None


def test_provenance_ignores_a_spec_row_belonging_to_another_repo(client, db_session):
    """Same path, same GUID, different ``AutomationProject`` — no cross-attribution."""
    mine = _repo(db_session, repo="web")
    other = _repo(db_session, repo="admin")
    _file(db_session, mine, SPEC_PATH, kind="spec", code=SPEC_CODE)
    _spec_row(db_session, other, filename=SPEC_PATH, run_code="RUN-ADMIN")

    assert _file_body(client, mine)["provenance"] is None


def test_provenance_omits_another_users_run(app, client, db_session):
    """Defence in depth: the ``Run`` leg is scoped by ``owned()`` too.

    The repo is mine, so ``_project_or_404`` lets the read through; the run that
    wrote the spec is someone else's, and only the ``owned()`` wrapper on the join
    keeps their run name out of my response.
    """
    me = _as_me(app, db_session)
    project = _repo(db_session, owner_id=me.id)
    _file(db_session, project, SPEC_PATH, kind="spec", code=SPEC_CODE)
    _spec_row(
        db_session,
        project,
        filename=SPEC_PATH,
        owner_id=me.id + 1000,
        run_code="RUN-THEIRS",
        run_name="Someone else's regression",
    )

    response = client.get(
        f"/projects/{GUID}/automation/repos/{project.id}/file", params={"path": SPEC_PATH}
    )

    assert response.status_code == 200
    assert response.json()["provenance"] is None
    assert "RUN-THEIRS" not in response.text
    assert "Someone else" not in response.text


def test_the_tree_grew_no_has_provenance_flag(client, db_session):
    """Explicitly *not* added: it would cost an N-row join per tree request.

    The predicate the client needs is exactly ``kind === "spec"``, which the tree
    already carries.
    """
    project = _repo(db_session)
    _file(db_session, project, SPEC_PATH, kind="spec", code=SPEC_CODE)
    _spec_row(db_session, project, filename=SPEC_PATH)

    body = client.get(f"/projects/{GUID}/automation/repos/{project.id}/files").json()

    assert body["files"], "the tree must still list the spec"
    for entry in body["files"]:
        assert "hasProvenance" not in entry, entry
        assert "provenance" not in entry, entry


# ---------------------------------------------------------------------------
# /repos/{id}/export/zip
# ---------------------------------------------------------------------------


@requires_git
def test_export_zip_returns_the_archive_and_the_filename_header(client, db_session):
    from app.models.audit import AuditLog

    project = aps.ensure_project(db_session, OWNER_ID, PROJECT_KEY, "web", project_guid=GUID)
    page = aps.project_dir(project) / "pages" / "LoginPage.ts"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(PAGE_CODE, encoding="utf-8")
    aps.git_commit(project, "feat: LoginPage")

    response = client.get(f"/projects/{GUID}/automation/repos/{project.id}/export/zip")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert 'attachment; filename="' in response.headers["content-disposition"]
    assert response.headers["access-control-expose-headers"] == "Content-Disposition"
    # The observable effect: a real archive holding the file we wrote.
    names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    assert any(name.endswith("pages/LoginPage.ts") for name in names), names
    # Audited against the project, with no run in the target — there is no run here.
    rows = [
        row
        for row in db_session.query(AuditLog).all()
        if row.action == "Exported the automation project as a ZIP"
    ]
    assert rows, "the project-keyed export was not audited"
    assert rows[0].target == project.slug


def test_export_zip_400s_when_the_repo_has_no_files_on_disk(client, db_session):
    """A seeded row with no tree: a clear refusal, not an empty ZIP."""
    project = _repo(db_session)

    response = client.get(f"/projects/{GUID}/automation/repos/{project.id}/export/zip")

    assert response.status_code == 400
    assert "Traceback" not in response.text


def test_export_zip_404s_for_a_repo_not_reachable_from_this_guid(client, db_session):
    theirs = _repo(db_session, project_guid=OTHER_GUID)

    response = client.get(f"/projects/{GUID}/automation/repos/{theirs.id}/export/zip")

    assert response.status_code == 404


def test_export_zip_404s_not_403_for_another_users_repo(app, client, db_session):
    me = _as_me(app, db_session)
    theirs = _repo(db_session, owner_id=me.id + 1000)

    response = client.get(f"/projects/{GUID}/automation/repos/{theirs.id}/export/zip")

    assert response.status_code == 404
    assert response.json()["detail"] == "AutomationProject not found"


def test_the_run_keyed_export_route_is_unchanged(client, db_session):
    """The run-keyed twin still exists and still resolves through its own run.

    #768 adds a project-keyed route; it does not refactor the run-keyed one, and
    a shared helper would couple two routers to save five lines of headers.
    """
    from app.main import create_app

    paths = set(create_app().openapi()["paths"])
    assert "/runs/{run_id}/automation/export/zip" in paths
    assert "/projects/{project_guid}/automation/repos/{project_id}/export/zip" in paths


# ---------------------------------------------------------------------------
# Running the selected specs — project-scoped execution (#797)
# ---------------------------------------------------------------------------


@pytest.fixture
def no_worker(monkeypatch):
    """Stop the background Playwright worker from ever starting.

    Patches the **service** attribute, which is the one the router resolves at
    call time (it does ``from app.services import project_execution`` and then
    ``project_execution.start_in_thread(...)``). These tests assert on what the
    endpoint creates synchronously; the worker has its own coverage.
    """
    started: list[int] = []
    monkeypatch.setattr(
        "app.services.project_execution.start_in_thread", lambda execution_id: started.append(execution_id)
    )
    return started


def _executions_url(project, guid: str = GUID) -> str:
    return f"/projects/{guid}/automation/repos/{project.id}/executions"


def _count_executions(db_session) -> int:
    from app.models.execution import Execution

    return db_session.query(Execution).count()


def test_starting_a_project_execution_creates_a_run_less_execution_per_selected_spec(
    client, db_session, no_worker
):
    """The whole point of #795: a suite run with no Run behind it.

    Asserts the *identity* of what was created, not just a 200 — ``runId`` is
    None, the repo is recorded, and there is exactly one result per selected
    spec carrying ``specPath`` in the order asked for. A handler that fell back
    to the run-scoped path would fail on the first of those.
    """
    project = _repo(db_session)
    _file(db_session, project, "pages/LoginPage.ts", kind="page")
    _file(db_session, project, "tests/SUR-1428/b.spec.ts", kind="spec", code=SPEC_CODE)
    _file(db_session, project, "tests/SUR-1428/a.spec.ts", kind="spec", code=SPEC_CODE)

    response = client.post(
        _executions_url(project),
        json={"specPaths": ["tests/SUR-1428/b.spec.ts", "tests/SUR-1428/a.spec.ts"]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["runId"] is None
    assert body["automationProjectId"] == project.id
    assert body["target"] == "server"
    assert body["total"] == 2
    # Selection order is preserved — the client chose it, and the report lists in it.
    assert [r["specPath"] for r in body["results"]] == [
        "tests/SUR-1428/b.spec.ts",
        "tests/SUR-1428/a.spec.ts",
    ]
    assert {r["status"] for r in body["results"]} == {"pending"}
    # ...and the worker was handed exactly this execution.
    assert no_worker == [body["id"]]

    from app.models.execution import Execution

    stored = db_session.get(Execution, body["id"])
    assert stored.run_id is None
    assert stored.automation_project_id == project.id


def test_starting_a_project_execution_never_defaults_to_the_whole_repo(
    client, db_session, no_worker
):
    """An empty selection is a refusal, not an implicit "run everything".

    This is a deliberate design rule, not an input-validation nicety: an
    automation repo is shared across q-agent projects, so "every spec in the
    repo" is a *different set* from "this project's specs" (#795). The negative
    control is the row count — a handler that quietly ran everything would also
    return 200 here, so the test pins that **nothing** was created.
    """
    project = _repo(db_session)
    _file(db_session, project, "tests/SUR-1428/a.spec.ts", kind="spec", code=SPEC_CODE)

    for body in ({}, {"specPaths": []}):
        response = client.post(_executions_url(project), json=body)
        assert response.status_code == 400, response.text
        assert "at least one spec" in response.json()["detail"]

    assert _count_executions(db_session) == 0
    assert no_worker == []


def test_starting_a_project_execution_refuses_a_shared_asset(client, db_session, no_worker):
    """A page object is not runnable on its own — and the spec beside it is.

    Both requests are made in one body so the refusal cannot be passing because
    the mirror lookup matches nothing at all: the *only* difference between them
    is the mirror row's ``kind``.
    """
    project = _repo(db_session)
    _file(db_session, project, "pages/LoginPage.ts", kind="page")
    _file(db_session, project, "tests/SUR-1428/a.spec.ts", kind="spec", code=SPEC_CODE)

    refused = client.post(_executions_url(project), json={"specPaths": ["pages/LoginPage.ts"]})
    assert refused.status_code == 400
    assert "pages/LoginPage.ts" in refused.json()["detail"]

    accepted = client.post(
        _executions_url(project), json={"specPaths": ["tests/SUR-1428/a.spec.ts"]}
    )
    assert accepted.status_code == 200, accepted.text


def test_starting_a_project_execution_refuses_a_spec_from_another_repo(
    client, db_session, no_worker
):
    """A path that exists — in a *different* repo — cannot be run through this one.

    The mirror lookup is scoped to ``project_id``, so naming another repo's spec
    is a 400 rather than a cross-repo hop. The other repo is reachable from the
    same GUID, which is what makes this a real test: resolution is not what
    refuses it.
    """
    project = _repo(db_session)
    other = _repo(db_session, repo="admin")
    _file(db_session, project, "tests/SUR-1428/a.spec.ts", kind="spec", code=SPEC_CODE)
    _file(db_session, other, "tests/SUR-9999/z.spec.ts", kind="spec", code=SPEC_CODE)

    response = client.post(
        _executions_url(project), json={"specPaths": ["tests/SUR-9999/z.spec.ts"]}
    )

    assert response.status_code == 400
    assert "tests/SUR-9999/z.spec.ts" in response.json()["detail"]
    assert _count_executions(db_session) == 0


def test_starting_a_project_execution_queues_the_local_agent_target_without_a_worker(
    app, client, db_session, no_worker
):
    """#798 makes the local-agent target real: queued for a device, never run here.

    Two things are asserted that a plain 200 would not catch. First the refusal
    leg: with no paired device the request is a 409 and creates **nothing** —
    a queued row no device can claim would spin the tab forever, which is the
    reason #797 refused this target outright. Then the accept leg pins *which
    branch ran*: ``status == "queued"`` and, the observable effect,
    ``no_worker == []`` — the in-process worker was never handed the execution.
    """
    from app.models.agent_device import AgentDevice

    # A real owner, because `AgentDevice.owner_id` is NOT NULL — a device can only
    # ever be paired to a user, so the un-owned default repo cannot express "paired".
    me = _as_me(app, db_session)
    project = _repo(db_session, owner_id=me.id)
    _file(db_session, project, "tests/SUR-1428/a.spec.ts", kind="spec", code=SPEC_CODE)
    body = {"specPaths": ["tests/SUR-1428/a.spec.ts"], "target": "local-agent"}

    refused = client.post(_executions_url(project), json=body)
    assert refused.status_code == 409, refused.text
    assert "No local agent paired" in refused.json()["detail"]
    assert _count_executions(db_session) == 0
    assert no_worker == []

    db_session.add(AgentDevice(owner_id=project.owner_id, name="Paired", token_hash="x" * 64))
    db_session.commit()

    response = client.post(_executions_url(project), json=body)
    assert response.status_code == 200, response.text
    created = response.json()
    assert created["target"] == "local-agent"
    assert created["status"] == "queued"
    assert created["runId"] is None
    # The branch that matters: nothing was executed in-process.
    assert no_worker == []

    from app.models.execution import Execution

    stored = db_session.get(Execution, created["id"])
    assert stored.status == "queued"
    assert stored.started_at is None


def test_the_server_target_persists_its_report_json_for_the_viewer(
    client, db_session, no_worker, monkeypatch
):
    """#798: the report viewer must not care which target produced the execution.

    The server target already writes ``report.json`` into its staging dir, so the
    only question is whether it survives the run. Asserted through the read
    endpoint (which is how the SPA gets it) **and** on disk under the owner's
    scope — a handler that stuffed it onto the row would pass the first check and
    fail the second.
    """
    from app.services import execution_report_service, project_execution

    project = _repo(db_session)
    _file(db_session, project, "tests/SUR-1428/a.spec.ts", kind="spec", code=SPEC_CODE)

    created = client.post(
        _executions_url(project), json={"specPaths": ["tests/SUR-1428/a.spec.ts"]}
    ).json()

    report = _report_for([("tests/SUR-1428/a.spec.ts", "passed")])
    # A key `parse_playwright_report` throws away — the viewer exists to show it,
    # so the stored document must be Playwright's, not a re-derived summary.
    report["config"] = {"version": "1.48.0"}

    def fake_invoke(spec_dir, workers, timeout_s, spec_file="", **_kwargs):
        (spec_dir / "report.json").write_text(_json.dumps(report), encoding="utf-8")
        return 0, "1 passed", ""

    monkeypatch.setattr(project_execution, "_invoke_playwright", fake_invoke)
    project_execution.run(created["id"])

    served = client.get(f"/executions/{created['id']}/report")
    assert served.status_code == 200, served.text
    assert served.json()["config"]["version"] == "1.48.0"

    from app.models.execution import Execution

    db_session.expire_all()
    execution = db_session.get(Execution, created["id"])
    assert execution.status == "done"
    assert execution_report_service.report_path(execution).is_file()


def test_an_execution_with_no_stored_report_is_a_404_not_an_empty_body(
    client, db_session, no_worker
):
    """The negative control for the test above: nothing ran, so nothing is served."""
    project = _repo(db_session)
    _file(db_session, project, "tests/SUR-1428/a.spec.ts", kind="spec", code=SPEC_CODE)
    created = client.post(
        _executions_url(project), json={"specPaths": ["tests/SUR-1428/a.spec.ts"]}
    ).json()

    response = client.get(f"/executions/{created['id']}/report")
    assert response.status_code == 404, response.text
    assert "No report stored" in response.json()["detail"]


def test_project_execution_history_is_newest_first_and_excludes_run_scoped_ones(
    client, db_session, no_worker
):
    """History is scoped to *project* executions of *this* repo.

    The run-scoped execution seeded here points at the same
    ``automation_project_id``, which is legitimate — a run executes specs out of
    a project's repo too. It must not appear in the tab's history, so the query's
    ``run_id IS NULL`` leg is what this pins; without it the assertion on the id
    list fails rather than merely counting one extra.
    """
    from app.models.execution import Execution
    from app.models.run import Run

    project = _repo(db_session)
    _file(db_session, project, "tests/SUR-1428/a.spec.ts", kind="spec", code=SPEC_CODE)

    first = client.post(
        _executions_url(project), json={"specPaths": ["tests/SUR-1428/a.spec.ts"]}
    ).json()["id"]
    second = client.post(
        _executions_url(project), json={"specPaths": ["tests/SUR-1428/a.spec.ts"]}
    ).json()["id"]

    run = Run(code="RUN-777", name="A run", status="executing", project_guid=GUID)
    db_session.add(run)
    db_session.flush()
    db_session.add(
        Execution(
            run_id=run.id,
            automation_project_id=project.id,
            status="done",
            target="server",
            env="",
            browser="chromium",
            workers=1,
            total=1,
        )
    )
    db_session.commit()

    response = client.get(_executions_url(project))

    assert response.status_code == 200
    assert [e["id"] for e in response.json()] == [second, first]
    # A summary, not the detail — results stay behind GET /executions/{id}.
    assert "results" not in response.json()[0]


def test_get_execution_serves_a_run_less_execution(client, db_session, no_worker):
    """``GET /executions/{id}`` used to 404 every project-scoped execution.

    It scoped through ``get_owned_or_404(db, Run, execution.run_id, …)``, and
    ``db.get(Run, None)`` is ``None``. Pinning ``specPath`` on the way out as
    well: it is the only identity such a result has.
    """
    project = _repo(db_session)
    _file(db_session, project, "tests/SUR-1428/a.spec.ts", kind="spec", code=SPEC_CODE)
    created = client.post(
        _executions_url(project), json={"specPaths": ["tests/SUR-1428/a.spec.ts"]}
    ).json()

    response = client.get(f"/executions/{created['id']}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["runId"] is None
    assert [r["specPath"] for r in body["results"]] == ["tests/SUR-1428/a.spec.ts"]


def test_starting_a_project_execution_is_404_for_another_users_repo(client, app, db_session):
    """Ownership is enforced on the write, not just on the reads.

    The suite runs with ``auth_required=False``, so this overrides
    ``current_user`` with a real user — otherwise the #91 bridge skips the check
    and the test would pass against a handler that has none.
    """
    project = _repo(db_session, owner_id=None)
    _file(db_session, project, "tests/SUR-1428/a.spec.ts", kind="spec", code=SPEC_CODE)
    me = _as_me(app, db_session)
    project.owner_id = me.id + 1000
    db_session.commit()

    response = client.post(
        _executions_url(project), json={"specPaths": ["tests/SUR-1428/a.spec.ts"]}
    )

    assert response.status_code == 404
    assert _count_executions(db_session) == 0


def test_match_result_identifies_a_project_scoped_result_by_spec_path():
    """A project-scoped result has no ticket or case, so the conventions can't name it.

    Both conventions build ``"{ticket}-{case}.spec.ts"``, which for empty fields
    is ``"-.spec.ts"`` — it would match nothing, and a spec run out of a repo is
    under no obligation to follow that naming at all. The last assertion is the
    negative control: a row with a ``spec_path`` must not start swallowing
    run-scoped filenames.
    """
    from app.models.execution import ExecutionResult
    from app.services.execution_service import match_result

    project_row = ExecutionResult(
        test_case_id=0, ticket_external_id="", case_code="",
        spec_path="tests/1377/1377-TC-09.spec.ts", status="pending",
    )
    run_row = ExecutionResult(
        test_case_id=1, ticket_external_id="SUR-1428", case_code="TC-01",
        spec_path="", status="pending",
    )
    rows = [project_row, run_row]

    # The full repo-relative path, as the selection recorded it.
    assert match_result(rows, "tests/1377/1377-TC-09.spec.ts") is project_row
    # ...and the basename, because Playwright reports paths relative to testDir.
    assert match_result(rows, "1377-TC-09.spec.ts") is project_row
    # The run-scoped convention still wins for a run-scoped file.
    assert match_result(rows, "SUR-1428-TC-01.spec.ts") is run_row
    assert match_result(rows, "nothing-at-all.spec.ts") is None


def _report_for(entries: list[tuple[str, str]]) -> dict:
    """A minimal Playwright JSON report: ``[(file, "passed"|"failed"), …]``."""
    return {
        "suites": [
            {
                "title": file,
                "file": file,
                "specs": [
                    {
                        "title": f"spec for {file}",
                        "file": file,
                        "ok": status == "passed",
                        "tests": [
                            {
                                "status": "expected" if status == "passed" else "unexpected",
                                "results": [
                                    {
                                        "status": status,
                                        "duration": 42,
                                        "error": (
                                            {} if status == "passed" else {"message": "boom"}
                                        ),
                                        "attachments": [],
                                    }
                                ],
                            }
                        ],
                    }
                ],
                "suites": [],
            }
            for file, status in entries
        ]
    }


def test_the_worker_runs_the_selected_specs_and_records_each_outcome(
    client, db_session, no_worker, monkeypatch
):
    """End-to-end through ``project_execution.run`` with Playwright mocked out.

    Called synchronously rather than through the thread the endpoint spawns, so
    the assertions cannot race it. What this actually proves, beyond a status:

    * The staged dir is built **from the mirror**. The seeded repo has no working
      tree on disk at all (``_repo`` writes no files), so every spec Playwright
      is asked to run had to be materialized from ``automation_files`` — the
      fallback in ``_stage``. The mock asserts the files are really there.
    * Results are attributed by ``spec_path``, mixing a pass and a failure so a
      handler that stamped one status over the whole set cannot pass.
    * ``finalize`` works with ``run=None``: status ``done``, progress 100, and no
      attempt to advance a run that does not exist.
    """
    from app.models.execution import Execution, ExecutionResult
    from app.services import project_execution

    project = _repo(db_session)
    _file(db_session, project, "tests/SUR-1428/a.spec.ts", kind="spec", code=SPEC_CODE)
    _file(db_session, project, "tests/SUR-1428/b.spec.ts", kind="spec", code=SPEC_CODE)

    created = client.post(
        _executions_url(project),
        json={"specPaths": ["tests/SUR-1428/a.spec.ts", "tests/SUR-1428/b.spec.ts"]},
    ).json()

    staged_specs: list[str] = []

    def fake_invoke(spec_dir, workers, timeout_s, spec_file="", **_kwargs):
        # Whatever ran had to be staged from the mirror — assert it, don't assume.
        staged_specs.extend(
            sorted(p.relative_to(spec_dir).as_posix() for p in spec_dir.rglob("*.spec.ts"))
        )
        (spec_dir / "report.json").write_text(
            _json.dumps(
                _report_for(
                    [("tests/SUR-1428/a.spec.ts", "passed"), ("tests/SUR-1428/b.spec.ts", "failed")]
                )
            ),
            encoding="utf-8",
        )
        return 0, "1 passed, 1 failed", ""

    monkeypatch.setattr(project_execution, "_invoke_playwright", fake_invoke)

    project_execution.run(created["id"])

    assert staged_specs == ["tests/SUR-1428/a.spec.ts", "tests/SUR-1428/b.spec.ts"]

    db_session.expire_all()
    execution = db_session.get(Execution, created["id"])
    assert execution.status == "done"
    assert execution.progress == 100
    assert (execution.passed, execution.failed) == (1, 1)
    assert execution.finished_at is not None

    outcomes = {
        r.spec_path: (r.status, r.error_message)
        for r in db_session.query(ExecutionResult)
        .filter(ExecutionResult.execution_id == execution.id)
        .all()
    }
    assert outcomes["tests/SUR-1428/a.spec.ts"][0] == "pass"
    assert outcomes["tests/SUR-1428/b.spec.ts"][0] == "fail"
    assert "boom" in outcomes["tests/SUR-1428/b.spec.ts"][1]


def test_the_worker_fails_a_spec_playwright_never_reported_on(
    client, db_session, no_worker, monkeypatch
):
    """A missing report entry is a failure, never a row left ``running`` forever.

    Playwright reports on only one of the two selected specs here (the shape a
    crashed or filtered-out spec produces), so the reconcile pass is the only
    thing that can give the second row a terminal status.
    """
    from app.models.execution import Execution, ExecutionResult
    from app.services import project_execution

    project = _repo(db_session)
    _file(db_session, project, "tests/SUR-1428/a.spec.ts", kind="spec", code=SPEC_CODE)
    _file(db_session, project, "tests/SUR-1428/b.spec.ts", kind="spec", code=SPEC_CODE)

    created = client.post(
        _executions_url(project),
        json={"specPaths": ["tests/SUR-1428/a.spec.ts", "tests/SUR-1428/b.spec.ts"]},
    ).json()

    def fake_invoke(spec_dir, workers, timeout_s, spec_file="", **_kwargs):
        (spec_dir / "report.json").write_text(
            _json.dumps(_report_for([("tests/SUR-1428/a.spec.ts", "passed")])), encoding="utf-8"
        )
        return 1, "", ""

    monkeypatch.setattr(project_execution, "_invoke_playwright", fake_invoke)

    project_execution.run(created["id"])

    db_session.expire_all()
    execution = db_session.get(Execution, created["id"])
    assert (execution.passed, execution.failed) == (1, 1)
    assert execution.status == "done"
    unreported = (
        db_session.query(ExecutionResult)
        .filter(
            ExecutionResult.execution_id == execution.id,
            ExecutionResult.spec_path == "tests/SUR-1428/b.spec.ts",
        )
        .one()
    )
    assert unreported.status == "fail"
    assert "No result reported" in unreported.error_message


def test_the_worker_reports_a_missing_report_on_every_spec(
    client, db_session, no_worker, monkeypatch
):
    """Playwright writing no report at all must surface its own output, not silence."""
    from app.models.execution import Execution, ExecutionResult
    from app.services import project_execution

    project = _repo(db_session)
    _file(db_session, project, "tests/SUR-1428/a.spec.ts", kind="spec", code=SPEC_CODE)

    created = client.post(
        _executions_url(project), json={"specPaths": ["tests/SUR-1428/a.spec.ts"]}
    ).json()

    monkeypatch.setattr(
        project_execution,
        "_invoke_playwright",
        lambda *a, **k: (1, "", "Error: could not resolve @playwright/test"),
    )

    project_execution.run(created["id"])

    db_session.expire_all()
    execution = db_session.get(Execution, created["id"])
    assert (execution.passed, execution.failed) == (0, 1)
    result = (
        db_session.query(ExecutionResult)
        .filter(ExecutionResult.execution_id == execution.id)
        .one()
    )
    assert result.status == "fail"
    assert "could not resolve @playwright/test" in result.error_message


def test_a_crashed_worker_still_leaves_a_terminal_status(
    client, db_session, no_worker, monkeypatch
):
    """A crash must not leave the tab's progress bar spinning forever.

    The run path can afford to bail on an exception — a Run has ``failed_stage``
    and a retry endpoint — but a project execution's entire lifecycle is this one
    row, so an unfinished ``running`` is unrecoverable from the UI.
    """
    from app.models.execution import Execution
    from app.services import project_execution

    project = _repo(db_session)
    _file(db_session, project, "tests/SUR-1428/a.spec.ts", kind="spec", code=SPEC_CODE)
    created = client.post(
        _executions_url(project), json={"specPaths": ["tests/SUR-1428/a.spec.ts"]}
    ).json()

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk is on fire")

    monkeypatch.setattr(project_execution, "_write_config", boom)

    project_execution.run(created["id"])

    db_session.expire_all()
    execution = db_session.get(Execution, created["id"])
    assert execution.status == "done"
    assert execution.finished_at is not None
    assert execution.failed == 1
    assert "disk is on fire" in execution.log


def test_failing_everything_does_not_overwrite_specs_that_already_passed(monkeypatch):
    """The crash path recomputes counts; it does not stamp ``fail`` over a pass.

    A crash can fire *after* some specs genuinely finished, and reporting a
    half-green suite as entirely failed would be a lie the user cannot see past.
    ``finalize`` is stubbed so the assertion is about the row arithmetic alone,
    and it captures ``run`` to pin that a project execution finalizes with none.
    """
    from types import SimpleNamespace

    from app.models.execution import Execution, ExecutionResult
    from app.services import execution_service, project_execution

    passed = ExecutionResult(
        test_case_id=0, ticket_external_id="", case_code="",
        spec_path="a.spec.ts", status="pass", duration_ms=11,
    )
    pending = ExecutionResult(
        test_case_id=0, ticket_external_id="", case_code="",
        spec_path="b.spec.ts", status="running",
    )
    execution = Execution(run_id=None, automation_project_id=7, total=2)

    finalized: dict = {}
    monkeypatch.setattr(
        execution_service,
        "finalize",
        lambda db, ex, run, log, advance_run=True: finalized.update(
            passed=ex.passed, failed=ex.failed, total=ex.total, run=run, log=log
        ),
    )

    project_execution._fail_all(
        SimpleNamespace(commit=lambda: None), execution, [passed, pending], "it broke"
    )

    assert (passed.status, passed.duration_ms) == ("pass", 11)
    assert (pending.status, pending.error_message) == ("fail", "it broke")
    assert finalized == {"passed": 1, "failed": 1, "total": 2, "run": None, "log": "it broke"}
