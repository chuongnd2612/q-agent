"""Tests for the per-owner workspace scope resolver + legacy migration (#116, ADR 0009).

Covers ``app.services.workspace_scope`` (``scope_for``, ``scoped_dir`` + its
kind-specific wrappers, ``slug``) and the one-time legacy-flat-dirs migration
in ``app.config`` (``migrate_legacy_workspace_dirs``).
"""

from __future__ import annotations

import re

from app import config as config_module
from app.services import workspace_scope

# The pre-existing duplicate slug helpers this module's `slug()` unifies
# (project_config_service._slug / knowledge_service._slug) — copied here so we
# can assert byte-for-byte parity without importing private helpers.
_OLD_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _old_slug(key: str) -> str:
    return _OLD_SLUG_RE.sub("-", key).strip("-") or "project"


# --------------------------------------------------------------- scope_for
def test_scope_for_none_is_shared():
    assert workspace_scope.scope_for(None) == "shared"


def test_scope_for_owner_id_is_users_prefixed():
    assert workspace_scope.scope_for(7) == "users/7"


# --------------------------------------------------------------- scoped_dir
def test_scoped_dir_for_owner_ends_with_users_scope(workspace_dir):
    path = workspace_scope.scoped_dir("knowledge", 7)
    assert path == workspace_dir / "users" / "7" / "knowledge"


def test_scoped_dir_for_shared_ends_with_shared_scope(workspace_dir):
    path = workspace_scope.scoped_dir("repos", None)
    assert path == workspace_dir / "shared" / "repos"


def test_scoped_dir_wrappers_match_scoped_dir(workspace_dir):
    assert workspace_scope.scoped_specs_dir(3) == workspace_scope.scoped_dir("specs", 3)
    assert workspace_scope.scoped_evidence_dir(3) == workspace_scope.scoped_dir("evidence", 3)
    assert workspace_scope.scoped_knowledge_dir(None) == workspace_scope.scoped_dir(
        "knowledge", None
    )
    assert workspace_scope.scoped_repos_dir(None) == workspace_scope.scoped_dir("repos", None)
    assert workspace_scope.scoped_auth_dir(3) == workspace_scope.scoped_dir("auth", 3)


def test_scoped_business_dir_is_a_peer_kind_under_the_scope_root(workspace_dir):
    """Business Knowledge snapshots land beside every other kind (#815, ADR 0009).

    Asserted against the literal path rather than ``scoped_dir("business", ...)``
    alone, so a typo in the kind string cannot make both sides wrong together.
    """
    assert workspace_scope.scoped_business_dir(7) == workspace_dir / "users" / "7" / "business"
    assert workspace_scope.scoped_business_dir(None) == workspace_dir / "shared" / "business"
    assert "business" in workspace_scope._KINDS


# --------------------------------------------------------------------- slug
def test_slug_matches_old_helper_for_various_inputs():
    for value in [
        "My Proj!/x",
        "Surency Platform",
        "  leading and trailing  ",
        "already-safe_name.v2",
        "!!!",
        "",
        "a/b\\c:d*e",
    ]:
        assert workspace_scope.slug(value) == _old_slug(value)


def test_slug_falls_back_to_project_for_empty_or_all_punctuation():
    assert workspace_scope.slug("") == "project"
    assert workspace_scope.slug("!!!") == "project"


# ------------------------------------------------------------- ensure_dirs
def test_ensure_dirs_creates_shared_scope_tree(workspace_dir):
    for kind in ("specs", "evidence", "knowledge", "repos", "auth"):
        assert (workspace_dir / "shared" / kind).is_dir()


# ------------------------------------------------------ legacy migration
def test_legacy_migration_moves_flat_dirs_into_shared_and_is_idempotent(workspace_dir):
    settings = config_module.settings

    # The `workspace_dir` fixture already ran `ensure_dirs()` once (on an empty
    # workspace), so the sentinel already exists. Remove it and seed a legacy
    # flat artifact to simulate an existing pre-ADR-0009 install being upgraded.
    sentinel = workspace_dir / ".workspace_scoped"
    assert sentinel.exists()
    sentinel.unlink()

    legacy_knowledge_dir = workspace_dir / "knowledge" / "Foo"
    legacy_knowledge_dir.mkdir(parents=True, exist_ok=True)
    legacy_file = legacy_knowledge_dir / "knowledge.json"
    legacy_file.write_text('{"stack": []}', encoding="utf-8")

    config_module.migrate_legacy_workspace_dirs(settings)

    moved_file = workspace_dir / "shared" / "knowledge" / "Foo" / "knowledge.json"
    assert moved_file.is_file()
    assert moved_file.read_text(encoding="utf-8") == '{"stack": []}'
    assert not legacy_file.exists()
    assert sentinel.exists()

    # Second run: sentinel present -> no-op, no error, no double-move attempt.
    config_module.migrate_legacy_workspace_dirs(settings)
    assert moved_file.is_file()


def test_legacy_migration_is_a_noop_when_no_legacy_content(workspace_dir):
    settings = config_module.settings
    sentinel = workspace_dir / ".workspace_scoped"
    sentinel.unlink()

    # No flat `<kind>/` dirs have any content (fresh workspace).
    config_module.migrate_legacy_workspace_dirs(settings)

    assert sentinel.exists()
    for kind in ("specs", "evidence", "knowledge", "repos", "auth"):
        assert list((workspace_dir / "shared" / kind).iterdir()) == []
