"""Export the persistent automation project to a customer-owned git remote (#549).

Wave 3 of epic #537, and the slice that makes the automation suite something the
**customer owns** rather than something trapped inside Q-Agent's workspace. The
project has been a real git repo since #538 (that is how rollback works), but its
history stayed local; this pushes it to a remote the customer controls so they can
run the suite in their own CI.

Four rules here are structural, not stylistic, and every one is enforced below:

1. **User-triggered only.** Nothing in this module is called from generation,
   healing, execution or any background pass — the single caller is the export
   endpoint, driven by an explicit click, with the target and branch chosen by the
   user. There is no push-on-generate and no background push.
2. **Never the remote's default branch.** The remote's own ``HEAD`` symref is
   resolved and refused, and a mainline-name set is refused on top of that.
3. **Never a forced overwrite.** There is no ``--force``, no ``--force-with-lease``
   and no ``+`` refspec anywhere in this module. A diverged remote branch is
   *refused and reported* with the local repo left intact; auto-merging AI-authored
   commits into human edits is explicitly out of scope, so no merge or rebase is
   attempted either. Pushing twice is a clean fast-forward or a clean refusal.
4. **The PAT never escapes.** Every git invocation that touches the remote goes
   through :func:`repo_service.run_git_captured` with ``secret=pat``, whose output is
   scrubbed of both the ``https://creds@host`` form *and* the literal token before it
   can reach a log line, an :class:`ExportError` message, an HTTP response or a WS
   frame. ``repo_service._run_git`` is deliberately **not** used for remote
   operations: it logs raw stderr, which is exactly where git echoes the
   authenticated URL back.

It lives in its own module rather than growing ``automation_project_service`` so the
export path adds no edit surface to the file the rest of the epic is changing.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

from sqlalchemy.orm import Session

from app.logging import logger
from app.models.automation_project import AutomationProject
from app.services import automation_project_service as aps
from app.services.repo_service import _authenticated_url, _redact, run_git_captured, scrub
from app.services.workspace_scope import slug

__all__ = [
    "EXPORT_BRANCH_PREFIX",
    "ExportError",
    "default_export_branch",
    "export_credentials",
    "export_preflight",
    "export_to_remote",
    "export_to_zip",
    "redact_remote",
]

EXPORT_BRANCH_PREFIX = "qagent/automation"

# Refused as an export target even when the remote's own default is something else:
# these are the names a protected/mainline branch actually has in the wild, and the
# cost of a false refusal (pick another branch) is trivial next to pushing
# AI-authored commits onto a customer's mainline.
_RESERVED_BRANCHES = frozenset({"main", "master", "trunk", "default", "develop", "release"})

_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class ExportError(Exception):
    """A refusal the user can act on, with a stable machine-readable ``code``.

    Carries no PAT by construction: every message built from git output is
    assembled from :func:`repo_service.run_git_captured`'s already-scrubbed text,
    and every remote URL in a message goes through :func:`redact_remote`.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def redact_remote(url: str) -> str:
    """A remote URL safe to show, log and publish (embedded credentials masked)."""
    return _redact((url or "").strip())


def default_export_branch(project: AutomationProject) -> str:
    """Suggested branch name — never a default/mainline name by construction.

    The ``qagent/automation/…`` prefix means the suggestion can never collide with
    the remote's default branch, so the happy path never trips the refusal in
    :func:`_check_not_default_branch`.
    """
    tail = slug(f"{project.project_key}-{project.repo or 'default'}").lower().strip("-")
    return f"{EXPORT_BRANCH_PREFIX}/{tail or 'project'}"


def _validate_branch(branch: str) -> str:
    """Normalize and sanity-check a user-supplied branch name."""
    branch = (branch or "").strip().strip("/")
    if branch.startswith("refs/heads/"):
        branch = branch[len("refs/heads/") :]
    if not branch:
        raise ExportError("branch_required", "Choose a branch name to push to.")
    if (
        len(branch) > 200
        or not _BRANCH_RE.match(branch)
        or ".." in branch
        or branch.endswith(".lock")
    ):
        raise ExportError(
            "branch_invalid",
            f"'{branch}' is not a valid git branch name. Use letters, digits, '.', '_', "
            "'-' and '/'.",
        )
    return branch


