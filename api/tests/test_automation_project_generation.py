"""Generation, gating and execution staging against the persistent project (#540).

Covers every acceptance-criteria bullet of the slice, including both Amendment
bullets: the ``SUR-1428``/``OPS-1428`` short-id collision, ``match_result``'s
legacy short-form fallback, staging only the current run's specs, and
``tests/<TICKET>/`` placement.

Nothing here touches the network or spawns node: ``ensure_deps`` is stubbed and
``automation_gate.list_ok_in_project``'s subprocess is either stubbed or asserted
to fail open.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import automation_gate
from app.services import automation_project_service as aps

pytestmark = pytest.mark.usefixtures("workspace_dir")

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

CANNED_SPEC = """```typescript
import { test, expect } from '@q-agent/playwright-base';
import { LoginPage } from '../../pages/LoginPage';

test('Login works', async ({ page }) => {
  const login = new LoginPage(page);
  await login.open();
  await expect(page).toHaveTitle(/Login/);
});
```"""


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

_run_counter = 0


def _seed_project_run(db_session, tickets=(("SUR-1428", "TC-01"),)):
    """A run whose cases resolve to a real project key, so generation is project-backed.

    Returns ``(run, [cases])``. Every ticket gets a Ticket + RunTicket row so
    ``spec_service.build_case_context`` resolves ``projectKey``.
    """
    global _run_counter
    _run_counter += 1
    from app.models.project_config import ProjectConfig
    from app.models.provider import Provider
    from app.models.run import Run, RunTicket
    from app.models.testcase import TestCase
    from app.models.ticket import Ticket

    if db_session.query(Provider).first() is None:
        db_session.add(
            Provider(kind="ado", name="ADO", connected=True,
                     config={"project": "Surency Platform"}, secrets={})
        )
        db_session.add(
            ProjectConfig(key="Surency Platform", name="Surency Platform",
                          base_url="https://app.test")
        )
    run = Run(code=f"RUN-P{_run_counter}", name="Project run", status="review")
    db_session.add(run)
    db_session.flush()

    cases = []
    for position, (ticket_id, case_code) in enumerate(tickets):
        if db_session.query(Ticket).filter(Ticket.external_id == ticket_id).first() is None:
            db_session.add(Ticket(external_id=ticket_id, provider_kind="ado", title="Login"))
        db_session.add(RunTicket(run_id=run.id, ticket_external_id=ticket_id, position=position))
        case = TestCase(
            run_id=run.id,
            ticket_external_id=ticket_id,
            code=case_code,
            title="Login works",
            precondition="User is on the login page",
            steps=[{"a": "Enter valid credentials", "e": "User is logged in"}],
            approval="approved",
            automation="Playwright",
        )
        db_session.add(case)
        cases.append(case)
    db_session.commit()
    db_session.refresh(run)
    for case in cases:
        db_session.refresh(case)
    return run, cases


@pytest.fixture
def project_generation(monkeypatch):
    """Stub the three external touchpoints of a project-backed generation pass.

    Claude (spec source), ``ensure_deps`` (npm) and the project ``--list`` gate.
    The gate stub records its calls so tests can assert *what* was gated, and can
    be swapped for a rejecting one.
    """
    from app.routers import automation as automation_router
    from app.services import claude_cli, failure_classifier

    monkeypatch.setattr(claude_cli, "run_prompt", lambda *a, **k: CANNED_SPEC)
    monkeypatch.setattr(automation_router, "_run_automation_review", lambda *a, **k: None)
    monkeypatch.setattr(aps, "ensure_deps", lambda *a, **k: "cached")
    monkeypatch.setattr(
        failure_classifier, "classify_failure",
        lambda *a, **k: {"failureClass": "test_defect", "suspectedProductDefect": False, "reason": ""},
    )
    calls: list[dict] = []

    def fake_gate(project_dir, expect_titles):
        calls.append(
            {
                "dir": Path(project_dir),
                "titles": list(expect_titles),
                # Snapshot of the specs on disk AT GATE TIME — proves the
                # candidate is written before it is gated.
                "specs": sorted(
                    p.relative_to(project_dir).as_posix()
                    for p in Path(project_dir).rglob("*.spec.ts")
                ),
            }
        )
        return True, "collected cleanly"

    monkeypatch.setattr(automation_gate, "list_ok_in_project", fake_gate)
    # The second static gate (#546) is stubbed here too, so these tests stay
    # hermetic: `settings.playwright_node_modules` may well hold a real `tsc` on a
    # dev machine, and shelling out to it per generation would make the suite slow
    # and machine-dependent. `tsc --noEmit` gets its own dedicated coverage in
    # `test_automation_gate.py`.
    monkeypatch.setattr(automation_gate, "typecheck_ok", lambda _dir: (True, "stubbed"))
    return SimpleNamespace(calls=calls, monkeypatch=monkeypatch)


def _generate(db_session, run, case):
    from app.routers import automation as automation_router

    spec = automation_router._generate_one(db_session, run, case)
    db_session.commit()
    db_session.refresh(spec)
    return spec


# ---------------------------------------------------------------------------
# spec_filename / match_result — the Amendment
# ---------------------------------------------------------------------------


def test_match_result_prefers_the_full_ticket_form():
    """SUR-1428 and OPS-1428 both shorten to '1428' — full form must win."""
    from app.services import execution_service

    sur = SimpleNamespace(ticket_external_id="SUR-1428", case_code="TC-01")
    ops = SimpleNamespace(ticket_external_id="OPS-1428", case_code="TC-01")
    results = [sur, ops]

    assert execution_service.match_result(results, "SUR-1428-TC-01.spec.ts") is sur
    assert execution_service.match_result(results, "OPS-1428-TC-01.spec.ts") is ops
    # Nested project-relative paths are basenamed, so attribution is unaffected.
    assert (
        execution_service.match_result(results, "tests/OPS-1428/OPS-1428-TC-01.spec.ts") is ops
    )
    # The full-form pass runs over EVERY row before the legacy pass, so the
    # ambiguous short form can never steal a filename that has an exact owner.
    assert execution_service.match_result(list(reversed(results)), "SUR-1428-TC-01.spec.ts") is sur


def test_match_result_still_matches_legacy_short_form():
    """In-flight runs generated before #540 keep matching."""
    from app.services import execution_service

    legacy = SimpleNamespace(ticket_external_id="SUR-1502", case_code="TC-03")
    assert execution_service.match_result([legacy], "1502-TC-03.spec.ts") is legacy
    assert execution_service.match_result([legacy], "nope-TC-03.spec.ts") is None


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_test_titles_extracts_only_real_tests():
    code = (
        "import { test, expect } from '@q-agent/playwright-base';\n"
        "test.describe('a group', () => {\n"
        "  test('Login works', async ({ page }) => {});\n"
        "  test.only(\"Logout works\", async ({ page }) => {});\n"
        "  test(`Interpolated ${x}`, async () => {});\n"
        "});\n"
    )
    titles = automation_gate.test_titles(code)
    assert titles == ["Login works", "Logout works"]
    # A describe name is a group, not proof a test exists.
    assert "a group" not in titles
    assert automation_gate.test_titles("") == []


