"""``tsc --noEmit`` — the second static gate (#546).

``playwright test --list`` (#540) transpiles with **esbuild**, which erases types
without checking them. It catches missing modules and syntax errors but is
*structurally* blind to the failure mode the layered architecture introduces: a spec
calling ``userFormPage.fillUsre(...)`` or passing the wrong argument shape to a page
object collects perfectly cleanly. Before this epic specs were self-contained and there
were no cross-file call signatures to get wrong; now there are.

Two kinds of coverage here:

* **Unit** — the diagnostic classifier and every fail-open branch, with ``subprocess``
  stubbed, so they run everywhere.
* **Real ``tsc``** (``needs_tsc``) — an actual compiler over an actual two-file layered
  project, proving the motivating case rather than asserting on a mock. Skipped when no
  local TypeScript install is resolvable, which is also the gate's own fail-open case.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import automation_gate, automation_project_service

# ---------------------------------------------------------------------------
# A minimal layered project: one page object + one spec-shaped consumer.
# Deliberately free of `@playwright/test` and `@types/node` so a real `tsc` can
# check it with nothing installed — the type error must be the ONLY diagnostic.
# ---------------------------------------------------------------------------

PAGE_OBJECT = """export interface UserPayload {
  firstName: string;
  age: number;
}

export class UserFormPage {
  async fillUser(payload: UserPayload): Promise<void> {
    void payload;
  }
}
"""

GOOD_SPEC = """import { UserFormPage } from '../pages/UserFormPage';

export async function run(): Promise<void> {
  const userFormPage = new UserFormPage();
  await userFormPage.fillUser({ firstName: 'Ada', age: 36 });
}
"""

# The exact case from the issue: `fillUsre` for `fillUser`. esbuild does not care.
MISSPELLED_SPEC = GOOD_SPEC.replace("fillUser(", "fillUsre(")

# Wrong argument shape: `age` as a string where the payload declares a number.
WRONG_ARGS_SPEC = GOOD_SPEC.replace("age: 36", "age: '36'")

# Derived from the **real** scaffold rather than hand-copied, so these real-tsc tests
# exercise the config that actually ships (#562 changed it once already after a
# hand-copied duplicate silently drifted). The single override is `types: []`: this
# project deliberately installs nothing, so demanding `@types/node` would add
# environmental noise and the type error must be the ONLY diagnostic.
_SCAFFOLD_TSCONFIG = json.loads(automation_project_service._TSCONFIG)
_MINIMAL_TSCONFIG = json.dumps(
    {
        **_SCAFFOLD_TSCONFIG,
        "compilerOptions": {**_SCAFFOLD_TSCONFIG["compilerOptions"], "types": []},
    },
    indent=2,
)


def _write_project(root: Path, spec: str) -> Path:
    (root / "pages").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tsconfig.json").write_text(_MINIMAL_TSCONFIG, encoding="utf-8")
    (root / "pages" / "UserFormPage.ts").write_text(PAGE_OBJECT, encoding="utf-8")
    (root / "tests" / "TC-01.spec.ts").write_text(spec, encoding="utf-8")
    return root


# Resolved ONCE at import, via the gate's own strategy. Captured as a constant so the
# real-tsc tests can point `resolve_tsc_bin` at it without recursing into themselves.
_TSC: str | None = automation_gate.resolve_tsc_bin(Path.cwd())

needs_tsc = pytest.mark.skipif(
    _TSC is None, reason="no local TypeScript install (the gate's fail-open case)"
)


# ---------------------------------------------------------------------------
# Binary resolution
# ---------------------------------------------------------------------------


def test_resolve_tsc_bin_prefers_the_project_then_the_server_install(tmp_path, monkeypatch):
    """Project's own node_modules first, server-wide second, never a bare npx."""
    server = tmp_path / "server-node-modules"
    (server / ".bin").mkdir(parents=True)
    (server / ".bin" / "tsc").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(settings, "playwright_node_modules", server)

    project = tmp_path / "proj"
    project.mkdir()
    # Only the server-wide install exists → fall back to it.
    assert automation_gate.resolve_tsc_bin(project) == str(server / ".bin" / "tsc")

    own = project / "node_modules" / ".bin"
    own.mkdir(parents=True)
    (own / "tsc").write_text("#!/bin/sh\n", encoding="utf-8")
    # Now the project's own install wins.
    assert automation_gate.resolve_tsc_bin(project) == str(own / "tsc")


