"""Application configuration, loaded from environment / `.env`."""

from __future__ import annotations

import shutil
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.logging import logger

# Repo/base directories. The backend keeps all local state under `workspace/`.
API_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = API_DIR.parent

# Artifact kinds every per-owner workspace scope holds (ADR 0009). Mirrors the
# flat `workspace/<kind>/` dirs `Settings.*_dir` has historically exposed.
_SCOPED_KINDS = ("specs", "evidence", "knowledge", "repos", "auth")

# Sentinel file marking that the one-time legacy-flat-dirs migration ran.
_LEGACY_MIGRATION_SENTINEL = ".workspace_scoped"


class Settings(BaseSettings):
    """Runtime settings for the Q-Agent backend.

    Everything is local-first: the database, the encryption key, generated
    Playwright specs, and captured evidence all live under ``workspace_dir``.
    """

    model_config = SettingsConfigDict(
        env_file=str(API_DIR / ".env"),
        env_prefix="QAGENT_",
        extra="ignore",
    )

    # Server
    host: str = "127.0.0.1"
    port: int = 8787
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # Storage
    workspace_dir: Path = API_DIR / "workspace"
    database_url: str = ""  # derived from workspace_dir if empty

    # Secret used to derive the Fernet key that encrypts provider credentials.
    # Also used to sign auth JWTs (access + short-lived MFA/reset tokens).
    # Override in production via QAGENT_SECRET_KEY.
    secret_key: str = "dev-only-insecure-change-me"

    # Auth (ADR 0007). The global auth guard is ON by default (go-live, #79):
    # every route/WS/artifact requires a valid session. Set QAGENT_AUTH_REQUIRED=
    # false to opt back into the local-first single-user mode. On an empty DB the
    # first admin is seeded from QAGENT_ADMIN_EMAIL/PASSWORD; in dev (cookie_secure
    # off) a fallback admin is auto-seeded with a logged password so you're never
    # locked out — see main._seed_admin.
    auth_required: bool = True
    # Set the `Secure` flag on auth cookies. Default False so http-localhost dev
    # works; set QAGENT_COOKIE_SECURE=true behind HTTPS in production.
    cookie_secure: bool = False
    # Optional admin seed: when both are set and the users table is empty, an
    # active Admin is created on startup.
    admin_email: str = ""
    admin_password: str = ""

    # EmeHub SSO (#478, docs/HUB-INTEGRATION.md). Off by default: while
    # `hub_sso_enabled` is false the whole integration is dormant — hub tokens
    # are REJECTED (not waved through), `/auth/sso/complete` isn't registered,
    # and /login shows only the local form. This gates a feature, not an
    # authentication check, unlike `auth_required` above.
    hub_sso_enabled: bool = False
    #: The hub's API as **the browser** must reach it — a public origin, because
    #: the SPA calls `/auth/agent-token` on it directly. Served to the client by
    #: `GET /health`.
    hub_base_url: str = ""  # e.g. https://hub.chuongnd.click/api
    #: The hub's API as **this container** should reach it. Optional; falls back
    #: to `hub_base_url`.
    #:
    #: These are two different vantage points and conflating them is expensive.
    #: Measured from inside the api container: the public URL answers in ~498 ms
    #: because the request leaves the host, crosses the Cloudflare edge and comes
    #: back to the same machine, while `http://host.docker.internal:8790` answers
    #: in ~2 ms. `hub_workspace.ensure_for_user` makes one call per ticket page
    #: plus two more, so a large hub cost seconds of pure round-trip for data
    #: sitting one bridge away.
    #:
    #: The browser cannot use this value — it names a host only reachable from
    #: inside Docker — which is exactly why it is a second setting rather than a
    #: correction to the first.
    hub_internal_base_url: str = ""
    # Must equal the hub's EMEHUB_JWT_SECRET (Phase 1 shared secret). Kept
    # SEPARATE from `secret_key` on purpose: that one already signs local JWTs
    # *and* derives the Fernet key for every encrypted credential
    # (app/crypto.py), and overloading it a third time makes the eventual re-key
    # worse — emehub ADR 0005.
    hub_jwt_secret: str = ""
    hub_audience: str = "qagent"
    # Read hub-OWNED data (tickets, Claude credentials, connections) — #497.
    # Separate from `hub_sso_enabled` because identity ships independently of
    # data, and data is meaningless without identity: `hub_client.enabled()`
    # requires BOTH. Off means no hub reads happen at all, never "allow anything".
    hub_data_enabled: bool = False

    # Claude CLI
    claude_bin: str = "claude"
    claude_model: str = "claude-sonnet-5"
    # Root of the local Claude Code state (session transcripts live under
    # `<claude_home>/projects/`). Read by `claude_usage_reader` for real /ai/stats.
    claude_home: Path = Path.home() / ".claude"
    claude_timeout_s: int = 300
    # project-bootstrap traverses a whole repo, so it gets a longer budget than a
    # one-shot prompt. It runs in a background thread, so a long wait is harmless.
    claude_bootstrap_timeout_s: int = 1200

    # Dedicated Q-Agent skills (SKILL.md methodology injected per Claude action).
    skills_dir: Path = REPO_ROOT / "skills"

    # Playwright
    playwright_bin: str = "npx"  # fallback: npx playwright test ...
    exec_timeout_s: int = 600
    # Self-heal loop: max times to re-generate + re-run a single failing spec
    # (feeding the failure back to Claude) before giving up.
    heal_max_attempts: int = 3
    # Heal re-runs use SHORTER Playwright timeouts than a normal run (#398): the
    # spec already failed, so a broken locator should fail fast instead of
    # stalling the full 30s per-test timeout on every attempt. Per-test timeout +
    # per-action timeout (ms) for heal re-runs only.
    heal_test_timeout_ms: int = 12000
    heal_action_timeout_ms: int = 8000
    # The heal fix call runs on a fast model (#398) — the fixer is a targeted,
    # DOM-grounded edit, so Haiku is enough and ~4x faster than the heavy global
    # model; fresh spec generation keeps the global model. Override via settings.
    heal_fix_model: str = "claude-haiku-4-5-20251001"
    # DOM exploration agent (ADR 0010): max observe→decide→act steps per session
    # (hard-clamped to <=20 in the loop) and the per-session Claude cost ceiling in
    # USD — the loop halts when either is reached, so exploration can never run
    # unbounded or burn unlimited spend.
    explore_max_steps: int = 15
    explore_cost_budget_usd: float = 0.50
    # Live spec-authoring mode (#400): a Bash-enabled agentic Claude drives the
    # `browser-harness` CLI to perform the test case live (discovering real
    # selectors on the real DOM) and then emit a Playwright spec. The run is
    # bounded on two axes so an autonomous tool-using session can never burn
    # unlimited spend or hang: a per-session Claude cost ceiling (USD, enforced
    # both by a pre-start check and natively via the CLI's --max-budget-usd), and
    # a wall-clock timeout (seconds). Authoring is pricier than a one-shot
    # generation (many browser-harness turns), hence a higher ceiling than
    # explore's $0.50. Both overridable via settings/env.
    authoring_cost_budget_usd: float = 2.00
    authoring_timeout_s: int = 900
    # Max seconds to wait for the operator to complete a manual login (headed
    # browser) before the capture is abandoned and the run fails cleanly.
    auth_capture_timeout_s: int = 300
    # Node modules dir that has @playwright/test + installed browsers. Generated
    # specs live under workspace/specs/<run> with no node_modules of their own, so
    # execution runs the binary here and sets NODE_PATH so the specs' and config's
    # `@playwright/test` imports resolve. Defaults to the frontend's install.
    playwright_node_modules: Path = REPO_ROOT / "app" / "node_modules"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.workspace_dir / 'q-agent.db').as_posix()}"

    @property
    def specs_dir(self) -> Path:
        return self.workspace_dir / "specs"

    @property
    def evidence_dir(self) -> Path:
        return self.workspace_dir / "evidence"

    @property
    def knowledge_dir(self) -> Path:
        return self.workspace_dir / "knowledge"

    @property
    def repos_dir(self) -> Path:
        """Local checkouts of application repos, pulled for project-bootstrap traversal."""
        return self.workspace_dir / "repos"

    @property
    def auth_dir(self) -> Path:
        """Saved per-project Playwright ``storageState.json`` sessions (manual login)."""
        return self.workspace_dir / "auth"

    def ensure_dirs(self) -> None:
        for d in (
            self.workspace_dir,
            self.specs_dir,
            self.evidence_dir,
            self.knowledge_dir,
            self.repos_dir,
            self.auth_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
        # Per-owner workspace scoping (ADR 0009): the admin/shared namespace's
        # artifact trees always exist, even before any user or migration writes
        # to them. Existing `specs_dir`/`evidence_dir`/etc above are untouched —
        # call sites migrate to the scoped dirs in a later slice.
        for kind in _SCOPED_KINDS:
            (self.workspace_dir / "shared" / kind).mkdir(parents=True, exist_ok=True)
        migrate_legacy_workspace_dirs(self)


def migrate_legacy_workspace_dirs(settings: Settings) -> None:
    """One-time, best-effort move of pre-ADR-0009 flat artifact dirs into ``shared/``.

    Before per-owner workspace scoping, every artifact kind lived directly
    under ``workspace/<kind>/`` (see ``Settings.specs_dir`` etc). This treats
    any pre-existing content there as belonging to the admin/shared namespace
    and relocates it to ``workspace/shared/<kind>/`` so scoped lookups
    (:mod:`app.services.workspace_scope`) find it. Runs at most once, guarded
    by the ``workspace/.workspace_scoped`` sentinel file: a no-op once that
    sentinel exists, and a no-op for any ``<kind>`` dir that doesn't exist or
    is empty. Never raises — any failure is logged and swallowed so a bad
    migration can't block startup.
    """
    sentinel = settings.workspace_dir / _LEGACY_MIGRATION_SENTINEL
    if sentinel.exists():
        return
    try:
        for kind in _SCOPED_KINDS:
            legacy_dir = settings.workspace_dir / kind
            if not legacy_dir.is_dir():
                continue
            entries = list(legacy_dir.iterdir())
            if not entries:
                continue
            shared_kind_dir = settings.workspace_dir / "shared" / kind
            shared_kind_dir.mkdir(parents=True, exist_ok=True)
            for entry in entries:
                destination = shared_kind_dir / entry.name
                if destination.exists():
                    continue  # already present in shared/ — leave the legacy copy alone
                shutil.move(str(entry), str(destination))
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("1", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - migration must never break startup
        logger.warning("workspace scope legacy migration failed: {}", exc)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings


settings = get_settings()
