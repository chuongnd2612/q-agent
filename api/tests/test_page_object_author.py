"""The agentic project editor: authoring + extending the shared library (#545).

Every test here drives a **fake editor** that performs real file writes in the
real project tree, so the three stacked defences are exercised against actual
disk state rather than against a mocked verdict:

1. whole-project ``playwright test --list``,
2. ``git reset --hard`` rollback,
3. ``diff_is_additive``.

Nothing touches the network or spawns node: ``claude_cli.run_agentic`` is replaced
per test, and both static gates are stubbed by default (a dev machine may well
have a real ``tsc`` under ``settings.playwright_node_modules``, and shelling out to
it would make the suite slow and machine-dependent — ``automation_gate`` has its
own dedicated coverage).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.services import automation_gate
from app.services import automation_planner_service as planner
from app.services import automation_project_service as aps
from app.services import claude_cli, page_object_author_service as author

pytestmark = pytest.mark.usefixtures("workspace_dir")

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

USER_PAGE = """import type { Page } from '@playwright/test';

export class UserPage {
  constructor(private readonly page: Page) {}

  async open() {
    await this.page.goto('/users');
  }
}
"""


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def gates(monkeypatch):
    """Both static gates stubbed to pass; tests flip the one they are about."""
    monkeypatch.setattr(automation_gate, "list_ok_in_project", lambda *a, **k: (True, "stubbed"))
    monkeypatch.setattr(automation_gate, "typecheck_ok", lambda *a, **k: (True, "stubbed"))
    return monkeypatch


def _project(db_session):
    return aps.ensure_project(db_session, None, "Surency Platform", "")


def _plan(project, entries, *, ticket="SUR-1428", cases=("TC-01",)):
    """A normalized plan for ``entries`` (raw planner-reply shape), against real disk."""
    return planner.normalize(
        {"pages": list(entries)},
        aps.inventory(project),
        feature="User Management",
        ticket=ticket,
        cases=list(cases),
    )


class _Case:
    def __init__(self, code="TC-01", title="Create a user"):
        self.code = code
        self.title = title
        self.steps = [{"a": "Open the user list", "e": "The list is shown"}]


def _editor(monkeypatch, writer):
    """Install ``writer(root)`` as the agentic editor; returns the recorded calls."""
    calls: list[dict] = []

    def fake_run_agentic(prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        writer(Path(kwargs["workspace_dir"]))
        return "wrote the files"

    monkeypatch.setattr(claude_cli, "run_agentic", fake_run_agentic)
    return calls


def _git_log(root: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "-C", str(root), "log", "--pretty=%s"],
        capture_output=True, text=True, timeout=30,
    )
    return (proc.stdout or "").splitlines()


def _audit_rows(db_session, action_prefix="Automation library"):
    from app.models.audit import AuditLog

    return [
        row
        for row in db_session.query(AuditLog).all()
        if (row.action or "").startswith(action_prefix)
    ]


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The tool lists
# ---------------------------------------------------------------------------


def test_project_tools_add_edit_and_drop_bash_without_touching_authoring_tools():
    """The two lists are separate on purpose (#545 AC): the project editor needs
    `Edit` (EXTEND means editing an existing file) and must NOT get `Bash` (it drives
    no browser and must not be able to run its own gates). Live authoring's list stays
    byte-identical."""
    assert claude_cli._PROJECT_TOOLS == ["Read", "Write", "Edit", "Glob", "Grep"]
    assert claude_cli._AUTHORING_TOOLS == ["Bash", "Read", "Write", "Glob", "Grep"]
    assert "Bash" not in claude_cli._PROJECT_TOOLS
    assert "Edit" not in claude_cli._AUTHORING_TOOLS


# ---------------------------------------------------------------------------
# The cost control: reuse-only makes NO agentic call
# ---------------------------------------------------------------------------


@requires_git
def test_a_reuse_only_plan_makes_no_agentic_call(db_session, gates, monkeypatch):
    """The slice's main cost control, verified from the usage records.

    `run_prompt` is the single exec path every Claude call funnels through — and the
    only place `ClaudeUsage` rows are written — so poisoning it proves no call was
    made, not merely that `run_agentic` wasn't the caller.
    """
    from app.models.claude_usage import ClaudeUsage

    project = _project(db_session)
    _write(aps.project_dir(project), "pages/UserPage.ts", USER_PAGE)

    def explode(*a, **k):
        raise AssertionError("a reuse-only plan must not reach the Claude CLI")

    monkeypatch.setattr(claude_cli, "run_prompt", explode)
    monkeypatch.setattr(claude_cli, "run_agentic", explode)

    plan = _plan(project, [
        {"name": "UserPage", "path": "pages/UserPage.ts", "action": "reuse", "methods": ["open()"]},
        {"name": "authenticatedUser", "action": "reuse-base"},
    ])
    assert plan["counts"] == {"reuse": 1, "extend": 0, "create": 0, "reuse-base": 1}

    refreshed, report = author.author_assets(
        db_session, project, "RUN-1", "SUR-1428", plan, [_Case()]
    )

    assert report["ran"] is False
    assert report["reason"] == "no create/extend actions"
    assert report["files"] == []
    assert refreshed is plan
    # No usage row was recorded, i.e. the pass genuinely cost nothing.
    assert db_session.query(ClaudeUsage).count() == 0
    # ...and no audit entry either — nothing was touched.
    assert _audit_rows(db_session) == []


@requires_git
def test_an_empty_or_failed_plan_makes_no_agentic_call(db_session, gates, monkeypatch):
    """A planning outage must not escalate into a paid editor run."""
    project = _project(db_session)
    monkeypatch.setattr(
        claude_cli, "run_agentic",
        lambda *a, **k: pytest.fail("an empty plan must not reach the editor"),
    )
    _, report = author.author_assets(
        db_session, project, "RUN-1", "SUR-1428", planner.empty_plan("f", "SUR-1428"), [_Case()]
    )
    assert report["ran"] is False


@requires_git
def test_the_editor_runs_once_per_ticket(db_session, gates, monkeypatch):
    """Planning is once per ticket (#544) and so is authoring: the plan's `authoredAt`
    stamp is what stops the ticket's second case paying for the editor again."""
    project = _project(db_session)
    calls = _editor(monkeypatch, lambda root: _write(root, "pages/UserPage.ts", USER_PAGE))
    plan = _plan(project, [{"name": "UserPage", "path": "pages/UserPage.ts", "action": "create",
                            "methods": ["open()"]}])

    plan, first = author.author_assets(db_session, project, "RUN-1", "SUR-1428", plan, [_Case()])
    assert first["ok"] and len(calls) == 1
    assert plan["authoredAt"]

    # The second case on the ticket loads the same (now stamped) plan.
    cached = planner.load_plan(project, "RUN-1", "SUR-1428")
    assert cached is not None and cached["authoredAt"] == plan["authoredAt"]
    _, second = author.author_assets(
        db_session, project, "RUN-1", "SUR-1428", cached, [_Case("TC-02")]
    )
    assert second["ran"] is False
    assert len(calls) == 1, "the editor must not run twice for one ticket"


@requires_git
def test_the_budget_preflight_skips_a_run_that_already_spent_it(db_session, gates, monkeypatch):
    """Bounded by the same ceiling as live authoring — checked BEFORE the call, not
    only inside the CLI's --max-budget-usd."""
    from app.services import ai_usage_service, settings_store

    project = _project(db_session)
    monkeypatch.setattr(settings_store, "authoring_cost_budget_usd", lambda: 1.0)
    monkeypatch.setattr(ai_usage_service, "run_breakdown", lambda *a, **k: {"totalCostUsd": 5.0})
    monkeypatch.setattr(
        claude_cli, "run_agentic",
        lambda *a, **k: pytest.fail("an over-budget run must not reach the editor"),
    )
    plan = _plan(project, [{"name": "UserPage", "path": "pages/UserPage.ts", "action": "create"}])
    _, report = author.author_assets(
        db_session, project, "RUN-1", "SUR-1428", plan, [_Case()], run_id=7
    )
    assert report["ran"] is False and "budget" in report["reason"]


# ---------------------------------------------------------------------------
# create / extend — the happy paths
# ---------------------------------------------------------------------------


@requires_git
def test_a_create_action_authors_a_page_object_and_makes_it_importable(
    db_session, gates, monkeypatch
):
    """The headline capability: a `create` becomes a real file, is committed, and the
    REFRESHED plan authorizes importing it — which is what lets the generated spec
    stop inlining locators in the very same pass."""
    from app.models.automation_project import AutomationFile

    project = _project(db_session)
    root = aps.project_dir(project)
    calls = _editor(monkeypatch, lambda r: _write(r, "pages/UserPage.ts", USER_PAGE))

    plan = _plan(project, [{"name": "UserPage", "path": "pages/UserPage.ts", "action": "create",
                            "methods": ["open()"], "reason": "First feature."}])
    # Before authoring the file does not exist, so it is NOT importable (that is #544's
    # behaviour and it is why the spec had to inline locators).
    assert plan["importable"] == [] and plan["writable"] == ["pages/UserPage.ts"]

    refreshed, report = author.author_assets(
        db_session, project, "RUN-1", "SUR-1428", plan, [_Case()], {"routes": [], "selectors": []}
    )

    assert report["ran"] is True and report["ok"] is True
    assert report["files"] == ["pages/UserPage.ts"]
    assert (root / "pages" / "UserPage.ts").is_file()
    # ...and now it IS importable, with the signatures the file really exports.
    assert refreshed["importable"] == ["pages/UserPage.ts"]
    entry = refreshed["pages"][0]
    assert entry["existingMethods"] == ["open()"]
    # The generator's prompt block reflects that, with no "to be created later" hedge.
    block = planner.render_plan(refreshed)
    assert "IMPORTABLE" in block and "pages/UserPage.ts" in block and "open()" in block
    assert "NOT ON DISK" not in block

    # Confined to the project dir, with the right skill and the project tool list.
    assert Path(calls[0]["workspace_dir"]) == root
    assert calls[0]["skill"] == "page-object-author"
    assert calls[0]["allowed_tools"] == claude_cli._PROJECT_TOOLS
    assert calls[0]["max_budget_usd"] > 0
    # The prompt names the plan action, the path, and the writable boundary.
    assert "CREATE `pages/UserPage.ts`" in calls[0]["prompt"]
    assert "Write ONLY these paths: pages/UserPage.ts" in calls[0]["prompt"]
    assert "ADDITIVE ONLY" in calls[0]["prompt"]

    # Committed, mirrored to the DB, and audited per file with the motivating action.
    assert any("author shared automation assets" in line for line in _git_log(root))
    paths = {row.path for row in db_session.query(AutomationFile)}
    assert "pages/UserPage.ts" in paths
    rows = _audit_rows(db_session)
    assert [r.action for r in rows] == ["Automation library create"]
    assert rows[0].status == "success"
    assert rows[0].detail["path"] == "pages/UserPage.ts"
    assert rows[0].detail["planAction"] == "create"


@requires_git
def test_an_extend_action_adds_a_method_without_touching_existing_bodies(
    db_session, gates, monkeypatch
):
    project = _project(db_session)
    root = aps.project_dir(project)
    _write(root, "pages/UserPage.ts", USER_PAGE)

    def extend(r: Path) -> None:
        path = r / "pages" / "UserPage.ts"
        text = path.read_text(encoding="utf-8")
        addition = (
            "\n  async expectDuplicateEmailError() {\n"
            "    await this.page.getByTestId('dup-email').isVisible();\n  }\n"
        )
        path.write_text(text.rstrip()[:-1] + addition + "}\n", encoding="utf-8")

    calls = _editor(monkeypatch, extend)
    plan = _plan(project, [{"name": "UserPage", "path": "pages/UserPage.ts", "action": "extend",
                            "methods": ["expectDuplicateEmailError()"]}])
    # An `extend` target is on disk, so it was importable already — but only its REAL
    # signatures were ever advertised, never the planned-but-unwritten one.
    assert plan["importable"] == ["pages/UserPage.ts"]
    assert plan["pages"][0]["existingMethods"] == ["open()"]

    refreshed, report = author.author_assets(
        db_session, project, "RUN-1", "SUR-1428", plan, [_Case()]
    )

    assert report["ok"] is True and report["files"] == ["pages/UserPage.ts"]
    text = (root / "pages" / "UserPage.ts").read_text(encoding="utf-8")
    # The pre-existing method survived untouched...
    assert "await this.page.goto('/users');" in text
    # ...and the planned one now exists, so the plan advertises it to the generator.
    assert refreshed["pages"][0]["existingMethods"] == ["open()", "expectDuplicateEmailError()"]
    assert "ALREADY IN THIS FILE" in calls[0]["prompt"]
    assert _audit_rows(db_session)[0].action == "Automation library extend"


# ---------------------------------------------------------------------------
# Defence 3 — diff_is_additive, plus defence 2 (rollback)
# ---------------------------------------------------------------------------


@requires_git
def test_rewriting_an_existing_method_is_rejected_and_rolled_back(
    db_session, gates, monkeypatch
):
    """AC: "an attempt that removes or rewrites an existing exported method is rejected
    by diff_is_additive and rolled back"."""
    project = _project(db_session)
    root = aps.project_dir(project)
    _write(root, "pages/UserPage.ts", USER_PAGE)
    aps.git_commit(project, "chore: seed the library")
    original = (root / "pages" / "UserPage.ts").read_text(encoding="utf-8")

    def sabotage(r: Path) -> None:
        # Same signature, different body — the silent-meaning-change case.
        _write(r, "pages/UserPage.ts", USER_PAGE.replace("'/users'", "'/admin/users'"))

    _editor(monkeypatch, sabotage)
    plan = _plan(project, [{"name": "UserPage", "path": "pages/UserPage.ts", "action": "extend",
                            "methods": ["expectDuplicateEmailError()"]}])
    _, report = author.author_assets(db_session, project, "RUN-1", "SUR-1428", plan, [_Case()])

    assert report["ran"] is True and report["ok"] is False
    assert "rewrote an existing exported method" in report["reason"]
    # Rolled back, byte for byte.
    assert (root / "pages" / "UserPage.ts").read_text(encoding="utf-8") == original
    # ...and the attempt is still diagnosable per file.
    rows = _audit_rows(db_session)
    assert rows and rows[0].status == "error"
    assert "Rolled back" in rows[0].meta


@requires_git
def test_deleting_an_existing_method_is_rejected_and_rolled_back(
    db_session, gates, monkeypatch
):
    project = _project(db_session)
    root = aps.project_dir(project)
    _write(root, "pages/UserPage.ts", USER_PAGE)
    aps.git_commit(project, "chore: seed the library")

    _editor(monkeypatch, lambda r: _write(
        r, "pages/UserPage.ts",
        "import type { Page } from '@playwright/test';\n"
        "export class UserPage {\n  constructor(private readonly page: Page) {}\n"
        "  async gone() {\n    return 1;\n  }\n}\n",
    ))
    plan = _plan(project, [{"name": "UserPage", "path": "pages/UserPage.ts", "action": "extend",
                            "methods": ["gone()"]}])
    _, report = author.author_assets(db_session, project, "RUN-1", "SUR-1428", plan, [_Case()])

    assert report["ok"] is False and "removed or rewrote" in report["reason"]
    assert "open()" in (root / "pages" / "UserPage.ts").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Defence 1 — the whole-project --list, and the #546 typecheck behind it
# ---------------------------------------------------------------------------


@requires_git
def test_breaking_another_cases_spec_fails_the_project_list_and_is_rolled_back(
    db_session, gates, monkeypatch
):
    """AC: "an attempt that breaks another case's spec fails the whole-project --list
    and is rolled back; the previously-good specs are untouched"."""
    project = _project(db_session)
    root = aps.project_dir(project)
    other = _write(
        root, "tests/SUR-1000/SUR-1000-TC-01.spec.ts",
        "import { test } from '@q-agent/playwright-base';\n"
        "import { UserPage } from '../../pages/UserPage';\n"
        "test('other ticket', async ({ page }) => { new UserPage(page); });\n",
    )
    _write(root, "pages/UserPage.ts", USER_PAGE)
    aps.git_commit(project, "chore: seed a previously-good spec")
    other_before = other.read_text(encoding="utf-8")

    # The edit renames the exported class, which breaks the OTHER ticket's spec.
    _editor(monkeypatch, lambda r: _write(
        r, "pages/UserPage.ts", USER_PAGE.replace("UserPage", "PeoplePage"),
    ))
    gates.setattr(
        automation_gate, "list_ok_in_project",
        lambda *a, **k: (False, "playwright --list failed (rc=1): has no exported member 'UserPage'"),
    )
    plan = _plan(project, [{"name": "UserPage", "path": "pages/UserPage.ts", "action": "extend",
                            "methods": ["rename()"]}])
    _, report = author.author_assets(db_session, project, "RUN-1", "SUR-1428", plan, [_Case()])

    assert report["ok"] is False
    assert "broke project collection" in report["reason"]
    # The other ticket's spec and the original page object are both intact.
    assert other.read_text(encoding="utf-8") == other_before
    assert "export class UserPage" in (root / "pages" / "UserPage.ts").read_text(encoding="utf-8")


@requires_git
def test_a_broken_signature_is_caught_by_typecheck_after_the_list(
    db_session, gates, monkeypatch
):
    """`--list` transpiles with esbuild, so a wrong signature collects cleanly — the
    #546 typecheck is what sees it. Assert the interaction: --list passes, tsc rejects,
    and the tree is still rolled back."""
    project = _project(db_session)
    root = aps.project_dir(project)
    _write(root, "pages/UserPage.ts", USER_PAGE)
    aps.git_commit(project, "chore: seed the library")

    order: list[str] = []
    gates.setattr(
        automation_gate, "list_ok_in_project",
        lambda *a, **k: (order.append("list"), (True, "collected cleanly"))[1],
    )
    gates.setattr(
        automation_gate, "typecheck_ok",
        lambda *a, **k: (order.append("tsc"), (False, "tsc --noEmit failed: error TS2554"))[1],
    )
    _editor(monkeypatch, lambda r: _write(
        r, "pages/UserPage.ts", USER_PAGE.rstrip()[:-1] + "  async broken(n: number) {\n    return n;\n  }\n}\n",
    ))
    plan = _plan(project, [{"name": "UserPage", "path": "pages/UserPage.ts", "action": "extend",
                            "methods": ["broken(n)"]}])
    _, report = author.author_assets(db_session, project, "RUN-1", "SUR-1428", plan, [_Case()])

    assert order == ["list", "tsc"], "the cheap collection check runs first"
    assert report["ok"] is False and "does not typecheck" in report["reason"]
    assert "broken" not in (root / "pages" / "UserPage.ts").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The plan boundary
# ---------------------------------------------------------------------------


@requires_git
def test_writing_outside_the_plans_writable_set_is_rejected(db_session, gates, monkeypatch):
    """The plan is a constraint, not advice: an unrequested extra file — or a spec, or
    a config file — reverts the whole edit."""
    project = _project(db_session)
    root = aps.project_dir(project)

    def overreach(r: Path) -> None:
        _write(r, "pages/UserPage.ts", USER_PAGE)
        _write(r, "utils/surprise.ts", "export const surprise = 1;\n")

    _editor(monkeypatch, overreach)
    plan = _plan(project, [{"name": "UserPage", "path": "pages/UserPage.ts", "action": "create",
                            "methods": ["open()"]}])
    _, report = author.author_assets(db_session, project, "RUN-1", "SUR-1428", plan, [_Case()])

    assert report["ok"] is False
    assert "did not authorize" in report["reason"] and "utils/surprise.ts" in report["reason"]
    # Both files are gone — the rollback is whole-tree, not per-file.
    assert not (root / "utils" / "surprise.ts").exists()
    assert not (root / "pages" / "UserPage.ts").exists()


@requires_git
def test_touching_tests_is_rejected(db_session, gates, monkeypatch):
    """The editor writes library code only; `tests/` belongs to the generator."""
    project = _project(db_session)
    _editor(monkeypatch, lambda r: (
        _write(r, "pages/UserPage.ts", USER_PAGE),
        _write(r, "tests/SUR-1428/SUR-1428-TC-01.spec.ts", "// mine now\n"),
    ))
    plan = _plan(project, [{"name": "UserPage", "path": "pages/UserPage.ts", "action": "create"}])
    _, report = author.author_assets(db_session, project, "RUN-1", "SUR-1428", plan, [_Case()])
    assert report["ok"] is False and "did not authorize" in report["reason"]


@requires_git
def test_an_editor_that_writes_nothing_is_a_rejection(db_session, gates, monkeypatch):
    project = _project(db_session)
    _editor(monkeypatch, lambda r: None)
    plan = _plan(project, [{"name": "UserPage", "path": "pages/UserPage.ts", "action": "create"}])
    _, report = author.author_assets(db_session, project, "RUN-1", "SUR-1428", plan, [_Case()])
    assert report["ok"] is False and report["reason"] == "the editor wrote nothing"


@requires_git
def test_a_crashing_editor_rolls_back_and_does_not_retry(db_session, gates, monkeypatch):
    """A CLI failure must degrade to the pre-#545 behaviour (inline locators), not
    retry a paid agentic call once per case of the ticket."""
    project = _project(db_session)

    def boom(prompt, **kwargs):
        _write(Path(kwargs["workspace_dir"]), "pages/Half.ts", "export class Half {}\n")
        raise claude_cli.ClaudeError("Claude CLI exited 1")

    monkeypatch.setattr(claude_cli, "run_agentic", boom)
    plan = _plan(project, [{"name": "UserPage", "path": "pages/UserPage.ts", "action": "create"}])
    refreshed, report = author.author_assets(
        db_session, project, "RUN-1", "SUR-1428", plan, [_Case()]
    )

    assert report["ran"] is True and report["ok"] is False
    assert not (aps.project_dir(project) / "pages" / "Half.ts").exists()
    assert refreshed["importable"] == []
    assert refreshed["authoredAt"] and refreshed["authoringError"]


# ---------------------------------------------------------------------------
# refresh_plan — the mechanism that connects the editor to the generator
# ---------------------------------------------------------------------------


@requires_git
def test_refresh_plan_promotes_only_what_is_actually_on_disk(db_session):
    """`importable` is re-derived from the tree, so a `create` the editor failed to
    write stays non-importable — the safety property survives without a special case."""
    project = _project(db_session)
    plan = _plan(project, [
        {"name": "UserPage", "path": "pages/UserPage.ts", "action": "create"},
        {"name": "GhostPage", "path": "pages/GhostPage.ts", "action": "create"},
    ])
    assert plan["importable"] == []

    _write(aps.project_dir(project), "pages/UserPage.ts", USER_PAGE)
    refreshed = planner.refresh_plan(project, "RUN-1", "SUR-1428", plan, authoredAt="now")

    assert refreshed["importable"] == ["pages/UserPage.ts"]
    assert refreshed["authoredAt"] == "now"
    assert refreshed["counts"] == plan["counts"], "refreshing must not re-decide anything"
    # The generator is told plainly that the other one is not there.
    block = planner.render_plan(refreshed)
    assert "NOT ON DISK" in block and "GhostPage" in block
    # ...and persisted over the cached plan, so the ticket's next case sees it.
    assert planner.load_plan(project, "RUN-1", "SUR-1428")["importable"] == ["pages/UserPage.ts"]


@requires_git
def test_git_changed_paths_reports_added_modified_and_untracked(db_session):
    project = _project(db_session)
    root = aps.project_dir(project)
    _write(root, "pages/UserPage.ts", USER_PAGE)
    aps.git_commit(project, "chore: seed")
    assert aps.git_changed_paths(project) == []

    _write(root, "pages/UserPage.ts", USER_PAGE + "\n// touched\n")
    _write(root, "data/users.ts", "export const validUser = { name: 'a' };\n")
    assert aps.git_changed_paths(project) == ["data/users.ts", "pages/UserPage.ts"]


@requires_git
def test_a_failed_pass_is_retried_not_treated_as_already_authored(db_session, gates, monkeypatch):
    """#608: a stamp that records an error means nothing was authored.

    The failure path stamps `authoredAt` to stop a paid agentic call being retried once
    per case of the ticket. But the entry guard used to look at `authoredAt` alone, so
    after the very first failure every later attempt returned
    `{"ran": False, "ok": True, "reason": "already authored for this ticket"}` — and the
    caller in `_generate_one` only warns when `ran and not ok`, so nothing ever surfaced
    again. On the live box that left `pages/` empty and `importable` `[]` permanently
    while the UI just showed an empty spec.
    """
    project = _project(db_session)

    boom = _editor(monkeypatch, lambda root: (_ for _ in ()).throw(RuntimeError("root refusal")))
    plan = _plan(project, [{"name": "UserPage", "path": "pages/UserPage.ts", "action": "create",
                            "methods": ["open()"]}])

    plan, first = author.author_assets(db_session, project, "RUN-1", "SUR-1428", plan, [_Case()])
    assert first["ran"] is True and first["ok"] is False
    assert plan["authoredAt"] and plan["authoringError"], "the failure is recorded on the plan"
    assert len(boom) == 1

    # The next attempt must RETRY rather than claim the ticket is already authored.
    calls = _editor(monkeypatch, lambda root: _write(root, "pages/UserPage.ts", USER_PAGE))
    cached = planner.load_plan(project, "RUN-1", "SUR-1428")
    assert cached is not None and cached["authoringError"]
    refreshed, second = author.author_assets(
        db_session, project, "RUN-1", "SUR-1428", cached, [_Case("TC-02")]
    )
    assert len(calls) == 1, "a plan stamped with an error must be retried"
    assert second["ran"] is True and second["ok"] is True
    assert "pages/UserPage.ts" in (refreshed["importable"] or []), (
        "the retry's files must become importable"
    )
    # And the success must clear the stale error, or every later case retries a pass
    # that has already succeeded (`**extra` merges onto the refreshed plan).
    assert not refreshed.get("authoringError")
    reloaded = planner.load_plan(project, "RUN-1", "SUR-1428")
    assert reloaded is not None and not reloaded.get("authoringError")

    # Now that it genuinely succeeded, the once-per-ticket cost guard applies again.
    _, third = author.author_assets(
        db_session, project, "RUN-1", "SUR-1428", reloaded, [_Case("TC-03")]
    )
    assert third["ran"] is False, "a clean stamp still skips"
    assert len(calls) == 1


def test_is_sandbox_is_set_only_when_root_and_skipping_permissions(monkeypatch):
    """#608: the CLI refuses `--dangerously-skip-permissions` at euid 0, which killed
    every agentic call in the container in ~0.3s. `IS_SANDBOX=1` is its documented
    container escape hatch — but it must not be set on a non-root host, and
    `os.geteuid` does not exist on Windows at all."""
    import os as _os

    monkeypatch.setattr(claude_cli.os, "geteuid", lambda: 0, raising=False)
    assert claude_cli._running_as_root() is True

    monkeypatch.setattr(claude_cli.os, "geteuid", lambda: 1000, raising=False)
    assert claude_cli._running_as_root() is False

    # Windows: no geteuid attribute at all — must not raise.
    monkeypatch.delattr(claude_cli.os, "geteuid", raising=False)
    assert claude_cli._running_as_root() is False
    assert not hasattr(_os, "geteuid") or True


# ---------------------------------------------------------------------------
# #617 — an already-satisfied plan makes NO agentic call
# ---------------------------------------------------------------------------


def _poison(monkeypatch) -> None:
    """Both Claude entry points explode.

    `run_prompt` is the single exec path every Claude call funnels through, and the
    only writer of `ClaudeUsage` rows, so poisoning it (not just `run_agentic`) is
    what makes "no call" a proof rather than a flag reading.
    """

    def explode(*a, **k):
        raise AssertionError("an already-satisfied plan must not reach the Claude CLI")

    monkeypatch.setattr(claude_cli, "run_prompt", explode)
    monkeypatch.setattr(claude_cli, "run_agentic", explode)


@requires_git
def test_a_create_whose_targets_are_all_on_disk_makes_no_agentic_call(
    db_session, gates, monkeypatch
):
    """#617: the cost leak. The reuse half worked — `normalize` marks an on-disk path
    importable and fills `existingMethods` — but the entry stayed in `writable`, so the
    editor bought a full agentic call (~60-115s live) to author files that were already
    there and already complete. Observed on the live box: `pages/DashboardPage.ts` and
    `pages/MyBenefitsPage.ts` on disk, plan still `create=2`, editor re-ran every run.
    """
    from app.models.claude_usage import ClaudeUsage

    project = _project(db_session)
    _write(aps.project_dir(project), "pages/UserPage.ts", USER_PAGE)
    _poison(monkeypatch)

    plan = _plan(project, [
        {"name": "UserPage", "path": "pages/UserPage.ts", "action": "create",
         "methods": ["open()"]},
    ])
    # #571's decision is untouched: the entry is still a `create`, still writable.
    assert plan["counts"]["create"] == 1
    assert plan["writable"] == ["pages/UserPage.ts"]
    assert plan["importable"] == ["pages/UserPage.ts"]

    refreshed, report = author.author_assets(
        db_session, project, "RUN-1", "SUR-1428", plan, [_Case()]
    )

    assert report["ran"] is False
    assert report["reason"] == "already satisfied by the project"
    assert report["satisfied"] == ["pages/UserPage.ts"]
    assert report["files"] == []
    assert refreshed is plan
    # The proof: no usage row, so no Claude call happened at all.
    assert db_session.query(ClaudeUsage).count() == 0
    assert _audit_rows(db_session) == []
    # ...and nothing was stamped, so the plan is not falsely marked authored.
    assert not plan.get("authoredAt")


@requires_git
def test_an_on_disk_target_with_no_planned_methods_is_nothing_to_author(
    db_session, gates, monkeypatch
):
    """No methods planned + the file exists == there is nothing left to add."""
    from app.models.claude_usage import ClaudeUsage

    project = _project(db_session)
    _write(aps.project_dir(project), "pages/UserPage.ts", USER_PAGE)
    _poison(monkeypatch)

    plan = _plan(project, [{"name": "UserPage", "path": "pages/UserPage.ts",
                            "action": "extend"}])
    _, report = author.author_assets(db_session, project, "RUN-1", "SUR-1428", plan, [_Case()])

    assert report["ran"] is False and report["reason"] == "already satisfied by the project"
    assert db_session.query(ClaudeUsage).count() == 0


@requires_git
def test_an_extend_for_methods_the_file_already_has_makes_no_call(
    db_session, gates, monkeypatch
):
    """Compared by method NAME, not signature: a plan writes `open` or `open(user)` while
    `inventory()` renders `open()`, so a whole-signature comparison would never match and
    the skip would never fire on real data."""
    from app.models.claude_usage import ClaudeUsage

    project = _project(db_session)
    _write(aps.project_dir(project), "pages/UserPage.ts", USER_PAGE)
    _poison(monkeypatch)

    plan = _plan(project, [{"name": "UserPage", "path": "pages/UserPage.ts",
                            "action": "extend", "methods": ["open", "async open(user)"]}])
    assert plan["pages"][0]["existingMethods"] == ["open()"]
    _, report = author.author_assets(db_session, project, "RUN-1", "SUR-1428", plan, [_Case()])

    assert report["ran"] is False and report["reason"] == "already satisfied by the project"
    assert db_session.query(ClaudeUsage).count() == 0


@requires_git
def test_an_extend_asking_for_a_genuinely_new_method_still_authors(
    db_session, gates, monkeypatch
):
    """The other half of the rule: the skip must not swallow a real capability gap."""
    project = _project(db_session)
    root = aps.project_dir(project)
    _write(root, "pages/UserPage.ts", USER_PAGE)

    calls = _editor(monkeypatch, lambda r: (r / "pages" / "UserPage.ts").write_text(
        USER_PAGE.rstrip()[:-1] + "\n  async expectEmpty() {\n    return 1;\n  }\n}\n",
        encoding="utf-8",
    ))
    plan = _plan(project, [{"name": "UserPage", "path": "pages/UserPage.ts", "action": "extend",
                            "methods": ["open()", "expectEmpty()"]}])
    refreshed, report = author.author_assets(
        db_session, project, "RUN-1", "SUR-1428", plan, [_Case()]
    )

    assert len(calls) == 1, "one new method is enough to keep authoring"
    assert report["ran"] is True and report["ok"] is True
    assert "expectEmpty" in (root / "pages" / "UserPage.ts").read_text(encoding="utf-8")
    assert "expectEmpty()" in (refreshed["pages"][0]["existingMethods"] or [])


@requires_git
def test_a_create_for_a_path_not_on_disk_still_authors(db_session, gates, monkeypatch):
    """No regression for a fresh project: nothing on disk means everything is pending."""
    project = _project(db_session)
    root = aps.project_dir(project)
    calls = _editor(monkeypatch, lambda r: _write(r, "pages/UserPage.ts", USER_PAGE))

    plan = _plan(project, [{"name": "UserPage", "path": "pages/UserPage.ts", "action": "create",
                            "methods": ["open()"]}])
    assert plan["importable"] == [], "nothing is on disk yet"
    _, report = author.author_assets(db_session, project, "RUN-1", "SUR-1428", plan, [_Case()])

    assert len(calls) == 1 and report["ok"] is True
    assert (root / "pages" / "UserPage.ts").exists()


@requires_git
def test_a_mixed_plan_authors_only_the_outstanding_entry(db_session, gates, monkeypatch):
    """A satisfied entry is dropped from the prompt; the outstanding one still drives it."""
    project = _project(db_session)
    root = aps.project_dir(project)
    _write(root, "pages/UserPage.ts", USER_PAGE)

    calls = _editor(monkeypatch, lambda r: _write(
        r, "pages/OrderPage.ts", "export class OrderPage {\n  async open() {\n  }\n}\n",
    ))
    plan = _plan(project, [
        {"name": "UserPage", "path": "pages/UserPage.ts", "action": "extend",
         "methods": ["open()"]},
        {"name": "OrderPage", "path": "pages/OrderPage.ts", "action": "create",
         "methods": ["open()"]},
    ])
    _, report = author.author_assets(db_session, project, "RUN-1", "SUR-1428", plan, [_Case()])

    assert len(calls) == 1 and report["ok"] is True
    prompt = calls[0]["prompt"]
    assert "`pages/OrderPage.ts`" in prompt
    assert "`pages/UserPage.ts`" not in prompt, "a satisfied entry is not re-authored"


def test_pending_actions_compares_method_names_not_signatures():
    """The unit-level rule, independent of disk: the name before `(`, casefolded."""
    def _p(action, methods, existing, *, path="pages/UserPage.ts", on_disk=True):
        return {
            "feature": "f", "ticket": "SUR-1",
            "specGroups": [{"name": "g", "testCases": ["TC-01"]}],
            "pages": [{"name": "UserPage", "path": path, "action": action,
                       "methods": methods, "existingMethods": existing}],
            "components": [], "fixtures": [], "data": [], "utils": [],
            "importable": [path] if on_disk else [],
        }

    # Argument-list formatting differs between the plan and `inventory()`.
    assert author.pending_actions(_p("extend", ["fillUser(user: User)"], ["fillUser(user)"])) == []
    # A genuinely new method survives.
    assert len(author.pending_actions(
        _p("extend", ["fillUser(u)", "reset()"], ["fillUser(user)"]))) == 1
    # Not on disk => always pending, whatever the methods say.
    assert len(author.pending_actions(
        _p("create", ["open()"], ["open()"], on_disk=False))) == 1