def test_resolve_tsc_bin_never_falls_back_to_npx(tmp_path, monkeypatch):
    """A bare `npx` could trigger a network fetch and hang, so there is no fallback."""
    monkeypatch.setattr(settings, "playwright_node_modules", tmp_path / "nope")
    assert automation_gate.resolve_tsc_bin(tmp_path / "proj") is None


# ---------------------------------------------------------------------------
# Fail-open — an incomplete toolchain must never block generation
# ---------------------------------------------------------------------------


def test_typecheck_fails_open_without_a_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(automation_gate, "resolve_tsc_bin", lambda _dir: None)
    ok, detail = automation_gate.typecheck_ok(tmp_path)
    assert ok is True
    assert detail.startswith("skipped")


def test_typecheck_fails_open_on_a_launch_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(automation_gate, "resolve_tsc_bin", lambda _dir: "tsc")

    def boom(*_args, **_kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(automation_gate.subprocess, "run", boom)
    ok, detail = automation_gate.typecheck_ok(tmp_path)
    assert ok is True
    assert "OSError" in detail


def test_typecheck_fails_open_on_a_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(automation_gate, "resolve_tsc_bin", lambda _dir: "tsc")

    def slow(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="tsc", timeout=1)

    monkeypatch.setattr(automation_gate.subprocess, "run", slow)
    ok, _detail = automation_gate.typecheck_ok(tmp_path)
    assert ok is True


@pytest.mark.parametrize(
    "output",
    [
        # No @types/node — the scaffolded tsconfig declares `types: ["node"]`, so a
        # project whose `npm install` never ran trips this on EVERY file.
        "error TS2688: Cannot find type definition file for 'node'.",
        # Deps not installed: a BARE specifier cannot resolve.
        "tests/a.spec.ts(1,25): error TS2307: Cannot find module '@playwright/test' or its "
        "corresponding type declarations.",
        "tests/a.spec.ts(2,25): error TS2307: Cannot find module '@q-agent/playwright-base'.",
        "tests/a.spec.ts(4,3): error TS2584: Cannot find name 'document'.",
        "error TS5083: Cannot read file 'tsconfig.json'.",
        # Measured, not hypothetical: TypeScript 7 REMOVED `moduleResolution: "node"`.
        # #562 fixed the scaffold so we no longer *emit* this, but the classifier stays:
        # it is cheap insurance against the next option removal, and it still covers
        # projects on disk that predate the migration. A server resolving a v7 tsc would
        # otherwise emit this once and reject every spec forever.
        "tsconfig.json(5,25): error TS5108: Option 'moduleResolution=node10' has been "
        "removed. Please remove it from your configuration.",
    ],
)
def test_typecheck_fails_open_on_environmental_diagnostics(tmp_path, monkeypatch, output):
    """A missing toolchain is not the spec's fault — rejecting here would fail CLOSED."""
    monkeypatch.setattr(automation_gate, "resolve_tsc_bin", lambda _dir: "tsc")
    monkeypatch.setattr(
        automation_gate.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=2, stdout=output, stderr=""),
    )
    ok, detail = automation_gate.typecheck_ok(tmp_path)
    assert ok is True
    assert detail.startswith("skipped")


def test_typecheck_fails_open_when_tsc_output_is_unparsable(tmp_path, monkeypatch):
    """rc != 0 with nothing diagnostic-shaped means tsc itself misbehaved."""
    monkeypatch.setattr(automation_gate, "resolve_tsc_bin", lambda _dir: "tsc")
    monkeypatch.setattr(
        automation_gate.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="Killed"),
    )
    ok, detail = automation_gate.typecheck_ok(tmp_path)
    assert ok is True
    assert detail.startswith("skipped")


def test_a_relative_missing_module_is_a_real_rejection(tmp_path, monkeypatch):
    """TS2307 is environmental only for a BARE specifier. `../pages/Foo` is on us."""
    monkeypatch.setattr(automation_gate, "resolve_tsc_bin", lambda _dir: "tsc")
    monkeypatch.setattr(
        automation_gate.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=2,
            stdout="tests/a.spec.ts(1,30): error TS2307: Cannot find module '../pages/Nope'.",
            stderr="",
        ),
    )
    ok, detail = automation_gate.typecheck_ok(tmp_path)
    assert ok is False
    assert "../pages/Nope" in detail


