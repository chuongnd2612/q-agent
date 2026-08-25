"""Tests for the Audit Log router — audit_logs table events + in-memory logs."""

from __future__ import annotations


def _seed_run_and_execution(db_session):
    from app.models.execution import Execution
    from app.models.run import Run

    run = Run(code="RUN-501", name="Audit sample run", status="evidence", framework="Playwright", env="Staging", workers=4)
    db_session.add(run)
    db_session.flush()
    execution = Execution(run_id=run.id, status="done", total=3, passed=2, failed=1)
    db_session.add(execution)
    db_session.commit()
    return run, execution


def _seed_events(db_session):
    """Seed real rows then backfill the audit_logs table from them (on the same
    session the endpoint reads through)."""
    from app.services import audit_service

    _seed_run_and_execution(db_session)
    inserted = audit_service.backfill_from_history(db_session)
    assert inserted >= 2
    return inserted


def test_record_appends_row(client, db_session):
    from app.db import SessionLocal
    from app.models.audit import AuditLog
    from app.services import audit_service

    audit_service.record(
        category="settings", action="Changed execution settings",
        actor_type="user", target="Parallel workers 4 → 6", meta="Workspace default",
    )
    # record() commits on its own session; verify via a fresh one.
    fresh = SessionLocal()
    try:
        rows = fresh.query(AuditLog).filter(AuditLog.action == "Changed execution settings").all()
        assert len(rows) == 1
        assert rows[0].category == "settings" and rows[0].actor_type == "user"
    finally:
        fresh.close()


def test_audit_events_shape_and_sources(client, db_session):
    _seed_events(db_session)

    events = client.get("/audit/events").json()
    assert isinstance(events, list) and len(events) >= 2

    keys = {"id", "ts", "category", "actor", "actorType", "action", "target", "ip", "status", "meta"}
    assert keys.issubset(events[0].keys())

    actions = {e["action"] for e in events}
    assert "Created run" in actions  # from the Run
    assert "Executed test run" in actions  # from the Execution

    exec_event = next(e for e in events if e["action"] == "Executed test run")
    assert exec_event["status"] == "warning"  # the execution had a failure
    assert exec_event["actorType"] == "ai"


def test_audit_events_filters(client, db_session):
    _seed_events(db_session)

    only_runs = client.get("/audit/events?category=run").json()
    assert only_runs and all(e["category"] == "run" for e in only_runs)

    only_user = client.get("/audit/events?actor=user").json()
    assert only_user and all(e["actorType"] == "user" for e in only_user)

    searched = client.get("/audit/events?q=RUN-501").json()
    assert searched and all("RUN-501" in (e["target"] + e["action"] + e["actor"] + e["id"]) for e in searched)

    empty = client.get("/audit/events?q=zzz-no-such-event").json()
    assert empty == []


def test_clear_events_empties_table_and_blocks_reseed(client, db_session):
    from app.services import audit_service

    _seed_events(db_session)
    assert client.get("/audit/events").json(), "precondition: events exist"

    resp = client.delete("/audit/events").json()
    assert resp["deleted"] >= 2
    assert client.get("/audit/events").json() == []

    # A subsequent backfill (e.g. on restart) must NOT re-seed a cleared log.
    reseeded = audit_service.backfill_from_history(db_session)
    assert reseeded == 0
    assert client.get("/audit/events").json() == []


def test_audit_stats_shape(client, db_session):
    _seed_events(db_session)
    stats = client.get("/audit/stats").json()
    assert set(stats.keys()) == {"eventsToday", "aiActions", "userActions", "failures"}
    assert all(isinstance(v, int) for v in stats.values())
    assert stats["failures"] >= 1  # the failing execution


def test_audit_logs_capture_and_filter(client):
    from app.logging import logger, setup_logging
    from app.services.log_buffer import install_sink

    setup_logging()
    install_sink()  # idempotent

    logger.info("audit-test marker line took 42 ms")
    logger.error("audit-test boom happened")

    logs = client.get("/audit/logs").json()
    assert isinstance(logs, list) and logs
    line = logs[0]
    assert {"ts", "level", "service", "message", "durationMs", "trace"}.issubset(line.keys())

    markers = [line for line in logs if "audit-test" in line["message"]]
    assert markers, "emitted log lines should be in the buffer"
    assert any(line["level"] == "error" for line in markers)
    # The "42 ms" line should have parsed a duration.
    assert any(line["durationMs"] == 42 for line in markers if "42" in line["message"])

    errors_only = client.get("/audit/logs?level=error").json()
    assert errors_only and all(line["level"] == "error" for line in errors_only)

    searched = client.get("/audit/logs?q=audit-test").json()
    assert searched and all("audit-test" in line["message"].lower() for line in searched)


def test_audit_log_stats_shape(client):
    from app.logging import logger, setup_logging

    setup_logging()
    logger.warning("audit-test warning for stats")

    stats = client.get("/audit/logs/stats").json()
    assert {"logVolume", "warnings", "errors"} <= set(stats.keys())
    assert all(isinstance(v, int) for v in stats.values())
    assert stats["logVolume"] >= 1
    # `servicesHealthy` / `servicesTotal` were REMOVED in #674 — they were invented
    # numbers, not measurements. Asserted absent rather than simply dropped from the
    # set above, so reinstating a fabricated metric fails here instead of shipping
    # (#683). This is also why the check is a subset and not `==`: the exact-shape
    # assertion is what left the suite red for weeks on a correct change.
    assert "servicesHealthy" not in stats
    assert "servicesTotal" not in stats
    assert stats["warnings"] >= 1, "the warning logged above is what logVolume counts"