def test_list_ok_in_project_fails_open_without_a_binary(tmp_path, monkeypatch):
    """The fail-open policy of the legacy gate is preserved verbatim."""
    monkeypatch.setattr(automation_gate, "resolve_list_bin", lambda _dir: None)
    ok, detail = automation_gate.list_ok_in_project(tmp_path, ["Login works"])
    assert ok is True
    assert detail.startswith("skipped")


def test_list_ok_in_project_rejects_a_failed_collection(tmp_path, monkeypatch):
    """rc != 0 is a real rejection — this is what a broken page object trips."""
    monkeypatch.setattr(automation_gate, "resolve_list_bin", lambda _dir: "playwright")
    monkeypatch.setattr(
        automation_gate.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="Cannot find module '../pages/Nope'"),
    )
    ok, detail = automation_gate.list_ok_in_project(tmp_path, [])
    assert ok is False
    assert "Cannot find module" in detail


def test_list_ok_in_project_requires_the_candidate_titles(tmp_path, monkeypatch):
    """"Collected, but contains no test" is a rejection too."""
    monkeypatch.setattr(automation_gate, "resolve_list_bin", lambda _dir: "playwright")
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["cwd"] = kwargs.get("cwd")
        return SimpleNamespace(returncode=0, stdout="  tests/x.spec.ts:3:1 › Other test\n", stderr="")

    monkeypatch.setattr(automation_gate.subprocess, "run", fake_run)

    ok, detail = automation_gate.list_ok_in_project(tmp_path, ["Login works"])
    assert ok is False
    assert "Login works" in detail
    # It lists the PROJECT (not a temp dir) and forces the list reporter, because
    # the scaffolded project config's JSON reporter would print nothing.
    assert captured["cwd"] == str(tmp_path)
    assert "--list" in captured["cmd"] and "--reporter=list" in captured["cmd"]

    ok, _detail = automation_gate.list_ok_in_project(tmp_path, ["Other test"])
    assert ok is True


