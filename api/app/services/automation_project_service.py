"""Persistent, git-backed per-project Playwright automation project (#538).

The foundation slice of epic #537. Nothing calls this yet — server generation,
the project-aware gate, execution staging and the agent bundle adopt it in the
Wave-2 slices (#540/#541). It ships as a pure library so it carries **zero
regression surface**.

Layout, one repo per ``(owner_id, project_key, repo)``::

    workspace/<scope>/automation/<project-slug>/<repo-slug>/    <- a real git repo
      node_modules/                 <- installed once (gitignored)
      pages/ components/ fixtures/ data/ utils/ config/
      tests/<TICKET-EXTERNAL-ID>/<spec filename>
      playwright.config.ts  tsconfig.json  package.json  package-lock.json
      .qagent/                      <- plans/, inventory.json (never bundled)

Three properties the rest of the epic leans on:

* **Git replaces snapshot/rollback.** An attempt is :func:`git_reset_hard`, a
  pass is :func:`git_commit`. All git work goes through
  ``repo_service._run_git``, which never raises and redacts nothing sensitive
  into logs.
* **A per-project lock registry is mandatory.** ``routers/automation._generating``
  is per-*run*, so two runs generating for the same project — or generation
  racing execution staging — would tear the tree. :func:`project_lock` is the
  serialization point.
* **Disk is the source of truth.** :func:`inventory` reads the real tree; the
  ``automation_files`` rows are a mirror refreshed by :func:`sync_files_to_db`.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import REPO_ROOT
from app.db import utcnow
from app.logging import logger
from app.models.automation_project import AutomationFile, AutomationProject
from app.services.repo_service import _run_git
from app.services.workspace_scope import scoped_automation_dir, scoped_specs_dir, slug

__all__ = [
    "BASE_PACKAGE",
    "BASE_VERSION_SPEC",
    "BASE_VERSION",
    "LIBRARY_DIRS",
    "SCAFFOLD_DIRS",
    "VENDORED_TARBALL_RELPATH",
    "project_lock",
    "project_dir",
    "ensure_project",
    "materialize_scaffold",
    "migrate_gitignore",
    "migrate_tsconfig",
    "migrate_base_pin",
    "base_pin_major",
    "base_version_drift",
    "installed_base_version",
    "ensure_deps",
    "git_init",
    "git_commit",
    "git_stash",
    "git_reset_hard",
    "git_changed_paths",
    "head_commit",
    "write_spec",
    "spec_dir",
    "inventory",
    "write_inventory",
    "signature_map",
    "diff_is_additive",
    "sync_files_to_db",
    "bundle_for_agent",
    "stage_for_run",
]

BASE_PACKAGE = "@q-agent/playwright-base"
BASE_VERSION = "1.0.0"
BASE_VERSION_SPEC = "^1.0.0"

# Pinned committed tarball, relative to the repo root — the offline/registry-down
# fallback for :func:`ensure_deps` (published by #539). Absent in a checkout that
# predates it, which `ensure_deps` degrades gracefully around.
#
# **This path must move with BASE_VERSION on a major bump.** Since #566 `deps_installed`
# is major-aware, so installing a 1.0.0 tarball under a server on 2.x no longer counts as
# installed — the fallback would correctly but silently degrade to "unavailable" forever.
VENDORED_TARBALL_RELPATH = "playwright-base/vendor/q-agent-playwright-base-1.0.0.tgz"

# The shared asset library — everything an accumulating project reuses. This is
# exactly what ships to the agent and what execution staging copies wholesale.
LIBRARY_DIRS = ("pages", "components", "fixtures", "data", "utils", "config")
# Every directory materialize_scaffold creates.
SCAFFOLD_DIRS = (*LIBRARY_DIRS, "tests", ".qagent", ".qagent/plans")

# Never bundled, never mirrored to the DB, never staged as-is.
_EXCLUDED_TOP_DIRS = ("node_modules", ".git", ".qagent")
_NPM_TIMEOUT_S = 600
_TEXT_SUFFIXES = {".ts", ".tsx", ".js", ".mjs", ".cjs", ".json", ".md", ".txt", ".yml", ".yaml", ".env"}

# path prefix -> AutomationFile.kind
_KIND_BY_DIR = {
    "pages": "page",
    "components": "component",
    "fixtures": "fixture",
    "data": "data",
    "utils": "util",
    "config": "config",
    "tests": "spec",
}

# ---------------------------------------------------------------------------
# Per-project lock registry
# ---------------------------------------------------------------------------

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def project_lock(key: "int | str | AutomationProject") -> threading.Lock:
    """The process-wide lock for one automation project.

    Mandatory, not optional. ``routers/automation._generating`` is keyed per
    *run*, so it does not stop two runs from generating into the same project's
    tree at once, nor generation from racing execution staging. Hold this lock
    around anything that mutates or reads-then-mutates the tree.

    Args:
        key: An :class:`AutomationProject`, its ``id``, or the
            ``"<owner>/<project_key>/<repo>"`` identity string used before the
            row exists (see :func:`ensure_project`).

    Returns:
        The same :class:`threading.Lock` instance for a given key, forever.
    """
    if isinstance(key, AutomationProject):
        key = key.id
    name = str(key)
    with _locks_guard:
        lock = _locks.get(name)
        if lock is None:
            lock = threading.Lock()
            _locks[name] = lock
        return lock


def _identity_key(owner_id: int | None, project_key: str, repo: str) -> str:
    return f"{owner_id if owner_id is not None else 'shared'}/{project_key}/{repo}"


# ---------------------------------------------------------------------------
# Project resolution
# ---------------------------------------------------------------------------


def _relative_slug(project_key: str, repo: str) -> str:
    """``"<project-slug>/<repo-slug>"`` — repo granularity is deliberate.

    ``spec_service.build_case_context`` already resolves the knowledge base per
    repo, so a project with two front-ends must not share one page-object
    namespace. A project with no distinct repo collapses to ``"default"``.
    """
    return f"{slug(project_key)}/{slug(repo) if repo else 'default'}"


def project_dir(project: AutomationProject) -> Path:
    """Absolute on-disk root of ``project``, recomputed from the current workspace.

    Deliberately *not* read straight from ``project.root_path``: recomputing
    means a moved or re-pointed ``workspace_dir`` (and every test's temp
    workspace) keeps resolving.
    """
    return scoped_automation_dir(project.owner_id) / project.slug


def ensure_project(
    db: Session, owner_id: int | None, project_key: str, repo: str = ""
) -> AutomationProject:
    """Get-or-create the automation project for ``(owner_id, project_key, repo)``.

    Idempotent: calling it twice returns the same row and the same directory.
    The directory is materialized (scaffold + ``git init`` + an initial commit)
    on first call only. Serialized on the project's identity lock, so two
    concurrent callers cannot produce a torn tree or a duplicate row.

    Args:
        db: Active session.
        owner_id: Owning user's id, or ``None`` for the shared namespace.
        project_key: The provider project key (e.g. ``"SUR"``).
        repo: The repo this asset library belongs to; ``""`` for the project's
            only/default repo.

    Returns:
        The persisted :class:`AutomationProject`, with its tree on disk.
    """
    project_key = (project_key or "").strip()
    repo = (repo or "").strip()
    with project_lock(_identity_key(owner_id, project_key, repo)):
        project = db.scalar(
            select(AutomationProject).where(
                AutomationProject.owner_id == owner_id,
                AutomationProject.project_key == project_key,
                AutomationProject.repo == repo,
            )
        )
        if project is None:
            project = AutomationProject(
                owner_id=owner_id,
                project_key=project_key,
                repo=repo,
                slug=_relative_slug(project_key, repo),
            )
            project.root_path = str(project_dir(project))
            db.add(project)
            try:
                db.commit()
            except IntegrityError:  # another process won the unique constraint
                db.rollback()
                project = db.scalar(
                    select(AutomationProject).where(
                        AutomationProject.owner_id == owner_id,
                        AutomationProject.project_key == project_key,
                        AutomationProject.repo == repo,
                    )
                )
                if project is None:  # pragma: no cover - unreachable in practice
                    raise
            else:
                db.refresh(project)

        # Idempotent on-disk materialization (a no-op for an existing tree).
        root = project_dir(project)
        materialize_scaffold(project)
        git_init(project)
        if head_commit(project) is None:
            git_commit(project, "chore: scaffold automation project")
        expected = str(root)
        if project.root_path != expected:
            project.root_path = expected
            project.updated_at = utcnow()
            db.commit()
        return project


# ---------------------------------------------------------------------------
# Scaffold
# ---------------------------------------------------------------------------

# `results.json` is the JSON reporter's output (see `_PLAYWRIGHT_CONFIG` below), so it
# lands in the project ROOT whenever anything runs the suite. Untracked, git reports it
# as a change, and the page-object author's authorization check then blames the editor
# for "writing files the plan did not authorize" and rolls back the assets it legitimately
# wrote (#613). A build artifact must never be able to look like an authored file.
_GITIGNORE = """node_modules/
test-results/
playwright-report/
blob-report/
results.json
.auth/
*.log
"""

#: Lines a project's .gitignore must contain for artifacts not to read as authored
#: files. Appended by `migrate_gitignore` to projects scaffolded before #613.
_REQUIRED_IGNORES = ("results.json",)

# `module`/`moduleResolution` are **NodeNext**, not the `CommonJS` + `node` pair this
# originally scaffolded (#562). Two reasons, both measured against a real compiler:
#
# 1. TypeScript 7 **removed** `moduleResolution: "node"` (it emits `TS5108: Option
#    'moduleResolution=node10' has been removed`). #546's gate fails open on the whole
#    TS5xxx family so *our* gate survives that, but #549 exports this project to a git
#    remote the customer runs in **their** CI, on a toolchain we don't control — and a
#    developer opening it in a modern IDE would see errors on a project we generated.
# 2. `NodeNext` models what Playwright actually does at runtime more honestly than
#    `bundler` does. The scaffolded `package.json` declares no `"type"`, so `NodeNext`
#    classifies every `.ts` file as **CommonJS** — exactly how Playwright requires them.
#    Extensionless relative imports (`../../pages/UserFormPage`) therefore stay legal:
#    the extension requirement `NodeNext` is known for applies only in *ESM* mode. That
#    was verified against TypeScript 5.9 **and** 7.1-dev before choosing it, precisely
#    because needing extensions would have forced a change to every generated import.
#    `"module": "ESNext"` + `"moduleResolution": "bundler"` also typechecks clean, but it
#    tells the compiler the files are ESM, which would let a generated spec use
#    `import.meta`/top-level `await` past the gate and then fail at Playwright runtime.
#
# Do not reintroduce `"type": "module"` in `_package_json` without revisiting this: that
# would flip these files into ESM mode and *then* extensions become mandatory.
_TSCONFIG = """{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "lib": ["ES2022", "DOM"],
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "resolveJsonModule": true,
    "noEmit": true,
    "types": ["node"]
  },
  "include": ["**/*.ts"],
  "exclude": ["node_modules"]
}
"""

_PLAYWRIGHT_CONFIG = """import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  timeout: 60_000,
  reporter: [['list'], ['json', { outputFile: 'results.json' }]],
  use: {
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    trace: 'retain-on-failure',
  },
});
"""


def _package_json(project: AutomationProject) -> str:
    name = slug(f"{project.project_key}-{project.repo or 'default'}").lower()
    return (
        "{\n"
        f'  "name": "qagent-automation-{name}",\n'
        '  "private": true,\n'
        '  "version": "0.0.0",\n'
        '  "scripts": {\n'
        '    "test": "playwright test",\n'
        '    "typecheck": "tsc --noEmit"\n'
        "  },\n"
        '  "dependencies": {\n'
        f'    "{BASE_PACKAGE}": "{BASE_VERSION_SPEC}"\n'
        "  }\n"
        "}\n"
    )


# `moduleResolution` values a modern compiler rejects outright. TypeScript 7 removed
# both `node`/`node10` and `classic`, so a project still declaring one cannot be
# typechecked at all — every spec in it fails for a reason that is not the spec's.
_REMOVED_MODULE_RESOLUTIONS = frozenset({"node", "node10", "classic"})


def migrate_gitignore(target: "Path | AutomationProject") -> bool:
    """Append any missing artifact-ignore lines to an existing project's .gitignore.

    Same shape as :func:`migrate_tsconfig` (#562) and for the same reason: projects
    scaffolded before #613 have a `.gitignore` with no `results.json`, so the JSON
    reporter's output reads as an unauthorized write and rolls back legitimately
    authored assets. Self-limiting — appends only the lines that are absent, leaves
    everything else (including hand edits) untouched, and never rewrites the file when
    there is nothing to add.

    Returns True only when it wrote.
    """
    root = target if isinstance(target, Path) else project_dir(target)
    path = root / ".gitignore"
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        return False
    present = {line.strip() for line in existing.splitlines()}
    missing = [line for line in _REQUIRED_IGNORES if line not in present]
    if not missing:
        return False
    trailing = existing.endswith("\n") or not existing
    body = existing if trailing else existing + "\n"
    try:
        path.write_text(body + "\n".join(missing) + "\n", encoding="utf-8")
    except OSError:
        return False
    logger.info("migrated .gitignore for {}: added {}", root.name, ", ".join(missing))
    return True


def migrate_tsconfig(path: Path) -> bool:
    """Repair a ``tsconfig.json`` that still declares a removed ``moduleResolution``.

    :func:`materialize_scaffold` deliberately never overwrites an existing file, so
    projects scaffolded before #562 would keep their broken ``moduleResolution: "node"``
    forever — and #549 hands exactly that tree to the customer's own CI. This is the one
    narrow exception, and it is self-limiting three ways:

    * It fires **only** when ``moduleResolution`` is a value no supported compiler
      accepts. Any other config, however hand-tuned, is left completely alone.
    * It patches the two offending keys in place rather than rewriting the file, so
      unrelated user edits (extra ``paths``, a relaxed ``strict``) survive.
    * A file that is not parseable JSON (hand-edited into JSONC with comments) is left
      alone rather than reformatted — better a stale config than a clobbered one.

    Returns:
        True when the file was rewritten.
    """
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    options = config.get("compilerOptions") if isinstance(config, dict) else None
    if not isinstance(options, dict):
        return False
    if str(options.get("moduleResolution", "")).lower() not in _REMOVED_MODULE_RESOLUTIONS:
        return False
    # Both keys move together: `moduleResolution: NodeNext` requires `module: NodeNext`
    # (TS5095 otherwise), so fixing one alone would swap TS5108 for a different TS5xxx.
    options["module"] = "NodeNext"
    options["moduleResolution"] = "NodeNext"
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    logger.info("migrated removed moduleResolution in {} to NodeNext", path)
    return True


# A dependency range this module is willing to reason about: plain semver only
# (``^2.1.0``, ``~2.0``, ``2.x``, ``>=2 <3``). Anything else — ``file:../base.tgz``,
# ``git+ssh://…``, ``npm:alias@2`` — is a deliberate override by whoever edited the
# manifest, and rewriting it would break their vendored/forked install.
_SEMVER_RANGE_RE = re.compile(r"^[\s\d.xX*^~<>=|v\-]+$")


def base_pin_major(spec: str | None) -> int | None:
    """The major version a ``@q-agent/playwright-base`` range resolves to, or None.

    ``"^2.1.0"``/``"~2.0"``/``"2.x"``/``">=2 <3"`` -> ``2``; an exact ``"2.1.0"`` -> ``2``.
    None for an empty value **and** for anything that is not a plain semver range, which
    is what keeps :func:`migrate_base_pin` off ``file:``/``git+``/``npm:`` pins.
    """
    text = (spec or "").strip()
    if not text or not _SEMVER_RANGE_RE.match(text):
        return None
    match = re.search(r"\d+", text)
    return int(match.group()) if match else None


def _read_json(path: Path) -> object | None:
    """Parsed JSON at ``path``, or None when missing/unreadable/not valid JSON.

    Returning None rather than raising is what lets both the migration and the drift check
    leave a hand-edited JSONC manifest alone instead of clobbering or 500-ing on it.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _base_dependency(manifest: object) -> tuple[dict, str] | None:
    """The ``(deps section, current range)`` holding :data:`BASE_PACKAGE`, or None."""
    if not isinstance(manifest, dict):
        return None
    for section in ("dependencies", "devDependencies"):
        deps = manifest.get(section)
        if isinstance(deps, dict) and isinstance(deps.get(BASE_PACKAGE), str):
            return deps, deps[BASE_PACKAGE]
    return None


def migrate_base_pin(target: "Path | AutomationProject") -> bool:
    """Bump a ``package.json`` whose base-package pin is a **major** behind (#566).

    :func:`materialize_scaffold` never overwrites, so a project's ``package.json`` — and
    with it the ``@q-agent/playwright-base`` pin — is frozen at whatever it was scaffolded
    against. Minor/patch releases still flow (the pin is a caret range and
    :func:`ensure_deps` runs ``npm install``), but the first ``2.x`` would never reach an
    existing project: it would keep resolving 1.x while the generator, prompted against
    the *current* base API surface, emits imports the installed base lacks — failing the
    gate for a reason that points at the spec rather than at the stale pin.

    Modelled on :func:`migrate_tsconfig` and self-limiting the same four ways:

    * It fires **only** on a major-version gap. A project on ``^1.0.0`` with the server on
      ``^1.4.0`` is left alone, because ``npm install`` already resolves that.
    * It rewrites **only** that one dependency entry, via a JSON round-trip, so every
      other field — a hand-added ``devDependencies``, extra ``scripts``, a changed
      ``name`` — survives verbatim.
    * A manifest that is not parseable JSON is left alone rather than clobbered, and so is
      a pin that is not a plain semver range (see :func:`base_pin_major`).
    * A stale ``package-lock.json`` is *deleted* when the pin moves, because ``npm ci``
      refuses a lockfile that disagrees with the manifest — that would turn the upgrade
      into a hard install failure. The lockfile is a generated artifact, never
      hand-authored, and ``npm install`` regenerates it on the next :func:`ensure_deps`.

    Args:
        target: The project's ``package.json`` path, or the :class:`AutomationProject`.

    Returns:
        True when the file was rewritten.
    """
    path = (project_dir(target) / "package.json") if isinstance(target, AutomationProject) else target
    current = base_pin_major(BASE_VERSION_SPEC)
    if current is None:  # pragma: no cover - BASE_VERSION_SPEC is a semver constant
        return False
    manifest = _read_json(path)
    found = _base_dependency(manifest)
    if found is None:
        return False
    deps, pinned = found
    pinned_major = base_pin_major(pinned)
    if pinned_major is None or pinned_major >= current:
        return False
    deps[BASE_PACKAGE] = BASE_VERSION_SPEC
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    logger.info(
        "migrated {} pin in {} from {} to {}", BASE_PACKAGE, path, pinned, BASE_VERSION_SPEC
    )
    lockfile = path.parent / "package-lock.json"
    if lockfile.exists():
        try:
            lockfile.unlink()
        except OSError as exc:  # pragma: no cover - permission edge
            logger.warning("could not drop stale lockfile {}: {}", lockfile, exc)
        else:
            logger.info("dropped {} — stale against the migrated pin", lockfile)
    return True


def installed_base_version(project: AutomationProject) -> str | None:
    """The ``@q-agent/playwright-base`` version actually present in ``node_modules``.

    None when nothing is installed, or when the installed package's own manifest is
    unreadable — callers treat that as "assume it is fine" rather than reinstalling on a
    guess.
    """
    manifest = _read_json(project_dir(project) / "node_modules" / BASE_PACKAGE / "package.json")
    version = manifest.get("version") if isinstance(manifest, dict) else None
    return str(version) if version else None


def base_version_drift(project: AutomationProject) -> str | None:
    """A legible one-line reason when this project's base package is a major behind.

    This is what stops ``AutomationProject.base_version`` being a **write-only** column —
    before #566 nothing ever read it, which was half the bug. All three signals are
    checked, because they can disagree and each says something different:

    * the on-disk ``package.json`` pin — what the *next* install will resolve;
    * ``node_modules`` — what a generated spec will actually compile and run against;
    * the recorded ``base_version`` — the DB's view, which a stale row can contradict.

    Returns:
        None when nothing is behind the server's :data:`BASE_VERSION`; otherwise a
        sentence naming the concrete versions, safe to log or surface to a user.
    """
    current = base_pin_major(BASE_VERSION)
    if current is None:  # pragma: no cover - BASE_VERSION is a semver constant
        return None
    found = _base_dependency(_read_json(project_dir(project) / "package.json"))
    pinned = found[1] if found else None
    installed = installed_base_version(project)
    recorded = project.base_version or None

    behind: list[str] = []
    for label, value in (
        ("package.json pins {}", pinned),
        ("node_modules has {}", installed),
        ("the recorded base_version is {}", recorded),
    ):
        major = base_pin_major(value)
        if major is not None and major < current:
            behind.append(label.format(value))
    if not behind:
        return None
    return (
        f"{BASE_PACKAGE} is a major version behind the server's {BASE_VERSION}: "
        + "; ".join(behind)
    )


def materialize_scaffold(project: AutomationProject) -> Path:
    """Create the directory skeleton and the baseline config files, idempotently.

    Existing files are never overwritten — the project accumulates AI-authored
    code and a user may legitimately have edited ``playwright.config.ts``. The only
    exceptions are the two targeted migrations, each narrowly scoped to a value that is
    outright broken rather than merely different: :func:`migrate_tsconfig` (a
    ``moduleResolution`` no supported compiler accepts) and :func:`migrate_base_pin` (a
    base-package pin a whole major behind the server).

    Whatever drift survives the migrations is **logged, not blocked** — see
    :func:`base_version_drift` for why refusing to generate would be the wrong trade.

    Returns:
        The project root.
    """
    root = project_dir(project)
    root.mkdir(parents=True, exist_ok=True)
    for relative in SCAFFOLD_DIRS:
        (root / relative).mkdir(parents=True, exist_ok=True)
        keep = root / relative / ".gitkeep"
        if not any(p.name != ".gitkeep" for p in (root / relative).iterdir()) and not keep.exists():
            keep.write_text("", encoding="utf-8")
    for name, content in (
        (".gitignore", _GITIGNORE),
        ("tsconfig.json", _TSCONFIG),
        ("playwright.config.ts", _PLAYWRIGHT_CONFIG),
        ("package.json", _package_json(project)),
    ):
        path = root / name
        if not path.exists():
            path.write_text(content, encoding="utf-8")
    migrate_tsconfig(root / "tsconfig.json")
    # Same idea for artifact ignores: a project scaffolded before #613 has no
    # `results.json` line, so the JSON reporter output reads as an unauthorized
    # write and rolls back legitimately authored assets.
    migrate_gitignore(root)
    migrate_base_pin(root / "package.json")
    drift = base_version_drift(project)
    if drift:
        logger.warning(
            "automation project {} ({}): {} — ensure_deps will reinstall; generation continues",
            project.id,
            project.slug,
            drift,
        )
    return root


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

# Injection seam: (args, cwd) -> True on success. Tests replace it; nothing here
# ever touches the network by default in the suite because every test injects.
NpmRunner = Callable[[Sequence[str], Path], bool]


def _run_npm(args: Sequence[str], cwd: Path) -> bool:
    """Run an npm command in ``cwd``; return True on success. Never raises.

    Mirrors ``repo_service._run_git``: failures are logged, not raised, so a
    registry outage degrades into a fallback rather than a 500.
    """
    executable = shutil.which("npm") or shutil.which("npm.cmd")
    if not executable:
        logger.warning("npm not found on PATH; cannot install automation deps")
        return False
    try:
        proc = subprocess.run(  # noqa: S603
            [executable, *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_NPM_TIMEOUT_S,
            encoding="utf-8",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("npm {} failed: {}", args[0] if args else "", exc)
        return False
    if proc.returncode != 0:
        logger.warning(
            "npm {} exited {}: {}",
            args[0] if args else "",
            proc.returncode,
            (proc.stderr or "").strip()[:300],
        )
        return False
    return True


def vendored_tarball() -> Path | None:
    """The pinned committed ``@q-agent/playwright-base`` tarball, or None.

    Published by #539 at ``<repo root>/playwright-base/vendor/…tgz``. Returns
    None in a checkout that predates it — :func:`ensure_deps` then degrades to
    ``"unavailable"`` instead of failing.
    """
    candidate = REPO_ROOT / VENDORED_TARBALL_RELPATH
    return candidate if candidate.is_file() else None


def _record_base_version(project: AutomationProject) -> None:
    """Set ``project.base_version`` from what is really installed on disk.

    Prefers the installed package's own version over :data:`BASE_VERSION` (which is only
    what the *server* ships) so the column can be trusted by :func:`base_version_drift`.
    Does not commit — the caller's session owns that, exactly as before.
    """
    version = installed_base_version(project) or BASE_VERSION
    if project.base_version != version:
        project.base_version = version


def deps_installed(project: AutomationProject) -> bool:
    """True when a **compatible** ``@q-agent/playwright-base`` is present in the tree.

    Compatible means *the same major* as :data:`BASE_VERSION`. Presence alone is not
    enough, and this is what makes :func:`migrate_base_pin` more than cosmetic: without
    the major check, a project holding 1.x under a server on 2.x would get its pin
    migrated and then :func:`ensure_deps` would still short-circuit to ``"cached"``
    forever, so the new base would never actually be installed.

    Fails **open**: an install whose own ``package.json`` is unreadable, or carries no
    parseable version, counts as installed rather than triggering a reinstall on a guess.
    """
    if not (project_dir(project) / "node_modules" / BASE_PACKAGE).exists():
        return False
    installed = base_pin_major(installed_base_version(project))
    current = base_pin_major(BASE_VERSION)
    if installed is None or current is None:
        return True
    return installed >= current


def ensure_deps(project: AutomationProject, *, runner: NpmRunner | None = None) -> str:
    """Install ``@q-agent/playwright-base`` into the project, once.

    Persistence is what makes the (brand-new) ``npm install`` step cheap: it
    runs once per project, not once per run. Resolution order:

    1. Already installed -> ``"cached"`` (a second call is a no-op).
    2. ``npm ci`` when a lockfile exists, else ``npm install <pkg>@^1.0.0``
       -> ``"registry"``.
    3. Registry unreachable -> ``npm install <pinned vendored tarball>``
       -> ``"vendored"``.
    4. Neither source available -> log + ``"unavailable"``. **Never raises** —
       the caller decides whether it can proceed.

    Args:
        project: The project to install into.
        runner: Injection seam for the npm invocation (tests never hit the
            network); defaults to :func:`_run_npm`.

    Returns:
        One of ``"cached"``, ``"registry"``, ``"vendored"``, ``"unavailable"``.
    """
    run = runner or _run_npm
    root = project_dir(project)
    root.mkdir(parents=True, exist_ok=True)
    with project_lock(project):
        if deps_installed(project):
            # Refresh the row even on the cheap path: `base_version` is only useful to
            # `base_version_drift` if it tracks what is really on disk, and a project
            # installed before #566 has an empty or stale value nothing would ever fix.
            _record_base_version(project)
            return "cached"

        if (root / "package-lock.json").exists():
            registry_args: Sequence[str] = ("ci",)
        else:
            registry_args = ("install", f"{BASE_PACKAGE}@{BASE_VERSION_SPEC}")
        if run(registry_args, root) and deps_installed(project):
            _record_base_version(project)
            return "registry"

        tarball = vendored_tarball()
        if tarball is None:
            logger.warning(
                "automation deps unavailable for project {}: registry install failed and no "
                "vendored tarball at {}",
                project.id,
                VENDORED_TARBALL_RELPATH,
            )
            return "unavailable"
        if run(("install", str(tarball)), root) and deps_installed(project):
            _record_base_version(project)
            logger.info("automation deps for project {} installed from vendored tarball", project.id)
            return "vendored"
        logger.warning("automation deps unavailable for project {}: vendored install failed", project.id)
        return "unavailable"


# ---------------------------------------------------------------------------
# Git — replaces the snapshot/rollback mechanism
# ---------------------------------------------------------------------------


def git_init(project: AutomationProject) -> bool:
    """``git init`` the project dir if it isn't a repo yet. Idempotent."""
    root = project_dir(project)
    if (root / ".git").exists():
        return True
    root.mkdir(parents=True, exist_ok=True)
    if not _run_git(["init", "-q", str(root)]):
        return False
    # Local identity so commits work on hosts with no global git config.
    _run_git(["-C", str(root), "config", "user.email", "qagent@local"])
    _run_git(["-C", str(root), "config", "user.name", "Q-Agent"])
    return True


def git_commit(project: AutomationProject, message: str) -> bool:
    """Stage everything and commit. A pass in the loop is a commit.

    Returns True on a real commit; False when git failed **or** there was
    nothing to commit (an empty commit is not an error worth raising, and the
    caller only cares that HEAD reflects the tree).
    """
    root = project_dir(project)
    if not _run_git(["-C", str(root), "add", "-A"]):
        return False
    return _run_git(["-C", str(root), "commit", "-q", "-m", message or "chore: update"])


def git_stash(project: AutomationProject, message: str = "qagent") -> bool:
    """Stash the working tree (including untracked files)."""
    root = project_dir(project)
    return _run_git(["-C", str(root), "stash", "push", "-u", "-m", message])


def git_reset_hard(project: AutomationProject, ref: str = "HEAD") -> bool:
    """Discard working-tree changes back to ``ref`` — an attempt is a reset.

    Also removes untracked/ignored-by-nothing files (``git clean -fd``, with
    ``node_modules/`` protected because it is gitignored and ``clean -fd``
    leaves ignored paths alone), so a failed generation leaves no debris.
    """
    root = project_dir(project)
    ok = _run_git(["-C", str(root), "reset", "-q", "--hard", ref])
    return _run_git(["-C", str(root), "clean", "-qfd"]) and ok


def git_changed_paths(project: AutomationProject) -> list[str]:
    """Project-relative paths the working tree changed since HEAD (#545).

    ``git status --porcelain`` covers added, modified, deleted and untracked
    files, which is exactly "what did the agentic project editor touch?" — the
    set an audit entry is recorded for, and the set checked against the plan's
    ``writable`` list. Renames (``R  old -> new``) contribute the destination.

    Never raises: an unavailable/failing git returns ``[]``, and the caller's
    other defences (``--list``, ``tsc``, :func:`diff_is_additive`) still stand.
    """
    root = project_dir(project)
    try:
        proc = subprocess.run(  # noqa: S603
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=60,
            encoding="utf-8",
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    paths: list[str] = []
    for line in (proc.stdout or "").splitlines():
        entry = line[3:].strip() if len(line) > 3 else ""
        if " -> " in entry:  # a rename: the destination is what exists now
            entry = entry.split(" -> ", 1)[1]
        entry = entry.strip('"').replace("\\", "/")
        if entry and entry not in paths:
            paths.append(entry)
    return sorted(paths)


def head_commit(project: AutomationProject) -> str | None:
    """Current HEAD sha, or None when the repo has no commits / isn't a repo."""
    root = project_dir(project)
    try:
        proc = subprocess.run(  # noqa: S603
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------


def spec_dir(project: AutomationProject, ticket_external_id: str) -> Path:
    """``<project>/tests/<TICKET-EXTERNAL-ID>/``.

    Per-ticket nesting makes the collision logged as *"Minor — filename
    collision"* in ``docs/ARCHITECTURE-REVIEW.md`` structurally impossible in a
    ``tests/`` tree that accumulates forever (see #540's amendment).
    """
    return project_dir(project) / "tests" / (slug(ticket_external_id) or "unknown")


def write_spec(
    project: AutomationProject, ticket_external_id: str, case_code: str, code: str
) -> Path:
    """Write one case's spec into ``tests/<TICKET-EXTERNAL-ID>/``.

    The **filename** comes from ``spec_service.spec_filename`` and is not
    reinterpreted here — #540 owns that convention (and changes it to the full
    ticket id), so delegating means this function needs no edit then. Imported
    lazily to keep the dependency one-way (#540 makes ``spec_service`` import
    this module).

    Returns:
        The absolute path written.
    """
    from app.services import spec_service  # local import: avoids an import cycle

    directory = spec_dir(project, ticket_external_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / spec_service.spec_filename(ticket_external_id, case_code)
    path.write_text(code, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Inventory — the ground truth that makes reuse decidable
# ---------------------------------------------------------------------------

_EXPORT_CLASS_RE = re.compile(
    r"^export\s+(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)", re.MULTILINE
)
_EXPORT_OTHER_RE = re.compile(
    r"^export\s+(?:default\s+)?(?:async\s+)?"
    r"(?:function|const|let|var|interface|type|enum)\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)
_EXPORT_FUNCTION_RE = re.compile(
    r"^export\s+(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(",
    re.MULTILINE,
)
# A class member: two-plus spaces of indentation, optional modifiers, a name, an
# argument list, an optional return annotation, then an opening brace.
_METHOD_RE = re.compile(
    r"^[ \t]{2,}(?:(?:public|private|protected|static|readonly|override|async|get|set)\s+)*"
    r"(?P<name>[A-Za-z_$][\w$]*)\s*\((?P<args>[^)]*)\)\s*(?::[^{;=]+)?\{",
    re.MULTILINE,
)
# Control-flow keywords that look like a call followed by a block.
_NOT_METHODS = {
    "if", "for", "while", "switch", "catch", "do", "else", "return",
    "function", "constructor", "try", "with", "await",
}


def _arg_names(raw: str) -> list[str]:
    """Argument *names* only — types, defaults and modifiers stripped.

    ``(user: User, opts: Opts = {})`` -> ``["user", "opts"]``. Destructured
    parameters keep their literal text so the signature stays recognizable.
    """
    names: list[str] = []
    depth = 0
    current = ""
    for char in raw:
        if char in "{[<(":
            depth += 1
        elif char in "}]>)":
            depth -= 1
        if char == "," and depth == 0:
            names.append(current)
            current = ""
        else:
            current += char
    names.append(current)
    out: list[str] = []
    for token in names:
        token = token.strip()
        if not token:
            continue
        # Strip a default value, then a type annotation (destructuring keeps its braces).
        token = token.split("=", 1)[0].strip()
        if not token.startswith(("{", "[")):
            token = token.split(":", 1)[0].strip()
        token = re.sub(r"^(?:public|private|protected|readonly)\s+", "", token).strip()
        token = token.lstrip(".").rstrip("?")
        if token:
            out.append(token)
    return out


def _body_after(text: str, brace_index: int) -> str:
    """Brace-matched body text starting at the ``{`` at ``brace_index``."""
    depth = 0
    for index in range(brace_index, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[brace_index : index + 1]
    return text[brace_index:]


def _normalized_body_hash(body: str) -> str:
    """Whitespace-insensitive hash of a method body — detects a *rewrite*.

    Reformatting is not a rewrite, so all whitespace runs collapse before
    hashing; any change to the statements themselves changes the digest.
    """
    return hashlib.sha256(re.sub(r"\s+", " ", body).strip().encode("utf-8")).hexdigest()[:16]


def _kind_for(relative: str) -> str:
    top = relative.split("/", 1)[0]
    return _KIND_BY_DIR.get(top, "util")


def _scan_file(text: str) -> tuple[list[str], list[dict]]:
    """Exported names and method entries (``{signature, body_hash}``) in ``text``."""
    exports = _EXPORT_CLASS_RE.findall(text) + _EXPORT_OTHER_RE.findall(text)
    seen: set[str] = set()
    ordered_exports = [n for n in exports if not (n in seen or seen.add(n))]

    methods: list[dict] = []
    signatures: set[str] = set()
    for match in _METHOD_RE.finditer(text):
        name = match.group("name")
        if name in _NOT_METHODS:
            continue
        signature = f"{name}({', '.join(_arg_names(match.group('args')))})"
        if signature in signatures:
            continue
        signatures.add(signature)
        body = _body_after(text, text.index("{", match.end() - 1))
        methods.append({"signature": signature, "body_hash": _normalized_body_hash(body)})

    # Exported top-level functions are part of the reusable surface too.
    for match in _EXPORT_FUNCTION_RE.finditer(text):
        open_paren = text.index("(", match.start())
        close_paren = text.index(")", open_paren)
        signature = f"{match.group(1)}({', '.join(_arg_names(text[open_paren + 1 : close_paren]))})"
        if signature in signatures:
            continue
        signatures.add(signature)
        brace = text.find("{", close_paren)
        body = _body_after(text, brace) if brace != -1 else ""
        methods.append({"signature": signature, "body_hash": _normalized_body_hash(body)})

    return ordered_exports, methods


def inventory(root: "Path | AutomationProject", dirs: Iterable[str] = LIBRARY_DIRS) -> list[dict]:
    """Scan the real tree for the reusable surface of every asset file.

    This is the ground-truth input that makes reuse decidable, and the correct
    resolution of the ``docs/ARCHITECTURE-REVIEW.md:266-274`` fork: signatures
    come from **Q-Agent's own project dir**, so they are always in sync with
    what a generated spec can actually import — never from scraping the
    customer app repo. Deliberately dependency-free (regex, no TS parser).

    Args:
        root: The project root ``Path``, or an :class:`AutomationProject`.
        dirs: Top-level directories to scan; defaults to :data:`LIBRARY_DIRS`
            (``tests/`` is not part of the reusable surface).

    Returns:
        ``[{path, kind, exports, methods}]`` sorted by path, where ``methods``
        is a list of rendered signatures like ``["openCreateUser()",
        "fillUser(user)"]``.
    """
    base = project_dir(root) if isinstance(root, AutomationProject) else Path(root)
    entries: list[dict] = []
    for directory in dirs:
        source_dir = base / directory
        if not source_dir.is_dir():
            continue
        for path in sorted(source_dir.rglob("*.ts")):
            if not path.is_file() or path.name.endswith(".d.ts"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            exports, methods = _scan_file(text)
            if not exports and not methods:
                continue
            relative = path.relative_to(base).as_posix()
            entries.append(
                {
                    "path": relative,
                    "kind": _kind_for(relative),
                    "exports": exports,
                    "methods": [m["signature"] for m in methods],
                    "_bodies": {m["signature"]: m["body_hash"] for m in methods},
                }
            )
    return sorted(entries, key=lambda e: e["path"])


def write_inventory(project: AutomationProject) -> Path:
    """Persist the inventory to ``.qagent/inventory.json`` (never bundled)."""
    import json

    path = project_dir(project) / ".qagent" / "inventory.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    public = [{k: v for k, v in entry.items() if not k.startswith("_")} for entry in inventory(project)]
    path.write_text(json.dumps(public, indent=2), encoding="utf-8")
    return path


def signature_map(entries: Sequence[dict]) -> dict[str, str]:
    """``{"pages/LoginPage.ts::fillUser(user)": <body hash>}`` from an inventory."""
    out: dict[str, str] = {}
    for entry in entries:
        bodies = entry.get("_bodies") or {}
        for signature in entry.get("methods", []):
            out[f"{entry['path']}::{signature}"] = bodies.get(signature, "")
    return out


def diff_is_additive(
    project: AutomationProject, before: Sequence[dict], *, allow_body_edits: bool = False
) -> bool:
    """True only when the project's reusable surface grew, never shrank.

    Extending the shared library must never break specs that already import
    from it. Adding a method is fine; **removing an existing exported method,
    or rewriting its body, is not** — that silently changes the meaning of
    every spec already calling it.

    Args:
        project: The project, scanned as it is *now*.
        before: The :func:`inventory` result captured before the edit.
        allow_body_edits: When True, a pre-existing method's **body** may change
            while its signature must still exist unchanged. This is the *heal*
            mode (#547): a stale locator lives inside a page object's body, so
            fixing it necessarily rewrites that body — refusing the rewrite is
            exactly what forced the heal loop to re-inline locators into the
            spec instead. The half of the guarantee other specs actually depend
            on — "every signature I import still exists, with the same
            parameters" — is unchanged; the body edit is additionally fenced by
            the whole-project ``--list`` + ``tsc`` gates and by the
            import-spanning assertion anti-cheat. Authoring (#545) leaves this
            False, so an *authoring* pass still may not touch a body at all.

    Returns:
        False if any pre-existing signature disappeared, or (unless
        ``allow_body_edits``) if its body changed.
    """
    old = signature_map(before)
    new = signature_map(inventory(project))
    for key, body_hash in old.items():
        if key not in new:
            logger.info("automation diff not additive: {} was removed", key)
            return False
        if not allow_body_edits and body_hash and new[key] != body_hash:
            logger.info("automation diff not additive: body of {} was rewritten", key)
            return False
    return True


# ---------------------------------------------------------------------------
# DB mirror / bundling / staging
# ---------------------------------------------------------------------------


def _project_files(root: Path) -> list[Path]:
    """Every tracked-worthy file in the tree (excludes node_modules/.git/.qagent)."""
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        parts = path.relative_to(root).parts
        if parts[0] in _EXCLUDED_TOP_DIRS or ".git" in parts or "node_modules" in parts:
            continue
        if path.name == ".gitkeep":
            continue
        files.append(path)
    return files


def _read_text(path: Path) -> str | None:
    if path.suffix and path.suffix not in _TEXT_SUFFIXES and path.name not in {".gitignore"}:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def sync_files_to_db(db: Session, project: AutomationProject) -> int:
    """Reconcile the ``automation_files`` mirror with what is on disk.

    One-way by design: **disk is the source of truth.** The agentic editor
    writes real files (the only way Claude can Read/Edit them) and this runs
    afterwards. Rows for files that no longer exist are deleted.

    Returns:
        The number of rows the mirror now holds.
    """
    root = project_dir(project)
    on_disk: dict[str, str] = {}
    for path in _project_files(root):
        text = _read_text(path)
        if text is None:
            continue
        on_disk[path.relative_to(root).as_posix()] = text

    existing = {
        row.path: row
        for row in db.scalars(
            select(AutomationFile).where(AutomationFile.project_id == project.id)
        ).all()
    }
    for relative, text in on_disk.items():
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        row = existing.get(relative)
        if row is None:
            db.add(
                AutomationFile(
                    project_id=project.id,
                    path=relative,
                    kind=_kind_for(relative),
                    code=text,
                    sha256=digest,
                )
            )
        elif row.sha256 != digest:
            row.code = text
            row.sha256 = digest
            row.kind = _kind_for(relative)
            row.updated_at = utcnow()
    for relative, row in existing.items():
        if relative not in on_disk:
            db.delete(row)
    project.updated_at = utcnow()
    db.commit()
    return len(on_disk)


def bundle_for_agent(project: AutomationProject) -> dict[str, str]:
    """The project's shared library as ``{relative path: source}``, ready to ship.

    Excludes ``tests/**`` (the run stages only its own specs — see
    :func:`stage_for_run`), ``.qagent/**`` (server-side plans/inventory the
    agent has no business seeing) and ``node_modules/**`` (installed on the
    other side, never transferred).
    """
    root = project_dir(project)
    bundle: dict[str, str] = {}
    for path in _project_files(root):
        relative = path.relative_to(root).as_posix()
        if relative.startswith("tests/"):
            continue
        text = _read_text(path)
        if text is None:
            continue
        bundle[relative] = text
    return bundle


def stage_for_run(
    project: AutomationProject,
    run_code: str,
    spec_paths: Sequence[str] = (),
    owner_id: int | None = None,
) -> Path:
    """Stage an ephemeral per-run copy under ``<scoped specs>/<RUN-CODE>/``.

    Copies the **whole** shared library (imports must resolve) but **only this
    run's spec files**. Playwright runs everything under ``testDir``, so staging
    all of ``tests/`` would re-run every test ever generated for the project on
    every run.

    Args:
        project: The source project.
        run_code: The owning Run's code, e.g. ``"RUN-211"``.
        spec_paths: Project-relative spec paths to include (e.g.
            ``["tests/SUR-1502/SUR-1502-TC-01.spec.ts"]``). Empty stages no specs.
        owner_id: Scope for the staging dir; defaults to the project's owner.

    Returns:
        The staged run directory.
    """
    root = project_dir(project)
    scope_owner = project.owner_id if owner_id is None else owner_id
    staged = scoped_specs_dir(scope_owner) / run_code
    with project_lock(project):
        staged.mkdir(parents=True, exist_ok=True)
        for relative in LIBRARY_DIRS:
            source = root / relative
            if source.is_dir():
                shutil.copytree(source, staged / relative, dirs_exist_ok=True)
        for name in ("package.json", "package-lock.json", "tsconfig.json", "playwright.config.ts"):
            if (root / name).is_file():
                shutil.copy2(root / name, staged / name)
        for relative in spec_paths:
            source = root / relative
            if not source.is_file():
                logger.warning("stage_for_run: spec {} missing in project {}", relative, project.id)
                continue
            destination = staged / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    return staged
