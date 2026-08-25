"""Review Center router — validate AI-generated test cases before automation.

Endpoints implemented:
  GET   /runs/{run_id}/cases                      -> list[TestCaseOut]
  POST  /runs/{run_id}/cases                      -> TestCaseOut        (add manual case; TestCaseCreate)
  PATCH /cases/{case_id}                          -> TestCaseOut        (edit; TestCaseUpdate, sets edited)
  POST  /cases/{case_id}/approval                 -> TestCaseOut        (ApprovalUpdate)
  POST  /cases/{case_id}/regenerate               -> TestCaseOut        (Claude regenerate a single case)
  POST  /runs/{run_id}/approve-all                -> list[TestCaseOut]  (bulk approve non-rejected)
  POST  /runs/{run_id}/tickets/{tid}/approve      -> list[TestCaseOut]  (approve a ticket's cases)

Uses prefix "" (mixes /runs and /cases paths) — declare full paths on each route.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps_auth import current_user
from app.deps_hub import hub_token as hub_token_dep
from app.deps_hub import use_hub_credential
from app.models.run import Run
from app.models.testcase import TestCase
from app.models.user import User
from app.schemas import (
    ApprovalUpdate,
    CreateLinkRequest,
    LinkStatusOut,
    TestCaseCreate,
    TestCaseOut,
    TestCaseUpdate,
)
from app.services import audit_service, link_service
from app.services.ai_service import next_case_code, regenerate_case
from app.services.claude_cli import ClaudeError
from app.services.ownership import get_owned_or_404

router = APIRouter(tags=["review"])


def _get_case_or_404(db: Session, case_id: int, user: User | None) -> TestCase:
    """Resolve a test case, 404ing if missing or if its run isn't owned by ``user``."""
    case = db.query(TestCase).filter(TestCase.id == case_id).first()
    if case is None:
        raise HTTPException(status_code=404, detail="Test case not found")
    get_owned_or_404(db, Run, case.run_id, user)
    return case