def _validate_remote(remote_url: str) -> str:
    """Accept an HTTP(S) remote (or a ``file://`` mirror); refuse anything else.

    SSH is refused rather than half-supported: the export authenticates with the
    repository connection's PAT, and an ``ssh://``/``git@`` remote cannot use one —
    it would fail on a key Q-Agent has no way to hold, with a message about
    ``known_hosts`` that no user could act on. ``file://`` is accepted because it is
    a legitimate remote for a mounted/self-hosted mirror, and it needs no
    credentials at all (:func:`_credentials_for`).
    """
    remote_url = (remote_url or "").strip()
    if not remote_url:
        raise ExportError("remote_required", "Enter the HTTPS URL of the git remote to export to.")
    if not remote_url.lower().startswith(("http://", "https://", "file://")):
        raise ExportError(
            "remote_unsupported",
            "Only HTTP(S) remotes can be exported to — the export authenticates with the "
            "repository connection's personal access token, which an SSH remote cannot "
            "use. Use the repository's https:// clone URL.",
        )
    return remote_url


def _needs_credentials(remote_url: str) -> bool:
    """A ``file://`` remote authenticates with filesystem permissions, not a PAT."""
    return not remote_url.lower().startswith("file://")


def _credentials_for(
    db: Session, remote_url: str, project_key: str, owner_id: int | None
) -> tuple[str, str]:
    """``(pat, connection name)``, with the PAT *required* only for HTTP(S).

    For a ``file://`` remote a connection is resolved opportunistically — if the
    project happens to have one, its PAT is still threaded through as the scrubbing
    ``secret`` so nothing can leak on that path either — but its absence is not an
    error, because there is nothing for a token to authenticate against.
    """
    try:
        return export_credentials(db, project_key, owner_id)
    except ExportError:
        if _needs_credentials(remote_url):
            raise
        return "", "local"


def export_credentials(db: Session, project_key: str, owner_id: int | None) -> tuple[str, str]:
    """``(pat, connection name)`` for the owner's own repository connection.

    Deliberately *not* ``repo_service._repo_pat_for_project``: that helper falls
    back to an empty PAT so a public clone still proceeds, which is right for
    cloning and wrong for pushing — an anonymous push can only fail, and it would
    fail with git's cryptic "could not read Username for …" rather than something
    the user can act on. Ownership is inherited from
    ``connection_service.resolve_repository_for_project(owner_id=…)`` (#93 /
    ADR 0009): a user's export can only ever authenticate with that user's own
    connection, never another user's.

    Raises:
        ExportError: ``no_repository_connection`` when nothing is bound, or
            ``no_repository_credentials`` when the bound connection stores no PAT.
    """
    # Local imports keep the import graph one-way (connection_service pulls in the
    # provider adapters, which import services of their own).
    from app import crypto
    from app.services import connection_service
    from app.services.adapters.base import ProviderError

    try:
        connection = connection_service.resolve_repository_for_project(
            db, project_key, owner_id=owner_id
        )
    except ProviderError:
        raise ExportError(
            "no_repository_connection",
            "No repository connection is available for this project, so there are no "
            "credentials to push with. Connect a repository provider (GitHub or Azure "
            "DevOps) under Settings → Integrations, bind it to the project, then export "
            "again.",
        ) from None
    pat = crypto.decrypt((connection.secrets or {}).get("pat", "")) or ""
    if not pat:
        raise ExportError(
            "no_repository_credentials",
            f"The repository connection '{connection.name}' has no personal access token "
            "stored, so the export cannot authenticate. Add a PAT with permission to push "
            "to that connection under Settings → Integrations, then export again.",
        )
    return pat, connection.name


def _remote_default_branch(output: str) -> str:
    """Parse ``git ls-remote --symref <url> HEAD`` for the remote's default branch."""
    for line in (output or "").splitlines():
        line = line.strip()
        if line.startswith("ref:") and line.endswith("HEAD"):
            ref = line[4:].strip().split()[0]
            return ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
    return ""


def _check_not_default_branch(branch: str, remote_default: str) -> None:
    """Refuse the remote's default branch, and any mainline-shaped name."""
    if remote_default and branch == remote_default:
        raise ExportError(
            "default_branch_refused",
            f"'{branch}' is the remote's default branch. Q-Agent only ever exports to a "
            "separate branch so the AI-authored commits go through your own review. "
            "Choose a different branch name.",
        )
    if branch.lower() in _RESERVED_BRANCHES:
        raise ExportError(
            "default_branch_refused",
            f"'{branch}' is a mainline branch name. Q-Agent only ever exports to a separate "
            "branch so the AI-authored commits go through your own review. Choose a "
            f"different branch name (for example '{EXPORT_BRANCH_PREFIX}/suite').",
        )


