"""B3 — the EmeHub SSO bootstrap round trip, ``POST /auth/sso/complete`` (#480).

Stage 2 of ``docs/HUB-INTEGRATION.md`` §7 on the HTTP surface: an anonymous caller
hands over a hub agent token and walks away with an ordinary Q-Agent session.

Three properties carry the whole design, and each has a test here:

1. **The response is login-shaped, but carries nothing durable.** ``{accessToken,
   user}`` exactly as ``/auth/login`` sends it — which is what leaves
   ``store/auth.ts``, ``lib/api.ts`` and ``RequireAuth`` untouched on the frontend
   — and, since #531, **no** ``qagent_refresh``/``qagent_csrf``. It also *clears*
   any it was sent. A cached identity outlived the hub session that authorised it,
   so the next visitor to that browser inherited the previous user's session.
   Identity is derived from the hub on every renewal now, not stored here.
2. **It is reachable anonymously.** With ``QAGENT_AUTH_REQUIRED`` on, the global
   guard in ``main.py`` 401s any path not in ``_AUTH_ALLOWLIST``. This caller has
   no local token by definition, so a missing allowlist entry breaks the feature
   in a way no other test would notice (``test_bootstrap_is_reachable_anonymously``).
3. **It is dormant behind the flag.** With ``QAGENT_HUB_SSO_ENABLED`` off the route
   404s — indistinguishable from never having been deployed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.models.user import ROLE_ADMIN, ROLE_MEMBER, User
from app.services import auth_service

# >=32 bytes so PyJWT doesn't emit InsecureKeyLengthWarning on every mint.
HUB_SECRET = "hub-shared-secret-for-tests-0123456789abcdef"


@pytest.fixture
def sso_on(monkeypatch):
    """Turn the integration on and configure the shared hub secret."""
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "hub_sso_enabled", True)
    monkeypatch.setattr(config_module.settings, "hub_jwt_secret", HUB_SECRET)
    monkeypatch.setattr(config_module.settings, "hub_audience", "qagent")
    return config_module.settings


@pytest.fixture
def auth_on(monkeypatch):
    """Turn the global auth guard on (it is off for the rest of the suite)."""
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "auth_required", True)
    return config_module.settings


def mint(
    *,
    secret: str = HUB_SECRET,
    sub: str = "3",
    email: str = "duna.nguyen@emesoft.net",
    role: str = "admin",
    aud: str = "qagent",
    iss: str = "emehub",
    ttl: timedelta = timedelta(minutes=15),
) -> str:
    """Mint a hub-shaped agent token (``docs/HUB-INTEGRATION.md`` §2.2)."""
    now = datetime.now(timezone.utc)
    claims = {
        "sub": sub,
        "email": email,
        "role": role,
        "sid": "3a7e1f",
        "aud": aud,
        "iss": iss,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
    }
    return jwt.encode(claims, secret, algorithm="HS256", headers={"kid": "emehub-hs256-2026-07"})


def complete(client, token: str, next_path: str | None = None):
    body: dict = {"hubToken": token}
    if next_path is not None:
        body["next"] = next_path
    return client.post("/auth/sso/complete", json=body)


# ---------------------------------------------------------------- the round trip
def test_bootstrap_returns_a_login_shaped_body(client, sso_on):
    r = complete(client, mint())

    assert r.status_code == 200
    body = r.json()
    # Exactly what /auth/login returns on success (plus the echoed `next`).
    assert body["accessToken"]
    assert body["user"]["email"] == "duna.nguyen@emesoft.net"
    assert body["mfaRequired"] is False
    assert body["mfaToken"] is None


def test_bootstrap_issues_no_refresh_cookie(client, sso_on):
    """#531 — an SSO session must leave nothing durable behind.

    It used to set the normal pair, which is what let a signed-out hub user's
    Q-Agent session survive: the cookie outlived the hub session that authorised
    it, and the next `/auth/refresh` restored them.

    The access token in the body is the whole credential now. Renewal goes back
    through the hub, so identity cannot drift from it.
    """
    complete(client, mint())

    assert not client.cookies.get("qagent_refresh")
    # No refresh cookie means nothing to protect with a double-submit: CSRF is
    # only verified on `/auth/refresh`.
    assert not client.cookies.get("qagent_csrf")


def test_bootstrap_clears_a_previous_users_refresh_cookie(client, sso_on, db_session):
    """THE bug in #531, in one test.

    Sign in locally as one user, then bootstrap as another — exactly what
    happens when someone signs out at the hub, signs in as somebody else and
    clicks Launch in the same browser. The first user's `qagent_refresh` is
    still in the jar, and merely declining to set a new one leaves it there to
    be presented on the next refresh.

    Not setting is not enough. It has to be cleared.
    """
    previous = User(
        email="sam.carter@emesoft.net",
        first_name="Sam",
        last_name="Carter",
        role=ROLE_MEMBER,
        password_hash=auth_service.hash_password("correct-horse"),
        is_active=True,
    )
    db_session.add(previous)
    db_session.commit()

    client.post(
        "/auth/login",
        json={"email": "sam.carter@emesoft.net", "password": "correct-horse", "remember": True},
    )
    assert client.cookies.get("qagent_refresh"), "precondition: the first user holds a cookie"

    complete(client, mint())

    assert not client.cookies.get("qagent_refresh")
    # And the cleared state is real, not just absent from the jar: presenting
    # whatever is left must not resurrect anyone.
    assert client.post("/auth/refresh").status_code == 401


def test_bootstrap_access_token_is_a_local_qagent_token(client, sso_on, db_session):
    """The returned token must be Q-Agent's own — decodable by the local decoder
    and carrying a real ``auth_sessions`` sid, not the hub's ``sid``."""
    from app.models.session import Session as AuthSession

    token = complete(client, mint()).json()["accessToken"]

    payload = auth_service.decode_access_token(token)
    user = db_session.get(User, int(payload["sub"]))
    assert user is not None and user.hub_user_id == "3"
    assert db_session.get(AuthSession, payload["sid"]) is not None
    assert payload["sid"] != "3a7e1f"  # the hub session id is not a local sid


