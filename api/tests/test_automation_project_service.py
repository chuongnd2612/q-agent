"""Tests for the persistent git-backed automation project (#538).

Covers every acceptance-criteria bullet of the foundation slice: idempotent
``ensure_project``, the git commit/reset round trip, ``ensure_deps`` install +
no-op + vendored fallback + graceful degradation, ``inventory`` signatures,
``diff_is_additive``, concurrent ``ensure_project`` serialization, and the
``bundle_for_agent`` exclusions.

Nothing here touches the network: every ``ensure_deps`` call injects a fake npm
runner via the ``runner=`` seam.
"""

from __future__ import annotations

import shutil
import subprocess
import threading

import pytest

from app.services import automation_project_service as aps

pytestmark = pytest.mark.usefixtures("workspace_dir")


LOGIN_PAGE = """import { Page } from '@playwright/test';

export class LoginPage {
  constructor(private readonly page: Page) {}

  async openCreateUser() {
    await this.page.click('#create-user');
  }

  async fillUser(user: { name: string }): Promise<void> {
    await this.page.fill('#name', user.name);
  }

  async submit(force: boolean = false) {
    if (force) {
      await this.page.click('#submit');
    }
  }
}
"""


def _git_available() -> bool:
    return shutil.which("git") is not None


requires_git = pytest.mark.skipif(not _git_available(), reason="git not on PATH")


def _write_page(project, name: str = "LoginPage.ts", code: str = LOGIN_PAGE):
    path = aps.project_dir(project) / "pages" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# ensure_project
# ---------------------------------------------------------------------------


def test_ensure_project_is_idempotent_same_row_and_dir(db_session):
    first = aps.ensure_project(db_session, 7, "SUR", "web")
    second = aps.ensure_project(db_session, 7, "SUR", "web")

    assert first.id == second.id
    assert aps.project_dir(first) == aps.project_dir(second)
    assert aps.project_dir(first).is_dir()
    from app.models.automation_project import AutomationProject

    assert db_session.query(AutomationProject).count() == 1


def test_project_dir_is_repo_scoped_under_the_owner_scope(db_session):
    web = aps.ensure_project(db_session, 7, "SUR", "web")
    admin = aps.ensure_project(db_session, 7, "SUR", "admin")
    other_owner = aps.ensure_project(db_session, 8, "SUR", "web")

    assert aps.project_dir(web) != aps.project_dir(admin)
    assert aps.project_dir(web) != aps.project_dir(other_owner)
    assert "users/7" in aps.project_dir(web).as_posix()
    assert aps.project_dir(web).name == "web"
    # A project with no distinct repo collapses to "default".
    only = aps.ensure_project(db_session, 7, "SUR")
    assert aps.project_dir(only).name == "default"


def test_materialize_scaffold_creates_skeleton_and_never_overwrites(db_session):
    project = aps.ensure_project(db_session, 1, "SUR", "web")
    root = aps.project_dir(project)

    for relative in aps.SCAFFOLD_DIRS:
        assert (root / relative).is_dir(), relative
    assert aps.BASE_PACKAGE in (root / "package.json").read_text(encoding="utf-8")
    assert "node_modules/" in (root / ".gitignore").read_text(encoding="utf-8")

    (root / "playwright.config.ts").write_text("// hand edited", encoding="utf-8")
    aps.materialize_scaffold(project)
    assert (root / "playwright.config.ts").read_text(encoding="utf-8") == "// hand edited"


# ---------------------------------------------------------------------------
# git — replaces snapshot/rollback
# ---------------------------------------------------------------------------


@requires_git
def test_project_dir_is_a_real_git_repo_with_an_initial_commit(db_session):
    project = aps.ensure_project(db_session, 1, "SUR", "web")
    assert (aps.project_dir(project) / ".git").exists()
    assert aps.head_commit(project)


@requires_git
def test_git_commit_then_reset_hard_restores_exactly(db_session):
    project = aps.ensure_project(db_session, 1, "SUR", "web")
    page = _write_page(project)
    assert aps.git_commit(project, "feat: add LoginPage")
    committed = aps.head_commit(project)

    # Mutate a tracked file and add an untracked one, then roll back.
    page.write_text("export class LoginPage {}\n", encoding="utf-8")
    stray = aps.project_dir(project) / "pages" / "Stray.ts"
    stray.write_text("export class Stray {}\n", encoding="utf-8")

    assert aps.git_reset_hard(project)
    assert page.read_text(encoding="utf-8") == LOGIN_PAGE
    assert not stray.exists()
    assert aps.head_commit(project) == committed


