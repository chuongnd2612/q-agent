"""Project-aware static gates: ``playwright test --list`` (#540) + ``tsc --noEmit`` (#546).

The legacy gate (``spec_service.playwright_list_ok``) parse-checks one spec
**alone in an empty temp dir**. That made every import a generated spec would
want illegal by construction: ``import '../pages/LoginPage'`` and
``import '@q-agent/playwright-base'`` cannot resolve in a directory that
contains neither. Epic #537 called that out as the hardest blocker.

Listing the **whole persistent project** instead gives three properties the
empty-temp-dir gate cannot have:

1. ``../pages/Foo`` and ``@q-agent/playwright-base`` resolve **because they
   genuinely exist** — the blocker dissolves by construction, not by weakening
   the gate.
2. Collection covers *every* spec in the project, so a page-object edit that
   breaks **another case's** spec fails the list and is rejected. This is the
   primary defence against shared-project coupling.
3. Requiring the candidate's own ``test()`` titles in the output catches
   "collected fine, but contains no test".

The fail-open policy of ``spec_service._resolve_list_bin`` is preserved
verbatim: an unavailable/failing-to-launch Playwright binary must **never**
block generation. ``--list`` only collects — it never launches a browser — so
the always-on evidence fixture in ``@q-agent/playwright-base`` (which depends on
``page``) costs nothing here.

``--list`` is necessary but **not sufficient** (#546). Playwright transpiles with
**esbuild**, which strips types without checking them: a spec calling
``userFormPage.fillUsre(...)`` or passing the wrong argument shape to a page-object
method collects perfectly cleanly. That is *precisely* the failure mode layering
introduces — before this epic specs were self-contained and there were no cross-file
call signatures to get wrong. :func:`typecheck_ok` closes that hole by running
``tsc --noEmit`` against the project's own ``tsconfig.json``, after ``--list``
(cheaper check first).
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from app.services.proc_shell import NEEDS_SHELL
from app.config import settings
from app.logging import logger

__all__ = [
    "list_ok_in_project",
    "test_titles",
    "resolve_list_bin",
    "resolve_tsc_bin",
    "typecheck_ok",
]

# `test('title'`, `test.only("title"`, `test.skip(`…`)` — the titles Playwright
# echoes in `--list` output. `test.describe` is deliberately excluded: a describe
# name is a *group*, not proof that a test exists (property 3 above).
_TEST_TITLE_RE = re.compile(
    r"\btest\s*(?:\.\s*(?:only|skip|fixme|fail|slow)\s*)*\(\s*(['\"`])(?P<title>(?:\\.|(?!\1).)*)\1",
    re.DOTALL,
)

_LIST_TIMEOUT_S = 120
_TSC_TIMEOUT_S = 180

# A tsc diagnostic line: `tests/x.spec.ts(5,17): error TS2551: Property 'fillUsre'…`
# or a config-level one with no file prefix: `error TS5083: Cannot read file …`.
_TSC_DIAGNOSTIC_RE = re.compile(
    r"^(?P<file>[^(\n]*?\([0-9]+,[0-9]+\):\s*)?error\s+TS(?P<code>[0-9]+):\s*(?P<message>.*)$",
    re.MULTILINE,
)

# Diagnostics that mean **the toolchain/deps are incomplete**, not that the spec is
# wrong. Rejecting on these would make the gate fail *closed* on a project whose
# `npm install` was unavailable — exactly what the fail-open policy forbids.
#
#   2688 Cannot find type definition file for 'node'      (no @types/node)
#   2318 Cannot find global type 'X'                      (no lib/@types at all)
#   2583/2584 Cannot find name 'Set'/'document'           (missing lib/@types)
#   7016 Could not find a declaration file for module 'x'  (untyped dep)
#   6053/6231 file-resolution failures
_TSC_ENVIRONMENTAL_CODES = frozenset({"2688", "2318", "2583", "2584", "7016", "6053", "6231"})

# The whole TS5xxx family is "command line / compiler option" errors, which are a
# property of `tsconfig.json` and the installed compiler — never of the candidate
# spec. Measured the hard way: TypeScript 7 **removed** ``moduleResolution: "node"``,
# which the scaffolded ``tsconfig.json`` still declares, so a server that happened to
# resolve a v7 ``tsc`` would emit TS5108 and reject *every* spec forever. Exactly the
# fail-closed catastrophe the fail-open policy exists to prevent.
_TSC_CONFIG_CODE_RANGE = range(5000, 6000)

# `Cannot find module 'x'` — environmental only for a **bare package** specifier
# (`@playwright/test`, `@q-agent/playwright-base`: deps not installed). A relative
# specifier (`../pages/LoginPage`) is a genuine authoring error and stays a rejection.
_TSC_MODULE_NOT_FOUND_CODE = "2307"
_TSC_MODULE_RE = re.compile(r"Cannot find module\s+'(?P<spec>[^']+)'")


def test_titles(code: str) -> list[str]:
    """The ``test()`` titles declared in ``code``, in source order, de-duped.

    Used to build the ``expect_titles`` argument of :func:`list_ok_in_project`
    from a freshly generated spec, so the gate can assert the candidate actually
    contributed tests to the collection.

    Escape sequences are unescaped for the common cases only (``\\'``, ``\\"``,
    ``\\\\``); a title containing anything more exotic simply won't be asserted
    on, which fails *open* rather than rejecting a valid spec.
    """
    titles: list[str] = []
    for match in _TEST_TITLE_RE.finditer(code or ""):
        title = match.group("title")
        title = title.replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\")
        title = title.strip()
        # A template literal with an interpolation renders differently at
        # collection time, so it can never be matched literally — skip it.
        if not title or "${" in title or title in titles:
            continue
        titles.append(title)
    return titles


def _resolve_local_bin(project_dir: Path, name: str) -> str | None:
    """Path to a locally-installed ``name`` CLI, or None when nowhere to be found.

    Prefers the project's **own** ``node_modules/.bin`` (installed by
    ``automation_project_service.ensure_deps``) and falls back to the server-wide
    install in ``settings.playwright_node_modules``. As in the legacy gate we
    deliberately never fall back to a bare ``npx``: that could trigger a network
    fetch and hang, and these checks must never block generation.
    """
    roots = (project_dir / "node_modules", settings.playwright_node_modules)
    for root in roots:
        for suffix in (".cmd", ""):
            candidate = root / ".bin" / f"{name}{suffix}"
            if candidate.exists():
                return str(candidate)
    return None


def resolve_list_bin(project_dir: Path) -> str | None:
    """Path to a Playwright CLI usable for listing ``project_dir``, or None."""
    return _resolve_local_bin(project_dir, "playwright")


def resolve_tsc_bin(project_dir: Path) -> str | None:
    """Path to a TypeScript compiler usable for typechecking ``project_dir``, or None.

    Same resolution strategy (and the same never-``npx`` rule) as
    :func:`resolve_list_bin`. No new install is needed for the server-wide
    fallback: ``settings.playwright_node_modules`` points at the SPA's
    ``app/node_modules``, which already carries ``typescript`` as a devDependency.
    """
    return _resolve_local_bin(project_dir, "tsc")


def _list_env(project_dir: Path) -> dict[str, str]:
    """Environment for a gate subprocess, with NODE_PATH covering both trees.

    The project's own ``node_modules`` resolves ``@q-agent/playwright-base``; the
    server-wide install resolves ``@playwright/test`` (and owns the browsers) for
    a project whose ``npm install`` was unavailable.
    """
    env = os.environ.copy()
    parts = [
        str(project_dir / "node_modules"),
        str(settings.playwright_node_modules),
    ]
    if env.get("NODE_PATH"):
        parts.append(env["NODE_PATH"])
    env["NODE_PATH"] = os.pathsep.join(parts)
    return env


def list_ok_in_project(project_dir: Path, expect_titles: list[str]) -> tuple[bool, str]:
    """Collect the whole project with ``playwright test --list``.

    Runs with ``cwd=project_dir`` and the **project's own**
    ``playwright.config.ts`` (only the reporter is overridden — see below), so
    ``testDir``, ``testMatch`` and TS path resolution are exactly what execution
    will use.

    ``--reporter=list`` is forced on the command line because the scaffolded
    project config declares a JSON reporter with an ``outputFile``; under that
    reporter ``--list`` writes to the file and prints nothing, so the title
    assertion would have no output to search.

    Args:
        project_dir: The project root to collect (an
            ``automation_project_service.project_dir`` result).
        expect_titles: Titles the candidate spec must contribute to the
            collection (see :func:`test_titles`). Empty means "only require a
            clean collection".

    Returns:
        ``(ok, detail)``. ``ok`` is False **only** when Playwright genuinely ran
        and either failed collection or omitted an expected title — ``detail``
        then carries the reason (truncated Playwright output / the missing
        titles) for the gate report. When the check cannot run at all (no
        binary, timeout, OS error) it returns ``(True, "skipped: …")``: the gate
        is an optimization and must never block generation when unavailable.
    """
    bin_path = resolve_list_bin(project_dir)
    if bin_path is None:
        return True, "skipped: no local Playwright install"
    try:
        proc = subprocess.run(  # noqa: S603
            [bin_path, "test", "--list", "--reporter=list"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=_LIST_TIMEOUT_S,
            shell=NEEDS_SHELL,  # Windows-only: on POSIX this would drop every arg (#613)
            env=_list_env(project_dir),
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("list_ok_in_project skipped ({}): {}", type(exc).__name__, exc)
        return True, f"skipped: {type(exc).__name__}"

    output = "\n".join(p for p in (proc.stdout, proc.stderr) if p).strip()
    if proc.returncode != 0:
        # A real collection failure — this is what catches a broken page object
        # that breaks *another* case's spec, not just the candidate's.
        return False, f"playwright --list failed (rc={proc.returncode}): {output[-1200:]}"

    missing = [title for title in (expect_titles or []) if title not in output]
    if missing:
        return False, "collected, but these test titles are missing: " + "; ".join(missing[:6])
    return True, "collected cleanly"


def _is_environmental(code: str, message: str) -> bool:
    """True when a tsc diagnostic reflects a missing toolchain rather than bad code."""
    if code in _TSC_ENVIRONMENTAL_CODES:
        return True
    if code.isdigit() and int(code) in _TSC_CONFIG_CODE_RANGE:
        return True
    if code == _TSC_MODULE_NOT_FOUND_CODE:
        match = _TSC_MODULE_RE.search(message)
        # A bare package specifier means the dependency isn't installed; a
        # relative one means the spec imports a page object that isn't there.
        return bool(match) and not match.group("spec").startswith((".", "/"))
    return False


def typecheck_ok(project_dir: Path) -> tuple[bool, str]:
    """``tsc --noEmit`` against the project's own ``tsconfig.json``.

    This is the gate that ``playwright test --list`` structurally cannot be: ``--list``
    transpiles with esbuild, which erases types without checking them, so a misspelled
    page-object method or a wrong argument shape collects cleanly. Only a real
    typechecker sees those (#546).

    Runs with ``cwd=project_dir`` and **no** ``--project`` flag, so ``tsc`` picks up the
    scaffolded ``tsconfig.json`` (``include: ["**/*.ts"]``) — i.e. every page object,
    fixture and spec in the project, not just the candidate. As with ``--list``, that
    means a page-object edit which breaks *another* case's spec is caught here too.

    Fail-open in three distinct ways, so an incomplete toolchain can never block
    generation:

    1. **No ``tsc``** → ``(True, "skipped: …")``.
    2. **Launch failure / timeout** → ``(True, "skipped: …")``.
    3. **Only environmental diagnostics** (missing ``@types/node``, an uninstalled
       ``@playwright/test``, a compiler-option error from ``tsconfig.json``) →
       ``(True, "skipped: …")``. A project whose ``npm install`` never ran, or whose
       ``tsconfig.json`` a newer compiler dislikes, would otherwise be rejected
       wholesale for reasons that have nothing to do with the generated spec.

    Only a *definitive* type error is a rejection.

    Returns:
        ``(ok, detail)``. ``detail`` always carries the measured elapsed time so the
        added gate latency is visible in ``gate_report``, not just in logs.
    """
    bin_path = resolve_tsc_bin(project_dir)
    if bin_path is None:
        logger.warning("typecheck_ok skipped: no local tsc install for {}", project_dir)
        return True, "skipped: no local TypeScript install"

    started = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603
            [bin_path, "--noEmit", "--pretty", "false"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=_TSC_TIMEOUT_S,
            shell=NEEDS_SHELL,  # Windows-only: on POSIX this would drop every arg (#613)
            env=_list_env(project_dir),
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("typecheck_ok skipped ({}): {}", type(exc).__name__, exc)
        return True, f"skipped: {type(exc).__name__}"
    elapsed_ms = int((time.monotonic() - started) * 1000)

    output = "\n".join(p for p in (proc.stdout, proc.stderr) if p).strip()
    if proc.returncode == 0:
        logger.info("typecheck_ok passed in {}ms for {}", elapsed_ms, project_dir)
        return True, f"tsc --noEmit clean ({elapsed_ms}ms)"

    diagnostics = [
        (match.group("code"), (match.group(0) or "").strip())
        for match in _TSC_DIAGNOSTIC_RE.finditer(output)
    ]
    real = [line for code, line in diagnostics if not _is_environmental(code, line)]
    if not diagnostics:
        # rc != 0 but nothing recognisable as a diagnostic: tsc itself misbehaved
        # (bad CLI flags, crash). Not the spec's fault — fail open.
        logger.warning("typecheck_ok skipped (unparsable tsc output, {}ms)", elapsed_ms)
        return True, f"skipped: tsc produced no diagnostics (rc={proc.returncode})"
    if not real:
        logger.warning(
            "typecheck_ok skipped ({} environmental diagnostic(s), {}ms): {}",
            len(diagnostics),
            elapsed_ms,
            diagnostics[0][1][:200],
        )
        return True, "skipped: TypeScript toolchain incomplete (missing types/deps)"

    logger.info("typecheck_ok rejected {} error(s) in {}ms", len(real), elapsed_ms)
    detail = "; ".join(real[:6])
    return False, f"tsc --noEmit failed ({elapsed_ms}ms): {detail[:1200]}"
