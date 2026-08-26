"""The report + ticket-comment Claude calls must run under the run's ambient
context so they resolve the run OWNER's credential (own→shared), not the
ambient/shared one — a request thread has no ambient run, so without this they
fall back to a possibly-expired shared credential ("Not logged in" / 401).
Regression for the run-10 comment/analysis failures.
"""

from __future__ import annotations

import app.routers.comments as comments
from app.services import report_service, run_context


def test_summarize_ticket_runs_under_the_run_context(monkeypatch):
    captured: dict[str, int | None] = {}
    monkeypatch.setattr(
        comments.claude_cli, "run_prompt",
        lambda *a, **k: captured.__setitem__("run", run_context.get_run()) or "ok",
    )
    observations, summary = comments._summarize_ticket(
        "SUR-1", {"passed": 1, "failed": 0, "total": 1, "cases": []}, "", run_id=10
    )
    # `_summarize_ticket` now returns the two parts that need judgement, not a whole
    # comment (#703) — the structure is assembled from facts about the run. A reply
    # that is not the agreed JSON degrades to the summary rather than being discarded.
    assert (observations, summary) == ({}, "ok")
    assert captured["run"] == 10  # Claude call attributed to the run → owner cred
    assert run_context.get_run() is None  # restored afterwards


def test_build_report_runs_analysis_under_the_run_context(db_session, monkeypatch):
    from app.models.run import Run

    run = Run(code="RUN-X", name="x", status="evidence", workers=1)
    db_session.add(run)
    db_session.commit()

    captured: dict[str, int | None] = {}
    monkeypatch.setattr(
        report_service, "_ai_failure_analysis",
        lambda failed: captured.__setitem__("run", run_context.get_run()) or "analysis",
    )
    report_service.build_report(db_session, run.id)
    assert captured["run"] == run.id
    assert run_context.get_run() is None
