"""Claude CLI integration (local execution).

Wraps the locally-installed Claude Code CLI in headless "print" mode to run
one-shot prompts for requirement analysis, test-case generation and Playwright
spec generation.

Invocation shape::

    claude -p "<prompt>" --output-format json --model <model>

In JSON output mode the CLI prints an envelope whose ``result`` field carries the
assistant's final text. We extract that, and for structured calls we ask the
model to emit a fenced JSON block and parse it.

Per ADR 0001 there is **no simulated fallback**: if the CLI is missing, errors,
or times out, we raise :class:`ClaudeError` and the caller surfaces it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from app.config import settings
from app.logging import logger


class ClaudeError(RuntimeError):
    """Raised when the Claude CLI is unavailable or returns an error."""


# Per-action model defaults (#175): small, mechanical structured-JSON tasks run
# on a cheaper/faster model; heavy generation/review inherit the global model.
# Keyed by skill name; overridable per skill via the "skillModels" settings map.
_HAIKU = "claude-haiku-4-5-20251001"
_DEFAULT_SKILL_MODELS = {
    "execution-analyzer": _HAIKU,        # failure classification (runs up to 3x per heal)
    "screenshot-annotator": _HAIKU,      # bounding-box / caption extraction
    "ticket-comment-generator": _HAIKU,  # short prose summary
}


def _resolve_model(skill: str | None = None) -> str:
    """Return the Claude model for a call, optionally specialized by ``skill``.

    Resolution order (#175): an explicit per-skill override in the settings
    ``skillModels`` map, then the built-in :data:`_DEFAULT_SKILL_MODELS` default
    for that skill, then the operator-selected global ``claudeModel``, then
    :attr:`settings.claude_model`. Heavy actions inherit the global model while
    cheap mechanical actions default to Haiku — with no per-caller changes,
    since the skill is already threaded through every call.
    """
    from app.services import settings_store  # local import avoids load-order coupling

    stored = settings_store.load_settings()
    if skill:
        override = (stored.get("skillModels") or {}).get(skill)
        if override:
            return override
        if skill in _DEFAULT_SKILL_MODELS:
            return _DEFAULT_SKILL_MODELS[skill]
    return stored.get("claudeModel") or settings.claude_model


def _extract_json(text: str) -> Any:
    """Pull the first JSON object/array out of a model response."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        # Fall back to the first {...} or [...] span in the text.
        span = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
        candidate = span.group(1) if span else text
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:  # noqa: TRY003
        raise ClaudeError(f"Claude returned non-JSON output: {exc}") from exc


def _compose_system(system: str | None, skill: str | None, include_template: bool) -> str | None:
    """Merge an explicit system prompt with a dedicated skill's SKILL.md."""
    if not skill:
        return system
    from app.services.skills import load_skill  # local import avoids any load-order coupling

    skill_text = load_skill(skill, include_template=include_template)
    if not skill_text:
        return system
    return f"{skill_text}\n\n{system}" if system else skill_text


_USAGE_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)

# Set on the first call to `_record_usage` in this process (see
# `_log_envelope_shape_once`) — a one-time diagnostic so operators can confirm the
# real envelope shape without logging on every call.
_envelope_shape_logged = False


