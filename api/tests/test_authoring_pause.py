"""Pause live authoring, feed guidance, continue the same Claude session (#619).

The user's ask was *"stop at the mid of the authoring process, feed more input,
then continue"*, with the browser left OPEN so they can click their way to the
screen Claude cannot reach on its own. Making that work required three things the
code did not have, and every one of them is a place this could quietly not work:

1. **Claude CLI's own ``session_id``** — ``AgentAuthoringSession.session_id`` is
   Q-Agent's queue id and ``claude --resume`` has never heard of it. The CLI's id
   rides on the ``--output-format stream-json`` envelope; nothing read it, so
   there was nothing to resume with. It is now handed over on ``/paused`` and
   persisted, which is what the first tests here pin.
2. **State that survives a restart.** A paused session is the *worst* thing to
   keep in process memory (#605/#625): losing it strands a live Chrome window and
   a temp dir on the user's machine. So the pause protocol is columns, and the
   tests below discard module state (``importlib.reload``) between the pause and
   the resume, the way a deploy would.
3. **A fallback.** ``--resume`` is not ours to control and the transcript can be
   gone, so Continue must degrade to a fresh guided pass. The server's half of
   that is handing the device the *whole* accumulated guidance rather than only
   the newest turn — a fresh Claude has no memory of the earlier ones. Asserted
   here; the device-side decision itself is unit-tested in
   ``agent/test/authoringResume.test.ts``.

Also pinned: the cost ceiling spans the WHOLE session across resumes (a per-pass
ceiling would make a pause/continue loop unbounded), a forgotten pause expires and
is torn down, and Pause outside live authoring is a clean refusal rather than a
half-applied state change.

Real engines only (ADR 0001): no browser, no ``claude``. Only the HTTP wiring, the
durable pause state and the sweep are under test.
"""

from __future__ import annotations

import importlib
import time
from datetime import timedelta

import pytest

from app.db import utcnow
from app.models.agent_authoring import AgentAuthoringSession
from app.models.run import Run
from app.models.testcase import AutomationSpec, TestCase
from app.models.user import User
from app.services import agent_authoring_service, agent_device_service, auth_service