@requires_git
def test_git_stash_removes_working_changes(db_session):
    project = aps.ensure_project(db_session, 1, "SUR", "web")
    page = _write_page(project)
    aps.git_commit(project, "feat: add LoginPage")
    page.write_text("// broken\n", encoding="utf-8")

    assert aps.git_stash(project, "attempt")
    assert page.read_text(encoding="utf-8") == LOGIN_PAGE


@requires_git
def test_git_init_is_idempotent(db_session):
    project = aps.ensure_project(db_session, 1, "SUR", "web")
    head = aps.head_commit(project)
    assert aps.git_init(project)
    assert aps.head_commit(project) == head


def test_head_commit_is_none_outside_a_repo(db_session, monkeypatch):
    project = aps.ensure_project(db_session, 1, "SUR", "web")
    shutil.rmtree(aps.project_dir(project) / ".git", ignore_errors=True)
    assert aps.head_commit(project) is None


# ---------------------------------------------------------------------------
# ensure_deps
# ---------------------------------------------------------------------------


class FakeNpm:
    """Injectable npm stand-in: records calls, optionally "installs" the package."""

    def __init__(self, *, succeed_on: tuple[str, ...] = (), root=None):
        self.calls: list[list[str]] = []
        self._succeed_on = succeed_on
        self._root = root

    def __call__(self, args, cwd):
        self.calls.append(list(args))
        joined = " ".join(args)
        if any(token in joined for token in self._succeed_on):
            target = (self._root or cwd) / "node_modules" / aps.BASE_PACKAGE
            target.mkdir(parents=True, exist_ok=True)
            (target / "package.json").write_text('{"version":"1.0.0"}', encoding="utf-8")
            return True
        return False


def test_ensure_deps_installs_base_once_then_is_a_noop(db_session):
    project = aps.ensure_project(db_session, 1, "SUR", "web")
    npm = FakeNpm(succeed_on=(aps.BASE_PACKAGE,))

    assert aps.ensure_deps(project, runner=npm) == "registry"
    assert npm.calls == [["install", f"{aps.BASE_PACKAGE}@{aps.BASE_VERSION_SPEC}"]]
    assert project.base_version == aps.BASE_VERSION

    assert aps.ensure_deps(project, runner=npm) == "cached"
    assert len(npm.calls) == 1  # second call ran no npm at all


def test_ensure_deps_uses_npm_ci_when_a_lockfile_exists(db_session):
    project = aps.ensure_project(db_session, 1, "SUR", "web")
    (aps.project_dir(project) / "package-lock.json").write_text("{}", encoding="utf-8")
    npm = FakeNpm(succeed_on=("ci",))

    assert aps.ensure_deps(project, runner=npm) == "registry"
    assert npm.calls == [["ci"]]


def test_ensure_deps_falls_back_to_the_pinned_vendored_tarball(db_session, tmp_path, monkeypatch):
    project = aps.ensure_project(db_session, 1, "SUR", "web")
    tarball = tmp_path / "q-agent-playwright-base-1.0.0.tgz"
    tarball.write_bytes(b"fake-tarball")
    monkeypatch.setattr(aps, "vendored_tarball", lambda: tarball)
    # The registry is unreachable: only the tarball install succeeds.
    npm = FakeNpm(succeed_on=(".tgz",))

    assert aps.ensure_deps(project, runner=npm) == "vendored"
    assert npm.calls[0] == ["install", f"{aps.BASE_PACKAGE}@{aps.BASE_VERSION_SPEC}"]
    assert npm.calls[-1] == ["install", str(tarball)]
    assert aps.deps_installed(project)
    assert project.base_version == aps.BASE_VERSION


def test_ensure_deps_degrades_gracefully_with_no_registry_and_no_tarball(db_session, monkeypatch):
    project = aps.ensure_project(db_session, 1, "SUR", "web")
    monkeypatch.setattr(aps, "vendored_tarball", lambda: None)
    npm = FakeNpm()  # everything fails

    assert aps.ensure_deps(project, runner=npm) == "unavailable"
    assert not aps.deps_installed(project)


