"""Few-shot example selection — the repo-sourced half (#868).

The proven half (specs Q-Agent generated and watched pass) is exercised end-to-end
by the generation tests; what is covered here is the top-up from the application
repository's OWN pre-existing e2e specs, and the ordering guarantee that keeps it
strictly behind the proven ones. The checkout resolution and the bounded walk the
top-up is built on live in ``repo_assets`` (#870) — see ``test_repo_assets.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import project_config_service, repo_assets, spec_examples, spec_service

PROJECT = "Surency"


def _case(title: str = "Member can update their profile", steps=None):
    """A stand-in for the TestCase ORM object; only title/steps/id are read."""
    return SimpleNamespace(id=999, title=title, steps=steps or [])


def _checkout(tmp_path, files: dict[str, str]):
    """Materialize a fake repo checkout from ``{relative path: contents}``."""
    root = tmp_path / "checkout"
    for relative, body in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _configure(db, root, *, repo: str = "", legacy: bool = False):
    """Point PROJECT at ``root`` — either as a named repo or a legacy single path."""
    patch = (
        {"local_repo_path": str(root)}
        if legacy
        else {"repos": [{"name": repo or "web", "local_repo_path": str(root), "default": True}]}
    )
    project_config_service.upsert_config(db, PROJECT, patch)
    db.commit()


def test_repo_examples_are_relevance_ranked_and_tagged(db_session, tmp_path):
    """The case's own words pick the example; the payload says where it came from."""
    root = _checkout(
        tmp_path,
        {
            "e2e/billing.spec.ts": "test('invoice totals', () => {});",
            "e2e/profile.spec.ts": "test('member profile update', () => {});",
            "e2e/search.spec.ts": "test('search results', () => {});",
        },
    )
    _configure(db_session, root)

    picked = spec_examples._repo_examples(db_session, PROJECT, "web", _case(), 2)
    assert picked[0]["filename"] == "e2e/profile.spec.ts"
    assert picked[0]["code"] == "test('member profile update', () => {});"
    assert {ex["source"] for ex in picked} == {"repo"}
    # `filename` is repo-relative, so the prompt shows where in the tree it lives.
    assert all(not ex["filename"].startswith(str(root)) for ex in picked)


def test_step_text_drives_ranking_not_just_the_title(db_session, tmp_path):
    """Steps are part of the query — a title-only match must not win on its own."""
    root = _checkout(
        tmp_path,
        {
            "e2e/unrelated.spec.ts": "test('nothing in common', () => {});",
            "e2e/refund.spec.ts": "test('refund a paid invoice', () => {});",
        },
    )
    _configure(db_session, root)
    case = _case(title="Operator handles a return", steps=[{"a": "Issue a refund", "e": "Invoice is credited"}])

    picked = spec_examples._repo_examples(db_session, PROJECT, "web", case, 1)
    assert [ex["filename"] for ex in picked] == ["e2e/refund.spec.ts"]


# --------------------------------------------------------------- the merge


@pytest.mark.parametrize("proven_count,expected_sources", [
    (0, ["repo", "repo"]),
    (1, ["proven", "repo"]),
    (2, ["proven", "proven"]),
])
def test_repo_examples_only_top_up_the_slots_proven_left_empty(
    db_session, tmp_path, monkeypatch, proven_count, expected_sources
):
    """Proven specs ran green against this app, so they always lead and never yield."""
    root = _checkout(
        tmp_path,
        {"e2e/a.spec.ts": "test('a profile', () => {});", "e2e/b.spec.ts": "test('b', () => {});"},
    )
    _configure(db_session, root)
    monkeypatch.setattr(
        spec_examples,
        "_proven_examples",
        lambda db, key, repo, case, limit: [
            {"filename": f"p{i}.spec.ts", "code": "// proven", "source": "proven"}
            for i in range(min(proven_count, limit))
        ],
    )

    picked = spec_examples.select_examples(db_session, PROJECT, "web", _case(), limit=2)
    assert [ex["source"] for ex in picked] == expected_sources


