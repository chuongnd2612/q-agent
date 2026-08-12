"""REUSE/EXTEND/CREATE planning (#544) — Wave 3, step 1 of epic #537.

Covers every acceptance-criteria bullet of the slice:

* a plan is produced, normalized, persisted to BOTH locations and exposed on the
  spec row (so the UI renders it beside the gate report);
* an inventory containing ``UserListPage.openCreateUser()`` yields ``reuse``, not
  ``create``;
* a page object missing a needed method yields ``extend``, naming the method;
* an empty project plans ``create`` for everything;
* the reuse/extend/create counts are logged per plan and per pass;
* generation respects the plan — a ``reuse`` decision cannot produce a new file,
  and an unauthorized asset import is rejected.

Nothing here touches the network: ``claude_cli.run_json`` is always stubbed.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import automation_planner_service as planner
from app.services import automation_project_service as aps

pytestmark = pytest.mark.usefixtures("workspace_dir")

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def project(db_session):
    return aps.ensure_project(db_session, None, "SUR", "web")


def _write(project, relative: str, source: str) -> Path:
    path = aps.project_dir(project) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


USER_LIST_PAGE = """import { Page } from '@playwright/test';

export class UserListPage {
  constructor(private page: Page) {}

  async openCreateUser() {
    await this.page.getByTestId('create-user').click();
  }

  async search(term: string) {
    await this.page.getByLabel('Search').fill(term);
  }
}
"""

USER_FORM_PAGE = """import { Page } from '@playwright/test';

export class UserFormPage {
  constructor(private page: Page) {}

  async fillUser(user: { email: string }) {
    await this.page.getByLabel('Email').fill(user.email);
  }
}
"""


def _case(code: str = "TC-01", ticket: str = "SUR-1428"):
    return SimpleNamespace(
        code=code,
        title="Create a user",
        ticket_external_id=ticket,
        precondition="",
        steps=[{"a": "Click Create User", "e": "The user form opens"}],
        test_data=[],
    )


def _plan(project, raw, cases=None, run_code="RUN-P1", ticket="SUR-1428", monkeypatch=None):
    """Run ``plan_for_ticket`` with ``raw`` as Claude's reply."""
    from app.services import claude_cli

    assert monkeypatch is not None
    monkeypatch.setattr(claude_cli, "run_json", lambda *a, **k: raw)
    return planner.plan_for_ticket(
        project, run_code, ticket, cases or [_case()], {"projectKey": "SUR"}
    )


# ---------------------------------------------------------------------------
# AC: reuse is chosen when the inventory already owns the capability
# ---------------------------------------------------------------------------


def test_existing_method_plans_reuse_and_becomes_importable(project, monkeypatch):
    """Given `UserListPage.openCreateUser()`, a 'create user' case plans REUSE."""
    _write(project, "pages/UserListPage.ts", USER_LIST_PAGE)
    plan = _plan(
        project,
        {
            "feature": "User Management",
            "pages": [
                {
                    "name": "UserListPage",
                    "path": "pages/UserListPage.ts",
                    "action": "reuse",
                    "methods": ["openCreateUser()"],
                    "reason": "Already owns the user list toolbar.",
                }
            ],
        },
        monkeypatch=monkeypatch,
    )
    entry = plan["pages"][0]
    assert entry["action"] == "reuse"
    # Ground truth from the real file, not the model's claim.
    assert entry["existingMethods"] == ["openCreateUser()", "search(term)"]
    assert plan["importable"] == ["pages/UserListPage.ts"]
    # A reuse authorizes NO write — that is what stops a duplicate page object.
    assert plan["writable"] == []
    assert plan["counts"]["reuse"] == 1 and plan["counts"]["create"] == 0


# ---------------------------------------------------------------------------
# AC: a page object missing a needed method plans extend, naming the method
# ---------------------------------------------------------------------------