def test_record_stamps_and_filters_by_run_code(client, db_session):
    """Events carry runCode; /audit/events?run= scopes to one run (#394)."""
    from app.services import audit_service

    audit_service.record(category="run", action="A", target="t", run_code="RUN-901")
    audit_service.record(category="run", action="B", target="t", run_code="RUN-902")
    audit_service.record(category="run", action="C", target="t")  # unscoped

    scoped = client.get("/audit/events?run=RUN-901").json()
    assert scoped and all(e["runCode"] == "RUN-901" for e in scoped)
    assert {e["action"] for e in scoped} == {"A"}

    other = client.get("/audit/events?run=RUN-902").json()
    assert {e["action"] for e in other} == {"B"}

    # The wire shape carries runCode ("" when unscoped).
    allev = client.get("/audit/events").json()
    assert "runCode" in allev[0]
    assert any(e["action"] == "C" and e["runCode"] == "" for e in allev)


def test_record_auto_stamps_ambient_run(client, db_session):
    """A background worker's run scope auto-attributes events without passing it."""
    from app.models.run import Run
    from app.services import audit_service, run_context

    run = Run(code="RUN-777", name="ambient", status="processing", framework="Playwright", env="Staging", workers=4)
    db_session.add(run)
    db_session.commit()

    run_context.set_run(run.id)
    try:
        audit_service.record(category="ai", actor_type="ai", action="Ambient event", target="x")
    finally:
        run_context.clear()

    scoped = client.get("/audit/events?run=RUN-777").json()
    assert scoped and scoped[0]["action"] == "Ambient event"


def test_audit_exploration_result_status_mapping(client, db_session):
    """Exploration outcome → event status: KB write=success, dry run=warning, hard fail=error."""
    from app.services import exploration_agent

    target = {"ticket": "1153", "screen": "Employees"}
    exploration_agent.audit_exploration_result(
        target=target, stop_reason="done", steps_taken=5,
        discovered_routes=1, discovered_selectors=3, wrote_kb=True, run_code="RUN-800",
    )
    exploration_agent.audit_exploration_result(
        target=target, stop_reason="unreachable", steps_taken=2,
        discovered_routes=0, discovered_selectors=0, wrote_kb=False, run_code="RUN-800",
    )
    exploration_agent.audit_exploration_result(
        target=target, stop_reason="decide-error", steps_taken=0,
        discovered_routes=0, discovered_selectors=0, wrote_kb=False, run_code="RUN-800",
    )

    events = client.get("/audit/events?run=RUN-800").json()
    by_status = {e["status"] for e in events}
    assert by_status == {"success", "warning", "error"}
    assert all(e["action"] == "Explored to unblock" for e in events)
    err = next(e for e in events if e["status"] == "error")
    assert "decide-error" in err["meta"]


def test_audit_exploration_result_persists_step_trail_and_discovery(client, db_session):
    """The exploration event carries a detail payload: step trail + discovered (#396)."""
    from app.services import exploration_agent

    exploration_agent.audit_exploration_result(
        target={"ticket": "1153", "screen": "Employees"},
        stop_reason="done", steps_taken=2, discovered_routes=1, discovered_selectors=1,
        wrote_kb=True, run_code="RUN-DET",
        log=[
            {"step": 1, "action": "goto", "args": {"url": "/employees"}, "reasoning": "open list", "observedUrl": "/employees"},
            {"step": 2, "action": "click", "args": {"role": "tab", "name": "Divisions"}, "reasoning": "open tab", "ok": True},
        ],
        routes=[{"path": "/employees", "description": "discovered"}],
        selectors=[{"screen": "Employees", "element": "Divisions", "selector": 'role=tab[name="Divisions"]', "strategy": "role"}],
    )

    event = client.get("/audit/events?run=RUN-DET").json()[0]
    detail = event["detail"]
    assert detail is not None
    assert detail["wroteKb"] is True and detail["stopReason"] == "done"
    # Step trail — normalized, with a human-readable target per action.
    assert [s["action"] for s in detail["steps"]] == ["goto", "click"]
    assert detail["steps"][0]["target"] == "/employees"
    assert 'tab "Divisions"' in detail["steps"][1]["target"]
    # What it retrieved / wrote to the KB.
    assert detail["routes"][0]["path"] == "/employees"
    assert detail["selectors"][0]["strategy"] == "role"
    assert detail["selectors"][0]["screen"] == "Employees"


def test_log_buffer_drops_polling_noise_but_keeps_errors(client):
    """High-frequency poll access lines are dropped at INFO but kept at ERROR (#394)."""
    from app.logging import logger, setup_logging
    from app.services.log_buffer import install_sink

    setup_logging()
    install_sink()

    logger.info('127.0.0.1 - "POST /agent/explore/next HTTP/1.1" 204 noise-info-marker')
    logger.error('127.0.0.1 - "POST /agent/explore/next HTTP/1.1" 500 noise-error-marker')

    logs = client.get("/audit/logs").json()
    messages = [line["message"] for line in logs]
    assert not any("noise-info-marker" in m for m in messages), "info poll line should be dropped"
    assert any("noise-error-marker" in m for m in messages), "error poll line must be kept"


def test_stdlib_logging_bridged_into_buffer(client):
    """Standard-library logging (e.g. uvicorn's access log) is mirrored into the
    Backend Logs buffer, not just our loguru records."""
    import logging

    from app.logging import setup_logging
    from app.services.log_buffer import install_stdlib_bridge

    setup_logging()
    install_stdlib_bridge()  # idempotent

    marker = 'stdlib-bridge 127.0.0.1 - "GET /audit-bridge-test HTTP/1.1" 200'
    logging.getLogger("uvicorn.access").info(marker)

    logs = client.get("/audit/logs?q=stdlib-bridge").json()
    assert logs, "stdlib log records should reach the buffer"
    assert any("audit-bridge-test" in line["message"] for line in logs)