def test_the_project_gate_resolves_imports_the_legacy_gate_cannot(
    db_session, project_generation, monkeypatch
):
    """The layered-import blocker dissolves *by construction*, not by weakening.

    There is no local Playwright install in CI, so this asserts the property that
    makes the gate work rather than shelling out: at gate time the candidate sits
    in a tree where ``../../pages/LoginPage`` and ``@q-agent/playwright-base``
    genuinely exist and NODE_PATH points at them — while the legacy gate collects
    the very same code alone in a directory containing neither, which is exactly
    why it rejects it today.
    """
    from app.models.automation_project import AutomationProject
    from app.services import spec_service

    run, (case,) = _seed_project_run(db_session)
    # Restore the real gate; stub only the subprocess so cwd/env are computed for real.
    project_generation.monkeypatch.undo()
    from app.services import claude_cli, failure_classifier

    monkeypatch.setattr(claude_cli, "run_prompt", lambda *a, **k: CANNED_SPEC)
    monkeypatch.setattr(aps, "ensure_deps", lambda *a, **k: "cached")
    monkeypatch.setattr(
        failure_classifier, "classify_failure",
        lambda *a, **k: {"failureClass": "test_defect", "suspectedProductDefect": False, "reason": ""},
    )
    from app.routers import automation as automation_router

    monkeypatch.setattr(automation_router, "_run_automation_review", lambda *a, **k: None)
    # The undo() above also dropped the fixture's `typecheck_ok` stub; re-apply it so
    # this test stays about `--list` cwd/NODE_PATH and never shells out to a real tsc.
    monkeypatch.setattr(automation_gate, "typecheck_ok", lambda _dir: (True, "stubbed"))

    project = aps.ensure_project(db_session, run.owner_id, "Surency Platform", "")
    root = aps.project_dir(project)
    (root / "pages" / "LoginPage.ts").write_text(
        "export class LoginPage { async open() {} }\n", encoding="utf-8"
    )
    base = root / "node_modules" / "@q-agent" / "playwright-base"
    base.mkdir(parents=True)
    (base / "package.json").write_text('{"name":"@q-agent/playwright-base"}', encoding="utf-8")

    seen: dict = {}
    real_run = automation_gate.subprocess.run

    def fake_run(cmd, **kwargs):
        # `subprocess` is one module object app-wide, so delegate anything that
        # isn't the gate's own invocation (git, notably) to the real thing.
        if "--list" not in cmd:
            return real_run(cmd, **kwargs)
        seen["cwd"] = Path(kwargs["cwd"])
        seen["node_path"] = kwargs["env"]["NODE_PATH"]
        return SimpleNamespace(returncode=0, stdout="  x.spec.ts:1:1 > Login works\n", stderr="")

    monkeypatch.setattr(automation_gate, "resolve_list_bin", lambda _dir: "playwright")
    monkeypatch.setattr(automation_gate.subprocess, "run", fake_run)

    spec = _generate(db_session, run, case)
    assert spec.status == "draft"

    # The gate collected the real project tree...
    assert seen["cwd"] == root
    assert str(root / "node_modules") in seen["node_path"]
    # ...where both of the candidate's imports genuinely exist, relative to it.
    candidate = root / spec.filename
    assert (candidate.parent / ".." / ".." / "pages" / "LoginPage.ts").resolve().is_file()
    assert (root / "node_modules" / "@q-agent" / "playwright-base").is_dir()

    # By contrast, the legacy gate collects the same code in a bare temp dir
    # holding nothing but the spec and a throwaway config — hence the rejection
    # this slice removes.
    legacy_dir: dict = {}

    def legacy_run(cmd, **kwargs):
        if "--list" not in cmd:
            return real_run(cmd, **kwargs)
        legacy_dir["contents"] = sorted(p.name for p in Path(kwargs["cwd"]).iterdir())
        return SimpleNamespace(returncode=1, stdout="", stderr="Cannot find module")

    monkeypatch.setattr(spec_service, "_resolve_list_bin", lambda: "playwright")
    monkeypatch.setattr(spec_service.subprocess, "run", legacy_run)
    assert spec_service.playwright_list_ok(spec.code, run.owner_id) is False
    assert legacy_dir["contents"] == ["_gate.spec.ts", "playwright.config.ts"]


