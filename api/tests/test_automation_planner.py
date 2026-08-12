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
# Duplicate detection (doc §21), machine-enforced (#571)
# ---------------------------------------------------------------------------
#
# Until #571 §21 was prompt-enforced only, so a plan that *deliberately* asked to
# create `pages/CreateUserPage.ts` beside `pages/UserPage.ts` was authorized,
# written, and rejected by nothing. What makes this hard to test honestly is that a
# false positive is worse than a miss: `UserPage` / `UserListPage` / `UserFormPage`
# are genuinely distinct screens (doc §11 lists the last two side by side) whose
# token sets differ by *exactly as much* as `CreateUserPage` differs from
# `UserPage`. Both directions are therefore pinned below.

USER_PAGE = """import { Page } from '@playwright/test';

export class UserPage {
  constructor(private page: Page) {}

  async open() {
    await this.page.goto('/users');
  }
}
"""

WAIT_FOR_DOWNLOAD = """import type { Page } from '@playwright/test';

export async function waitForDownload(page: Page): Promise<void> {
  await page.waitForEvent('download');
}
"""


def test_creating_a_second_page_object_for_one_screen_is_demoted_to_extend(
    project, monkeypatch
):
    """Doc §21's FIRST example: no `CreateUserPage.ts` beside `UserPage.ts`."""
    _write(project, "pages/UserPage.ts", USER_PAGE)
    plan = _plan(
        project,
        {
            "pages": [
                {
                    "name": "CreateUserPage",
                    "path": "pages/CreateUserPage.ts",
                    "action": "create",
                    "methods": ["fillUser(user)", "open()"],
                    "reason": "The feature creates users.",
                }
            ]
        },
        monkeypatch=monkeypatch,
    )
    entry = plan["pages"][0]
    assert entry["action"] == "extend", "a duplicate screen owner is an extend, doc §8.2"
    assert entry["path"] == "pages/UserPage.ts"
    assert entry["name"] == "UserPage", "the real owner's exported class, not the duplicate"
    assert entry["duplicateOf"] == "pages/UserPage.ts"
    assert entry["plannedPath"] == "pages/CreateUserPage.ts"
    # Only the genuinely new capability is extended in; `open()` is already there.
    assert entry["methods"] == ["fillUser(user)"]
    assert "pages/UserPage.ts" in entry["reason"]

    # The enforcement itself: the duplicate path was never authorized for writing.
    assert plan["writable"] == ["pages/UserPage.ts"]
    assert "pages/CreateUserPage.ts" not in plan["writable"]
    assert plan["importable"] == ["pages/UserPage.ts"]
    assert plan["counts"] == {"reuse": 0, "extend": 1, "create": 0, "reuse-base": 0}
    # Counted and logged beside reuse/extend/create.
    assert plan["duplicatesDemoted"] == 1
    finding = plan["duplicates"][0]
    assert finding["plannedPath"] == "pages/CreateUserPage.ts"
    assert finding["existingPath"] == "pages/UserPage.ts"
    assert finding["action"] == "extend"


def test_a_duplicate_needing_nothing_new_is_demoted_to_reuse(project, monkeypatch):
    """Nothing to add means nothing to author — a plain `reuse`, and no editor call."""
    _write(project, "pages/UserPage.ts", USER_PAGE)
    plan = _plan(
        project,
        {
            "pages": [
                {"name": "ViewUserPage", "path": "pages/ViewUserPage.ts",
                 "action": "create", "methods": ["open()"]}
            ]
        },
        monkeypatch=monkeypatch,
    )
    assert plan["pages"][0]["action"] == "reuse"
    assert plan["pages"][0]["path"] == "pages/UserPage.ts"
    assert plan["writable"] == [], "a reuse authorizes no write at all"
    assert plan["duplicatesDemoted"] == 1


def test_a_duplicate_of_an_already_planned_owner_merges_into_it(project, monkeypatch):
    """The realistic §21 shape: "reuse `UserPage` AND create `CreateUserPage`".

    Demoting naively would leave two entries for one path, which hands the project
    editor (#545) two conflicting instructions for the same file.
    """
    _write(project, "pages/UserPage.ts", USER_PAGE)
    plan = _plan(
        project,
        {
            "pages": [
                {"name": "UserPage", "path": "pages/UserPage.ts", "action": "reuse",
                 "methods": ["open()"]},
                {"name": "CreateUserPage", "path": "pages/CreateUserPage.ts",
                 "action": "create", "methods": ["fillUser(user)"]},
            ]
        },
        monkeypatch=monkeypatch,
    )
    assert len(plan["pages"]) == 1, "the duplicate disappears into the real owner"
    entry = plan["pages"][0]
    assert entry["path"] == "pages/UserPage.ts"
    assert entry["action"] == "extend", "the merged-in method is new, so reuse becomes extend"
    assert entry["methods"] == ["open()", "fillUser(user)"]
    assert plan["writable"] == ["pages/UserPage.ts"]
    assert plan["duplicates"][0]["mergedInto"] is True


