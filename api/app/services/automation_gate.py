"""Project-aware ``playwright test --list`` gate (#540).

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
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from app.config import settings
from app.logging import logger

__all__ = ["list_ok_in_project", "test_titles", "resolve_list_bin"]

# `test('title'`, `test.only("title"`, `test.skip(`…`)` — the titles Playwright
# echoes in `--list` output. `test.describe` is deliberately excluded: a describe
# name is a *group*, not proof that a test exists (property 3 above).
_TEST_TITLE_RE = re.compile(
    r"\btest\s*(?:\.\s*(?:only|skip|fixme|fail|slow)\s*)*\(\s*(['\"`])(?P<title>(?:\\.|(?!\1).)*)\1",
    re.DOTALL,
)

_LIST_TIMEOUT_S = 120


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


def resolve_list_bin(project_dir: Path) -> str | None:
    """Path to a Playwright CLI usable for listing ``project_dir``, or None.

    Prefers the project's **own** ``node_modules/.bin`` (installed by
    ``automation_project_service.ensure_deps``) and falls back to the
    server-wide install in ``settings.playwright_node_modules``. As in the legacy
    gate we deliberately never fall back to a bare ``npx``: that could trigger a
    network fetch and hang, and this check must never block generation.
    """
    candidates = (
        project_dir / "node_modules" / ".bin" / "playwright.cmd",
        project_dir / "node_modules" / ".bin" / "playwright",
        settings.playwright_node_modules / ".bin" / "playwright.cmd",
        settings.playwright_node_modules / ".bin" / "playwright",
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _list_env(project_dir: Path) -> dict[str, str]:
    """Environment for the list subprocess, with NODE_PATH covering both trees.

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
            shell=True,  # noqa: S602 - .cmd resolution on Windows
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
