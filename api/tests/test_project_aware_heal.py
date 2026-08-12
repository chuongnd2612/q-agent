"""Project-aware self-heal: the heal loop may repair page objects (#547).

Every test here drives a **fake library healer** that performs real writes in a
real project tree, so the defences are exercised against actual disk state rather
than a mocked verdict. Nothing spawns node or touches the network:
``claude_cli.run_agentic`` (the healer) and ``_invoke_playwright`` (the runner) are
replaced per test, and the two static gates are stubbed by default — a dev machine
may have a real ``tsc``, and shelling out to it would make the suite slow and
machine-dependent (``automation_gate`` has its own coverage).

``heal_spec`` is invoked **directly**, not through ``POST /cases/{id}/spec/heal``:
the endpoint routes to whichever execution target the workspace settings name, and
this slice is about what the in-process loop does once it is running.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services import (
    automation_gate,
    automation_project_service as aps,
    claude_cli,
    failure_classifier,
    heal_service,
    page_object_healer_service as healer,
    playwright_runner,
    spec_service,
)

pytestmark = pytest.mark.usefixtures("workspace_dir")

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

SPEC_RELATIVE = "tests/SUR-1428/SUR-1428-TC-01.spec.ts"

# The layered shape #542 introduced: the spec is a sequence of business steps and
# the locator lives in the page object. `#stale-user` is the defect every heal test
# below is about.
LOGIN_PAGE = """import type { Page } from '@playwright/test';
import { expectVisible } from '@q-agent/playwright-base';

export class LoginPage {
  constructor(private readonly page: Page) {}

  async open() {
    await this.page.goto('/login');
  }

  async expectLoaded() {
    await expectVisible(this.page.getByTestId('login-form'));
  }

  async signIn(user: string) {
    await this.page.locator('#stale-user').fill(user);
  }
}
"""

LAYERED_SPEC = """import { test, expect } from '@q-agent/playwright-base';
import { LoginPage } from '../../pages/LoginPage';