def test_a_fresh_project_with_no_passing_specs_still_gets_repo_grounding(db_session, tmp_path):
    """The cold-start case, end to end: no proven specs exist, so the repo fills in.

    Nothing is stubbed here — the real ``_proven_examples`` runs against an empty DB
    — so this is what proves the two sources are actually wired together, rather
    than the merge test's stub agreeing with itself.
    """
    root = _checkout(tmp_path, {"e2e/profile.spec.ts": "test('member profile update', () => {});"})
    _configure(db_session, root)

    picked = spec_examples.select_examples(db_session, PROJECT, "web", _case(), limit=2)
    assert [(ex["filename"], ex["source"]) for ex in picked] == [("e2e/profile.spec.ts", "repo")]


def test_a_failing_repo_scan_never_breaks_generation(db_session, monkeypatch):
    """Example selection is an optimization; a broken checkout must not raise."""
    monkeypatch.setattr(spec_examples, "_proven_examples", lambda *a, **k: [])

    def _boom(*args, **kwargs):
        raise OSError("checkout is on a dead network share")

    monkeypatch.setattr(repo_assets, "checkout_path", _boom)
    assert spec_examples.select_examples(db_session, PROJECT, "web", _case(), limit=2) == []


def test_no_project_key_skips_both_sources(db_session, tmp_path, monkeypatch):
    """Without a resolved project there is no scope to read a checkout from."""
    calls: list[str] = []
    monkeypatch.setattr(
        spec_examples, "_repo_examples", lambda *a, **k: calls.append("repo") or []
    )
    assert spec_examples.select_examples(db_session, "", "web", _case(), limit=2) == []
    assert calls == []


# --------------------------------------------------------------- prompt rendering


def test_render_separates_proven_from_repo_examples():
    """The two kinds get different captions — a repo spec's imports are NOT copyable."""
    rendered = spec_service._render_examples(
        [
            {"filename": "1428-TC-01.spec.ts", "code": "// proven code", "source": "proven"},
            {"filename": "apps/web/e2e/smoke.spec.ts", "code": "// repo code", "source": "repo"},
        ]
    )
    assert "REFERENCE SPECS" in rendered
    assert "EXISTING SPECS FROM THE APPLICATION REPOSITORY" in rendered
    # Both bodies survive, each under its own heading.
    assert rendered.index("// proven code") < rendered.index(
        "EXISTING SPECS FROM THE APPLICATION REPOSITORY"
    )
    assert "// repo code" in rendered
    assert "apps/web/e2e/smoke.spec.ts" in rendered
    # The repo block must forbid the structural copying the proven block invites.
    assert "@q-agent/playwright-base" in rendered


def test_render_untagged_examples_keeps_the_original_block():
    """An example with no `source` is the pre-#868 shape: treat it as proven."""
    rendered = spec_service._render_examples([{"filename": "old.spec.ts", "code": "// old"}])
    assert "REFERENCE SPECS" in rendered
    assert "EXISTING SPECS FROM THE APPLICATION REPOSITORY" not in rendered


def test_render_repo_only_emits_no_proven_heading():
    """With nothing proven yet (a fresh project) the misleading caption is absent."""
    rendered = spec_service._render_examples(
        [{"filename": "e2e/smoke.spec.ts", "code": "// repo code", "source": "repo"}]
    )
    assert "EXISTING SPECS FROM THE APPLICATION REPOSITORY" in rendered
    assert "REFERENCE SPECS" not in rendered


def test_render_is_empty_without_usable_examples():
    """No examples, or only blank ones, leaves the prompt exactly as it was."""
    assert spec_service._render_examples(None) == ""
    assert spec_service._render_examples([]) == ""
    assert spec_service._render_examples([{"filename": "x.spec.ts", "code": "  ", "source": "repo"}]) == ""