def test_vendored_tarball_path_is_resolved_relative_to_the_repo_root():
    # The tarball itself is published by #539 and may not exist yet; only the
    # location contract is asserted here.
    from app.config import REPO_ROOT

    assert aps.VENDORED_TARBALL_RELPATH == "playwright-base/vendor/q-agent-playwright-base-1.0.0.tgz"
    resolved = aps.vendored_tarball()
    assert resolved is None or resolved == REPO_ROOT / aps.VENDORED_TARBALL_RELPATH


# ---------------------------------------------------------------------------
# inventory
# ---------------------------------------------------------------------------


def test_inventory_returns_method_signatures_for_a_handwritten_page_object(db_session):
    project = aps.ensure_project(db_session, 1, "SUR", "web")
    _write_page(project)

    entries = aps.inventory(project)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["path"] == "pages/LoginPage.ts"
    assert entry["kind"] == "page"
    assert entry["exports"] == ["LoginPage"]
    assert entry["methods"] == ["openCreateUser()", "fillUser(user)", "submit(force)"]


def test_inventory_covers_every_library_dir_and_kind(db_session):
    project = aps.ensure_project(db_session, 1, "SUR", "web")
    root = aps.project_dir(project)
    (root / "components" / "Nav.ts").write_text(
        "export class Nav {\n  async goTo(path: string) {\n    return path;\n  }\n}\n",
        encoding="utf-8",
    )
    (root / "utils" / "wait.ts").write_text(
        "export async function waitForIt(ms: number, retries = 2) {\n  return ms + retries;\n}\n",
        encoding="utf-8",
    )

    by_path = {e["path"]: e for e in aps.inventory(project)}
    assert by_path["components/Nav.ts"]["kind"] == "component"
    assert by_path["components/Nav.ts"]["methods"] == ["goTo(path)"]
    assert by_path["utils/wait.ts"]["kind"] == "util"
    assert by_path["utils/wait.ts"]["methods"] == ["waitForIt(ms, retries)"]


def test_inventory_ignores_specs_and_control_flow_blocks(db_session):
    project = aps.ensure_project(db_session, 1, "SUR", "web")
    root = aps.project_dir(project)
    (root / "tests" / "SUR-1428").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "SUR-1428" / "SUR-1428-TC-01.spec.ts").write_text(
        "export class NotAnAsset {}\n", encoding="utf-8"
    )
    _write_page(project)

    paths = [e["path"] for e in aps.inventory(project)]
    assert paths == ["pages/LoginPage.ts"]
    # `if (force) {` inside submit() must not be mistaken for a method.
    assert "if(force)" not in aps.inventory(project)[0]["methods"]


def test_write_inventory_persists_json_without_internal_fields(db_session):
    import json

    project = aps.ensure_project(db_session, 1, "SUR", "web")
    _write_page(project)
    path = aps.write_inventory(project)

    assert path == aps.project_dir(project) / ".qagent" / "inventory.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data[0]["methods"][0] == "openCreateUser()"
    assert not any(key.startswith("_") for key in data[0])


# ---------------------------------------------------------------------------
# diff_is_additive
# ---------------------------------------------------------------------------


def test_diff_is_additive_true_when_a_method_is_added(db_session):
    project = aps.ensure_project(db_session, 1, "SUR", "web")
    page = _write_page(project)
    before = aps.inventory(project)

    page.write_text(
        LOGIN_PAGE.replace(
            "  async submit(", "  async logout() {\n    await this.page.click('#out');\n  }\n\n  async submit("
        ),
        encoding="utf-8",
    )
    assert "logout()" in aps.inventory(project)[0]["methods"]
    assert aps.diff_is_additive(project, before) is True


def test_diff_is_additive_false_when_an_existing_method_is_removed(db_session):
    project = aps.ensure_project(db_session, 1, "SUR", "web")
    page = _write_page(project)
    before = aps.inventory(project)

    page.write_text(
        "export class LoginPage {\n  async openCreateUser() {\n"
        "    await this.page.click('#create-user');\n  }\n}\n",
        encoding="utf-8",
    )
    assert aps.diff_is_additive(project, before) is False


def test_diff_is_additive_false_when_an_existing_body_is_rewritten(db_session):
    project = aps.ensure_project(db_session, 1, "SUR", "web")
    page = _write_page(project)
    before = aps.inventory(project)

    page.write_text(
        LOGIN_PAGE.replace("await this.page.click('#create-user');", "await this.page.click('#other');"),
        encoding="utf-8",
    )
    assert aps.diff_is_additive(project, before) is False