test('SUR-1428-TC-01 — Login works', async ({ page }) => {
  const login = new LoginPage(page);
  await login.open();
  await login.signIn('ana');
  await expect(page).toHaveTitle(/Home/);
});
"""


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_heal_state():
    playwright_runner._healing.clear()
    yield
    playwright_runner._healing.clear()


@pytest.fixture
def gates(monkeypatch):
    """Both static gates stubbed to pass; tests flip the one they are about."""
    monkeypatch.setattr(automation_gate, "list_ok_in_project", lambda *a, **k: (True, "stubbed"))
    monkeypatch.setattr(automation_gate, "typecheck_ok", lambda *a, **k: (True, "stubbed"))
    return monkeypatch


@pytest.fixture(autouse=True)
def _test_defect_by_default(monkeypatch):
    """The classifier is a Claude call; default it to "the test is wrong". The
    product-defect test overrides it."""
    monkeypatch.setattr(
        failure_classifier,
        "classify_failure",
        lambda *a, **k: {
            "failureClass": "test_defect",
            "suspectedProductDefect": False,
            "reason": "stub",
        },
    )


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _project(db_session):
    return aps.ensure_project(db_session, None, "Surency Platform", "")


def _git_log(root: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "-C", str(root), "log", "--pretty=%s"], capture_output=True, text=True, timeout=30
    )
    return (proc.stdout or "").splitlines()


_run_counter = 0


def _seed(db_session, *, project=None, spec_code=LAYERED_SPEC, page_object=LOGIN_PAGE):
    """A run + case + project-backed AutomationSpec whose spec imports a page object.

    Returns ``(run, case, spec, project)``. ``project=None`` seeds a project;
    pass ``project=False`` for a **legacy** spec (``project_id IS NULL``), which is
    how the "the project-aware path is inert for legacy" tests are written.
    """
    global _run_counter
    _run_counter += 1
    from app.models.run import Run
    from app.models.testcase import AutomationSpec, TestCase

    run = Run(code=f"RUN-H{_run_counter}", name="Heal run", status="executing")
    db_session.add(run)
    db_session.flush()
    case = TestCase(
        run_id=run.id,
        ticket_external_id="SUR-1428",
        code="TC-01",
        title="Login works",
        precondition="User is on the login page",
        steps=[{"a": "Sign in", "e": "Home is shown"}],
        approval="approved",
        automation="Playwright",
    )
    db_session.add(case)
    db_session.flush()

    legacy = project is False
    if not legacy and project is None:
        project = _project(db_session)
    if not legacy:
        root = aps.project_dir(project)
        if page_object is not None:
            _write(root, "pages/LoginPage.ts", page_object)
        _write(root, SPEC_RELATIVE, spec_code)
        aps.git_commit(project, "chore: seed")

    spec = AutomationSpec(
        test_case_id=case.id,
        code=spec_code,
        path="",
        filename="" if legacy else SPEC_RELATIVE,
        project_id=None if legacy else project.id,
        status="failed",
    )
    db_session.add(spec)
    db_session.commit()
    return run, case, spec, (None if legacy else project)


def _seed_result(db_session, run, case):
    from app.models.execution import Execution, ExecutionResult

    execution = Execution(run_id=run.id, env="dev", browser="chromium", workers=1, total=1)
    db_session.add(execution)
    db_session.flush()
    result = ExecutionResult(
        execution_id=execution.id,
        test_case_id=case.id,
        ticket_external_id=case.ticket_external_id,
        case_code=case.code,
        status="fail",
        error_message="locator not found",
    )
    db_session.add(result)
    db_session.commit()
    return result


def _runner(monkeypatch, statuses: list[str]) -> dict:
    """Fake Playwright: attempt N reports ``statuses[N-1]`` for the spec."""
    calls = {"n": 0}

    def fake_invoke(spec_dir, workers, timeout_s, spec_file="", run_id=None):
        calls["n"] += 1
        status = statuses[min(calls["n"], len(statuses)) - 1]
        entry: dict = {"status": status, "duration": 10, "attachments": []}
        if status != "passed":
            entry["error"] = {"message": "locator '#stale-user' not found"}
        report = {
            "suites": [
                {
                    "file": Path(SPEC_RELATIVE).name,
                    "specs": [
                        {
                            "title": "SUR-1428-TC-01 — Login works",
                            "file": Path(SPEC_RELATIVE).name,
                            "tests": [{"results": [entry]}],
                        }
                    ],
                    "suites": [],
                }
            ]
        }
        (spec_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
        return (0 if status == "passed" else 1), status, ""

    monkeypatch.setattr(playwright_runner, "_invoke_playwright", fake_invoke)
    return calls


def _healer_editor(monkeypatch, writer):
    """Install ``writer(root)`` as the agentic library healer; returns its calls."""
    calls: list[dict] = []

    def fake_run_agentic(prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        writer(Path(kwargs["workspace_dir"]))
        return "repaired the locator"

    monkeypatch.setattr(claude_cli, "run_agentic", fake_run_agentic)
    return calls


def _no_spec_fixer(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the spec fixer must not run — the library was repaired")

    monkeypatch.setattr(spec_service, "generate_fixed_spec_code", boom)


def _fix_locator(root: Path) -> None:
    """A correct library repair: the stale locator, fixed in place, body rewritten."""
    path = root / "pages" / "LoginPage.ts"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "this.page.locator('#stale-user')", "this.page.getByTestId('user')"
        ),
        encoding="utf-8",
    )


def _reload_spec(case_id):
    from app.db import SessionLocal
    from app.models.testcase import AutomationSpec

    db = SessionLocal()
    try:
        spec = db.query(AutomationSpec).filter(AutomationSpec.test_case_id == case_id).first()
        db.refresh(spec)
        return spec.code, spec.status, json.loads(spec.heal_report or "{}"), spec.path
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Import resolution — what the heal is allowed to blame, and to count
# ---------------------------------------------------------------------------


def test_imported_library_paths_is_transitive_and_refuses_tests_and_escapes(db_session, tmp_path):
    root = tmp_path / "proj"
    _write(root, "pages/LoginPage.ts", "import { Header } from '../components/Header';\n")
    _write(root, "components/Header.ts", "export class Header {}\n")
    _write(root, "tests/SUR-1428/other.spec.ts", "export const x = 1;\n")
    code = (
        "import { LoginPage } from '../../pages/LoginPage';\n"
        "import { other } from './other.spec';\n"          # a spec: never library
        "import { nope } from '../../../escape/Thing';\n"  # escapes the root
        "import { test } from '@q-agent/playwright-base';\n"  # a package
        "import { missing } from '../../pages/Missing';\n"  # not on disk
    )
    assert healer.imported_library_paths(root, SPEC_RELATIVE, code) == [
        "components/Header.ts",  # reached TRANSITIVELY through LoginPage
        "pages/LoginPage.ts",
    ]


def test_a_legacy_spec_imports_no_library_files(tmp_path):
    """`root=None` is the legacy spec, and it must resolve to nothing — that is what
    keeps every project-aware branch inert for one."""
    assert healer.imported_library_paths(None, "x.spec.ts", LAYERED_SPEC) == []


# ---------------------------------------------------------------------------
# THE crux: the anti-cheat count spans the spec + its imported page objects
# ---------------------------------------------------------------------------


def test_assertion_scope_spans_the_imported_page_objects(db_session, tmp_path):
    from app.services import placeholder_gate

    root = tmp_path / "proj"
    _write(root, "pages/LoginPage.ts", LOGIN_PAGE)
    _write(root, "components/Header.ts", "export class Header {\n  async ok() {\n    await expectVisible(this.x);\n  }\n}\n")
    spec_only = placeholder_gate.count_assertions(LAYERED_SPEC)
    spanned = healer.assertion_scope_count(root, SPEC_RELATIVE, LAYERED_SPEC)
    # The page object's `expectVisible` import counts via #542's `\bexpect[A-Z]\w*\(`
    # widening — inherited from placeholder_gate, not re-implemented here.
    assert spanned > spec_only
    assert spanned == spec_only + placeholder_gate.count_assertions(LOGIN_PAGE)
    # A legacy spec is EXACTLY the old single-file count.
    assert healer.assertion_scope_count(None, SPEC_RELATIVE, LAYERED_SPEC) == spec_only


def test_moving_an_assertion_into_a_page_object_does_not_trip_the_anti_cheat(tmp_path):
    """#547's hard blocker. Doc §14 puts page-level UI assertions in the page object,
    so a legitimate heal MOVES one out of the spec — the assertion leaves the spec at
    the same time as it arrives in the page object, and the total is what is compared.
    The old single-file count saw only the departure and rejected it."""
    root = tmp_path / "proj"
    bare = "import type { Page } from '@playwright/test';\nexport class LoginPage {\n}\n"
    _write(root, "pages/LoginPage.ts", bare)
    spec_before = (
        "import { LoginPage } from '../../pages/LoginPage';\n"
        "test('x', async ({ page }) => {\n"
        "  await expect(page).toHaveTitle(/Home/);\n"
        "});\n"
    )
    before = healer.assertion_scope_count(root, SPEC_RELATIVE, spec_before)

    # The move: the healer adds the page-level assertion to the page object...
    _write(
        root, "pages/LoginPage.ts",
        "import type { Page } from '@playwright/test';\nexport class LoginPage {\n"
        "  async expectHome() {\n    await expect(this.page).toHaveTitle(/Home/);\n  }\n}\n",
    )
    # ...and the spec calls it instead of asserting inline.
    spec_after = (
        "import { LoginPage } from '../../pages/LoginPage';\n"
        "test('x', async ({ page }) => {\n"
        "  await new LoginPage(page).expectHome();\n"
        "});\n"
    )
    assert healer.assertion_scope_count(root, SPEC_RELATIVE, spec_after) >= before

    # ...and this is exactly the shape the OLD one-file count rejected.
    from app.services import placeholder_gate

    assert placeholder_gate.count_assertions(spec_after) < placeholder_gate.count_assertions(
        spec_before
    )


def test_genuinely_removing_an_assertion_still_trips_the_anti_cheat(tmp_path):
    """The whole point of the anti-cheat: widened scope must not mean weakened."""
    root = tmp_path / "proj"
    _write(root, "pages/LoginPage.ts", LOGIN_PAGE)
    stripped = LAYERED_SPEC.replace("  await expect(page).toHaveTitle(/Home/);\n", "")
    assert healer.assertion_scope_count(root, SPEC_RELATIVE, stripped) < healer.assertion_scope_count(
        root, SPEC_RELATIVE, LAYERED_SPEC
    )


@requires_git
def test_the_heal_loop_rejects_a_spec_fix_that_really_removes_an_assertion(
    db_session, gates, monkeypatch
):
    """End-to-end through ``heal_spec``: the widened count did not weaken the gate."""
    run, case, spec, project = _seed(db_session)
    _seed_result(db_session, run, case)
    _runner(monkeypatch, ["failed"])
    monkeypatch.setattr(claude_cli, "run_agentic", lambda *a, **k: "nothing to change")
    stripped = LAYERED_SPEC.replace("  await expect(page).toHaveTitle(/Home/);\n", "")
    monkeypatch.setattr(spec_service, "generate_fixed_spec_code", lambda *a, **k: stripped)

    playwright_runner.heal_spec(case.id)

    code, status, report, _path = _reload_spec(case.id)
    assert status == "failed"
    assert code == LAYERED_SPEC, "the weakened fix must not be persisted"
    assert report["attempts"][0]["rejected"] == "assertion-weakening"


@requires_git
def test_a_library_gain_is_headroom_for_a_move_only_up_to_what_it_gained(
    db_session, gates, monkeypatch
):
    """The move allowance must be exactly the size of the gain — otherwise "repair the
    page object" becomes a licence to gut the spec. Here the library gains nothing and
    the spec sheds an assertion: rejected, even though a library heal did run."""
    run, case, spec, project = _seed(db_session)
    _seed_result(db_session, run, case)
    _runner(monkeypatch, ["failed", "failed"])
    _healer_editor(monkeypatch, _fix_locator)  # a locator fix — no assertion gained
    stripped = LAYERED_SPEC.replace("  await expect(page).toHaveTitle(/Home/);\n", "")
    monkeypatch.setattr(spec_service, "generate_fixed_spec_code", lambda *a, **k: stripped)

    playwright_runner.heal_spec(case.id)

    code, status, report, _p = _reload_spec(case.id)
    assert status == "failed" and code == LAYERED_SPEC
    assert report["attempts"][1]["rejected"] == "assertion-weakening"


@requires_git
def test_the_heal_loop_accepts_a_spec_fix_that_moves_an_assertion_into_the_page_object(
    db_session, gates, monkeypatch
):
    """The counterpart, end-to-end through the real two-stage flow: attempt 1's library
    heal ADDS the page-level assertion to the page object, attempt 2's spec fix replaces
    the inline assertion with a call to it. The spec ends with fewer assertions of its
    own and is ACCEPTED, which is precisely what the one-file count made impossible."""
    run, case, spec, project = _seed(db_session)
    _seed_result(db_session, run, case)
    _runner(monkeypatch, ["failed", "failed", "passed"])

    def add_assertion_method(root: Path) -> None:
        path = root / "pages" / "LoginPage.ts"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "  async signIn(user: string) {",
                "  async expectHome() {\n    await expect(this.page).toHaveTitle(/Home/);\n  }\n\n"
                "  async signIn(user: string) {",
            ),
            encoding="utf-8",
        )

    _healer_editor(monkeypatch, add_assertion_method)
    moved = LAYERED_SPEC.replace(
        "  await expect(page).toHaveTitle(/Home/);", "  await login.expectHome();"
    )
    monkeypatch.setattr(spec_service, "generate_fixed_spec_code", lambda *a, **k: moved)

    playwright_runner.heal_spec(case.id)

    code, status, report, _path = _reload_spec(case.id)
    assert status == "passed"
    assert code == moved, "moving an assertion into the page object is a legal heal"
    assert report["attempts"][0]["libraryHealed"] == ["pages/LoginPage.ts"]
    assert "rejected" not in report["attempts"][1]


# ---------------------------------------------------------------------------
# The headline capability: heal the PAGE OBJECT, do not inline
# ---------------------------------------------------------------------------


@requires_git
def test_a_stale_locator_in_a_page_object_is_healed_in_the_page_object(
    db_session, gates, monkeypatch
):
    """The #547 AC. The spec fixer is poisoned, so the pass can only have come from
    repairing `pages/LoginPage.ts` — and the spec is byte-identical afterwards,
    i.e. nothing was inlined back into it."""
    from app.models.audit import AuditLog
    from app.models.automation_project import AutomationFile

    run, case, spec, project = _seed(db_session)
    _seed_result(db_session, run, case)
    root = aps.project_dir(project)
    _runner(monkeypatch, ["failed", "passed"])
    calls = _healer_editor(monkeypatch, _fix_locator)
    _no_spec_fixer(monkeypatch)

    playwright_runner.heal_spec(case.id)

    code, status, report, _path = _reload_spec(case.id)
    assert status == "passed"
    # 1. The page object was repaired, in place.
    text = (root / "pages" / "LoginPage.ts").read_text(encoding="utf-8")
    assert "getByTestId('user')" in text and "#stale-user" not in text
    # 2. The SPEC was not touched — no inlined locator, imports intact.
    assert code == LAYERED_SPEC
    assert "#stale-user" not in code and "getByTestId" not in code
    assert "from '../../pages/LoginPage'" in code
    # 3. Committed, mirrored to the DB, audited.
    assert any("heal library for TC-01" in line for line in _git_log(root))
    files = {row.path: row.code for row in db_session.query(AutomationFile)}
    assert "getByTestId('user')" in files["pages/LoginPage.ts"]
    rows = [r for r in db_session.query(AuditLog) if r.action == "Automation library heal"]
    assert [r.status for r in rows] == ["success"]
    assert rows[0].detail["path"] == "pages/LoginPage.ts"
    # 4. Confined, right skill, right tools, and the prompt states the boundary.
    assert Path(calls[0]["workspace_dir"]) == root
    assert calls[0]["skill"] == "page-object-healer"
    assert calls[0]["allowed_tools"] == claude_cli._PROJECT_TOOLS
    assert "Write ONLY these paths: pages/LoginPage.ts" in calls[0]["prompt"]
    assert "must NOT edit it" in calls[0]["prompt"]
    # 5. The heal report names the repaired file.
    assert report["attempts"][0]["libraryHealed"] == ["pages/LoginPage.ts"]


@requires_git
def test_the_repaired_library_is_re_staged_so_the_next_attempt_runs_it(
    db_session, gates, monkeypatch
):
    """A repair the runner never sees is not a repair: the staged per-run tree must
    carry the healed page object into the next attempt."""
    run, case, spec, project = _seed(db_session)
    _seed_result(db_session, run, case)
    seen: list[str] = []

    def fake_invoke(spec_dir, workers, timeout_s, spec_file="", run_id=None):
        seen.append((spec_dir / "pages" / "LoginPage.ts").read_text(encoding="utf-8"))
        status = "failed" if len(seen) == 1 else "passed"
        report = {"suites": [{"file": Path(SPEC_RELATIVE).name, "specs": [
            {"title": "t", "file": Path(SPEC_RELATIVE).name,
             "tests": [{"results": [{"status": status, "duration": 1, "attachments": []}]}]}], "suites": []}]}
        (spec_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
        return (0 if status == "passed" else 1), status, ""

    monkeypatch.setattr(playwright_runner, "_invoke_playwright", fake_invoke)
    _healer_editor(monkeypatch, _fix_locator)
    _no_spec_fixer(monkeypatch)

    playwright_runner.heal_spec(case.id)

    assert len(seen) == 2
    assert "#stale-user" in seen[0], "attempt 1 ran the stale library"
    assert "getByTestId('user')" in seen[1], "attempt 2 ran the REPAIRED library"


@requires_git
def test_the_library_healer_runs_at_most_once_per_heal_pass(db_session, gates, monkeypatch):
    """Cost control: one bounded agentic call per pass, then the loop falls through
    to the spec fixer rather than paying again per attempt."""
    run, case, spec, project = _seed(db_session)
    _seed_result(db_session, run, case)
    _runner(monkeypatch, ["failed"] * 10)
    calls = _healer_editor(monkeypatch, _fix_locator)
    fixes = {"n": 0}

    def fake_fix(*a, **k):
        fixes["n"] += 1
        return LAYERED_SPEC.replace("await login.open();", "await login.open(); // n")

    monkeypatch.setattr(spec_service, "generate_fixed_spec_code", fake_fix)

    playwright_runner.heal_spec(case.id)

    assert len(calls) == 1, "the library healer must not run per attempt"
    assert fixes["n"] >= 1, "and the loop must still fall through to the spec fixer"


@requires_git
def test_a_healed_page_object_selector_is_proposed_back_to_the_kb(
    db_session, gates, monkeypatch
):
    """Doc §22: the page object is where the fix is APPLIED. The KB proposal is the
    complement — otherwise the KB keeps handing the next generation the stale value."""
    proposals: list[tuple] = []
    monkeypatch.setattr(
        playwright_runner.knowledge_service,
        "propose_selector_fix",
        lambda *a, **k: proposals.append(a),
    )
    playwright_runner._propose_healed_library_selector_to_kb(
        "surency", "repo",
        {"pages/LoginPage.ts": (LOGIN_PAGE, LOGIN_PAGE.replace("#stale-user", "user-input"))},
        7,
    )
    assert proposals == [("surency", "repo", "#stale-user", "user-input", 7)]

    # Two files each swapping a different selector stays ambiguous -> skipped.
    proposals.clear()
    playwright_runner._propose_healed_library_selector_to_kb(
        "surency", "repo",
        {
            "pages/A.ts": (".locator('#a')", ".locator('#a2')"),
            "pages/B.ts": (".locator('#b')", ".locator('#b2')"),
        },
        7,
    )
    assert proposals == []


# ---------------------------------------------------------------------------
# The three defences (plus the write boundary), each proven to ROLL BACK
# ---------------------------------------------------------------------------


@requires_git
def test_a_heal_that_breaks_another_cases_spec_is_rejected_and_rolled_back(
    db_session, monkeypatch
):
    """Defence 1 + 2. Collection covers the WHOLE project, so an edit that breaks a
    different ticket's spec is rejected and the tree is restored byte-for-byte."""
    monkeypatch.setattr(automation_gate, "typecheck_ok", lambda *a, **k: (True, "stubbed"))
    monkeypatch.setattr(
        automation_gate, "list_ok_in_project",
        lambda *a, **k: (False, "tests/SUR-9/x.spec.ts: LoginPage has no method 'signIn'"),
    )
    run, case, spec, project = _seed(db_session)
    root = aps.project_dir(project)
    other = _write(root, "tests/SUR-9/SUR-9-TC-01.spec.ts", "// another ticket's spec\n")
    aps.git_commit(project, "chore: another ticket")
    original = (root / "pages" / "LoginPage.ts").read_text(encoding="utf-8")
    _seed_result(db_session, run, case)
    _runner(monkeypatch, ["failed"])
    _healer_editor(monkeypatch, _fix_locator)
    monkeypatch.setattr(spec_service, "generate_fixed_spec_code", lambda *a, **k: LAYERED_SPEC)

    playwright_runner.heal_spec(case.id)

    assert (root / "pages" / "LoginPage.ts").read_text(encoding="utf-8") == original
    assert other.read_text(encoding="utf-8") == "// another ticket's spec\n"
    _code, _status, report, _p = _reload_spec(case.id)
    assert "broke project collection" in report["attempts"][0]["library"]["reason"]
    assert report["attempts"][0]["library"]["ok"] is False


