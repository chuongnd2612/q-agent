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

import json

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
from app.services import (
    audit_service,
    claude_cli,
    comment_evidence,
    comment_template,
    run_context,
    run_control,
)
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
) -> tuple[dict[str, str], str]:
    """Ask Claude for the two parts of a comment that need judgement (#703).

    Returns ``(observations_by_case_code, summary)``. The structure around them — the
    greeting, the ENV/Status/OS/Browser header, the numbered list, the inline
    screenshots — is assembled by :mod:`app.services.comment_template` from facts about
    the run, because a model asked to produce those has nothing to produce them *from*
    and will write something plausible instead.

    The ticket is only "Passed" when every case passed; any failure means the ticket
    failed. Raises ClaudeError to the caller (ADR 0001 — no simulated fallback); the
    router surfaces it as an HTTP error.
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
        + "The comment's STRUCTURE is assembled by Q-Agent, not by you (#703) — the "
        "greeting, the ENV/Status/OS/Browser header and the numbered per-case list are "
        "facts about the run and are added around what you write. Your job is the two "
        "pieces that need judgement.\n\n"
        "OUTPUT CONTRACT: Return ONLY a JSON object, no prose and no code fence, shaped "
        "exactly like:\n"
        '{"observations": {"<caseCode>": "<one or two sentences on what this case '
        'verified, or for a failure what actually happened>"}, '
        '"summary": "<2-3 sentences consolidating the outcome, folding in the '
        'cross-case analysis for any failures>"}\n\n'
        "One observation per case code listed above, including the ones that PASSED — a "
        "passing case still needs to say what it verified, because the reader is deciding "
        "whether the coverage is right, not just whether it was green. Write plainly, no "
        "markdown, no bullet characters, no case code prefix (it is already in the "
        "heading). Never mention your process, your tools, or any file (never "
        "knowledge.md, never whether a file exists). Everything you need is in this "
        "prompt; do NOT read or look for any file on disk."
    )
    # Attribute to the run so Claude resolves the run OWNER's credential
    # (own→shared) rather than the ambient/shared one — a request thread has no
    # ambient run, so it would otherwise fall back to a possibly-expired shared credential.
    _prev_run = run_context.get_run()
    run_context.set_run(run_id)
    try:
        raw = claude_cli.run_prompt(
            prompt,
            skill=TICKET_COMMENT_GENERATOR,
            include_template=True,
            label=f"Comment: {ticket_external_id}",
        ).strip()
    finally:
        run_context.set_run(_prev_run)
    return _parse_observations(raw)


def _parse_observations(raw: str) -> tuple[dict[str, str], str]:
    """Pull ``{observations, summary}`` out of the model's reply, tolerantly.

    A reply that is not the agreed JSON is treated as the **summary** rather than
    discarded: the structural half of the comment is ours and is unaffected, so a model
    that ignored the contract costs per-case observations, not the whole comment. That
    is the difference between a comment that is thinner than intended and a 502 in front
    of someone trying to publish.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        # Strip a fence the contract asked it not to use.
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}, text
    if not isinstance(parsed, dict):
        return {}, text
    observations = parsed.get("observations")
    clean = (
        {str(k): str(v).strip() for k, v in observations.items() if str(v).strip()}
        if isinstance(observations, dict)
        else {}
    )
    return clean, str(parsed.get("summary") or "").strip()




def _run_facts(db: Session, run_id: int) -> tuple[str, str, str]:
    """``(env, browser, operating_system)`` for the run's latest execution (#703).

    Every one of these is a claim a reader acts on, so an unknown is returned as ""
    and the template omits the line. The OS is the honest problem: a server-executed
    run happened on this container, but an agent-executed one happened on someone's
    laptop and the agent does not report its platform yet — stating this container's
    OS there would send a reader chasing a platform difference that never existed.
    """
    from app.models.execution import Execution

    execution = (
        db.query(Execution)
        .filter(Execution.run_id == run_id)
        .order_by(Execution.id.desc())
        .first()
    )
    if execution is None:
        return "", "", ""
    on_server = (execution.target or "server") == "server"
    return (
        execution.env or "",
        execution.browser or "",
        comment_template.server_os() if on_server else "",
    )


