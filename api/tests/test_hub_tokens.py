"""EmeHub agent-token decoding — B1 of the hub SSO integration (#478).

Stage 1 of the verification plan in ``docs/HUB-INTEGRATION.md`` §7, in full: a
valid token decodes; wrong ``iss`` rejected; wrong ``aud`` rejected (minted for
``dagent``); expired rejected; tampered signature rejected; and a token with an
unknown ``kid`` still verifies — which is the test that proves ``kid`` is not
load-bearing, so the hub's eventual move to RS256 + JWKS stays non-breaking.

These are pure decoder tests: no app, no DB. The dual-accept wiring and the
local-user mapping land in B2 (#479).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.services import hub_tokens

# >=32 bytes so PyJWT doesn't emit InsecureKeyLengthWarning on every mint.
HUB_SECRET = "hub-shared-secret-for-tests-0123456789abcdef"
LOCAL_SECRET = "a-different-local-secret-0123456789abcdef"


@pytest.fixture(autouse=True)
def hub_settings(monkeypatch):
    """Configure the hub secret/audience the decoder reads.

    Autouse so no test accidentally exercises the "secret not configured" refusal
    path; that case gets its own explicit test below.
    """
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "hub_jwt_secret", HUB_SECRET)
    monkeypatch.setattr(config_module.settings, "hub_audience", "qagent")
    return config_module.settings


def mint(
    *,
    secret: str = HUB_SECRET,
    sub: str = "3",
    email: str = "duna.nguyen@emesoft.net",
    role: str = "admin",
    sid: str | None = "3a7e1f",
    iss: str = "emehub",
    aud: str = "qagent",
    ttl: timedelta = timedelta(minutes=15),
    kid: str | None = "emehub-hs256-2026-07",
    drop: tuple[str, ...] = (),
) -> str:
    """Mint a hub-shaped token. Mirrors the §2.2 contract; kwargs bend one thing at a time."""
    now = datetime.now(timezone.utc)
    claims = {
        "sub": sub,
        "email": email,
        "role": role,
        "sid": sid,
        "aud": aud,
        "iss": iss,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
    }
    for claim in drop:
        claims.pop(claim, None)
    headers = {"kid": kid} if kid else None
    return jwt.encode(claims, secret, algorithm="HS256", headers=headers)


# ---------------------------------------------------------------- happy path
def test_valid_token_decodes_to_claims():
    claims = hub_tokens.decode(mint())

    assert claims.hub_user_id == "3"
    assert claims.email == "duna.nguyen@emesoft.net"
    assert claims.role == "admin"
    assert claims.hub_session_id == "3a7e1f"
    assert claims.expires_at > claims.issued_at


def test_hub_user_id_is_the_hub_subject_not_a_local_id():
    """`sub` is a HUB user id (§3.1) — it must survive as an opaque string.

    Guards the crux of the design: anything that casts `sub` to a local
    `users.id` resolves to the wrong account.
    """
    claims = hub_tokens.decode(mint(sub="98765"))

    assert claims.hub_user_id == "98765"
    assert isinstance(claims.hub_user_id, str)


def test_email_is_normalized_for_local_lookup():
    """`users.email` is stored lowercased, so linking must compare lowercased."""
    claims = hub_tokens.decode(mint(email="  Duna.Nguyen@EmeSoft.net "))

    assert claims.normalized_email == "duna.nguyen@emesoft.net"


def test_absent_sid_is_tolerated():
    claims = hub_tokens.decode(mint(sid=None))

    assert claims.hub_session_id is None


# ---------------------------------------------------------------- rejections
def test_wrong_issuer_rejected():
    with pytest.raises(hub_tokens.HubTokenError):
        hub_tokens.decode(mint(iss="somebody-else"))


def test_token_minted_for_dagent_is_rejected():
    """§7 Stage 1, verbatim: mint one for `dagent` and confirm it fails.

    The audience check is what stops a token issued for a sibling agent being
    replayed against Q-Agent.
    """
    with pytest.raises(hub_tokens.HubTokenError):
        hub_tokens.decode(mint(aud="dagent"))


def test_expired_token_rejected():
    with pytest.raises(hub_tokens.HubTokenError):
        hub_tokens.decode(mint(ttl=timedelta(minutes=-1)))


def test_tampered_signature_rejected():
    token = mint()
    header, payload, signature = token.split(".")
    tampered = f"{header}.{payload}.{signature[:-4]}AAAA"

    with pytest.raises(hub_tokens.HubTokenError):
        hub_tokens.decode(tampered)


def test_token_signed_with_the_local_secret_is_rejected():
    """The hub secret is deliberately separate from `secret_key` (emehub ADR 0005).

    A token signed with Q-Agent's own secret must not verify as a hub token, or
    the separation buys nothing.
    """
    with pytest.raises(hub_tokens.HubTokenError):
        hub_tokens.decode(mint(secret=LOCAL_SECRET))


@pytest.mark.parametrize("claim", ["exp", "iat", "iss", "aud", "sub"])
def test_missing_required_claim_rejected(claim):
    with pytest.raises(hub_tokens.HubTokenError):
        hub_tokens.decode(mint(drop=(claim,)))


def test_empty_subject_rejected():
    with pytest.raises(hub_tokens.HubTokenError):
        hub_tokens.decode(mint(sub="   "))


def test_missing_email_rejected():
    """A token without an email can't provision or link a local account."""
    with pytest.raises(hub_tokens.HubTokenError):
        hub_tokens.decode(mint(drop=("email",)))