@requires_git
def test_a_heal_that_re_signs_an_existing_method_is_rejected_and_rolled_back(
    db_session, gates, monkeypatch
):
    """Defence 3. Bodies are free in heal mode — signatures are not: other tickets'
    specs call `signIn(user)` by name and arity."""
    run, case, spec, project = _seed(db_session)
    root = aps.project_dir(project)
    original = (root / "pages" / "LoginPage.ts").read_text(encoding="utf-8")
    _seed_result(db_session, run, case)
    _runner(monkeypatch, ["failed"])

    def re_sign(r: Path) -> None:
        path = r / "pages" / "LoginPage.ts"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "async signIn(user: string) {", "async signIn(user: string, pw: string) {"
            ),
            encoding="utf-8",
        )

    _healer_editor(monkeypatch, re_sign)
    monkeypatch.setattr(spec_service, "generate_fixed_spec_code", lambda *a, **k: LAYERED_SPEC)

    playwright_runner.heal_spec(case.id)

    assert (root / "pages" / "LoginPage.ts").read_text(encoding="utf-8") == original
    _c, _s, report, _p = _reload_spec(case.id)
    assert "re-signed" in report["attempts"][0]["library"]["reason"]


@requires_git
def test_a_heal_that_deletes_an_assertion_from_the_page_object_is_rejected(
    db_session, gates, monkeypatch
):
    """The anti-cheat applies INSIDE the library too, not just to the spec: healing
    by deleting the page object's own check is not healing."""
    page = LOGIN_PAGE.replace(
        "  async signIn(user: string) {",
        "  async expectHome() {\n    await expect(this.page).toHaveTitle(/Home/);\n  }\n\n"
        "  async signIn(user: string) {",
    )
    run, case, spec, project = _seed(db_session, page_object=page)
    root = aps.project_dir(project)
    original = (root / "pages" / "LoginPage.ts").read_text(encoding="utf-8")
    _seed_result(db_session, run, case)
    _runner(monkeypatch, ["failed"])

    def gut(r: Path) -> None:
        path = r / "pages" / "LoginPage.ts"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "    await expect(this.page).toHaveTitle(/Home/);\n", ""
            ),
            encoding="utf-8",
        )

    _healer_editor(monkeypatch, gut)
    monkeypatch.setattr(spec_service, "generate_fixed_spec_code", lambda *a, **k: LAYERED_SPEC)

    playwright_runner.heal_spec(case.id)

    assert (root / "pages" / "LoginPage.ts").read_text(encoding="utf-8") == original
    _c, _s, report, _p = _reload_spec(case.id)
    assert "removed/weakened assertions" in report["attempts"][0]["library"]["reason"]