def _log_envelope_shape_once(envelope: dict) -> None:
    """Log the envelope's top-level (and ``usage``/``modelUsage``) keys once per
    process (#171), so operators can confirm what the installed CLI actually
    returns without spamming the log on every call. Logs keys only — never the
    prompt/response text. Best-effort: never raises.
    """
    global _envelope_shape_logged
    if _envelope_shape_logged:
        return
    _envelope_shape_logged = True
    try:
        usage = envelope.get("usage")
        model_usage = envelope.get("modelUsage")
        logger.info(
            "Claude CLI envelope shape (once/process): top_keys={} usage_keys={} "
            "modelUsage_models={}",
            sorted(envelope.keys()),
            sorted(usage.keys()) if isinstance(usage, dict) else usage,
            sorted(model_usage.keys()) if isinstance(model_usage, dict) else model_usage,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic only, must never raise
        logger.warning("Claude envelope shape log skipped: {}", exc)


def _usage_from_model_usage(model_usage: Any) -> dict[str, int] | None:
    """Sum token counts across a newer-CLI ``modelUsage`` envelope into the legacy
    ``usage`` shape (#171).

    Some Claude CLI versions nest per-call token usage under a top-level
    ``modelUsage`` dict keyed by model id (each value using camelCase keys, e.g.
    ``inputTokens``) instead of a single top-level ``usage`` dict. Returns the
    summed totals in the legacy snake_case shape, or ``None`` if ``model_usage``
    isn't a non-empty dict of per-model stats.
    """
    if not isinstance(model_usage, dict) or not model_usage:
        return None
    totals = {key: 0 for key in _USAGE_TOKEN_KEYS}
    found = False
    for stats in model_usage.values():
        if not isinstance(stats, dict):
            continue
        totals["input_tokens"] += int(stats.get("inputTokens") or stats.get("input_tokens") or 0)
        totals["output_tokens"] += int(stats.get("outputTokens") or stats.get("output_tokens") or 0)
        totals["cache_read_input_tokens"] += int(
            stats.get("cacheReadInputTokens") or stats.get("cache_read_input_tokens") or 0
        )
        totals["cache_creation_input_tokens"] += int(
            stats.get("cacheCreationInputTokens") or stats.get("cache_creation_input_tokens") or 0
        )
        found = True
    return totals if found else None


def _cost_from_model_usage(model_usage: Any) -> float | None:
    """Sum per-model ``costUSD``/``cost_usd`` across a ``modelUsage`` envelope
    (#171), for CLI versions that omit the top-level ``total_cost_usd``. Returns
    ``None`` if there's nothing to sum."""
    if not isinstance(model_usage, dict) or not model_usage:
        return None
    total = 0.0
    found = False
    for stats in model_usage.values():
        if not isinstance(stats, dict):
            continue
        cost = stats.get("costUSD", stats.get("cost_usd"))
        if isinstance(cost, (int, float)):
            total += float(cost)
            found = True
    return total if found else None


def _record_usage(
    envelope: dict | None, *, model: str, action: str, wall_ms: int, owner_id: int | None
) -> None:
    """Best-effort: log a successful call's real token/cost/latency usage.

    Parses the CLI's JSON result envelope for ``total_cost_usd``, ``usage`` token
    counts and ``duration_ms`` (falling back to the measured wall-clock time), and
    hands them to :func:`ai_usage_service.record`. If the top-level ``usage``
    dict is missing or all-zero, falls back to summing ``modelUsage`` (#171) — a
    shape some newer Claude CLI versions use instead. ``owner_id`` stamps the row
    for per-user cost attribution (#95) — the same user whose credentials the
    call ran under (see :func:`_resolve_claude_env`). Wrapped so a logging
    failure can never break the Claude call it observes.
    """
    try:
        from app.services import ai_usage_service

        env = envelope or {}
        if env:
            _log_envelope_shape_once(env)
        usage = env.get("usage") or {}
        if not isinstance(usage, dict) or not any(usage.get(key) for key in _USAGE_TOKEN_KEYS):
            fallback = _usage_from_model_usage(env.get("modelUsage"))
            if fallback:
                usage = fallback
        cost = env.get("total_cost_usd")
        if not isinstance(cost, (int, float)) or cost == 0:
            cost = _cost_from_model_usage(env.get("modelUsage")) or cost or 0.0
        duration = env.get("duration_ms")
        ai_usage_service.record(
            model=model,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read=usage.get("cache_read_input_tokens", 0),
            cache_write=usage.get("cache_creation_input_tokens", 0),
            cost_usd=cost,
            duration_ms=int(duration) if isinstance(duration, (int, float)) else wall_ms,
            action=action,
            owner_id=owner_id,
        )
    except Exception as exc:  # noqa: BLE001 - usage capture is additive + best-effort
        logger.warning("Claude usage capture skipped: {}", exc)


def _resolve_cwd(cwd: str | Path | None) -> str | None:
    """Return an existing directory to run the CLI in, or None (inherit ours)."""
    if not cwd:
        return None
    path = Path(cwd)
    return str(path) if path.is_dir() else None


def _resolve_claude_env() -> tuple[dict[str, str], int | None]:
    """Resolve the effective Claude credentials and build the subprocess env.

    Resolves the current owner (see
    :func:`app.services.claude_credentials.resolve_ambient_owner_id`), then that
    user's own credential, else the shared/admin credential (#95), materializes
    it into a private config dir, and returns ``(env, owner_id)`` where ``env``
    points ``CLAUDE_CONFIG_DIR`` at that dir. ``owner_id`` is also returned so the
    caller can stamp the usage row with the same user. Raises :class:`ClaudeError`
    when no credential is configured at all — there is no interactive
    ``claude login`` fallback (ADR 0001).

    When the ambient run resolved its credential from EmeHub at run start (#499),
    that already-materialized dir wins. This is a **filesystem** check — the hub
    is never called from here, because here is a background worker thread with no
    fresh hub token (agent tokens live 15 minutes and are session-bound).
    """
    from app.db import SessionLocal
    from app.services import claude_credentials, hub_client, run_context

    owner_id = claude_credentials.resolve_ambient_owner_id()

    if hub_client.enabled():
        run_id = run_context.get_run()
        if run_id is not None:
            hub_dir = claude_credentials.hub_run_config_dir(run_id)
            if hub_dir is not None:
                return {**os.environ, "CLAUDE_CONFIG_DIR": str(hub_dir)}, owner_id

    db = SessionLocal()
    try:
        config_dir = claude_credentials.resolve_effective_config_dir(db, owner_id)
    finally:
        db.close()
    if config_dir is None:
        raise ClaudeError(
            "No Claude credentials configured. Upload your own credentials in "
            "Settings, or ask an admin to configure the shared credential."
        )
    return {**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir)}, owner_id


