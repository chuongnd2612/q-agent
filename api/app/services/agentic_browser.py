"""Shared dedicated-Chrome plumbing for agentic browser runs (#889).

Extracted from :mod:`app.services.live_authoring_service` (#400), which was the
only caller until the planner agent (#889) needed the *same* setup: a long-lived,
pre-authenticated Chrome on a private CDP port, plus the ``PW_CLI_*`` /``BU_*``
environment the ported Playwright Test Agents and the live-authoring skills
expect to find already wired.

Nothing here decides *what* the agentic run does — it only owns launch, readiness,
the driver env block and teardown, so the two callers cannot drift apart on, say,
which env var carries the CDP URL.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from app.config import settings
from app.logging import logger

_LAUNCHER = Path(__file__).resolve().parent / "pw_scripts" / "authoring_browser.cjs"

#: How long to wait for the launched Chrome's CDP endpoint to come up.
CDP_READY_TIMEOUT_S = 25.0

#: `BU_NAME` must match this — browser-harness turns it into a socket/pid filename
#: and rejects anything else (`_ipc._check`).
_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")


class BrowserSessionError(RuntimeError):
    """Raised when the dedicated Chrome could not be brought up."""


def session_name(*parts: str) -> str:
    """Build a per-session browser daemon/session name from arbitrary parts.

    Unique per caller-supplied tuple (e.g. run + case, or run + ticket) so each
    agentic session gets its own dedicated tab, and sanitised to
    ``[A-Za-z0-9_-]{1,64}`` because browser-harness builds a filename from it and
    refuses anything else.

    Args:
        *parts: Identifying fragments (run code, case code, ticket id, ...).

    Returns:
        A safe, non-empty session name.
    """
    raw = "-".join(["qagent", *[p for p in parts if p]])
    return _NAME_RE.sub("-", raw)[:64].strip("-") or "qagent-authoring"


def free_port() -> int:
    """Pick a free localhost TCP port for the dedicated Chrome's CDP endpoint."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_cdp(port: int, timeout_s: float) -> bool:
    """Poll the Chrome DevTools ``/json/version`` endpoint until it responds.

    Args:
        port: The CDP port Chrome was launched on.
        timeout_s: Give up after this many seconds.

    Returns:
        True once the endpoint answers 200, False if the deadline passed.
    """
    deadline = time.monotonic() + timeout_s
    url = f"http://127.0.0.1:{port}/json/version"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:  # noqa: S310 - localhost only
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - endpoint not up yet
            time.sleep(0.3)
    return False


def launch_browser(base_url: str, port: int, profile_dir: Path) -> subprocess.Popen[str]:
    """Start the long-lived pre-authenticated Chrome launcher (Node subprocess).

    The launcher (``authoring_browser.cjs``) uses only Node built-ins — no
    Playwright/node_modules — so it runs from its own dir and needs no NODE_PATH
    (the API image ships chromium + node but no Playwright). It finds Chrome via
    ``QAGENT_CHROME_BIN`` (set in the image) or platform defaults. stdin is a
    pipe: closing it (in :func:`teardown`) tells the launcher to kill Chrome —
    cross-platform cleanup that works on Windows where ``terminate()`` won't run
    signal handlers.

    Args:
        base_url: The URL Chrome opens on (and whose origin the captured session
            is restored for).
        port: The CDP port to expose.
        profile_dir: Persistent ``--user-data-dir`` for this project's browser.

    Returns:
        The launcher subprocess (pass it to :func:`teardown` when done).
    """
    cmd = ["node", str(_LAUNCHER), base_url, str(port), str(profile_dir)]
    logger.info("Agentic browser: launching {}", " ".join(cmd))
    return subprocess.Popen(  # noqa: S603
        cmd,
        cwd=str(_LAUNCHER.parent),
        env=os.environ.copy(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


def teardown(proc: subprocess.Popen[str] | None) -> None:
    """Stop the launcher (and thus Chrome). Best-effort, never raises."""
    if proc is None:
        return
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()  # triggers the launcher's stdin-end → kills Chrome
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def driver_env(port: int, name: str, browser_driver: str) -> dict[str, str]:
    """The env block that wires an agentic run's CLI to the launched Chrome.

    Both drivers attach over CDP to the SAME pre-authenticated Chrome; only the
    variable names differ, because the two CLIs read different ones.

    Args:
        port: The launched Chrome's CDP port.
        name: The per-session daemon/session name (see :func:`session_name`).
        browser_driver: ``"playwright-cli"`` or ``"browser-harness"``.

    Returns:
        Environment overrides for :func:`claude_cli.run_agentic`.
    """
    if browser_driver == "playwright-cli":
        return {
            "PW_CLI_CDP_URL": f"http://127.0.0.1:{port}",
            "PW_CLI_SESSION": name,
            "PLAYWRIGHT_CLI_JS": str(settings.playwright_cli_js),
        }
    return {"BU_CDP_URL": f"http://127.0.0.1:{port}", "BU_NAME": name}


@contextmanager
def browser_session(
    base_url: str,
    profile_dir: Path,
    name: str,
    *,
    browser_driver: str = "playwright-cli",
):
    """Launch a dedicated Chrome, yield the driver env, and always tear it down.

    Args:
        base_url: URL the browser opens on.
        profile_dir: Persistent user-data dir (created if absent — a fresh one
            simply means an unauthenticated session, which is fine for a public
            target).
        name: Session name, typically from :func:`session_name`.
        browser_driver: Which CLI the agentic run will drive.

    Yields:
        The ``extra_env`` dict to hand :func:`claude_cli.run_agentic`.

    Raises:
        BrowserSessionError: if the CDP endpoint never came up.
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    port = free_port()
    proc = None
    try:
        proc = launch_browser(base_url, port, profile_dir)
        if not wait_cdp(port, CDP_READY_TIMEOUT_S):
            raise BrowserSessionError(f"Chrome CDP endpoint did not come up on port {port}.")
        yield driver_env(port, name, browser_driver)
    finally:
        teardown(proc)