@requires_git
def test_the_healer_may_not_write_the_spec_or_a_file_the_spec_does_not_import(
    db_session, gates, monkeypatch
):
    """The write boundary, tighter than authoring's: only the library files THIS
    failing spec imports. Re-inlining into the spec is structurally impossible."""
    run, case, spec, project = _seed(db_session)
    root = aps.project_dir(project)
    _seed_result(db_session, run, case)
    _runner(monkeypatch, ["failed"])

    def overreach(r: Path) -> None:
        _fix_locator(r)
        _write(r, SPEC_RELATIVE, "// the healer inlining locators into the spec\n")
        _write(r, "pages/Sneaky.ts", "export class Sneaky {}\n")

    _healer_editor(monkeypatch, overreach)
    monkeypatch.setattr(spec_service, "generate_fixed_spec_code", lambda *a, **k: LAYERED_SPEC)

    playwright_runner.heal_spec(case.id)

    assert "#stale-user" in (root / "pages" / "LoginPage.ts").read_text(encoding="utf-8")
    assert (root / SPEC_RELATIVE).read_text(encoding="utf-8") == LAYERED_SPEC
    assert not (root / "pages" / "Sneaky.ts").exists()
    _c, _s, report, _p = _reload_spec(case.id)
    reason = report["attempts"][0]["library"]["reason"]
    assert "does not import" in reason and SPEC_RELATIVE in reason


