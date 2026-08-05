"""Dual-accept hub tokens + JIT provisioning — B2 of the hub SSO work (#479).

``docs/HUB-INTEGRATION.md`` §7 **Stage 2**, end to end: a hub token reaches an
authenticated route and resolves to the right local user; ``users.hub_user_id`` is
populated; a *second* login with the same hub ``sub`` reuses the row; an existing
local user's runs/evidence/workspace path are untouched; and a run WebSocket
connects with a hub-derived session.

Plus the traps from #479 that no other test would catch:

- **The global ``auth_guard`` HTTP middleware** (``app/main.py``) validates the
  bearer token *before any route dependency runs*. The rest of the suite runs with
  ``auth_required`` off, which makes the middleware a passthrough — so
  ``test_middleware_*`` below run with ``auth_required=True`` on purpose. Without
  them, the dual-accept decoder would look correct here and 401 on every real
  request.
- **``_token_user_id`` must resolve via ``hub_user_id``**, never by casting the hub
  ``sub`` to a local id, or a hub-authed user reads someone else's evidence.
- **``user._sid``** is a local ``auth_sessions`` id; a hub ``sid`` is not, so the
  session routes must never try to revoke one.
- **Role is a create-time seed only** — a hub token claiming ``admin`` must not
  escalate an existing local ``member``.

The decoder itself is covered by ``test_hub_tokens.py`` (Stage 1); ``mint`` is
imported from there so the two files cannot drift on the token shape.
"""

from __future__ import annotations

import pytest
from starlette.websockets import WebSocketDisconnect

from app.models.run import Run, RunTicket
from app.models.user import ROLE_ADMIN, ROLE_MEMBER, User
from app.services import auth_service
from app.services.workspace_scope import scope_for, scoped_evidence_dir
from tests.test_hub_tokens import HUB_SECRET, mint


@pytest.fixture
def hub_on(workspace_dir, monkeypatch):
    """Switch the integration on and configure the shared secret.

    Depends on ``workspace_dir`` so it runs *after* the settings singleton has
    been rebuilt for this test — otherwise the rebuild would wipe the flag.
    """
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "hub_sso_enabled", True)
    monkeypatch.setattr(config_module.settings, "hub_jwt_secret", HUB_SECRET)
    monkeypatch.setattr(config_module.settings, "hub_audience", "qagent")
    return config_module.settings


@pytest.fixture
def hub_off(workspace_dir, monkeypatch):
    """The default posture: secret configured, flag OFF. Hub tokens must be rejected."""
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "hub_sso_enabled", False)
    monkeypatch.setattr(config_module.settings, "hub_jwt_secret", HUB_SECRET)
    monkeypatch.setattr(config_module.settings, "hub_audience", "qagent")
    return config_module.settings


@pytest.fixture
def auth_on(monkeypatch):
    """Turn the global HTTP auth guard on — the middleware stops being a passthrough."""
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "auth_required", True)
    yield