# ---------------------------------------------------------------------------
# Generation into the project
# ---------------------------------------------------------------------------


@requires_git
def test_generation_writes_into_the_project_and_commits(db_session, project_generation):
    """A generated spec lands at <project>/tests/<TICKET>/ with a commit per pass."""
    from app.models.automation_project import AutomationFile, AutomationProject

    run, (case,) = _seed_project_run(db_session)
    spec = _generate(db_session, run, case)

    assert spec.status == "draft"
    assert spec.project_id is not None
    project = db_session.get(AutomationProject, spec.project_id)
    root = aps.project_dir(project)

    expected = root / "tests" / "SUR-1428" / "SUR-1428-TC-01.spec.ts"
    assert expected.is_file()
    assert "LoginPage" in expected.read_text(encoding="utf-8")
    # The row records the PROJECT-RELATIVE path, so execution staging and the UI
    # both know where the spec lives in the tree.
    assert spec.filename == "tests/SUR-1428/SUR-1428-TC-01.spec.ts"
    assert spec.path == str(expected)

    # The candidate was on disk before the gate ran (the gate lists disk).
    assert project_generation.calls[-1]["dir"] == root
    assert project_generation.calls[-1]["titles"] == ["Login works"]
    assert "tests/SUR-1428/SUR-1428-TC-01.spec.ts" in project_generation.calls[-1]["specs"]

    # sync_files_to_db mirrored the tree, which is what _spec_out serves the UI.
    paths = {
        row.path
        for row in db_session.query(AutomationFile).filter(
            AutomationFile.project_id == project.id
        )
    }
    assert "tests/SUR-1428/SUR-1428-TC-01.spec.ts" in paths

    # One commit per generation pass, on top of the pre-state commit.
    log = _git_log(root)
    assert any("spec for TC-01" in line for line in log)


