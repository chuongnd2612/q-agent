"""Project-keyed automation read endpoints (#768).

Covers ``routers/automation_projects.py``: the repo selector's aggregates, the
code-free file tree, lazy per-file content, path validation and the ZIP export —
all reached by **project GUID** rather than by run.

Three things this file is deliberately careful about:

* **It asserts which resolution leg ran**, not just the status code.
  ``projects_for_guid`` is a two-leg union, and a test that seeds both legs would
  pass even if one were deleted. Every resolution test therefore seeds exactly
  one leg: a stamped ``project_guid`` with no specs at all, or a row whose
  ``project_guid`` is NULL that is only findable through
  ``automation_specs -> test_cases -> runs.project_guid``.
* **It asserts the tree carries no ``code`` key.** That negative is the entire
  point of the slice — a tree that quietly regrew ``code`` would still pass every
  positive assertion.
* **Ownership is exercised with a real user.** The suite runs with
  ``auth_required=False``, so ``current_user`` resolves to ``None`` and the #91
  ownership bridge lets everything through; the ownership tests override the
  dependency so the check actually runs.
"""

from __future__ import annotations

import hashlib
import io
import shutil
import zipfile

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
    """"No automation yet" is a state, not an error — the tab renders an empty view."""
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
    assert PAGE_CODE.strip() not in client.get(
        f"/projects/{GUID}/automation/repos/{project.id}/files"
    ).text


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


def test_file_serves_the_mirror_content_with_null_provenance(client, db_session):
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
    # Typed and on the wire already; #769 populates it for spec files.
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