@requires_git
def test_typecheck_runs_after_collection(db_session, monkeypatch):
    """#546 interaction: `--list` first (esbuild, cheap), `tsc` after — and `tsc`
    is what catches what esbuild erases, so it must actually run."""
    order: list[str] = []
    monkeypatch.setattr(
        automation_gate, "list_ok_in_project",
        lambda *a, **k: (order.append("list"), (True, ""))[1],
    )
    monkeypatch.setattr(
        automation_gate, "typecheck_ok",
        lambda *a, **k: (order.append("tsc"), (False, "TS2554"))[1],
    )
    run, case, spec, project = _seed(db_session)
    root = aps.project_dir(project)
    original = (root / "pages" / "LoginPage.ts").read_text(encoding="utf-8")
    _seed_result(db_session, run, case)
    _runner(monkeypatch, ["failed"])
    _healer_editor(monkeypatch, _fix_locator)
    monkeypatch.setattr(spec_service, "generate_fixed_spec_code", lambda *a, **k: LAYERED_SPEC)

    playwright_runner.heal_spec(case.id)

    assert order == ["list", "tsc"]
    assert (root / "pages" / "LoginPage.ts").read_text(encoding="utf-8") == original


@requires_git
def test_a_healer_that_changes_nothing_falls_through_to_the_spec_fixer(
    db_session, gates, monkeypatch
):
    """"The page object looks correct" is a legitimate, cheap conclusion — it must
    not be an error, and it must not stall the loop."""
    run, case, spec, project = _seed(db_session)
    _seed_result(db_session, run, case)
    _runner(monkeypatch, ["failed", "passed"])
    _healer_editor(monkeypatch, lambda r: None)
    fixed = LAYERED_SPEC.replace("await login.open();", "await login.open(); // fixed")
    monkeypatch.setattr(spec_service, "generate_fixed_spec_code", lambda *a, **k: fixed)

    playwright_runner.heal_spec(case.id)

    code, status, report, _p = _reload_spec(case.id)
    assert status == "passed" and code == fixed
    assert "nothing to change" in report["attempts"][0]["library"]["reason"]