# --------------------------------------------------------------------- helpers
# Defined locally, on purpose: tests/conftest.py is shared by the whole suite and
# a pause-specific fixture has no business in it.
def _make_user(db_session, email: str = "pause-owner@example.com") -> User:
    user = User(
        email=email,
        first_name="Pause",
        last_name="Owner",
        password_hash=auth_service.hash_password("password123"),
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _pair_device(db_session, user, name: str = "Pause Device") -> str:
    code = agent_device_service.create_pairing_code(db_session, user)
    _device, token = agent_device_service.redeem_pairing_code(db_session, code, name)
    return token


def _seed_case(db_session, *, owner_id: int | None) -> tuple[Run, TestCase, AutomationSpec]:
    run = Run(code="RUN-8801", name="Pause/resume", status="automation", owner_id=owner_id)
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    case = TestCase(
        run_id=run.id,
        ticket_external_id="SUR-1900",
        code="TC-01",
        title="Approve a claim from the queue",
        objective="Prove an approval sticks",
        steps=[{"a": "Open the queue", "e": "Queue renders"}],
        test_data=[],
        linked_ac=["AC-1"],
        approval="approved",
    )
    db_session.add(case)
    db_session.commit()
    db_session.refresh(case)
    spec = AutomationSpec(
        test_case_id=case.id,
        filename="tests/SUR-1900/SUR-1900-TC-01.spec.ts",
        status="running",
        code="",
    )
    db_session.add(spec)
    db_session.commit()
    db_session.refresh(spec)
    return run, case, spec


def _enqueue(*, owner_id: int | None, case_id: int, run_id: int | None, budget: float = 3.5) -> None:
    agent_authoring_service.request_authoring(
        "sess-pause-1",
        owner_id=owner_id,
        project_key="Surency",
        repo="web",
        base_url="https://hub-qa.surency.com/",
        origin="https://hub-qa.surency.com",
        case_id=case_id,
        run_id=run_id,
        spec_filename="tests/SUR-1900/SUR-1900-TC-01.spec.ts",
        system_prompt="the live-authoring skill",
        task_prompt="author SUR-1900 TC-01",
        model="claude-sonnet-4-5",
        max_budget_usd=budget,
    )


@pytest.fixture
def live_authoring(client, db_session):
    """A case whose authoring session is CLAIMED by a paired device (status running).

    Returns everything the tests address it by, including the device auth header —
    the agent half of the protocol is device-authenticated and the user half is
    not, and mixing those up is an easy way to test nothing.
    """
    user = _make_user(db_session)
    token = _pair_device(db_session, user)
    run, case, spec = _seed_case(db_session, owner_id=user.id)
    _enqueue(owner_id=user.id, case_id=case.id, run_id=run.id)
    headers = {"Authorization": f"Bearer {token}"}
    claim = client.post("/agent/authoring/next", headers=headers)
    assert claim.status_code == 200, claim.text
    return {
        "user": user,
        "headers": headers,
        "run": run,
        "case": case,
        "spec": spec,
        "session_id": claim.json()["sessionId"],
    }


def _step(client, ctx) -> dict:
    """One progress post — the channel the agent already uses once per Claude step."""
    resp = client.post(
        f"/agent/authoring/{ctx['session_id']}/events",
        json={"event": "authoring.progress", "payload": {"phase": "step", "message": "clicked"}},
        headers=ctx["headers"],
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _row(db_session) -> AgentAuthoringSession:
    return db_session.query(AgentAuthoringSession).one()


# --------------------------------------- pause reaches the device, on one channel
def test_pause_reaches_the_device_on_the_step_channel_it_already_polls(client, live_authoring):
    """No new poller: the user's Pause rides the per-step progress post.

    A separate endpoint the agent had to poll would be a second thing to get
    wrong; this one is already called once per Claude step, and already carries
    the abort-on-stop signal (#420).
    """
    ctx = live_authoring
    assert _step(client, ctx)["control"] == "", "nothing was asked for yet"

    resp = client.post(f"/cases/{ctx['case'].id}/authoring/pause")
    assert resp.status_code == 200, resp.text
    assert resp.json()["outcome"] == "requested"

    assert _step(client, ctx)["control"] == "pause"


def test_pausing_a_case_that_is_not_being_live_authored_is_a_clean_refusal(client, db_session):
    """"Pause during a NON-authoring stage is either unavailable or a clean no-op."

    A 409 with nothing changed, rather than a 200 that flags a pause nobody can
    act on — which would leave the UI showing "pausing…" forever.
    """
    user = _make_user(db_session)
    _run, case, _spec = _seed_case(db_session, owner_id=user.id)
    resp = client.post(f"/cases/{case.id}/authoring/pause")
    assert resp.status_code == 409, resp.text
    assert db_session.query(AgentAuthoringSession).count() == 0


def test_pausing_a_session_no_device_has_claimed_yet_is_refused(client, db_session):
    """A ``queued`` session has no Chrome and no Claude — there is nothing to pause."""
    user = _make_user(db_session)
    run, case, _spec = _seed_case(db_session, owner_id=user.id)
    _enqueue(owner_id=user.id, case_id=case.id, run_id=run.id)

    resp = client.post(f"/cases/{case.id}/authoring/pause")
    assert resp.status_code == 409, resp.text
    # And the row is untouched, so the agent's eventual claim behaves normally.
    assert _row(db_session).pause_requested is False
    assert _row(db_session).status == "queued"


# ------------------------------------------- capturing Claude's OWN session id
def test_the_claude_cli_session_id_is_captured_and_persisted(client, db_session, live_authoring):
    """The thing that made resume impossible: nobody read the CLI's own id.

    ``session_id`` on the row is Q-Agent's; ``claude_session_id`` is Claude's. The
    test asserts they are DIFFERENT values, because conflating them is exactly the
    bug: a ``--resume`` with the queue id would fail every time.
    """
    ctx = live_authoring
    resp = client.post(
        f"/agent/authoring/{ctx['session_id']}/paused",
        json={"claudeSessionId": "claude-abc-123", "costUsd": 0.25},
        headers=ctx["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "paused"

    row = _row(db_session)
    assert row.status == "paused"
    assert row.paused_at is not None
    assert row.pause_requested is False, "the request was consumed, not left pending"
    assert row.claude_session_id == "claude-abc-123"
    assert row.claude_session_id != row.session_id, "these are two different identities"


def test_a_pause_that_captured_no_session_id_is_flagged_as_unresumable(client, live_authoring):
    """An EMPTY id is data, not an error — it is what makes Continue fall back.

    The device reports the pause either way; the UI needs to know which kind it
    got, so it does not promise preserved context it cannot deliver.
    """
    ctx = live_authoring
    resp = client.post(
        f"/agent/authoring/{ctx['session_id']}/paused",
        json={"claudeSessionId": "", "costUsd": 0.1},
        headers=ctx["headers"],
    )
    assert resp.status_code == 200, resp.text
    state = client.get(f"/cases/{ctx['case'].id}/authoring").json()
    assert state["canContinue"] is True, "it is still continuable…"
    assert state["resumable"] is False, "…but only via the fallback path"


# ------------------------------------- the pause survives losing every process
def test_a_paused_session_survives_a_state_discard_and_is_still_resumable(
    client, db_session, live_authoring
):
    """A deploy mid-pause must not strand the browser the device is holding.

    ``importlib.reload`` throws away every scrap of module state, the way a new
    process would. Before the pause protocol was durable this is where a paused
    session would simply cease to exist — and with it any way to tell the device
    to close Chrome.
    """
    ctx = live_authoring
    client.post(
        f"/agent/authoring/{ctx['session_id']}/paused",
        json={"claudeSessionId": "claude-abc-123", "costUsd": 0.25},
        headers=ctx["headers"],
    )
    importlib.reload(agent_authoring_service)

    state = client.get(f"/cases/{ctx['case'].id}/authoring").json()
    assert (state["active"], state["status"], state["canContinue"]) == (True, "paused", True)
    assert state["resumable"] is True

    resume = client.post(
        f"/cases/{ctx['case'].id}/authoring/continue",
        json={"guidance": "I opened the approval modal by hand — approve it"},
    )
    assert resume.status_code == 200, resume.text

    directive = client.post(
        f"/agent/authoring/{ctx['session_id']}/resume", headers=ctx["headers"]
    ).json()
    assert directive["action"] == "resume"
    assert directive["claudeSessionId"] == "claude-abc-123"
    assert directive["guidance"] == ["I opened the approval modal by hand — approve it"]
    assert _row(db_session).status == "running"
    assert _row(db_session).resume_count == 1


def test_a_still_paused_session_tells_the_device_to_keep_waiting(client, live_authoring):
    """``wait`` is what keeps Chrome open while the user is still clicking."""
    ctx = live_authoring
    client.post(
        f"/agent/authoring/{ctx['session_id']}/paused",
        json={"claudeSessionId": "claude-abc-123", "costUsd": 0.0},
        headers=ctx["headers"],
    )
    body = client.post(
        f"/agent/authoring/{ctx['session_id']}/resume", headers=ctx["headers"]
    ).json()
    assert body["action"] == "wait"
    assert body["guidance"] == []


def test_guidance_is_handed_over_exactly_once(client, live_authoring):
    """A duplicated poll must not replay the turn as if the user typed it twice.

    The consume and the ``resuming -> running`` flip are one conditional UPDATE, so
    the second poll sees ``running`` and gets nothing to act on.
    """
    ctx = live_authoring
    client.post(
        f"/agent/authoring/{ctx['session_id']}/paused",
        json={"claudeSessionId": "claude-abc-123", "costUsd": 0.0},
        headers=ctx["headers"],
    )
    client.post(f"/cases/{ctx['case'].id}/authoring/continue", json={"guidance": "approve it"})

    first = client.post(
        f"/agent/authoring/{ctx['session_id']}/resume", headers=ctx["headers"]
    ).json()
    second = client.post(
        f"/agent/authoring/{ctx['session_id']}/resume", headers=ctx["headers"]
    ).json()
    assert first["action"] == "resume" and first["guidance"] == ["approve it"]
    assert second["action"] != "resume", second
    assert second["guidance"] == []


def test_continue_on_a_session_that_is_not_paused_is_refused(client, live_authoring):
    ctx = live_authoring
    resp = client.post(f"/cases/{ctx['case'].id}/authoring/continue", json={"guidance": "go"})
    assert resp.status_code == 409, resp.text


# ------------------------------------------------- guidance comes from the chat
def test_spec_chat_becomes_the_guidance_channel_while_authoring_holds_the_case(
    client, live_authoring
):
    """"Guidance is typed in the existing spec chat panel", not a new surface.

    The negative control matters more than the positive one here: the message must
    NOT also start a spec-edit pass. Editing a spec a device is mid-authoring is
    incoherent — the agent overwrites it on finalize — so a green "message
    accepted" that silently ran the old path would be a real bug wearing a pass.
    """
    from app.routers import automation as automation_router

    ctx = live_authoring
    client.post(
        f"/agent/authoring/{ctx['session_id']}/paused",
        json={"claudeSessionId": "claude-abc-123", "costUsd": 0.0},
        headers=ctx["headers"],
    )
    resp = client.post(
        f"/cases/{ctx['case'].id}/spec/chat",
        json={"message": "the record I need is called ACME-7 — use that one"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["routedToAuthoring"] is True
    assert body["started"] is False
    assert body["guidancePending"] == 1
    # The negative control: no spec-edit pass was started for this case.
    assert ctx["case"].id not in automation_router._chatting_cases

    # And it really reaches the device.
    client.post(f"/cases/{ctx['case'].id}/authoring/continue", json={})
    directive = client.post(
        f"/agent/authoring/{ctx['session_id']}/resume", headers=ctx["headers"]
    ).json()
    assert directive["guidance"] == ["the record I need is called ACME-7 — use that one"]


def test_the_fallback_gets_every_guidance_turn_not_just_the_newest(client, live_authoring):
    """A fresh pass has no memory, so the newest turn alone is a non-sequitur.

    ``guidance`` is what a genuine ``--resume`` needs (the session remembers the
    rest); ``guidanceHistory`` is what the fallback needs. The device picks, so the
    server must always send both — and they must genuinely differ, or the fallback
    is silently as forgetful as no fallback at all.
    """
    ctx = live_authoring
    client.post(
        f"/agent/authoring/{ctx['session_id']}/paused",
        json={"claudeSessionId": "", "costUsd": 0.0},
        headers=ctx["headers"],
    )
    client.post(f"/cases/{ctx['case'].id}/spec/chat", json={"message": "log in as admin"})
    client.post(f"/cases/{ctx['case'].id}/authoring/continue", json={"guidance": "then approve"})

    directive = client.post(
        f"/agent/authoring/{ctx['session_id']}/resume", headers=ctx["headers"]
    ).json()
    assert directive["guidance"] == ["log in as admin", "then approve"]
    assert directive["guidanceHistory"] == ["log in as admin", "then approve"]
    assert directive["claudeSessionId"] == "", "no id ⇒ the device will run a fresh pass"

    # After a second pause the undelivered queue is empty again, but the HISTORY
    # still carries everything — which is the whole reason it exists.
    client.post(
        f"/agent/authoring/{ctx['session_id']}/paused",
        json={"claudeSessionId": "", "costUsd": 0.1},
        headers=ctx["headers"],
    )
    client.post(f"/cases/{ctx['case'].id}/authoring/continue", json={"guidance": "and save"})
    second = client.post(
        f"/agent/authoring/{ctx['session_id']}/resume", headers=ctx["headers"]
    ).json()
    assert second["guidance"] == ["and save"]
    assert second["guidanceHistory"] == ["log in as admin", "then approve", "and save"]


def test_an_empty_chat_message_is_still_rejected_while_authoring(client, live_authoring):
    """The guidance route must not weaken the existing validation."""
    ctx = live_authoring
    assert client.post(f"/cases/{ctx['case'].id}/spec/chat", json={"message": "   "}).status_code == 400


# ------------------------------------------- the budget spans the WHOLE session
def test_the_cost_ceiling_spans_the_whole_session_across_resumes(client, db_session, live_authoring):
    """A per-pass ceiling would make pause/continue an unbounded spend.

    The ceiling is 3.50. Each pause reports the SESSION total spent so far, and
    each resume is handed the REMAINDER — never the ceiling again.
    """
    ctx = live_authoring
    assert _row(db_session).max_budget_usd == pytest.approx(3.5)

    def pause_then_resume(total_spent: float) -> dict:
        paused = client.post(
            f"/agent/authoring/{ctx['session_id']}/paused",
            json={"claudeSessionId": "claude-abc-123", "costUsd": total_spent},
            headers=ctx["headers"],
        )
        assert paused.status_code == 200, paused.text
        client.post(f"/cases/{ctx['case'].id}/authoring/continue", json={})
        return client.post(
            f"/agent/authoring/{ctx['session_id']}/resume", headers=ctx["headers"]
        ).json()

    first = pause_then_resume(1.25)
    assert first["remainingBudgetUsd"] == pytest.approx(2.25)

    second = pause_then_resume(3.0)
    assert second["remainingBudgetUsd"] == pytest.approx(0.5)
    assert second["resumeCount"] == 2

    # Spend past the ceiling and there is nothing to resume with — refused on BOTH
    # sides: the user's Continue is a 409, and a device that resumed anyway is told
    # to abort rather than being handed a nonsense budget.
    client.post(
        f"/agent/authoring/{ctx['session_id']}/paused",
        json={"claudeSessionId": "claude-abc-123", "costUsd": 3.6},
        headers=ctx["headers"],
    )
    refused = client.post(f"/cases/{ctx['case'].id}/authoring/continue", json={})
    assert refused.status_code == 409, refused.text
    assert "budget" in refused.json()["detail"].lower()
    assert _row(db_session).status == "paused", "a refused Continue must not flip the state"


def test_a_replayed_pause_post_cannot_lower_the_recorded_spend(client, db_session, live_authoring):
    """Spend is stored absolutely and monotonically.

    The device reports a session TOTAL, so a retried or out-of-order post must not
    walk the ledger backwards — that would hand back budget that was already spent.
    """
    ctx = live_authoring
    for total in (2.0, 0.5):
        client.post(
            f"/agent/authoring/{ctx['session_id']}/paused",
            json={"claudeSessionId": "c1", "costUsd": total},
            headers=ctx["headers"],
        )
    assert _row(db_session).cost_usd_so_far == pytest.approx(2.0)


def test_a_session_cannot_be_resumed_without_end(client, db_session, live_authoring):
    """The resume cap: a pass killed for a pause may never report its cost.

    The budget subtraction cannot bound spend the device never told us about, so
    the number of resumes is capped too. Without this, an unreported pass is free
    and a pause/continue loop is unbounded regardless of the ceiling.
    """
    ctx = live_authoring
    cap = agent_authoring_service.MAX_RESUMES_PER_SESSION
    for _ in range(cap):
        client.post(
            f"/agent/authoring/{ctx['session_id']}/paused",
            json={"claudeSessionId": "c1", "costUsd": 0.0},
            headers=ctx["headers"],
        )
        assert (
            client.post(f"/cases/{ctx['case'].id}/authoring/continue", json={}).status_code == 200
        )
        assert (
            client.post(
                f"/agent/authoring/{ctx['session_id']}/resume", headers=ctx["headers"]
            ).json()["action"]
            == "resume"
        )
    assert _row(db_session).resume_count == cap

    client.post(
        f"/agent/authoring/{ctx['session_id']}/paused",
        json={"claudeSessionId": "c1", "costUsd": 0.0},
        headers=ctx["headers"],
    )
    over = client.post(f"/cases/{ctx['case'].id}/authoring/continue", json={})
    assert over.status_code == 409, over.text


# ------------------------------------------------------------ the pause expires
def test_an_expired_pause_tells_the_device_to_tear_everything_down(
    client, db_session, live_authoring
):
    """"A paused session expires: browser and temp dir torn down, nothing leaked."

    This is the ONLY path that can actually close the held-open Chrome, because
    only the device can close it. So the directive is asserted, not just the row.
    """
    ctx = live_authoring
    client.post(
        f"/agent/authoring/{ctx['session_id']}/paused",
        json={"claudeSessionId": "c1", "costUsd": 0.1},
        headers=ctx["headers"],
    )
    row = _row(db_session)
    row.paused_at = utcnow() - agent_authoring_service.PAUSE_EXPIRES_AFTER - timedelta(minutes=1)
    db_session.add(row)
    db_session.commit()

    directive = client.post(
        f"/agent/authoring/{ctx['session_id']}/resume", headers=ctx["headers"]
    ).json()
    assert directive["action"] == "abort"
    assert directive["reason"] == "pause-expired"

    # And the user cannot revive it either — otherwise the device would already
    # have torn Chrome down while the server still thought it was resumable.
    late = client.post(f"/cases/{ctx['case'].id}/authoring/continue", json={"guidance": "hi"})
    assert late.status_code == 409, late.text


def test_the_boot_sweep_keeps_a_fresh_pause_and_expires_a_forgotten_one(client, db_session):
    """A paused session is the one state the #605 sweep must NOT destroy…

    …because the device is holding a live Chrome and a live workdir for it and
    survives an API restart exactly like a ``running`` one. But a pause nobody
    continued has to be released, or the browser stays on the user's desktop
    forever. Both halves in one test so neither can be "fixed" by breaking the
    other.
    """
    from app.services.run_status import recover_orphaned_authoring

    user = _make_user(db_session)
    fresh_run, fresh_case, fresh_spec = _seed_case(db_session, owner_id=user.id)
    _enqueue(owner_id=user.id, case_id=fresh_case.id, run_id=fresh_run.id)
    fresh = db_session.query(AgentAuthoringSession).one()
    fresh.status = "paused"
    fresh.paused_at = utcnow()
    fresh.claude_session_id = "c1"
    db_session.add(fresh)
    db_session.commit()

    recovered, dropped = recover_orphaned_authoring(db_session)
    db_session.expire_all()
    assert db_session.query(AgentAuthoringSession).count() == 1, "a fresh pause is NOT stranded"
    assert (recovered, dropped) == (0, 0)
    assert db_session.get(AutomationSpec, fresh_spec.id).status == "running"

    # Now let the same pause go stale and sweep again.
    stale = db_session.query(AgentAuthoringSession).one()
    stale.paused_at = utcnow() - agent_authoring_service.PAUSE_EXPIRES_AFTER - timedelta(hours=1)
    db_session.add(stale)
    db_session.commit()

    recovered, dropped = recover_orphaned_authoring(db_session)
    db_session.expire_all()
    assert db_session.query(AgentAuthoringSession).count() == 0
    assert dropped == 1
    # The spec is left re-triggerable rather than spinning forever on a session
    # that no longer exists.
    assert recovered == 1
    assert db_session.get(AutomationSpec, fresh_spec.id).status == "blocked"


def test_a_fresh_pause_blocks_a_re_enqueue_but_an_expired_one_does_not(client, db_session):
    """Regenerate must not yank the case out from under a live pause…

    …and must not be blocked forever by a dead one. ``request_authoring`` measures
    a paused row from ``paused_at``, not ``claimed_at``: a long, legitimate pause is
    not an abandoned claim.
    """
    user = _make_user(db_session)
    run, case, _spec = _seed_case(db_session, owner_id=user.id)
    _enqueue(owner_id=user.id, case_id=case.id, run_id=run.id)
    row = db_session.query(AgentAuthoringSession).one()
    row.status = "paused"
    row.paused_at = utcnow()
    row.claude_session_id = "c1"
    row.cost_usd_so_far = 1.0
    row.resume_count = 2
    db_session.add(row)
    db_session.commit()

    _enqueue(owner_id=user.id, case_id=case.id, run_id=run.id)
    db_session.expire_all()
    assert db_session.query(AgentAuthoringSession).one().status == "paused"

    row = db_session.query(AgentAuthoringSession).one()
    row.paused_at = utcnow() - agent_authoring_service.PAUSE_EXPIRES_AFTER - timedelta(minutes=1)
    db_session.add(row)
    db_session.commit()

    _enqueue(owner_id=user.id, case_id=case.id, run_id=run.id)
    db_session.expire_all()
    recycled = db_session.query(AgentAuthoringSession).one()
    assert recycled.status == "queued"
    # The whole pause ledger is reset, so the new pass gets its FULL budget and a
    # stale Claude session id can never be resumed into a different attempt.
    assert recycled.claude_session_id == ""
    assert recycled.cost_usd_so_far == pytest.approx(0.0)
    assert recycled.resume_count == 0
    assert recycled.paused_at is None


def test_a_claim_resets_the_pause_ledger_from_any_earlier_life(client, db_session):
    """A re-claimed session must not inherit another attempt's spend or session id."""
    user = _make_user(db_session)
    token = _pair_device(db_session, user)
    run, case, _spec = _seed_case(db_session, owner_id=user.id)
    _enqueue(owner_id=user.id, case_id=case.id, run_id=run.id)
    row = db_session.query(AgentAuthoringSession).one()
    row.claude_session_id = "stale-session"
    row.cost_usd_so_far = 3.4
    row.resume_count = 5
    row.pause_requested = True
    db_session.add(row)
    db_session.commit()

    assert (
        client.post(
            "/agent/authoring/next", headers={"Authorization": f"Bearer {token}"}
        ).status_code
        == 200
    )
    db_session.expire_all()
    claimed = db_session.query(AgentAuthoringSession).one()
    assert claimed.status == "running"
    assert claimed.claude_session_id == ""
    assert claimed.cost_usd_so_far == pytest.approx(0.0)
    assert claimed.resume_count == 0
    assert claimed.pause_requested is False


def test_the_state_endpoint_returns_the_guidance_already_sent(client, live_authoring):
    """#644: the paused UI needs the turns themselves, not just how many.

    The state endpoint reported `guidanceGiven` as a COUNT, which is exactly the
    information a user resuming a second time cannot use: without seeing what they
    already said they either repeat it or contradict an instruction they forgot.
    (Until #644 the Continue control also sent `""` every time, so this history was
    always empty in practice — the field is what makes the input worth having.)
    """
    ctx = live_authoring
    client.post(
        f"/agent/authoring/{ctx['session_id']}/paused",
        json={"claudeSessionId": "claude-abc-123", "costUsd": 0.0},
        headers=ctx["headers"],
    )

    first = "The dashboard needs a hard reload before the widget appears"
    assert (
        client.post(
            f"/cases/{ctx['case'].id}/authoring/continue", json={"guidance": first}
        ).status_code
        == 200
    )
    # Hand it over, so it moves from pending into history.
    client.post(f"/agent/authoring/{ctx['session_id']}/resume", headers=ctx["headers"])

    state = client.get(f"/cases/{ctx['case'].id}/authoring").json()
    assert state["guidanceHistory"] == [first]
    assert state["guidanceGiven"] == 1

    # A second turn accumulates rather than replacing — the history is the whole
    # conversation, which is the point.
    client.post(
        f"/agent/authoring/{ctx['session_id']}/paused",
        json={"claudeSessionId": "claude-abc-123", "costUsd": 0.0},
        headers=ctx["headers"],
    )
    second = "Assert on the total, not the row count"
    client.post(f"/cases/{ctx['case'].id}/authoring/continue", json={"guidance": second})
    client.post(f"/agent/authoring/{ctx['session_id']}/resume", headers=ctx["headers"])

    state = client.get(f"/cases/{ctx['case'].id}/authoring").json()
    assert state["guidanceHistory"] == [first, second]


def test_continuing_with_no_guidance_is_still_valid(client, live_authoring):
    """#644: an empty box means "carry on as you were", not a validation error.

    The input must never block Continue — that would make the new field a
    regression for the plain resume that worked before it existed.
    """
    ctx = live_authoring
    client.post(
        f"/agent/authoring/{ctx['session_id']}/paused",
        json={"claudeSessionId": "claude-abc-123", "costUsd": 0.0},
        headers=ctx["headers"],
    )
    resp = client.post(f"/cases/{ctx['case'].id}/authoring/continue", json={"guidance": ""})
    assert resp.status_code == 200, resp.text

    directive = client.post(
        f"/agent/authoring/{ctx['session_id']}/resume", headers=ctx["headers"]
    ).json()
    assert directive["action"] == "resume"
    assert directive["guidance"] == []
    assert client.get(f"/cases/{ctx['case'].id}/authoring").json()["guidanceHistory"] == []


# ------------------------------------------------------------------ cancel (#645)
def test_cancel_frees_a_running_session_and_stops_the_device(client, live_authoring, db_session):
    """#645: cancel needs nothing from the agent, so it works on deployed devices.

    Deleting the row IS the cancel: the device posts a progress event per Claude
    step, and that endpoint answers 404 once the row is gone — which the agent
    already treats as "the run was stopped, abort". Pinning the 404 here is
    pinning the mechanism, not just the status code: without it cancel would need
    an agent release, and the state it rescues users from otherwise lasts an hour.
    """
    ctx = live_authoring
    assert _step(client, ctx) == {"ok": True, "control": ""}

    resp = client.post(f"/cases/{ctx['case'].id}/authoring/cancel")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"cancelled": True, "was": "running"}

    assert db_session.query(AgentAuthoringSession).count() == 0
    # The device's next step post is told the session is gone.
    gone = client.post(
        f"/agent/authoring/{ctx['session_id']}/events",
        json={"event": "authoring.progress", "payload": {"phase": "step", "message": "clicked"}},
        headers=ctx["headers"],
    )
    assert gone.status_code == 404


def test_cancel_tells_a_paused_device_to_tear_down(client, live_authoring, db_session):
    """#645: the reported case — paused, browser open, user wants out.

    The paused device polls the resume directive, and `abort` is what closes
    Chrome. Before cancel existed the only answer was `wait` until
    PAUSE_EXPIRES_AFTER (an hour) had passed.
    """
    ctx = live_authoring
    client.post(
        f"/agent/authoring/{ctx['session_id']}/paused",
        json={"claudeSessionId": "claude-abc-123", "costUsd": 0.1},
        headers=ctx["headers"],
    )
    assert (
        client.post(f"/agent/authoring/{ctx['session_id']}/resume", headers=ctx["headers"]).json()[
            "action"
        ]
        == "wait"
    )

    assert client.post(f"/cases/{ctx['case'].id}/authoring/cancel").json() == {
        "cancelled": True,
        "was": "paused",
    }

    directive = client.post(
        f"/agent/authoring/{ctx['session_id']}/resume", headers=ctx["headers"]
    ).json()
    assert directive["action"] == "abort"
    assert directive["reason"] == "session-gone"


def test_cancel_is_idempotent(client, live_authoring):
    """#645: cancelling twice is not an error — the intent is already satisfied."""
    ctx = live_authoring
    assert client.post(f"/cases/{ctx['case'].id}/authoring/cancel").json()["cancelled"] is True
    second = client.post(f"/cases/{ctx['case'].id}/authoring/cancel")
    assert second.status_code == 200
    assert second.json() == {"cancelled": False}


def test_a_cancelled_case_is_re_runnable(client, live_authoring, db_session):
    """#645: the point of cancelling is being able to run it again.

    Two things had to be true and neither was: the case's UNIQUE session slot must
    be free so a fresh session can be queued, and the placeholder spec row (which
    `_enqueue_agent_authoring` writes at status="running" with NO code) must not
    make the case look already-generated to an incremental pass — the same
    invisible-retry shape as #641.
    """
    from app.routers.automation import _eligible_cases_query
    from app.models.testcase import AutomationSpec as Spec

    ctx = live_authoring
    client.post(f"/cases/{ctx['case'].id}/authoring/cancel")

    spec = db_session.query(Spec).filter(Spec.test_case_id == ctx["case"].id).one()
    db_session.refresh(spec)
    assert spec.status == "failed"
    assert "cancelled" in spec.block_reason.lower()
    assert not (spec.code or "").strip()

    assert ctx["case"].id in {c.id for c in _eligible_cases_query(db_session, ctx["run"].id).all()}

    # Drive a REAL incremental pass (force=False) and record which cases it hands
    # to generation. Asserting a locally-recomputed skip set here would only test
    # the test: negative-controlled by reverting the empty-spec exclusion, which
    # leaves `attempted` empty.
    from app.routers import automation as automation_router

    attempted: list[int] = []

    def spy(_db, _run, case, **_kwargs):
        attempted.append(case.id)
        raise ValueError("stop here — the skip decision is what is under test")

    original = automation_router._generate_one
    automation_router._generate_one = spy
    try:
        assert client.post(f"/runs/{ctx['run'].id}/automation/generate").status_code == 200
        for _ in range(100):
            time.sleep(0.05)
            if not automation_router.is_generating(ctx["run"].id):
                break
    finally:
        automation_router._generate_one = original

    assert attempted == [ctx["case"].id], (
        "an empty placeholder spec hid the cancelled case from an incremental retry"
    )

    # And the session slot is free, so a fresh authoring session can be queued.
    _enqueue(owner_id=ctx["user"].id, case_id=ctx["case"].id, run_id=ctx["run"].id)
    assert _row(db_session).status == "queued"


def test_cancel_is_owner_scoped(client, live_authoring, db_session):
    """#645: one user must not be able to cancel another's authoring session."""
    import app.config as config_module

    ctx = live_authoring
    other = _make_user(db_session, "not-the-owner@example.com")

    # With the guard ON the caller is a real user, so ownership actually applies —
    # with the suite default the caller is None and this would pass vacuously.
    config_module.settings.auth_required = True
    try:
        headers = {
            "Authorization": f"Bearer {auth_service.create_access_token(other, sid='other-sid')}"
        }
        resp = client.post(f"/cases/{ctx['case'].id}/authoring/cancel", headers=headers)
        assert resp.status_code == 404, resp.text
    finally:
        config_module.settings.auth_required = False

    assert db_session.query(AgentAuthoringSession).count() == 1, "another user's session was cancelled"


def test_cancel_announces_a_terminal_phase_the_client_recognises(
    client, live_authoring, monkeypatch
):
    """#653: the phase name is a contract with the SPA, so pin it.

    #645 added `phase: "cancelled"` on the server and the client's reducer only
    treated `done`/`failed` as terminal, so after a cancel the trail kept its
    `working…` spinner and the header kept saying `authoring…` for a session that
    had already been deleted. The client now handles `cancelled`; this fails if the
    phase is ever renamed or the event dropped, which is the half a frontend with
    no test harness cannot pin for itself.
    """
    from app.ws import hub

    published: list[tuple] = []
    monkeypatch.setattr(hub, "publish", lambda *a: published.append(a))

    ctx = live_authoring
    assert client.post(f"/cases/{ctx['case'].id}/authoring/cancel").json()["cancelled"] is True

    progress = [p for p in published if p[1] == "authoring.progress"]
    assert progress, "cancel published no progress event — the trail would spin forever"
    payload = progress[-1][2]
    assert payload["phase"] == "cancelled"
    assert payload["case"] == ctx["case"].id
    assert "cancelled" in payload["message"].lower()


# ------------------------------------------------------ verification (#657)
def test_the_claim_ships_the_project_so_the_spec_can_be_run(client, db_session):
    """#657: the fix is inert unless the bundle actually reaches the device.

    Live-authored specs were finalized having never been executed — "authored"
    meant a non-empty FILE existed. The agent now runs the spec through the real
    execution path, but only if it has the project staged: the authoring workdir is
    a bare temp dir, so without this bundle there is no package.json and no
    `@q-agent/playwright-base`, and any run fails on the ENVIRONMENT rather than
    telling anyone anything about the spec.
    """
    from app.models.automation_project import AutomationProject
    from app.models.testcase import AutomationSpec as Spec

    user = _make_user(db_session, "verify-owner@example.com")
    token = _pair_device(db_session, user)
    run, case, spec = _seed_case(db_session, owner_id=user.id)

    project = AutomationProject(
        project_key="surency", repo="surency-admin-hub", owner_id=user.id, slug="surency-admin-hub"
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)
    db_session.query(Spec).filter(Spec.id == spec.id).update({"project_id": project.id})
    db_session.commit()

    _enqueue(owner_id=user.id, case_id=case.id, run_id=run.id)
    claim = client.post("/agent/authoring/next", headers={"Authorization": f"Bearer {token}"})
    assert claim.status_code == 200, claim.text
    body = claim.json()

    assert body["project"] is not None, "no bundle ⇒ the device cannot verify the spec it authors"
    assert "files" in body["project"] and "baseVersion" in body["project"]
    # The verification must match the real run, so the headless setting is SENT,
    # not guessed: a headless run can fail a bot-protected app whose spec is fine.
    assert "headless" in body


def test_a_case_with_no_project_still_claims(client, db_session):
    """#657: verification is a bonus, never a gate.

    A legacy case with no automation project must still be authorable — the agent
    reports it as unverified rather than refusing the work.
    """
    user = _make_user(db_session, "no-project@example.com")
    token = _pair_device(db_session, user)
    run, case, _spec = _seed_case(db_session, owner_id=user.id)

    _enqueue(owner_id=user.id, case_id=case.id, run_id=run.id)
    body = client.post(
        "/agent/authoring/next", headers={"Authorization": f"Bearer {token}"}
    ).json()

    assert body["project"] is None
    assert body["specFilename"], "the claim itself must still be complete"