def test_missing_method_plans_extend_naming_the_method(project, monkeypatch):
    _write(project, "pages/UserFormPage.ts", USER_FORM_PAGE)
    plan = _plan(
        project,
        {
            "pages": [
                {
                    "name": "UserFormPage",
                    "path": "pages/UserFormPage.ts",
                    "action": "extend",
                    "methods": ["expectDuplicateEmailError()"],
                }
            ]
        },
        monkeypatch=monkeypatch,
    )
    entry = plan["pages"][0]
    assert entry["action"] == "extend"
    assert entry["methods"] == ["expectDuplicateEmailError()"]
    # The planned method is NOT presented as existing — #545 authors it.
    assert entry["existingMethods"] == ["fillUser(user)"]
    assert plan["counts"]["extend"] == 1
    # The file exists, so it is importable; the extension makes it writable.
    assert plan["importable"] == ["pages/UserFormPage.ts"]
    assert plan["writable"] == ["pages/UserFormPage.ts"]


# ---------------------------------------------------------------------------
# AC: an empty project plans create for everything
# ---------------------------------------------------------------------------


def test_empty_project_plans_create_for_everything(project, monkeypatch):
    plan = _plan(
        project,
        {
            "pages": [{"name": "UserListPage", "action": "create"}],
            "data": [{"name": "userData", "action": "create"}],
            "fixtures": [{"name": "authenticatedUser", "action": "reuse-base"}],
        },
        monkeypatch=monkeypatch,
    )
    assert plan["counts"] == {"reuse": 0, "extend": 0, "create": 2, "reuse-base": 1}
    # Paths are derived from the group when the model omits them.
    assert plan["pages"][0]["path"] == "pages/UserListPage.ts"
    assert plan["data"][0]["path"] == "data/userData.ts"
    # `reuse-base` has no file in this project at all.
    assert plan["fixtures"][0]["path"] == ""
    # NOTHING is importable: #545 has not authored these files, so an import would
    # fail collection. They are writable, so #545 can act on the plan.
    assert plan["importable"] == []
    assert plan["writable"] == ["data/userData.ts", "pages/UserListPage.ts"]


# ---------------------------------------------------------------------------
# Normalization: the model never gets to authorize an import
# ---------------------------------------------------------------------------


def test_a_hallucinated_reuse_path_is_demoted_to_create(project, monkeypatch):
    """`importable` comes from disk, never from the model — the #178 lesson."""
    plan = _plan(
        project,
        {"pages": [{"name": "GhostPage", "path": "pages/GhostPage.ts", "action": "reuse"}]},
        monkeypatch=monkeypatch,
    )
    assert plan["pages"][0]["action"] == "create"
    assert plan["importable"] == []


def test_paths_outside_the_library_are_rejected(project, monkeypatch):
    """A plan must not be able to point generation at the repo root or tests/."""
    _write(project, "pages/UserListPage.ts", USER_LIST_PAGE)
    plan = planner.normalize(
        {
            "pages": [
                {"name": "Escape", "path": "../../../etc/passwd", "action": "create"},
                {"name": "Spec", "path": "tests/x.spec.ts", "action": "create"},
            ]
        },
        aps.inventory(project),
    )
    # Both fall back to the group's default path rather than escaping the tree.
    assert [e["path"] for e in plan["pages"]] == ["pages/Escape.ts", "pages/Spec.ts"]


def test_an_unknown_action_degrades_to_create(project, monkeypatch):
    plan = planner.normalize({"pages": [{"name": "X", "action": "borrow"}]}, [])
    assert plan["pages"][0]["action"] == "create"


def test_a_non_dict_reply_yields_an_empty_non_actionable_plan(project, monkeypatch):
    plan = planner.normalize("not a plan", [])
    assert planner.is_actionable(plan) is False
    assert plan["counts"] == {"reuse": 0, "extend": 0, "create": 0, "reuse-base": 0}


def test_planning_failure_degrades_to_an_empty_plan(project, monkeypatch):
    from app.services import claude_cli

    def boom(*a, **k):
        raise claude_cli.ClaudeError("cli down")

    monkeypatch.setattr(claude_cli, "run_json", boom)
    plan = planner.plan_for_ticket(project, "RUN-P1", "SUR-1428", [_case()], {})
    assert planner.is_actionable(plan) is False
    # Nothing cached, so a later case retries instead of inheriting the outage.
    assert planner.load_plan(project, "RUN-P1", "SUR-1428") is None


# ---------------------------------------------------------------------------
# Once per ticket — the slice's cost lever
# ---------------------------------------------------------------------------