# ---------------------------------------------------------------------------
# A product defect is still terminal — and never reaches the healer
# ---------------------------------------------------------------------------


@requires_git
def test_a_product_defect_is_terminal_and_never_reaches_the_library_healer(
    db_session, gates, monkeypatch
):
    """The app being wrong is never "healed" by editing the test — and now that the
    library is writable too, that must stay true of the library."""
    monkeypatch.setattr(
        failure_classifier, "classify_failure",
        lambda *a, **k: {
            "failureClass": "product_defect",
            "suspectedProductDefect": True,
            "reason": "the app rejected a valid password",
        },
    )
    run, case, spec, project = _seed(db_session)
    root = aps.project_dir(project)
    original = (root / "pages" / "LoginPage.ts").read_text(encoding="utf-8")
    _seed_result(db_session, run, case)
    _runner(monkeypatch, ["failed"])
    monkeypatch.setattr(
        claude_cli, "run_agentic",
        lambda *a, **k: pytest.fail("a product defect must not reach the library healer"),
    )
    monkeypatch.setattr(
        spec_service, "generate_fixed_spec_code",
        lambda *a, **k: pytest.fail("a product defect must not reach the spec fixer"),
    )

    playwright_runner.heal_spec(case.id)

    code, status, report, _p = _reload_spec(case.id)
    assert status == "product_defect"
    assert code == LAYERED_SPEC
    assert (root / "pages" / "LoginPage.ts").read_text(encoding="utf-8") == original
    assert report["attempts"][0]["productDefect"] is True


