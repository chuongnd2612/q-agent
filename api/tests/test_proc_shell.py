"""#613: `shell=True` with an argument list silently drops every argument on POSIX.

Python passes only ``argv[0]`` to ``/bin/sh -c``; the rest become ``$0``, ``$1``… of
that shell and never reach the program. So `playwright test --list` ran as bare
`playwright` (usage banner, exit 1) and `tsc --noEmit` ran as bare `tsc` — dropping
`--noEmit`, so it *emitted*. Both gates therefore "failed" for months while looking
like real failures, and every project-backed spec was rejected.

These tests assert on the **argv actually delivered**, not on the exit code. That
distinction is the whole point: a usage banner also exits non-zero, so an
exit-code-only assertion passes just as happily when the arguments are gone.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from app.services.proc_shell import NEEDS_SHELL


def test_needs_shell_is_windows_only():
    """Windows needs it for `.cmd` shims; POSIX must never have it."""
    assert NEEDS_SHELL is (os.name == "nt")


def _recorder(tmp_path: Path) -> tuple[list[str], Path]:
    """A tiny executable that writes the argv it received, and the argv to run it."""
    out = tmp_path / "argv.txt"
    script = tmp_path / "record.py"
    script.write_text(
        "import sys, pathlib\n"
        f"pathlib.Path(r'{out}').write_text(chr(10).join(sys.argv[1:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    return [sys.executable, str(script)], out


def test_shell_true_with_a_list_loses_the_arguments_on_posix(tmp_path):
    """The trap itself, pinned so nobody 'simplifies' the policy back.

    Skipped on Windows, where the semantics differ and `shell=True` is required.
    """
    if os.name == "nt":
        return  # different semantics; the POSIX trap does not apply
    cmd, out = _recorder(tmp_path)
    subprocess.run([*cmd, "test", "--list"], shell=True, capture_output=True, timeout=60)
    # argv.txt is either absent (the shell ran something else entirely) or empty —
    # what must NOT happen is the arguments arriving.
    delivered = out.read_text(encoding="utf-8").splitlines() if out.exists() else []
    assert "test" not in delivered and "--list" not in delivered


def test_needs_shell_delivers_every_argument(tmp_path):
    """The fix: with `shell=NEEDS_SHELL` the program receives what we passed."""
    cmd, out = _recorder(tmp_path)
    subprocess.run(
        [*cmd, "test", "--list", "--reporter=list"],
        shell=NEEDS_SHELL,
        capture_output=True,
        timeout=60,
    )
    assert out.read_text(encoding="utf-8").splitlines() == ["test", "--list", "--reporter=list"]


def test_every_playwright_and_tsc_invocation_uses_the_shared_policy():
    """No call site may hardcode `shell=True` again.

    A grep-style guard rather than a behavioural one, because the failure mode is a
    *silently* wrong subprocess: there is no exception and no obviously bad exit code
    to assert on, so the only cheap defence is that the policy stays in one place.
    """
    services = Path(__file__).resolve().parents[1] / "app" / "services"
    offenders = []
    for f in services.glob("*.py"):
        if f.name == "proc_shell.py":
            continue  # documents the trap on purpose
        for line in f.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # prose explaining the trap is fine; a kwarg is not
            if "shell=True" in stripped:
                offenders.append(f"{f.name}: {stripped}")
    assert offenders == [], f"hardcoded shell=True (use NEEDS_SHELL, #613): {offenders}"