def _mark_credential_invalid(owner_id: int | None) -> None:
    """Best-effort: flag the effective credential ``expired`` after a call failed
    with an auth error, so the header/AI-stats reflect it without a separate
    probe (Layer 1). Never raises."""
    from app.db import SessionLocal
    from app.models.claude_credentials import STATUS_EXPIRED
    from app.services import claude_credentials

    try:
        db = SessionLocal()
        try:
            claude_credentials.set_effective_status(db, owner_id, STATUS_EXPIRED)
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not flag Claude credential invalid: {}", exc)


def _persist_refreshed_credential(owner_id: int | None) -> None:
    """Best-effort: capture any token the CLI just refreshed back into the store
    (see :func:`app.services.claude_credentials.persist_refreshed`). Never raises
    — credential bookkeeping must not fail a CLI run."""
    from app.db import SessionLocal
    from app.services import claude_credentials

    try:
        db = SessionLocal()
        try:
            claude_credentials.persist_refreshed(db, owner_id)
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not persist refreshed Claude credential: {}", exc)


# Substrings (lowercased) in a failed CLI call's output that mean the Claude
# credential is invalid/expired — covers both the local-token phrasing and a
# rejected-token API 401.
_AUTH_ERROR_MARKERS = (
    "not logged in",
    "please run /login",
    "invalid authentication credentials",
    "failed to authenticate",
    "api error: 401",
)