def test_plan_is_produced_once_per_ticket_and_cached_on_disk(project, monkeypatch):
    from app.services import claude_cli

    _write(project, "pages/UserListPage.ts", USER_LIST_PAGE)
    calls: list[int] = []

    def fake(*a, **k):
        calls.append(1)
        return {"pages": [{"name": "UserListPage", "path": "pages/UserListPage.ts",
                           "action": "reuse"}]}

    monkeypatch.setattr(claude_cli, "run_json", fake)
    cases = [_case("TC-01"), _case("TC-02"), _case("TC-03")]
    first = planner.plan_for_ticket(project, "RUN-P1", "SUR-1428", cases, {})
    second = planner.plan_for_ticket(project, "RUN-P1", "SUR-1428", cases, {})
    assert len(calls) == 1, "planning must happen once per ticket, not once per case"
    assert second["importable"] == first["importable"]
    # Persisted to the second of the two required locations.
    path = planner.plan_path(project, "RUN-P1", "SUR-1428")
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8"))["counts"]["reuse"] == 1
    # `.qagent/` is never shipped to the agent nor mirrored to the DB.
    assert ".qagent" in path.as_posix()
    assert not any(p.startswith(".qagent") for p in aps.bundle_for_agent(project))


def test_force_replans_even_with_a_cached_plan(project, monkeypatch):
    from app.services import claude_cli

    calls: list[int] = []
    monkeypatch.setattr(
        claude_cli, "run_json",
        lambda *a, **k: (calls.append(1), {"pages": [{"name": "P", "action": "create"}]})[1],
    )
    planner.plan_for_ticket(project, "RUN-P1", "SUR-1428", [_case()], {})
    planner.plan_for_ticket(project, "RUN-P1", "SUR-1428", [_case()], {}, force=True)
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Enforcement — the plan constrains imports and writes
# ---------------------------------------------------------------------------


SPEC_IMPORTING_PAGE = (
    "import { test, expect } from '@q-agent/playwright-base';\n"
    "import { UserListPage } from '../../pages/UserListPage';\n"
    "test('TC-01 — x', async ({ page }) => { await expect(page).toHaveURL(/x/); });\n"
)


def test_import_violations_allows_only_what_the_plan_authorized():
    plan = {"pages": [{"name": "UserListPage", "path": "pages/UserListPage.ts",
                       "action": "reuse"}],
            "importable": ["pages/UserListPage.ts"]}
    assert planner.import_violations(SPEC_IMPORTING_PAGE, plan) == []
    # The same spec under a plan that authorized nothing is a violation.
    create_plan = {"pages": [{"name": "UserListPage", "path": "pages/UserListPage.ts",
                              "action": "create"}], "importable": []}
    assert planner.import_violations(SPEC_IMPORTING_PAGE, create_plan) == ["pages/UserListPage"]


def test_import_violations_ignores_the_base_package_and_relative_non_assets():
    plan = {"pages": [{"name": "P", "path": "pages/P.ts", "action": "create"}], "importable": []}
    code = (
        "import { test } from '@q-agent/playwright-base';\n"
        "import x from './local';\n"
        "import y from '../../results.json';\n"
    )
    assert planner.import_violations(code, plan) == []


def test_import_violations_is_inert_without_an_actionable_plan():
    """A planning outage must not reject specs for a reason that isn't theirs."""
    assert planner.import_violations(SPEC_IMPORTING_PAGE, None) == []
    assert planner.import_violations(SPEC_IMPORTING_PAGE, planner.empty_plan()) == []


def test_reuse_plan_forbids_producing_a_new_file():
    """A case whose plan says `reuse` must not produce a new page object."""
    reuse_plan = {
        "pages": [{"name": "UserListPage", "path": "pages/UserListPage.ts", "action": "reuse"}],
        "importable": ["pages/UserListPage.ts"],
        "writable": [],
    }
    assert planner.unplanned_new_paths(
        ["pages/UserListPage.ts"],
        ["pages/UserListPage.ts", "pages/CreateUserPage.ts"],
        reuse_plan,
    ) == ["pages/CreateUserPage.ts"]
    # An extend's own file appearing/changing IS authorized.
    extend_plan = {
        "pages": [{"name": "P", "path": "pages/P.ts", "action": "extend"}],
        "writable": ["pages/P.ts"],
    }
    assert planner.unplanned_new_paths([], ["pages/P.ts"], extend_plan) == []