@requires_git
def test_a_legacy_spec_never_reaches_the_library_healer(db_session, gates, monkeypatch):
    """`project_id IS NULL` -> no project to edit, no agentic call, and the anti-cheat
    is the pre-#547 single-file count."""
    run, case, spec, project = _seed(db_session, project=False)
    _seed_result(db_session, run, case)
    _runner(monkeypatch, ["failed", "passed"])
    monkeypatch.setattr(
        claude_cli, "run_agentic",
        lambda *a, **k: pytest.fail("a legacy spec has no project library to heal"),
    )
    fixed = LAYERED_SPEC.replace("await login.open();", "await login.open(); // fixed")
    monkeypatch.setattr(spec_service, "generate_fixed_spec_code", lambda *a, **k: fixed)

    playwright_runner.heal_spec(case.id)

    code, status, report, _p = _reload_spec(case.id)
    assert status == "passed" and code == fixed
    assert "library" not in report["attempts"][0]


# ---------------------------------------------------------------------------
# The local-agent target reaches the project tree too
# ---------------------------------------------------------------------------


@requires_git
def test_agent_finalize_writes_the_healed_spec_into_the_project_tree(db_session, monkeypatch):
    """#540's leftover, fixed: `finalize_agent_heal` wrote only the legacy per-run
    dir, so an agent-executed heal never reached the project — the next generation
    read the pre-heal code."""
    from app.models.automation_project import AutomationFile

    run, case, spec, project = _seed(db_session)
    root = aps.project_dir(project)
    healed = LAYERED_SPEC.replace("await login.open();", "await login.open(); // healed on device")

    heal_service.finalize_agent_heal(
        db_session, case, run, {"finalStatus": "pass", "finalCode": healed, "attempts": []}
    )

    # 1. The PROJECT tree — the source of truth — carries the fix, and it is committed.
    assert (root / SPEC_RELATIVE).read_text(encoding="utf-8") == healed
    assert any("heal TC-01" in line for line in _git_log(root))
    # 2. ...mirrored to the DB, and `spec.path` points AT the project, not the run dir.
    files = {row.path: row.code for row in db_session.query(AutomationFile)}
    assert files[SPEC_RELATIVE] == healed
    _code, status, _r, path = _reload_spec(case.id)
    assert status == "passed"
    assert Path(path) == root / SPEC_RELATIVE


@requires_git
def test_agent_finalize_still_uses_the_run_dir_for_a_legacy_spec(db_session, monkeypatch):
    run, case, spec, project = _seed(db_session, project=False)
    heal_service.finalize_agent_heal(
        db_session, case, run, {"finalStatus": "pass", "finalCode": LAYERED_SPEC, "attempts": []}
    )
    _code, status, _r, path = _reload_spec(case.id)
    assert status == "passed" and path and "tests" not in Path(path).parts