@requires_git
def test_two_tickets_with_the_same_short_id_coexist(db_session, project_generation):
    """Amendment: SUR-1428/TC-01 and OPS-1428/TC-01 must not overwrite each other."""
    from app.models.automation_project import AutomationProject

    run, (sur_case, ops_case) = _seed_project_run(
        db_session, tickets=(("SUR-1428", "TC-01"), ("OPS-1428", "TC-01"))
    )
    sur_spec = _generate(db_session, run, sur_case)
    ops_spec = _generate(db_session, run, ops_case)

    assert sur_spec.project_id == ops_spec.project_id  # same project
    root = aps.project_dir(db_session.get(AutomationProject, sur_spec.project_id))
    assert (root / "tests" / "SUR-1428" / "SUR-1428-TC-01.spec.ts").is_file()
    assert (root / "tests" / "OPS-1428" / "OPS-1428-TC-01.spec.ts").is_file()
    assert sur_spec.filename != ops_spec.filename

    # ...and each result is attributed to the correct row.
    from app.services import execution_service

    rows = [
        SimpleNamespace(ticket_external_id="SUR-1428", case_code="TC-01"),
        SimpleNamespace(ticket_external_id="OPS-1428", case_code="TC-01"),
    ]
    assert execution_service.match_result(rows, sur_spec.filename) is rows[0]
    assert execution_service.match_result(rows, ops_spec.filename) is rows[1]


@requires_git
def test_rejected_generation_resets_the_tree_and_keeps_previous_specs(
    db_session, project_generation
):
    """A rejected candidate rolls the WHOLE project back — the extended
    has_previous_good contract."""
    from app.models.automation_project import AutomationProject

    run, (first, second) = _seed_project_run(
        db_session, tickets=(("SUR-1428", "TC-01"), ("SUR-1428", "TC-02"))
    )
    good = _generate(db_session, run, first)
    project = db_session.get(AutomationProject, good.project_id)
    root = aps.project_dir(project)
    good_path = root / "tests" / "SUR-1428" / "SUR-1428-TC-01.spec.ts"
    assert good_path.is_file()

    # Now make the project gate reject — as a deliberately broken page object
    # breaking another case's spec would.
    project_generation.monkeypatch.setattr(
        automation_gate, "list_ok_in_project",
        lambda *a, **k: (False, "playwright --list failed (rc=1): Cannot find module"),
    )
    rejected = _generate(db_session, run, second)

    assert rejected.status == "blocked"
    assert "Rejected:" in rejected.block_reason
    # The candidate is gone (reset --hard + clean), the previously-good spec intact.
    assert not (root / "tests" / "SUR-1428" / "SUR-1428-TC-02.spec.ts").exists()
    assert good_path.is_file()
    assert db_session.get(type(good), good.id).code == good.code


@requires_git
def test_a_legacy_spec_keeps_the_legacy_path(db_session, project_generation, monkeypatch):
    """project_id IS NULL specs generate, gate and write exactly as before #540."""
    from app.models.testcase import AutomationSpec
    from app.services import spec_service
    from app.services.workspace_scope import scoped_specs_dir

    run, (case,) = _seed_project_run(db_session)
    # A pre-#540 row: bound to no project, with code.
    db_session.add(
        AutomationSpec(
            test_case_id=case.id, filename="1428-TC-01.spec.ts", code="// old", status="draft"
        )
    )
    db_session.commit()
    monkeypatch.setattr(spec_service, "playwright_list_ok", lambda *a, **k: True)

    spec = _generate(db_session, run, case)

    assert spec.project_id is None
    # The legacy per-run dir, and the legacy single-spec gate — not the project one.
    assert (scoped_specs_dir(run.owner_id) / run.code / "SUR-1428-TC-01.spec.ts").is_file()
    assert project_generation.calls == []


def _git_log(root: Path) -> list[str]:
    import subprocess

    proc = subprocess.run(
        ["git", "-C", str(root), "log", "--pretty=%s"],
        capture_output=True, text=True, timeout=30,
    )
    return (proc.stdout or "").splitlines()


# ---------------------------------------------------------------------------
# Execution staging
# ---------------------------------------------------------------------------


