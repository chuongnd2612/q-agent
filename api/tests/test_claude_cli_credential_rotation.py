"""The CLI end of #682: rotations are captured, and a dead credential says so.

`hub_credentials` owns *what* is posted to the hub (see
`test_hub_claude_credentials.py`); this file owns the two things `claude_cli`
must do around every call:

1. **Capture the rotation.** The local `persist_refreshed` write-back resolves a
   local `claude_credentials` row, of which hub-data mode has none — so on its own
   it is a silent no-op and the rotation is lost, taking the hub's copy with it.
2. **Say what is wrong.** A dead credential exits 1 with its reason in the JSON
   envelope on *stdout*, and handing that envelope to the SPA is what rendered as
   the bare `Request failed (HTTP 502)` the bug was reported as.
"""

from __future__ import annotations

import json

import pytest

from app.services import claude_cli
from tests.conftest import FakePopen

#: What the CLI actually writes when the credential is dead — copied from the
#: server log in #682, trimmed to the fields `run_prompt` reads.
NOT_LOGGED_IN = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "result": "Not logged in - Please run /login",
    }
)


@pytest.fixture
def _cli(monkeypatch, workspace_dir):
    """`run_prompt` with the subprocess and the credential resolution stubbed."""
    monkeypatch.setattr(claude_cli, "_resolve_claude_env", lambda: ({}, 7))
    monkeypatch.setattr(claude_cli, "_persist_refreshed_credential", lambda owner_id: None)
    monkeypatch.setattr(claude_cli, "_mark_credential_invalid", lambda owner_id: None)


def _popen(returncode: int, stdout: str):
    return lambda *a, **k: FakePopen(returncode=returncode, stdout=stdout)


def test_a_rotation_is_captured_for_the_hub_after_every_call(_cli, monkeypatch):
    """#682: the CLI rotates the token in place; nothing else notices."""
    monkeypatch.setattr(
        claude_cli.subprocess,
        "Popen",
        _popen(0, json.dumps({"type": "result", "result": "hello"})),
    )
    captured: list[int] = []
    import app.services.hub_credentials as hub_credentials

    monkeypatch.setattr(
        hub_credentials, "capture_rotated_credential", lambda run_id: captured.append(run_id)
    )

    from app.services import run_context

    run_context.set_run(30)
    try:
        assert claude_cli.run_prompt("hi") == "hello"
    finally:
        run_context.set_run(None)

    assert captured == [30], "the rotation was never offered to the hub"


def test_a_call_with_no_run_captures_nothing(_cli, monkeypatch):
    """Only a run has a hub-resolved dir and a grant to post it with."""
    monkeypatch.setattr(
        claude_cli.subprocess,
        "Popen",
        _popen(0, json.dumps({"type": "result", "result": "hello"})),
    )
    captured: list[int] = []
    import app.services.hub_credentials as hub_credentials

    monkeypatch.setattr(
        hub_credentials, "capture_rotated_credential", lambda run_id: captured.append(run_id)
    )

    from app.services import run_context

    run_context.set_run(None)
    assert claude_cli.run_prompt("hi") == "hello"
    assert captured == []


def test_a_failed_capture_never_fails_the_call(_cli, monkeypatch):
    """Credential bookkeeping is not the work — #682's guard, not its feature."""
    monkeypatch.setattr(
        claude_cli.subprocess,
        "Popen",
        _popen(0, json.dumps({"type": "result", "result": "hello"})),
    )
    import app.services.hub_credentials as hub_credentials

    def boom(run_id):
        raise RuntimeError("hub exploded")

    monkeypatch.setattr(hub_credentials, "capture_rotated_credential", boom)

    from app.services import run_context

    run_context.set_run(30)
    try:
        assert claude_cli.run_prompt("hi") == "hello"
    finally:
        run_context.set_run(None)


def test_a_dead_credential_says_what_to_do_instead_of_leaking_the_envelope(_cli, monkeypatch):
    """#682: the SPA showed `Request failed (HTTP 502)` and nothing else.

    The reason was in the JSON envelope all along; it just was not turned into a
    sentence anyone could act on.
    """
    monkeypatch.setattr(claude_cli.subprocess, "Popen", _popen(1, NOT_LOGGED_IN))

    with pytest.raises(claude_cli.ClaudeError) as excinfo:
        claude_cli.run_prompt("hi")

    message = str(excinfo.value)
    assert "not logged in" in message.lower()
    assert "Settings" in message, "no advice, so the user is where they started"
    # The raw envelope is noise to a user and must not be the whole message.
    assert '"is_error"' not in message


def test_a_hub_resolved_run_is_told_to_fix_it_in_emehub(_cli, monkeypatch, workspace_dir):
    """Where the credential lives decides the advice.

    Telling someone to update Q-Agent's Settings is wrong — and unactionable —
    when the account is EmeHub's to change.
    """
    monkeypatch.setattr(claude_cli.subprocess, "Popen", _popen(1, NOT_LOGGED_IN))
    from app.services import claude_credentials, run_context

    claude_credentials.materialize_raw(
        json.dumps({"claudeAiOauth": {"accessToken": "x", "expiresAt": 1}}),
        claude_credentials.hub_run_key(30),
    )

    run_context.set_run(30)
    try:
        with pytest.raises(claude_cli.ClaudeError) as excinfo:
            claude_cli.run_prompt("hi")
    finally:
        run_context.set_run(None)

    assert "EmeHub" in str(excinfo.value)
    assert "Settings" not in str(excinfo.value)


def test_an_unrelated_failure_still_reports_its_own_output(_cli, monkeypatch):
    """Only auth failures get the credential advice — a rate limit is not one.

    Replacing every message would hide the actual cause of everything else.
    """
    envelope = json.dumps({"type": "result", "is_error": True, "result": "Rate limit exceeded"})
    monkeypatch.setattr(claude_cli.subprocess, "Popen", _popen(1, envelope))

    with pytest.raises(claude_cli.ClaudeError) as excinfo:
        claude_cli.run_prompt("hi")

    assert "Rate limit exceeded" in str(excinfo.value)
