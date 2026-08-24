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
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import settings
from app.db import utcnow
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
    if not hub_client.enabled():
        claude_credentials.discard_hub_run_credential(run_id)
        return SOURCE_LOCAL
    if not hub_token:
        # Hub-data mode: the hub's Claude credential is the ONLY source (#607). A
        # run started without a fresh hub token cannot resolve it, and silently
        # running on local material would be exactly the wrong answer — the whole
        # point of the mode is that the hub decides which account is used.
        claude_credentials.discard_hub_run_credential(run_id)
        raise HubCredentialRefusedError(
            "No EmeHub token on the request, so the hub's Claude credential could "
            "not be resolved. Reload Q-Agent (which mints a fresh hub token) and "
            "start the run again."
        )

    try:
        payload = hub_client.resolve_claude_credential(hub_token)
    except (hub_client.HubUnauthorizedError, hub_client.HubUnavailableError) as exc:
        # Not an answer about the credential — the hub is down, or this token's 15
        # minutes are up. This used to fall back to the local credential at INFO,
        # which is how an unreachable hub surfaced 20 minutes later, in a background
        # worker, as "No Claude credentials configured. Upload your own credentials
        # in Settings" — advice that was simply wrong, for a hub that had the
        # credential all along (#607). In hub-data mode the hub is authoritative, so
        # refuse the run here, while we still know why.
        logger.warning(
            "run {} could not read the hub credential from {} ({}): {}",
            run_id,
            hub_client.effective_base_url(),
            type(exc).__name__,
            exc,
        )
        claude_credentials.discard_hub_run_credential(run_id)
        expired = isinstance(exc, hub_client.HubUnauthorizedError)
        raise HubCredentialRefusedError(
            "Your EmeHub session token expired before the run could start. Reload "
            "Q-Agent and start the run again."
            if expired
            else "Could not reach EmeHub to resolve the Claude credential "
            f"({hub_client.effective_base_url()}). The hub is unreachable from the "
            "Q-Agent server, so the run was not started."
        ) from exc
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
        # A 200 that isn't the documented object is a broken hub, not an answer —
        # and in hub-data mode it must not silently become "use local" (#607).
        logger.warning(
            "run {} got a malformed hub credential payload from {}",
            run_id,
            hub_client.effective_base_url(),
        )
        claude_credentials.discard_hub_run_credential(run_id)
        raise HubCredentialRefusedError(
            "EmeHub returned an unreadable Claude credential response, so the run "
            "was not started."
        )

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
    # Mint the grant while the browser's token is still fresh (#667). Without it
    # the material above is all the run will ever have: a change of account in
    # EmeHub could not reach a run already under way, because a background worker
    # has no token to ask with. Best-effort — a hub that cannot mint one leaves
    # the run on the pinned material, exactly as before.
    _store_grant(run_id, hub_token)
    # Metadata only — never the material, and never the path's contents.
    logger.info(
        "run {} will use the hub-resolved Claude credential (hub source: {}, status: {}, dir: {})",
        run_id,
        hub_source or "unknown",
        status or "unknown",
        config_dir.name,
    )
    return SOURCE_HUB


# --------------------------------------------------------------- grants (#667)
#: How stale the materialised credential may be before it is re-resolved.
#:
#: "Always take it from the hub" in practice, without a hub round-trip per Claude
#: invocation: `claude_cli` resolves the environment for every call, and a
#: multi-case generation pass makes many. A minute is far below the time it takes
#: a person to change the account in EmeHub and come back, so the change is picked
#: up on the next action either way.
CREDENTIAL_MAX_AGE = timedelta(seconds=60)


def _grant_path(run_id: int) -> Path:
    """Where a run's credential grant lives — beside the config dirs, not IN one.

    Deliberately not inside ``hub-run-<id>/``: that directory is handed to the
    Claude CLI as ``CLAUDE_CONFIG_DIR``, and putting an unrelated secret in a
    directory another program owns is how a stray file becomes someone's bug.
    """
    return settings.workspace_dir / "claude-config" / "hub-grants" / f"{run_id}.json"