def test_diff_is_additive_false_when_a_whole_asset_file_is_deleted(db_session):
    project = aps.ensure_project(db_session, 1, "SUR", "web")
    page = _write_page(project)
    before = aps.inventory(project)

    page.unlink()
    assert aps.diff_is_additive(project, before) is False


def test_diff_is_additive_ignores_reformatting(db_session):
    project = aps.ensure_project(db_session, 1, "SUR", "web")
    page = _write_page(project)
    before = aps.inventory(project)

    page.write_text(LOGIN_PAGE.replace("\n  }", "\n\n      }"), encoding="utf-8")
    assert aps.diff_is_additive(project, before) is True


# ---------------------------------------------------------------------------
# concurrency
# ---------------------------------------------------------------------------


def test_two_concurrent_ensure_project_calls_serialize(db_session):
    """One row, one intact tree — and the calls do not interleave."""
    import app.db as db_module
    from app.models.automation_project import AutomationProject

    order: list[str] = []
    original = aps.materialize_scaffold

    def slow_scaffold(project):
        order.append("enter")
        threading.Event().wait(0.05)
        order.append("exit")
        return original(project)

    aps.materialize_scaffold = slow_scaffold  # noqa: B010 - restored in finally
    results: list = []
    errors: list = []

    def worker():
        session = db_module.SessionLocal()
        try:
            results.append(aps.ensure_project(session, 5, "SUR", "web").id)
        except Exception as exc:  # noqa: BLE001 - surfaced in the assertion below
            errors.append(exc)
        finally:
            session.close()

    try:
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
    finally:
        aps.materialize_scaffold = original  # noqa: B010

    assert not errors, errors
    assert len(results) == 2 and results[0] == results[1]
    assert db_session.query(AutomationProject).count() == 1
    # Serialized, not interleaved: no "enter" follows an unmatched "enter".
    assert order == ["enter", "exit", "enter", "exit"]


def test_project_lock_returns_the_same_lock_per_key(db_session):
    project = aps.ensure_project(db_session, 1, "SUR", "web")
    assert aps.project_lock(project) is aps.project_lock(project.id)
    assert aps.project_lock("a/b/c") is aps.project_lock("a/b/c")
    assert aps.project_lock("a/b/c") is not aps.project_lock("a/b/d")


# ---------------------------------------------------------------------------
# specs / bundling / staging / db mirror
# ---------------------------------------------------------------------------


def test_write_spec_lands_under_tests_ticket_dir(db_session):
    from app.services import spec_service

    project = aps.ensure_project(db_session, 1, "SUR", "web")
    path = aps.write_spec(project, "SUR-1428", "TC-01", "// spec\n")

    assert path.parent == aps.project_dir(project) / "tests" / "SUR-1428"
    # Filename convention stays owned by spec_service (#540 changes it there).
    assert path.name == spec_service.spec_filename("SUR-1428", "TC-01")
    assert path.read_text(encoding="utf-8") == "// spec\n"


def test_write_spec_keeps_same_case_code_from_two_tickets_apart(db_session):
    project = aps.ensure_project(db_session, 1, "SUR", "web")
    first = aps.write_spec(project, "SUR-1428", "TC-01", "// sur\n")
    second = aps.write_spec(project, "OPS-1428", "TC-01", "// ops\n")

    assert first != second
    assert first.read_text(encoding="utf-8") == "// sur\n"
    assert second.read_text(encoding="utf-8") == "// ops\n"


def test_bundle_for_agent_excludes_tests_qagent_and_node_modules(db_session):
    project = aps.ensure_project(db_session, 1, "SUR", "web")
    root = aps.project_dir(project)
    _write_page(project)
    aps.write_spec(project, "SUR-1428", "TC-01", "// spec\n")
    (root / ".qagent" / "inventory.json").write_text("[]", encoding="utf-8")
    (root / ".qagent" / "plans").mkdir(parents=True, exist_ok=True)
    (root / ".qagent" / "plans" / "p.json").write_text("{}", encoding="utf-8")
    (root / "node_modules" / aps.BASE_PACKAGE).mkdir(parents=True, exist_ok=True)
    (root / "node_modules" / aps.BASE_PACKAGE / "index.js").write_text("x", encoding="utf-8")

    bundle = aps.bundle_for_agent(project)

    assert "pages/LoginPage.ts" in bundle
    assert "package.json" in bundle and "playwright.config.ts" in bundle
    assert not any(path.startswith("tests/") for path in bundle)
    assert not any(path.startswith(".qagent/") for path in bundle)
    assert not any(path.startswith("node_modules/") for path in bundle)
    assert not any(".git/" in path for path in bundle)