def test_a_duplicate_utility_for_an_existing_capability_is_caught(project, monkeypatch):
    """Doc §21's SECOND example: no `helpers/download.ts` beside `waitForDownload`."""
    _write(project, "utils/waitForDownload.ts", WAIT_FOR_DOWNLOAD)
    plan = _plan(
        project,
        {
            "utils": [
                {"name": "download", "path": "helpers/download.ts", "action": "create",
                 "methods": ["download(page)"]}
            ]
        },
        monkeypatch=monkeypatch,
    )
    entry = plan["utils"][0]
    assert entry["action"] == "reuse", "the capability is already there in full"
    assert entry["path"] == "utils/waitForDownload.ts"
    assert entry["duplicateOf"] == "utils/waitForDownload.ts"
    assert plan["writable"] == []
    assert plan["duplicatesDemoted"] == 1
    assert "waitForDownload" in plan["duplicates"][0]["reason"]


def test_capability_overlap_is_found_inside_a_differently_named_file(project, monkeypatch):
    """The exported NAME is the capability, so the file it lives in need not match."""
    _write(project, "utils/browser.ts", WAIT_FOR_DOWNLOAD)
    plan = _plan(
        project,
        {
            "utils": [
                {"name": "download", "path": "utils/download.ts", "action": "create",
                 "methods": ["download(page)"]}
            ]
        },
        monkeypatch=monkeypatch,
    )
    assert plan["utils"][0]["path"] == "utils/browser.ts"
    assert "already provides this capability" in plan["duplicates"][0]["reason"]


# -- The false-positive side, which matters more than the true-positive side ----


@pytest.mark.parametrize(
    "name",
    [
        "OrderPage",      # a wholly unrelated screen — the issue's own AC
        "UserListPage",   # doc §11: a distinct screen that CONTAINS the existing name
        "UserFormPage",   # doc §11, listed beside UserListPage
        "UserTablePage",  # the noun differs, so the screen differs
        "UserAuditLogPage",
    ],
)
def test_a_genuinely_distinct_page_object_is_never_blocked(project, monkeypatch, name):
    """A distinct screen must survive as a `create`, even sharing a noun.

    ``UserListPage`` differs from ``UserPage`` by one token, exactly like
    ``CreateUserPage`` does — so a heuristic that collapses these is wrong, and this
    is the test that says so. The separator is the *kind* of the extra token: a noun
    names another screen, a verb names a capability of this one.
    """
    _write(project, "pages/UserPage.ts", USER_PAGE)
    plan = _plan(
        project,
        {"pages": [{"name": name, "path": f"pages/{name}.ts", "action": "create",
                    "methods": ["open()"]}]},
        monkeypatch=monkeypatch,
    )
    entry = plan["pages"][0]
    assert entry["action"] == "create", f"{name} is a distinct screen, not a duplicate"
    assert entry["path"] == f"pages/{name}.ts"
    assert plan["writable"] == [f"pages/{name}.ts"]
    assert plan["duplicates"] == [] and plan["duplicatesDemoted"] == 0


def test_distinct_utilities_sharing_a_noun_are_not_blocked(project, monkeypatch):
    _write(project, "utils/tableSorting.ts",
           "export async function sortColumn(page: any, column: string) {}\n")
    plan = _plan(
        project,
        {
            "utils": [
                {"name": "tableFiltering", "path": "utils/tableFiltering.ts",
                 "action": "create", "methods": ["filterColumn(page, column)"]}
            ]
        },
        monkeypatch=monkeypatch,
    )
    assert plan["utils"][0]["action"] == "create"
    assert plan["writable"] == ["utils/tableFiltering.ts"]


def test_a_page_is_never_judged_a_duplicate_of_a_util(project, monkeypatch):
    """Kinds do not cross: `pages/DownloadPage.ts` is not `utils/waitForDownload.ts`."""
    _write(project, "utils/waitForDownload.ts", WAIT_FOR_DOWNLOAD)
    plan = _plan(
        project,
        {"pages": [{"name": "DownloadPage", "path": "pages/DownloadPage.ts",
                    "action": "create"}]},
        monkeypatch=monkeypatch,
    )
    assert plan["pages"][0]["action"] == "create"
    assert plan["writable"] == ["pages/DownloadPage.ts"]