def _store_grant(run_id: int, hub_token: str) -> None:
    """Mint and persist a credential grant for ``run_id``. Never raises."""
    try:
        payload = hub_client.mint_credential_grant(hub_token, run_id)
        grant = str((payload or {}).get("grant") or "")
        if not grant:
            logger.warning("run {} got no grant back from the hub — staying on pinned material", run_id)
            return
        expires_in = int((payload or {}).get("expiresIn") or 0)
        path = _grant_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "grant": grant,
                    "expiresAt": (utcnow() + timedelta(seconds=expires_in)).isoformat()
                    if expires_in
                    else "",
                }
            ),
            encoding="utf-8",
        )
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:  # pragma: no cover - no-op on Windows shares
            pass
        # Metadata only: the grant itself is a secret.
        logger.info("run {} holds a hub credential grant for {}s", run_id, expires_in or "?")
    except Exception as exc:  # noqa: BLE001 - a grant is an upgrade, never a gate
        logger.warning("run {} could not mint a hub credential grant: {}", run_id, exc)


def _load_grant(run_id: int) -> str | None:
    """The run's unexpired grant, or None."""
    try:
        raw = json.loads(_grant_path(run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    grant = str(raw.get("grant") or "")
    if not grant:
        return None
    expires_at = str(raw.get("expiresAt") or "")
    if expires_at:
        try:
            if datetime.fromisoformat(expires_at) <= utcnow():
                return None
        except ValueError:
            return None
    return grant


def discard_grant(run_id: int) -> None:
    """Forget a run's grant (best-effort)."""
    try:
        _grant_path(run_id).unlink(missing_ok=True)
    except OSError:  # pragma: no cover
        pass


def refresh_run_credential(run_id: int) -> bool:
    """Re-resolve the run's Claude credential from the hub. True when it changed.

    This is what makes a change of account in EmeHub reach a run that is already
    under way (#667). It runs on background worker threads, which is only legal
    because the grant exists: it was minted from the browser's token at run start
    and lives long enough (240 minutes by default) to carry the run.

    Every failure is swallowed and leaves the pinned material in place. The
    credential is a *dependency* of the work, not the work itself — a hub blip
    must not fail a generation pass that has perfectly good material on disk.
    """
    if not hub_client.enabled():
        return False
    config_dir = claude_credentials.hub_run_config_dir(run_id)
    if config_dir is None:
        return False  # nothing pinned ⇒ this run never resolved from the hub
    creds_file = config_dir / ".credentials.json"
    try:
        age = utcnow() - datetime.fromtimestamp(creds_file.stat().st_mtime, tz=timezone.utc)
        if age < CREDENTIAL_MAX_AGE:
            return False
    except OSError:
        return False
    grant = _load_grant(run_id)
    if grant is None:
        return False

    try:
        payload = hub_client.resolve_claude_credential(grant)
        material = _extract_material(payload if isinstance(payload, dict) else {})
        status = str((payload or {}).get("status") or "")
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.warning("run {} could not refresh its hub credential: {}", run_id, exc)
        return False
    if material is None or not _status_is_usable(status):
        logger.warning(
            "run {} kept its pinned credential — the hub now reports status {}",
            run_id,
            status or "none",
        )
        return False

    before = creds_file.read_text(encoding="utf-8") if creds_file.exists() else ""
    claude_credentials.materialize_raw(material, claude_credentials.hub_run_key(run_id))
    changed = before.strip() != material.strip()
    if changed:
        # Worth an INFO: "which account did this run use?" must stay answerable
        # from the logs, and now the answer can change mid-run.
        logger.info("run {} picked up a DIFFERENT Claude credential from the hub", run_id)
    return changed

