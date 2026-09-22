"""Tests for the live-planner device dispatch (#900).

Two halves, and the second is the one that matters:

1. The ``/agent/planning/*`` endpoints (all ``require_agent`` — a paired device
   token): claim, the progress relay + its ``alive`` flag, and a finalize that
   stores the sidecar RAW and normalises it server-side.

2. ``planner_agent_service.plan_ticket``'s target branch. Per CLAUDE.md the
   assertion that counts is **which branch ran**, not the return value: every
   failure path here returns ``None``, and ``None`` is also what a legitimately
   unusable plan returns, so a test that only checked the result would pass just
   as happily if the dispatch never happened. So each test below pins the branch
   with a negative control — ``agentic_browser.browser_session`` is replaced by
   something that RAISES, so any test claiming "the server did not plan locally"
   fails loudly if it did.

Real engines only (ADR 0001): no browser, no Claude. The device is a thread that
claims the queued session and posts a result, which exercises the real
``claim_next``/``await_result``/``set_result`` path rather than stubbing it.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.models.agent_planning import AgentPlanningSession
from app.services import (
    agent_device_service,
    agent_planning_service,
    auth_service,
    planner_agent_service,
    settings_store,
)
from app.ws import hub

_RUN = SimpleNamespace(id=None, code="RUN-1")
_TICKET = SimpleNamespace(external_id="SUR-1428", title="Add password reset flow", owner_id=None)
_CONTEXT = {"projectKey": "surency", "repo": "web", "baseUrl": "https://app.test"}
_TARGET = {"ticket": "SUR-1428", "screen": "Add password reset flow", "goal": "Reach the reset screen"}

_PLAN_PAYLOAD = {
    "overview": "Docs site",
    "auth": "storage state",
    "scenarios": [
        {
            "title": "Reset a password",
            "steps": [
                {
                    "action": "Click Forgot password",
                    "locator": "getByRole('link', { name: 'Forgot password' })",
                    "expect": "The reset form appears",
                    "expectLocator": "getByRole('heading', { name: 'Reset' })",
                }
            ],
        }
    ],
    "routes": [{"path": "/reset", "description": "reset form"}],
    "selectors": [{"screen": "Login", "element": "email", "selector": "getByLabel('Email')"}],
}


def _make_user(db_session, email: str, password: str = "password123"):
    from app.models.user import User

    user = User(
        email=email,
        first_name="Agent",
        last_name="Owner",
        password_hash=auth_service.hash_password(password),
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _pair_device(db_session, user, name: str = "Test Device") -> str:
    """Pair a device for ``user`` and return its bearer token."""
    code = agent_device_service.create_pairing_code(db_session, user)
    _device, token = agent_device_service.redeem_pairing_code(db_session, code, name)
    return token


def _enqueue(owner_id, *, session_id="sess-1", run_id=None) -> None:
    agent_planning_service.request_planning(
        session_id,
        owner_id=owner_id,
        run_id=run_id,
        project_key="surency",
        repo="web",
        base_url="https://app.test",
        origin="https://app.test",
        run_code="RUN-1",
        ticket="SUR-1428",
        sidecar_filename="plan.json",
        system_prompt="# planner methodology",
        task_prompt="Plan the scenarios",
        model="sonnet",
        max_budget_usd=2.5,
        log_verbosity="concise",
    )


# ------------------------------------------------------------------ claim
def test_planning_next_204_when_empty(client, db_session):
    user = _make_user(db_session, "plan-empty@example.com")
    token = _pair_device(db_session, user)

    resp = client.post("/agent/planning/next", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 204


def test_planning_next_returns_the_claim_payload_once(client, db_session):
    user = _make_user(db_session, "plan-claim@example.com")
    token = _pair_device(db_session, user)
    _enqueue(user.id)

    resp = client.post("/agent/planning/next", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The methodology travels as a system prompt STRING — the device has no
    # `--agent` support (#894/#901), which is the whole reason for this field.
    assert body["systemPrompt"] == "# planner methodology"
    assert body["taskPrompt"] == "Plan the scenarios"
    assert body["sessionId"] == "sess-1"
    assert body["baseUrl"] == "https://app.test"
    assert body["ticket"] == "SUR-1428"
    assert body["sidecarFilename"] == "plan.json"
    assert body["maxBudgetUsd"] == 2.5
    # No browserDriver on the wire: the planner agent is playwright-cli-native
    # and the server hardcodes it, so a per-session choice could only drift.
    assert "browserDriver" not in body

    # Claimed once, and only once.
    again = client.post("/agent/planning/next", headers={"Authorization": f"Bearer {token}"})
    assert again.status_code == 204
    assert agent_planning_service.get_session("sess-1")["status"] == "running"


def test_planning_next_is_owner_scoped(client, db_session):
    owner = _make_user(db_session, "plan-owner@example.com")
    other = _make_user(db_session, "plan-other@example.com")
    other_token = _pair_device(db_session, other, name="Other Device")
    _enqueue(owner.id)

    resp = client.post("/agent/planning/next", headers={"Authorization": f"Bearer {other_token}"})
    assert resp.status_code == 204
    # The negative control for owner scoping: the row is still claimable by its
    # own owner, so the 204 above is scoping and not an empty queue.
    assert agent_planning_service.claim_next(owner.id) is not None


# ------------------------------------------------------------------ events
def test_events_relay_to_the_run_ws_and_report_alive(client, db_session, monkeypatch):
    user = _make_user(db_session, "plan-events@example.com")
    token = _pair_device(db_session, user)
    _enqueue(user.id, run_id=None)
    agent_planning_service.claim_next(user.id)

    published: list[tuple] = []
    monkeypatch.setattr(hub, "publish", lambda ch, ev, pl: published.append((ch, ev, pl)))

    resp = client.post(
        "/agent/planning/sess-1/events",
        json={"event": "planning.progress", "payload": {"phase": "step"}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    # Field asserts, not `== {whole body}`: that shape rots on a correct
    # additive change (#579).
    assert resp.json()["ok"] is True
    assert resp.json()["alive"] is True
    # No run id on this session, so nothing is relayed — but it is still `ok`.
    assert published == []


def test_events_say_not_alive_once_the_server_gave_up(client, db_session):
    """The device's abort signal: a session the server stopped waiting for."""
    user = _make_user(db_session, "plan-dead@example.com")
    token = _pair_device(db_session, user)
    _enqueue(user.id)
    agent_planning_service.claim_next(user.id)
    agent_planning_service._expire("sess-1", "deadline")

    resp = client.post(
        "/agent/planning/sess-1/events",
        json={"event": "planning.progress", "payload": {}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["alive"] is False


def test_events_unknown_session_404(client, db_session):
    user = _make_user(db_session, "plan-404@example.com")
    token = _pair_device(db_session, user)

    resp = client.post(
        "/agent/planning/nope/events",
        json={"event": "planning.progress", "payload": {}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------- finalize
def test_finalize_stores_the_raw_sidecar_and_normalises_it(client, db_session):
    user = _make_user(db_session, "plan-finalize@example.com")
    token = _pair_device(db_session, user)
    _enqueue(user.id)
    agent_planning_service.claim_next(user.id)

    raw = json.dumps(_PLAN_PAYLOAD)
    resp = client.post(
        "/agent/planning/sess-1/finalize",
        json={"planJson": raw, "summary": "Planned live", "ok": True, "costUsd": 0.27},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert resp.json()["scenarios"] == 1

    stored = agent_planning_service.get_session("sess-1")
    assert stored["status"] == "done"
    # RAW, byte-for-byte: normalisation is the server's contract, so the wire
    # carries text and the parsing lives in one place.
    assert stored["plan_json"] == raw
    assert stored["cost_usd"] == 0.27


def test_finalize_rejects_an_unusable_plan_even_when_the_device_says_ok(client, db_session):
    """NEGATIVE CONTROL: `ok` is the server's verdict on the sidecar, not the device's.

    A device could post `ok: true` with junk (or with a plan carrying no steps).
    That must land as `failed`, because the waiting generation pass would
    otherwise be told a plan exists and then find nothing in it.
    """
    user = _make_user(db_session, "plan-junk@example.com")
    token = _pair_device(db_session, user)
    _enqueue(user.id)
    agent_planning_service.claim_next(user.id)

    resp = client.post(
        "/agent/planning/sess-1/finalize",
        json={"planJson": "{ not json,,, ", "summary": "wrote something", "ok": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert resp.json()["scenarios"] == 0
    assert agent_planning_service.get_session("sess-1")["status"] == "failed"


def test_finalize_unknown_session_404(client, db_session):
    user = _make_user(db_session, "plan-f404@example.com")
    token = _pair_device(db_session, user)

    resp = client.post(
        "/agent/planning/nope/finalize",
        json={"planJson": "{}", "ok": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


# ------------------------------------------------------------------- queue
def test_a_new_attempt_supersedes_a_stale_session_for_the_same_ticket(db_session):
    _enqueue(None, session_id="old")
    _enqueue(None, session_id="new")

    assert agent_planning_service.get_session("old")["status"] == "expired"
    assert agent_planning_service.get_session("new")["status"] == "queued"
    # And the device can only ever get the live one.
    claim = agent_planning_service.claim_next(None)
    assert claim["session_id"] == "new"
    assert agent_planning_service.claim_next(None) is None


def test_await_result_expires_a_session_nobody_claims(db_session):
    _enqueue(None, session_id="unclaimed")

    result = agent_planning_service.await_result(
        "unclaimed", claim_deadline=timedelta(seconds=0.05), poll_interval=0.01
    )
    assert result is None
    # Expired, not left queued: a device that starts polling later must not open
    # a browser for a plan the server already gave up on.
    assert agent_planning_service.get_session("unclaimed")["status"] == "expired"


def test_purge_run_expires_a_stopped_runs_sessions(db_session, client):
    from app.models.run import Run

    run = Run(code="RUN-PURGE", name="Purge me", env="Staging", status="running")
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    _enqueue(None, session_id="to-purge", run_id=run.id)

    assert agent_planning_service.purge_run(run.id) == 1
    assert agent_planning_service.get_session("to-purge")["status"] == "expired"


# -------------------------------------------------- plan_ticket dispatch
@pytest.fixture
def no_local_browser(monkeypatch):
    """Make any server-side browser launch EXPLODE.

    The negative control for every dispatch test: "the server did not plan
    locally" is only meaningful if planning locally would have been noticed.
    """
    launched: list[int] = []

    def _boom(*args, **kwargs):
        launched.append(1)
        raise AssertionError("the server must not launch a browser on the local-agent path")

    monkeypatch.setattr(planner_agent_service.agentic_browser, "browser_session", _boom)
    return launched


def _fake_device(owner_id, *, plan_json: str, ok: bool = True, summary: str = "done"):
    """A thread standing in for the paired agent: claim, then post a result.

    Drives the REAL claim/finalize service calls, so the wait under test is the
    real one rather than a stub that always returns.
    """

    def _run():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            claim = agent_planning_service.claim_next(owner_id)
            if claim is not None:
                agent_planning_service.set_result(
                    claim["session_id"], plan_json=plan_json, summary=summary, ok=ok, cost_usd=0.27
                )
                return
            time.sleep(0.01)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread


def test_plan_ticket_dispatches_to_the_device_and_never_plans_locally(
    db_session, monkeypatch, local_agent_target, no_local_browser
):
    user = _make_user(db_session, "plan-dispatch@example.com")
    _pair_device(db_session, user)
    monkeypatch.setattr(agent_planning_service, "POLL_INTERVAL", 0.01)
    ticket = SimpleNamespace(**{**_TICKET.__dict__, "owner_id": user.id})
    device = _fake_device(user.id, plan_json=json.dumps(_PLAN_PAYLOAD))

    plan = planner_agent_service.plan_ticket(
        _RUN, ticket, _CONTEXT, owner_id=user.id, target=_TARGET
    )
    device.join(timeout=5)

    # The plan came back through the wire, normalised on arrival.
    assert plan is not None
    assert plan["scenarios"][0]["title"] == "Reset a password"
    assert (
        plan["scenarios"][0]["steps"][0]["locator"]
        == "getByRole('link', { name: 'Forgot password' })"
    )
    # BRANCH: a session row exists, carrying the payload the device planned from.
    assert no_local_browser == []
    row = (
        db_session.query(AgentPlanningSession)
        .filter(AgentPlanningSession.ticket == "SUR-1428")
        .first()
    )
    assert row is not None
    assert row.status == "done"
    assert row.run_code == "RUN-1"
    assert row.base_url == "https://app.test"
    # The methodology was shipped as text, and the task prompt is the planner's.
    assert "$PLAYWRIGHT_CLI_JS" in row.system_prompt
    assert planner_agent_service.SIDECAR_NAME in row.task_prompt


def test_plan_ticket_falls_back_to_text_without_a_paired_device(
    db_session, local_agent_target, no_local_browser
):
    """NEGATIVE CONTROL: no device ⇒ no plan, no exception, no local browser."""
    user = _make_user(db_session, "plan-nodevice@example.com")
    ticket = SimpleNamespace(**{**_TICKET.__dict__, "owner_id": user.id})

    plan = planner_agent_service.plan_ticket(
        _RUN, ticket, _CONTEXT, owner_id=user.id, target=_TARGET
    )

    assert plan is None
    assert no_local_browser == []
    # Nothing was queued either — a device that pairs later must not pick up a
    # plan for a pass that already generated from text.
    assert db_session.query(AgentPlanningSession).count() == 0


def test_plan_ticket_falls_back_when_the_device_never_claims(
    db_session, monkeypatch, local_agent_target, no_local_browser
):
    """NEGATIVE CONTROL for the timeout: paired but not polling."""
    user = _make_user(db_session, "plan-timeout@example.com")
    _pair_device(db_session, user)
    monkeypatch.setattr(agent_planning_service, "CLAIM_DEADLINE", timedelta(seconds=0.05))
    monkeypatch.setattr(agent_planning_service, "POLL_INTERVAL", 0.01)
    ticket = SimpleNamespace(**{**_TICKET.__dict__, "owner_id": user.id})

    plan = planner_agent_service.plan_ticket(
        _RUN, ticket, _CONTEXT, owner_id=user.id, target=_TARGET
    )

    assert plan is None
    assert no_local_browser == []
    row = db_session.query(AgentPlanningSession).first()
    assert row is not None and row.status == "expired"


def test_plan_ticket_falls_back_when_the_device_reports_a_failure(
    db_session, monkeypatch, local_agent_target, no_local_browser
):
    """NEGATIVE CONTROL for a device-side failure (no captured login, Chrome died…)."""
    user = _make_user(db_session, "plan-devicefail@example.com")
    _pair_device(db_session, user)
    monkeypatch.setattr(agent_planning_service, "POLL_INTERVAL", 0.01)
    ticket = SimpleNamespace(**{**_TICKET.__dict__, "owner_id": user.id})
    device = _fake_device(user.id, plan_json="", ok=False, summary="No authenticated profile")

    plan = planner_agent_service.plan_ticket(
        _RUN, ticket, _CONTEXT, owner_id=user.id, target=_TARGET
    )
    device.join(timeout=5)

    assert plan is None
    assert no_local_browser == []
    row = db_session.query(AgentPlanningSession).first()
    assert row is not None and row.status == "failed"


def test_plan_ticket_without_a_base_url_never_reaches_the_device(
    db_session, local_agent_target, no_local_browser
):
    user = _make_user(db_session, "plan-nourl@example.com")
    _pair_device(db_session, user)
    ticket = SimpleNamespace(**{**_TICKET.__dict__, "owner_id": user.id})

    plan = planner_agent_service.plan_ticket(
        _RUN,
        ticket,
        {"projectKey": "surency", "repo": "web"},
        owner_id=user.id,
        target=_TARGET,
    )

    assert plan is None
    assert no_local_browser == []
    assert db_session.query(AgentPlanningSession).count() == 0


def test_server_target_never_enqueues_a_planning_session(db_session, monkeypatch):
    """The `executionTarget="server"` path is untouched: in-process, no queue.

    Pinned by an observable effect of the server branch — it calls
    `browser_session` — rather than by the return value, which is `None` on both
    paths when things go wrong.
    """
    assert settings_store.load_settings().get("executionTarget") == "server"
    monkeypatch.setattr(planner_agent_service.claude_cli, "playwright_cli_available", lambda: True)
    launched: list[str] = []

    def _record(base_url, profile_dir, name, *, browser_driver="playwright-cli"):
        launched.append(browser_driver)
        raise RuntimeError("stop here — the in-process branch is what we are pinning")

    monkeypatch.setattr(planner_agent_service.agentic_browser, "browser_session", _record)

    plan = planner_agent_service.plan_ticket(
        _RUN, _TICKET, _CONTEXT, owner_id=None, target=_TARGET
    )

    assert plan is None  # the raise degrades to text-only, as before
    assert launched == ["playwright-cli"]
    assert db_session.query(AgentPlanningSession).count() == 0