def test_bootstrapped_session_reaches_a_guarded_route(client, sso_on, auth_on):
    """End of the round trip: the session behaves like any other (guard on)."""
    token = complete(client, mint()).json()["accessToken"]

    assert client.get("/audit/stats", headers={"Authorization": f"Bearer {token}"}).status_code == 200


# ---------------------------------------------------------------- the allowlist trap
def test_bootstrap_is_reachable_anonymously(client, sso_on, auth_on):
    """THE trap: the caller arrives with no local access token, so the route must
    be in ``main._AUTH_ALLOWLIST``. Without the entry the global guard 401s before
    the handler runs and the bootstrap can never complete.

    Asserted as "not 401" rather than "200" so this keeps testing the guard rather
    than the happy path.
    """
    assert client.get("/audit/stats").status_code == 401  # the guard really is on

    r = complete(client, mint())

    assert r.status_code != 401
    assert r.status_code == 200


def test_invalid_token_is_still_rejected_while_allowlisted(client, sso_on, auth_on):
    """Allowlisted means "the guard steps aside", not "anyone gets a session"."""
    assert complete(client, mint(secret="not-the-hub-secret-0123456789abcd")).status_code == 401


# ---------------------------------------------------------------- the flag
def test_route_404s_when_the_flag_is_off(client):
    """Dormant by default — 404, not 401, so it looks undeployed."""
    assert complete(client, mint()).status_code == 404


def test_route_404s_when_the_flag_is_off_even_with_the_guard_on(client, auth_on):
    """The allowlist entry is unconditional, so the guard must not turn this into
    a 401 — an allowlisted 404 leaks nothing."""
    assert complete(client, mint()).status_code == 404


# ---------------------------------------------------------------- token rejection
@pytest.mark.parametrize(
    "token_kwargs",
    [
        pytest.param({"secret": "a-totally-different-secret-0123456789ab"}, id="tampered-signature"),
        pytest.param({"ttl": timedelta(minutes=-5)}, id="expired"),
        pytest.param({"aud": "dagent"}, id="wrong-audience"),
        pytest.param({"iss": "somebody-else"}, id="wrong-issuer"),
        pytest.param({"email": ""}, id="no-email"),
        pytest.param({"sub": ""}, id="empty-subject"),
    ],
)
def test_bad_tokens_are_rejected(client, sso_on, token_kwargs, db_session):
    r = complete(client, mint(**token_kwargs))

    assert r.status_code == 401
    # And no account was provisioned as a side effect.
    assert db_session.query(User).count() == 0


def test_a_local_qagent_access_token_is_not_accepted(client, sso_on, db_session):
    """Only *hub* tokens bootstrap. A local access token carries no ``iss``, so
    the hub decoder must reject it rather than mint a second session from it."""
    user = User(email="local@example.com", password_hash="x")
    db_session.add(user)
    db_session.commit()
    local = auth_service.create_access_token(user, "sid-1")

    assert complete(client, local).status_code == 401


