"""Resolve the Claude credential from EmeHub **at run start** (C2 of #497).

Why at run start, and only there
--------------------------------
A hub agent token lives 15 minutes *and* is bound to a live hub session, and
agents may not refresh it. A background worker therefore has no way to obtain
one mid-run. So the resolution happens once, inside the request that started the
run, while the browser-minted token is fresh; the material is written to a
per-run config dir (:func:`app.services.claude_credentials.materialize_raw`) and
every later CLI call in that run reads the *file*. There is no hub call on a
background thread — see :func:`app.services.claude_cli._resolve_claude_env`.

Fallback policy (deliberate, and asymmetric)
--------------------------------------------
* **Hub unreachable, or 401** → fall back to the local credential and let the run
  proceed. Neither is an answer about the credential: one is "the hub is down",
  the other "this 15-minute token is done". Failing the run on infrastructure
  would make the hub a single point of failure for work that has a perfectly good
  local credential.
* **Hub answers authoritatively that there is no usable credential** → the run
  **refuses**. That *is* an answer, and quietly running on a possibly-stale local
  credential is the one thing an agent must not do here.
* **Flag off** → this module does nothing at all; behaviour is byte-identical to
  before the integration existed.

``status`` has four values and ``refreshable`` is the common live one
--------------------------------------------------------------------
A Claude OAuth *access* token expires within hours, so a genuinely healthy
credential is past ``expiresAt`` almost immediately and the hub reports
``refreshable`` (a refresh token exists) rather than ``expired``. Treating it as
expired would refuse nearly every real credential — see
:data:`_USABLE_STATUSES`.

The credential material is a secret: it is written to an owner-only file and is
never logged (not even at debug), never put in an exception message, and never
returned to the SPA.
"""

from __future__ import annotations

import json
from typing import Any

from app.logging import logger
from app.services import claude_credentials, hub_client

__all__ = [
    "HubCredentialRefusedError",
    "SOURCE_HUB",
    "SOURCE_LOCAL",
    "prepare_run_credential",
]

SOURCE_HUB = "hub"
SOURCE_LOCAL = "local"

# Usable: the hub has material we can hand to the CLI. ``refreshable`` means the
# access token is past its expiry but a refresh token exists — the CLI refreshes
# it in place on first use, which is the ordinary path for a live credential.
_USABLE_STATUSES = frozenset({"active", "valid", "refreshable"})

# Authoritatively dead. ``none``/``missing`` mean the hub resolved own → shared →
# nothing; ``expired``/``revoked`` mean material exists but cannot be used.
_DEAD_STATUSES = frozenset({"none", "missing", "absent", "expired", "revoked"})


class HubCredentialRefusedError(Exception):
    """The hub answered authoritatively that there is no usable credential.

    The run must refuse rather than fall back — see the module docstring. The
    message is safe to surface to the user; it never contains credential
    material.
    """


def _extract_material(payload: dict[str, Any]) -> str | None:
    """The ``.credentials.json`` text to write, or ``None`` if the payload has none.

    Accepts the shapes the hub may serve for ``credentials``: the file's contents
    as a JSON string, the whole ``{"claudeAiOauth": {...}}`` object, or the bare
    OAuth object (which we wrap, because that is what the CLI reads).

    Never logs or echoes what it inspected.
    """
    raw = payload.get("credentials")
    if raw is None:
        return None

    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
    elif isinstance(raw, dict):
        parsed = raw
    else:
        return None

    if not parsed:
        return None
    if "claudeAiOauth" not in parsed:
        # A bare OAuth object — wrap it into the file layout the CLI expects.
        parsed = {"claudeAiOauth": parsed}
    oauth = parsed.get("claudeAiOauth")
    if not isinstance(oauth, dict) or not str(oauth.get("accessToken") or "").strip():
        return None
    return json.dumps(parsed)


def _status_is_usable(status: str) -> bool:
    """Whether ``status`` means "we can run with this".

    Unknown values are treated as **usable**: the hub may add a status we have
    never heard of, and refusing every run on an unrecognised label is a worse
    failure than trying material the hub chose to send us. Only the explicitly
    dead ones refuse.
    """
    normalised = (status or "").strip().lower()
    if normalised in _USABLE_STATUSES:
        return True
    if normalised in _DEAD_STATUSES:
        return False
    logger.warning(
        "hub reported an unrecognised Claude credential status {!r}; treating it as usable",
        normalised or "<empty>",
    )
    return True


def prepare_run_credential(run_id: int, hub_token: str | None) -> str:
    """Resolve + materialise the Claude credential for ``run_id`` at run start.

    Returns which source the run will use — ``"hub"`` or ``"local"`` — and logs
    it, so "which credential did this run use?" is answerable from the logs
    without a database dig.

    Raises :class:`HubCredentialRefusedError` when the hub authoritatively
    reports no usable credential. Any other hub problem (disabled, unreachable,
    401) returns ``"local"``.
    """
    # Flag off (or no hub token on the request): nothing to do, and crucially any
    # dir left behind by an earlier attempt is cleared, so the run cannot pick up
    # stale hub material once the integration is switched off.
    if not hub_client.enabled() or not hub_token:
        claude_credentials.discard_hub_run_credential(run_id)
        return SOURCE_LOCAL

    try:
        payload = hub_client.resolve_claude_credential(hub_token)
    except (hub_client.HubUnauthorizedError, hub_client.HubUnavailableError) as exc:
        # Not an answer about the credential — the hub is down, or this token's
        # 15 minutes are up. Proceed on the local credential.
        logger.info(
            "run {} could not read the hub credential ({}); using the local credential",
            run_id,
            type(exc).__name__,
        )
        claude_credentials.discard_hub_run_credential(run_id)
        return SOURCE_LOCAL
    except hub_client.HubDisabledError:
        claude_credentials.discard_hub_run_credential(run_id)
        return SOURCE_LOCAL
    except hub_client.HubRefusedError as exc:
        # The hub answered on its own behalf: 404 "no credential", 403 "not for
        # you". Authoritative, so do not paper over it with local material.
        claude_credentials.discard_hub_run_credential(run_id)
        raise HubCredentialRefusedError(
            "EmeHub reports no usable Claude credential for this account "
            f"(hub said {exc.status_code}). Connect a Claude account in EmeHub, "
            "then start the run again."
        ) from exc

    if not isinstance(payload, dict):
        # A 200 that isn't the documented object is a broken hub, not an answer.
        logger.warning("run {} got a malformed hub credential payload; using local", run_id)
        claude_credentials.discard_hub_run_credential(run_id)
        return SOURCE_LOCAL

    status = str(payload.get("status") or "")
    hub_source = str(payload.get("source") or "")
    material = _extract_material(payload)

    if material is None or not _status_is_usable(status):
        claude_credentials.discard_hub_run_credential(run_id)
        raise HubCredentialRefusedError(
            "EmeHub reports no usable Claude credential for this account "
            f"(status: {status or 'none'}). Connect or renew the Claude account "
            "in EmeHub, then start the run again."
        )

    config_dir = claude_credentials.materialize_raw(material, claude_credentials.hub_run_key(run_id))
    # Metadata only — never the material, and never the path's contents.
    logger.info(
        "run {} will use the hub-resolved Claude credential (hub source: {}, status: {}, dir: {})",
        run_id,
        hub_source or "unknown",
        status or "unknown",
        config_dir.name,
    )
    return SOURCE_HUB
