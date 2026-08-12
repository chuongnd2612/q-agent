"""Tests for Claude CLI credentials management + per-user cost attribution (#95).

Covers: effective-credential resolution (own > shared > none), that
``CLAUDE_CONFIG_DIR`` is set to the right per-user/shared dir in the actual
``claude_cli.run_prompt`` invocation (subprocess mocked), that missing
credentials raise a clean :class:`ClaudeError`, that usage rows are stamped
with the right ``owner_id``, and the HTTP surface (status/upload/delete,
admin-only shared). Never asserts on plaintext tokens in responses.
"""

from __future__ import annotations

import json

import pytest
from conftest import FakePopen

from app.services import claude_cli, claude_credentials

OWN_JSON = '{"token": "own-secret-token"}'
SHARED_JSON = '{"token": "shared-secret-token"}'


def _make_user(db_session, email, password, role="member"):
    from app.models.user import User
    from app.services import auth_service

    user = User(
        email=email.strip().lower(),
        first_name="Test",
        last_name="User",
        role=role,
        password_hash=auth_service.hash_password(password),
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _login(client, email, password):
    return client.post("/auth/login", json={"email": email, "password": password})


def _fake_popen(returncode=0, result="ok", usage=None):
    envelope = {"result": result}
    if usage is not None:
        envelope["usage"] = usage
        envelope["total_cost_usd"] = 0.01
    return lambda *a, **k: FakePopen(
        returncode=returncode, stdout=json.dumps(envelope), stderr=""
    )


# ------------------------------------------------------------- metadata parsing
RICH_JSON = json.dumps(
    {
        "claudeAiOauth": {
            "accessToken": "secret-access",
            "refreshToken": "secret-refresh",
            "expiresAt": 1780000000000,  # epoch ms
            "scopes": ["user:inference", "user:profile"],
            "subscriptionType": "max",
        }
    }
)


def test_upsert_extracts_metadata_from_nested_oauth_object(db_session):
    from datetime import datetime, timezone

    row = claude_credentials.upsert_own(db_session, 1, RICH_JSON)

    assert row.expires_at == datetime.fromtimestamp(1780000000000 / 1000, tz=timezone.utc)
    assert row.scopes == ["user:inference", "user:profile"]
    assert row.subscription_type == "max"


def test_upsert_extracts_metadata_from_top_level_fallback(db_session):
    """No ``claudeAiOauth`` wrapper — falls back to top-level keys."""
    flat_json = json.dumps({"expiresAt": 1780000000000, "scopes": ["user:inference"], "subscriptionType": "pro"})

    row = claude_credentials.upsert_shared(db_session, flat_json)

    assert row.expires_at is not None
    assert row.scopes == ["user:inference"]
    assert row.subscription_type == "pro"


def test_upsert_metadata_missing_yields_null_not_error(db_session):
    """No metadata present at all -> every extracted field is None, no raise."""
    row = claude_credentials.upsert_own(db_session, 2, OWN_JSON)

    assert row.expires_at is None
    assert row.scopes is None
    assert row.subscription_type is None


def test_upsert_metadata_ignores_malformed_types(db_session):
    """Wrong-typed fields (not a number/list/str) are defensively dropped, not raised."""
    weird_json = json.dumps(
        {"claudeAiOauth": {"expiresAt": "not-a-number", "scopes": "not-a-list", "subscriptionType": 42}}
    )

    row = claude_credentials.upsert_own(db_session, 3, weird_json)

    assert row.expires_at is None
    assert row.scopes is None
    assert row.subscription_type is None


# ------------------------------------------------------- resolution precedence
def test_resolve_effective_prefers_own_over_shared(db_session):
    claude_credentials.upsert_own(db_session, 1, OWN_JSON)
    claude_credentials.upsert_shared(db_session, SHARED_JSON)

    config_dir = claude_credentials.resolve_effective_config_dir(db_session, 1)

    assert config_dir is not None
    assert config_dir.name == "1"
    stored = (config_dir / ".credentials.json").read_text(encoding="utf-8")
    assert stored == OWN_JSON


def test_resolve_effective_falls_back_to_shared(db_session):
    claude_credentials.upsert_shared(db_session, SHARED_JSON)

    config_dir = claude_credentials.resolve_effective_config_dir(db_session, 42)

    assert config_dir is not None
    assert config_dir.name == "shared"
    stored = (config_dir / ".credentials.json").read_text(encoding="utf-8")
    assert stored == SHARED_JSON


def test_resolve_effective_none_when_nothing_configured(db_session):
    assert claude_credentials.resolve_effective_config_dir(db_session, 7) is None


def test_prefer_shared_switches_effective_to_shared_without_deleting_own(db_session):
    """prefer_shared on the own row makes shared effective, but keeps own on file."""
    claude_credentials.upsert_own(db_session, 1, OWN_JSON)
    claude_credentials.upsert_shared(db_session, SHARED_JSON)

    claude_credentials.set_preferred_mode(db_session, 1, "shared")

    config_dir = claude_credentials.resolve_effective_config_dir(db_session, 1)
    assert config_dir is not None and config_dir.name == "shared"
    assert (config_dir / ".credentials.json").read_text(encoding="utf-8") == SHARED_JSON
    # own credential is untouched — the user can flip back without re-uploading.
    assert claude_credentials.get_own(db_session, 1) is not None
    assert claude_credentials.status_for(db_session, 1)["mode"] == "shared"

    # Flip back to own.
    claude_credentials.set_preferred_mode(db_session, 1, "own")
    back = claude_credentials.resolve_effective_config_dir(db_session, 1)
    assert back is not None and back.name == "1"
    assert claude_credentials.status_for(db_session, 1)["mode"] == "own"


def test_prefer_shared_ignored_when_no_shared_configured(db_session):
    """A prefer-shared flag with no shared credential must not strand the user."""
    row = claude_credentials.upsert_own(db_session, 1, OWN_JSON)
    row.prefer_shared = True
    db_session.commit()

    config_dir = claude_credentials.resolve_effective_config_dir(db_session, 1)
    assert config_dir is not None and config_dir.name == "1"
    assert claude_credentials.status_for(db_session, 1)["mode"] == "own"


def test_set_preferred_mode_requires_own_credential(db_session):
    claude_credentials.upsert_shared(db_session, SHARED_JSON)
    with pytest.raises(claude_credentials.ClaudeCredentialsError):
        claude_credentials.set_preferred_mode(db_session, 1, "shared")


def test_set_preferred_mode_shared_requires_shared_credential(db_session):
    claude_credentials.upsert_own(db_session, 1, OWN_JSON)
    with pytest.raises(claude_credentials.ClaudeCredentialsError):
        claude_credentials.set_preferred_mode(db_session, 1, "shared")


def test_set_preferred_mode_rejects_unknown_mode(db_session):
    claude_credentials.upsert_own(db_session, 1, OWN_JSON)
    with pytest.raises(claude_credentials.ClaudeCredentialsError):
        claude_credentials.set_preferred_mode(db_session, 1, "bogus")


# ------------------------------------------------------------- claude_cli wiring
def test_missing_credentials_raises_clean_error(monkeypatch, workspace_dir):
    """No own/shared credential at all -> ClaudeError, subprocess never invoked."""
    called = []
    monkeypatch.setattr(claude_cli.subprocess, "Popen", lambda *a, **k: called.append(1))

    with pytest.raises(claude_cli.ClaudeError, match="No Claude credentials configured"):
        claude_cli.run_prompt("hi", label="Test call")

    assert called == []  # never reached the subprocess


def test_run_prompt_sets_claude_config_dir_for_own_credential(monkeypatch, db_session):
    """An ambient run owned by user 5 -> CLAUDE_CONFIG_DIR points at that user's dir."""
    from app.models.run import Run
    from app.services import run_context

    claude_credentials.upsert_own(db_session, 5, OWN_JSON)
    run = Run(code="RUN-500", name="test", owner_id=5)
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        return FakePopen(returncode=0, stdout=json.dumps({"result": "ok"}), stderr="")

    monkeypatch.setattr(claude_cli.subprocess, "Popen", fake_popen)

    run_context.set_run(run.id)
    try:
        out = claude_cli.run_prompt("hi", label="Owned call")
    finally:
        run_context.clear()

    assert out == "ok"
    config_dir = captured["env"]["CLAUDE_CONFIG_DIR"]
    assert config_dir.replace("\\", "/").endswith("claude-config/5")


def test_run_prompt_falls_back_to_shared_config_dir(monkeypatch, shared_claude_credential):
    """No ambient run (no owner resolvable) -> falls back to the shared credential dir."""
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        return FakePopen(returncode=0, stdout=json.dumps({"result": "ok"}), stderr="")

    monkeypatch.setattr(claude_cli.subprocess, "Popen", fake_popen)

    claude_cli.run_prompt("hi", label="Unowned call")

    config_dir = captured["env"]["CLAUDE_CONFIG_DIR"]
    assert config_dir.replace("\\", "/").endswith("claude-config/shared")


# --------------------------------------------------------------- usage attribution
def test_usage_records_under_the_right_owner(monkeypatch, db_session):
    from app.models.claude_usage import ClaudeUsage
    from app.models.run import Run
    from app.services import run_context

    claude_credentials.upsert_own(db_session, 9, OWN_JSON)
    run = Run(code="RUN-900", name="test", owner_id=9)
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    usage = {"input_tokens": 10, "output_tokens": 20}
    monkeypatch.setattr(claude_cli.subprocess, "Popen", _fake_popen(usage=usage))

    run_context.set_run(run.id)
    try:
        claude_cli.run_prompt("hi", label="Owned usage call")
    finally:
        run_context.clear()

    row = db_session.query(ClaudeUsage).filter(ClaudeUsage.action == "Owned usage call").first()
    assert row is not None
    assert row.owner_id == 9
    assert row.input_tokens == 10
    assert row.output_tokens == 20


def test_usage_records_with_no_owner_when_using_shared(monkeypatch, shared_claude_credential):
    from app.models.claude_usage import ClaudeUsage

    monkeypatch.setattr(claude_cli.subprocess, "Popen", _fake_popen(usage={"input_tokens": 1}))

    claude_cli.run_prompt("hi", label="Shared usage call")

    row = (
        shared_claude_credential.query(ClaudeUsage)
        .filter(ClaudeUsage.action == "Shared usage call")
        .first()
    )
    assert row is not None
    assert row.owner_id is None


# ------------------------------------------------------------------------- HTTP
def test_status_reports_none_when_unconfigured(client, db_session):
    r = client.get("/ai/credentials")
    assert r.status_code == 200
    body = r.json()
    # Field-wise, not an exact-dict `==`: the payload legitimately gains keys
    # (see test_upload_and_delete_own_credentials) and this test is about
    # "nothing is configured", not the endpoint's exact key set.
    assert body["hasOwn"] is False
    assert body["hasShared"] is False
    assert body["mode"] == "none"
    assert body["own"] is None
    assert body["shared"] is None


def test_upload_and_delete_own_credentials(client, db_session):
    """PUT then DELETE /ai/credentials round-trips, scoped to the caller.

    Asserted field-wise on purpose. This used to compare ``status["own"]``
    against a whole literal dict with ``==``, which broke the moment the
    endpoint grew ``status`` / ``accountEmail`` / ``accountOrg`` — a test that
    fails on *correct* changes. Deletion is verified against the store (the row
    is gone) and against a second user's row (it is not), so it cannot pass if
    DELETE silently no-ops or over-deletes.
    """
    from app import crypto

    owner = _make_user(db_session, "own@example.com", "password123")
    other = _make_user(db_session, "other@example.com", "password123")
    token = _login(client, "own@example.com", "password123").json()["accessToken"]
    headers = {"Authorization": f"Bearer {token}"}
    other_token = _login(client, "other@example.com", "password123").json()["accessToken"]
    other_headers = {"Authorization": f"Bearer {other_token}"}

    r = client.put("/ai/credentials", json={"credentials": OWN_JSON}, headers=headers)
    assert r.status_code == 200
    assert "own-secret-token" not in r.text  # never echoes the plaintext token back
    # A second user uploads their own, so delete-scoping is observable below.
    assert (
        client.put("/ai/credentials", json={"credentials": SHARED_JSON}, headers=other_headers).status_code
        == 200
    )

    status = client.get("/ai/credentials", headers=headers).json()
    assert status["hasOwn"] is True
    assert status["hasShared"] is False  # nobody uploaded an admin/shared credential
    assert status["mode"] == "own"
    assert "own-secret-token" not in json.dumps(status)  # nor on the status read
    own = status["own"]
    assert own is not None
    # OWN_JSON carries no OAuth metadata, so every extracted field is empty...
    assert own["subscriptionType"] is None
    assert own["expiresAt"] is None
    assert own["scopes"] == []
    # ...``assignedUsers`` is a shared-credential-only count...
    assert own["assignedUsers"] is None
    # ...and the upload stamped a refresh time.
    assert own["lastRefreshed"]
    assert status["shared"] is None

    # Owner scoping of the *write*: each user's row holds their own payload.
    db_session.expire_all()
    assert json.loads(crypto.decrypt(claude_credentials.get_own(db_session, owner.id).credentials)) == json.loads(OWN_JSON)
    assert json.loads(crypto.decrypt(claude_credentials.get_own(db_session, other.id).credentials)) == json.loads(SHARED_JSON)

    r = client.delete("/ai/credentials", headers=headers)
    assert r.status_code == 200

    status = client.get("/ai/credentials", headers=headers).json()
    assert status["hasOwn"] is False
    assert status["own"] is None
    assert status["mode"] == "none"  # nothing left to fall back to
    # Really deleted from the store, not merely omitted from the summary...
    db_session.expire_all()
    assert claude_credentials.get_own(db_session, owner.id) is None
    assert claude_credentials.resolve_effective_config_dir(db_session, owner.id) is None
    # ...and owner-scoped: the other user's credential is untouched.
    assert claude_credentials.get_own(db_session, other.id) is not None
    assert client.get("/ai/credentials", headers=other_headers).json()["hasOwn"] is True


def test_switch_credential_mode_endpoint(client, db_session):
    """PUT /ai/credentials/mode flips the effective mode without deleting own."""
    user = _make_user(db_session, "switch@example.com", "password123")
    token = _login(client, "switch@example.com", "password123").json()["accessToken"]
    headers = {"Authorization": f"Bearer {token}"}

    client.put("/ai/credentials", json={"credentials": OWN_JSON}, headers=headers)
    claude_credentials.upsert_shared(db_session, SHARED_JSON)
    assert client.get("/ai/credentials", headers=headers).json()["mode"] == "own"

    r = client.put("/ai/credentials/mode", json={"mode": "shared"}, headers=headers)
    assert r.status_code == 200
    status = client.get("/ai/credentials", headers=headers).json()
    assert status["mode"] == "shared"
    assert status["hasOwn"] is True  # kept on file

    r = client.put("/ai/credentials/mode", json={"mode": "own"}, headers=headers)
    assert r.status_code == 200
    assert client.get("/ai/credentials", headers=headers).json()["mode"] == "own"


def test_switch_credential_mode_to_shared_without_shared_400(client, db_session):
    _make_user(db_session, "noshared@example.com", "password123")
    token = _login(client, "noshared@example.com", "password123").json()["accessToken"]
    headers = {"Authorization": f"Bearer {token}"}
    client.put("/ai/credentials", json={"credentials": OWN_JSON}, headers=headers)

    r = client.put("/ai/credentials/mode", json={"mode": "shared"}, headers=headers)
    assert r.status_code == 400


def test_upload_own_rejects_malformed_json(client, db_session):
    _make_user(db_session, "bad@example.com", "password123")
    token = _login(client, "bad@example.com", "password123").json()["accessToken"]
    headers = {"Authorization": f"Bearer {token}"}

    r = client.put("/ai/credentials", json={"credentials": "not json"}, headers=headers)
    assert r.status_code == 400


def test_shared_credentials_require_admin(client, db_session):
    _make_user(db_session, "member@example.com", "password123", role="member")
    token = _login(client, "member@example.com", "password123").json()["accessToken"]
    headers = {"Authorization": f"Bearer {token}"}

    r = client.put("/ai/credentials/shared", json={"credentials": SHARED_JSON}, headers=headers)
    assert r.status_code == 403


def test_admin_user_list_reports_credential_source(client, db_session):
    """#95: ``GET /auth/users`` reports each user's effective credential
    source — "personal" (own upload), "shared" (falls back to the shared
    credential), or "none" (nothing resolves for them)."""
    admin = _make_user(db_session, "admin2@example.com", "password123", role="admin")
    personal_user = _make_user(db_session, "personal@example.com", "password123")
    fallback_user = _make_user(db_session, "fallback@example.com", "password123")

    claude_credentials.upsert_own(db_session, personal_user.id, OWN_JSON)
    claude_credentials.upsert_shared(db_session, SHARED_JSON)

    token = _login(client, "admin2@example.com", "password123").json()["accessToken"]
    headers = {"Authorization": f"Bearer {token}"}

    by_email = {u["email"]: u for u in client.get("/auth/users", headers=headers).json()}
    assert by_email["personal@example.com"]["credentialSource"] == "personal"
    assert by_email["fallback@example.com"]["credentialSource"] == "shared"
    assert by_email["admin2@example.com"]["credentialSource"] == "shared"


def test_admin_user_list_reports_no_credential_source_when_nothing_configured(client, db_session):
    _make_user(db_session, "admin3@example.com", "password123", role="admin")
    token = _login(client, "admin3@example.com", "password123").json()["accessToken"]
    headers = {"Authorization": f"Bearer {token}"}

    users = client.get("/auth/users", headers=headers).json()
    assert all(u["credentialSource"] == "none" for u in users)


def test_admin_can_manage_shared_credentials(client, db_session):
    _make_user(db_session, "admin@example.com", "password123", role="admin")
    token = _login(client, "admin@example.com", "password123").json()["accessToken"]
    headers = {"Authorization": f"Bearer {token}"}

    r = client.put("/ai/credentials/shared", json={"credentials": SHARED_JSON}, headers=headers)
    assert r.status_code == 200
    assert "shared-secret-token" not in r.text

    status = client.get("/ai/credentials", headers=headers).json()
    assert status["hasOwn"] is False
    assert status["hasShared"] is True
    assert status["mode"] == "shared"
    assert status["own"] is None
    # The one admin user has no own credential, so it falls back to shared.
    assert status["shared"]["assignedUsers"] == 1

    r = client.delete("/ai/credentials/shared", headers=headers)
    assert r.status_code == 200
    status = client.get("/ai/credentials", headers=headers).json()
    assert status["hasShared"] is False


# ---- persist_refreshed: capture the CLI's in-place token refresh (see the -----
# ---- "Not logged in" credential-lifecycle bug) -------------------------------


def _oauth_json(token: str, expires_ms: int) -> str:
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": token,
                "refreshToken": "refresh-token",
                "expiresAt": expires_ms,
                "scopes": ["user:inference"],
                "subscriptionType": "max",
            }
        }
    )


