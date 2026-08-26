"""Read REAL Claude Code usage from local session transcripts.

LOCAL-ONLY: this reads the Claude Code session logs stored on THIS machine under
``~/.claude/projects/<slug>/<sessionId>.jsonl`` — the same data the CLI's
``/usage`` command reports. It aggregates **all** local Claude sessions on the
machine (every project), reads local files only, and never transmits anything.

Each transcript line is a JSON record; assistant API responses carry
``message.model`` and ``message.usage`` (``input_tokens`` / ``output_tokens`` /
``cache_creation_input_tokens`` → cacheWrite / ``cache_read_input_tokens`` →
cacheRead), a top-level ISO ``timestamp`` ('Z'), and a message id
(``message.id`` and/or top-level ``uuid``). Duplicate lines for the same message
occur, so we DEDUP by message id. Per-message cost is not stored by the CLI
(``costUSD`` is null), so cost is computed from token counts × a per-model price
table (see ``_PRICES``).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import settings as app_settings
from app.services import model_catalog
from app.logging import logger
from app.services.ai_usage_service import MODEL_LABELS, _current_model

# --- Pricing -----------------------------------------------------------------
# USD per 1,000,000 tokens. Sourced from the `claude-api` skill (Current Models
# pricing table + the prompt-caching economics section): cache-read ≈ 0.1× the
# input price, cache-write ≈ 1.25× the input price (5-minute TTL — the CLI's
# default). Sonnet uses the standard (non-introductory) list price. Models absent
# from this table contribute 0 to cost (we don't guess prices).
_MTOK = 1_000_000
# From `model_catalog`, the single source of truth (#715). This used to be a second
# hardcoded table: Sonnet 5 was priced at $3/$15 instead of $2/$10, so every Sonnet cost
# the product displayed was overstated by half, and Opus 5 was missing entirely — which,
# by the "absent contributes 0" rule below, reported the current flagship as free.
_PRICES: dict[str, dict[str, float]] = model_catalog.prices()

_SESSION_WINDOW = timedelta(hours=5)
_CACHE_TTL_S = 60.0

# In-process (timestamp, result) cache. The frontend polls ~every 30s and a full
# scan of the transcripts is comparatively expensive, so we reuse the computed
# result within a short TTL.
_cache: tuple[float, dict[str, Any]] | None = None

# --- Plan limit % (from the CLI's own `/usage`) ------------------------------
# The session/week limit percentages come from the exact same source as the
# CLI's `/usage` view: a live authenticated call the CLI makes. We can't reach
# that endpoint ourselves, but we CAN drive the CLI to produce it by piping
# `/usage` to it and parsing the rendered output. Spawning the CLI is slow
# (~10–40s), so we cache the parsed result and refresh it in a BACKGROUND thread
# — `read_stats()` never blocks on it. `limitsStatus` tells the UI whether to
# show a skeleton ("loading"), the real bars ("ready"), or fall back ("unavailable").
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_SESSION_RE = re.compile(r"Current session:\s*(\d+)%\s*used(?:[^\n]*?resets\s*([^\n]+))?", re.I)
_WEEK_RE = re.compile(r"Current week \(all models\):\s*(\d+)%\s*used(?:[^\n]*?resets\s*([^\n]+))?", re.I)
_LIMITS_TTL_S = 180.0
_LIMITS_USAGE_TIMEOUT_S = 45

# (monotonic_ts, parsed | None). parsed = {"session": {...}, "week": {...}}.
_limits_cache: tuple[float, dict[str, Any] | None] | None = None
_limits_refreshing = False
_limits_lock = threading.Lock()

# The OAuth usage endpoint the CLI's `/usage` view itself consumes. Called with a
# credential's bearer token it returns the plan-limit windows as JSON
# (`five_hour` → session, `seven_day` → week) — deterministic, headless-safe
# (works in Docker where no interactive `~/.claude` login exists), and
# per-credential. Preferred over scraping the TUI.
_USAGE_API_URL = "https://api.anthropic.com/api/oauth/usage"
_USAGE_API_BETA = "oauth-2025-04-20"
_USAGE_API_TIMEOUT_S = 15


def _transcript_roots() -> list[Path]:
    """Every dir that may hold Claude session transcripts under ``projects/``.

    Since each user's (and the shared) credential is materialized into its own
    ``CLAUDE_CONFIG_DIR`` (``workspace/claude-config/<key>``), the CLI writes its
    session logs THERE, not in ``~/.claude``. We scan the legacy machine-wide
    ``claude_home`` plus every per-credential config dir so usage is captured
    regardless of which credential a call ran under."""
    roots = [app_settings.claude_home]
    config_root = app_settings.workspace_dir / "claude-config"
    if config_root.is_dir():
        roots.extend(child for child in config_root.iterdir() if child.is_dir())
    return roots


def _usage_config_dir() -> Path | None:
    """A materialized credential config dir to run the CLI's ``/usage`` under, so
    that authenticated plan-limit read works (prefers the shared account, else
    the first credential on disk). None when nothing is configured."""
    config_root = app_settings.workspace_dir / "claude-config"
    shared = config_root / "shared"
    if (shared / ".credentials.json").is_file():
        return shared
    if config_root.is_dir():
        for child in sorted(config_root.iterdir()):
            if (child / ".credentials.json").is_file():
                return child
    return None


def _scrape_usage(cfg: Path | None) -> dict[str, Any] | None:
    """Spawn `claude` under ``cfg``, pipe `/usage`, and parse the session/week %.

    ``cfg`` sets ``CLAUDE_CONFIG_DIR`` (None inherits the ambient env). Pipes
    `/usage` to the CLI's stdin (the interactive command), strips ANSI, and
    regex-parses the two limit lines. Returns None on any failure/parse miss.
    """
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(cfg)} if cfg is not None else None
    try:
        proc = subprocess.run(  # noqa: S603
            [app_settings.claude_bin],
            input="/usage\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_LIMITS_USAGE_TIMEOUT_S,
            env=env,
        )
    except Exception as exc:  # noqa: BLE001 - never break stats on a CLI hiccup
        logger.warning("Claude /usage read failed: {}", exc)
        return None
    out = _ANSI_RE.sub("", proc.stdout or "")
    sm = _SESSION_RE.search(out)
    wm = _WEEK_RE.search(out)
    if not sm and not wm:
        return None
    return {
        "session": {
            "pctUsed": int(sm.group(1)) if sm else -1,
            "resetLabel": (sm.group(2) or "").strip() if sm else "",
        },
        "week": {
            "pctUsed": int(wm.group(1)) if wm else -1,
            "resetLabel": (wm.group(2) or "").strip() if wm else "",
        },
    }


def _run_cli_usage() -> dict[str, Any] | None:
    """Drive `claude` to emit its `/usage` view and parse the session/week %.

    Tries the selected credential's materialized config dir first (so the % maps
    to the account in effect), then falls back to the machine's default
    ``~/.claude`` login. The fallback matters because the CLI only renders the
    interactive `/usage` view under a fully set-up config: a per-credential
    materialized dir typically renders nothing (it exits with just the session
    summary), whereas the default login renders it reliably — restoring the
    behavior that predated the ``CLAUDE_CONFIG_DIR`` override (#157). Returns
    None only if neither target produces a parseable result.
    """
    seen: set[str] = set()
    for target in (_usage_config_dir(), app_settings.claude_home):
        if target is None or str(target) in seen:
            continue
        seen.add(str(target))
        parsed = _scrape_usage(target)
        if parsed is not None:
            return parsed
    return None


def _credential_candidates() -> list[Path]:
    """Config dirs holding a ``.credentials.json`` to source the plan-limit %
    from: the selected credential's materialized dir first (shared-preferred,
    matching ``_usage_config_dir``), then every other materialized credential
    dir, then the machine's default ``claude_home``."""
    ordered: list[Path] = []
    preferred = _usage_config_dir()
    if preferred is not None:
        ordered.append(preferred)
    config_root = app_settings.workspace_dir / "claude-config"
    if config_root.is_dir():
        for child in sorted(config_root.iterdir()):
            if child not in ordered and (child / ".credentials.json").is_file():
                ordered.append(child)
    home = app_settings.claude_home
    if home not in ordered and (home / ".credentials.json").is_file():
        ordered.append(home)
    return ordered