# ---------------------------------------------------------------------------
# The generation prompt — the plan is the import authorization
# ---------------------------------------------------------------------------


def test_render_plan_separates_importable_from_not_yet_authored():
    plan = planner.normalize(
        {
            "pages": [
                {"name": "UserFormPage", "path": "pages/UserFormPage.ts", "action": "extend",
                 "methods": ["expectDuplicateEmailError()"]},
                {"name": "AuditPage", "path": "pages/AuditPage.ts", "action": "create"},
            ]
        },
        [{"path": "pages/UserFormPage.ts", "kind": "page", "exports": ["UserFormPage"],
          "methods": ["fillUser(user)"]}],
    )
    block = planner.render_plan(plan)
    assert "IMPORTABLE NOW" in block
    assert "pages/UserFormPage.ts" in block and "fillUser(user)" in block
    # The planned extension is explicitly NOT callable yet.
    assert "PLANNED EXTENSIONS" in block
    assert "expectDuplicateEmailError()" in block
    # A `create` target is named but explicitly not importable.
    assert "TO BE CREATED LATER" in block and "pages/AuditPage.ts" in block
    assert "Import NOTHING else" in block


def test_render_plan_is_empty_without_an_actionable_plan():
    assert planner.render_plan(None) == ""
    assert planner.render_plan(planner.empty_plan()) == ""


def test_generation_prompt_carries_the_plan_block():
    from app.services.spec_service import _build_prompt

    plan = planner.normalize(
        {"pages": [{"name": "P", "path": "pages/P.ts", "action": "reuse"}]},
        [{"path": "pages/P.ts", "kind": "page", "exports": ["P"], "methods": ["open()"]}],
    )
    prompt = _build_prompt(_case(), None, None, None, plan)
    assert "AUTOMATION PLAN" in prompt
    assert "pages/P.ts" in prompt
    # The pre-#544 "a reference spec proves it exists" rule is gone.
    assert "appears as an import in a REFERENCE SPEC" not in prompt


# ---------------------------------------------------------------------------
# The inventory prompt block
# ---------------------------------------------------------------------------


def test_render_inventory_states_an_empty_project_plainly(project):
    block = planner.render_inventory(aps.inventory(project))
    assert "EMPTY" in block and "create" in block


def test_render_inventory_lists_real_signatures(project):
    _write(project, "pages/UserListPage.ts", USER_LIST_PAGE)
    block = planner.render_inventory(aps.inventory(project))
    assert "pages/UserListPage.ts" in block
    assert "openCreateUser()" in block and "search(term)" in block


# ---------------------------------------------------------------------------
# Observability — the epic's success metric
# ---------------------------------------------------------------------------


def test_counts_are_logged_per_plan_and_per_pass(project, monkeypatch):
    # loguru bypasses `caplog` (no stdlib propagation) and holds stderr from before
    # pytest's capture, so assert through a temporary loguru sink instead.
    from app.logging import logger

    lines: list[str] = []
    sink_id = logger.add(lambda message: lines.append(str(message)), level="INFO")
    try:
        _write(project, "pages/UserListPage.ts", USER_LIST_PAGE)
        plan = _plan(
            project,
            {
                "pages": [
                    {"name": "UserListPage", "path": "pages/UserListPage.ts", "action": "reuse"},
                    {"name": "AuditPage", "action": "create"},
                ]
            },
            monkeypatch=monkeypatch,
        )
        total = planner.log_pass_counts("RUN-P1", [plan, plan])
    finally:
        logger.remove(sink_id)
    text = "".join(lines)
    assert "reuse=1" in text and "create=1" in text
    assert total == {"reuse": 2, "extend": 0, "create": 2, "reuse-base": 0}
    assert "planned 2 ticket(s)" in text


def test_counts_helper_tallies_every_group():
    plan = planner.normalize(
        {
            "pages": [{"name": "A", "action": "create"}],
            "utils": [{"name": "B", "action": "create"}],
            "fixtures": [{"name": "C", "action": "reuse-base"}],
        },
        [],
    )
    assert planner.counts(plan) == {"reuse": 0, "extend": 0, "create": 2, "reuse-base": 1}
