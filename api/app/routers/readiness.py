"""Setup readiness for the signed-in user (#642).

One endpoint so the SPA never has to re-derive "can this user actually run
anything yet?" from four unrelated resources — and so a new account, especially
one provisioned from EmeHub, is told what it still needs *before* the click
rather than by a failure afterwards (#640).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps_auth import current_user
from app.models import User
from app.services import readiness_service

router = APIRouter(tags=["readiness"])


@router.get("/readiness")
def get_readiness(
    db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> dict:
    """What this user still needs before a run can work.

    Owner-scoped: another user's paired device or captured login must never make
    this account look ready. Each item reports ``required`` under the settings in
    force, so a server-target user is not nagged about a Local Agent that blocks
    nothing for them.
    """
    return readiness_service.check(db, user)
