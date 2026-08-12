"""The acceptance test for epic #537 — incremental generation (#548).

Doc §19 calls this *"one of the most important requirements"*: **the generator must
not assume every feature starts from an empty project.** The epic's success metric is
not speed, it is *"how little new code does QAgent need to generate while still fully
covering the new test cases"* — a **regression-able property**, and the whole reason
this file exists. Without it the property erodes silently the first time a prompt is
retuned.

## What is real here and what is faked

Nothing is faked that the assertions depend on. The project is a **real git-backed
tree on disk**; the planner (#544), the page-object author (#545), the plan
enforcement, the inventory scan, the additive-diff check, the git rollback and the
whole-project collection gate are the **real code paths**, driven through the real
``automation._generate_one``.

Only the three Claude calls are stubbed, and they are stubbed as *obedient readers of
the real prompts* rather than as canned replies — which is what couples the test to
the production prompts instead of to itself:

* :func:`_FakeModels.plan` reads the **inventory block the real planner prompt renders
  from disk** and applies doc §8's decision order (REUSE > EXTEND > CREATE) to it. If
  ``render_inventory`` ever stops advertising an on-disk asset, this stub plans
  ``create`` and the count assertions fail.
* :func:`_FakeModels.author` reads the real authoring prompt's plan block and performs
  **real writes into the real tree** (the pattern ``test_page_object_author.py``
  established), so the writable boundary, ``diff_is_additive``, the collection gate and
  the rollback all run for real against real disk state.
* :func:`_FakeModels.generate` reads the real generation prompt's ``render_plan`` block
  and imports exactly what it says is importable, falling back to inline locators for
  anything ``NOT ON DISK``. If ``render_plan`` stops carrying the reuse decision, specs
  re-inline locators, generated lines per case stop falling, and the trend fails.

The collection gate is not ``lambda: True`` either: :func:`_collects` resolves **every
relative import of every spec in the tree against real files**, which is what makes
"both features' specs still collect under the whole-project gate" an actual assertion
rather than a stub returning ``True``.

## Duplicate detection, now machine-enforced (#571)

This file used to record duplicate detection (doc §21) as the one property enforced
by the **planner prompt** alone: ``normalize`` demoted a *hallucinated* ``reuse`` and
the writable boundary stopped files the plan never authorized, but a plan that
*deliberately* asked to ``create`` ``pages/CreateUserPage.ts`` beside an existing
``pages/UserPage.ts`` was rejected by no code. #571 closed that hole in ``normalize``,
so all three halves are pinned here now: the §21 instruction and the on-disk semantic
owner in the prompt
(:func:`test_the_planner_is_shown_everything_duplicate_detection_needs`), the
resulting file set (the scenario test), and — with the planner replaced by one that
deliberately violates §21 —
:func:`test_a_rogue_plan_cannot_create_a_second_owner_for_one_screen`, which proves
the duplicate never reaches disk even when the model asks for it.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple, Sequence

import pytest

from app.services import automation_gate, placeholder_gate
from app.services import automation_project_service as aps

pytestmark = pytest.mark.usefixtures("workspace_dir")

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


# ---------------------------------------------------------------------------
# The three features — A shares screens with B, and B with C
# ---------------------------------------------------------------------------


class Method(NamedTuple):
    name: str
    args: tuple[str, ...] = ()

    @property
    def signature(self) -> str:
        return f"{self.name}({', '.join(self.args)})"


class Asset(NamedTuple):
    """One asset a feature needs. ``path == ""`` means the base framework owns it."""

    group: str
    name: str
    path: str
    methods: tuple[Method, ...] = ()


class Feature(NamedTuple):
    ticket: str
    name: str
    cases: tuple[tuple[str, str], ...]
    assets: tuple[Asset, ...]


# Both features reach the app through login and a data table — the issue's own
# example of "a different feature sharing screens with A".
_LOGIN = Asset(
    "pages", "LoginPage", "pages/LoginPage.ts",
    (Method("open"), Method("login", ("user", "password"))),
)
_TABLE_METHODS = (Method("rows"), Method("search", ("term",)))
_SORTING = Asset(
    "utils", "tableSorting", "utils/tableSorting.ts",
    (Method("sortColumn", ("page", "column")),),
)
# The base framework owns auth (doc §16/§17) and generic waits (doc §9), so neither may
# ever become a file in this project — doc §21's `utils/waitForDownload.ts` vs
# `helpers/download.ts` example, in the direction where the base package is the owner.
_AUTH_FIXTURE = Asset("fixtures", "authenticatedUser", "")
_DOWNLOAD_WAIT = Asset("utils", "waitForDownload", "")

FEATURE_A = Feature(
    "AUT-1001",
    "Login and user list",
    (("TC-01", "A user signs in and sees the user list"),
     ("TC-02", "A user searches the user list")),
    (_LOGIN,
     Asset("pages", "UserTablePage", "pages/UserTablePage.ts", _TABLE_METHODS),
     _SORTING,
     _AUTH_FIXTURE),
)

FEATURE_B = Feature(
    "AUT-1002",
    "Export users",
    (("TC-01", "A user exports the selected users"),
     ("TC-02", "A user cancels an export")),
    (_LOGIN,
     # The same screen as A, one capability short -> EXTEND, never a second page object.
     Asset("pages", "UserTablePage", "pages/UserTablePage.ts",
           _TABLE_METHODS + (Method("selectRow", ("index",)),)),
     Asset("pages", "ExportDialogPage", "pages/ExportDialogPage.ts",
           (Method("confirmExport"),)),
     _DOWNLOAD_WAIT,
     _AUTH_FIXTURE),
)

FEATURE_C = Feature(
    "AUT-1003",
    "Filter users",
    (("TC-01", "A user filters the user list"),
     ("TC-02", "A user clears the filter")),
    (_LOGIN,
     Asset("pages", "UserTablePage", "pages/UserTablePage.ts", _TABLE_METHODS),
     _SORTING,
     _AUTH_FIXTURE),
)


# ---------------------------------------------------------------------------
# Seeding — one run per feature, all resolving to the SAME persistent project
# ---------------------------------------------------------------------------

_run_counter = 0


def _seed_feature(db_session, feature: Feature):
    """A run carrying one feature's cases, resolving to the shared project key."""
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
    run = Run(code=f"RUN-INC{_run_counter}", name=feature.name, status="review")
    db_session.add(run)
    db_session.flush()

    if db_session.query(Ticket).filter(Ticket.external_id == feature.ticket).first() is None:
        db_session.add(
            Ticket(external_id=feature.ticket, provider_kind="ado", title=feature.name)
        )
    db_session.add(RunTicket(run_id=run.id, ticket_external_id=feature.ticket, position=0))

    cases = []
    for code, title in feature.cases:
        case = TestCase(
            run_id=run.id,
            ticket_external_id=feature.ticket,
            code=code,
            title=title,
            precondition="The application is reachable",
            steps=[{"a": "Sign in", "e": "The user list is shown"}],
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


# ---------------------------------------------------------------------------
# A real collection check, in Python
# ---------------------------------------------------------------------------


def _project_specs(root: Path) -> list[Path]:
    return sorted(
        p for p in Path(root).rglob("*.spec.ts")
        if not {"node_modules", ".git", ".qagent"} & set(p.relative_to(root).parts)
    )


def _collects(root: Path, expect_titles: Sequence[str] = ()) -> tuple[bool, str]:
    """Resolve every relative import of every spec in the tree against real files.

    A stand-in for ``playwright test --list`` that needs no node, reproducing the only
    failure mode the real gate exists to catch here — an import that does not resolve —
    over the **whole project**, so an edit that breaks another ticket's spec fails
    exactly as the real gate would fail it.
    """
    root = Path(root)
    titles: list[str] = []
    for spec in _project_specs(root):
        text = spec.read_text(encoding="utf-8")
        titles += automation_gate.test_titles(text)
        for specifier in placeholder_gate.import_specifiers(text):
            if not specifier.startswith("."):
                continue  # a package, not a project file
            target = (spec.parent / specifier).resolve()
            if not (Path(f"{target}.ts").is_file() or target.is_file()):
                return False, (
                    f"Cannot find module '{specifier}' imported from "
                    f"{spec.relative_to(root).as_posix()}"
                )
    missing = [t for t in expect_titles if t not in titles]
    if missing:
        return False, f"collected, but no test titled {missing[0]!r}"
    return True, f"collected cleanly ({len(titles)} test(s))"


# ---------------------------------------------------------------------------
# The three obedient models
# ---------------------------------------------------------------------------

# `- `pages/LoginPage.ts` (page) exports LoginPage — open(), login(user, password)`
_INVENTORY_LINE = re.compile(r"^- `([^`]+)` \([^)]*\) exports [^—]*— (.*)$", re.MULTILINE)
# `  - `pages/LoginPage.ts` (reuse) — open(), login(user, password)`
_IMPORTABLE_LINE = re.compile(r"^  - `([^`]+)` \(([a-z-]+)\) — (.*)$", re.MULTILINE)
# `- CREATE `pages/LoginPage.ts` (pages, LoginPage)`
_ACTION_LINE = re.compile(r"^- (CREATE|EXTEND) `([^`]+)` \(([^,]+), ([^)]+)\)$", re.MULTILINE)
_NOT_ON_DISK = re.compile(r"^- NOT ON DISK — .*?exception: (.*)$", re.MULTILINE)
_MISSING_ASSET = re.compile(r"([A-Za-z_]\w*) \(`([^`]+)`\)")


def _split_methods(raw: str) -> list[str]:
    """``"open(), login(user, password)"`` -> ``["open()", "login(user, password)"]``.

    Paren-aware: a naive ``split(", ")`` tears a multi-argument signature in half,
    which silently drops the arguments from everything downstream.
    """
    if not raw or raw.strip().startswith("(no method"):
        return []
    out: list[str] = []
    depth = 0
    current = ""
    for char in raw:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            out.append(current.strip())
            current = ""
        else:
            current += char
    out.append(current.strip())
    return [item for item in out if item]


def _method_name(signature: str) -> str:
    return signature.split("(", 1)[0].strip()


def _signature_args(signature: str) -> list[str]:
    inner = signature.partition("(")[2].rpartition(")")[0]
    return [a.strip() for a in inner.split(",") if a.strip()]


def _class_method(method: Method) -> list[str]:
    args = ", ".join(f"{a}: string" for a in method.args)
    return [
        f"  async {method.name}({args}): Promise<void> {{",
        f"    await this.page.getByRole('button', {{ name: '{method.name}' }}).click();",
        "    await this.page.waitForLoadState('load');",
        "  }",
    ]


def _class_source(name: str, methods: Sequence[Method]) -> list[str]:
    lines = [
        "import type { Page } from '@playwright/test';",
        "",
        f"export class {name} {{",
        "  constructor(private readonly page: Page) {}",
    ]
    for method in methods:
        lines.append("")
        lines += _class_method(method)
    lines.append("}")
    return lines


def _function_source(method: Method) -> list[str]:
    args = ", ".join("page: Page" if a == "page" else f"{a}: string" for a in method.args)
    return [
        f"export async function {method.name}({args}): Promise<void> {{",
        f"  await page.getByRole('columnheader', {{ name: '{method.name}' }}).click();",
        "}",
    ]


class _FakeModels:
    """The three Claude calls, each answering from the real prompt it is handed."""

    def __init__(self, features: Sequence[Feature]):
        self.by_ticket = {f.ticket: f for f in features}
        self.planner_prompts: list[str] = []
        self.author_prompts: list[str] = []
        self.generator_prompts: list[str] = []
        # Measurement, reset per feature by `_run_feature`.
        self.library_lines = 0
        self.spec_lines = 0
        self.written: list[str] = []

    # -- the planner (claude_cli.run_json) -------------------------------
    def plan(self, prompt: str, *_a: Any, **_k: Any) -> dict:
        self.planner_prompts.append(prompt)
        ticket = re.search(r"Feature / ticket: (\S+)", prompt).group(1)
        feature = self.by_ticket[ticket]
        inventory = {
            path: [_method_name(s) for s in _split_methods(methods)]
            for path, methods in _INVENTORY_LINE.findall(prompt)
        }
        base_rule_present = "reuse-base" in prompt and "already provides" in prompt

        out: dict[str, Any] = {
            "feature": feature.name,
            "specGroups": [{"name": feature.name.lower().replace(" ", "-"),
                            "testCases": [code for code, _t in feature.cases]}],
            "pages": [], "components": [], "fixtures": [], "data": [], "utils": [],
        }
        for asset in feature.assets:
            if not asset.path:
                # Doc §9/§21: the base framework already provides it. Strip that
                # instruction from the prompt and a model authors a duplicate file —
                # which is exactly what the `else` does, so the test notices.
                if base_rule_present:
                    out[asset.group].append(
                        {"name": asset.name, "action": "reuse-base",
                         "reason": "@q-agent/playwright-base already provides it."}
                    )
                else:
                    out[asset.group].append(
                        {"name": asset.name, "path": f"{asset.group}/{asset.name}.ts",
                         "action": "create", "reason": "No base capability was advertised."}
                    )
                continue

            have = inventory.get(asset.path)
            all_signatures = [m.signature for m in asset.methods]
            if have is None:
                action, methods = "create", all_signatures
                reason = "Nothing in the project owns this screen yet."
            else:
                missing = [m.signature for m in asset.methods if m.name not in have]
                if missing:
                    action, methods = "extend", missing
                    reason = f"`{asset.path}` already owns this screen; add what is missing."
                else:
                    action, methods = "reuse", all_signatures
                    reason = f"`{asset.path}` already provides everything this feature needs."
            out[asset.group].append(
                {"name": asset.name, "path": asset.path, "action": action,
                 "methods": methods, "reason": reason}
            )
        return out

    # -- the page-object author (claude_cli.run_agentic) -----------------
    def author(self, prompt: str, **kwargs: Any) -> str:
        self.author_prompts.append(prompt)
        root = Path(kwargs["workspace_dir"])
        done: list[str] = []
        for verb, path, group, name in _ACTION_LINE.findall(prompt):
            block = prompt.split(f"`{path}` ({group}, {name})", 1)[1]
            methods = _split_methods(
                re.search(r"methods to provide: (.*)", block).group(1).strip()
            )
            self._write_asset(root, verb, path, group, name, methods)
            done.append(path)
        return ("wrote " + ", ".join(sorted(done))) if done else "nothing to do"

    def _write_asset(
        self, root: Path, verb: str, path: str, group: str, name: str, methods: Sequence[str]
    ) -> None:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        parsed = tuple(Method(_method_name(s), tuple(_signature_args(s))) for s in methods)
        is_class = group in ("pages", "components")

        if verb == "CREATE" or not target.is_file():
            lines = _class_source(name, parsed) if is_class else (
                ["import type { Page } from '@playwright/test';", ""]
                + [line for m in parsed for line in _function_source(m) + [""]]
            )
            target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
            self.library_lines += len([line for line in lines if line.strip()])
            self.written.append(path)
            return

        # EXTEND — strictly additive, or `diff_is_additive` reverts the whole pass.
        text = target.read_text(encoding="utf-8")
        added: list[str] = []
        for method in parsed:
            added += [""] + (_class_method(method) if is_class else _function_source(method))
        if is_class:
            head, _sep, _tail = text.rstrip().rpartition("}")
            text = head.rstrip("\n") + "\n" + "\n".join(added) + "\n}\n"
        else:
            text = text.rstrip() + "\n" + "\n".join(added) + "\n"
        target.write_text(text, encoding="utf-8")
        self.library_lines += len([line for line in added if line.strip()])
        self.written.append(path)

    # -- the spec generator (claude_cli.run_prompt) ----------------------
    def generate(self, prompt: str, *_a: Any, **_k: Any) -> str:
        self.generator_prompts.append(prompt)
        code = re.search(r"^Test Case ID: (\S+)$", prompt, re.MULTILINE).group(1)
        title = re.search(r"^Title: (.*)$", prompt, re.MULTILINE).group(1).strip()

        imports = ["import { test, expect } from '@q-agent/playwright-base';"]
        body: list[str] = []
        for path, _action, raw_methods in _IMPORTABLE_LINE.findall(prompt):
            stem = Path(path).stem
            methods = _split_methods(raw_methods)
            if path.startswith(("pages/", "components/")):
                imports.append(f"import {{ {stem} }} from '../../{path[:-3]}';")
                var = stem[0].lower() + stem[1:]
                body.append(f"  const {var} = new {stem}(page);")
                for signature in methods:
                    args = ", ".join(f"'{a}'" for a in _signature_args(signature))
                    body.append(f"  await {var}.{_method_name(signature)}({args});")
            else:
                names = [_method_name(s) for s in methods]
                imports.append(f"import {{ {', '.join(names)} }} from '../../{path[:-3]}';")
                for signature in methods:
                    args = ", ".join(
                        "page" if a == "page" else f"'{a}'" for a in _signature_args(signature)
                    )
                    body.append(f"  await {_method_name(signature)}({args});")

        # Anything the plan named but that is NOT on disk falls back to an inline
        # locator — the cost of not reusing, made visible in the line count.
        offline = _NOT_ON_DISK.search(prompt)
        for name, _path in _MISSING_ASSET.findall(offline.group(1) if offline else ""):
            body.append(f"  await page.getByRole('button', {{ name: '{name}' }}).click();")
            body.append("  await expect(page.getByRole('heading')).toBeVisible();")

        lines = [
            *imports,
            "",
            f"test('{code} — {title}', async ({{ page }}) => {{",
            *body,
            "  await expect(page).toHaveURL(/.*/);",
            "});",
        ]
        self.spec_lines += len([line for line in lines if line.strip()])
        return "```typescript\n" + "\n".join(lines) + "\n```"


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def incremental(monkeypatch):
    """Wire the three obedient models plus the real-disk collection gate."""
    from app.routers import automation as automation_router
    from app.services import claude_cli, failure_classifier

    models = _FakeModels((FEATURE_A, FEATURE_B, FEATURE_C))
    gate_calls: list[dict] = []

    def gate(project_dir, expect_titles):
        ok, detail = _collects(Path(project_dir), list(expect_titles))
        gate_calls.append(
            {
                "dir": Path(project_dir),
                "titles": list(expect_titles),
                "ok": ok,
                "specs": sorted(
                    p.relative_to(project_dir).as_posix()
                    for p in _project_specs(Path(project_dir))
                ),
            }
        )
        return ok, detail

    monkeypatch.setattr(claude_cli, "run_json", models.plan)
    monkeypatch.setattr(claude_cli, "run_agentic", models.author)
    monkeypatch.setattr(claude_cli, "run_prompt", models.generate)
    monkeypatch.setattr(automation_router, "_run_automation_review", lambda *a, **k: None)
    monkeypatch.setattr(aps, "ensure_deps", lambda *a, **k: "cached")
    monkeypatch.setattr(automation_gate, "list_ok_in_project", gate)
    # `tsc --noEmit` (#546) has dedicated coverage in `test_automation_gate.py`; shelling
    # out to a real tsc per generation would make this suite slow and machine-dependent.
    monkeypatch.setattr(automation_gate, "typecheck_ok", lambda _dir: (True, "stubbed"))
    monkeypatch.setattr(
        failure_classifier, "classify_failure",
        lambda *a, **k: {"failureClass": "test_defect", "suspectedProductDefect": False,
                         "reason": ""},
    )
    return SimpleNamespace(models=models, gate_calls=gate_calls, monkeypatch=monkeypatch)


# ---------------------------------------------------------------------------
# Driving a whole feature and measuring what it cost
# ---------------------------------------------------------------------------


class Measured(NamedTuple):
    feature: Feature
    run_code: str
    plan: dict
    counts: dict[str, int]
    created: list[str]
    library_lines: int
    spec_lines: int
    specs: list[str]
    project_id: int

    @property
    def cases(self) -> int:
        return len(self.feature.cases)

    @property
    def total_lines(self) -> int:
        return self.library_lines + self.spec_lines

    @property
    def lines_per_case(self) -> float:
        return self.total_lines / self.cases

    @property
    def reuse_rate(self) -> float:
        decided = sum(self.counts[a] for a in ("reuse", "extend", "create"))
        return (self.counts["reuse"] / decided) if decided else 0.0

    @property
    def actions(self) -> dict[str, str]:
        return {
            entry["name"]: entry["action"]
            for group in ("pages", "components", "fixtures", "data", "utils")
            for entry in self.plan[group]
        }


def _run_feature(db_session, harness, feature: Feature) -> Measured:
    """Generate every case of ``feature`` through the real pipeline, and measure it."""
    from app.routers import automation as automation_router

    models = harness.models
    models.library_lines = 0
    models.spec_lines = 0
    models.written = []

    run, cases = _seed_feature(db_session, feature)
    specs = []
    for case in cases:
        spec = automation_router._generate_one(db_session, run, case)
        db_session.commit()
        db_session.refresh(spec)
        assert spec.status == "draft", f"{feature.ticket} {case.code}: {spec.block_reason}"
        specs.append(spec)

    plan = json.loads(specs[-1].plan_report)
    return Measured(
        feature=feature,
        run_code=run.code,
        plan=plan,
        counts=plan["counts"],
        created=sorted(set(models.written)),
        library_lines=models.library_lines,
        spec_lines=models.spec_lines,
        specs=[s.filename for s in specs],
        project_id=specs[0].project_id,
    )


def _library(project) -> set[str]:
    return {entry["path"] for entry in aps.inventory(project)}


def _project(db_session, project_id):
    from app.models.automation_project import AutomationProject

    return db_session.get(AutomationProject, project_id)


# ---------------------------------------------------------------------------
# THE acceptance test
# ---------------------------------------------------------------------------


@requires_git
def test_feature_b_reuses_feature_a_and_generates_less_code(db_session, incremental):
    """Doc §19, end to end: A into an EMPTY project, then B and C into the same one.

    Every number below comes out of the real pipeline — the reuse decisions from
    ``plan_report``'s actions, the file set from the real tree, the line counts from
    what the (obedient) models actually wrote.
    """
    a = _run_feature(db_session, incremental, FEATURE_A)
    b = _run_feature(db_session, incremental, FEATURE_B)
    c = _run_feature(db_session, incremental, FEATURE_C)

    # The epic's metric, printed so `pytest -s` reports it rather than only asserting it.
    for label, m in (("A", a), ("B", b), ("C", c)):
        print(
            f"feature {label} {m.feature.ticket}: created={m.created} "
            f"library_lines={m.library_lines} spec_lines={m.spec_lines} "
            f"lines/case={m.lines_per_case} counts={m.counts} reuse_rate={m.reuse_rate:.2f}"
        )

    assert a.project_id == b.project_id == c.project_id, "all three share one project"
    project = _project(db_session, a.project_id)
    root = aps.project_dir(project)

    # -- A: the empty project. Everything is genuinely new (doc §8.3). ---------
    assert a.counts == {"reuse": 0, "extend": 0, "create": 3, "reuse-base": 1}
    assert a.created == [
        "pages/LoginPage.ts", "pages/UserTablePage.ts", "utils/tableSorting.ts"
    ]

    # -- B: shares login and the data table with A. ----------------------------
    # Read off the plan's ACTIONS, not by eyeballing the tree.
    assert b.actions["LoginPage"] == "reuse"               # doc §8.1
    assert b.actions["UserTablePage"] == "extend"          # doc §8.2, not a 2nd page object
    assert b.actions["ExportDialogPage"] == "create"       # doc §8.3, genuinely new
    assert b.actions["authenticatedUser"] == "reuse-base"  # the base fixtures
    assert b.actions["waitForDownload"] == "reuse-base"
    assert b.counts == {"reuse": 1, "extend": 1, "create": 1, "reuse-base": 2}

    # The reuse is a real import, authorized off the real tree.
    assert "pages/LoginPage.ts" in b.plan["importable"]
    assert b.plan["writable"] == ["pages/ExportDialogPage.ts", "pages/UserTablePage.ts"]
    for spec in b.specs:
        assert "../../pages/LoginPage" in (root / spec).read_text(encoding="utf-8")

    # B creates ONLY genuinely new assets — LoginPage is untouched.
    assert b.created == ["pages/ExportDialogPage.ts", "pages/UserTablePage.ts"]
    assert "pages/LoginPage.ts" not in b.created
    # ...and the extend was additive: A's methods survived verbatim.
    table = (root / "pages" / "UserTablePage.ts").read_text(encoding="utf-8")
    assert "async rows()" in table and "async search(term: string)" in table
    assert "async selectRow(index: string)" in table

    # -- Duplicate detection (doc §21) ----------------------------------------
    pages = sorted(p.name for p in (root / "pages").glob("*.ts"))
    assert pages == ["ExportDialogPage.ts", "LoginPage.ts", "UserTablePage.ts"], (
        "a second page object for a screen A already covers is the §21 failure"
    )
    # One owner per screen: no `CreateUserPage.ts` beside `UserPage.ts`, in any spelling.
    assert [p for p in pages if "UserTable" in p] == ["UserTablePage.ts"]
    # No duplicate utility for a capability the base framework already provides: the
    # download wait stayed `reuse-base`, so `utils/` never gained a file for it and
    # `fixtures/` never gained one for auth.
    assert sorted(p.name for p in (root / "utils").glob("*.ts")) == ["tableSorting.ts"]
    assert not list((root / "fixtures").glob("*.ts"))

    # -- C: a third feature keeps the reuse rate at or above B's --------------
    assert c.counts == {"reuse": 3, "extend": 0, "create": 0, "reuse-base": 1}
    assert c.created == []
    assert c.reuse_rate >= b.reuse_rate > a.reuse_rate

    # -- The success metric: generated lines per case trends DOWN -------------
    assert a.lines_per_case > b.lines_per_case > c.lines_per_case, (
        f"A={a.lines_per_case} B={b.lines_per_case} C={c.lines_per_case}"
    )
    assert b.library_lines < a.library_lines
    assert c.library_lines == 0, "a fully reuse-only feature writes no library code"

    # -- Both features' specs still collect under the whole-project gate ------
    ok, detail = _collects(root)
    assert ok, detail
    on_disk = {p.relative_to(root).as_posix() for p in _project_specs(root)}
    assert on_disk == set(a.specs) | set(b.specs) | set(c.specs)
    assert len(on_disk) == 6
    # The real gate collected the whole tree on the last pass, A's specs included.
    last = incremental.gate_calls[-1]
    assert last["dir"] == root
    assert set(a.specs) <= set(last["specs"])


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


@requires_git
def test_regenerating_feature_a_duplicates_nothing(db_session, incremental):
    """Generating feature A twice must not duplicate its assets.

    Two distinct shapes of "twice", both of which bite real generators:

    1. the **same run** again — the on-disk plan's ``authoredAt`` stamp has to stop the
       (paid) editor re-running per case;
    2. a **new run** over the same ticket — a full replan, which now sees A's own
       assets in the inventory and must therefore decide ``reuse``, not ``create``.
    """
    from app.models.run import Run
    from app.models.testcase import TestCase
    from app.routers import automation as automation_router

    first = _run_feature(db_session, incremental, FEATURE_A)
    project = _project(db_session, first.project_id)
    root = aps.project_dir(project)
    library = _library(project)
    assert library == {
        "pages/LoginPage.ts", "pages/UserTablePage.ts", "utils/tableSorting.ts"
    }
    fingerprint = {path: (root / path).read_text(encoding="utf-8") for path in sorted(library)}

    # (1) The same run, the same cases, again.
    run = db_session.query(Run).filter(Run.code == first.run_code).one()
    cases = (
        db_session.query(TestCase)
        .filter(TestCase.run_id == run.id)
        .order_by(TestCase.id)
        .all()
    )
    incremental.models.written = []
    authored_before = len(incremental.models.author_prompts)
    for case in cases:
        spec = automation_router._generate_one(db_session, run, case)
        db_session.commit()
        assert spec.status == "draft", spec.block_reason
    assert incremental.models.written == [], "a re-run must not rewrite the library"
    assert len(incremental.models.author_prompts) == authored_before, (
        "the authoredAt stamp must stop the editor re-running"
    )

    # (2) A brand-new run over the same ticket — a genuine replan against A's own tree.
    second = _run_feature(db_session, incremental, FEATURE_A)
    assert second.counts == {"reuse": 3, "extend": 0, "create": 0, "reuse-base": 1}
    assert second.created == []
    assert second.library_lines == 0
    assert second.lines_per_case < first.lines_per_case

    # The library is unchanged, byte for byte — no `LoginPage2.ts`, no rewrite.
    assert _library(project) == library
    for path, text in fingerprint.items():
        assert (root / path).read_text(encoding="utf-8") == text
    assert sorted(p.name for p in (root / "pages").glob("*.ts")) == [
        "LoginPage.ts", "UserTablePage.ts"
    ]
    # ...and everything still collects, both passes' specs included.
    ok, detail = _collects(root)
    assert ok, detail


# ---------------------------------------------------------------------------
# The cost control, and what the planner is actually shown
# ---------------------------------------------------------------------------


@requires_git
def test_a_reuse_only_feature_costs_no_authoring_call(db_session, incremental):
    """The epic's cost lever: a feature that reuses everything pays for no editor.

    ``claude_cli.run_agentic`` is the single authoring path (and ``run_prompt`` the only
    writer of ``ClaudeUsage`` rows), so "no agentic call" is asserted at the call
    itself — the strongest available form of the property.
    """
    _run_feature(db_session, incremental, FEATURE_A)
    authored = len(incremental.models.author_prompts)

    c = _run_feature(db_session, incremental, FEATURE_C)  # reuse-only against A's library

    assert c.counts["create"] == 0 and c.counts["extend"] == 0
    assert len(incremental.models.author_prompts) == authored, (
        "a reuse-only plan must not run the project editor"
    )
    assert c.library_lines == 0


@requires_git
def test_the_planner_is_shown_everything_duplicate_detection_needs(db_session, incremental):
    """Doc §21's *first* line of defence is the planner PROMPT, so the prompt is pinned.

    #571 added the second line (``normalize`` demotes a duplicate ``create``, see
    :func:`test_a_rogue_plan_cannot_create_a_second_owner_for_one_screen`), but that
    guard is a conservative name heuristic by design — it cannot catch a duplicate
    called something wholly different. Keeping the planner *told the rule* and *shown
    the semantic owner already on disk* therefore stays load-bearing, and a prompt
    retune dropping either half is exactly how the property erodes.
    """
    _run_feature(db_session, incremental, FEATURE_A)
    # A: nothing to reuse, and the prompt says so in as many words.
    assert "shared library is EMPTY" in incremental.models.planner_prompts[0]

    _run_feature(db_session, incremental, FEATURE_B)
    prompt = incremental.models.planner_prompts[-1]

    # The real, on-disk inventory, with the signatures the files really export.
    assert "PROJECT INVENTORY" in prompt
    inventory = dict(_INVENTORY_LINE.findall(prompt))
    assert set(inventory) == {
        "pages/LoginPage.ts", "pages/UserTablePage.ts", "utils/tableSorting.ts"
    }
    assert "login(user, password)" in inventory["pages/LoginPage.ts"]
    # The §21 rule itself, with both of the doc's examples.
    assert "Duplicate detection" in prompt
    assert "CreateUserPage" in prompt and "waitForDownload" in prompt
    # The base-framework rule, without which auth and waits become project files.
    assert "reuse-base" in prompt and "already provides" in prompt
    # Doc §22: prefer extending the existing owner over a duplicate locator elsewhere.
    assert "Locator reuse" in prompt

    # The generation prompt carries the decision through, which is what makes the spec
    # import the reused page object instead of re-inlining its locators.
    generation = incremental.models.generator_prompts[-1]
    assert "AUTOMATION PLAN" in generation
    assert "`pages/LoginPage.ts` (reuse)" in generation


@requires_git
def test_a_rogue_plan_cannot_create_a_second_owner_for_one_screen(db_session, incremental):
    """Doc §21, machine-enforced end to end (#571): the model asks, the pipeline refuses.

    The obedient planner is replaced, for one ticket only, by one that **deliberately**
    violates §21 — it asks to ``create`` ``pages/CreateUserTablePage.ts`` while A's
    ``pages/UserTablePage.ts`` already owns that screen. Everything downstream is the
    real code path, so this measures the guarantee rather than the heuristic: the
    duplicate path is never authorized (``writable``), never authored, and the
    capability lands on the existing owner instead.
    """
    from app.models.run import Run
    from app.routers import automation as automation_router
    from app.services import claude_cli

    a = _run_feature(db_session, incremental, FEATURE_A)
    project = _project(db_session, a.project_id)
    root = aps.project_dir(project)

    rogue_feature = Feature(
        "AUT-1004", "Bulk create users",
        (("TC-01", "A user bulk-creates users from the user list"),), (),
    )
    rogue_plan = {
        "feature": rogue_feature.name,
        "specGroups": [{"name": "bulk-create", "testCases": ["TC-01"]}],
        "pages": [
            {
                "name": "CreateUserTablePage",
                "path": "pages/CreateUserTablePage.ts",
                "action": "create",
                "methods": ["bulkCreate(count)"],
                "reason": "This feature creates users, so it gets its own page object.",
            }
        ],
        "components": [], "fixtures": [], "data": [], "utils": [],
    }
    incremental.monkeypatch.setattr(claude_cli, "run_json", lambda *a, **k: rogue_plan)

    run, cases = _seed_feature(db_session, rogue_feature)
    run = db_session.query(Run).filter(Run.code == run.code).one()
    spec = automation_router._generate_one(db_session, run, cases[0])
    db_session.commit()
    db_session.refresh(spec)
    assert spec.status == "draft", spec.block_reason

    plan = json.loads(spec.plan_report)
    entry = plan["pages"][0]
    assert entry["action"] == "extend", "the §21 duplicate is demoted, not authored"
    assert entry["path"] == "pages/UserTablePage.ts"
    assert entry["duplicateOf"] == "pages/UserTablePage.ts"
    assert entry["plannedPath"] == "pages/CreateUserTablePage.ts"
    assert plan["writable"] == ["pages/UserTablePage.ts"], (
        "the duplicate path was never authorized for writing"
    )
    assert plan["duplicatesDemoted"] == 1

    # The tree: one owner for the screen, carrying the new capability.
    assert sorted(p.name for p in (root / "pages").glob("*.ts")) == [
        "LoginPage.ts", "UserTablePage.ts"
    ]
    table = (root / "pages" / "UserTablePage.ts").read_text(encoding="utf-8")
    assert "async bulkCreate(count: string)" in table
    assert "async rows()" in table, "the extend stayed additive"
    # ...and the spec drives the screen through its real owner.
    assert "../../pages/UserTablePage" in (root / spec.filename).read_text(encoding="utf-8")
    ok, detail = _collects(root)
    assert ok, detail


@requires_git
def test_the_counters_are_machine_checked_per_ticket(db_session, incremental):
    """The success metric is a persisted number — on the row and on disk, not a log line."""
    from app.services import automation_planner_service as planner

    a = _run_feature(db_session, incremental, FEATURE_A)
    b = _run_feature(db_session, incremental, FEATURE_B)
    project = _project(db_session, a.project_id)

    for measured in (a, b):
        path = planner.plan_path(project, measured.run_code, measured.feature.ticket)
        assert path.is_file(), "the plan is persisted per TICKET under .qagent/plans/"
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["counts"] == measured.counts, "both plan copies must agree"
        assert on_disk["ticket"] == measured.feature.ticket
        assert on_disk["cases"] == [code for code, _t in measured.feature.cases]
        assert on_disk["authoredAt"], "the once-per-ticket authoring stamp"

    # Two cases on one ticket share ONE planning call — the cost lever.
    assert len(incremental.models.planner_prompts) == 2, "one plan per ticket, not per case"
    # One audit row per file the editor touched, carrying the plan action behind it.
    assert _library_audit(db_session) == {
        ("pages/LoginPage.ts", "create"),
        ("pages/UserTablePage.ts", "create"),
        ("utils/tableSorting.ts", "create"),
        ("pages/UserTablePage.ts", "extend"),
        ("pages/ExportDialogPage.ts", "create"),
    }

    # And the metric moved in the right direction.
    assert b.counts["reuse"] + b.counts["extend"] > a.counts["reuse"] + a.counts["extend"]
    assert b.counts["create"] < a.counts["create"]


def _library_audit(db_session) -> set[tuple[str, str]]:
    """``{(path, planAction)}`` from the audit trail #545 writes per file touched."""
    from app.models.audit import AuditLog

    rows = db_session.query(AuditLog).filter(AuditLog.category == "ai").all()
    out: set[tuple[str, str]] = set()
    for row in rows:
        detail = row.detail if isinstance(row.detail, dict) else {}
        if detail.get("path"):
            out.add((detail["path"], detail.get("planAction") or ""))
    return out