def _result_rows(
    summary: dict, cases_evidence: list[dict], observations: dict[str, str]
) -> list[dict]:
    """One row per test case for the template's numbered list (#703).

    Driven by the REPORT's case list, not the evidence: a case that captured no
    screenshot still has a result, and dropping it would silently shorten the report.
    Evidence is matched in by case code to supply the inline screenshot.

    The screenshot is the *attachment filename* the publish step will upload, so the
    draft and the published comment name the same file — the adapter swaps it for the
    real embed once it knows the URL.
    """
    shots = {
        case["caseCode"]: next(
            (
                f"{case['caseCode']}-{(file['annotatedPath'] or file['path']).replace(chr(92), '/').rsplit('/', 1)[-1]}"
                for file in case["files"]
                if file["kind"] == "screenshot"
            ),
            "",
        )
        for case in cases_evidence
    }
    # Old reports predate `_per_ticket_summary`'s `cases` list. Falling back to the
    # cases the EVIDENCE knows about keeps a regenerated comment from claiming "no test
    # cases were executed" for a run that plainly executed some — which is the shape a
    # reader would act on, not merely a cosmetic gap.
    cases = summary.get("cases") or [
        {
            "caseCode": case["caseCode"],
            "title": case["title"],
            "status": case["status"],
        }
        for case in cases_evidence
    ]
    rows: list[dict] = []
    for case in cases:
        code = str(case.get("caseCode") or "")
        # A failure's own diagnosis is a better observation than anything generic, and
        # it is already computed; the model's line is the fallback, not the other way
        # round, because the diagnosis was derived from the actual error.
        observation = observations.get(code) or ""
        if case.get("status") == "fail" and not observation:
            observation = str(case.get("diagnosis") or case.get("error") or "").strip()
        rows.append(
            {
                "caseCode": code,
                "title": case.get("title") or "",
                "status": case.get("status") or "",
                "observation": observation,
                "screenshot": shots.get(code, ""),
            }
        )
    return rows


def _build_comment(
    db: Session,
    *,
    run_id: int,
    summary: dict,
    provider_kind: str,
    ai_failure_analysis: str,
    project_context: str,
    cases_evidence: list[dict],
    assignee: str = "",
    run_env: str = "",
    run_browser: str = "",
    operating_system: str = "",
) -> TicketComment:
    """Generate (or regenerate) one ticket's draft comment, in place.

    Shared by the run-wide prepare and the per-comment regenerate (#700), so a
    regenerated comment cannot drift from a freshly prepared one — which is the whole
    point of a Regenerate button that nobody has to think twice about.

    Upserts on ``(run_id, ticket_external_id)`` and always leaves the row as a
    ``draft`` with no error: a regeneration replaces whatever the previous attempt
    left behind, including a failure message that no longer applies.

    The caller commits.
    """
    ticket_external_id = summary["ticketExternalId"]
    # Passed only when every approved case's script ran and passed (ticket status from
    # the report); fall back to the failed-count for old reports.
    ticket_status = summary.get("status") or ("Passed" if summary["failed"] == 0 else "Failed")
    target_status = (
        _TARGET_STATUS_ALL_PASS if ticket_status == "Passed" else _TARGET_STATUS_ANY_FAIL
    )
    try:
        observations, prose = _summarize_ticket(
            ticket_external_id, summary, ai_failure_analysis, run_id, project_context
        )
    except ClaudeError as exc:
        raise HTTPException(status_code=502, detail=f"Claude CLI failed: {exc}") from exc

    # Everything structural is a FACT about the run and is assembled here, not asked
    # for (#703). The model supplies the two parts that need judgement — what each case
    # observed, and the consolidated summary.
    body = comment_template.build_body(
        assignee=assignee,
        env=run_env,
        status="PASSED" if ticket_status == "Passed" else "FAILED",
        operating_system=operating_system,
        browser=comment_template.browser_label(run_browser),
        results=_result_rows(summary, cases_evidence, observations),
        summary=prose,
        # Screenshots are inline above; video and trace have no inline form, and #696's
        # promise is that every case's evidence is NAMED. The manifest keeps that true
        # without the template's shape quietly narrowing it to "screenshots only".
        evidence=comment_evidence.manifest_block(cases_evidence),
    )

    existing = (
        db.execute(
            select(TicketComment).where(
                TicketComment.run_id == run_id,
                TicketComment.ticket_external_id == ticket_external_id,
            )
        )
        .scalars()
        .first()
    )
    # Refs, not bytes: a draft may sit for days, and a comment row is not the place to
    # keep megabytes of video. Resolved back to files on publish.
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
    return comment


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
    # Facts about HOW the run executed, for the comment's header block (#703).
    run_env, run_browser, operating_system = _run_facts(db, run_id)

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
        ticket = tickets.get(summary["ticketExternalId"])
        comments.append(
            _build_comment(
                db,
                run_id=run_id,
                summary=summary,
                provider_kind=ticket.provider_kind if ticket else "",
                ai_failure_analysis=ai_failure_analysis,
                project_context=project_context,
                cases_evidence=evidence_by_ticket.get(summary["ticketExternalId"], []),
                assignee=ticket.assignee if ticket else "",
                run_env=run_env,
                run_browser=run_browser,
                operating_system=operating_system,
            )
        )

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