def _hub_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _local_user(db_session, email: str, *, role: str = ROLE_MEMBER) -> User:
    user = User(
        email=email.strip().lower(),
        first_name="Local",
        last_name="User",
        role=role,
        password_hash=auth_service.hash_password("password123"),
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _local_token(user: User) -> str:
    return auth_service.create_access_token(user, sid=f"sid-{user.id}")


def _owned_run(db_session, owner: User, code: str = "RUN-900") -> Run:
    run = Run(code=code, name="Owned run", scope="selected", status="done", owner_id=owner.id)
    db_session.add(run)
    db_session.flush()
    db_session.add(RunTicket(run_id=run.id, ticket_external_id="SUR-1", position=0))
    db_session.commit()
    db_session.refresh(run)
    return run


def _local_session(db_session, user: User, sid: str):
    """A real ``auth_sessions`` row with a chosen id (``create_session`` picks a uuid)."""
    from datetime import timedelta

    from app.db import utcnow
    from app.models.session import Session as AuthSession

    session = AuthSession(
        id=sid,
        user_id=user.id,
        refresh_token_hash="0" * 64,
        expires_at=utcnow() + timedelta(days=1),
    )
    db_session.add(session)
    db_session.commit()
    return session


def _fetch(db_session, hub_user_id: str) -> User | None:
    db_session.expire_all()  # the guard/JIT path commits on its own session
    return db_session.query(User).filter(User.hub_user_id == hub_user_id).first()


# ------------------------------------------------- Stage 2: reach a real route
def test_hub_token_authenticates_and_provisions_a_local_user(client, db_session, hub_on):
    r = client.get("/auth/me", headers=_hub_headers(mint(sub="42", email="new.hub@emesoft.net")))

    assert r.status_code == 200
    assert r.json()["email"] == "new.hub@emesoft.net"
    provisioned = _fetch(db_session, "42")
    assert provisioned is not None
    assert provisioned.email == "new.hub@emesoft.net"
    # `sub` is a HUB id — it must live in hub_user_id, never as the local id.
    assert provisioned.hub_user_id == "42"
    assert provisioned.id == r.json()["id"] != 42
    assert provisioned.password_hash == ""  # no local password to guess


def test_second_login_with_the_same_sub_reuses_the_row(client, db_session, hub_on):
    first = client.get("/auth/me", headers=_hub_headers(mint(sub="7", email="repeat@emesoft.net")))
    second = client.get("/auth/me", headers=_hub_headers(mint(sub="7", email="repeat@emesoft.net")))

    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    db_session.expire_all()
    assert db_session.query(User).filter(User.hub_user_id == "7").count() == 1


def test_role_claim_seeds_a_brand_new_user(client, db_session, hub_on):
    """The claim is a sensible default for accounts that didn't exist yet (§8.2)."""
    client.get("/auth/me", headers=_hub_headers(mint(sub="11", email="fresh.admin@emesoft.net")))

    assert _fetch(db_session, "11").role == ROLE_ADMIN


def test_unknown_role_claim_falls_back_to_member(client, db_session, hub_on):
    client.get("/auth/me", headers=_hub_headers(mint(sub="12", email="odd@emesoft.net", role="root")))

    assert _fetch(db_session, "12").role == ROLE_MEMBER


def test_hub_token_with_no_local_account_does_not_leak_a_local_id(client, db_session, hub_on):
    """A hub `sub` that happens to match an existing local `users.id` must not
    resolve to that user — the whole point of the mapping column (§3.1)."""
    victim = _local_user(db_session, "victim@example.com")

    r = client.get("/auth/me", headers=_hub_headers(mint(sub=str(victim.id), email="attacker@evil.test")))

    assert r.status_code == 200
    assert r.json()["email"] == "attacker@evil.test"
    db_session.expire_all()
    db_session.refresh(victim)
    assert victim.hub_user_id is None


# ------------------------------------------------- email collision → auto-link
def test_email_collision_links_the_existing_row(client, db_session, hub_on):
    existing = _local_user(db_session, "duna.nguyen@emesoft.net")

    r = client.get("/auth/me", headers=_hub_headers(mint(sub="3", email="Duna.Nguyen@EmeSoft.net")))

    assert r.status_code == 200
    assert r.json()["id"] == existing.id  # linked, not duplicated
    db_session.expire_all()
    db_session.refresh(existing)
    assert existing.hub_user_id == "3"
    assert db_session.query(User).filter(User.email == "duna.nguyen@emesoft.net").count() == 1


def test_email_collision_writes_an_audit_entry(client, db_session, hub_on):
    """§8.1 accepted the auto-link risk *on condition* that every link is audited."""
    from app.models.audit import AuditLog

    _local_user(db_session, "linkme@emesoft.net")

    client.get("/auth/me", headers=_hub_headers(mint(sub="55", email="linkme@emesoft.net")))

    db_session.expire_all()
    events = db_session.query(AuditLog).filter(AuditLog.category == "auth").all()
    linked = [e for e in events if "EmeHub" in e.action and e.target == "linkme@emesoft.net"]
    assert len(linked) == 1
    assert "55" in linked[0].meta


def test_role_claim_never_escalates_an_existing_local_user(client, db_session, hub_on):
    """The trap in §8.2: a find-or-create that *refreshes* attributes would make a
    hub-side role change silently grant admin here. Local role stays authoritative."""
    member = _local_user(db_session, "member@emesoft.net", role=ROLE_MEMBER)

    r = client.get("/auth/me", headers=_hub_headers(mint(sub="66", email="member@emesoft.net", role="admin")))

    assert r.status_code == 200
    assert r.json()["role"] == ROLE_MEMBER
    db_session.expire_all()
    db_session.refresh(member)
    assert member.role == ROLE_MEMBER
    assert member.hub_user_id == "66"


def test_role_is_not_refreshed_on_repeat_login_after_local_promotion(client, db_session, hub_on):
    """Same rule the other way round: a local promotion survives a hub token that
    still claims `member`."""
    user = _local_user(db_session, "promoted@emesoft.net", role=ROLE_MEMBER)
    client.get("/auth/me", headers=_hub_headers(mint(sub="67", email="promoted@emesoft.net", role="member")))
    db_session.expire_all()
    db_session.refresh(user)
    user.role = ROLE_ADMIN
    db_session.commit()

    r = client.get("/auth/me", headers=_hub_headers(mint(sub="67", email="promoted@emesoft.net", role="member")))

    assert r.json()["role"] == ROLE_ADMIN


def test_inactive_local_user_is_rejected(client, db_session, hub_on):
    user = _local_user(db_session, "disabled@emesoft.net")
    user.is_active = False
    db_session.commit()

    r = client.get("/auth/me", headers=_hub_headers(mint(sub="68", email="disabled@emesoft.net")))

    assert r.status_code == 401


# ------------------------------------------------- the flag gates everything
def test_hub_token_is_rejected_with_the_flag_off(client, db_session, hub_off):
    r = client.get("/auth/me", headers=_hub_headers(mint(sub="99", email="nope@emesoft.net")))

    assert r.status_code == 401
    assert _fetch(db_session, "99") is None  # and nothing was provisioned


def test_invalid_hub_token_is_rejected_with_the_flag_on(client, hub_on):
    """Flag on is not "accept anything shaped like a hub token"."""
    assert client.get("/auth/me", headers=_hub_headers(mint(secret="a-wrong-secret-0123456789abcdef"))).status_code == 401
    assert client.get("/auth/me", headers=_hub_headers(mint(aud="dagent"))).status_code == 401


# ------------------------------------------------- THE TRAP: HTTP middleware
def test_middleware_accepts_a_hub_token_when_auth_is_required(client, db_session, auth_on, hub_on):
    """#479's critical trap. ``auth_guard`` in ``app/main.py`` 401s on an
    unrecognised bearer token *before* ``require_user`` runs, so dual-accept in the
    dependency alone would fail every real request. This is the only test in the
    suite that exercises the middleware with a hub token, because it's the only
    one that turns ``auth_required`` on.
    """
    headers = _hub_headers(mint(sub="21", email="guarded@emesoft.net"))

    # A route with no auth dependency at all: reaching it proves the *middleware*
    # accepted the token, not the dependency.
    assert client.get("/audit/stats", headers=headers).status_code == 200
    # And one behind `require_user`, which is where the local user is resolved.
    assert client.get("/auth/me", headers=headers).json()["email"] == "guarded@emesoft.net"
    assert _fetch(db_session, "21") is not None


def test_middleware_rejects_a_hub_token_when_the_flag_is_off(client, auth_on, hub_off):
    r = client.get("/audit/stats", headers=_hub_headers(mint(sub="22", email="guarded@emesoft.net")))

    assert r.status_code == 401


def test_middleware_still_rejects_a_tokenless_request(client, auth_on, hub_on):
    assert client.get("/audit/stats").status_code == 401


def test_local_token_still_passes_the_middleware_with_the_flag_on(client, db_session, auth_on, hub_on):
    """Non-regression: dual-accept must not disturb Q-Agent's own tokens."""
    user = _local_user(db_session, "local@example.com")

    r = client.get("/audit/stats", headers=_hub_headers(_local_token(user)))

    assert r.status_code == 200


# ------------------------------------------------- existing data stays intact
def test_linked_users_runs_and_workspace_path_are_untouched(client, db_session, auth_on, hub_on):
    """The §3.1 promise: local ids don't move, so every owned row and the
    ``users/<owner_id>`` workspace path keep working after linking."""
    owner = _local_user(db_session, "owner@emesoft.net")
    run = _owned_run(db_session, owner, code="RUN-901")
    expected_scope = scope_for(owner.id)
    headers = _hub_headers(mint(sub="31", email="owner@emesoft.net"))

    assert client.get(f"/runs/{run.id}", headers=headers).status_code == 200
    assert [r["id"] for r in client.get("/runs", headers=headers).json()] == [run.id]
    db_session.expire_all()
    db_session.refresh(run)
    db_session.refresh(owner)
    assert run.owner_id == owner.id
    assert scope_for(owner.id) == expected_scope


def test_hub_user_cannot_read_another_users_artifacts(client, db_session, auth_on, hub_on):
    """``_token_user_id`` resolves a hub token through ``hub_user_id``; if it cast
    ``sub`` to a local id instead, this is the test that catches it."""
    owner = _local_user(db_session, "art-owner@emesoft.net")
    intruder = _local_user(db_session, "art-intruder@emesoft.net")
    run = _owned_run(db_session, owner, code="RUN-902")

    evidence_dir = scoped_evidence_dir(owner.id) / run.code
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "shot.png").write_bytes(b"fake-png")
    url = f"/artifacts/{scope_for(owner.id)}/evidence/{run.code}/shot.png"

    owner_token = mint(sub="32", email="art-owner@emesoft.net")
    intruder_token = mint(sub=str(owner.id), email="art-intruder@emesoft.net")

    assert client.get(url, params={"token": owner_token}).status_code == 200
    # The intruder's hub `sub` is literally the owner's local id — still 404.
    assert client.get(url, params={"token": intruder_token}).status_code == 404
    assert intruder.id != owner.id