def _read_live_access_token(cfg: Path) -> str | None:
    """The OAuth access token from ``cfg/.credentials.json`` if it is still
    live, else None. Expired tokens are skipped — never refreshed here — the
    CLI refreshes and rewrites the file on its next real run under that dir."""
    try:
        raw = json.loads((cfg / ".credentials.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    oauth = raw.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    token = oauth.get("accessToken")
    try:
        expires_ms = float(oauth.get("expiresAt") or 0)
    except (TypeError, ValueError):
        expires_ms = 0
    if not token or time.time() * 1000 >= expires_ms:
        return None
    return str(token)


def _parse_usage_payload(payload: Any) -> dict[str, Any] | None:
    """Map the usage endpoint's JSON to the parsed-limits shape, or None.

    ``five_hour`` → session and ``seven_day`` → week; each window contributes
    ``pctUsed`` (rounded ``utilization``) and, when present, the authoritative
    ``resetsAt`` ISO timestamp (overlaying the transcript-derived estimate).
    """
    if not isinstance(payload, dict):
        return None

    def window(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict) or raw.get("utilization") is None:
            return {}
        try:
            out: dict[str, Any] = {"pctUsed": int(round(float(raw["utilization"])))}
        except (TypeError, ValueError):
            return {}
        if raw.get("resets_at"):
            out["resetsAt"] = str(raw["resets_at"])
        return out

    session = window(payload.get("five_hour"))
    week = window(payload.get("seven_day"))
    if not session and not week:
        return None
    return {"session": session, "week": week}


def _fetch_usage_api(token: str) -> dict[str, Any] | None:
    """GET the OAuth usage endpoint with ``token`` and parse it, or None."""
    req = urllib.request.Request(  # noqa: S310 - fixed https URL
        _USAGE_API_URL,
        headers={"Authorization": f"Bearer {token}", "anthropic-beta": _USAGE_API_BETA},
    )
    try:
        with urllib.request.urlopen(req, timeout=_USAGE_API_TIMEOUT_S) as resp:  # noqa: S310
            payload = json.load(resp)
    except Exception as exc:  # noqa: BLE001 - never break stats on a network hiccup
        logger.warning("Claude usage API read failed: {}", exc)
        return None
    return _parse_usage_payload(payload)


def _fetch_limits() -> dict[str, Any] | None:
    """Plan-limit % — usage API first (per-credential, headless-safe), then the
    TUI scrape as last resort. First candidate with a live token that yields a
    parseable result wins."""
    for cfg in _credential_candidates():
        token = _read_live_access_token(cfg)
        if token is None:
            continue
        parsed = _fetch_usage_api(token)
        if parsed is not None:
            return parsed
    return _run_cli_usage()


def _refresh_limits() -> None:
    global _limits_cache, _limits_refreshing
    try:
        parsed = _fetch_limits()
        _limits_cache = (time.monotonic(), parsed)
    finally:
        _limits_refreshing = False


def _get_limits(force: bool = False) -> tuple[dict[str, Any] | None, str]:
    """Return (parsed_or_None, status). Never blocks — refreshes in the background.

    status: "loading" until the first fetch completes, then "ready" (parsed) or
    "unavailable" (fetch/parse failed). ``force`` (manual refresh) drops the
    cached value so a fresh CLI `/usage` read is kicked off and the status flips
    back to "loading" until it lands.
    """
    global _limits_refreshing, _limits_cache
    now = time.monotonic()
    if force:
        _limits_cache = None
    stale = _limits_cache is None or (now - _limits_cache[0]) >= _LIMITS_TTL_S
    if stale:
        with _limits_lock:
            if not _limits_refreshing:
                _limits_refreshing = True
                threading.Thread(target=_refresh_limits, daemon=True).start()
    if _limits_cache is None:
        return None, "loading"
    parsed = _limits_cache[1]
    return parsed, ("ready" if parsed else "unavailable")


def _int(value: Any) -> int:
    """Best-effort non-negative int coercion for a token count."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _parse_ts(raw: Any) -> datetime | None:
    """Parse an ISO-8601 'Z' timestamp into an aware datetime, or None."""
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _empty_kinds() -> dict[str, int]:
    return {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}


def _cost(model: str, kinds: dict[str, int]) -> float:
    """Compute USD cost for one model's token sums via the price table."""
    price = _PRICES.get(model)
    if not price:
        return 0.0
    return (
        kinds["input"] * price["input"]
        + kinds["output"] * price["output"]
        + kinds["cacheRead"] * price["cacheRead"]
        + kinds["cacheWrite"] * price["cacheWrite"]
    ) / _MTOK


def _iso_z(dt: datetime) -> str:
    """Format an aware datetime as an ISO-8601 'Z' (UTC) string."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _window_summary(
    models: dict[str, dict[str, int]], requests: int, resets_at: str
) -> dict[str, Any]:
    """Roll per-model token sums into a window summary (all-model totals)."""
    total_tokens = 0
    total_cost = 0.0
    for model, kinds in models.items():
        total_tokens += sum(kinds.values())
        total_cost += _cost(model, kinds)
    return {
        "costUsd": round(total_cost, 2),
        "tokens": total_tokens,
        "requests": requests,
        "resetsAt": resets_at,
        "pctUsed": -1,  # overlaid from the CLI's /usage in read_stats (-1 = unknown)
        "resetLabel": "",
    }


def _compute() -> dict[str, Any]:
    """Scan the local transcripts and build the ``/ai/stats`` contract dict."""
    from app.services import claude_cli  # local import avoids load-order coupling

    cur_model = _current_model()
    cur_label, cur_ctx = MODEL_LABELS.get(cur_model, (cur_model, "—"))

    now_local = datetime.now().astimezone()
    today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())
    session_start = now_local - _SESSION_WINDOW
    # Only files touched at/after the earlier of the two window starts can hold
    # relevant records (the 5h session window can precede Monday 00:00 early in
    # the week).
    file_cutoff = min(week_start, session_start)

    # Next Monday 00:00 UTC — the ISO-week reset boundary.
    now_utc = datetime.now(timezone.utc)
    today_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    days_ahead = 7 - now_utc.weekday()  # Mon=0 -> 7 (always the *next* Monday)
    week_resets_at = (today_utc + timedelta(days=days_ahead)).strftime("%Y-%m-%dT%H:%M:%SZ")

    week_models: dict[str, dict[str, int]] = defaultdict(_empty_kinds)
    session_models: dict[str, dict[str, int]] = defaultdict(_empty_kinds)
    week_requests = 0
    session_requests = 0
    session_earliest: datetime | None = None
    seen: set[str] = set()

    for root in _transcript_roots():
        projects_dir = root / "projects"
        if not projects_dir.is_dir():
            continue
        for path in projects_dir.rglob("*.jsonl"):
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
            if mtime < file_cutoff:
                continue
            try:
                handle = path.open("r", encoding="utf-8")
            except OSError:
                continue
            with handle:
                for line in handle:
                    if '"usage"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(rec, dict):
                        continue
                    msg = rec.get("message")
                    if not isinstance(msg, dict):
                        continue
                    usage = msg.get("usage")
                    if not isinstance(usage, dict):
                        continue

                    mid = msg.get("id") or rec.get("uuid")
                    if mid is not None:
                        if mid in seen:
                            continue  # DEDUP: same assistant message already counted
                        seen.add(mid)

                    ts = _parse_ts(rec.get("timestamp"))
                    if ts is None:
                        continue
                    in_week = ts >= week_start
                    in_session = ts >= session_start
                    if not (in_week or in_session):
                        continue

                    model = msg.get("model") or "unknown"
                    kinds = {
                        "input": _int(usage.get("input_tokens")),
                        "output": _int(usage.get("output_tokens")),
                        "cacheRead": _int(usage.get("cache_read_input_tokens")),
                        "cacheWrite": _int(usage.get("cache_creation_input_tokens")),
                    }
                    if in_week:
                        acc = week_models[model]
                        for k, v in kinds.items():
                            acc[k] += v
                        week_requests += 1
                    if in_session:
                        acc = session_models[model]
                        for k, v in kinds.items():
                            acc[k] += v
                        session_requests += 1
                        if session_earliest is None or ts < session_earliest:
                            session_earliest = ts

    session_resets_at = _iso_z((session_earliest or now_local) + _SESSION_WINDOW)

    cur_kinds = week_models.get(cur_model, _empty_kinds())
    by_model = sorted(
        (
            {
                "model": model,
                "modelLabel": MODEL_LABELS.get(model, (model, "—"))[0],
                "input": kinds["input"],
                "output": kinds["output"],
                "cacheRead": kinds["cacheRead"],
                "cacheWrite": kinds["cacheWrite"],
                "costUsd": round(_cost(model, kinds), 2),
            }
            for model, kinds in week_models.items()
        ),
        key=lambda m: m["costUsd"],
        reverse=True,
    )

    return {
        "model": cur_model,
        "modelLabel": cur_label,
        "operational": claude_cli.is_available(),
        "ctxWindow": cur_ctx,
        "session": _window_summary(session_models, session_requests, session_resets_at),
        "week": _window_summary(week_models, week_requests, week_resets_at),
        "breakdown": {
            "input": cur_kinds["input"],
            "output": cur_kinds["output"],
            "cacheRead": cur_kinds["cacheRead"],
            "cacheWrite": cur_kinds["cacheWrite"],
        },
        "byModel": by_model,
    }


def _compute_zero() -> dict[str, Any]:
    """A well-formed all-zero stats dict used as the defensive fallback."""
    now_local = datetime.now().astimezone()
    cur_model = "unknown"
    try:
        cur_model = _current_model()
    except Exception:  # noqa: BLE001
        pass
    label, ctx = MODEL_LABELS.get(cur_model, (cur_model, "—"))
    now_utc = datetime.now(timezone.utc)
    today_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    week_resets = (today_utc + timedelta(days=7 - now_utc.weekday())).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    zero = {"costUsd": 0.0, "tokens": 0, "requests": 0, "pctUsed": -1, "resetLabel": ""}
    return {
        "model": cur_model,
        "modelLabel": label,
        "operational": False,
        "ctxWindow": ctx,
        "session": {**zero, "resetsAt": _iso_z(now_local + _SESSION_WINDOW)},
        "week": {**zero, "resetsAt": week_resets},
        "breakdown": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "byModel": [],
    }


def read_stats(force: bool = False) -> dict[str, Any]:
    """Return real local Claude usage as the ``GET /ai/stats`` contract dict.

    Never raises: a malformed transcript, missing log directory, or unreadable
    file yields a well-formed (all-zero if necessary) stats dict. Results are
    cached in-process for ~60s; ``force`` (manual refresh) bypasses both the
    transcript-scan cache and the plan-limit cache.
    """
    global _cache
    now = time.monotonic()
    if force:
        _cache = None
    if _cache is not None and now - _cache[0] < _CACHE_TTL_S:
        base = _cache[1]
    else:
        try:
            base = _compute()
        except Exception as exc:  # noqa: BLE001 - stats must never break the endpoint
            logger.warning("Claude usage read failed: {}", exc)
            base = _compute_zero()
        _cache = (now, base)

    # Overlay the real plan-limit % from the CLI's /usage (background-refreshed,
    # never blocks). Build a shallow copy so the cached compute isn't mutated.
    limits, status = _get_limits(force=force)
    return {
        **base,
        "limitsStatus": status,
        "session": {**base["session"], **((limits or {}).get("session", {}))},
        "week": {**base["week"], **((limits or {}).get("week", {}))},
    }