def _remote_head_sha(root: Path, authed: str, branch: str, pat: str) -> str:
    """The remote branch's sha, or ``""`` when the branch does not exist there yet."""
    code, output = run_git_captured(
        ["-C", str(root), "ls-remote", "--heads", authed, f"refs/heads/{branch}"], secret=pat
    )
    if code != 0:
        raise ExportError(
            "remote_unreachable",
            f"Could not read branch '{branch}' from the remote: "
            f"{output or 'git ls-remote failed'}",
        )
    first = (output or "").strip().split("\n")[0].split()
    return first[0] if first else ""


def export_preflight(
    db: Session,
    project: AutomationProject,
    *,
    project_key: str = "",
    owner_id: int | None = None,
    suggested_remote: str = "",
) -> dict:
    """Everything the export panel needs to prefill itself — **pushes nothing**.

    Returns ``credentialsError`` (an actionable sentence) instead of raising, so the
    UI can warn *before* the user commits to an action rather than surfacing the same
    problem as a failed push. It is a warning and not a hard block on purpose: a user
    who has just added a PAT would otherwise be locked out by a cached preflight, and
    :func:`export_to_remote` refuses authoritatively anyway.
    """
    key = (project_key or project.project_key or "").strip()
    scope = project.owner_id if owner_id is None else owner_id
    credentials_error = ""
    credentials_code = ""
    connection_name = ""
    try:
        _, connection_name = export_credentials(db, key, scope)
    except ExportError as exc:
        credentials_error = exc.message
        credentials_code = exc.code
    return {
        "projectId": project.id,
        "projectSlug": project.slug,
        "projectKey": project.project_key,
        "repo": project.repo,
        "branch": default_export_branch(project),
        "remoteUrl": redact_remote(suggested_remote),
        "commit": aps.head_commit(project),
        "connection": connection_name or None,
        "hasCredentials": not credentials_error,
        "credentialsError": credentials_error or None,
        "credentialsCode": credentials_code or None,
        # No provider adapter implements pull-request creation today (#549's scope
        # is explicit: open a PR only if the adapter already supports it, and none
        # does — `api/app/services/adapters/` has no create-PR call), so the export
        # reports the pushed branch and the user opens the PR. Flipping this to True
        # is the whole client-side change needed once an adapter grows it.
        "canOpenPullRequest": False,
    }