# ------------------------------------------------- run WebSocket
def test_run_ws_connects_with_a_hub_derived_session(client, db_session, auth_on, hub_on):
    owner = _local_user(db_session, "ws-owner@emesoft.net")
    run = _owned_run(db_session, owner, code="RUN-903")
    token = mint(sub="41", email="ws-owner@emesoft.net")

    with client.websocket_connect(f"/ws/runs/{run.id}?token={token}"):
        pass  # connecting without a 1008 close is the assertion


def test_run_ws_rejects_a_hub_user_who_does_not_own_the_run(client, db_session, auth_on, hub_on):
    owner = _local_user(db_session, "ws-owner2@emesoft.net")
    run = _owned_run(db_session, owner, code="RUN-904")
    other = mint(sub="42", email="ws-other@emesoft.net")

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/runs/{run.id}?token={other}"):
            pass


def test_ai_ws_accepts_a_hub_token(client, auth_on, hub_on):
    with client.websocket_connect(f"/ws/ai?token={mint(sub='43', email='ai-ws@emesoft.net')}"):
        pass


def test_run_ws_rejects_a_hub_token_with_the_flag_off(client, db_session, auth_on, hub_off):
    owner = _local_user(db_session, "ws-owner3@emesoft.net")
    run = _owned_run(db_session, owner, code="RUN-905")
    token = mint(sub="44", email="ws-owner3@emesoft.net")

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/runs/{run.id}?token={token}"):
            pass


