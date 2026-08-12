"""Shipping a persistent automation project to a stateless Local Agent (#541).

The Local Agent is fully stateless: specs arrive inline as ``specs[].code``, are
written into an ``os.tmpdir()`` workdir, and the workdir is deleted when the job
ends. There is **no list-dir/read-file capability in either direction**, so a
layered project (page objects, fixtures, data) cannot be fetched on demand — it
must ship *wholesale* with the claim.

This module owns the two things that makes safe:

* :func:`bundle_payload` — the ``{"baseVersion", "files"}`` bundle, excluding
  ``tests/**`` (other runs' specs are irrelevant and inflate the payload),
  ``.qagent/**`` and ``node_modules/**``, with a hard :data:`BUNDLE_MAX_BYTES`
  cap. Over-cap fails fast with a reason instead of shipping a truncated tree
  that would fail collection on the device.
* :func:`version_ok` — the version-skew guard. A device below
  :data:`MIN_AGENT_VERSION`, **or reporting no version at all**, cannot
  materialize a nested tree; it would flatten the bundle into one directory and
  every relative import would fail collection. That is a silent mass failure
  across the whole run, so the server refuses the claim and fails the execution
  with :data:`UPDATE_MESSAGE` instead.

``baseVersion`` ships from day one so a later content-hash delta protocol can be
introduced without another payload change.
"""

from __future__ import annotations

import re

from app.logging import logger
from app.models.automation_project import AutomationProject
from app.services import automation_project_service

__all__ = [
    "BUNDLE_BASE_VERSION",
    "BUNDLE_MAX_BYTES",
    "MIN_AGENT_VERSION",
    "OVERSIZE_MESSAGE",
    "UPDATE_MESSAGE",
    "bundle_payload",
    "parse_version",
    "version_ok",
]

# Bundle protocol version. "1" is "the whole library, every time". A future
# content-hash delta protocol bumps this rather than changing the payload shape.
BUNDLE_BASE_VERSION = "1"

# ~5 MB. A mature project is 40-100 text files, realistically 200-600 KB, so this
# is a blast-radius cap, not a working limit: hitting it means something is wrong
# (a committed fixture dump, a generated artifact) and shipping it would stall
# every claim on that project.
BUNDLE_MAX_BYTES = 5 * 1024 * 1024

# The first agent release that materializes `project.files[]` into a nested tree
# and computes depth-aware `fixtures` specifiers. Anything below this — including
# a device that reports nothing — is refused for layered specs.
MIN_AGENT_VERSION = "0.2.0"

UPDATE_MESSAGE = (
    "Update your Local Agent to run layered specs — this project ships a nested "
    f"automation project that requires agent v{MIN_AGENT_VERSION} or newer."
)

OVERSIZE_MESSAGE = (
    "The automation project is too large to ship to the Local Agent "
    f"(limit {BUNDLE_MAX_BYTES // (1024 * 1024)} MB). Remove large or generated "
    "files from the project and re-run."
)

_VERSION_RE = re.compile(r"^\s*v?(\d+)\.(\d+)\.(\d+)")


def parse_version(raw: str | None) -> tuple[int, int, int] | None:
    """``"0.2.1"`` / ``"v0.2.1-beta"`` -> ``(0, 2, 1)``; junk -> ``None``.

    Pre-release/build suffixes are ignored (compared as their release version),
    which is deliberate: a maintainer testing ``0.2.0-rc1`` should not be locked
    out of the feature the release contains.
    """
    if not raw:
        return None
    match = _VERSION_RE.match(str(raw))
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def version_ok(reported: str | None, minimum: str = MIN_AGENT_VERSION) -> bool:
    """True only when ``reported`` is a parseable version >= ``minimum``.

    **An absent, empty, ``"unknown"`` or unparseable version is False.** That is
    the entire point of the guard: pre-#541 agents send no version, and letting
    them proceed produces a wall of import errors that reads as a product
    failure instead of a stale install.
    """
    current = parse_version(reported)
    if current is None:
        return False
    floor = parse_version(minimum) or (0, 0, 0)
    return current >= floor


def bundle_payload(project: AutomationProject) -> tuple[dict, int]:
    """The agent-bound project bundle plus its total source size in bytes.

    Wraps :func:`automation_project_service.bundle_for_agent` (which already
    excludes ``tests/**``, ``.qagent/**`` and ``node_modules/**``) into the wire
    shape and measures it. The size is **logged here** so the server side of
    every ship is on the record next to the agent's own log line.

    Returns:
        ``({"baseVersion": ..., "files": [{"path", "code"}, ...]}, total_bytes)``.
        The caller compares ``total_bytes`` against :data:`BUNDLE_MAX_BYTES` and
        decides how to fail — this function never raises.
    """
    files = automation_project_service.bundle_for_agent(project)
    total = sum(len(code.encode("utf-8")) for code in files.values())
    payload = {
        "baseVersion": BUNDLE_BASE_VERSION,
        "files": [{"path": path, "code": files[path]} for path in sorted(files)],
    }
    if total > BUNDLE_MAX_BYTES:
        logger.warning(
            "automation bundle for project {} is OVER CAP: {} files, {} bytes (limit {})",
            project.id,
            len(files),
            total,
            BUNDLE_MAX_BYTES,
        )
    else:
        logger.info(
            "automation bundle for project {}: {} files, {} bytes",
            project.id,
            len(files),
            total,
        )
    return payload, total