@requires_git
def test_agent_plan_fix_repairs_the_library_and_ships_the_files_to_the_device(
    db_session, gates, monkeypatch
):
    """The agent is stateless with no read-file channel, so a server-side repair only
    reaches it if the new sources ride back on the fix — the same reasoning as the
    claim's project bundle. `code` comes back UNCHANGED: the spec is not the defect."""
    run, case, spec, project = _seed(db_session)
    _healer_editor(monkeypatch, _fix_locator)
    monkeypatch.setattr(
        spec_service, "generate_fixed_spec_code",
        lambda *a, **k: pytest.fail("the library was repaired — the spec fixer must not run"),
    )

    result = heal_service.plan_fix(
        db_session, case, run, LAYERED_SPEC, "locator '#stale-user' not found", "", None, 1
    )

    assert result["action"] == "fixed"
    assert result["code"] == LAYERED_SPEC, "the spec is unchanged — the fix is in the page object"
    assert [f["path"] for f in result["libraryFiles"]] == ["pages/LoginPage.ts"]
    assert "getByTestId('user')" in result["libraryFiles"][0]["code"]
    assert "getByTestId('user')" in (
        aps.project_dir(project) / "pages" / "LoginPage.ts"
    ).read_text(encoding="utf-8")


@requires_git
def test_agent_plan_fix_tries_the_library_only_on_the_first_attempt(
    db_session, gates, monkeypatch
):
    """Cost control on the agent path too: `plan_fix` is called per failed attempt, so
    without the gate a bounded agentic call becomes one per attempt."""
    run, case, spec, project = _seed(db_session)
    calls = _healer_editor(monkeypatch, _fix_locator)
    monkeypatch.setattr(spec_service, "generate_fixed_spec_code", lambda *a, **k: LAYERED_SPEC)

    heal_service.plan_fix(db_session, case, run, LAYERED_SPEC, "boom", "", None, 2)

    assert calls == []


@requires_git
def test_agent_plan_fix_anti_cheat_spans_the_imported_page_objects(
    db_session, gates, monkeypatch
):
    """Both halves on the agent path: a move is accepted, a deletion is rejected.

    Attempt 1 repairs the library (adding the page-level assertion), attempt 2's spec
    fix moves the inline assertion into a call to it.
    """
    run, case, spec, project = _seed(db_session)

    def add_assertion_method(root: Path) -> None:
        path = root / "pages" / "LoginPage.ts"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "  async signIn(user: string) {",
                "  async expectHome() {\n    await expect(this.page).toHaveTitle(/Home/);\n  }\n\n"
                "  async signIn(user: string) {",
            ),
            encoding="utf-8",
        )

    _healer_editor(monkeypatch, add_assertion_method)
    assert heal_service.plan_fix(
        db_session, case, run, LAYERED_SPEC, "boom", "", None, 1
    )["libraryFiles"]

    moved = LAYERED_SPEC.replace(
        "  await expect(page).toHaveTitle(/Home/);", "  await login.expectHome();"
    )
    monkeypatch.setattr(spec_service, "generate_fixed_spec_code", lambda *a, **k: moved)
    accepted = heal_service.plan_fix(db_session, case, run, LAYERED_SPEC, "boom", "", None, 2)
    assert accepted["action"] == "fixed" and accepted["code"] == moved

    stripped = LAYERED_SPEC.replace("  await expect(page).toHaveTitle(/Home/);\n", "")
    monkeypatch.setattr(spec_service, "generate_fixed_spec_code", lambda *a, **k: stripped)
    rejected = heal_service.plan_fix(db_session, case, run, LAYERED_SPEC, "boom", "", None, 3)
    assert rejected["action"] == "rejected" and "anti-cheat" in rejected["reason"]


# ---------------------------------------------------------------------------
# The one deliberate difference from authoring's defences
# ---------------------------------------------------------------------------


@requires_git
def test_diff_is_additive_allows_a_body_edit_only_in_heal_mode(db_session):
    """The flag exists because a stale locator IS a method body: refusing the rewrite
    is precisely what forced the loop to re-inline. Authoring keeps the strict form."""
    project = _project(db_session)
    root = aps.project_dir(project)
    _write(root, "pages/LoginPage.ts", LOGIN_PAGE)
    before = aps.inventory(project)
    _fix_locator(root)

    assert aps.diff_is_additive(project, before) is False           # authoring: rejected
    assert aps.diff_is_additive(project, before, allow_body_edits=True) is True  # heal: allowed

    # Even in heal mode, losing a signature is still a rejection.
    _write(root, "pages/LoginPage.ts", LOGIN_PAGE.replace("async signIn(user: string) {", "async renamed(user: string) {"))
    assert aps.diff_is_additive(project, before, allow_body_edits=True) is False
