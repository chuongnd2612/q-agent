"""Health + capability probe endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from app import __version__
from app.config import settings
from app.services import claude_cli

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    """Liveness probe, plus the handful of flags the SPA needs while anonymous.

    ``hubSsoEnabled`` lives here rather than on ``/capabilities`` (#478)
    deliberately: ``/health`` is in ``main._AUTH_ALLOWLIST`` and ``/capabilities``
    is not, so with ``QAGENT_AUTH_REQUIRED`` on only this endpoint is readable by
    an unauthenticated visitor — and the login screen that renders the "Sign in
    with EmeHub" button (B4) is by definition anonymous.

    ``hubBaseUrl`` rides along for the same reason (#480): the SSO callback screen
    is anonymous too, and it needs the hub's origin to call
    ``POST {hubBaseUrl}/auth/agent-token``. It is a public URL, not a secret —
    the shared JWT secret stays server-side and is never exposed here.

    ``hubDataEnabled`` rides along too (#528). When the hub owns Claude credentials
    and projects, Q-Agent must present that state read-only and hide its own
    configuration controls — offering a switch that doesn't decide the outcome is
    the defect #512 fixed for Settings. The flag *is* the switch, so the SPA reads
    it from one place rather than guessing per resource. Deployment configuration,
    not a secret.
    """
    return {
        "status": "ok",
        "version": __version__,
        "hubSsoEnabled": settings.hub_sso_enabled,
        "hubDataEnabled": settings.hub_data_enabled,
        "hubBaseUrl": settings.hub_base_url.rstrip("/"),
    }


@router.get("/capabilities")
def capabilities() -> dict:
    """Report which local engines are available (Claude CLI, Playwright)."""
    return {
        "claude": claude_cli.is_available(),
        "version": __version__,
    }
