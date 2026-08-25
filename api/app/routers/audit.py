"""Audit Log router — read-only.

Endpoints:
  GET    /audit/events?category=&actor=&q=&run=  -> list[AuditEventOut]  (newest first)
  DELETE /audit/events                       -> {deleted: int}        (clear all)
  GET    /audit/stats                        -> {eventsToday, aiActions, userActions, failures}
  GET    /audit/logs?level=&service=&q=      -> list[BackendLogOut]  (in-memory buffer, newest first)
  GET    /audit/logs/stats                   -> {logVolume, warnings, errors}

Returns plain camelCase dicts (the wire format the app expects).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import audit_service, log_buffer

router = APIRouter(tags=["audit"])


@router.get("/audit/events")
def audit_events(
    category: str = "all",
    actor: str = "all",
    q: str = "",
    run: str = "",
    db: Session = Depends(get_db),
) -> list[dict]:
    """Filtered activity events, newest first. ``run`` scopes to one run's code."""
    return audit_service.list_events(db, category=category, actor=actor, q=q, run=run)


@router.delete("/audit/events")
def clear_audit_events(db: Session = Depends(get_db)) -> dict:
    """Delete every recorded audit event (and stop history re-seeding on restart)."""
    return {"deleted": audit_service.clear_events(db)}


@router.get("/audit/stats")
def audit_stats(db: Session = Depends(get_db)) -> dict:
    """Aggregate activity counts (events today, AI vs user actions, failures)."""
    return audit_service.stats(db)


@router.get("/audit/logs")
def audit_logs(level: str = "all", service: str = "all", q: str = "") -> list[dict]:
    """Recent backend log lines from the in-memory buffer, newest first."""
    return log_buffer.list_logs(level=level, service=service, q=q)


@router.get("/audit/logs/stats")
def audit_log_stats() -> dict:
    """Aggregate log counts over the current buffer."""
    return log_buffer.log_stats()
