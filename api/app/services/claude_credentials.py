"""Claude CLI credentials — per-user or shared ``.credentials.json`` (#95).

The Claude CLI reads its OAuth session from ``<CLAUDE_CONFIG_DIR>/.credentials.json``
(default ``~/.claude/.credentials.json``). Instead of one shared machine login,
each user may upload their own credentials file; an admin may also configure one
shared/fallback credential (``owner_id`` NULL) for users who haven't.

Resolution order (:func:`resolve_effective`): the requesting user's own
credential, else the shared credential, else ``None`` (no interactive
``claude login`` fallback — see ADR 0001, no simulated/implicit auth).

On resolve, the stored (Fernet-encrypted) contents are decrypted and written to
a private per-key directory under ``workspace/claude-config/<key>/.credentials.json``
(``<key>`` is the user id for an own credential, or ``"shared"``) so
``app.services.claude_cli`` can point the subprocess at it via
``CLAUDE_CONFIG_DIR`` — see :func:`materialize`.
"""

from __future__ import annotations

import json
import stat
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app import crypto
from app.config import settings
from app.models.claude_credentials import STATUS_ACTIVE, STATUS_EXPIRED, ClaudeCredentials

__all__ = [
    "ClaudeCredentialsError",
    "delete_own",
    "delete_shared",
    "discard_hub_run_credential",
    "get_own",
    "get_shared",
    "hub_run_config_dir",
    "hub_run_key",
    "materialize",
    "materialize_raw",
    "resolve_ambient_owner_id",
    "resolve_effective_config_dir",
    "resolve_scoped_config_dir",
    "set_effective_status",
    "set_scoped_status",
    "set_preferred_mode",
    "status_for",
    "upsert_own",
    "upsert_shared",
]


class ClaudeCredentialsError(ValueError):
    """Raised when uploaded credentials contents are not valid JSON."""