def run_prompt(
    prompt: str,
    *,
    system: str | None = None,
    skill: str | None = None,
    include_template: bool = False,
    timeout: int | None = None,
    label: str | None = None,
    cwd: str | Path | None = None,
    model: str | None = None,
    allowed_tools: list[str] | None = None,
    add_dir: str | Path | None = None,
    max_budget_usd: float | None = None,
    skip_permissions: bool = False,
    extra_env: dict[str, str] | None = None,
) -> str:
    """Run a single prompt through the Claude CLI and return its text result.

    If ``skill`` is given, that dedicated Q-Agent skill's SKILL.md is injected as
    the system prompt so the action follows the skill's methodology. If ``cwd`` is
    an existing directory, the CLI runs there so its file tools can traverse that
    codebase (used by project-bootstrap against a local repo clone). An explicit
    ``model`` overrides the skill/global resolution (#398 — the heal fixer forces
    a fast model without changing the skill's default for fresh generation).

    Agentic tool use (#400): the default call is a pure, non-agentic completion —
    ``allowed_tools`` is ``None`` and no tool/permission flags are added, so every
    existing caller is byte-for-byte unchanged. When ``allowed_tools`` is given the
    CLI runs as a Bash/file-capable agent (``--allowedTools`` allowlist), and
    ``skip_permissions`` adds ``--dangerously-skip-permissions`` so tool calls
    execute without hanging on a prompt in headless mode (there is no TTY). This
    single exec path is reused (not duplicated) by :func:`run_agentic`. Even with
    tools, ``--output-format json`` still blocks until the whole agentic loop
    finishes and emits one envelope with ``result``/``usage``/``total_cost_usd``,
    so the parser and :func:`_record_usage` below are unchanged. ``add_dir`` scopes
    file tools to a workspace dir; ``max_budget_usd`` sets the CLI's native hard
    dollar ceiling for the whole agentic run (``--max-budget-usd``).
    """
    system = _compose_system(system, skill, include_template)
    model = model or _resolve_model(skill)
    cmd = [
        settings.claude_bin,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--model",
        model,
    ]
    if system:
        cmd += ["--append-system-prompt", system]
    if allowed_tools:
        # --allowedTools takes a variadic <tools...> list (space-separated), so
        # pass each tool as its own arg; commander stops collecting at the next flag.
        cmd += ["--allowedTools", *allowed_tools]
    if skip_permissions:
        cmd += ["--dangerously-skip-permissions"]
    add_dir_resolved = _resolve_cwd(add_dir)
    if add_dir_resolved:
        cmd += ["--add-dir", add_dir_resolved]
    if max_budget_usd:
        cmd += ["--max-budget-usd", str(max_budget_usd)]
    resolved_cwd = _resolve_cwd(cwd)
    env, owner_id = _resolve_claude_env()
    # Extra env for the child (e.g. BU_CDP_URL so an agentic run's browser-harness
    # attaches to our pre-authenticated Chrome — #400). Merged last so it can't
    # clobber CLAUDE_CONFIG_DIR unless a caller explicitly intends to.
    if extra_env:
        env = {**env, **extra_env}

    # Register the call so operators can observe it live (logs + /ai/activity + WS).
    from app.services import activity, run_context, run_control

    call_id = activity.start(label or skill or "Claude CLI", skill)
    logger.info("Claude CLI: {} chars prompt, model={}", len(prompt), model)
    # Attribute this call to the ambient run so cancelling the run can terminate
    # the in-flight subprocess (below); None for non-run callers (bootstrap/test).
    run_id = run_context.get_run()
    t0 = time.monotonic()
    proc: subprocess.Popen[str] | None = None
    try:
        # Popen (not subprocess.run) so we hold a handle to register with
        # run_control — a run cancel then kills the live CLI process immediately
        # (run_control.kill_processes) instead of waiting for the next ticket
        # checkpoint while a long analysis blocks the worker thread.
        proc = subprocess.Popen(  # noqa: S603
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=resolved_cwd,
            env=env,
        )
        if run_id is not None:
            run_control.register_process(run_id, proc)
        try:
            stdout_text, stderr_text = proc.communicate(
                timeout=timeout or settings.claude_timeout_s
            )
        except subprocess.TimeoutExpired as exc:  # noqa: TRY003
            proc.kill()
            proc.communicate()
            activity.finish(call_id, ok=False, error="timed out")
            raise ClaudeError(
                f"Claude CLI timed out after {timeout or settings.claude_timeout_s}s"
            ) from exc
    except FileNotFoundError as exc:  # noqa: TRY003
        activity.finish(call_id, ok=False, error="Claude CLI not found")
        raise ClaudeError(
            f"Claude CLI not found (looked for '{settings.claude_bin}'). Install it and "
            "authenticate with `claude login`."
        ) from exc
    except ClaudeError:
        raise
    except Exception as exc:  # noqa: BLE001
        activity.finish(call_id, ok=False, error=str(exc)[:200])
        raise
    finally:
        if proc is not None and run_id is not None:
            run_control.unregister_process(run_id, proc)

    # If the CLI refreshed the (short-lived) OAuth access token in-place, capture
    # it back into the store so the credential doesn't silently expire between
    # calls. Guarded + best-effort — never let this bookkeeping break the run.
    _persist_refreshed_credential(owner_id)

    if proc.returncode != 0:
        # `claude -p --output-format json` writes its failure reason (auth /
        # credit / rate-limit / unknown-model) to STDOUT as JSON and leaves
        # STDERR empty — so a bare `exited 1:` message hides the real cause.
        # Surface stdout when stderr is empty, and log both streams in full.
        err = stderr_text.strip()
        out = stdout_text.strip()
        logger.error(
            "Claude CLI exited {}: stderr={!r} stdout={!r}",
            proc.returncode,
            err[:800],
            out[:800],
        )
        activity.finish(call_id, ok=False, error=f"exit {proc.returncode}")
        # Feed an auth failure back into the stored credential so the UI can flag
        # it (Layer 1). The CLI reports auth failures a few ways: "Not logged in ·
        # Please run /login" (expired local token) AND "API Error: 401 Invalid
        # authentication credentials" / "Failed to authenticate" (rejected token).
        # Match all so a real 401 flags the credential (previously only the
        # "/login" phrasing did, so API 401s slipped through) and lands in the run
        # activity log (#394).
        if any(m in (out + err).lower() for m in _AUTH_ERROR_MARKERS):
            _mark_credential_invalid(owner_id)
            from app.services import audit_service

            audit_service.record(
                category="ai", actor_type="system", action="Claude authentication failed",
                target="Claude API credentials", status="error",
                meta="Invalid or expired credentials — update them in Settings, then re-run.",
            )
        detail = (err or out or "no output on stderr/stdout")[:800]
        raise ClaudeError(f"Claude CLI exited {proc.returncode}: {detail}")

    activity.finish(call_id, ok=True)
    wall_ms = int((time.monotonic() - t0) * 1000)
    raw = stdout_text.strip()
    # JSON envelope: {"type":"result","result":"...","usage":{...},"total_cost_usd":...}
    envelope: dict | None = None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            envelope = parsed
    except json.JSONDecodeError:
        pass
    # Capture real per-call usage (tokens/cost/latency) for the stats panel.
    # Record the skill (not the per-call label) as the action so per-run cost
    # attribution can group calls by process; run_id is read from the ambient
    # run context inside ai_usage_service.record.
    _record_usage(
        envelope,
        model=model,
        action=skill or label or "Claude CLI",
        wall_ms=wall_ms,
        owner_id=owner_id,
    )
    if envelope is not None and "result" in envelope:
        return str(envelope["result"])
    return raw