def export_to_remote(
    db: Session,
    project: AutomationProject,
    *,
    remote_url: str,
    branch: str,
    project_key: str = "",
    owner_id: int | None = None,
    message: str = "",
) -> dict:
    """Push the automation project to ``branch`` on ``remote_url``. User-triggered.

    The sequence, and why each step is where it is:

    1. Validate the remote and branch **before** touching credentials, so a typo
       never causes a decrypt or a network call.
    2. Resolve the owner's own repository-connection PAT
       (:func:`export_credentials`) — a missing connection is an actionable refusal,
       not a raw git failure.
    3. Resolve the remote's default branch via ``ls-remote --symref``. That single
       call doubles as the credential/reachability check *and* as the "never the
       default branch" guard, and it happens before anything local is mutated.
    4. Commit the working tree if it is dirty (an export exports a commit).
    5. If the branch already exists on the remote: an identical sha means there is
       nothing to do; otherwise ``fetch`` it — which writes only ``FETCH_HEAD``, no
       local branch, no checkout, no working-tree change, so **the local repo is
       left intact** — and require the remote tip to be an ancestor of ``HEAD``. If
       it is not, that branch has **diverged** and the export is refused.
    6. Push ``HEAD:refs/heads/<branch>``. No force, no lease; the remote's own
       non-fast-forward rejection is the second net under step 5.

    Args:
        db: Active session (credential lookup only).
        project: The project to export. Ownership is the caller's job
            (``get_owned_or_404``), plus ``owner_id`` scoping the PAT lookup here.
        remote_url: HTTPS URL of the customer-owned remote.
        branch: Target branch. Never the remote's default (refused).
        project_key: Overrides ``project.project_key`` for credential resolution.
        owner_id: Overrides ``project.owner_id`` for credential resolution.
        message: Commit message used for any uncommitted work.

    Returns:
        ``{ok, branch, remote, commit, committed, pushed, upToDate, created, prUrl,
        detail}``. ``remote`` is always redacted; ``prUrl`` is always ``None`` until
        an adapter can open one.

    Raises:
        ExportError: Every refusal path. ``.message`` is safe to show the user.
    """
    remote_url = _validate_remote(remote_url)
    branch = _validate_branch(branch)
    redacted = redact_remote(remote_url)
    key = (project_key or project.project_key or "").strip()
    scope = project.owner_id if owner_id is None else owner_id

    pat, connection_name = _credentials_for(db, remote_url, key, scope)
    authed = _authenticated_url(remote_url, pat)
    root = aps.project_dir(project)

    code, output = run_git_captured(["ls-remote", "--symref", authed, "HEAD"], secret=pat)
    if code != 0:
        raise ExportError(
            "remote_unreachable",
            f"Could not reach {redacted} with the '{connection_name}' connection's "
            f"credentials: {output or 'git ls-remote failed'}",
        )
    _check_not_default_branch(branch, _remote_default_branch(output))

    with aps.project_lock(project):
        if not (root / ".git").is_dir() and not aps.git_init(project):
            raise ExportError(
                "not_a_repository",
                "The automation project is not a git repository yet, so there is nothing "
                "to export. Generate at least one spec first.",
            )
        committed = False
        if aps.git_changed_paths(project):
            committed = aps.git_commit(project, message or "chore: export automation project")
        sha = aps.head_commit(project)
        if not sha:
            raise ExportError(
                "nothing_to_export",
                "The automation project has no commits yet, so there is nothing to export. "
                "Generate at least one spec first.",
            )

        remote_sha = _remote_head_sha(root, authed, branch, pat)
        if remote_sha == sha:
            return {
                "ok": True,
                "branch": branch,
                "remote": redacted,
                "commit": sha,
                "committed": committed,
                "pushed": False,
                "upToDate": True,
                "created": False,
                "prUrl": None,
                "detail": f"Branch '{branch}' on the remote is already at {sha[:8]}.",
            }
        if remote_sha:
            code, output = run_git_captured(
                ["-C", str(root), "fetch", "--no-tags", authed, f"refs/heads/{branch}"],
                secret=pat,
            )
            if code != 0:
                raise ExportError(
                    "fetch_failed",
                    f"Could not fetch branch '{branch}' from {redacted} to check for "
                    f"divergence: {output or 'git fetch failed'}",
                )
            code, _ = run_git_captured(
                ["-C", str(root), "merge-base", "--is-ancestor", remote_sha, "HEAD"], secret=pat
            )
            if code != 0:
                raise ExportError(
                    "diverged",
                    f"Branch '{branch}' on {redacted} has commits that are not in this "
                    f"project's history (the remote is at {remote_sha[:8]}, this project at "
                    f"{sha[:8]}). Q-Agent will not force-push over them, and will not merge "
                    "AI-authored commits into hand-edited code. Export to a new branch, or "
                    "reconcile that branch yourself and export again. Nothing was changed.",
                )

        # No --force, no --force-with-lease, no '+' refspec: a push that is not a
        # fast-forward is rejected by the remote rather than overwriting it.
        code, output = run_git_captured(
            ["-C", str(root), "push", authed, f"HEAD:refs/heads/{branch}"], secret=pat
        )
        if code != 0:
            raise ExportError(
                "push_failed",
                f"Push to {redacted} ({branch}) failed: {output or 'git push failed'}",
            )

    logger.info(
        "exported automation project {} to {} branch {} at {}",
        project.id,
        redacted,
        branch,
        sha[:8],
    )
    return {
        "ok": True,
        "branch": branch,
        "remote": redacted,
        "commit": sha,
        "committed": committed,
        "pushed": True,
        "upToDate": False,
        "created": not remote_sha,
        # See export_preflight: no adapter can open a PR, so the branch is reported
        # and the user opens it.
        "prUrl": None,
        # `output` is already scrubbed by run_git_captured; scrubbing again is the
        # cheap belt-and-braces that keeps this true if that ever changes.
        "detail": scrub(output, pat) or f"Pushed {sha[:8]} to '{branch}' on {redacted}.",
    }


