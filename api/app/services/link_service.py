"""Create approved test cases in the provider and link them to their work items.

Runs in a background thread (provider writes can be slow — several per ticket),
publishing ``sync.progress`` / ``sync.done`` over the run's WebSocket topic, and
persists LinkedTestCase rows shown on the Ticket detail page.
"""

from __future__ import annotations

import threading
from typing import Any

from app import crypto
from app import db as db_module
from app.logging import logger
from app.models.linked import LinkedTestCase
from app.models.project_config import ProjectConfig
from app.models.run import Run
from app.models.testcase import TestCase
from app.models.ticket import Ticket
from app.services import connection_service, run_control
from app.services.adapters import get_adapter
from app.services.adapters.base import ProviderError
from app.services.run_status import set_run_status
from app.ws import hub

# Runs currently executing a create+link pass (drives the 'running' status).
_running: set[int] = set()


def is_running(run_id: int) -> bool:
    return run_id in _running


def forget_run(run_id: int) -> None:
    """Clear the in-flight create+link marker for a run (#420, on stop)."""
    _running.discard(run_id)


def link_status(db, run_id: int) -> dict[str, Any]:
    """Current create+link status + per-ticket results for a run."""
    rows = db.query(LinkedTestCase).filter(LinkedTestCase.run_id == run_id).all()
    by_ticket: dict[str, list[LinkedTestCase]] = {}
    for r in rows:
        by_ticket.setdefault(r.ticket_external_id, []).append(r)
    results = [
        {
            "ticketExternalId": tid,
            "providerKind": items[0].provider_kind,
            "count": len(items),
            "created": True,
            "linked": any(i.linked for i in items),
            "local": all(str(i.external_id).startswith("LOCAL-") for i in items),
            "error": "",
        }
        for tid, items in by_ticket.items()
    ]
    status = "running" if run_id in _running else ("done" if rows else "idle")
    return {"status": status, "results": results}


def start_create_link(
    run_id: int, link: bool, ticket_ids: list[str] | None, dry_run: bool = False
) -> None:
    """Kick off the background create+link pass (no-op if already running).

    When ``dry_run`` is True, cases are recorded locally with a ``LOCAL-`` marker
    and the provider is never called — nothing is created in the live project.
    """
    if run_id in _running:
        return
    _running.add(run_id)
    threading.Thread(
        target=_worker, args=(run_id, link, ticket_ids or [], dry_run), daemon=True
    ).start()


def _adapter_for(db, connection, cache: dict[int, Any]) -> Any:
    """Build (and cache, keyed by **connection id**) the adapter for a connection.

    Keying by connection id — not provider kind — lets two connections of the same
    kind (e.g. two ADO accounts) each get their own adapter within one pass.
    """
    if connection.id in cache:
        return cache[connection.id]
    secrets = {k: crypto.decrypt(v) for k, v in (connection.secrets or {}).items()}
    adapter = get_adapter(connection.kind, connection.config or {}, secrets)
    cache[connection.id] = adapter
    return adapter


def _target_connection(db, run: Run, ticket: Ticket):
    """The connection this run's approved cases are created in.

    The project's TEST CASE TARGET when one is explicitly bound (ADR 0015 §3) —
    an ADO Test Plans project can legitimately be somewhere other than the board
    the tickets came from — otherwise the ticket's own work-item connection,
    which is what every case creation used before the role existed.

    Deliberately "explicit binding only": the migration backfills the target from
    the ticket source, so falling through here is the same connection anyway, and
    a project whose *config* binding drifted from a ticket's stamped
    ``connection_id`` keeps routing by the ticket — the more specific fact.
    """
    if run.project_guid:
        cfg = (
            db.query(ProjectConfig)
            .filter(
                ProjectConfig.project_guid == run.project_guid,
                ProjectConfig.test_case_connection_id.isnot(None),
            )
            .first()
        )
        if cfg is not None:
            conn = connection_service.get_connection(
                db, cfg.test_case_connection_id, owner_id=run.owner_id
            )
            if conn is not None and conn.id != ticket.connection_id:
                return conn
    return connection_service.resolve_work_item_for_ticket(db, ticket)


