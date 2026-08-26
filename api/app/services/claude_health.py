"""Did the Claude credential actually work last time we used it? (#736)

The AI chip reported **Operational** while every Claude call was failing with
``Not logged in · Please run /login``, because both signals behind it answer a
different question than the one a reader takes them for:

* ``claude_cli.is_available()`` runs ``claude --version``. Its own docstring says
  *"does not verify auth"* — it means the binary is installed, which it always is.
* The credential's ``status`` field is the **store's claim** about its row. The hub
  reports ``active``/``refreshable`` because a row exists and has a refresh token,
  which says nothing about whether the material still authenticates.

So a credential can be dead while both report healthy. The missing signal is the only
authoritative one: **the outcome of the last real call**. A CLI invocation that came back
"not logged in" is an observation; a stored status field is an assertion.

This is also the only signal that works in hub-data mode at all.
``claude_cli._mark_credential_invalid`` flags the local ``claude_credentials`` row —
and in hub mode there is no local row, so the existing failure path was a silent no-op
precisely where the credential lives.

**Recorded, not probed.** Asking the API whether the token is good costs a Claude call
per poll, and the chip polls. Every real call already produces the answer for free; this
just remembers it.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import settings
from app.db import utcnow
from app.logging import logger

__all__ = ["record_success", "record_auth_failure", "status"]


def _path() -> Path:
    return settings.workspace_dir / "claude-health.json"


def record_success() -> None:
    """The credential authenticated. Clears any outstanding failure."""
    _write({"ok": True, "at": utcnow().isoformat(), "detail": ""})


def record_auth_failure(detail: str) -> None:
    """The credential was REJECTED — not merely a failed call.

    Called only for an authentication failure (see ``claude_cli._AUTH_ERROR_MARKERS``),
    never for a rate limit, a bad model or a crashed pass. Those are problems with the
    request, and flagging the credential for them would put an amber warning on the chip
    every time something unrelated went wrong — which is how a health indicator stops
    being read at all.
    """
    _write({"ok": False, "at": utcnow().isoformat(), "detail": detail[:300]})


def status() -> dict[str, Any]:
    """``{"ok": bool, "at": iso|None, "detail": str}``.

    ``ok`` is True when nothing has been recorded: a workspace that has never made a
    Claude call has no evidence of a problem, and inventing one would warn every fresh
    install about a credential it has not tried yet.
    """
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": True, "at": None, "detail": ""}
    if not isinstance(raw, dict):
        return {"ok": True, "at": None, "detail": ""}
    return {
        "ok": bool(raw.get("ok", True)),
        "at": str(raw.get("at") or "") or None,
        "detail": str(raw.get("detail") or ""),
    }


def _write(payload: dict[str, Any]) -> None:
    """Persist the outcome. Never raises — health bookkeeping must not fail a run."""
    try:
        settings.workspace_dir.mkdir(parents=True, exist_ok=True)
        _path().write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:  # pragma: no cover - disk/permissions; not fatal
        logger.warning("could not record Claude credential health: {}", exc)


def since(payload: dict[str, Any]) -> datetime | None:
    """Parse ``at`` back to a datetime, or None."""
    at = payload.get("at")
    if not at:
        return None
    try:
        return datetime.fromisoformat(str(at))
    except ValueError:
        return None