# ---------------------------------------------------------------------------
# Export to a ZIP the user downloads (#686, v1)
# ---------------------------------------------------------------------------
#
# The remote push above is v2. This is v1, and it is deliberately the simpler
# thing: a ZIP needs no repository connection, no PAT, no branch policy and no
# network at all, so none of the four rules that make the push path delicate
# apply to it. What the two share is the *contents* — the whole automation suite,
# which is the thing the customer owns.


def _zip_stem(project: AutomationProject) -> str:
    """A single flat name for the archive and its top-level directory.

    ``project.slug`` is a *path* (``"Surency-Platform/web"`` — key over repo), which
    is fine on disk and wrong in both places here: a slash in the download filename
    is not a filename at all, and inside the archive it would silently split the top
    level in two.
    """
    return "qagent-automation-" + project.slug.strip("/").replace("/", "-")


def zip_filename(project: AutomationProject) -> str:
    """The download's filename: identifies the project and pins the commit.

    The short SHA matters because the suite is regenerated as work proceeds — two
    exports of "the same project" are otherwise indistinguishable in a downloads
    folder, and the one you kept is not necessarily the one you meant.
    """
    commit = (aps.head_commit(project) or "").strip()[:8]
    stem = _zip_stem(project)
    return f"{stem}-{commit}.zip" if commit else f"{stem}.zip"


#: Output of *running* the suite, not the suite. The project's file walk keeps these
#: because they legitimately live in the tree, but an export is a source drop: shipping
#: a customer last night's reporter output invites them to read a stale result as
#: theirs. Excluded here only — the agent bundle and the DB mirror are unaffected.
_RUN_ARTIFACTS = ("results.json",)
_RUN_ARTIFACT_DIRS = ("playwright-report/", "test-results/", "blob-report/")


def _is_run_artifact(relative: str) -> bool:
    return relative in _RUN_ARTIFACTS or relative.startswith(_RUN_ARTIFACT_DIRS)


def export_to_zip(project: AutomationProject) -> bytes:
    """The project's automation suite as a ZIP archive, built in memory.

    Contents follow the project's own file walk, which already excludes
    ``node_modules`` (installed on the other side, never shipped), ``.git`` (the
    export is a source drop, not a history transplant) and ``.qagent`` (server-side
    plans and inventory the customer has no business receiving). Unlike
    :func:`automation_project_service.bundle_for_agent`, ``tests/**`` **is**
    included — the specs are the point of an export, where for the agent they are
    staged per run instead.

    Run artifacts (``results.json``, ``playwright-report/``, ``test-results/``) are
    excluded on top of that — they are the output of *running* the suite, not the
    suite, and a stale reporter file in a customer's download reads as their result.

    Everything is nested under one top-level directory named for the project, so
    unzipping cannot scatter files across the user's working directory.

    Files are added in sorted order with a fixed timestamp, so exporting the same
    commit twice produces byte-identical archives — which is what makes "did
    anything actually change?" answerable by comparing two downloads.

    Raises:
        ExportError: when the project has no files on disk yet, which is a clearer
            answer than handing back a valid, empty archive.
    """
    root = aps.project_dir(project)
    files = [
        path
        for path in (aps._project_files(root) if root.is_dir() else [])
        if not _is_run_artifact(path.relative_to(root).as_posix())
    ]
    if not files:
        raise ExportError(
            "empty_project",
            "This automation project has no files to export yet — generate its "
            "automation first, then export.",
        )

    top = _zip_stem(project)
    buffer = io.BytesIO()
    # DEFLATE, not STORE: a Playwright suite is all text and compresses ~4x, and
    # this archive is streamed through a response.
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            relative = path.relative_to(root).as_posix()
            # Fixed mtime — see the determinism note above. ZIP's epoch starts at
            # 1980, so this is the earliest legal value rather than an arbitrary one.
            info = zipfile.ZipInfo(f"{top}/{relative}", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            try:
                archive.writestr(info, path.read_bytes())
            except OSError as exc:  # pragma: no cover - unreadable file mid-walk
                logger.warning("export_to_zip: skipping unreadable {}: {}", relative, exc)
    logger.info(
        "exported automation project {} as a zip ({} files, {} bytes)",
        project.id,
        len(files),
        buffer.tell(),
    )
    return buffer.getvalue()
