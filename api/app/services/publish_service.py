"""Publish orchestration — pushes a prepared TicketComment to its provider.

Resolves the ticket's provider, decrypts its stored secrets, builds a live
adapter (per ADR 0001 — real REST calls, no simulated fallback), posts the
comment body, optionally transitions the work item status, and records the
outcome on the TicketComment row. Emits `publish.status` WS events so run-scoped
screens can reflect progress live.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import crypto
from app.models.comment import TicketComment
from app.models.ticket import Ticket
from app.services import audit_service, comment_evidence, connection_service
from app.services.adapters import ProviderError, get_adapter
from app.ws import hub


def _resolve_connection(db: Session, comment: TicketComment):
    """Resolve the work-item connection a comment publishes through (ADR 0006).

    Routes by the comment's ticket → its work-item connection. Falls back to the
    first connection of the comment's stamped ``provider_kind`` when the ticket
    row is missing.
    """
    ticket = (
        db.execute(select(Ticket).where(Ticket.external_id == comment.ticket_external_id))
        .scalars()
        .first()
    )
    if ticket is not None:
        return connection_service.resolve_work_item_for_ticket(db, ticket)
    if comment.provider_kind:
        conn = connection_service.first_of_kind(db, comment.provider_kind)
        if conn is not None:
            return conn
    raise ProviderError(
        f"Work-item provider for '{comment.ticket_external_id}' is not configured"
    )


def _build_adapter(db: Session, comment: TicketComment):
    """Resolve the comment's connection, decrypt secrets, and build a live adapter."""
    connection = _resolve_connection(db, comment)
    decrypted_secrets = {k: crypto.decrypt(v) for k, v in (connection.secrets or {}).items()}
    return get_adapter(connection.kind, connection.config or {}, decrypted_secrets)


def _evidence_paths(db: Session, comment: TicketComment) -> tuple[list[str], list[str]]:
    """Resolve a draft's evidence refs to files on disk. Returns (present, missing).

    The refs were stored at prepare time and the upload happens now (#696) — a draft
    is cheap and repeatable, so uploading on every regeneration would litter the work
    item with attachments nobody asked for. The cost of deferring is that a file can
    disappear between the two (a purged run, a cleaned workspace), which is why
    missing ones are *named* rather than skipped: a comment that quietly attaches four
    of five screenshots is the failure a reviewer is least likely to notice.
    """
    from pathlib import Path

    from app.models.run import Run

    run = db.get(Run, comment.run_id)
    owner_id = run.owner_id if run is not None else None
    present: list[str] = []
    missing: list[str] = []
    for ref in comment.attachments or []:
        if not isinstance(ref, dict) or not ref.get("path"):
            continue
        absolute = comment_evidence.absolute_path(ref, owner_id)
        if Path(absolute).is_file():
            present.append(absolute)
        else:
            missing.append(str(ref.get("filename") or ref["path"]))
    return present, missing


def _body_for_provider(comment: TicketComment, adapter, missing: list[str]) -> str:
    """The body to post, told the truth about what will and will not be attached.

    The draft already lists every case's evidence inline (that is what the reviewer
    approved). What this adds is the part only the provider knows: whether the files
    themselves are coming. An adapter that cannot attach says so, rather than leaving
    a reader hunting a work item for files that were never going to arrive — which is
    exactly what the old fake `evidence.zip` chip invited.
    """
    body = comment.body
    if missing:
        body = f"{body}\n\n_Evidence no longer on file: {', '.join(sorted(missing))}._"
    if (comment.attachments or []) and not adapter.supports_attachments():
        body = (
            f"{body}\n\n_The evidence above is held in Q-Agent; this provider does not "
            "support comment attachments._"
        )
    return body


def publish_one(db: Session, comment: TicketComment) -> TicketComment:
    """Publish a single draft/failed comment to its provider and persist the outcome.

    On success sets status='published' + external_comment_id, and applies the
    target_status transition on the ticket if one was set. On failure sets
    status='failed' + error_message. Always emits a `publish.status` WS event.
    """
    comment.status = "publishing"
    db.add(comment)
    db.commit()
    hub.publish(
        str(comment.run_id), "publish.status", {"ticket": comment.ticket_external_id, "status": "publishing"}
    )

    try:
        adapter = _build_adapter(db, comment)
        # Phase two of #696: the draft listed the evidence, this uploads it. Paths, not
        # the stored refs — `attachments` used to be handed straight to the adapter,
        # which is a list of dicts no adapter could have done anything with even if one
        # had tried to.
        present, missing = _evidence_paths(db, comment)
        external_id = adapter.publish_comment(
            comment.ticket_external_id,
            _body_for_provider(comment, adapter, missing),
            attachments=present if adapter.supports_attachments() else None,
        )
        if comment.target_status:
            adapter.update_status(comment.ticket_external_id, comment.target_status)
    except Exception as exc:  # noqa: BLE001 - surface any adapter/provider failure
        comment.status = "failed"
        comment.error_message = str(exc)
        db.add(comment)
        db.commit()
        db.refresh(comment)
        hub.publish(
            str(comment.run_id),
            "publish.status",
            {"ticket": comment.ticket_external_id, "status": "failed"},
        )
        audit_service.record(
            category="comment", actor_type="ai", action="Posted results comment",
            target=comment.ticket_external_id, status="error", meta=comment.error_message,
        )
        return comment

    comment.status = "published"
    comment.external_comment_id = external_id
    comment.error_message = ""
    db.add(comment)
    db.commit()
    db.refresh(comment)
    hub.publish(
        str(comment.run_id), "publish.status", {"ticket": comment.ticket_external_id, "status": "published"}
    )
    audit_service.record(
        category="comment", actor_type="ai", action="Posted results comment",
        target=comment.ticket_external_id, meta="Comment published",
    )
    return comment
