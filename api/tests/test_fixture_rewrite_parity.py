"""Server half of the shared fixtures-rewrite parity test (#557).

The server (``playwright_runner.fixture_targets`` / ``_skip_fixture_rewrite``) and
the Local Agent (``agent/src/playwrightConfig.ts`` ``fixtureTargets``) are two
independent ports of the same rule. In #557 they had already drifted on
``*.config.ts`` at depth, which makes the **same** project pass on server-target
and fail collection on local-agent target — a divergence no single-sided test can
catch.

So both sides build the **same declared tree** from
``contracts/fixture-rewrite-tree.json`` and assert the identical rewritten-path
set with the identical specifier per path. The agent's half lives in
``agent/test/fixtureRewriteParity.test.ts`` and reads the same file. Change the
rule on one side only and one of the two gates goes red.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import playwright_runner as runner

IMPORT_LINE = "import { test, expect } from '@playwright/test';\n"


def _contract() -> dict:
    """The shared declared tree, found by walking up to the repo root."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "contracts" / "fixture-rewrite-tree.json"
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise AssertionError("contracts/fixture-rewrite-tree.json not found above this test")


@pytest.fixture()
def declared_tree(tmp_path: Path) -> tuple[Path, dict]:
    contract = _contract()
    for relative in contract["files"]:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(IMPORT_LINE, encoding="utf-8")
    return tmp_path, contract


def test_fixture_targets_match_the_shared_contract(declared_tree):
    """The set of rewritten files is exactly the contract's — no more, no less."""
    spec_dir, contract = declared_tree
    assert runner.fixture_targets(spec_dir) == sorted(contract["rewritten"])


def test_specifier_per_path_matches_the_shared_contract(declared_tree):
    """And each one gets the depth-correct specifier the agent will also compute."""
    spec_dir, contract = declared_tree
    computed = {
        target: runner.fixtures_specifier(Path(target))
        for target in runner.fixture_targets(spec_dir)
    }
    assert computed == contract["rewritten"]


def test_apply_fixtures_honours_the_contract_on_disk(declared_tree):
    """End-to-end: the real writer rewrites exactly the contract's files."""
    spec_dir, contract = declared_tree
    runner._apply_fixtures(spec_dir, spec_dir / "sessionStorage.json", replay_session=False)

    for relative in contract["files"]:
        text = (spec_dir / relative).read_text(encoding="utf-8")
        expected = contract["rewritten"].get(relative)
        if expected is None:
            # Skipped files keep the real package. (The root fixtures.ts is
            # regenerated wholesale and legitimately imports it too.)
            assert "'@playwright/test'" in text, relative
        else:
            assert f"'{expected}'" in text, relative
            assert "'@playwright/test'" not in text, relative


def test_the_load_bearing_pair_stays_split(declared_tree):
    """Root ``fixtures.ts`` is skipped; nested ``fixtures/authenticated.ts`` is not.

    Pinned separately because the obvious "simplification" of the rule — matching
    ``fixtures.ts`` or a ``fixtures`` segment at any depth — silently stops
    rewriting a real library file, and its imports then fail on the device.
    """
    spec_dir, _contract = declared_tree
    targets = runner.fixture_targets(spec_dir)
    assert "fixtures.ts" not in targets
    assert "fixtures/authenticated.ts" in targets
    assert runner.fixtures_specifier(Path("fixtures/authenticated.ts")) == "../fixtures"


def test_configs_are_spared_at_every_depth(declared_tree):
    """The #557 regression itself: `config/environments.config.ts` is left alone."""
    spec_dir, _contract = declared_tree
    targets = runner.fixture_targets(spec_dir)
    assert not [t for t in targets if t.endswith(".config.ts")]
    assert runner._skip_fixture_rewrite(Path("config/environments.config.ts")) is True
    assert runner._skip_fixture_rewrite(Path("vitest.config.ts")) is True
    assert runner._skip_fixture_rewrite(Path("a/b/c/some.config.ts")) is True


def test_output_dirs_are_never_walked(declared_tree):
    """Parity add-on: a stray staged report dir is skipped like node_modules."""
    spec_dir, _contract = declared_tree
    targets = runner.fixture_targets(spec_dir)
    for noise in ("node_modules", "test-results", "playwright-report", "blob-report", ".git"):
        assert not [t for t in targets if t.startswith(f"{noise}/")], noise
    # At depth, too — the pre-#557 server rule only looked at parts[0].
    assert runner._skip_fixture_rewrite(Path("pages/node_modules/vendored.ts")) is True
