"""The AI chip tells the truth about the credential (#736).

It reported **Operational** while every Claude call was failing with
``Not logged in · Please run /login``, because both signals behind it answer a
different question than a reader takes them for:

* ``is_available()`` runs ``claude --version``; its own docstring says *"does not verify
  auth"*. It means the binary is installed, which it always is.
* the credential's ``status`` field is the **store's claim** about its row — the hub
  reports ``active``/``refreshable`` because a row exists with a refresh token, which
  says nothing about whether the material still authenticates.

So the missing signal is the only authoritative one: what happened on the last real
call. And it is the only one that works in hub-data mode at all —
``_mark_credential_invalid`` flags the LOCAL row, and in hub mode there is none, so the
existing failure path was a silent no-op exactly where the credential lives.
"""

from __future__ import annotations

import json

from app.services import claude_health, claude_usage_reader


def test_a_workspace_that_has_never_called_claude_is_not_warned(workspace_dir):
    """Absence of evidence is not evidence of a problem — a fresh install must not
    warn about a credential it has not tried."""
    assert claude_health.status()["ok"] is True
    assert claude_health.status()["at"] is None


def test_a_rejection_is_recorded_and_survives_a_restart(workspace_dir):
    """File-backed on purpose: the chip is polled by a process that restarts, and a
    health signal that forgets on restart reports healthy every time it matters."""
    claude_health.record_auth_failure("Not logged in · Please run /login")

    status = claude_health.status()
    assert status["ok"] is False
    assert "Not logged in" in status["detail"]
    assert status["at"]
    # Re-read from disk rather than a module global.
    assert json.loads((workspace_dir / "claude-health.json").read_text())["ok"] is False


def test_a_successful_call_clears_an_earlier_rejection(workspace_dir):
    """Otherwise the first bad token pins an amber warning on the chip forever, and a
    warning that never clears is a warning nobody reads."""
    claude_health.record_auth_failure("rejected")

    claude_health.record_success()

    assert claude_health.status()["ok"] is True
    assert claude_health.status()["detail"] == ""


def test_the_detail_is_bounded(workspace_dir):
    """It comes from CLI output and ends up in a popover."""
    claude_health.record_auth_failure("x" * 5000)

    assert len(claude_health.status()["detail"]) <= 300


def test_stats_reports_the_credential_separately_from_the_binary(workspace_dir):
    """Two questions, two fields. "No CLI" and "CLI fine, credential rejected" need
    different advice, so folding them into one flag would make the chip unable to say
    which one it is."""
    claude_health.record_auth_failure("Not logged in")
    claude_usage_reader._cache = None  # the reader caches for 60s

    stats = claude_usage_reader.read_stats()

    assert stats["credentialOk"] is False
    assert "Not logged in" in stats["credentialDetail"]
    # `operational` still answers its own question.
    assert "operational" in stats


def test_a_healthy_credential_leaves_stats_clean(workspace_dir):
    claude_health.record_success()
    claude_usage_reader._cache = None

    stats = claude_usage_reader.read_stats()

    assert stats["credentialOk"] is True
    assert stats["credentialDetail"] == ""
