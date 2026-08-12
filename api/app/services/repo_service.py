"""Resolve a traversable local checkout of an application repo for project-bootstrap.

Resolution order, best-effort (never raises — a failure just means Claude falls
back to metadata inference):

1. A configured ``local_repo_path`` that exists on disk — used as-is, no network.
2. Otherwise a remote git URL (the project's ``repo_url``, or one derived from the
   provider + repo identifier) is cloned/pulled into the owner's scoped repos dir
   (ADR 0009 — ``workspace/<scope>/repos/<slug>``) and that checkout is traversed.

Private repos are authenticated by injecting the project's **repository
connection** PAT (``secrets['pat']``) into the HTTPS URL (ADR 0006 — repository
credentials come from the project's bound repository connection, not from
host-guessing). Tokens are redacted from all logs.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from sqlalchemy.orm import Session

from app import crypto
from app.logging import logger
from app.services import connection_service
from app.services.adapters.base import ProviderError
from app.services.workspace_scope import scoped_repos_dir

_CLONE_TIMEOUT_S = 180


def _slug(key: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", key).strip("-") or "project"


def _redact(url: str) -> str:
    """Hide any embedded credentials before logging a URL."""
    return re.sub(r"://[^@/]+@", "://***@", url)


def _derive_url(repo: str, provider_display: str) -> str:
    """Best-effort clone URL from a bare repo identifier (e.g. 'org/repo').

    Only GitHub's ``org/repo`` shorthand is derivable unambiguously. For other
    hosts (notably Azure DevOps) the user should supply an explicit ``repo_url``.
    """
    repo = (repo or "").strip()
    if not repo:
        return ""
    if repo.startswith(("http://", "https://", "git@", "ssh://")):
        return repo
    if re.fullmatch(r"[\w.-]+/[\w.-]+", repo) and "github" in (provider_display or "github").lower():
        return f"https://github.com/{repo}.git"
    return ""


def _repo_pat_for_project(db: Session, project_key: str, owner_id: int | None = None) -> str:
    """PAT of the project's bound repository connection (ADR 0006), or "".

    Best-effort: an un-bound project (no repository connection resolvable) yields
    an empty PAT, so a public clone still proceeds without credentials.
    ``owner_id`` (#93 — private per-user data) restricts resolution to that
    user's own connections.
    """
    try:
        connection = connection_service.resolve_repository_for_project(
            db, project_key, owner_id=owner_id
        )
    except ProviderError:
        return ""
    return crypto.decrypt((connection.secrets or {}).get("pat", "")) or ""


def _authenticated_url(url: str, pat: str) -> str:
    """Inject a PAT into an HTTPS URL that has no credentials of its own."""
    if not pat or not url.startswith("https://"):
        return url
    parsed = urlparse(url)
    if "@" in parsed.netloc:  # URL already carries credentials
        return url
    netloc = f"{pat}@{parsed.netloc}"
    return urlunparse(parsed._replace(netloc=netloc))


def _run_git(args: list[str]) -> bool:
    """Run a git command; return True on success. Never raises."""
    try:
        proc = subprocess.run(  # noqa: S603
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=_CLONE_TIMEOUT_S,
            encoding="utf-8",
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        logger.warning("git {} failed: {}", args[0], exc)
        return False
    if proc.returncode != 0:
        logger.warning("git {} exited {}: {}", args[0], proc.returncode, proc.stderr.strip()[:300])
        return False
    return True


def scrub(text: str, secret: str = "") -> str:
    """Make ``text`` safe to log, return to a client, or publish over WS (#549).

    Two independent passes, because either alone leaks:

    * :func:`_redact` masks ``https://<credentials>@host`` — the form git echoes
      back when a push/fetch against an authenticated URL fails.
    * The literal ``secret`` is then replaced verbatim, which catches every other
      shape a token could take in git's output (a bare token in a credential
      helper message, a URL-encoded variant of the same host line, and so on).

    Args:
        text: Raw command output or message.
        secret: The PAT to scrub verbatim. Empty means "URL masking only".

    Returns:
        The scrubbed text (never ``None``).
    """
    out = _redact(text or "")
    if secret:
        out = out.replace(secret, "***")
    return out


def run_git_captured(
    args: list[str], *, secret: str = "", timeout: int = _CLONE_TIMEOUT_S
) -> tuple[int, str]:
    """Run git and hand back ``(returncode, scrubbed combined output)`` (#549).

    :func:`_run_git` is deliberately left alone: it answers pass/fail and logs raw
    stderr, which is fine for the clone paths it serves but unusable for export —
    the caller there must *surface* git's message to the user, and git routinely
    echoes the remote URL (PAT included) in push/fetch failures. Every byte
    returned here has already been through :func:`scrub`.

    Interactive credential prompts are disabled (``GIT_TERMINAL_PROMPT=0`` and a
    no-op ``GIT_ASKPASS``), so a wrong or expired PAT fails fast instead of
    hanging the request waiting on a terminal that does not exist.

    Never raises: a missing git or a timeout comes back as ``(-1, message)``.
    """
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "echo",
        "GCM_INTERACTIVE": "never",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    try:
        proc = subprocess.run(  # noqa: S603
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        # `exc` can embed the command line — and therefore the authenticated URL.
        return -1, scrub(f"git {args[0] if args else ''} failed: {exc}", secret)
    output = scrub(f"{proc.stdout or ''}{proc.stderr or ''}".strip(), secret)
    if proc.returncode != 0:
        logger.warning("git {} exited {}: {}", args[0] if args else "", proc.returncode, output[:300])
    return proc.returncode, output


def materialize_remote(
    db: Session,
    key: str,
    repo_url: str,
    provider_display: str = "",
    repo_name: str = "",
    owner_id: int | None = None,
) -> str | None:
    """Clone (or pull) ``repo_url`` into the owner's scoped repos dir
    (``workspace/<scope>/repos/<project>[/<repo>]``); return path or None.

    Args:
        db: Active session (to look up the provider PAT for private repos).
        key: Project key — determines the on-disk clone directory.
        repo_url: Explicit git URL, or a bare identifier we can derive one from.
        provider_display: The project's provider label, used to guess the host
            when only a bare ``org/repo`` identifier is available.
        repo_name: When set, clone into a per-repo subdirectory so a project's
            many repos each get their own checkout (per-repo knowledge).
        owner_id: Restricts the repository-connection PAT lookup to that user's
            own connections (#93 — private per-user data).

    Returns:
        The local checkout path, or None when no URL is resolvable or git fails.
    """
    url = repo_url.strip() or _derive_url(repo_url, provider_display)
    if not url:
        return None

    pat = _repo_pat_for_project(db, key, owner_id=owner_id)
    authed = _authenticated_url(url, pat)

    dest = scoped_repos_dir(owner_id) / _slug(key)
    if repo_name:
        dest = dest / _slug(repo_name)
    if (dest / ".git").is_dir():
        logger.info("Pulling latest for {} into {}", _redact(url), dest)
        # Refresh to the remote's default branch; robust for shallow clones.
        ok = _run_git(["-C", str(dest), "fetch", "--depth", "1", "origin"]) and _run_git(
            ["-C", str(dest), "reset", "--hard", "origin/HEAD"]
        )
        if ok:
            return str(dest)
        # Fall through to a fresh clone if the pull could not reconcile.
        logger.info("Pull failed for {}; re-cloning", _redact(url))
        _rmtree(dest)

    dest.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Cloning {} into {}", _redact(url), dest)
    if _run_git(["clone", "--depth", "1", authed, str(dest)]):
        return str(dest)
    _rmtree(dest)
    return None


def _rmtree(path: Path) -> None:
    import shutil

    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def resolve_repo_path(
    db: Session,
    key: str,
    config,
    provider_display: str = "",
    repo: str = "",
    owner_id: int | None = None,
) -> str | None:
    """Return a local checkout to traverse: configured local path, else a clone.

    Args:
        db: Active session.
        key: Project key.
        config: The project's ProjectConfig row (or None).
        provider_display: Provider label for URL derivation/PAT lookup.
        repo: Repo identifier fallback (e.g. ProjectKnowledge.repo) when no
            explicit ``repo_url`` is configured.
        owner_id: Restricts the clone PAT lookup to that user's own repository
            connection (#93 — private per-user data).

    Returns:
        A local directory path, or None if nothing is available.
    """
    if config and config.local_repo_path and Path(config.local_repo_path).is_dir():
        return config.local_repo_path

    repo_url = (config.repo_url if config and config.repo_url else "") or repo
    if not repo_url:
        return None
    return materialize_remote(db, key, repo_url, provider_display, owner_id=owner_id)


def resolve_one_repo(
    db: Session,
    project_key: str,
    repo: dict,
    provider_display: str = "",
    owner_id: int | None = None,
) -> str | None:
    """Resolve a single project repo to a local checkout for bootstrap traversal.

    Uses the repo's ``local_repo_path`` if it exists, else clones/pulls its
    ``repo_url`` into ``workspace/repos/<project>/<repo>``. ``owner_id`` (#93)
    restricts the clone PAT lookup to that user's own repository connection.
    """
    local = (repo.get("local_repo_path") or "").strip()
    if local and Path(local).is_dir():
        return local
    repo_url = (repo.get("repo_url") or "").strip()
    if not repo_url:
        return None
    return materialize_remote(
        db, project_key, repo_url, provider_display, repo_name=repo.get("name", ""), owner_id=owner_id
    )