def _worker(run_id: int, link: bool, ticket_ids: list[str], dry_run: bool = False) -> None:
    db = db_module.SessionLocal()
    try:
        run = db.get(Run, run_id)
        if run is None:
            return
        try:
            if not set_run_status(db, run, "sync"):
                return  # already terminal (e.g. cancelled) — don't overwrite it

            query = db.query(TestCase).filter(
                TestCase.run_id == run_id, TestCase.approval == "approved"
            )
            if ticket_ids:
                query = query.filter(TestCase.ticket_external_id.in_(ticket_ids))
            cases = query.order_by(TestCase.ticket_external_id, TestCase.code).all()

            grouped: dict[str, list[TestCase]] = {}
            for c in cases:
                grouped.setdefault(c.ticket_external_id, []).append(c)

            adapters: dict[int, Any] = {}
            for tid, group in grouped.items():
                if run_control.is_cancelled(run_id, db):
                    logger.info("Run {} cancelled — stopping create+link pass", run.code)
                    return
                ticket = db.query(Ticket).filter(Ticket.external_id == tid).first()
                created_any = False
                linked_any = False
                error = ""
                try:
                    if dry_run:
                        adapter = None
                        kind = ticket.provider_kind if ticket else "ado"
                    elif ticket is None:
                        raise ProviderError(f"Ticket '{tid}' not found")
                    else:
                        connection = _target_connection(db, run, ticket)
                        adapter = _adapter_for(db, connection, adapters)
                        kind = connection.kind
                    for case in group:
                        if dry_run:
                            # Local mode: record the case locally, never touch the provider.
                            res = {
                                "external_id": f"LOCAL-{case.id}",
                                "status": "Local",
                                "url": "",
                                "linked": False,
                            }
                        else:
                            res = adapter.create_test_case(
                                tid,
                                title=case.title,
                                precondition=case.precondition,
                                steps=case.steps or [],
                                priority=case.priority,
                                link=link,
                            )
                        _upsert_link(db, run_id, case, kind, res)
                        created_any = True
                        linked_any = linked_any or bool(res.get("linked"))
                    db.commit()
                except Exception as exc:  # noqa: BLE001 - surface per-ticket, don't kill the pass
                    db.rollback()
                    error = str(exc)[:200]
                    logger.error("Create&link failed for {}: {}", tid, error)
                hub.publish(
                    str(run_id),
                    "sync.progress",
                    {
                        "ticket": tid,
                        "created": created_any,
                        "linked": linked_any,
                        "local": dry_run,
                        "error": error,
                    },
                )
            hub.publish(str(run_id), "sync.done", {})
        except Exception as exc:  # noqa: BLE001 - never crash the worker thread silently
            logger.error("Create+link pass crashed for run {}: {}", run_id, exc)
            db.rollback()
            run.failed_stage = run.status
            set_run_status(db, run, "failed")
    finally:
        _running.discard(run_id)
        db.close()


def _upsert_link(db, run_id: int, case: TestCase, kind: str, res: dict[str, Any]) -> None:
    row = (
        db.query(LinkedTestCase)
        .filter(LinkedTestCase.run_id == run_id, LinkedTestCase.test_case_id == case.id)
        .first()
    )
    if row is None:
        row = LinkedTestCase(run_id=run_id, test_case_id=case.id)
        db.add(row)
    row.ticket_external_id = case.ticket_external_id
    row.provider_kind = kind
    row.external_id = str(res.get("external_id", ""))
    row.title = case.title
    row.status = res.get("status", "Design")
    row.url = res.get("url", "")
    row.linked = bool(res.get("linked"))
