"""Shared evidence persistence — extracted from
``playwright_runner._store_evidence`` (Local Agent feature, #DRY) so both the
server runner (copies files a local Playwright process just wrote) and the
Local Agent's multipart evidence-upload endpoint (``routers/agent.py``) write
evidence through one implementation.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path, PurePosixPath

from sqlalchemy.orm import Session

from app.logging import logger
from app.models.execution import Evidence, ExecutionResult
from app.models.run import Run
from app.services.workspace_scope import scoped_evidence_dir

# Log-capture "evidence" kinds (#456): these carry JSON data, not media, and are
# written into the result's own columns rather than stored as files.
_LOG_CAPTURE_COLUMNS = {"console": "console_logs", "network": "network_logs"}


def is_log_capture(kind: str) -> bool:
    """Whether ``kind`` is a JSON log capture (console/network) rather than media."""
    return kind in _LOG_CAPTURE_COLUMNS


def apply_log_capture(
    result: ExecutionResult, kind: str, src_file_or_bytes: str | Path | bytes | bytearray
) -> bool:
    """Parse a console/network JSON capture into the matching result column (#456).

    Console/network are captured by the injected Playwright fixtures and flow
    through the same attachment/upload path as media evidence, but they are DATA
    (a list of log entries), so instead of storing a file we decode the JSON and
    assign it to ``result.console_logs`` / ``result.network_logs`` — the columns
    the Evidence UI reads. Shared by the server runner (passes a file path a local
    Playwright process just wrote) and the Local Agent upload endpoint (passes the
    uploaded bytes). The caller commits.

    Args:
        result: The ExecutionResult whose column is populated.
        kind: ``"console"`` or ``"network"``.
        src_file_or_bytes: The JSON payload — a path to read, or raw bytes.

    Returns:
        True if a list was parsed and assigned; False on a missing/unparseable
        payload (best-effort — a capture failure never fails the result).
    """
    column = _LOG_CAPTURE_COLUMNS.get(kind)
    if column is None:
        return False
    try:
        if isinstance(src_file_or_bytes, (bytes, bytearray)):
            raw = bytes(src_file_or_bytes).decode("utf-8")
        else:
            src = Path(src_file_or_bytes)
            if not src.exists():
                return False
            raw = src.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        logger.warning("Failed to parse {} capture: {}", kind, exc)
        return False
    if not isinstance(data, list):
        return False
    setattr(result, column, data)
    return True


def _destination_dir(run: Run | None, result: ExecutionResult) -> Path:
    """``<scoped evidence root>/<execution label>/<case or spec segments>``.

    A run-scoped result is filed the way it always has been —
    ``<run.code>/<ticket>/<case>/`` — so nothing already on disk moves.

    A **project-scoped** result (#798) has no run, no ticket and no case code:
    its only identity is ``spec_path``. It is filed under the execution id (the
    same key ``project_execution.staging_label`` uses, so the two are obviously
    the same run) plus the spec's own repo-relative directories, with the spec
    filename as the leaf directory. That keeps two specs of the same execution
    from overwriting each other's ``screenshot.png``.
    """
    if run is not None:
        return (
            scoped_evidence_dir(run.owner_id)
            / run.code
            / result.ticket_external_id
            / result.case_code
        )
    execution = result.execution
    relative = PurePosixPath((result.spec_path or f"result-{result.id}").replace("\\", "/"))
    segments = [part for part in relative.parts if part not in ("", ".", "..")]
    return scoped_evidence_dir(execution.owner_id).joinpath(
        f"projexec-{execution.id}", *segments
    )


def store_uploaded_evidence(
    db: Session,
    run: Run | None,
    result: ExecutionResult,
    kind: str,
    src_file_or_bytes: str | Path | bytes | bytearray,
    filename: str,
) -> Evidence | None:
    """Persist one evidence artifact and record its ``Evidence`` row.

    Writes into the owner's scoped evidence dir (ADR 0009 §1) — see
    :func:`_destination_dir` for the two layouts (run-scoped and project-scoped).

    Args:
        db: Active session (the created row is added but not committed — the
            caller commits, matching the rest of this codebase's session
            handling).
        run: The run whose owner scopes the on-disk evidence root, or ``None``
            for a project-scoped execution (#798), whose owner and layout come
            from ``result.execution`` instead.
        result: The ExecutionResult the evidence belongs to.
        kind: Evidence kind (see ``EVIDENCE_KINDS``).
        src_file_or_bytes: Either a filesystem path to copy (the server runner's
            case — a local Playwright process already wrote the file) or raw
            bytes to write directly (the Local Agent's multipart upload case).
        filename: Destination filename (e.g. the original attachment's basename,
            or the uploaded file's name).

    Returns:
        The created (uncommitted) ``Evidence`` row, or ``None`` if a given
        source path does not exist, or the copy/write failed.
    """
    evidence_root = scoped_evidence_dir(
        run.owner_id if run is not None else result.execution.owner_id
    )
    dest_dir = _destination_dir(run, result)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename

    if isinstance(src_file_or_bytes, (bytes, bytearray)):
        try:
            dest.write_bytes(bytes(src_file_or_bytes))
        except OSError as exc:
            logger.warning("Failed to write evidence {}: {}", dest, exc)
            return None
    else:
        src = Path(src_file_or_bytes)
        if not src.exists():
            return None
        try:
            shutil.copy2(src, dest)
        except OSError as exc:
            logger.warning("Failed to copy evidence {}: {}", src, exc)
            return None

    rel_path = dest.relative_to(evidence_root).as_posix()
    evidence = Evidence(
        result_id=result.id,
        kind=kind,
        path=rel_path,
        filename=dest.name,
        size_bytes=dest.stat().st_size,
    )
    db.add(evidence)
    return evidence
