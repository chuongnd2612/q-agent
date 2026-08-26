"""Reports router.

Endpoints:
  POST /runs/{run_id}/report      -> ReportOut         (build/refresh from latest execution + Claude failure analysis)
  GET  /runs/{run_id}/report      -> ReportOut
  GET  /reports                   -> list[ReportOut]   (recent, for the Reports screen)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps_auth import current_user
from app.models.report import Report
from app.models.run import Run
from app.models.user import User
from app.schemas import ReportOut
from app.services.ownership import get_owned_or_404
from app.services.project_config_service import UNASSIGNED_PROJECT
from app.services.report_service import build_report

router = APIRouter(tags=["reports"])


@router.post("/runs/{run_id}/report", response_model=ReportOut)
def create_report(
    run_id: int, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> Report:
    get_owned_or_404(db, Run, run_id, user)
    return build_report(db, run_id)


@router.get("/runs/{run_id}/report", response_model=ReportOut)
def get_run_report(
    run_id: int, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> Report:
    get_owned_or_404(db, Run, run_id, user)
    stmt = (
        select(Report).where(Report.run_id == run_id).order_by(Report.id.desc()).limit(1)
    )
    report = db.execute(stmt).scalars().first()
    if report is None:
        raise HTTPException(status_code=404, detail="No report for this run")
    return report


@router.get("/reports", response_model=list[ReportOut])
def list_reports(
    project: str | None = Query(None),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> list[Report]:
    """Recent reports, scoped to the current user's own runs (data is per-user
    private — see #92). Reports have no ``owner_id`` of their own, so ownership
    is resolved via their run; unowned (pre-ownership) runs stay visible to
    everyone, mirroring ``app.services.ownership.get_owned_or_404``.
    """
    stmt = select(Report).join(Run, Report.run_id == Run.id).order_by(Report.id.desc()).limit(50)
    if user is not None:
        stmt = stmt.where((Run.owner_id.is_(None)) | (Run.owner_id == user.id))
    # Project containment (#727, ADR 0015). Reports have no project of their own;
    # they inherit the run's stamped one, which is exactly why stamping had to be
    # a column — the join can filter on it, a per-row derivation cannot.
    if project == UNASSIGNED_PROJECT:
        stmt = stmt.where(Run.project_guid.is_(None))
    elif project:
        stmt = stmt.where(Run.project_guid == project)
    return list(db.execute(stmt).scalars())
