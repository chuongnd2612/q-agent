"""Tests for comment preparation, editing, and publish/retry orchestration."""

from __future__ import annotations

import pytest


class FakeAdapter:
    """Records publish_comment/update_status calls instead of hitting a real API."""

    calls: list[dict] = []
    fail_tickets: set[str] = set()
    #: Whether this stand-in claims the attachment capability (#696). A real adapter
    #: inherits `supports_attachments` from `ProviderAdapter`; this one must declare it
    #: explicitly, and both answers need exercising — the evidence path branches on it.
    attachments_supported: bool = True

    def __init__(self, config, secrets):  # noqa: ANN001
        self.config = config
        self.secrets = secrets

    def supports_attachments(self):
        return FakeAdapter.attachments_supported

    def publish_comment(self, ticket_external_id, body, *, attachments=None):  # noqa: ANN001
        if ticket_external_id in FakeAdapter.fail_tickets:
            raise RuntimeError(f"upstream rejected comment for {ticket_external_id}")
        FakeAdapter.calls.append(
            {"ticket": ticket_external_id, "body": body, "attachments": attachments}
        )
        return f"ext-comment-{ticket_external_id}"

    def update_status(self, ticket_external_id, target_status):  # noqa: ANN001
        FakeAdapter.calls.append({"ticket": ticket_external_id, "status": target_status})


@pytest.fixture(autouse=True)
def _reset_fake_adapter():
    FakeAdapter.calls = []
    FakeAdapter.fail_tickets = set()
    FakeAdapter.attachments_supported = True
    yield


def _seed_report(db_session, run_id: int = 1, second_ticket_fails: bool = False):
    from app.models.provider_connection import ProviderConnection
    from app.models.report import Report
    from app.models.run import Run
    from app.models.ticket import Ticket
    from app import crypto

    conn = ProviderConnection(
        kind="ado",
        name="Azure DevOps",
        connected=True,
        config={"org_url": "https://dev.azure.com/acme"},
        secrets={"pat": crypto.encrypt("super-secret-pat")},
    )
    db_session.add(conn)
    db_session.flush()
    db_session.add(Run(id=run_id, code="RUN-1", name="Run 1", status="comment"))
    db_session.add(
        Ticket(external_id="SUR-1", provider_kind="ado", title="Login works", connection_id=conn.id)
    )
    db_session.add(
        Ticket(external_id="SUR-2", provider_kind="ado", title="Logout works", connection_id=conn.id)
    )

    ticket_summary = [
        {"ticketExternalId": "SUR-1", "passed": 2, "failed": 0, "total": 2},
        {
            "ticketExternalId": "SUR-2",
            "passed": 1,
            "failed": 1 if second_ticket_fails else 0,
            "total": 2,
        },
    ]
    db_session.add(
        Report(
            run_id=run_id,
            execution_id=1,
            overall_result="failed" if second_ticket_fails else "passed",
            pass_rate=75.0 if second_ticket_fails else 100.0,
            passed=3,
            failed=1 if second_ticket_fails else 0,
            duration_s=10,
            env="Staging",
            data={"ticketSummary": ticket_summary, "aiFailureAnalysis": "flaky click handler"},
        )
    )
    db_session.commit()


def _patch_adapter_and_claude(monkeypatch):
    from app.services import publish_service
    from app.routers import comments as comments_router

    monkeypatch.setattr(publish_service, "get_adapter", lambda kind, config, secrets: FakeAdapter(config, secrets))
    monkeypatch.setattr(
        comments_router.claude_cli, "run_prompt", lambda prompt, **k: f"QA summary: {prompt[:20]}..."
    )


def test_prepare_comments_creates_drafts_with_status_mapping(client, db_session, monkeypatch):
    _patch_adapter_and_claude(monkeypatch)
    _seed_report(db_session, second_ticket_fails=True)

    resp = client.post("/runs/1/comments/prepare")
    assert resp.status_code == 200
    comments = resp.json()
    assert len(comments) == 2

    by_ticket = {c["ticketExternalId"]: c for c in comments}
    assert by_ticket["SUR-1"]["targetStatus"] == "Passed"
    assert by_ticket["SUR-1"]["status"] == "draft"
    assert by_ticket["SUR-2"]["targetStatus"] == "QA Failed"
    assert all(c["body"] for c in comments)