def test_empty_token_rejected():
    with pytest.raises(hub_tokens.HubTokenError):
        hub_tokens.decode("")


def test_refuses_when_hub_secret_not_configured(monkeypatch):
    """With no secret set, refuse — never fall back to another secret."""
    import app.config as config_module

    token = mint()
    monkeypatch.setattr(config_module.settings, "hub_jwt_secret", "")

    with pytest.raises(hub_tokens.HubTokenError):
        hub_tokens.decode(token)


# ---------------------------------------------------------------- kid handling
def test_unknown_kid_still_verifies():
    """§7 Stage 1: proves `kid` is read but NOT load-bearing.

    This is what keeps the hub's Phase 3 move to RS256 + JWKS non-breaking — if
    verification ever keys on `kid`, this test fails and the upgrade path breaks.
    """
    claims = hub_tokens.decode(mint(kid="emehub-hs256-2099-01-rotated"))

    assert claims.hub_user_id == "3"


def test_missing_kid_header_still_verifies():
    claims = hub_tokens.decode(mint(kid=None))

    assert claims.hub_user_id == "3"


# ------------------------------------------------- dual-accept discrimination
def test_looks_like_hub_token_discriminates_on_iss():
    """§2.3: hub tokens carry `iss`; Q-Agent's own access tokens don't."""
    from app.services import auth_service

    local_access_token = auth_service._encode({"sub": "1", "role": "admin"}, timedelta(minutes=15), "access")

    assert hub_tokens.looks_like_hub_token(mint()) is True
    assert hub_tokens.looks_like_hub_token(local_access_token) is False


def test_looks_like_hub_token_is_safe_on_garbage():
    assert hub_tokens.looks_like_hub_token("not-a-jwt") is False
    assert hub_tokens.looks_like_hub_token("") is False


def test_looks_like_hub_token_does_not_imply_validity():
    """It reads UNVERIFIED claims — a forged token still looks hub-shaped.

    Encodes the rule that callers must always follow the sniff with `decode`.
    """
    forged = mint(secret="attacker-secret")

    assert hub_tokens.looks_like_hub_token(forged) is True
    with pytest.raises(hub_tokens.HubTokenError):
        hub_tokens.decode(forged)
