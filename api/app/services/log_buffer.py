"""In-memory ring buffer of recent backend log lines for the Audit Log page's
Backend Logs tab. A loguru sink (installed from ``setup_logging``) maps each
record into a compact dict and appends it to a bounded deque.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import deque
from datetime import datetime
from typing import Any

_BUFFER: deque[dict[str, Any]] = deque(maxlen=2000)
_installed = False
_bridge_installed = False

# High-frequency polling endpoints whose uvicorn access lines are pure noise:
# the paired agent hits these every ~1s, so at INFO they evict real errors from
# the ring buffer within minutes. Dropped from the buffer unless the line is a
# warning/error (a failing poll still surfaces). Matched as substrings of the
# access message (#394).
_NOISE_PATHS = (
    "/agent/explore/next",
    "/agent/jobs/next",
    "/heartbeat",
    "/healthz",
    "/ai/activity",
)

_LEVEL_MAP = {
    "WARNING": "warn",
    "ERROR": "error",
    "CRITICAL": "error",
    "DEBUG": "debug",
    "TRACE": "debug",
}

_MS_RE = re.compile(r"(\d+)\s?ms\b", re.IGNORECASE)


def _service_for(name: str) -> str:
    """Map a logger/module name to a friendly service label."""
    n = (name or "").lower()
    if "claude" in n or "ai_service" in n or ".ai" in n or "knowledge" in n or "spec_service" in n:
        return "ai-orchestrator"
    if "playwright" in n or "runner" in n or "execution" in n:
        return "test-runner"
    if "link" in n or "sync" in n:
        return "sync-worker"
    if "evidence" in n:
        return "evidence-store"
    if "capture" in n or "auth" in n:
        return "auth"
    if "project_config" in n or "provider" in n or "repo_service" in n or "adapter" in n:
        return "integration"
    return "api-gateway"


def _append(ts: str, level: str, service: str, message: str) -> None:
    """Append one normalized line to the ring buffer (parses `Nms` duration).

    Info-level access lines for high-frequency polling endpoints are dropped so
    they don't crowd real signal out of the buffer; warnings/errors are always
    kept (a failing poll still shows)."""
    if level in ("info", "debug") and any(p in message for p in _NOISE_PATHS):
        return
    m = _MS_RE.search(message)
    duration = int(m.group(1)) if m else None
    trace = hashlib.md5(f"{message}{ts}".encode()).hexdigest()[:6]
    _BUFFER.append({
        "ts": ts,
        "level": level,
        "service": service,
        "message": message,
        "durationMs": duration,
        "trace": trace,
    })


def push(record: dict[str, Any]) -> None:
    """Map a loguru record dict into a buffer line."""
    try:
        t = record["time"]
        ts = t.strftime("%H:%M:%S.") + f"{t.microsecond // 1000:03d}"
        level = _LEVEL_MAP.get(record["level"].name, "info")
        _append(ts, level, _service_for(record["name"]), str(record["message"]))
    except Exception:  # noqa: BLE001 - logging must never raise
        pass


class _BufferHandler(logging.Handler):
    """stdlib logging handler that mirrors records into the ring buffer — lets
    uvicorn's access log and any library that uses the standard ``logging``
    module show up in the Backend Logs tab alongside our loguru records."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            t = datetime.fromtimestamp(record.created)
            ts = t.strftime("%H:%M:%S.") + f"{int(record.msecs):03d}"
            level = _LEVEL_MAP.get(record.levelname, "info")
            _append(ts, level, _service_for(record.name), record.getMessage())
        except Exception:  # noqa: BLE001 - logging must never raise
            pass


def install_stdlib_bridge() -> None:
    """Forward standard-library logging (uvicorn access/error + libraries) into
    the buffer, exactly once. uvicorn's loggers set ``propagate=False``, so the
    handler is attached to them directly as well as to the root logger."""
    global _bridge_installed
    if _bridge_installed:
        return
    handler = _BufferHandler(level=logging.DEBUG)
    for name in ("", "uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        if not any(isinstance(h, _BufferHandler) for h in lg.handlers):
            lg.addHandler(handler)
        # uvicorn.* default to INFO; make sure INFO access lines are emitted.
        if name.startswith("uvicorn") and (lg.level == logging.NOTSET or lg.level > logging.INFO):
            lg.setLevel(logging.INFO)
    _bridge_installed = True


def install_sink() -> None:
    """Add the buffer sink to loguru exactly once."""
    global _installed
    if _installed:
        return
    from app.logging import logger

    def _sink(message: Any) -> None:  # loguru passes a str with a .record attr
        push(message.record)

    logger.add(_sink, level="DEBUG", format="{message}")
    _installed = True


def list_logs(level: str = "all", service: str = "all", q: str = "") -> list[dict[str, Any]]:
    """Newest-first log lines matching the filters (cap 200)."""
    ql = (q or "").strip().lower()
    out: list[dict[str, Any]] = []
    for line in reversed(_BUFFER):
        if level != "all" and line["level"] != level:
            continue
        if service != "all" and line["service"] != service:
            continue
        if ql and ql not in f"{line['message']} {line['service']} {line['trace']}".lower():
            continue
        out.append(line)
        if len(out) >= 200:
            break
    return out


def log_stats() -> dict[str, int]:
    """Aggregate counts over the current buffer."""
    warnings = sum(1 for line in _BUFFER if line["level"] == "warn")
    errors = sum(1 for line in _BUFFER if line["level"] == "error")
    return {
        "logVolume": len(_BUFFER),
        "warnings": warnings,
        "errors": errors,
    }
