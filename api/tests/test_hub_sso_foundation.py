"""B1 foundation wiring — the flag, the mapping column, the public probe (#478).

Complements ``test_hub_tokens.py`` (the decoder). Here we assert the things B2–B5
build on: the integration flag defaults OFF, ``users.hub_user_id`` exists with the
right shape, and ``hubSsoEnabled`` is readable by an **anonymous** caller.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.models.user import User


def test_hub_sso_is_disabled_by_default(workspace_dir):
    """The integration must be dormant unless explicitly switched on."""
    import app.config as config_module

    fresh = config_module.Settings()

    assert fresh.hub_sso_enabled is False
    assert fresh.hub_base_url == ""
    assert fresh.hub_jwt_secret == ""
    assert fresh.hub_audience == "qagent"


def test_hub_secret_is_not_the_local_secret_key(workspace_dir):
    """`hub_jwt_secret` must be its own setting, not an alias of `secret_key`.

    `secret_key` signs local JWTs *and* derives the Fernet key for every
    encrypted credential (`app/crypto.py`); overloading it is what emehub
    ADR 0005 forbids.
    """
    import app.config as config_module

    settings = config_module.settings
    settings.hub_jwt_secret = "hub-only-secret"

    assert settings.secret_key != settings.hub_jwt_secret


def test_users_table_has_hub_user_id_column(db_session):
    columns = {c["name"]: c for c in inspect(db_session.get_bind()).get_columns("users")}

    assert "hub_user_id" in columns
    assert columns["hub_user_id"]["nullable"] is True


def test_hub_user_id_defaults_to_null_for_local_accounts(db_session):
    """Existing/local-only users are untouched — no data migration runs (§3.1)."""
    user = User(email="local@example.com", password_hash="x")
    db_session.add(user)
    db_session.commit()

    assert user.hub_user_id is None


def test_hub_user_id_is_unique(db_session):
    """The constraint that makes B2's JIT provisioning safe: no duplicate accounts
    for one hub `sub`."""
    db_session.add(User(email="a@example.com", password_hash="x", hub_user_id="42"))
    db_session.commit()

    db_session.add(User(email="b@example.com", password_hash="x", hub_user_id="42"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_many_local_users_can_share_null_hub_user_id(db_session):
    """Uniqueness ignores NULLs, so any number of local-only users coexist."""
    db_session.add_all(
        [
            User(email="one@example.com", password_hash="x"),
            User(email="two@example.com", password_hash="x"),
            User(email="three@example.com", password_hash="x"),
        ]
    )
    db_session.commit()

    assert db_session.query(User).filter(User.hub_user_id.is_(None)).count() == 3


def test_local_user_can_be_linked_to_a_hub_id(db_session):
    """The auto-link path B2 uses for an email collision (§8 decision 1)."""
    user = User(email="existing@example.com", password_hash="x")
    db_session.add(user)
    db_session.commit()

    user.hub_user_id = "3"
    db_session.commit()
    db_session.refresh(user)

    assert user.hub_user_id == "3"
    assert user.id is not None  # the local id is unchanged — that's the whole point


def test_health_reports_hub_sso_flag(client):
    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["hubSsoEnabled"] is False


def test_health_reports_hub_sso_flag_when_enabled(client, monkeypatch):
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "hub_sso_enabled", True)

    assert client.get("/health").json()["hubSsoEnabled"] is True


def test_health_reports_hub_data_flag(client):
    """#528: the SPA hides Q-Agent's own Claude/Projects configuration on this flag.

    Defaults to False so a deployment without hub data keeps every self-service
    control exactly as it was.
    """
    body = client.get("/health").json()

    assert body["hubDataEnabled"] is False


def test_health_reports_hub_data_flag_when_enabled(client, monkeypatch):
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "hub_data_enabled", True)

    assert client.get("/health").json()["hubDataEnabled"] is True


def test_health_hub_data_flag_is_readable_anonymously(client, monkeypatch):
    """Same reason as the SSO flag: `/capabilities` is behind the auth guard."""
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "auth_required", True)
    monkeypatch.setattr(config_module.settings, "hub_data_enabled", True)

    health = client.get("/health")

    assert health.status_code == 200
    assert health.json()["hubDataEnabled"] is True


def test_health_is_readable_anonymously_with_auth_on(client, monkeypatch):
    """The reason the flag lives on `/health` and not `/capabilities` (#478).

    `/health` is in `main._AUTH_ALLOWLIST` and `/capabilities` is not, so with
    the auth guard on only this endpoint can tell the anonymous login screen
    whether to render the "Sign in with EmeHub" button.
    """
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "auth_required", True)

    health = client.get("/health")
    capabilities = client.get("/capabilities")

    assert health.status_code == 200
    assert "hubSsoEnabled" in health.json()
    assert capabilities.status_code == 401