@requires_git
def test_staging_copies_the_library_but_only_this_runs_specs(db_session, project_generation):
    """A project holding 3 tickets' specs stages only the current run's."""
    from app.models.automation_project import AutomationProject
    from app.services import playwright_runner

    run, (case,) = _seed_project_run(db_session)
    spec = _generate(db_session, run, case)
    project = db_session.get(AutomationProject, spec.project_id)
    root = aps.project_dir(project)

    # Two other tickets' specs already live in the project, plus a page object.
    for other in ("SUR-1502", "OPS-1433"):
        path = root / "tests" / other / f"{other}-TC-01.spec.ts"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// other ticket\n", encoding="utf-8")
    (root / "pages" / "LoginPage.ts").write_text(
        "export class LoginPage { async open() {} }\n", encoding="utf-8"
    )

    staged = playwright_runner._stage_specs_for_run(
        db_session, run, [(case.id, case.ticket_external_id, case.code)]
    )

    # The WHOLE library, so imports resolve...
    assert (staged / "pages" / "LoginPage.ts").is_file()
    # ...but only this run's specs, or every test ever generated would re-run.
    assert (staged / "tests" / "SUR-1428" / "SUR-1428-TC-01.spec.ts").is_file()
    assert not (staged / "tests" / "SUR-1502").exists()
    assert not (staged / "tests" / "OPS-1433").exists()


@requires_git
def test_two_concurrent_runs_get_separate_staged_dirs(db_session, project_generation):
    """Two runs on the same project must not fight over config/report/imports."""
    from app.services import playwright_runner

    run_a, (case_a,) = _seed_project_run(db_session)
    run_b, (case_b,) = _seed_project_run(db_session, tickets=(("SUR-1502", "TC-01"),))
    spec_a = _generate(db_session, run_a, case_a)
    spec_b = _generate(db_session, run_b, case_b)
    assert spec_a.project_id == spec_b.project_id

    staged_a = playwright_runner._stage_specs_for_run(
        db_session, run_a, [(case_a.id, case_a.ticket_external_id, case_a.code)]
    )
    staged_b = playwright_runner._stage_specs_for_run(
        db_session, run_b, [(case_b.id, case_b.ticket_external_id, case_b.code)]
    )
    assert staged_a != staged_b
    assert (staged_a / "tests" / "SUR-1428").is_dir()
    assert not (staged_a / "tests" / "SUR-1502").exists()
    assert (staged_b / "tests" / "SUR-1502").is_dir()
    assert not (staged_b / "tests" / "SUR-1428").exists()


@requires_git
def test_staging_materializes_a_blocked_spec_from_the_row(db_session, project_generation):
    """"Run this blocked spec anyway" still works for a project-backed spec, whose
    blocked code is deliberately never committed to the project."""
    from app.services import playwright_runner

    run, (case,) = _seed_project_run(db_session)
    project_generation.monkeypatch.setattr(
        automation_gate, "list_ok_in_project", lambda *a, **k: (False, "nope")
    )
    spec = _generate(db_session, run, case)
    assert spec.status == "blocked" and spec.project_id is not None

    staged = playwright_runner._stage_specs_for_run(
        db_session, run, [(case.id, case.ticket_external_id, case.code)]
    )
    materialized = staged / spec.filename
    assert materialized.is_file()
    assert "LoginPage" in materialized.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Depth-aware fixtures injection
# ---------------------------------------------------------------------------


def test_fixtures_specifier_is_depth_aware():
    from app.services.playwright_runner import fixtures_specifier

    assert fixtures_specifier(Path("1428-TC-01.spec.ts")) == "./fixtures"
    assert fixtures_specifier(Path("tests/SUR-1428/x.spec.ts")) == "../../fixtures"
    assert fixtures_specifier(Path("pages/LoginPage.ts")) == "../fixtures"


