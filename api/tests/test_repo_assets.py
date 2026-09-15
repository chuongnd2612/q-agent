"""Reading grounding material off the application repo's own checkout (#870).

The shared plumbing behind #868's spec examples and the page-object author's house
style block: which files count as what, how the walk is bounded, and how a stale or
unreadable checkout degrades.
"""

from __future__ import annotations

import pytest

from app.services import project_config_service, repo_assets

PROJECT = "Surency"


def _checkout(tmp_path, files: dict[str, str]):
    """Materialize a fake repo checkout from ``{relative path: contents}``."""
    root = tmp_path / "checkout"
    root.mkdir(parents=True, exist_ok=True)
    for relative, body in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


def _configure(db, root, name: str = "web"):
    project_config_service.upsert_config(
        db, PROJECT, {"repos": [{"name": name, "local_repo_path": str(root), "default": True}]}
    )
    db.commit()


# --------------------------------------------------------------- classification


@pytest.mark.parametrize("relative", [
    "apps/web/e2e/smoke.spec.ts",
    "tests/login.test.tsx",
    "cypress/e2e/checkout.spec.js",
])
def test_spec_files_are_recognised(relative):
    assert repo_assets.is_spec_file(relative)
    # A spec is not library code, whatever directory it sits in.
    assert not repo_assets.is_library_file(relative)


@pytest.mark.parametrize("relative", [
    "apps/web/e2e/pages/LoginPage.ts",
    "e2e/support/commands.ts",
    "tests/fixtures/app.fixture.ts",
    "cypress/e2e/AdminPage.ts",
    "playwright/utils/wait.ts",
])
def test_library_files_are_recognised(relative):
    assert repo_assets.is_library_file(relative)


@pytest.mark.parametrize("relative", [
    # The one that matters: a React app's own route components live in `pages/`,
    # and feeding those to the page-object author would be application source code.
    "src/pages/Dashboard.tsx",
    "app/components/Button.tsx",
    "lib/utils/date.ts",
    "e2e/pages/README.md",
    "e2e/fixtures/users.json",
])
def test_application_source_is_not_mistaken_for_a_page_object(relative):
    assert not repo_assets.is_library_file(relative)


# --------------------------------------------------------------- the walk


def test_walk_prunes_vendored_and_built_trees_and_hidden_dirs(tmp_path):
    root = _checkout(
        tmp_path,
        {
            "apps/web/e2e/smoke.spec.ts": "// smoke",
            "node_modules/pkg/index.spec.ts": "// vendored",
            "dist/bundle.spec.js": "// built",
            "coverage/report.spec.js": "// coverage",
            ".cache/stale.spec.ts": "// hidden",
        },
    )
    found = {p.relative_to(root).as_posix() for p in repo_assets.iter_files(root, repo_assets.is_spec_file)}
    assert found == {"apps/web/e2e/smoke.spec.ts"}


def test_walk_is_bounded_by_the_file_ceiling(tmp_path, monkeypatch):
    """A huge suite cannot make an inline generation request walk forever."""
    root = _checkout(tmp_path, {f"e2e/t{i}.spec.ts": f"// {i}" for i in range(12)})
    monkeypatch.setattr(repo_assets, "SCAN_MAX_FILES", 5)
    assert len(repo_assets.iter_files(root, repo_assets.is_spec_file)) == 5


# --------------------------------------------------------------- collect


def test_collect_ranks_by_relevance_and_reports_repo_relative_paths(db_session, tmp_path):
    root = _checkout(
        tmp_path,
        {
            "e2e/pages/BillingPage.ts": "class BillingPage { invoice() {} }",
            "e2e/pages/ProfilePage.ts": "class ProfilePage { updateProfile() {} }",
        },
    )
    _configure(db_session, root)

    found = repo_assets.collect(
        db_session, PROJECT, "web", repo_assets.is_library_file, "member profile update", 2
    )
    assert [item["filename"] for item in found] == [
        "e2e/pages/ProfilePage.ts",
        "e2e/pages/BillingPage.ts",
    ]
    assert found[0]["code"] == "class ProfilePage { updateProfile() {} }"
    assert {item["source"] for item in found} == {"repo"}


def test_collect_skips_oversized_and_blank_files(db_session, tmp_path, monkeypatch):
    root = _checkout(
        tmp_path,
        {
            "e2e/pages/HugePage.ts": "// profile\n" + ("x" * 500),
            "e2e/pages/BlankPage.ts": "   \n",
            "e2e/pages/ProfilePage.ts": "// profile",
        },
    )
    _configure(db_session, root)
    monkeypatch.setattr(repo_assets, "MAX_FILE_BYTES", 200)

    found = repo_assets.collect(
        db_session, PROJECT, "web", repo_assets.is_library_file, "profile", 5
    )
    assert [item["filename"] for item in found] == ["e2e/pages/ProfilePage.ts"]


def test_collect_without_a_usable_checkout_returns_nothing(db_session, tmp_path):
    """No project, no config, a stale path, and limit<=0 all degrade to []."""
    query = repo_assets.is_library_file
    assert repo_assets.collect(db_session, "", "web", query, "anything", 2) == []
    assert repo_assets.collect(db_session, PROJECT, "web", query, "anything", 2) == []

    project_config_service.upsert_config(
        db_session, PROJECT, {"local_repo_path": str(tmp_path / "gone")}
    )
    db_session.commit()
    assert repo_assets.checkout_path(db_session, PROJECT, "") is None
    assert repo_assets.collect(db_session, PROJECT, "", query, "anything", 2) == []

    root = _checkout(tmp_path, {"e2e/pages/ProfilePage.ts": "// profile"})
    _configure(db_session, root)
    assert repo_assets.collect(db_session, PROJECT, "web", query, "profile", 0) == []
