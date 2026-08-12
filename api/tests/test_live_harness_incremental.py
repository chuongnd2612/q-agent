"""Incremental generation on the LIVE-HARNESS paths (#569).

``test_incremental_generation.py`` is the epic's acceptance test for the **blind**
generation branch. Until #569, page-object authoring ran on that branch only, so the
other three branches of ``automation._generate_one`` — server ``live-harness``,
``live-harness`` on a paired ``local-agent``, and the agent post-back — never grew the
asset library and the reuse rate depended on a workspace setting (which is exactly what
would have made #548's metric incomparable across modes).

This file asserts the property on those branches, reusing the acceptance test's own
machinery (its three features, its obedient models, its real-disk collection gate) so
the two files cannot drift: the models here are the **same** obedient readers of the
**same** production prompts, with one addition — the live-authoring agent, which is
handed the real live task prompt and answers it with ``_FakeModels.generate``, i.e. by
reading the ``render_plan`` block the prompt now carries. If the plan stops reaching the
live prompt, the live spec re-inlines locators and these tests fail.

What stays real: the project is a real git-backed tree, the planner, the page-object
author, all three of #545's defences (whole-project ``--list``, ``diff_is_additive``,
``git reset --hard``), the plan-import gate, and the agent-authoring queue.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import automation_project_service as aps

# The acceptance test's fixtures/helpers, imported so both files describe the same
# features with the same models. `incremental` is a fixture and works when imported.
from test_incremental_generation import (  # noqa: F401 - `incremental` is used as a fixture
    FEATURE_A,
    FEATURE_B,
    FEATURE_C,
    _collects,
    _project_specs,
    _run_feature,
    _seed_feature,
    incremental,
    requires_git,
)

pytestmark = pytest.mark.usefixtures("workspace_dir")

_SPEC_DELIVERABLE = re.compile(r"1\. `([^`]+\.spec\.ts)`")


def _settings(monkeypatch, **overrides) -> None:
    """Overlay authoring settings on the real stored defaults."""
    from app.services import settings_store

    base = dict(settings_store.load_settings())
    monkeypatch.setattr(settings_store, "load_settings", lambda: {**base, **overrides})


class _FakeProc:
    """Stand-in for the Chrome launcher subprocess (`_teardown` only needs these)."""

    stdin = None

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
        return 0


@pytest.fixture
def live(incremental, monkeypatch):  # noqa: F811 - the imported fixture
    """The `incremental` harness, plus a live-authoring agent driven by the real prompt.

    ``claude_cli.run_agentic`` is the one entry point for BOTH agentic calls in this
    pipeline (the page-object author and live authoring), so the dispatcher below is
    also the measurement point: ``author_prompts`` counts authoring calls and
    ``live_prompts`` counts live sessions, which is how "a reuse-only case makes no
    *authoring* call, though it still makes its live call" is asserted at the call
    itself.
    """
    from app.services import claude_cli, live_authoring_service, project_config_service, spec_service

    models = incremental.models
    live_prompts: list[str] = []
    rogue = SimpleNamespace(enabled=False, path="pages/LoginPage.ts")

    def run_agentic(prompt: str, **kwargs):
        if "browser-harness" in prompt:
            live_prompts.append(prompt)
            workspace = Path(kwargs["workspace_dir"])
            filename = _SPEC_DELIVERABLE.search(prompt).group(1)
            # The live agent answers the REAL prompt with the shared obedient
            # generator, which imports exactly what the plan block says is importable.
            (workspace / filename).write_text(models.generate(prompt), encoding="utf-8")
            (workspace / "discovered.json").write_text(
                json.dumps({"routes": [{"path": "/users", "description": "User list"}],
                            "selectors": []}),
                encoding="utf-8",
            )
            return "drove the app live and authored the spec"
        out = models.author(prompt, **kwargs)
        if rogue.enabled:
            # An editor that touches a file the plan never marked writable — defence
            # 3's plan-boundary check, which must roll the WHOLE pass back.
            target = Path(kwargs["workspace_dir"]) / rogue.path
            target.write_text(
                target.read_text(encoding="utf-8") + "\n// rogue edit\n", encoding="utf-8"
            )
        return out

    monkeypatch.setattr(claude_cli, "run_agentic", run_agentic)
    # Live authoring's real preconditions: the CLI, a signed-in profile, a Chrome.
    monkeypatch.setattr(claude_cli, "browser_harness_available", lambda: True)
    monkeypatch.setattr(live_authoring_service, "_launch_browser", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(live_authoring_service, "_wait_cdp", lambda *a, **k: True)
    profile = project_config_service.auth_path("Surency Platform", None).parent / "browser-profile"
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "Default").mkdir(exist_ok=True)

    return SimpleNamespace(
        models=models,
        gate_calls=incremental.gate_calls,
        live_prompts=live_prompts,
        rogue=rogue,
        monkeypatch=monkeypatch,
        spec_service=spec_service,
    )


def _live_server(monkeypatch) -> None:
    _settings(monkeypatch, authoringMode="live-harness", executionTarget="server")


def _project(db_session, project_id):
    from app.models.automation_project import AutomationProject

    return db_session.get(AutomationProject, project_id)


# ---------------------------------------------------------------------------
# Server live-harness: the reuse property, on the live path
# ---------------------------------------------------------------------------


@requires_git
def test_live_harness_second_feature_reuses_the_first_features_page_objects(db_session, live):
    """The acceptance property (#548/#569) on the SERVER live-harness branch.

    Same shape as ``test_feature_b_reuses_feature_a_and_generates_less_code``, read off
    ``plan_report`` — but every spec here was authored by the live agent, from the live
    prompt, which is the branch that used to reuse nothing.
    """
    _live_server(live.monkeypatch)

    a = _run_feature(db_session, live, FEATURE_A)
    b = _run_feature(db_session, live, FEATURE_B)
    c = _run_feature(db_session, live, FEATURE_C)

    assert live.live_prompts, "every spec on this branch must come from a live session"
    assert len(live.live_prompts) == 6, "one live session per case"
    assert a.project_id == b.project_id == c.project_id
    root = aps.project_dir(_project(db_session, a.project_id))

    # A into an empty project: everything is genuinely new, and the library is authored
    # SERVER-side even though the spec came from the live session.
    assert a.counts == {"reuse": 0, "extend": 0, "create": 3, "reuse-base": 1}
    assert a.created == [
        "pages/LoginPage.ts", "pages/UserTablePage.ts", "utils/tableSorting.ts"
    ]

    # B reuses A's login page and EXTENDS the shared table page — never a duplicate.
    assert b.actions["LoginPage"] == "reuse"
    assert b.actions["UserTablePage"] == "extend"
    assert b.actions["ExportDialogPage"] == "create"
    assert b.counts == {"reuse": 1, "extend": 1, "create": 1, "reuse-base": 2}
    assert "pages/LoginPage.ts" in b.plan["importable"]
    assert b.created == ["pages/ExportDialogPage.ts", "pages/UserTablePage.ts"]

    # The reuse is visible in the live-authored spec itself: a real import at the real
    # spec depth, not a re-inlined locator.
    for spec in b.specs:
        assert "../../pages/LoginPage" in (root / spec).read_text(encoding="utf-8")
    # ...because the live prompt carried the plan.
    assert "AUTOMATION PLAN" in live.live_prompts[-1]
    assert "`pages/LoginPage.ts` (reuse)" in live.live_prompts[-1]

    # C: fully reuse-only, so it writes no library code at all.
    assert c.counts == {"reuse": 3, "extend": 0, "create": 0, "reuse-base": 1}
    assert c.created == []
    assert c.reuse_rate >= b.reuse_rate > a.reuse_rate

    # The epic's success metric, on the live branch: generated lines per case trend DOWN.
    assert a.lines_per_case > b.lines_per_case > c.lines_per_case, (
        f"A={a.lines_per_case} B={b.lines_per_case} C={c.lines_per_case}"
    )
    assert c.library_lines == 0

    # Every feature's specs still collect together under the whole-project gate.
    ok, detail = _collects(root)
    assert ok, detail
    assert len({p.relative_to(root).as_posix() for p in _project_specs(root)}) == 6


@requires_git
def test_a_reuse_only_feature_makes_no_authoring_call_on_the_live_branch(db_session, live):
    """The cost control survives the move: reuse-only pays for no page-object editor.

    The live-authoring call itself is separate and expected (it is how the spec is
    written at all), so both counters are asserted: authoring calls stay flat while live
    sessions increase.
    """
    _live_server(live.monkeypatch)

    _run_feature(db_session, live, FEATURE_A)
    authored = len(live.models.author_prompts)
    lived = len(live.live_prompts)

    c = _run_feature(db_session, live, FEATURE_C)  # reuse-only against A's library

    assert c.counts["create"] == 0 and c.counts["extend"] == 0
    assert len(live.models.author_prompts) == authored, (
        "a reuse-only plan must not run the project editor on the live branch either"
    )
    assert len(live.live_prompts) == lived + len(FEATURE_C.cases), "but it still authors live"
    assert c.library_lines == 0


@requires_git
def test_a_rejected_asset_edit_rolls_back_and_leaves_the_live_specs_untouched(db_session, live):
    """Defence 3 + the git rollback, on the live branch.

    The editor is made to touch a file the plan never marked ``writable`` (A's
    ``LoginPage``, which B only ``reuse``s). The whole pass must roll back to the
    pre-authoring commit — including the *legitimate* extend in the same pass — while
    A's already-good specs stay exactly as they were and B still gets a spec (falling
    back to inline locators, the pre-#545 behaviour).
    """
    from app.models.audit import AuditLog

    _live_server(live.monkeypatch)
    a = _run_feature(db_session, live, FEATURE_A)
    project = _project(db_session, a.project_id)
    root = aps.project_dir(project)
    library = {path: (root / path).read_text(encoding="utf-8")
               for path in sorted(entry["path"] for entry in aps.inventory(project))}
    a_specs = {spec: (root / spec).read_text(encoding="utf-8") for spec in a.specs}

    live.rogue.enabled = True
    b = _run_feature(db_session, live, FEATURE_B)

    # Defence 2: the tree is byte-for-byte back at the pre-authoring commit.
    assert {entry["path"] for entry in aps.inventory(project)} == set(library)
    for path, text in library.items():
        assert (root / path).read_text(encoding="utf-8") == text, f"{path} was not rolled back"
    assert "rogue edit" not in (root / "pages" / "LoginPage.ts").read_text(encoding="utf-8")
    # The legitimate extend in the same pass went back too — rollback is per PASS.
    assert "selectRow" not in (root / "pages" / "UserTablePage.ts").read_text(encoding="utf-8")
    assert not (root / "pages" / "ExportDialogPage.ts").exists()

    # A's specs are untouched, and the tree still collects.
    for spec, text in a_specs.items():
        assert (root / spec).read_text(encoding="utf-8") == text
    ok, detail = _collects(root)
    assert ok, detail

    # The rejection is diagnosable, and B still got runnable specs (inline fallback).
    rolled_back = [
        row for row in db_session.query(AuditLog).filter(AuditLog.category == "ai").all()
        if (row.meta or "").startswith("Rolled back")
    ]
    assert rolled_back, "a rejected authoring pass records why it was rolled back"
    assert len(b.specs) == len(FEATURE_B.cases)


# ---------------------------------------------------------------------------
# local-agent live-harness + the post-back
# ---------------------------------------------------------------------------


def _owned(db_session, feature):
    """Seed a feature whose run has a real owner (a paired device needs a user)."""
    from app.models.user import User

    user = db_session.query(User).first()
    if user is None:
        user = User(email="device@test", first_name="Device", password_hash="x", is_active=True)
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
    run, cases = _seed_feature(db_session, feature)
    run.owner_id = user.id
    db_session.commit()
    db_session.refresh(run)
    return user, run, cases


def _pair_device(db_session, user):
    from app.models.agent_device import AgentDevice

    db_session.add(AgentDevice(owner_id=user.id, name="Test device", token_hash="x" * 64))
    db_session.commit()


@requires_git
def test_local_agent_live_harness_authors_the_library_before_enqueueing(db_session, live):
    """Option (a) of #569: the device receives a project whose assets already exist.

    The paired device is stateless and has no persistent project (#541), so authoring
    stays server-side and the plan rides in on the EXISTING ``task_prompt`` field. Both
    halves are asserted, including the wire shape — that is what makes "no agent release
    is required" a checked claim rather than a hope.
    """
    from app.routers import automation as automation_router
    from app.services import agent_authoring_service

    enqueued: list[dict] = []
    live.monkeypatch.setattr(
        agent_authoring_service, "request_authoring",
        lambda session_id, **kwargs: enqueued.append({"session_id": session_id, **kwargs}),
    )

    # Feature A first, blind, so there is a library to reuse.
    user, run_a, cases_a = _owned(db_session, FEATURE_A)
    for case in cases_a:
        spec = automation_router._generate_one(db_session, run_a, case)
        db_session.commit()
        assert spec.status == "draft", spec.block_reason
    project = _project(db_session, spec.project_id)
    root = aps.project_dir(project)

    # Now feature B on the paired device.
    _pair_device(db_session, user)
    _settings(live.monkeypatch, authoringMode="live-harness", executionTarget="local-agent")
    _, run_b, cases_b = _owned(db_session, FEATURE_B)
    pending = []
    for case in cases_b:
        pending.append(automation_router._generate_one(db_session, run_b, case))
        db_session.commit()

    assert [s.status for s in pending] == ["running", "running"], "handed to the device"
    assert len(enqueued) == len(cases_b)

    # The library was authored SERVER-side before the hand-off: the extend landed on
    # A's page object and the new page object exists — on disk, before the device ran.
    table = (root / "pages" / "UserTablePage.ts").read_text(encoding="utf-8")
    assert "async selectRow(" in table and "async rows()" in table
    assert (root / "pages" / "ExportDialogPage.ts").is_file()
    assert sorted(p.name for p in (root / "pages").glob("*.ts")) == [
        "ExportDialogPage.ts", "LoginPage.ts", "UserTablePage.ts"
    ]

    # ...and the plan reached the device inside the prompt it already receives.
    job = enqueued[0]
    assert "AUTOMATION PLAN" in job["task_prompt"]
    assert "`pages/LoginPage.ts` (reuse)" in job["task_prompt"]
    assert "`pages/UserTablePage.ts` (extend)" in job["task_prompt"]
    # No new wire field: the job shape the agent claims is exactly the pre-#569 one.
    assert set(job) == {
        "session_id", "owner_id", "project_key", "repo", "base_url", "origin", "case_id",
        "run_id", "spec_filename", "system_prompt", "task_prompt", "model",
        "max_budget_usd", "log_verbosity",
    }

    # -- The post-back: the device's spec lands against the existing library --------
    authored_before = len(live.models.author_prompts)
    case = cases_b[0]
    code = live.spec_service._extract_code(live.models.generate(job["task_prompt"]))
    assert "../../pages/LoginPage" in code, "the device consumed the plan's importable set"
    final = automation_router.finalize_authored_spec(
        db_session, run_b.id, case.id, code, {"routes": [], "selectors": []}
    )
    db_session.commit()

    assert final is not None and final.status == "draft", final.block_reason
    assert "../../pages/LoginPage" in (root / final.filename).read_text(encoding="utf-8")
    assert len(live.models.author_prompts) == authored_before, (
        "the post-back reuses the library this ticket already authored — no second call"
    )
    plan = json.loads(final.plan_report)
    assert "pages/LoginPage.ts" in plan["importable"]
    assert "pages/ExportDialogPage.ts" in plan["importable"]
    ok, detail = _collects(root)
    assert ok, detail


# ---------------------------------------------------------------------------
# #178: the skill and the prompt are one contract
# ---------------------------------------------------------------------------


def test_the_live_authoring_skill_documents_the_plan_block_it_is_handed():
    """The skill must consume the exact block ``render_plan`` emits (#178 discipline)."""
    from app.services import skills
    from app.services.automation_planner_service import render_plan

    rendered = render_plan({
        "pages": [{"name": "LoginPage", "path": "pages/LoginPage.ts", "action": "reuse",
                   "existingMethods": ["open()"]}],
        "importable": ["pages/LoginPage.ts"],
        "writable": [],
        "counts": {"reuse": 1, "extend": 0, "create": 0, "reuse-base": 0},
    })
    assert "AUTOMATION PLAN" in rendered

    skills.load_skill.cache_clear()
    text = skills.load_skill("live-authoring", include_template=True) or ""
    assert "AUTOMATION PLAN" in text, "the skill must name the block the prompt sends"
    assert "IMPORTABLE" in text and "NOT ON DISK" in text
    assert "writable" in text, "the skill must honour the plan's writable boundary"
    assert "../../pages/" in text, "specs live two levels deep"