def test_apply_fixtures_rewrites_nested_files_and_spares_the_config(tmp_path):
    from app.services import playwright_runner as runner

    flat = tmp_path / "1428-TC-01.spec.ts"
    flat.write_text("import { test } from '@playwright/test';\n", encoding="utf-8")
    nested = tmp_path / "tests" / "SUR-1428" / "SUR-1428-TC-01.spec.ts"
    nested.parent.mkdir(parents=True)
    nested.write_text('import { test } from "@playwright/test";\n', encoding="utf-8")
    page = tmp_path / "pages" / "LoginPage.ts"
    page.parent.mkdir(parents=True)
    page.write_text("import { Page } from '@playwright/test';\n", encoding="utf-8")
    config = tmp_path / "playwright.config.ts"
    config.write_text("import { defineConfig } from '@playwright/test';\n", encoding="utf-8")
    based = tmp_path / "tests" / "SUR-1502" / "SUR-1502-TC-01.spec.ts"
    based.parent.mkdir(parents=True)
    based.write_text("import { test } from '@q-agent/playwright-base';\n", encoding="utf-8")

    runner._apply_fixtures(tmp_path, tmp_path / "sessionStorage.json", replay_session=False)

    assert "'./fixtures'" in flat.read_text(encoding="utf-8")
    assert '"../../fixtures"' in nested.read_text(encoding="utf-8")
    assert "'../fixtures'" in page.read_text(encoding="utf-8")
    # The config must keep importing the real package — defineConfig lives only there.
    assert "'@playwright/test'" in config.read_text(encoding="utf-8")
    # The base package already ships an extended `test`; leave its imports alone.
    assert "'@q-agent/playwright-base'" in based.read_text(encoding="utf-8")
    assert (tmp_path / "fixtures.ts").is_file()

    # Idempotent: a second pass (the heal loop calls it per attempt) must not
    # rewrite the generated fixtures.ts itself.
    runner._apply_fixtures(tmp_path, tmp_path / "sessionStorage.json", replay_session=True)
    assert "'@playwright/test'" in (tmp_path / "fixtures.ts").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The read-only project tree on the wire (for #543)
# ---------------------------------------------------------------------------


@requires_git
def test_spec_out_exposes_the_project_files(db_session, project_generation):
    from app.routers import automation as automation_router

    run, (case,) = _seed_project_run(db_session)
    spec = _generate(db_session, run, case)

    out = automation_router._spec_out(spec)
    assert out["projectId"] == spec.project_id
    tree = {entry["path"]: entry for entry in out["projectFiles"]}
    # The case's OWN spec is part of the tree, at its real project-relative path.
    assert "tests/SUR-1428/SUR-1428-TC-01.spec.ts" in tree
    assert tree["tests/SUR-1428/SUR-1428-TC-01.spec.ts"]["kind"] == "spec"
    assert "LoginPage" in tree["tests/SUR-1428/SUR-1428-TC-01.spec.ts"]["code"]
    # Every kind is one of the seven the UI (#543) buckets; anything else would
    # land in its uninformative "Other" group.
    from app.models.automation_project import FILE_KINDS

    assert {entry["kind"] for entry in out["projectFiles"]} <= set(FILE_KINDS)

    # A shared cache means a list endpoint builds each project's tree once.
    cache: dict[int, list[dict]] = {}
    automation_router._spec_out(spec, cache)
    assert list(cache) == [spec.project_id]


def test_spec_out_omits_project_files_for_a_legacy_spec(db_session):
    """Legacy specs must render exactly as before — no empty panel, no `[]`."""
    from app.models.run import Run
    from app.models.testcase import AutomationSpec, TestCase
    from app.routers import automation as automation_router

    run = Run(code="RUN-LEGACY", name="Legacy", status="review")
    db_session.add(run)
    db_session.flush()
    case = TestCase(run_id=run.id, ticket_external_id="SUR-1", code="TC-01", title="x",
                    approval="approved", automation="Playwright")
    db_session.add(case)
    db_session.flush()
    spec = AutomationSpec(test_case_id=case.id, filename="1-TC-01.spec.ts", code="// x")
    db_session.add(spec)
    db_session.commit()

    out = automation_router._spec_out(spec)
    assert out["projectId"] is None
    assert "projectFiles" not in out
