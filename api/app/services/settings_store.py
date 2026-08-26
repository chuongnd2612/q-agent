"""Persistence for app-wide execution settings (`SettingsOut` fields).

Settings are not tied to any provider or run, so they don't need a DB model —
they're small, local-first config persisted as JSON under the workspace dir.
"""

from __future__ import annotations

import json
from typing import Any

from app.config import settings as app_settings

DEFAULTS: dict[str, Any] = {
    "parallel": 4,
    "retryFlaky": True,
    "screenshotOnFail": True,
    "video": False,
    "maxCasesPerTicket": 8,
    "headless": True,
    "autoAnnotate": True,
    # Never write to a provider (#712). When on, creating + linking test cases and
    # publishing comments do all their local work and touch NO work item: nothing is
    # created, no comment is posted, no status is transitioned.
    #
    # A property of how the workspace is being used — evaluating, demoing, testing
    # against a live board — not a decision to re-make per click, which is what the
    # three near-identical "create" buttons on Review were asking of everyone.
    # Enforced server-side: a dry run that is only a UI state is one forgotten request
    # away from writing to a real work item.
    "dryRun": False,
    "neuralBackground": True,
    "claudeModel": "claude-sonnet-5",
    # Per-action model overrides, keyed by skill name (#175). Empty = every action
    # uses its built-in default / the global claudeModel (see claude_cli._resolve_model).
    "skillModels": {},
    # Ticket concurrency for the analyze+generate pipeline (#179); 0 = auto (3 on
    # Postgres, 1 on SQLite). See ai_service._resolve_worker_count.
    "aiPipelineWorkers": 0,
    "weeklyTokenBudget": 0,
    # Default execution target for new runs when a request doesn't specify one
    # (Local Agent feature — see EXEC_TARGETS): "server" (legacy in-process
    # runner) or "local-agent" (queued for a paired device to claim). Fresh
    # installs default to the user's machine ("My machine").
    "executionTarget": "local-agent",
    # How approved cases are turned into Playwright specs (#400). "blind"
    # (default) = generate from the KB then heal failures (automation-generator);
    # "live-harness" = drive the real app live via browser-harness to discover
    # real selectors, then emit the spec (live_authoring_service). Orthogonal to
    # executionTarget — live-harness always authors server-side.
    "authoringMode": "blind",
    # How a failing spec is self-healed (#428). "classic" (default) = generate a
    # fix from the failure + captured DOM then re-run Playwright, up to
    # heal_max_attempts. "live-harness" = drive the real app live via
    # browser-harness (reusing the live-authoring pipeline) seeded with the failing
    # spec + error, and emit a corrected spec. live-harness needs a paired local
    # agent (browser-harness + claude run there); falls back to classic otherwise.
    "healMode": "classic",
    # Per-session Claude $ ceiling for a live browser-harness run — shared by live
    # authoring AND live self-heal (#430). Enforced natively by the CLI's
    # --max-budget-usd. Defaults to the config value; Settings-configurable so the
    # operator can raise it when a heal/author needs to create data + drive a long
    # flow. See config.authoring_cost_budget_usd.
    "authoringCostBudgetUsd": 2.00,
    # Verbosity of the live-authoring step trail shown in the UI (#400). "concise"
    # (default) shows only user-readable lines (Claude's narration + phase status);
    # "verbose" also shows the raw tool/Bash calls (browser-harness invocations).
    # Presentation-only — the agent always streams both; the client filters.
    "authoringLogVerbosity": "concise",
    # Global spec quality-gate toggle (#gate-toggle). True = gate specs on
    # generation/edit/heal (default); False = bypass gating and accept every
    # spec as runnable. See placeholder_gate + automation._gate_spec_or_bypass.
    "gateEnabled": True,
}


def _settings_path():
    return app_settings.workspace_dir / "settings.json"


def load_settings() -> dict[str, Any]:
    """Load persisted settings, falling back to defaults for missing keys."""
    path = _settings_path()
    if not path.exists():
        return dict(DEFAULTS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULTS)
    merged = dict(DEFAULTS)
    merged.update(data)
    return merged


def gate_enabled() -> bool:
    """Whether the global spec quality gate is active (default on).

    Read by the spec generation/edit/heal paths to decide whether to gate a spec
    or bypass gating entirely (#gate-toggle). Defaults to True for any install
    that predates the setting.
    """
    return bool(load_settings().get("gateEnabled", True))


def authoring_cost_budget_usd() -> float:
    """Effective per-session $ ceiling for live authoring + live heal (#430).

    The Settings-configurable ``authoringCostBudgetUsd`` if set, else the config
    default (``settings.authoring_cost_budget_usd``) for installs that predate it.
    """
    val = load_settings().get("authoringCostBudgetUsd")
    try:
        return float(val) if val is not None else float(app_settings.authoring_cost_budget_usd)
    except (TypeError, ValueError):
        return float(app_settings.authoring_cost_budget_usd)


def save_settings(data: dict[str, Any]) -> dict[str, Any]:
    """Persist settings (merged with existing values) and return the result."""
    current = load_settings()
    current.update({k: v for k, v in data.items() if v is not None})
    app_settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    _settings_path().write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current