def test_a_real_error_rejects_even_alongside_environmental_noise(tmp_path, monkeypatch):
    """One definitive type error is enough; the environmental lines are filtered out."""
    monkeypatch.setattr(automation_gate, "resolve_tsc_bin", lambda _dir: "tsc")
    monkeypatch.setattr(
        automation_gate.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=2,
            stdout=(
                "error TS2688: Cannot find type definition file for 'node'.\n"
                "tests/a.spec.ts(9,21): error TS2551: Property 'fillUsre' does not exist on "
                "type 'UserFormPage'. Did you mean 'fillUser'?\n"
            ),
            stderr="",
        ),
    )
    ok, detail = automation_gate.typecheck_ok(tmp_path)
    assert ok is False
    assert "fillUsre" in detail
    assert "TS2688" not in detail


def test_typecheck_reports_its_latency(tmp_path, monkeypatch):
    """Recorded in the detail, so the added gate latency lands in `gate_report`."""
    monkeypatch.setattr(automation_gate, "resolve_tsc_bin", lambda _dir: "tsc")
    monkeypatch.setattr(
        automation_gate.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    ok, detail = automation_gate.typecheck_ok(tmp_path)
    assert ok is True
    assert detail.endswith("ms)")


def test_typecheck_uses_the_projects_own_tsconfig(tmp_path, monkeypatch):
    """cwd = the project root, so tsc picks up its `tsconfig.json` and `include`."""
    monkeypatch.setattr(automation_gate, "resolve_tsc_bin", lambda _dir: "tsc")
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["cwd"] = kwargs.get("cwd")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(automation_gate.subprocess, "run", fake_run)
    automation_gate.typecheck_ok(tmp_path)
    assert seen["cwd"] == str(tmp_path)
    assert "--noEmit" in seen["cmd"]
    # `--pretty false` keeps ANSI colour codes out of the diagnostics we parse.
    assert "--pretty" in seen["cmd"] and "false" in seen["cmd"]
    # No `--project`: the project's own tsconfig is discovered from cwd.
    assert "--project" not in seen["cmd"]


# ---------------------------------------------------------------------------
# Real tsc — the motivating case, proven rather than mocked
# ---------------------------------------------------------------------------


def test_the_real_tsc_tests_run_against_the_shipped_scaffold_config():
    """Guards the derivation above: these tests must not drift from what we scaffold.

    The whole point of the real-tsc suite is that the config the gate meets in production
    is the config proven here. A hand-copied duplicate would let #562 regress invisibly.
    """
    options = json.loads(_MINIMAL_TSCONFIG)["compilerOptions"]
    shipped = _SCAFFOLD_TSCONFIG["compilerOptions"]
    assert options["module"] == shipped["module"] == "NodeNext"
    assert options["moduleResolution"] == shipped["moduleResolution"] == "NodeNext"


@needs_tsc
def test_real_tsc_accepts_a_correct_layered_project(tmp_path, monkeypatch):
    monkeypatch.setattr(automation_gate, "resolve_tsc_bin", lambda _dir: _TSC)
    ok, detail = automation_gate.typecheck_ok(_write_project(tmp_path, GOOD_SPEC))
    assert ok is True, detail
    assert "clean" in detail


@needs_tsc
def test_real_tsc_rejects_a_misspelled_page_object_method(tmp_path, monkeypatch):
    """THE motivating case: `--list`/esbuild collects this; only tsc sees it."""
    monkeypatch.setattr(automation_gate, "resolve_tsc_bin", lambda _dir: _TSC)
    ok, detail = automation_gate.typecheck_ok(_write_project(tmp_path, MISSPELLED_SPEC))
    assert ok is False
    # The tsc error is surfaced verbatim, which is what lands in `gate_report`.
    assert "fillUsre" in detail
    assert "TS2551" in detail


@needs_tsc
def test_real_tsc_rejects_a_wrong_argument_type(tmp_path, monkeypatch):
    monkeypatch.setattr(automation_gate, "resolve_tsc_bin", lambda _dir: _TSC)
    ok, detail = automation_gate.typecheck_ok(_write_project(tmp_path, WRONG_ARGS_SPEC))
    assert ok is False
    assert "TS2322" in detail or "TS2345" in detail


@needs_tsc
def test_real_tsc_catches_a_page_object_edit_that_breaks_another_spec(tmp_path, monkeypatch):
    """Checking the WHOLE project is the defence against shared-project coupling."""
    monkeypatch.setattr(automation_gate, "resolve_tsc_bin", lambda _dir: _TSC)
    root = _write_project(tmp_path, GOOD_SPEC)
    # A "helpful" refactor renames the page-object method. The candidate spec is
    # untouched and perfectly valid on its own — the project no longer typechecks.
    (root / "pages" / "UserFormPage.ts").write_text(
        PAGE_OBJECT.replace("fillUser(", "populate("), encoding="utf-8"
    )
    ok, detail = automation_gate.typecheck_ok(root)
    assert ok is False
    assert "fillUser" in detail


# ---------------------------------------------------------------------------
# Wiring — `_gate_spec_or_bypass`
# ---------------------------------------------------------------------------


@pytest.fixture()
def gate_wiring(monkeypatch, tmp_path):
    """`_gate_spec_or_bypass` with the deterministic gate and `--list` both passing.

    Leaves exactly one variable in play: what `typecheck_ok` returns.
    """
    from app.routers import automation as automation_router
    from app.services import automation_project_service, placeholder_gate, settings_store

    monkeypatch.setattr(settings_store, "gate_enabled", lambda: True)
    monkeypatch.setattr(
        placeholder_gate,
        "gate_spec",
        lambda *a, **k: {"outcome": "passed", "findings": [], "reason": "", "unblock_action": ""},
    )
    monkeypatch.setattr(automation_gate, "list_ok_in_project", lambda *a, **k: (True, "clean"))
    monkeypatch.setattr(automation_project_service, "project_dir", lambda _p: tmp_path)
    return SimpleNamespace(
        call=lambda **kw: automation_router._gate_spec_or_bypass(
            "test('x', async () => {});",
            {},
            1,
            noun="generated spec",
            fix_verb="Regenerate",
            **kw,
        ),
        monkeypatch=monkeypatch,
    )


def test_wiring_rejects_a_type_error_and_surfaces_it_in_the_gate_report(gate_wiring):
    gate_wiring.monkeypatch.setattr(
        automation_gate,
        "typecheck_ok",
        lambda _dir: (False, "tsc --noEmit failed (900ms): error TS2551: 'fillUsre'"),
    )
    gate, outcome = gate_wiring.call(project=object())
    assert outcome == "rejected"
    assert gate["outcome"] == "rejected"
    # The tsc output is the finding, so the UI shows the actual compiler error.
    assert "fillUsre" in gate["findings"][0]
    assert "TypeScript" in gate["reason"]
    assert gate["unblock_action"]


def test_wiring_passes_when_both_static_gates_pass(gate_wiring):
    gate_wiring.monkeypatch.setattr(automation_gate, "typecheck_ok", lambda _dir: (True, "clean"))
    _gate, outcome = gate_wiring.call(project=object())
    assert outcome == "passed"


def test_wiring_skips_the_typecheck_for_legacy_specs(gate_wiring):
    """`project_id IS NULL` specs have no project tree and no tsconfig to check."""
    from app.services import spec_service

    called: list[Path] = []
    gate_wiring.monkeypatch.setattr(spec_service, "playwright_list_ok", lambda *a, **k: True)
    gate_wiring.monkeypatch.setattr(
        automation_gate, "typecheck_ok", lambda d: (called.append(d), (True, ""))[1]
    )
    _gate, outcome = gate_wiring.call(project=None)
    assert outcome == "passed"
    assert called == []


def test_wiring_runs_the_cheaper_list_check_first(gate_wiring):
    """A failed collection short-circuits, so tsc is never paid for."""
    called: list[Path] = []
    gate_wiring.monkeypatch.setattr(
        automation_gate, "list_ok_in_project", lambda *a, **k: (False, "collection failed")
    )
    gate_wiring.monkeypatch.setattr(
        automation_gate, "typecheck_ok", lambda d: (called.append(d), (True, ""))[1]
    )
    gate, outcome = gate_wiring.call(project=object())
    assert outcome == "rejected"
    assert "collection failed" in gate["findings"][0]
    assert called == []


def test_wiring_skips_both_static_gates_when_gating_is_disabled(gate_wiring):
    from app.services import settings_store

    called: list[Path] = []
    gate_wiring.monkeypatch.setattr(settings_store, "gate_enabled", lambda: False)
    gate_wiring.monkeypatch.setattr(
        automation_gate, "typecheck_ok", lambda d: (called.append(d), (True, ""))[1]
    )
    _gate, outcome = gate_wiring.call(project=object())
    assert outcome == "passed"
    assert called == []