# ------------------------------------------------- the `_sid` trap
def test_hub_sid_is_never_treated_as_a_local_session_id(client, db_session, hub_on):
    """A hub ``sid`` must not reach ``auth_sessions``. Logout with a hub token
    succeeds (it clears cookies) without revoking anything, and no session row
    named after the hub sid is touched."""
    from app.models.session import Session as AuthSession

    user = _local_user(db_session, "sid@emesoft.net")
    _local_session(db_session, user, "3a7e1f")

    r = client.post("/auth/logout", headers=_hub_headers(mint(sub="51", email="sid@emesoft.net", sid="3a7e1f")))

    assert r.status_code == 200
    db_session.expire_all()
    kept = db_session.get(AuthSession, "3a7e1f")
    assert kept is not None and kept.revoked_at is None


def test_hub_authed_request_cannot_revoke_local_sessions(client, db_session, hub_on):
    """``revoke-others`` with an empty ``keep_sid`` would wipe every local session,
    so a hub-authenticated caller is refused instead."""
    from app.models.session import Session as AuthSession

    user = _local_user(db_session, "revoke@emesoft.net")
    _local_session(db_session, user, "local-sid-1")

    r = client.post(
        "/auth/sessions/revoke-others",
        headers=_hub_headers(mint(sub="52", email="revoke@emesoft.net")),
    )

    assert r.status_code == 400
    db_session.expire_all()
    assert db_session.get(AuthSession, "local-sid-1").revoked_at is None


def test_hub_authed_session_list_marks_nothing_current(client, db_session, hub_on):
    user = _local_user(db_session, "list@emesoft.net")
    _local_session(db_session, user, "3a7e1f")

    r = client.get("/auth/sessions", headers=_hub_headers(mint(sub="53", email="list@emesoft.net", sid="3a7e1f")))

    assert r.status_code == 200
    assert [s["current"] for s in r.json()] == [False]


def test_local_session_routes_are_unaffected(client, db_session, hub_on):
    """Non-regression for the ``_sid`` guard: a local token still marks its own
    session current and can still revoke the others."""
    from app.models.session import Session as AuthSession

    user = _local_user(db_session, "locals@emesoft.net")
    _local_session(db_session, user, f"sid-{user.id}")
    _local_session(db_session, user, "other-sid")
    headers = _hub_headers(_local_token(user))

    listed = client.get("/auth/sessions", headers=headers).json()
    assert {s["id"]: s["current"] for s in listed}[f"sid-{user.id}"] is True

    assert client.post("/auth/sessions/revoke-others", headers=headers).status_code == 200
    db_session.expire_all()
    assert db_session.get(AuthSession, "other-sid").revoked_at is not None
    assert db_session.get(AuthSession, f"sid-{user.id}").revoked_at is None
