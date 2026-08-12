"""Per-owner workspace filesystem scope resolver (ADR 0009, #116).

The database went per-user in ADR 0008 (``owner_id`` on runs, tickets,
projects, ...), but the filesystem stayed keyed by project slug / run code
only, so two users' same-named projects collided on disk. This module is the
single resolver that maps an owner to an on-disk **scope** directory, mirroring
the ``workspace/claude-config/<owner_id|"shared">/`` pattern already used by
:mod:`app.services.claude_credentials` (see ``resolve_effective_config_dir``,
``claude_credentials.py:297-311``):

- owner present -> ``workspace/users/<owner_id>/``
- owner absent (``owner_id is None``) -> ``workspace/shared/``

Every per-owner artifact tree (``specs``, ``evidence``, ``knowledge``, ``repos``,
``auth``, ``automation``) lives under the scope root. This module is a pure new library: it does
not change any existing call site (those migrate in later slices per ADR 0009).
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config import get_settings

__all__ = [
    "scope_for",
    "scoped_dir",
    "scoped_specs_dir",
    "scoped_evidence_dir",
    "scoped_knowledge_dir",
    "scoped_repos_dir",
    "scoped_auth_dir",
    "scoped_automation_dir",
    "served_evidence_path",
    "slug",
]

# The artifact kinds every scope holds (mirrors the flat `workspace/<kind>/`
# dirs config.py has historically exposed as `specs_dir`/`evidence_dir`/etc).
_KINDS = ("specs", "evidence", "knowledge", "repos", "auth", "automation")


def scope_for(owner_id: int | None) -> str:
    """Return the on-disk scope segment for ``owner_id``.

    ``owner_id`` is a user's primary key, or ``None`` when there is no owner
    (auth disabled, or an explicitly shared/admin-managed artifact). Mirrors
    the ``owner_id|"shared"`` key pattern used by
    :func:`app.services.claude_credentials.resolve_effective_config_dir`.

    Returns ``f"users/{owner_id}"`` when ``owner_id`` is set, else ``"shared"``.
    """
    if owner_id is not None:
        return f"users/{owner_id}"
    return "shared"


def scoped_dir(kind: str, owner_id: int | None) -> Path:
    """Return the scoped directory for artifact ``kind`` owned by ``owner_id``.

    ``kind`` is one of ``"specs"``, ``"evidence"``, ``"knowledge"``, ``"repos"``,
    ``"auth"``, ``"automation"``. The path is not created on disk here — callers ``mkdir`` as
    needed, matching the existing (unscoped) ``Settings.*_dir`` properties.

    Returns ``get_settings().workspace_dir / scope_for(owner_id) / kind``.
    """
    return get_settings().workspace_dir / scope_for(owner_id) / kind


def scoped_specs_dir(owner_id: int | None) -> Path:
    """Scoped ``specs`` directory for ``owner_id`` — see :func:`scoped_dir`."""
    return scoped_dir("specs", owner_id)


def scoped_evidence_dir(owner_id: int | None) -> Path:
    """Scoped ``evidence`` directory for ``owner_id`` — see :func:`scoped_dir`."""
    return scoped_dir("evidence", owner_id)


def scoped_knowledge_dir(owner_id: int | None) -> Path:
    """Scoped ``knowledge`` directory for ``owner_id`` — see :func:`scoped_dir`."""
    return scoped_dir("knowledge", owner_id)


def scoped_repos_dir(owner_id: int | None) -> Path:
    """Scoped ``repos`` directory for ``owner_id`` — see :func:`scoped_dir`."""
    return scoped_dir("repos", owner_id)


def scoped_auth_dir(owner_id: int | None) -> Path:
    """Scoped ``auth`` directory for ``owner_id`` — see :func:`scoped_dir`."""
    return scoped_dir("auth", owner_id)


def scoped_automation_dir(owner_id: int | None) -> Path:
    """Scoped ``automation`` directory for ``owner_id`` — see :func:`scoped_dir`.

    Root of the persistent git-backed automation projects (#538):
    ``workspace/<scope>/automation/<project-slug>/<repo-slug>/``.
    """
    return scoped_dir("automation", owner_id)


def served_evidence_path(owner_id: int | None, relative_path: str) -> str:
    """Path segment served under ``/artifacts/`` for a scoped evidence file.

    ``relative_path`` is the value stored in ``Evidence.path`` — relative to
    ``scoped_evidence_dir(owner_id)`` (e.g. ``"<RUN-CODE>/<ticket>/<case>/<file>"``,
    see ``playwright_runner._store_evidence``). The frontend builds artifact URLs
    by naive concatenation (``app/src/lib/api.ts:408``:
    `` `${API_BASE}/artifacts/${path}` ``), and the ``/artifacts`` mount now serves
    ``settings.workspace_dir`` (ADR 0009 §5, ``app.main``), so this returns the
    on-disk suffix under that mount: ``<scope>/evidence/<relative_path>``.
    """
    return f"{scope_for(owner_id)}/evidence/{relative_path}"


def slug(value: str) -> str:
    """Canonical filesystem-safe slug for a project/knowledge key.

    Replaces every run of characters outside ``[a-zA-Z0-9._-]`` with a single
    ``-``, then strips leading/trailing ``-``. Falls back to ``"project"`` if
    that leaves nothing (e.g. ``value`` was empty or all-punctuation).

    Unifies the two duplicate ``_slug`` helpers at
    ``app/services/project_config_service.py:213-215`` and
    ``app/services/knowledge_service.py:150-151`` — this is the canonical
    implementation those two will import from in a later slice.
    """
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-") or "project"
