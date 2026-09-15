"""Read grounding material off the application repository's own checkout (#870).

Two stages of the pipeline want the same thing — *show the model what this team
already writes* — from two different corners of the repo: spec generation wants
the team's existing e2e specs (#868), and the page-object author wants their
existing page objects and fixtures. Both need identical plumbing (find the
checkout, walk it without drowning in ``node_modules``, rank what is found
against the work at hand), so it lives here once rather than twice.

Everything this module returns is **reference material, never importable code**.
It comes from a different tree, with its own imports, its own Playwright config
and its own login; the prompts that inject it are responsible for saying so.

Defensive throughout: a missing config, a stale path, an unreadable file or a
checkout on a dead network share all degrade to "no examples". Grounding is an
optimization, never a correctness requirement.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from app.models.project_config import ProjectConfig
from app.services import project_config_service

__all__ = [
    "MAX_FILE_BYTES",
    "SCAN_MAX_FILES",
    "checkout_path",
    "code_keywords",
    "collect",
    "is_library_file",
    "is_spec_file",
    "iter_files",
    "keywords",
]

_SPEC_NAME_RE = re.compile(r"\.(?:spec|test)\.(?:ts|tsx|js|mjs)$", re.IGNORECASE)

# A library file is recognised by WHERE it sits as much as by its name, and both
# halves matter. The "where" half is the important one: a React app's own
# `src/pages/Dashboard.tsx` is a route component, not a page object, so a bare
# `pages/` match would feed the authoring prompt application source code. Requiring
# a test-suite segment somewhere in the path keeps the match to the team's actual
# automation tree.
_TEST_TREE_SEGMENTS = frozenset(
    {
        "e2e", "test", "tests", "__tests__", "playwright", "cypress",
        "integration", "acceptance", "qa", "automation",
    }
)
_LIBRARY_DIR_SEGMENTS = frozenset(
    {
        "pages", "page-objects", "pageobjects", "page_objects", "components",
        "fixtures", "helpers", "support", "utils",
    }
)
_LIBRARY_NAME_RE = re.compile(
    r"(?:[^/]*(?:Page|PageObject)|[^/]*\.(?:po|fixture))\.(?:ts|tsx|js|mjs)$"
)
_CODE_SUFFIXES = (".ts", ".tsx", ".js", ".mjs")

# Directories that are never walked: vendored or built output, where a match is
# always someone else's code. `node_modules` alone would otherwise dominate the
# walk of any JS monorepo.
PRUNED_DIRS = frozenset({"node_modules", "dist", "build", "out", "coverage", "target", "vendor"})

# Ceilings. These scans run inline inside a generation or authoring request, so
# they are bounded on both axes rather than trusted to be small.
SCAN_MAX_FILES = 300
MAX_FILE_BYTES = 60_000

_WORD_RE = re.compile(r"[a-z0-9]+")
# Identifier boundaries: `updateProfile` and `ProfilePage` have to score against a
# case that says "update the member profile". Without this the file side tokenizes
# to `updateprofile` / `profilepage`, overlaps nothing, and every candidate ties at
# zero — which is ranking by walk order wearing a relevance costume.
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
# Common English / test-boilerplate words that add noise to overlap scoring.
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with",
        "is", "are", "be", "should", "test", "case", "page", "user", "when",
        "then", "given", "verify", "check", "that", "this", "as", "it", "from",
    }
)


def keywords(text: str) -> set[str]:
    """Lowercase alphanumeric tokens of length >= 3, minus stopwords.

    The shared scoring vocabulary: the same tokenizer ranks KB routes/selectors
    (``prompts._rank_by_relevance``) and everything this module returns, so one
    change to what counts as a word moves all of them together.

    :param text: Free text to tokenize (None-safe).
    :returns: The distinct significant tokens.
    """
    return {
        w for w in _WORD_RE.findall((text or "").lower())
        if len(w) >= 3 and w not in _STOPWORDS
    }


def code_keywords(text: str) -> set[str]:
    """:func:`keywords`, but splitting identifiers first — for scoring source files.

    Only the *file* side of a comparison needs this: a case or feature is written in
    prose, while the code it should match is written in ``camelCase``.

    :param text: Source text (a path, file contents, or both).
    :returns: The distinct significant tokens, identifiers broken apart.
    """
    return keywords(_CAMEL_BOUNDARY_RE.sub(" ", text or ""))


def is_spec_file(relative: str) -> bool:
    """True for a test file the team wrote: ``*.spec.ts`` / ``*.test.tsx`` etc.

    :param relative: POSIX path relative to the checkout root.
    """
    return bool(_SPEC_NAME_RE.search(relative))


def is_library_file(relative: str) -> bool:
    """True for a page object / fixture / helper — the reusable half of a suite.

    Specs are deliberately excluded (that is :func:`is_spec_file`'s job, and a spec
    is not library code), and so is anything outside a test-suite tree, because
    ``pages/`` is an extremely common application directory name.

    :param relative: POSIX path relative to the checkout root.
    """
    if is_spec_file(relative) or not relative.lower().endswith(_CODE_SUFFIXES):
        return False
    segments = [seg.lower() for seg in relative.split("/")[:-1]]
    if not _TEST_TREE_SEGMENTS.intersection(segments):
        return False
    return bool(_LIBRARY_DIR_SEGMENTS.intersection(segments)) or bool(
        _LIBRARY_NAME_RE.search(relative)
    )


def _config_repo_entry(config: ProjectConfig, repo: str) -> dict | None:
    """The configured repo entry matching ``repo``, else the project's default."""
    repos = project_config_service.get_repos(config)
    entry = next((r for r in repos if (r.get("name") or "") == repo), None) if repo else None
    return entry if entry is not None else project_config_service.default_repo(config)