def test_stage_for_run_copies_the_library_but_only_this_runs_specs(db_session):
    from app.services.workspace_scope import scoped_specs_dir

    project = aps.ensure_project(db_session, 3, "SUR", "web")
    _write_page(project)
    old = aps.write_spec(project, "SUR-1428", "TC-01", "// old\n")
    current = aps.write_spec(project, "SUR-1502", "TC-01", "// current\n")
    relative = current.relative_to(aps.project_dir(project)).as_posix()

    staged = aps.stage_for_run(project, "RUN-211", [relative])

    assert staged == scoped_specs_dir(3) / "RUN-211"
    assert (staged / "pages" / "LoginPage.ts").is_file()
    assert (staged / "playwright.config.ts").is_file()
    assert (staged / relative).read_text(encoding="utf-8") == "// current\n"
    staged_specs = sorted(p.name for p in (staged / "tests").rglob("*.spec.ts"))
    assert staged_specs == [current.name]
    assert old.name not in staged_specs


def test_sync_files_to_db_mirrors_disk_and_prunes_deletions(db_session):
    from app.models.automation_project import AutomationFile

    project = aps.ensure_project(db_session, 1, "SUR", "web")
    page = _write_page(project)
    spec = aps.write_spec(project, "SUR-1428", "TC-01", "// spec\n")
    spec_relative = spec.relative_to(aps.project_dir(project)).as_posix()

    count = aps.sync_files_to_db(db_session, project)
    rows = {row.path: row for row in db_session.query(AutomationFile).all()}
    assert count == len(rows)
    assert rows["pages/LoginPage.ts"].kind == "page"
    assert rows["pages/LoginPage.ts"].code == LOGIN_PAGE
    assert rows["pages/LoginPage.ts"].sha256
    assert rows[spec_relative].kind == "spec"
    assert not any(path.startswith(".qagent/") for path in rows)

    page.write_text(LOGIN_PAGE + "\n// touched\n", encoding="utf-8")
    aps.sync_files_to_db(db_session, project)
    refreshed = db_session.query(AutomationFile).filter_by(path="pages/LoginPage.ts").one()
    assert "// touched" in refreshed.code

    page.unlink()
    aps.sync_files_to_db(db_session, project)
    assert db_session.query(AutomationFile).filter_by(path="pages/LoginPage.ts").count() == 0


def test_automation_spec_project_columns_are_nullable(db_session, seed_ticket):
    """Every existing spec keeps working as ``project_id IS NULL`` — no backfill."""
    from app.models.run import Run
    from app.models.testcase import AutomationSpec, TestCase

    run = Run(code="RUN-900", name="Automation project columns", owner_id=1)
    db_session.add(run)
    db_session.commit()
    case = TestCase(run_id=run.id, ticket_external_id="SUR-1428", code="TC-01", title="t")
    db_session.add(case)
    db_session.commit()
    spec = AutomationSpec(test_case_id=case.id, filename="1428-TC-01.spec.ts", code="// x")
    db_session.add(spec)
    db_session.commit()
    db_session.refresh(spec)

    assert spec.project_id is None
    assert spec.plan_report is None

    project = aps.ensure_project(db_session, 1, "SUR", "web")
    spec.project_id = project.id
    spec.plan_report = '{"reuse": 2, "extend": 1, "create": 0}'
    db_session.commit()
    db_session.refresh(spec)
    assert spec.project_id == project.id


def test_workspace_scope_exposes_the_automation_kind():
    from app.config import _SCOPED_KINDS
    from app.services.workspace_scope import scoped_automation_dir

    assert "automation" in _SCOPED_KINDS
    assert scoped_automation_dir(7).as_posix().endswith("users/7/automation")
    assert scoped_automation_dir(None).as_posix().endswith("shared/automation")


def test_run_npm_returns_false_when_npm_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(aps.shutil, "which", lambda _name: None)
    assert aps._run_npm(["install"], tmp_path) is False


def test_run_npm_returns_false_on_a_nonzero_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(aps.shutil, "which", lambda _name: "npm")

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(args=["npm"], returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(aps.subprocess, "run", fake_run)
    assert aps._run_npm(["install"], tmp_path) is False
