"""Evidence + screenshot annotation router.

Endpoints:
  GET  /runs/{run_id}/evidence                 -> {tickets: [...], byTicket: {...}}  (grouped by ticket)
  GET  /results/{result_id}/evidence           -> list[EvidenceOut]
  POST /evidence/{evidence_id}/annotate         -> EvidenceOut   (AnnotateRequest; Pillow burns shapes)

Artifacts are served under /artifacts/... (StaticFiles). Annotation writes a new
annotated PNG next to the original and flips Evidence.annotated.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.deps_auth import current_user
from app.deps_hub import hub_token as hub_token_dep
from app.deps_hub import use_hub_credential
from app.models.execution import Evidence, Execution, ExecutionResult
from app.models.run import Run
from app.models.ticket import Ticket
from app.models.user import User
from app.schemas import AnnotateRequest, EvidenceOut, ExecutionResultOut
from app.services import evidence_analysis, report_service
from app.services.annotate import render_annotations
from app.services.ownership import check_owned_or_404, get_owned_or_404
from app.services.workspace_scope import scoped_evidence_dir, served_evidence_path

router = APIRouter(tags=["evidence"])

# Providers show a short glyph + color chip next to the ticket in evidence lists.
_PROVIDER_GLYPH = {"ado": "AD", "jira": "JR", "github": "GH"}
_PROVIDER_COLOR = {"ado": "#0078d4", "jira": "#0052cc", "github": "#24292e"}


def _check_result_owner(
    db: Session, result: ExecutionResult, user: User | None
) -> tuple[Run | None, int | None]:
    """404s unless ``user`` owns the execution behind ``result``.

    Returns ``(run, owner_id)``. The owner id is returned **separately from the
    run** because a project-scoped execution (#796) has no run at all: its
    ``run_id`` is ``NULL`` and it is owned directly. Scoping such a result through
    ``get_owned_or_404(db, Run, None, user)`` would 404 every one of them, which
    is what kept the Automation tab's report viewer (#801) from ever reaching a
    project execution's failure screenshots.

    Callers need the owner id to build the served evidence path (ADR 0009) and
    the run only where a run-scoped side effect is involved (the hub credential),
    so the two are handed back as they are, rather than the run standing in for
    both.
    """
    if result.execution is None:
        return None, None
    if result.execution.run_id is None:
        check_owned_or_404(result.execution, user, not_found="Execution result not found")
        return None, result.execution.owner_id
    run = get_owned_or_404(db, Run, result.execution.run_id, user)
    return run, run.owner_id


def _evidence_out(evidence: Evidence, owner_id: int | None) -> dict:
    """API view of an Evidence row.

    ``Evidence.path`` is stored relative to the scoped evidence root
    (``<RUN-CODE>/<ticket>/<case>/<file>``), so the served path returned here
    prepends the owner's scope + ``evidence/`` to match the ``/artifacts``
    mount (ADR 0009 §5) — the frontend builds the URL by naive concatenation
    (``app/src/lib/api.ts:408``).
    """
    return {
        "id": evidence.id,
        "kind": evidence.kind,
        "filename": evidence.filename,
        "path": served_evidence_path(owner_id, evidence.path) if evidence.path else evidence.path,
        "size_bytes": evidence.size_bytes,
        "annotated": evidence.annotated,
        "meta": evidence.meta,
    }


def _latest_execution(db: Session, run_id: int) -> Execution | None:
    stmt = (
        select(Execution)
        .options(selectinload(Execution.results).selectinload(ExecutionResult.evidence))
        .where(Execution.run_id == run_id)
        .order_by(Execution.id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalars().first()


@router.get("/runs/{run_id}/evidence")
def get_run_evidence(
    run_id: int, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> dict:
    run = get_owned_or_404(db, Run, run_id, user)
    execution = _latest_execution(db, run_id)
    results = list(execution.results) if execution else []

    by_ticket: dict[str, list[ExecutionResult]] = {}
    for r in results:
        by_ticket.setdefault(r.ticket_external_id, []).append(r)

    # Approved automatable cases per ticket — a ticket is "Passed" only when every
    # one of these scripts ran and passed (see report_service.ticket_status).
    approved_counts = (
        report_service.approved_case_counts(db, execution.run_id) if execution else {}
    )

    tickets_meta = {
        t.external_id: t
        for t in db.execute(
            select(Ticket).where(Ticket.external_id.in_(by_ticket.keys()))
        ).scalars()
    }

    tickets_summary = []
    for ticket_external_id, ticket_results in by_ticket.items():
        ticket = tickets_meta.get(ticket_external_id)
        passed = sum(1 for r in ticket_results if r.status == "pass")
        failed = sum(1 for r in ticket_results if r.status == "fail")
        approved = approved_counts.get(ticket_external_id, len(ticket_results))
        provider_kind = ticket.provider_kind if ticket else ""
        tickets_summary.append(
            {
                "id": ticket_external_id,
                "title": ticket.title if ticket else ticket_external_id,
                "pass": passed,
                "fail": failed,
                "approved": approved,
                "provGlyph": _PROVIDER_GLYPH.get(provider_kind, "?"),
                "provColor": _PROVIDER_COLOR.get(provider_kind, "#6b7280"),
                "statusLabel": report_service.ticket_status(approved, passed, failed),
            }
        )

    return {
        "tickets": tickets_summary,
        "byTicket": {
            tid: _results_out(rs, run.owner_id) for tid, rs in by_ticket.items()
        },
    }


def _results_out(results: list[ExecutionResult], owner_id: int | None) -> list[dict]:
    """Dump ``ExecutionResultOut`` rows, rewriting each nested evidence path to
    the served ``<scope>/evidence/...`` form (see ``_evidence_out``)."""
    dumped = [ExecutionResultOut.model_validate(r).model_dump(by_alias=True) for r in results]
    for result in dumped:
        for ev in result.get("evidence", []):
            if ev.get("path"):
                ev["path"] = served_evidence_path(owner_id, ev["path"])
    return dumped


@router.get("/results/{result_id}/evidence", response_model=list[EvidenceOut])
def get_result_evidence(
    result_id: int, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> list[dict]:
    result = db.get(ExecutionResult, result_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Execution result not found")
    _run, owner_id = _check_result_owner(db, result, user)
    return [_evidence_out(e, owner_id) for e in result.evidence]


@router.post("/evidence/{evidence_id}/auto-annotate", response_model=EvidenceOut)
def auto_annotate_evidence(
    evidence_id: int,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
    hub_token: str | None = Depends(hub_token_dep),
) -> dict:
    """Analyze a failure screenshot with Claude vision and burn annotations on it.

    Stores the diagnosis + annotated-image path in ``meta`` and flips ``annotated``.
    502 if the analysis/render couldn't produce an annotation.
    """
    evidence = db.get(Evidence, evidence_id)
    if evidence is None:
        raise HTTPException(status_code=404, detail="Evidence not found")
    if evidence.kind != "screenshot":
        raise HTTPException(status_code=400, detail="Only screenshot evidence can be annotated")

    result = db.get(ExecutionResult, evidence.result_id)
    run, owner_id = _check_result_owner(db, result, user) if result is not None else (None, None)
    error_message = result.error_message if result else ""
    # Same hub credential the run resolved, re-resolved with this request's fresh
    # token (#689) — Q-Agent has no credential of its own to fall back on while it is
    # connected to the hub, and material pinned at run start is routinely expired by
    # the time someone takes a later action on the run.
    if run is not None:
        use_hub_credential(run.id, hub_token)
    if not evidence_analysis.annotate_screenshot(db, evidence, error_message or "", force=True):
        raise HTTPException(status_code=502, detail="Auto-annotation failed (see server logs)")
    db.refresh(evidence)
    return _evidence_out(evidence, owner_id)


@router.post("/evidence/{evidence_id}/annotate", response_model=EvidenceOut)
def annotate_evidence(
    evidence_id: int,
    body: AnnotateRequest,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> dict:
    evidence = db.get(Evidence, evidence_id)
    if evidence is None:
        raise HTTPException(status_code=404, detail="Evidence not found")
    if evidence.kind != "screenshot":
        raise HTTPException(status_code=400, detail="Only screenshot evidence can be annotated")
    result = db.get(ExecutionResult, evidence.result_id)
    _run, owner_id = _check_result_owner(db, result, user) if result is not None else (None, None)

    evidence_root = scoped_evidence_dir(owner_id)
    src_path = evidence_root / evidence.path
    if not src_path.exists():
        raise HTTPException(status_code=404, detail=f"Evidence file not found: {evidence.path}")

    dst_relpath = Path(evidence.path).with_name(Path(evidence.path).stem + "-annotated.png")
    dst_path = evidence_root / dst_relpath

    render_annotations(src_path, body.shapes, dst_path)

    evidence.path = str(dst_relpath).replace("\\", "/")
    evidence.filename = dst_relpath.name
    evidence.size_bytes = dst_path.stat().st_size
    evidence.annotated = True
    db.add(evidence)
    db.commit()
    db.refresh(evidence)
    return _evidence_out(evidence, owner_id)