def test_prepare_comment_consolidates_all_cases(client, db_session, monkeypatch):
    """The per-ticket comment aggregates every executed case + its diagnosis."""
    from app.models.report import Report
    from app.models.run import Run
    from app.models.ticket import Ticket
    from app.routers import comments as comments_router

    db_session.add(Run(id=3, code="RUN-3", name="Run 3", status="comment"))
    db_session.add(Ticket(external_id="SUR-7", provider_kind="ado", title="Broker list"))
    db_session.add(
        Report(
            run_id=3, execution_id=1, overall_result="failed", pass_rate=50.0,
            passed=1, failed=1, duration_s=8, env="Staging",
            data={
                "ticketSummary": [
                    {
                        "ticketExternalId": "SUR-7", "passed": 1, "failed": 1, "total": 2,
                        "cases": [
                            {"caseCode": "TC-01", "title": "loads", "status": "pass", "error": "", "diagnosis": ""},
                            {"caseCode": "TC-02", "title": "activate menu", "status": "fail",
                             "error": "timeout", "diagnosis": "Activate option is missing from the menu"},
                        ],
                    }
                ],
                "aiFailureAnalysis": "menu rendering issue",
            },
        )
    )
    db_session.commit()

    captured = {}
    monkeypatch.setattr(
        comments_router.claude_cli, "run_prompt",
        lambda prompt, **k: captured.setdefault("prompt", prompt) or "Consolidated QA summary",
    )
    from app.services import publish_service
    monkeypatch.setattr(publish_service, "get_adapter", lambda kind, config, secrets: FakeAdapter(config, secrets))

    resp = client.post("/runs/3/comments/prepare")
    assert resp.status_code == 200
    p = captured["prompt"]
    # The prompt aggregates every case and folds in the failure's diagnosis.
    assert "TC-01" in p and "TC-02" in p
    assert "Activate option is missing" in p
    assert "1/2 cases passed" in p and "consolidated" in p.lower()


def test_prepare_auto_builds_report_when_missing(client, db_session, monkeypatch):
    """Coming from Evidence without a report shouldn't 404 — prepare builds one."""
    _patch_adapter_and_claude(monkeypatch)
    from app.models.execution import Execution, ExecutionResult
    from app.models.run import Run
    from app.models.ticket import Ticket

    db_session.add(Run(id=2, code="RUN-2", name="Run 2", status="evidence"))
    db_session.add(Ticket(external_id="SUR-3", provider_kind="ado", title="Search works"))
    execution = Execution(run_id=2, status="done", env="Staging")
    db_session.add(execution)
    db_session.flush()
    db_session.add(
        ExecutionResult(
            execution_id=execution.id,
            test_case_id=1,
            ticket_external_id="SUR-3",
            case_code="TC-01",
            title="Search works",
            status="pass",
            duration_ms=1200,
        )
    )
    db_session.commit()

    # No Report seeded — prepare should build one on demand, not 404.
    resp = client.post("/runs/2/comments/prepare")
    assert resp.status_code == 200
    comments = resp.json()
    assert len(comments) == 1
    assert comments[0]["ticketExternalId"] == "SUR-3"
    assert comments[0]["targetStatus"] == "Passed"

    # A report now exists for the run.
    assert client.get("/runs/2/report").status_code == 200