def run_json(
    prompt: str,
    *,
    system: str | None = None,
    skill: str | None = None,
    include_template: bool = False,
    timeout: int | None = None,
    label: str | None = None,
    cwd: str | Path | None = None,
) -> Any:
    """Run a prompt expecting a JSON response and parse it.

    ``skill`` injects a dedicated Q-Agent skill; the JSON-only instruction still
    pins the machine-parseable output shape the backend consumes. ``cwd`` runs the
    CLI in a codebase directory so its file tools can read that project.
    """
    instruction = (
        "\n\nRespond with ONLY a single valid JSON value (object or array). "
        "Do not include prose or markdown fences."
    )
    text = run_prompt(
        prompt + instruction,
        system=system,
        skill=skill,
        include_template=include_template,
        timeout=timeout,
        label=label,
        cwd=cwd,
    )
    return _extract_json(text)


# Tools the live-authoring agent (#400) is allowed to use: Bash to drive the
# `browser-harness` CLI, and the file tools to write the emitted spec + sidecar
# into the confined authoring workspace. Deliberately excludes Edit (writes are
# fresh files, not edits to existing ones) and any web tools.
_AUTHORING_TOOLS = ["Bash", "Read", "Write", "Glob", "Grep"]


def run_agentic(
    prompt: str,
    *,
    workspace_dir: str | Path,
    system: str | None = None,
    skill: str | None = None,
    include_template: bool = False,
    timeout: int | None = None,
    label: str | None = None,
    model: str | None = None,
    allowed_tools: list[str] | None = None,
    max_budget_usd: float | None = None,
    extra_env: dict[str, str] | None = None,
) -> str:
    """Run Claude as a Bash/file-capable agent and return its final text result (#400).

    This is the one path where a Q-Agent-invoked Claude may run shell commands —
    used by live spec-authoring to let Claude drive the ``browser-harness`` CLI
    against a real browser, then write the emitted Playwright spec + a verified-
    selectors sidecar into ``workspace_dir``. The run is confined to that dir
    (``cwd`` + ``--add-dir``) and bounded by ``max_turns`` and ``timeout``
    (defaults from :attr:`settings.authoring_max_turns` /
    :attr:`settings.authoring_timeout_s`). Permission prompts are skipped
    (``--dangerously-skip-permissions``) because headless ``-p`` has no TTY to
    approve them; the tight tool allowlist + confined workspace + mode-gating are
    the safety boundary (see ADR 0012). A native hard dollar ceiling
    (``--max-budget-usd``, default :attr:`settings.authoring_cost_budget_usd`)
    bounds spend inside the CLI, and usage/cost is still recorded like a normal call.
    """
    return run_prompt(
        prompt,
        system=system,
        skill=skill,
        include_template=include_template,
        timeout=timeout or settings.authoring_timeout_s,
        label=label,
        cwd=workspace_dir,
        model=model,
        allowed_tools=allowed_tools or _AUTHORING_TOOLS,
        add_dir=workspace_dir,
        max_budget_usd=max_budget_usd or settings.authoring_cost_budget_usd,
        skip_permissions=True,
        extra_env=extra_env,
    )