def _validate(raw_credentials: str) -> str:
    """Return ``raw_credentials`` unchanged if it parses as a JSON object.

    Raises :class:`ClaudeCredentialsError` (a 400 to the caller) on malformed
    input — we never persist something the CLI couldn't read back.
    """
    try:
        parsed = json.loads(raw_credentials)
    except json.JSONDecodeError as exc:
        raise ClaudeCredentialsError(f"credentials must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ClaudeCredentialsError("credentials must be a JSON object")
    return raw_credentials


def _extract_metadata(raw_credentials: str) -> dict:
    """Best-effort extraction of subscription metadata from an uploaded
    ``.credentials.json``.

    Reads from the ``claudeAiOauth`` object when present, else falls back to
    top-level keys. Every field is optional — malformed/missing data yields
    ``None`` rather than raising; the caller has already validated
    ``raw_credentials`` parses as a JSON object.

    Returns a dict with ``expires_at`` (``datetime | None``, converted from an
    ``expiresAt`` epoch-ms value), ``scopes`` (``list[str] | None``), and
    ``subscription_type`` (``str | None``).
    """
    empty = {"expires_at": None, "scopes": None, "subscription_type": None}
    try:
        parsed = json.loads(raw_credentials)
    except json.JSONDecodeError:
        return empty
    if not isinstance(parsed, dict):
        return empty

    oauth = parsed.get("claudeAiOauth")
    source = oauth if isinstance(oauth, dict) else parsed

    expires_at = None
    raw_expires_ms = source.get("expiresAt")
    if isinstance(raw_expires_ms, (int, float)) and not isinstance(raw_expires_ms, bool):
        try:
            expires_at = datetime.fromtimestamp(raw_expires_ms / 1000, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            expires_at = None

    scopes = None
    raw_scopes = source.get("scopes")
    if isinstance(raw_scopes, list) and all(isinstance(s, str) for s in raw_scopes):
        scopes = raw_scopes

    subscription_type = None
    raw_subscription_type = source.get("subscriptionType")
    if isinstance(raw_subscription_type, str):
        subscription_type = raw_subscription_type

    return {
        "expires_at": expires_at,
        "scopes": scopes,
        "subscription_type": subscription_type,
    }


def get_own(db: Session, owner_id: int) -> ClaudeCredentials | None:
    """Return the given user's own credential row, or None."""
    return (
        db.query(ClaudeCredentials)
        .filter(ClaudeCredentials.owner_id == owner_id)
        .first()
    )


def get_shared(db: Session) -> ClaudeCredentials | None:
    """Return the single shared/admin credential row, or None."""
    return db.query(ClaudeCredentials).filter(ClaudeCredentials.owner_id.is_(None)).first()


def _effective_row(db: Session, owner_id: int | None) -> ClaudeCredentials | None:
    """Return the credential row that :func:`resolve_effective_config_dir` would
    actually use for ``owner_id`` — own (unless it yields to shared), else shared,
    else None. Shared with the status-writeback helpers so a failed/tested call
    updates the same row it ran under."""
    if owner_id is not None:
        own = get_own(db, owner_id)
        if own is not None and not _own_yields_to_shared(db, own):
            return own
    return get_shared(db)


def set_effective_status(db: Session, owner_id: int | None, status: str) -> bool:
    """Set the status of the credential that ``owner_id`` effectively uses.

    Used to reflect real CLI outcomes back into the stored row without a separate
    probe: a call that fails with "Not logged in" marks it ``expired``; a
    successful call / test marks it ``active`` again. No-op (returns False) when
    no credential is configured. Idempotent — only writes on an actual change.
    """
    row = _effective_row(db, owner_id)
    if row is None:
        return False
    if row.status != status:
        row.status = status
        db.commit()
    return True


def _scoped_row(db: Session, owner_id: int | None, scope: str) -> ClaudeCredentials | None:
    """Resolve the credential row for an explicit ``scope`` — "shared", "own", or
    "effective" (the default own→shared precedence). Used by the test endpoint so
    the admin Claude-credentials page can test the *shared* account even when the
    caller has their own on file."""
    if scope == "shared":
        return get_shared(db)
    if scope == "own":
        return get_own(db, owner_id) if owner_id is not None else None
    return _effective_row(db, owner_id)


def resolve_scoped_config_dir(db: Session, owner_id: int | None, scope: str) -> Path | None:
    """Materialize + return the config dir for an explicit ``scope`` (see
    :func:`_scoped_row`), or None when that scope has no credential."""
    row = _scoped_row(db, owner_id, scope)
    if row is None:
        return None
    key = "shared" if row.owner_id is None else str(row.owner_id)
    return materialize(row, key)


def set_scoped_status(db: Session, owner_id: int | None, scope: str, status: str) -> bool:
    """Write ``status`` back to the credential resolved for ``scope`` (idempotent)."""
    row = _scoped_row(db, owner_id, scope)
    if row is None:
        return False
    if row.status != status:
        row.status = status
        db.commit()
    return True


def _own_yields_to_shared(db: Session, own: ClaudeCredentials | None) -> bool:
    """True when an own credential exists but the user prefers the shared account
    (``prefer_shared``) *and* a shared credential exists to fall back to — so
    every resolution site should skip the own credential and use shared instead.
    A ``prefer_shared`` flag with no shared credential configured is ignored
    (own still wins), so a user is never left with nothing.
    """
    return own is not None and own.prefer_shared and get_shared(db) is not None


def set_preferred_mode(db: Session, owner_id: int, mode: str) -> ClaudeCredentials:
    """Set which credential ``owner_id`` prefers, persisted on their own row.

    ``mode`` is ``"own"`` or ``"shared"``. This lets a user who has uploaded
    their own credential *prefer* the shared account without deleting the upload
    (and flip back later). Requires an own credential row to exist — with no own
    credential the effective mode is already shared and there is nothing to store
    the preference on. Selecting ``"shared"`` additionally requires a shared
    credential to actually fall back to. Returns the updated own row.

    Raises :class:`ClaudeCredentialsError` (a 400 to the caller) on an unknown
    mode, a missing own credential, or ``"shared"`` with no shared credential.
    """
    if mode not in ("own", "shared"):
        raise ClaudeCredentialsError('mode must be "own" or "shared"')
    own = get_own(db, owner_id)
    if own is None:
        raise ClaudeCredentialsError("no personal credentials on file to switch")
    if mode == "shared" and get_shared(db) is None:
        raise ClaudeCredentialsError("no shared Claude account is configured")
    own.prefer_shared = mode == "shared"
    db.commit()
    db.refresh(own)
    return own


def _upsert(db: Session, owner_id: int | None, raw_credentials: str, label: str) -> ClaudeCredentials:
    validated = _validate(raw_credentials)
    row = (
        db.query(ClaudeCredentials)
        .filter(ClaudeCredentials.owner_id == owner_id)
        .first()
    )
    if row is None:
        row = ClaudeCredentials(owner_id=owner_id)
        db.add(row)
    meta = _extract_metadata(validated)
    row.credentials = crypto.encrypt(validated) or ""
    row.label = label
    row.status = STATUS_ACTIVE
    row.expires_at = meta["expires_at"]
    row.scopes = meta["scopes"]
    row.subscription_type = meta["subscription_type"]
    db.commit()
    db.refresh(row)
    return row


def upsert_own(db: Session, owner_id: int, raw_credentials: str, label: str = "") -> ClaudeCredentials:
    """Store/replace ``owner_id``'s own credentials file contents."""
    return _upsert(db, owner_id, raw_credentials, label)


def upsert_shared(db: Session, raw_credentials: str, label: str = "") -> ClaudeCredentials:
    """Store/replace the shared/admin credentials file contents."""
    return _upsert(db, None, raw_credentials, label)


def persist_refreshed(db: Session, owner_id: int | None) -> bool:
    """Capture a token the Claude CLI refreshed in-place back into the store.

    OAuth access tokens live only hours; the CLI refreshes them by rewriting
    ``<config_dir>/.credentials.json`` whenever it runs with an expired-but-
    refreshable token. Because :func:`materialize` re-writes that file from the
    DB on every call, an un-captured refresh is lost — and once the rotated
    refresh token goes stale the credential can no longer be refreshed at all,
    so it dies (the CLI then reports "Not logged in"). After each CLI run we
    read the file back and, when it holds a **non-empty** access token with a
    **strictly newer** ``expiresAt`` than what we stored, re-encrypt and persist
    it. Those two guards mean a *failed* refresh — which the CLI records as an
    empty/logged-out file — can never clobber a good stored credential.

    Mirrors :func:`resolve_effective_config_dir`'s own→shared precedence so it
    writes back to whichever row was actually used. Returns True if it updated
    the stored credential.
    """
    row, key = _writeback_row(db, owner_id)
    if row is None or key is None:
        return False
    try:
        raw = (_config_dir_for(key) / ".credentials.json").read_text(encoding="utf-8")
    except OSError:
        return False
    return _persist_if_newer(db, row, raw)


def persist_refreshed_from_raw(db: Session, owner_id: int | None, raw: str) -> bool:
    """Capture a token rotated on a PAIRED DEVICE back into the store (#cred-rotate).

    Mirror of :func:`persist_refreshed` for the Local Agent path: when a device
    runs ``claude`` with the owner's uploaded credential (live authoring / heal),
    the CLI refreshes the token in the device's temp config dir — which is then
    deleted. Unless the device posts the rotated ``.credentials.json`` content back
    (``raw``) and we persist it here, the stored credential's refresh token goes
    stale and the credential eventually dies, exactly like an un-captured local
    refresh. Same strictly-newer guard as the server path, so a logged-out/failed
    refresh can never clobber a good stored credential. Returns True if it updated.
    """
    if not raw or not raw.strip():
        return False
    row, _key = _writeback_row(db, owner_id)
    if row is None:
        return False
    return _persist_if_newer(db, row, raw)


def _writeback_row(db: Session, owner_id: int | None) -> tuple[ClaudeCredentials | None, str | None]:
    """The credential row a refresh should write back to — own→shared precedence,
    matching :func:`resolve_effective_config_dir`."""
    if owner_id is not None:
        own = get_own(db, owner_id)
        if own is not None and not _own_yields_to_shared(db, own):
            return own, str(owner_id)
    shared = get_shared(db)
    if shared is not None:
        return shared, "shared"
    return None, None


def _persist_if_newer(db: Session, row: ClaudeCredentials, raw: str) -> bool:
    """Persist ``raw`` (.credentials.json content) onto ``row`` only when it holds a
    non-empty access token with a **strictly newer** ``expiresAt`` than the stored
    one — so a failed/logged-out refresh never clobbers a good credential."""
    try:
        new_oauth = json.loads(raw).get("claudeAiOauth") or {}
    except (json.JSONDecodeError, AttributeError):
        return False

    new_token = str(new_oauth.get("accessToken") or "").strip()
    new_ms = new_oauth.get("expiresAt")
    if not new_token or not isinstance(new_ms, (int, float)):
        return False  # logged-out / failed-refresh / malformed — never persist

    # Compare raw epoch-ms (int) to sidestep tz-aware/naive datetime pitfalls.
    old_dec = crypto.decrypt(row.credentials) or ""
    try:
        old_ms = (json.loads(old_dec).get("claudeAiOauth") or {}).get("expiresAt") if old_dec else None
    except (json.JSONDecodeError, AttributeError):
        old_ms = None
    if isinstance(old_ms, (int, float)) and new_ms <= old_ms:
        return False  # not a fresher token — nothing to capture

    meta = _extract_metadata(raw)
    row.credentials = crypto.encrypt(raw) or ""
    row.status = STATUS_ACTIVE
    row.expires_at = meta["expires_at"]
    row.scopes = meta["scopes"]
    row.subscription_type = meta["subscription_type"]
    db.commit()
    return True


def delete_own(db: Session, owner_id: int) -> bool:
    """Delete ``owner_id``'s own credential row. Returns True if one existed."""
    row = get_own(db, owner_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def delete_shared(db: Session) -> bool:
    """Delete the shared/admin credential row. Returns True if one existed."""
    row = get_shared(db)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def _account_for(key: str) -> dict:
    """Best-effort account identity (email/org) the Claude CLI writes to
    ``<config_dir>/.claude.json`` under ``oauthAccount`` after it first
    authenticates. Empty dict when that file/field isn't present yet (e.g. the
    credential was just uploaded and no call has run under it). Never raises."""
    try:
        raw = (_config_dir_for(key) / ".claude.json").read_text(encoding="utf-8")
        account = json.loads(raw).get("oauthAccount") or {}
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}
    if not isinstance(account, dict):
        return {}
    return {
        "account_email": account.get("emailAddress") or None,
        "account_org": account.get("organizationName") or None,
    }


def _meta_for(row: ClaudeCredentials | None, *, with_assigned_users: bool = False, db: Session | None = None) -> dict | None:
    """Public metadata for one credential row (never the token). ``None`` if
    ``row`` doesn't exist. ``assigned_users`` (shared credential only) counts
    active users who fall back to it, i.e. have no own credential."""
    if row is None:
        return None
    assigned_users = None
    if with_assigned_users and db is not None:
        from app.models.user import User

        owned_ids = {
            r[0]
            for r in db.query(ClaudeCredentials.owner_id)
            .filter(ClaudeCredentials.owner_id.isnot(None))
            .all()
        }
        assigned_users = (
            db.query(User).filter(User.is_active.is_(True), User.id.notin_(owned_ids)).count()
            if owned_ids
            else db.query(User).filter(User.is_active.is_(True)).count()
        )
    account = _account_for("shared" if row.owner_id is None else str(row.owner_id))
    return {
        "status": row.status,
        "subscription_type": row.subscription_type,
        "expires_at": row.expires_at,
        "scopes": row.scopes or [],
        "last_refreshed": row.updated_at,
        "assigned_users": assigned_users,
        "account_email": account.get("account_email"),
        "account_org": account.get("account_org"),
    }


def status_for(db: Session, owner_id: int | None) -> dict:
    """Status summary for the AI settings screen: never leaks the token itself.

    ``mode`` is the credential that will actually be used for this user per
    :func:`resolve_effective`'s precedence: "own" > "shared" > "none". ``own``
    and ``shared`` carry each row's metadata (subscription/expiry/scopes/last
    refresh), so the personal card and the admin shared-account card can both
    render from one call.
    """
    own_row = get_own(db, owner_id) if owner_id is not None else None
    shared_row = get_shared(db)
    has_own = own_row is not None
    has_shared = shared_row is not None
    # Precedence: own > shared > none, unless the user prefers shared and one
    # exists (prefer_shared honoured only when there's a shared to fall back to).
    prefers_shared = has_own and own_row.prefer_shared and has_shared
    mode = "shared" if prefers_shared else "own" if has_own else "shared" if has_shared else "none"
    return {
        "hasOwn": has_own,
        "hasShared": has_shared,
        "mode": mode,
        "own": _meta_for(own_row),
        "shared": _meta_for(shared_row, with_assigned_users=True, db=db),
    }


def resolve_ambient_owner_id() -> int | None:
    """Best-effort resolve the user id to attribute for the in-flight Claude CLI call.

    Claude CLI calls happen deep in :mod:`app.services.claude_cli`, invoked from
    background worker threads that have no request/user object in scope. Two
    ambient mechanisms carry the owner:

    * An explicit ambient **owner** (``run_context.owner_scope``) — set by
      background work that has no run to derive an owner from (e.g. a knowledge
      build). It takes priority so that work resolves its owner's own/preferred
      credentials rather than the shared fallback.
    * Otherwise the ambient **run**'s ``owner_id`` — run-scoped workers set the
      run id so per-run cost can be attributed, and we resolve its owner here.

    Returns ``None`` when neither is set, the run can't be found, or it has no
    owner (pre-ownership data or shared/local-first use) — callers then fall back
    to the shared credential and unattributed usage, matching today's behavior.
    """
    from app.services import run_context

    owner_id = run_context.get_owner()
    if owner_id is not None:
        return owner_id

    run_id = run_context.get_run()
    if run_id is None:
        return None
    from app.db import SessionLocal
    from app.models.run import Run

    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        return run.owner_id if run is not None else None
    finally:
        db.close()


def _config_dir_for(key: str) -> Path:
    return settings.workspace_dir / "claude-config" / key


def _lock_down(path: Path, *, is_dir: bool) -> None:
    """Best-effort restrict permissions to the owner (no-op on failure/platforms
    where chmod has no effect, e.g. Windows) — never fatal."""
    try:
        path.chmod(stat.S_IRWXU if is_dir else (stat.S_IRUSR | stat.S_IWUSR))
    except OSError:
        pass


def _write_config_dir(key: str, contents: str) -> Path:
    """Write ``contents`` to ``workspace/claude-config/<key>/.credentials.json``.

    The single writer behind :func:`materialize` (stored, encrypted rows) and
    :func:`materialize_raw` (material handed to us in the clear, e.g. resolved
    from EmeHub — #499), so both produce a byte-identical, owner-only config dir.
    Returns the directory (not the file): that is what ``CLAUDE_CONFIG_DIR``
    points at.
    """
    config_dir = _config_dir_for(key)
    config_dir.mkdir(parents=True, exist_ok=True)
    _lock_down(config_dir, is_dir=True)

    creds_path = config_dir / ".credentials.json"
    creds_path.write_text(contents, encoding="utf-8")
    _lock_down(creds_path, is_dir=False)
    return config_dir


def materialize(row: ClaudeCredentials, key: str) -> Path:
    """Decrypt ``row.credentials`` and write it to ``workspace/claude-config/<key>/.credentials.json``.

    Returns the directory (not the file) — that's the value ``CLAUDE_CONFIG_DIR``
    is set to. Rewrites the file on every call so an updated stored credential
    (re-upload) always takes effect on the next CLI invocation.
    """
    decrypted = crypto.decrypt(row.credentials)
    if decrypted is None:
        raise ClaudeCredentialsError(f"stored credentials for '{key}' could not be decrypted")
    return _write_config_dir(key, decrypted)


def materialize_raw(raw_credentials: str, key: str) -> Path:
    """Write already-plaintext ``.credentials.json`` material for ``key``.

    Same output as :func:`materialize`, for material that never lived in our
    table — today only the hub-resolved credential (#499). Validated as JSON
    first so a garbled payload fails here rather than as an opaque CLI error.

    ``raw_credentials`` is a secret: it is written to an owner-only file and is
    never logged or returned.
    """
    return _write_config_dir(key, _validate(raw_credentials))


# ------------------------------------------------------- hub-resolved (per-run)
# A run that resolved its credential from EmeHub materialises it under its own
# key, so the background pipeline reads a *file* and never calls the hub
# mid-run (hub agent tokens live 15 minutes and are session-bound — #497 §4b).
_HUB_RUN_PREFIX = "hub-run-"


def hub_run_key(run_id: int) -> str:
    """Config-dir key for the credential a run resolved from the hub."""
    return f"{_HUB_RUN_PREFIX}{run_id}"


def hub_run_config_dir(run_id: int) -> Path | None:
    """The hub-resolved config dir for ``run_id``, or ``None`` if there isn't one.

    A pure filesystem check — deliberately no hub call, so this is safe on the
    background worker threads that actually invoke the CLI.
    """
    config_dir = _config_dir_for(hub_run_key(run_id))
    return config_dir if (config_dir / ".credentials.json").is_file() else None


def discard_hub_run_credential(run_id: int) -> None:
    """Remove any hub-resolved credential previously materialised for ``run_id``.

    Called when a run start falls back to the local credential, so a *stale* dir
    from an earlier attempt can never be picked up silently. Best-effort.
    """
    creds_path = _config_dir_for(hub_run_key(run_id)) / ".credentials.json"
    try:
        creds_path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - permissions/locking; not fatal
        pass


def resolve_effective_config_dir(db: Session, owner_id: int | None) -> Path | None:
    """Resolve + materialize the effective credentials dir for ``owner_id``.

    Precedence: the user's own credential, else the shared/admin credential,
    else ``None`` (no credential configured at all — the caller must raise a
    clean error; there is no interactive ``claude login`` fallback per ADR 0001).
    A user who set ``prefer_shared`` on their own row skips it and uses the
    shared credential (see :func:`_own_yields_to_shared`).
    """
    if owner_id is not None:
        own = get_own(db, owner_id)
        if own is not None and not _own_yields_to_shared(db, own):
            return materialize(own, str(owner_id))
    shared = get_shared(db)
    if shared is not None:
        return materialize(shared, "shared")
    return None