# ---------------------------------------------------------------- user resolution
def test_second_bootstrap_reuses_the_same_row(client, sso_on, db_session):
    """Same hub ``sub`` twice → one local user, two sessions (§7 Stage 2)."""
    first = complete(client, mint()).json()
    second = complete(client, mint()).json()

    assert first["user"]["id"] == second["user"]["id"]
    assert first["accessToken"] != second["accessToken"]
    assert db_session.query(User).filter(User.hub_user_id == "3").count() == 1
    assert db_session.query(User).count() == 1


def test_new_user_is_provisioned_with_the_token_role(client, sso_on, db_session):
    body = complete(client, mint(sub="9", email="Fresh.User@Example.com", role="admin")).json()

    user = db_session.query(User).filter(User.hub_user_id == "9").one()
    assert user.email == "fresh.user@example.com"  # normalized, matching users.email
    assert user.role == ROLE_ADMIN
    assert body["user"]["role"] == ROLE_ADMIN


def test_unknown_role_falls_back_to_member(client, sso_on, db_session):
    complete(client, mint(sub="11", email="odd@example.com", role="superuser"))

    assert db_session.query(User).filter(User.hub_user_id == "11").one().role == ROLE_MEMBER


def test_email_collision_links_the_existing_local_user(client, sso_on, db_session):
    """Auto-link (§8 decision 1): the existing row is claimed, not duplicated —
    so its runs, evidence and workspace path (keyed on ``users.id``) survive."""
    existing = User(
        email="duna.nguyen@emesoft.net",
        password_hash=auth_service.hash_password("localpassword1"),
        role=ROLE_MEMBER,
    )
    db_session.add(existing)
    db_session.commit()
    original_id = existing.id

    body = complete(client, mint(email="Duna.Nguyen@EmeSoft.net")).json()

    db_session.refresh(existing)
    assert body["user"]["id"] == original_id
    assert existing.hub_user_id == "3"
    assert db_session.query(User).count() == 1


def test_hub_role_never_overwrites_an_existing_local_role(client, sso_on, db_session):
    """Q-Agent authorises (§8 decision 2). A hub token minted for an ``admin``
    must not promote a linked local member — nor on any later login."""
    existing = User(email="duna.nguyen@emesoft.net", password_hash="x", role=ROLE_MEMBER)
    db_session.add(existing)
    db_session.commit()

    complete(client, mint(role="admin"))
    complete(client, mint(role="admin"))

    db_session.refresh(existing)
    assert existing.role == ROLE_MEMBER


def test_deactivated_local_account_is_refused(client, sso_on, db_session):
    """The hub authenticates; Q-Agent still decides who may in."""
    db_session.add(
        User(email="gone@example.com", password_hash="x", hub_user_id="3", is_active=False)
    )
    db_session.commit()

    assert complete(client, mint(email="gone@example.com")).status_code == 403


def test_provisioned_account_cannot_sign_in_with_a_local_password(client, sso_on, db_session):
    """An empty password hash never verifies, so provisioning doesn't open a
    password door into the new account."""
    complete(client, mint(sub="12", email="hubonly@example.com"))

    r = client.post("/auth/login", json={"email": "hubonly@example.com", "password": ""})
    assert r.status_code == 401


# ---------------------------------------------------------------- `next`
def test_next_is_echoed_back(client, sso_on):
    assert complete(client, mint(), "/runs/RUN-42/review").json()["next"] == "/runs/RUN-42/review"


def test_absent_next_lands_on_root(client, sso_on):
    assert complete(client, mint()).json()["next"] == "/"


@pytest.mark.parametrize(
    "hostile",
    ["//evil.example/pwn", "https://evil.example", "http://evil.example", "/\\evil.example", ""],
)
def test_hostile_next_is_clamped_to_root(client, sso_on, hostile):
    """The SPA navigates to whatever comes back, so an off-origin ``next`` would
    make this an open redirect wearing our domain."""
    assert complete(client, mint(), hostile).json()["next"] == "/"


# ---------------------------------------------------------------- the public probe
def test_health_carries_the_hub_base_url(client, sso_on, monkeypatch):
    """The callback screen is anonymous and needs the hub's origin to call
    ``/auth/agent-token``; ``/health`` is the only allowlisted place to read it."""
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "hub_base_url", "https://hub.chuongnd.click/")

    body = client.get("/health").json()

    assert body["hubSsoEnabled"] is True
    assert body["hubBaseUrl"] == "https://hub.chuongnd.click"


def test_health_never_leaks_the_shared_secret(client, sso_on):
    assert HUB_SECRET not in client.get("/health").text
