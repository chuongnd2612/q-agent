"""Ticket comments / publish router.

Endpoints:
  POST  /runs/{run_id}/comments/prepare   -> list[TicketCommentOut]   (draft from report; Claude summarizes)
  GET   /runs/{run_id}/comments           -> list[TicketCommentOut]
  PATCH /comments/{comment_id}            -> TicketCommentOut          (CommentEdit)
  POST  /comments/{comment_id}/publish    -> TicketCommentOut          (publish one via adapter)
  POST  /runs/{run_id}/comments/publish   -> list[TicketCommentOut]    (PublishRequest; publish all/selected)
  POST  /runs/{run_id}/comments/retry     -> list[TicketCommentOut]    (retry failed)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps_auth import current_user
from app.deps_hub import hub_token as hub_token_dep
from app.deps_hub import use_hub_credential
from app.models.comment import TicketComment
from app.models.knowledge import ProjectKnowledge
from app.models.report import Report
from app.models.run import Run
from app.models.ticket import Ticket
from app.models.user import User
from app.schemas import CommentEdit, PublishRequest, TicketCommentOut
from app.services import claude_cli, comment_evidence, run_context, run_control
from app.services.claude_cli import ClaudeError
from app.services.ownership import get_owned_or_404
from app.services.publish_service import publish_one
from app.services.report_service import build_report
from app.services.run_status import set_run_status
from app.services.skills import TICKET_COMMENT_GENERATOR

router = APIRouter(tags=["comments"])


def _maybe_finish_run(db: Session, run_id: int) -> None:
    """Close the ADR 0005 'done' gap: once every comment for the run has
    reached a terminal outcome (published or failed), the pipeline — whose
    last stage is publishing results comments — has reached its natural end.
    """
    run = db.get(Run, run_id)
    if run is None:
        return
    comments = db.execute(
        select(TicketComment).where(TicketComment.run_id == run_id)
    ).scalars().all()
    if comments and all(c.status in ("published", "failed") for c in comments):
        set_run_status(db, run, "done")

# Status mapping applied to the provider work item once a comment is published.
# All cases passing -> "Passed"; any failure -> "QA Failed".
_TARGET_STATUS_ALL_PASS = "Passed"
_TARGET_STATUS_ANY_FAIL = "QA Failed"


def _latest_report(db: Session, run_id: int) -> Report:
    """Latest report for the run, building one on demand if none exists yet.

    Preparing comments shouldn't dead-end the user: the report is derived from the
    run's latest execution, so if they came straight from Evidence without building
    a report first, we build it here rather than 404'ing.
    """
    stmt = select(Report).where(Report.run_id == run_id).order_by(Report.id.desc()).limit(1)
    report = db.execute(stmt).scalars().first()
    if report is None:
        report = build_report(db, run_id)
    return report


def _project_context_block(db: Session, run: Run) -> str:
    """Concise project-KB grounding for the comment prompt (#452-followup): the
    environment URL plus the app's domain/architecture and key screen names, so
    comments use REAL project terminology and URLs instead of generic wording.

    Best-effort — returns "" when the run has no resolvable project or indexed KB.
    """
    try:
        from app.services.playwright_runner import _resolve_project_for_run

        project_key, base_url, _manual, _provider = _resolve_project_for_run(db, run, run.env)
    except Exception:  # noqa: BLE001 - grounding is additive; never block comments
        project_key, base_url = None, ""

    parts: list[str] = []
    if base_url:
        parts.append(f"Environment ({run.env}): {base_url}")
    if project_key:
        row = (
            db.query(ProjectKnowledge)
            .filter(
                ProjectKnowledge.project_key == project_key,
                ProjectKnowledge.owner_id == run.owner_id,
            )
            .order_by(ProjectKnowledge.confidence.desc())
            .first()
        )
        kb = (row.knowledge if row else {}) or {}
        if kb.get("domain"):
            parts.append(f"Domain: {str(kb['domain'])[:600]}")
        if kb.get("architecture"):
            parts.append(f"Architecture: {str(kb['architecture'])[:400]}")
        names = [
            (r.get("name") or r.get("path") or r.get("url"))
            for r in (kb.get("routes") or [])
            if isinstance(r, dict)
        ]
        names = [str(n) for n in names if n][:12]
        if names:
            parts.append("Key screens/routes: " + ", ".join(names))
    return "\n".join(parts)


def _summarize_ticket(
    ticket_external_id: str,
    summary: dict,
    ai_failure_analysis: str,
    run_id: int,
    project_context: str = "",
) -> str:
    """Ask Claude for ONE consolidated QA comment aggregating all of a ticket's
    test cases — overall verdict, per-case breakdown, and consolidated findings.

    The ticket is only "Passed" when every case passed; any failure means the
    ticket failed. Raises ClaudeError to the caller (ADR 0001 — no simulated
    fallback); the router surfaces it as an HTTP error.
    """
    passed, failed, total = summary["passed"], summary["failed"], summary["total"]
    case_lines = []
    for c in summary.get("cases", []):
        status = c.get("status", "")
        mark = "PASS" if status == "pass" else "FAIL" if status == "fail" else status.upper()
        detail = ""
        if status == "fail":
            detail = " — " + (c.get("diagnosis") or c.get("error") or "failed").strip()
        case_lines.append(f"- {c.get('caseCode', '')} {c.get('title', '')}: {mark}{detail}")
    cases_block = "\n".join(case_lines) or "- (no test cases executed)"

    prompt = (
        f"Write ONE consolidated QA result comment to post on ticket {ticket_external_id}. "
        "It must summarize the OVERALL outcome across ALL of the ticket's executed test "
        "cases — the ticket is 'Passed' only if every case passed; any failure means the "
        f"ticket failed. Overall: {passed}/{total} cases passed, {failed} failed.\n\n"
        f"Per test case:\n{cases_block}\n\n"
        + (f"Cross-case failure analysis: {ai_failure_analysis}\n\n" if failed and ai_failure_analysis else "")
        + (
            f"## Project context (use these real URLs + terminology; do not invent names)\n{project_context}\n\n"
            if project_context.strip()
            else ""
        )
        + "Structure the comment as: (1) a one-line overall verdict, (2) a short per-case "
        "breakdown, and (3) consolidated key findings for any failures (fold in each failing "
        "case's diagnosis). Do not include a greeting or signature.\n\n"
        "OUTPUT CONTRACT: Return ONLY the comment body as Markdown — nothing else. Do NOT "
        "prepend any preamble, status line, or commentary about your process, tools, or files "
        "(never mention knowledge.md or whether any file exists). Everything you need is in "
        "this prompt; do NOT read, look for, or reference any files on disk."
    )
    # Attribute to the run so Claude resolves the run OWNER's credential
    # (own→shared) rather than the ambient/shared one — a request thread has no
    # ambient run, so it would otherwise fall back to a possibly-expired shared credential.
    _prev_run = run_context.get_run()
    run_context.set_run(run_id)
    try:
        return claude_cli.run_prompt(
            prompt,
            skill=TICKET_COMMENT_GENERATOR,
            include_template=True,
            label=f"Comment: {ticket_external_id}",
        ).strip()
    finally:
        run_context.set_run(_prev_run)


@router.post("/runs/{run_id}/comments/prepare", response_model=list[TicketCommentOut])
def prepare_comments(
    run_id: int,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
    hub_token: str | None = Depends(hub_token_dep),
) -> list[TicketComment]:
    run = get_owned_or_404(db, Run, run_id, user)
    # Resolve the Claude credential from the hub, with THIS request's fresh token,
    # exactly as the run's own start did (#689). Publishing happens whenever the
    # person gets round to it — often hours after the run — by which point the
    # material pinned at run start is past its expiry and the run's grant has died,
    # so the background re-resolve cannot renew it either. That is what surfaced as
    # `Not logged in · Please run /login` → HTTP 502 on this very endpoint.
    use_hub_credential(run_id, hub_token)
    # Preparing comments is a deliberate, synchronous post-run action. If the run
    # was previously cancelled, its in-memory cancel event lingers (run_control
    # only clears it on retry/delete), and register_process would INSTANTLY SIGKILL
    # this summarize Claude call (exit -9 → ClaudeError → HTTP 502). Drop that stale
    # bookkeeping first — the durable Run.cancel_requested + the terminal-status
    # guard in set_run_status still stand, so this can't un-cancel the run.
    run_control.clear(run_id)
    report = _latest_report(db, run_id)
    ticket_summaries = report.data.get("ticketSummary", [])
    ai_failure_analysis = report.data.get("aiFailureAnalysis", "")
    # Resolve once per run — same project KB grounds every ticket's comment.
    project_context = _project_context_block(db, run)
    # Evidence for every executed case, passes included (#696). Gathered once and
    # sliced per ticket below; nothing is uploaded here — a draft is cheap and
    # repeatable, and pushing files into a work item on every regeneration would
    # litter the ticket. The upload happens on publish.
    evidence_by_ticket = comment_evidence.collect_for_run(db, run_id, run.owner_id)

    tickets = {
        t.external_id: t
        for t in db.execute(
            select(Ticket).where(
                Ticket.external_id.in_([s["ticketExternalId"] for s in ticket_summaries])
            )
        ).scalars()
    }

    comments: list[TicketComment] = []
    for summary in ticket_summaries:
        ticket_external_id = summary["ticketExternalId"]
        ticket = tickets.get(ticket_external_id)
        provider_kind = ticket.provider_kind if ticket else ""
        # Passed only when every approved case's script ran and passed (ticket
        # status from the report); fall back to the failed-count for old reports.
        ticket_status = summary.get("status") or (
            "Passed" if summary["failed"] == 0 else "Failed"
        )
        target_status = (
            _TARGET_STATUS_ALL_PASS if ticket_status == "Passed" else _TARGET_STATUS_ANY_FAIL
        )
        cases_evidence = evidence_by_ticket.get(ticket_external_id, [])
        try:
            body = _summarize_ticket(
                ticket_external_id, summary, ai_failure_analysis, run_id, project_context
            )
            # The prose is the model's; the evidence list is a FACT and is appended by
            # code. A model that invented a trace file into this list would be worse
            # than no list, because the list is what a reader trusts enough to go
            # looking for the file.
            manifest = comment_evidence.manifest_block(cases_evidence)
            if manifest:
                body = body + "\n\n" + manifest
        except ClaudeError as exc:
            raise HTTPException(status_code=502, detail=f"Claude CLI failed: {exc}") from exc

        existing = db.execute(
            select(TicketComment).where(
                TicketComment.run_id == run_id,
                TicketComment.ticket_external_id == ticket_external_id,
            )
        ).scalars().first()
        # Refs, not bytes: a draft may sit for days, and a comment row is not the
        # place to keep megabytes of video. Resolved back to files on publish.
        refs = comment_evidence.attachment_refs(cases_evidence)
        if existing is not None:
            existing.body = body
            existing.target_status = target_status
            existing.status = "draft"
            existing.error_message = ""
            existing.attachments = refs
            comment = existing
        else:
            comment = TicketComment(
                run_id=run_id,
                ticket_external_id=ticket_external_id,
                provider_kind=provider_kind,
                body=body,
                status="draft",
                target_status=target_status,
                attachments=refs,
            )
        db.add(comment)
        comments.append(comment)

    db.commit()
    for c in comments:
        db.refresh(c)

    run = db.get(Run, run_id)
    if run is not None:
        set_run_status(db, run, "comment")

    return comments


@router.get("/runs/{run_id}/comments", response_model=list[TicketCommentOut])
def list_comments(
    run_id: int, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> list[TicketComment]:
    get_owned_or_404(db, Run, run_id, user)
    stmt = select(TicketComment).where(TicketComment.run_id == run_id).order_by(TicketComment.id)
    return list(db.execute(stmt).scalars())


def _get_comment_or_404(db: Session, comment_id: int, user: User | None) -> TicketComment:
    """Resolve a comment, 404ing if missing or if its run isn't owned by ``user``."""
    comment = db.get(TicketComment, comment_id)
    if comment is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    get_owned_or_404(db, Run, comment.run_id, user)
    return comment


@router.patch("/comments/{comment_id}", response_model=TicketCommentOut)
def edit_comment(
    comment_id: int,
    body: CommentEdit,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> TicketComment:
    comment = _get_comment_or_404(db, comment_id, user)
    if body.body is not None:
        comment.body = body.body
    if body.target_status is not None:
        comment.target_status = body.target_status
    db.add(comment)
    db.commit()
    db.refresh(comment)
    return comment


@router.post("/comments/{comment_id}/publish", response_model=TicketCommentOut)
def publish_comment(
    comment_id: int, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> TicketComment:
    comment = _get_comment_or_404(db, comment_id, user)
    result = publish_one(db, comment)
    _maybe_finish_run(db, comment.run_id)
    return result


@router.post("/runs/{run_id}/comments/publish", response_model=list[TicketCommentOut])
def publish_comments(
    run_id: int,
    body: PublishRequest,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> list[TicketComment]:
    get_owned_or_404(db, Run, run_id, user)
    stmt = select(TicketComment).where(TicketComment.run_id == run_id)
    if body.ticket_ids:
        stmt = stmt.where(TicketComment.ticket_external_id.in_(body.ticket_ids))
    comments = list(db.execute(stmt).scalars())
    results = [publish_one(db, c) for c in comments]
    _maybe_finish_run(db, run_id)
    return results


@router.post("/runs/{run_id}/comments/retry", response_model=list[TicketCommentOut])
def retry_comments(
    run_id: int, db: Session = Depends(get_db), user: User | None = Depends(current_user)
) -> list[TicketComment]:
    get_owned_or_404(db, Run, run_id, user)
    stmt = select(TicketComment).where(
        TicketComment.run_id == run_id, TicketComment.status == "failed"
    )
    comments = list(db.execute(stmt).scalars())
    results = [publish_one(db, c) for c in comments]
    _maybe_finish_run(db, run_id)
    return results