def checkout_path(db: Session, project_key: str, repo: str) -> Path | None:
    """The local checkout to read from, or None.

    Resolution mirrors how the rest of the pipeline picks a repo: the configured
    entry whose ``name`` equals ``repo`` wins, else the project's default repo, and
    a legacy single-repo config falls back to ``ProjectConfig.local_repo_path``. A
    configured-but-absent directory resolves to None, so a stale path degrades to
    "no examples" rather than an error.

    :param db: Active session.
    :param project_key: The project whose config is read.
    :param repo: Target repository NAME ("" means "the default repo").
    :returns: An existing directory, or None when nothing is configured or present.
    """
    if not project_key:
        return None
    config = project_config_service.get_config(db, project_key)
    if config is None:
        return None
    entry = _config_repo_entry(config, repo)
    # A repo entry with no path of its own does NOT inherit the project-level one:
    # that field belongs to the legacy single-repo shape, and borrowing it here would
    # hand one repo another repo's checkout.
    raw = (entry.get("local_repo_path") or "") if entry else (config.local_repo_path or "")
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_dir() else None


def iter_files(root: Path, matches: Callable[[str], bool]) -> list[Path]:
    """Collect files under ``root`` matching ``pattern``, pruned and bounded.

    ``matches`` is called with the POSIX path **relative to root**, so it can key
    off a directory segment (``e2e/pages/Foo.ts``) as well as a filename.

    :param root: The checkout to walk.
    :param matches: Predicate deciding what counts as a match.
    :returns: Matching paths, sorted for determinism, at most :data:`SCAN_MAX_FILES`.
    """
    found: list[Path] = []
    stack = [root]
    while stack and len(found) < SCAN_MAX_FILES:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name not in PRUNED_DIRS and not entry.name.startswith("."):
                    stack.append(entry)
                continue
            try:
                relative = entry.relative_to(root).as_posix()
            except ValueError:  # pragma: no cover - entry always sits under root
                relative = entry.name
            if matches(relative):
                found.append(entry)
                if len(found) >= SCAN_MAX_FILES:
                    break
    return sorted(found)


def collect(
    db: Session,
    project_key: str,
    repo: str,
    matches: Callable[[str], bool],
    query: str,
    limit: int,
    *,
    source: str = "repo",
) -> list[dict]:
    """Return up to ``limit`` files from the repo, most relevant to ``query`` first.

    :param db: Active session.
    :param project_key: Project scope, used to resolve the checkout.
    :param repo: Target repository NAME ("" means "the default repo").
    :param matches: Which files qualify (:func:`is_spec_file` / :func:`is_library_file`).
    :param query: Free text — the case or feature being worked on — that files are
        scored against by keyword overlap with their path and contents.
    :param limit: Max files to return.
    :param source: Value stamped on each payload's ``source`` key, so a prompt can
        caption repo-sourced material differently from its own proven material.
    :returns: ``[{"filename", "code", "source"}]`` where ``filename`` is the path
        relative to the checkout, so the prompt shows where in the tree it came
        from. Empty when nothing qualifies.
    """
    if limit <= 0:
        return []
    root = checkout_path(db, project_key, repo)
    if root is None:
        return []

    query_keywords = keywords(query)
    candidates: list[tuple[int, int, dict]] = []  # (-score, walk order, payload)
    for order, path in enumerate(iter_files(root, matches)):
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            code = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not code:
            continue
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:  # pragma: no cover - path always sits under root
            relative = path.name
        score = len(query_keywords & code_keywords(f"{relative} {code}"))
        candidates.append((-score, order, {"filename": relative, "code": code, "source": source}))

    # Most relevant first; walk order breaks ties so the result is deterministic.
    candidates.sort(key=lambda item: (item[0], item[1]))
    return [payload for _, _, payload in candidates[:limit]]