@router.get("/runs/{run_id}/cases", response_model=list[TestCaseOut])
def list_cases(
    run_id: int, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> list[TestCase]:
    get_owned_or_404(db, Run, run_id, user)
    return (
        db.query(TestCase)
        .filter(TestCase.run_id == run_id)
        .order_by(TestCase.ticket_external_id, TestCase.code)
        .all()
    )


@router.post("/runs/{run_id}/cases", response_model=TestCaseOut)
def create_case(
    run_id: int,
    body: TestCaseCreate,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> TestCase:
    get_owned_or_404(db, Run, run_id, user)

    code = next_case_code(db, run_id, body.ticket_external_id)
    case = TestCase(
        run_id=run_id,
        ticket_external_id=body.ticket_external_id,
        code=code,
        title=body.title,
        precondition=body.precondition,
        steps=[step.model_dump() for step in body.steps],
        priority=body.priority,
        test_type=body.test_type,
        automation=body.automation,
        platform=body.platform,
        source="manual",
    )
    db.add(case)
    db.commit()
    db.refresh(case)
    return case


@router.patch("/cases/{case_id}", response_model=TestCaseOut)
def update_case(
    case_id: int,
    body: TestCaseUpdate,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> TestCase:
    case = _get_case_or_404(db, case_id, user)

    updates = body.model_dump(exclude_unset=True)
    if "steps" in updates and updates["steps"] is not None:
        updates["steps"] = [step if isinstance(step, dict) else step for step in updates["steps"]]
    for field, value in updates.items():
        if value is not None:
            setattr(case, field, value)
    case.edited = True

    db.add(case)
    db.commit()
    db.refresh(case)
    return case


@router.post("/cases/{case_id}/approval", response_model=TestCaseOut)
def set_case_approval(
    case_id: int,
    body: ApprovalUpdate,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> TestCase:
    case = _get_case_or_404(db, case_id, user)
    case.approval = body.approval
    db.add(case)
    db.commit()
    db.refresh(case)
    _action = {
        "approved": "Approved test case",
        "rejected": "Rejected test case",
    }.get(body.approval, "Reset test case approval")
    audit_service.record(
        category="review", actor_type="user", action=_action,
        target=f"{case.ticket_external_id} · {case.code}",
    )
    return case


@router.post("/cases/{case_id}/regenerate", response_model=TestCaseOut)
def regenerate_single_case(
    case_id: int,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
    hub_token: str | None = Depends(hub_token_dep),
) -> TestCase:
    case = _get_case_or_404(db, case_id, user)
    # Same hub credential the run resolved, re-resolved with this request's fresh
    # token (#689) — Q-Agent has no credential of its own to fall back on while it is
    # connected to the hub, and material pinned at run start is routinely expired by
    # the time someone takes a later action on the run.
    use_hub_credential(case.run_id, hub_token)
    try:
        return regenerate_case(db, case)
    except ClaudeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/runs/{run_id}/testcases/create-link", response_model=LinkStatusOut)
def create_and_link(
    run_id: int,
    body: CreateLinkRequest,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> LinkStatusOut:
    """Create approved test cases in the provider (+link to work items when link=true).

    Runs in the background; poll GET /runs/{id}/linked or subscribe to the run WS
    (sync.progress / sync.done) for results.
    """
    run = get_owned_or_404(db, Run, run_id, user)
    approved = (
        db.query(TestCase)
        .filter(TestCase.run_id == run_id, TestCase.approval == "approved")
        .count()
    )
    if not approved:
        raise HTTPException(status_code=400, detail="No approved test cases to create")
    link_service.start_create_link(run_id, body.link, body.ticket_ids, body.dry_run)
    audit_service.record(
        category="sync", actor_type="ai",
        action="Created & linked test cases" if body.link else "Created test cases",
        target=run.code, meta=f"{approved} approved cases"
        + (" · dry run" if body.dry_run else ""),
    )
    return LinkStatusOut(**link_service.link_status(db, run_id))


@router.get("/runs/{run_id}/linked", response_model=LinkStatusOut)
def linked_status(
    run_id: int, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> LinkStatusOut:
    get_owned_or_404(db, Run, run_id, user)
    return LinkStatusOut(**link_service.link_status(db, run_id))


@router.post("/runs/{run_id}/approve-all", response_model=list[TestCaseOut])
def approve_all(
    run_id: int, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> list[TestCase]:
    run = get_owned_or_404(db, Run, run_id, user)
    cases = (
        db.query(TestCase)
        .filter(TestCase.run_id == run_id, TestCase.approval != "rejected")
        .all()
    )
    for case in cases:
        case.approval = "approved"
        db.add(case)
    db.commit()
    audit_service.record(
        category="review", actor_type="user", action="Approved all test cases",
        target=run.code, meta=f"{len(cases)} cases",
    )
    return (
        db.query(TestCase)
        .filter(TestCase.run_id == run_id)
        .order_by(TestCase.ticket_external_id, TestCase.code)
        .all()
    )


@router.post("/runs/{run_id}/tickets/{tid}/approve", response_model=list[TestCaseOut])
def approve_ticket_cases(
    run_id: int,
    tid: str,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> list[TestCase]:
    get_owned_or_404(db, Run, run_id, user)
    cases = (
        db.query(TestCase)
        .filter(
            TestCase.run_id == run_id,
            TestCase.ticket_external_id == tid,
            TestCase.approval != "rejected",
        )
        .all()
    )
    for case in cases:
        case.approval = "approved"
        db.add(case)
    db.commit()
    audit_service.record(
        category="review", actor_type="user", action="Approved test cases",
        target=tid, meta=f"{len(cases)} cases",
    )
    return (
        db.query(TestCase)
        .filter(TestCase.run_id == run_id, TestCase.ticket_external_id == tid)
        .order_by(TestCase.code)
        .all()
    )
