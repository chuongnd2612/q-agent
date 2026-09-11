"""Persist and serve one Execution's raw Playwright ``report.json`` (#798).

The Automation tab's report viewer (#801) is our own React rendering of
Playwright's JSON reporter output — the steps tree, per-retry results,
stdout/stderr and the *flaky* outcome that ``parse_playwright_report`` flattens
away. That means the raw document has to survive the run, for **both** targets:

* the ``local-agent`` target uploads it via ``POST /agent/jobs/{id}/report``;
* the ``server`` target already wrote it into its staging dir, so
  :func:`store_from_file` copies it to the same place before the dir is reused.

It is a **file under the owner's workspace scope** (ADR 0009), not a column: a
200-spec suite with full step trees runs to megabytes, which has no business
sitting inline on the ``executions`` row that every list query selects. The
directory (``workspace/<scope>/reports/``) is not one of the ``/artifacts``
mount's servable kinds — ``auth_guard`` admits only paths containing
``/evidence/`` — so the only way to read a report back is the authenticated
``GET /executions/{id}/report`` endpoint.

The size cap fails *legibly and without storing anything*, rather than truncating:
a half-written JSON document is not a smaller report, it is an unparseable one,
and the viewer would report it as a corrupt run instead of an oversized one.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.logging import logger
from app.models.execution import Execution
from app.services.workspace_scope import scoped_reports_dir

__all__ = [
    "MAX_REPORT_BYTES",
    "OVERSIZE_MESSAGE",
    "ReportTooLarge",
    "load",
    "oversize_reason",
    "report_path",
    "store",
    "store_from_file",
]

#: ~25 MB. Playwright's JSON reporter is tens of KB for a normal suite and a few
#: MB for a large one with deep step trees; this is a blast-radius cap, not a
#: working limit. Note the report carries no binary attachments (screenshots are
#: uploaded separately as evidence), so nothing legitimate approaches it.
MAX_REPORT_BYTES = 25 * 1024 * 1024

OVERSIZE_MESSAGE = (
    "The Playwright report is too large to store "
    f"(limit {MAX_REPORT_BYTES // (1024 * 1024)} MB). Reduce the number of specs "
    "in one execution, or lower the trace/step detail Playwright records."
)


class ReportTooLarge(ValueError):
    """Raised by :func:`store` when the payload exceeds :data:`MAX_REPORT_BYTES`."""


def oversize_reason(size_bytes: int) -> str | None:
    """``None`` when ``size_bytes`` fits the cap, else the legible refusal.

    Split out so a caller can refuse on the declared ``Content-Length`` before
    reading the body, and reuse the exact same wording after reading it.
    """
    if size_bytes > MAX_REPORT_BYTES:
        return OVERSIZE_MESSAGE
    return None


def report_path(execution: Execution) -> Path:
    """Where this execution's ``report.json`` lives on disk.

    Keyed on the execution id inside the **owner's** scope, so two users' runs
    can never collide and a report is unreadable outside its owner's tree.
    """
    return scoped_reports_dir(execution.owner_id) / f"execution-{execution.id}.json"


def store(execution: Execution, raw: bytes) -> Path:
    """Write ``raw`` as this execution's report, replacing any previous one.

    Args:
        execution: The Execution the report belongs to (supplies owner + id).
        raw: The report file's bytes, exactly as Playwright's JSON reporter
            wrote them. Stored verbatim — it is parsed only to *validate*, never
            re-serialized, so the viewer sees what Playwright produced.

    Returns:
        The path written.

    Raises:
        ReportTooLarge: ``raw`` exceeds :data:`MAX_REPORT_BYTES`; nothing is
            written.
        ValueError: ``raw`` is not valid JSON. Refused up front rather than
            stored, so a later read cannot fail in the viewer with no clue where
            the bad document came from.
    """
    reason = oversize_reason(len(raw))
    if reason is not None:
        raise ReportTooLarge(reason)
    try:
        json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Not a valid Playwright JSON report: {exc}") from exc
    destination = report_path(execution)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(raw)
    logger.info(
        "stored Playwright report for execution {}: {} bytes", execution.id, len(raw)
    )
    return destination


def store_from_file(execution: Execution, source: Path) -> bool:
    """Persist a ``report.json`` the server target already wrote to disk.

    Best-effort by design: the run itself has finished and its results are
    already recorded, so a missing, oversized or unreadable report must not turn
    a completed execution into a failed one. Returns whether it was stored, and
    logs why not.
    """
    try:
        if not source.is_file():
            return False
        raw = source.read_bytes()
        store(execution, raw)
        return True
    except (OSError, ValueError) as exc:
        logger.warning("Could not persist report for execution {}: {}", execution.id, exc)
        return False


def load(execution: Execution) -> bytes | None:
    """This execution's stored report bytes, or ``None`` when there is none."""
    path = report_path(execution)
    try:
        if not path.is_file():
            return None
        return path.read_bytes()
    except OSError as exc:
        logger.warning("Could not read report for execution {}: {}", execution.id, exc)
        return None