def test_page_method_names_never_trigger_capability_overlap(project, monkeypatch):
    """Two screens legitimately both `open()`; only utils get the capability check."""
    _write(project, "pages/UserPage.ts", USER_PAGE)
    plan = _plan(
        project,
        {"pages": [{"name": "OrderPage", "path": "pages/OrderPage.ts", "action": "create",
                    "methods": ["open()"]}]},
        monkeypatch=monkeypatch,
    )
    assert plan["pages"][0]["action"] == "create"


def test_the_threshold_is_tunable(project, monkeypatch):
    """Two action words is beyond the default tolerance — deliberately conservative."""
    _write(project, "pages/UserPage.ts", USER_PAGE)
    entries = aps.inventory(project)
    assert planner.duplicate_owner("pages/CreateUserPage.ts", "CreateUserPage", [], entries)
    # `AddNewUserPage` differs by TWO action words, so the default threshold of 1
    # leaves it alone; raising the knob catches it.
    assert planner.duplicate_owner("pages/AddNewUserPage.ts", "AddNewUserPage", [], entries) is None
    monkeypatch.setattr(planner, "DUPLICATE_MAX_EXTRA_TOKENS", 2)
    assert planner.duplicate_owner("pages/AddNewUserPage.ts", "AddNewUserPage", [], entries)
    # 0 disables everything but an exact name match.
    monkeypatch.setattr(planner, "DUPLICATE_MAX_EXTRA_TOKENS", 0)
    assert planner.duplicate_owner("pages/CreateUserPage.ts", "CreateUserPage", [], entries) is None


def test_a_create_of_a_path_already_on_disk_is_left_alone(project, monkeypatch):
    """Not a near-duplicate: same file, already covered by the additive-diff guard."""
    _write(project, "pages/UserPage.ts", USER_PAGE)
    plan = _plan(
        project,
        {"pages": [{"name": "UserPage", "path": "pages/UserPage.ts", "action": "create"}]},
        monkeypatch=monkeypatch,
    )
    assert plan["pages"][0]["action"] == "create"
    assert plan["duplicates"] == []


def test_demotions_are_logged_and_survive_the_post_authoring_refresh(project, monkeypatch):
    """The demotion rate is the real signal on the planner prompt, so it is observable."""
    from app.logging import logger

    _write(project, "pages/UserPage.ts", USER_PAGE)
    lines: list[str] = []
    sink_id = logger.add(lambda message: lines.append(str(message)), level="INFO")
    try:
        plan = _plan(
            project,
            {"pages": [{"name": "CreateUserPage", "path": "pages/CreateUserPage.ts",
                        "action": "create", "methods": ["fillUser(user)"]}]},
            monkeypatch=monkeypatch,
        )
        planner.log_pass_counts("RUN-P1", [plan])
    finally:
        logger.remove(sink_id)
    text = "".join(lines)
    assert "duplicate detected (doc §21)" in text
    assert "pages/CreateUserPage.ts" in text and "pages/UserPage.ts" in text
    assert "duplicates-demoted=1" in text

    # `refresh_plan` re-normalizes against the tree; the entry is an `extend` by then,
    # so nothing is re-demoted — but the record must not evaporate from the plan file.
    refreshed = planner.refresh_plan(project, "RUN-P1", "SUR-1428", plan)
    assert refreshed["duplicatesDemoted"] == 1
    assert refreshed["duplicates"][0]["plannedPath"] == "pages/CreateUserPage.ts"
    on_disk = json.loads(
        planner.plan_path(project, "RUN-P1", "SUR-1428").read_text(encoding="utf-8")
    )
    assert on_disk["duplicatesDemoted"] == 1


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


def test_render_plan_separates_what_is_on_disk_from_what_is_not():
    """#545 rewords this block deliberately: the "TO BE CREATED LATER / PLANNED
    EXTENSIONS — a later stage authors these" framing was true only while nothing
    authored page objects. The editor now runs BEFORE generation, so the split is
    simply on-disk vs not, and an inline locator is the exception rather than the
    instruction. (Same sentence family as the `_SPEC_ARCHITECTURE` bullet and the
    automation-generator skill — all of them move in one commit, #178.)"""
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
    assert "IMPORTABLE" in block
    assert "pages/UserFormPage.ts" in block and "fillUser(user)" in block
    # A path that is not on disk is named, and is explicitly not importable.
    assert "NOT ON DISK" in block and "pages/AuditPage.ts" in block
    assert "Import NOTHING else" in block
    # The superseded "a later stage authors this" premise is gone.
    assert "TO BE CREATED LATER" not in block and "PLANNED EXTENSIONS" not in block


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