@router.post("/comments/{comment_id}/regenerate", response_model=TicketCommentOut)
def regenerate_comment(
    comment_id: int,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
    hub_token: str | None = Depends(hub_token_dep),
) -> TicketComment:
    """Rebuild ONE ticket's draft from the current report and evidence (#700).

    Once drafts exist there was no way back: Prepare lives in the Publish screen's
    empty state and disappears with the first draft, so a comment written before the
    evidence manifest existed — or before a case was re-run or healed — kept asserting
    whatever it was generated from, and the only remedy was deleting the row by hand.

    Scoped to one ticket on purpose. The run-wide prepare rebuilds *every* draft, which
    is the wrong tool when one ticket's result changed and the others carry hand edits.

    A **published** comment is refused (409). It is already on the work item, and
    rebuilding it locally then re-publishing would post a *second* comment rather than
    replace the first — a footgun dressed as a feature. Edit stays available for
    anyone who wants the local record to match.
    """
    comment = _get_comment_or_404(db, comment_id, user)
    if comment.status == "published":
        raise HTTPException(
            status_code=409,
            detail=(
                "This comment is already published to the work item. Regenerating it "
                "would post a second comment rather than replace the first — edit it "
                "instead."
            ),
        )
    run = get_owned_or_404(db, Run, comment.run_id, user)
    # Same hub-resolved Claude credential every other run action uses (#689).
    use_hub_credential(comment.run_id, hub_token)
    # See prepare_comments: a lingering cancel event would SIGKILL the summarize call.
    run_control.clear(comment.run_id)

    report = _latest_report(db, comment.run_id)
    summary = next(
        (
            s
            for s in report.data.get("ticketSummary", [])
            if s.get("ticketExternalId") == comment.ticket_external_id
        ),
        None,
    )
    if summary is None:
        # The report no longer covers this ticket — regenerating from nothing would
        # produce a confident comment about a run that does not describe it.
        raise HTTPException(
            status_code=400,
            detail=(
                f"The latest report has no results for {comment.ticket_external_id}, "
                "so there is nothing to regenerate from."
            ),
        )

    evidence_by_ticket = comment_evidence.collect_for_run(db, comment.run_id, run.owner_id)
    run_env, run_browser, operating_system = _run_facts(db, comment.run_id)
    ticket = (
        db.execute(select(Ticket).where(Ticket.external_id == comment.ticket_external_id))
        .scalars()
        .first()
    )
    regenerated = _build_comment(
        db,
        run_id=comment.run_id,
        summary=summary,
        provider_kind=comment.provider_kind,
        ai_failure_analysis=report.data.get("aiFailureAnalysis", ""),
        project_context=_project_context_block(db, run),
        cases_evidence=evidence_by_ticket.get(comment.ticket_external_id, []),
        assignee=ticket.assignee if ticket is not None else "",
        run_env=run_env,
        run_browser=run_browser,
        operating_system=operating_system,
    )
    db.commit()
    db.refresh(regenerated)
    audit_service.record(
        category="publish",
        actor_type="ai",
        action="Regenerated a ticket comment",
        target=f"{run.code} · {comment.ticket_external_id}",
    )
    return regenerated


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