def test_list_comments(client, db_session, monkeypatch):
    _patch_adapter_and_claude(monkeypatch)
    _seed_report(db_session)
    client.post("/runs/1/comments/prepare")

    resp = client.get("/runs/1/comments")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_patch_comment_edits_body_and_target_status(client, db_session, monkeypatch):
    _patch_adapter_and_claude(monkeypatch)
    _seed_report(db_session)
    prepared = client.post("/runs/1/comments/prepare").json()
    comment_id = prepared[0]["id"]

    resp = client.patch(f"/comments/{comment_id}", json={"body": "Edited body", "targetStatus": "Testing"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["body"] == "Edited body"
    assert body["targetStatus"] == "Testing"


def test_publish_single_comment_success(client, db_session, monkeypatch):
    _patch_adapter_and_claude(monkeypatch)
    _seed_report(db_session)
    prepared = client.post("/runs/1/comments/prepare").json()
    comment_id = prepared[0]["id"]

    resp = client.post(f"/comments/{comment_id}/publish")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "published"
    assert body["externalCommentId"] == "ext-comment-SUR-1"
    assert body["errorMessage"] == ""

    assert any(c.get("ticket") == "SUR-1" and "body" in c for c in FakeAdapter.calls)
    assert any(c.get("ticket") == "SUR-1" and c.get("status") == "Passed" for c in FakeAdapter.calls)


def test_publish_all_and_selected(client, db_session, monkeypatch):
    _patch_adapter_and_claude(monkeypatch)
    _seed_report(db_session)
    client.post("/runs/1/comments/prepare")

    resp = client.post("/runs/1/comments/publish", json={"ticketIds": []})
    assert resp.status_code == 200
    statuses = {c["ticketExternalId"]: c["status"] for c in resp.json()}
    assert statuses == {"SUR-1": "published", "SUR-2": "published"}


def test_publish_failure_sets_failed_status_and_retry_recovers(client, db_session, monkeypatch):
    _patch_adapter_and_claude(monkeypatch)
    _seed_report(db_session, second_ticket_fails=True)
    client.post("/runs/1/comments/prepare")

    FakeAdapter.fail_tickets = {"SUR-2"}
    resp = client.post("/runs/1/comments/publish", json={"ticketIds": []})
    assert resp.status_code == 200
    statuses = {c["ticketExternalId"]: c["status"] for c in resp.json()}
    assert statuses["SUR-1"] == "published"
    assert statuses["SUR-2"] == "failed"

    failed_comment = next(c for c in resp.json() if c["ticketExternalId"] == "SUR-2")
    assert "upstream rejected" in failed_comment["errorMessage"]

    # Retry: unblock the ticket, then retry should recover it.
    FakeAdapter.fail_tickets = set()
    retry_resp = client.post("/runs/1/comments/retry")
    assert retry_resp.status_code == 200
    retried = retry_resp.json()
    assert len(retried) == 1
    assert retried[0]["ticketExternalId"] == "SUR-2"
    assert retried[0]["status"] == "published"


def test_publish_missing_provider_marks_failed(client, db_session, monkeypatch):
    from app.routers import comments as comments_router

    monkeypatch.setattr(
        comments_router.claude_cli, "run_prompt", lambda prompt, **k: f"QA summary: {prompt[:20]}..."
    )
    # No FakeAdapter patch, no Provider row seeded -> publish_service should fail cleanly.
    from app.models.report import Report
    from app.models.run import Run
    from app.models.ticket import Ticket

    db_session.add(Run(id=5, code="RUN-5", name="Run 5", status="comment"))
    db_session.add(Ticket(external_id="SUR-9", provider_kind="ado", title="No provider configured"))
    db_session.add(
        Report(
            run_id=5,
            execution_id=1,
            overall_result="passed",
            pass_rate=100.0,
            passed=1,
            failed=0,
            duration_s=1,
            env="Staging",
            data={
                "ticketSummary": [{"ticketExternalId": "SUR-9", "passed": 1, "failed": 0, "total": 1}],
                "aiFailureAnalysis": "",
            },
        )
    )
    db_session.commit()

    prepared = client.post("/runs/5/comments/prepare").json()
    resp = client.post(f"/comments/{prepared[0]['id']}/publish")
    assert resp.status_code == 200
    assert resp.json()["status"] == "failed"
    assert "not configured" in resp.json()["errorMessage"]


# ---------------------------------------------------------------------------
# Evidence in the comment, and only uploaded on publish (#696)
# ---------------------------------------------------------------------------
#
# QA needs a published comment to SHOW the evidence for every case it reports,
# passes included — a pass asserted in prose is not a pass demonstrated. Before
# this, no evidence reached a work item by any route: the UI drew two decorative
# `evidence.zip` / `trace.zip` chips with no files behind them,
# `TicketComment.attachments` was never populated, and every adapter's
# `publish_comment` accepted an `attachments` argument and silently dropped it.
#
# The two phases are the design and are asserted separately: preparing lists the
# evidence inline and uploads NOTHING (a draft is regenerated, edited, thrown away
# — pushing files into a work item each time would litter the ticket), and
# publishing is what uploads.


def _seed_evidence(db_session, run_id: int = 1):
    """One passing and one failing case on SUR-1, each with real files on disk."""
    from pathlib import Path

    from app.models.execution import Evidence, Execution, ExecutionResult
    from app.models.testcase import TestCase
    from app.services.workspace_scope import scoped_evidence_dir

    execution = Execution(id=1, run_id=run_id, status="done", env="Staging")
    db_session.add(execution)
    db_session.flush()

    root = scoped_evidence_dir(None)
    seeded = []
    for case_code, status, kinds in (
        ("TC-01", "pass", ("screenshot",)),
        ("TC-02", "fail", ("screenshot", "video", "console")),
    ):
        case = TestCase(
            run_id=run_id,
            ticket_external_id="SUR-1",
            code=case_code,
            title=f"{case_code} title",
        )
        db_session.add(case)
        db_session.flush()
        result = ExecutionResult(
            execution_id=execution.id,
            test_case_id=case.id,
            ticket_external_id="SUR-1",
            case_code=case_code,
            title=f"{case_code} title",
            status=status,
            duration_ms=1000,
            error_message="" if status == "pass" else "expect(locator) failed",
        )
        db_session.add(result)
        db_session.flush()
        for kind in kinds:
            rel = f"RUN-1/SUR-1/{case_code}/{kind}.bin"
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * 2048)
            db_session.add(
                Evidence(
                    result_id=result.id,
                    kind=kind,
                    path=rel,
                    filename=f"{kind}.bin",
                    size_bytes=2048,
                )
            )
            seeded.append(Path(path))
    db_session.commit()
    return seeded


def test_the_draft_lists_evidence_for_every_case_including_passes(
    client, db_session, monkeypatch
):
    """"Including passes" is the requirement, and the easy thing to get wrong.

    A failure-only manifest is the natural implementation and the wrong one: the
    passes are the bulk of what QA is asked to evidence.
    """
    _patch_adapter_and_claude(monkeypatch)
    _seed_report(db_session)
    _seed_evidence(db_session)

    resp = client.post("/runs/1/comments/prepare")

    assert resp.status_code == 200
    body = next(c for c in resp.json() if c["ticketExternalId"] == "SUR-1")["body"]
    assert "Evidence per test case" in body
    assert "TC-01 — PASS" in body, "the passing case has no evidence line"
    assert "TC-02 — FAIL" in body
    # Named artifacts, not a vague "evidence attached".
    assert "Screenshot: screenshot.bin" in body
    assert "Video: video.bin" in body


def test_preparing_a_draft_uploads_nothing(client, db_session, monkeypatch):
    """Phase one. A draft is regenerated, edited and thrown away — uploading on each
    of those would litter the work item with attachments nobody asked for."""
    _patch_adapter_and_claude(monkeypatch)
    _seed_report(db_session)
    _seed_evidence(db_session)

    client.post("/runs/1/comments/prepare")
    client.post("/runs/1/comments/prepare")  # regenerate, as a reviewer would

    assert FakeAdapter.calls == [], "a draft reached the provider"


def test_the_draft_records_what_it_will_attach(client, db_session, monkeypatch):
    """The chips the UI shows are a PLAN, and it has to be a real one.

    Console/network logs are excluded deliberately: they are JSON blobs the DB
    already holds, and attaching them to a work item is noise, not evidence.
    """
    _patch_adapter_and_claude(monkeypatch)
    _seed_report(db_session)
    _seed_evidence(db_session)

    resp = client.post("/runs/1/comments/prepare")

    attachments = next(c for c in resp.json() if c["ticketExternalId"] == "SUR-1")["attachments"]
    kinds = {a["kind"] for a in attachments}
    assert kinds == {"screenshot", "video"}, kinds
    # The case code is in the uploaded filename: a work item with a dozen attachments
    # all called `screenshot.bin` is unreadable.
    assert all(a["filename"].startswith(a["caseCode"]) for a in attachments)
    assert {a["caseCode"] for a in attachments} == {"TC-01", "TC-02"}


def test_publishing_uploads_the_evidence_as_file_paths(client, db_session, monkeypatch):
    """Phase two, and the bug this closes.

    `comment.attachments` was handed straight to the adapter — a list of dicts no
    adapter could have done anything with, even if one had tried.
    """
    _patch_adapter_and_claude(monkeypatch)
    _seed_report(db_session)
    _seed_evidence(db_session)
    comment_id = next(
        c for c in client.post("/runs/1/comments/prepare").json()
        if c["ticketExternalId"] == "SUR-1"
    )["id"]

    resp = client.post(f"/comments/{comment_id}/publish")

    assert resp.status_code == 200
    assert resp.json()["status"] == "published"
    published = next(c for c in FakeAdapter.calls if c.get("ticket") == "SUR-1" and "body" in c)
    paths = published["attachments"]
    assert paths, "nothing was uploaded"
    assert all(isinstance(p, str) for p in paths), paths
    from pathlib import Path

    assert all(Path(p).is_file() for p in paths), "uploaded a path that is not a file"


def test_a_provider_without_attachments_says_so_instead_of_implying_it_attached(
    client, db_session, monkeypatch
):
    """Honest degradation. The fake `evidence.zip` chip's real sin was implying a file
    existed; a comment that lists evidence and stays silent about not attaching it
    repeats exactly that."""
    _patch_adapter_and_claude(monkeypatch)
    FakeAdapter.attachments_supported = False
    _seed_report(db_session)
    _seed_evidence(db_session)
    comment_id = next(
        c for c in client.post("/runs/1/comments/prepare").json()
        if c["ticketExternalId"] == "SUR-1"
    )["id"]

    client.post(f"/comments/{comment_id}/publish")

    published = next(c for c in FakeAdapter.calls if c.get("ticket") == "SUR-1" and "body" in c)
    assert published["attachments"] is None, "uploaded to a provider that cannot attach"
    assert "does not support comment attachments" in published["body"]


def test_evidence_deleted_between_draft_and_publish_is_named_not_skipped(
    client, db_session, monkeypatch
):
    """The cost of deferring the upload is that a file can vanish in between.

    Quietly attaching four of five screenshots is the failure a reviewer is least
    likely to notice, so the missing one is named in the comment.
    """
    _patch_adapter_and_claude(monkeypatch)
    _seed_report(db_session)
    seeded = _seed_evidence(db_session)
    comment_id = next(
        c for c in client.post("/runs/1/comments/prepare").json()
        if c["ticketExternalId"] == "SUR-1"
    )["id"]
    for path in seeded:
        path.unlink()

    resp = client.post(f"/comments/{comment_id}/publish")

    assert resp.status_code == 200
    published = next(c for c in FakeAdapter.calls if c.get("ticket") == "SUR-1" and "body" in c)
    assert not published["attachments"], "uploaded a file that is gone"
    assert "no longer on file" in published["body"]


def test_a_case_with_no_artifacts_is_reported_not_omitted(client, db_session, monkeypatch):
    """A silent gap in the list looks like a bug; "no artifacts captured" is itself
    information about the run."""
    from app.models.execution import Execution, ExecutionResult

    _patch_adapter_and_claude(monkeypatch)
    _seed_report(db_session)
    execution = Execution(id=1, run_id=1, status="done", env="Staging")
    db_session.add(execution)
    db_session.flush()
    from app.models.testcase import TestCase

    case = TestCase(run_id=1, ticket_external_id="SUR-1", code="TC-09", title="no artifacts")
    db_session.add(case)
    db_session.flush()
    db_session.add(
        ExecutionResult(
            execution_id=execution.id,
            test_case_id=case.id,
            ticket_external_id="SUR-1",
            case_code="TC-09",
            title="no artifacts",
            status="pass",
            duration_ms=10,
        )
    )
    db_session.commit()

    resp = client.post("/runs/1/comments/prepare")

    body = next(c for c in resp.json() if c["ticketExternalId"] == "SUR-1")["body"]
    assert "TC-09 — PASS" in body
    assert "no artifacts captured" in body