def _stored_token(db_session) -> str:
    from app import crypto

    row = claude_credentials.get_shared(db_session)
    return json.loads(crypto.decrypt(row.credentials))["claudeAiOauth"]["accessToken"]


def test_persist_refreshed_captures_newer_token(db_session):
    """A CLI refresh (newer expiresAt, non-empty token) is written back."""
    claude_credentials.upsert_shared(db_session, _oauth_json("old-token", 1000))
    cfg = claude_credentials.resolve_effective_config_dir(db_session, None)
    # Simulate the CLI refreshing the access token in-place.
    (cfg / ".credentials.json").write_text(_oauth_json("new-token", 5000), encoding="utf-8")

    assert claude_credentials.persist_refreshed(db_session, None) is True
    assert _stored_token(db_session) == "new-token"


def test_persist_refreshed_ignores_logged_out_file(db_session):
    """A failed refresh leaves empty tokens — it must never clobber the store."""
    claude_credentials.upsert_shared(db_session, _oauth_json("good-token", 5000))
    cfg = claude_credentials.resolve_effective_config_dir(db_session, None)
    (cfg / ".credentials.json").write_text(_oauth_json("", 0), encoding="utf-8")

    assert claude_credentials.persist_refreshed(db_session, None) is False
    assert _stored_token(db_session) == "good-token"


def test_persist_refreshed_ignores_non_newer_token(db_session):
    """An equal/older expiry is not a genuine refresh — leave the store as-is."""
    claude_credentials.upsert_shared(db_session, _oauth_json("good-token", 5000))
    cfg = claude_credentials.resolve_effective_config_dir(db_session, None)
    (cfg / ".credentials.json").write_text(_oauth_json("older-token", 4000), encoding="utf-8")

    assert claude_credentials.persist_refreshed(db_session, None) is False
    assert _stored_token(db_session) == "good-token"
