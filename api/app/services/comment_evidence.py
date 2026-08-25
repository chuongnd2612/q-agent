"""Per-test-case evidence for a ticket comment (#696).

QA needs a published comment to *show* the evidence for every case it reports —
including the ones that passed, because a pass asserted in prose is not a pass
demonstrated. Before this, no evidence reached a work item by any route: the SPA drew
two decorative `evidence.zip` / `trace.zip` chips with no files behind them,
``TicketComment.attachments`` was never populated, and every adapter's
``publish_comment`` accepted an ``attachments`` argument and silently ignored it.

**Two phases, deliberately.** Preparing a comment lists the evidence *inline* in the
draft and uploads nothing; the upload happens when the user publishes. Preparing is a
cheap, repeatable, local act — a reviewer regenerates a draft, edits it, throws it
away — and pushing files into a customer's work item on each of those would litter the
ticket with attachments nobody asked for. See :mod:`app.services.publish_service` for
the second phase.

**The manifest is built by code, never by Claude.** The prose summary is the model's
(``_summarize_ticket``); which artifacts exist, and how big they are, is a fact. A
model that hallucinated a trace file into an evidence list would be worse than no list
at all, because the list is what a reader trusts to go looking.

Linking to Q-Agent's own ``/artifacts`` is not an option here: those URLs need a
short-lived ``?token=`` access token (see ``main.py``), so a link pasted into a ticket
is dead for whoever reads it. Hence real attachments at publish time.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.execution import Evidence, Execution, ExecutionResult
from app.services.workspace_scope import scoped_evidence_dir

__all__ = ["collect_for_run", "manifest_block", "attachment_refs"]

#: Order artifacts are listed in, most-useful-first for a human reading a ticket.
_KIND_ORDER = ("screenshot", "video", "trace", "console", "network")

#: What each kind is called in a comment. Provider-neutral plain words: the audience
#: is whoever picks the ticket up, not a Q-Agent user.
_KIND_LABEL = {
    "screenshot": "Screenshot",
    "video": "Video",
    "trace": "Playwright trace",
    "console": "Console log",
    "network": "Network log",
}


def _human_size(size_bytes: int) -> str:
    """A size a person can read. Never "0 bytes" for a file that exists."""
    if size_bytes <= 0:
        return "—"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def collect_for_run(db: Session, run_id: int, owner_id: int | None) -> dict[str, list[dict[str, Any]]]:
    """Evidence for every executed case of ``run_id``, grouped by ticket.

    Returns ``{ticket_external_id: [case, ...]}`` where each case is
    ``{caseCode, title, status, files: [{kind, filename, path, absPath, sizeBytes}]}``.

    **Every executed case is included, passing ones too.** A run's passes are the bulk
    of the evidence QA is asked to produce, and filtering them out here is what would
    make "including passes" impossible to honour downstream.

    ``path`` stays workspace-relative (it is what the DB holds and what the SPA
    resolves); ``absPath`` is resolved once here so the publish phase does not have to
    know about evidence scoping.
    """
    root = scoped_evidence_dir(owner_id)
    results = (
        db.query(ExecutionResult)
        .join(Execution, ExecutionResult.execution_id == Execution.id)
        .filter(Execution.run_id == run_id)
        .order_by(ExecutionResult.id)
        .all()
    )
    by_ticket: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        files: list[dict[str, Any]] = []
        for ev in sorted(
            result.evidence or [],
            key=lambda e: (_KIND_ORDER.index(e.kind) if e.kind in _KIND_ORDER else len(_KIND_ORDER)),
        ):
            files.append(
                {
                    "kind": ev.kind,
                    "filename": ev.filename,
                    "path": ev.path,
                    "absPath": str(root / ev.path),
                    "sizeBytes": int(ev.size_bytes or 0),
                    # Prefer the annotated copy when one exists: it is the picture a
                    # reviewer actually marked up, and the plain one is a worse
                    # answer to "show me what went wrong".
                    "annotatedPath": ((ev.meta or {}).get("annotatedPath") or "")
                    if ev.kind == "screenshot"
                    else "",
                }
            )
        # A case with no artifacts is still reported — silently dropping it would make
        # the comment claim evidence coverage it does not have.
        by_ticket.setdefault(result.ticket_external_id, []).append(
            {
                "caseCode": result.case_code,
                "title": result.title,
                "status": result.status,
                "files": files,
            }
        )
    return by_ticket


def manifest_block(cases: list[dict[str, Any]]) -> str:
    """The inline evidence section for one ticket's comment, as Markdown.

    Empty string when the ticket has no executed cases at all — an "Evidence" heading
    with nothing under it reads as a failure to capture rather than as nothing to say.
    """
    if not cases:
        return ""
    lines = ["**Evidence per test case:**"]
    for case in cases:
        mark = {"pass": "PASS", "fail": "FAIL"}.get(case["status"], case["status"].upper())
        lines.append(f"- {case['caseCode']} — {mark}")
        if not case["files"]:
            # Said plainly rather than omitted: "no artifacts captured" is itself
            # information about the run, and a silent gap looks like a bug.
            lines.append("  - no artifacts captured")
            continue
        for file in case["files"]:
            label = _KIND_LABEL.get(file["kind"], file["kind"].title())
            lines.append(f"  - {label}: {file['filename']} ({_human_size(file['sizeBytes'])})")
    return "\n".join(lines)


def attachment_refs(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The file refs to upload when this comment is published.

    Stored on ``TicketComment.attachments`` at prepare time and consumed by
    :mod:`app.services.publish_service`. Refs, not bytes: a draft may sit for days,
    and a comment row is not the place to keep megabytes of video.

    Screenshots resolve to their **annotated** copy when there is one. Console and
    network logs are excluded — they are JSON blobs the DB already holds, and
    attaching them to a work item is noise rather than evidence.
    """
    refs: list[dict[str, Any]] = []
    for case in cases:
        for file in case["files"]:
            if file["kind"] in ("console", "network"):
                continue
            path = file["annotatedPath"] or file["path"]
            refs.append(
                {
                    "caseCode": case["caseCode"],
                    "kind": file["kind"],
                    # The uploaded name carries the case, so a work item with a dozen
                    # attachments is still readable — the raw names are all
                    # `test-failed-1.png`.
                    "filename": f"{case['caseCode']}-{_basename(path)}",
                    "path": path,
                    "sizeBytes": file["sizeBytes"],
                }
            )
    return refs


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def absolute_path(ref: dict[str, Any], owner_id: int | None) -> str:
    """Resolve a stored ref back to a file on disk at publish time."""
    return str(scoped_evidence_dir(owner_id) / ref["path"])