def browser_harness_available() -> bool:
    """Best-effort preflight: is the ``browser-harness`` CLI on PATH? (#400)

    Live-authoring requires the CLI to be installed on the API host (the "treat
    the host as a server" prerequisite). Returns True only if the executable
    resolves and ``--version`` exits cleanly; used to fail fast with a clear
    message before launching a browser + agentic run that would otherwise error
    deep inside the loop. Never raises.
    """
    from shutil import which

    if which("browser-harness") is None:
        return False
    try:
        proc = subprocess.run(  # noqa: S603
            ["browser-harness", "--version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return proc.returncode == 0
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return False


def verify_credentials(config_dir: str | Path) -> tuple[str, str]:
    """Run a minimal prompt under an explicit ``CLAUDE_CONFIG_DIR`` and classify
    the outcome for the credential-test endpoint.

    Returns ``(result, message)`` where ``result`` is one of:
      * ``"ok"``      — the credential authenticated and Claude replied.
      * ``"invalid"`` — the CLI reported "Not logged in" (expired/revoked token).
      * ``"error"``   — anything else (CLI missing, timeout, rate-limit, …).

    Deliberately does NOT record usage/activity or resolve ambient owners — the
    caller passes the exact config dir to test (see ``routers.ai.test_credentials``),
    so testing user A's credential never accidentally probes the shared one.
    """
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir)}
    cmd = [
        settings.claude_bin,
        "-p",
        "Reply with exactly: ok",
        "--output-format",
        "json",
        "--model",
        _resolve_model(),
    ]
    try:
        proc = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, timeout=60, encoding="utf-8", env=env
        )
    except FileNotFoundError:
        return ("error", f"Claude CLI not found (looked for '{settings.claude_bin}').")
    except subprocess.TimeoutExpired:
        return ("error", "Claude CLI timed out while testing the credential.")
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode == 0:
        try:
            envelope = json.loads(out)
            is_error = isinstance(envelope, dict) and bool(envelope.get("is_error"))
        except json.JSONDecodeError:
            is_error = False
        if not is_error:
            return ("ok", "Credential is valid — Claude responded.")
    combined = f"{out}\n{err}".lower()
    if "not logged in" in combined or "please run /login" in combined:
        return ("invalid", "Not logged in — the token is expired or revoked. Re-upload it.")
    return ("error", (err or out or "Unknown error")[:200])


# Cache of the last is_available() probe: (monotonic_ts, result). The /ai/stats
# endpoint is polled by the UI and each call otherwise spawns `claude --version`
# with a 15s timeout; the CLI's presence changes rarely, so a short TTL removes a
# subprocess-per-poll (#180).
_IS_AVAILABLE_TTL_S = 60.0
_is_available_cache: tuple[float, bool] | None = None


def is_available() -> bool:
    """Best-effort check that the CLI is present (does not verify auth).

    Cached for ``_IS_AVAILABLE_TTL_S`` so polling ``/ai/stats`` doesn't spawn a
    subprocess on every request (#180).
    """
    global _is_available_cache
    now = time.monotonic()
    if _is_available_cache is not None and now - _is_available_cache[0] < _IS_AVAILABLE_TTL_S:
        return _is_available_cache[1]
    try:
        proc = subprocess.run(  # noqa: S603
            [settings.claude_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        result = proc.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        result = False
    _is_available_cache = (now, result)
    return result
