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
    """
    return {
        "status": "ok",
        "version": __version__,
        "hubSsoEnabled": settings.hub_sso_enabled,
    }


@router.get("/capabilities")
def capabilities() -> dict:
    """Report which local engines are available (Claude CLI, Playwright)."""
    return {
        "claude": claude_cli.is_available(),
        "version": __version__,
    }
