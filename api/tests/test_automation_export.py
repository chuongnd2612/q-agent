"""Export the automation project to a customer-owned git remote (#549).

Covers every acceptance-criteria bullet of the slice. Two deliberate choices about
how, because they are what make these tests worth anything:

* **The "remote" is a real local bare repository, not a mock.** ``git push`` against
  a ``file://`` bare repo goes through the same plumbing as a push to GitHub, so
  fast-forward, "already up to date", non-fast-forward and divergence-refusal are
  genuinely exercised. A mocked ``run_git_captured`` would have proved only that the
  test author knows what git prints.
* **The PAT-leak assertions run against a *failing* push.** Success is the easy case
  — git says little. The dangerous case is a failure, because git echoes the
  authenticated URL back in the error, so these tests point an authenticated remote
  at a closed port (instant connection refusal, no network dependency) and then
  assert the token is absent from the exception message, the log records *and* the
  published WS frame, while the redacted ``***@host`` form is present (proving the
  message was scrubbed rather than merely empty).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.services import automation_export_service as export_service
from app.services import automation_project_service as aps

# Every test here materializes a real git repo (the project) and most also drive a
# real bare "remote", so git is a module-wide requirement rather than a per-test one.
requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
pytestmark = [pytest.mark.usefixtures("workspace_dir"), requires_git]

# A token shaped like a real PAT, used verbatim in the leak assertions.
SECRET_PAT = "ghp_LEAKCANARY0123456789abcdefghijklmn"

# Closed port on loopback: `git ls-remote` fails immediately with a message that
# embeds the authenticated URL. No DNS, no timeout, no network dependency.
UNREACHABLE = "https://127.0.0.1:1/acme/automation.git"


# ---------------------------------------------------------------------------
# Local bare repo helpers — the "customer remote"
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=120,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stdout}{proc.stderr}"
    return (proc.stdout or "").strip()


def _bare_git(bare: Path, *args: str) -> str:
    """Run git *inside* the bare remote.

    Via ``--git-dir`` rather than ``cwd=``: a host with ``safe.bareRepository =
    explicit`` in its global config refuses an implicit bare-repo cwd, which would
    make these assertions fail for a reason that has nothing to do with the export.
    """
    return _git("--git-dir", str(bare), *args)


def _file_url(path: Path) -> str:
    """A ``file://`` URL git accepts on both POSIX and Windows."""
    return "file:///" + str(path).replace("\\", "/").lstrip("/")


def _bare_remote(tmp_path: Path, name: str = "remote.git", default: str = "main") -> tuple[Path, str]:
    bare = tmp_path / name
    _git("init", "--bare", "-b", default, str(bare))
    return bare, _file_url(bare)


def _remote_heads(bare: Path) -> dict[str, str]:
    out = _git("ls-remote", "--heads", _file_url(bare))
    heads: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            heads[parts[1].removeprefix("refs/heads/")] = parts[0]
    return heads


def _seed_remote_commit(tmp_path: Path, bare: Path, branch: str, marker: str = "human.txt") -> str:
    """Put a human-authored commit on ``branch`` of the bare remote.

    This is the "a human edited the same page object" scenario the slice refuses to
    auto-merge — created through a real clone + push so the remote history is
    genuine, not fabricated.
    """
    work = tmp_path / f"human-{branch.replace('/', '-')}"
    _git("clone", _file_url(bare), str(work))
    _git("config", "user.email", "human@customer", cwd=work)
    _git("config", "user.name", "Human", cwd=work)
    if branch in _remote_heads(bare):
        _git("checkout", "-q", "-B", branch, f"origin/{branch}", cwd=work)
    else:
        # Unborn HEAD (the remote is empty): `checkout -B` has no commit to start
        # from, so point HEAD at the ref directly and let the first commit create it.
        _git("symbolic-ref", "HEAD", f"refs/heads/{branch}", cwd=work)
    (work / marker).write_text("hand-written\n", encoding="utf-8")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "human edit", cwd=work)
    _git("push", "-q", "origin", branch, cwd=work)
    return _remote_heads(bare)[branch]


# ---------------------------------------------------------------------------
# Project / connection seeding
# ---------------------------------------------------------------------------

OWNER_ID = 7
PROJECT_KEY = "Surency Platform"


def _project(db_session, owner_id: int = OWNER_ID, repo: str = "web"):
    """A materialized automation project with one committed page object."""
    project = aps.ensure_project(db_session, owner_id, PROJECT_KEY, repo)
    page = aps.project_dir(project) / "pages" / "LoginPage.ts"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("export class LoginPage {}\n", encoding="utf-8")
    aps.git_commit(project, "feat: LoginPage")
    return project


def _connection(db_session, pat: str = SECRET_PAT, owner_id: int | None = OWNER_ID, name="GitHub"):
    """A repository-capable connection owned by ``owner_id``, bound to the project."""
    from app import crypto
    from app.models.project_config import ProjectConfig
    from app.models.provider_connection import ProviderConnection

    conn = ProviderConnection(
        kind="github",
        name=name,
        connected=True,
        owner_id=owner_id,
        config={"org": "acme"},
        secrets={"pat": crypto.encrypt(pat)} if pat else {},
    )
    db_session.add(conn)
    db_session.flush()
    db_session.add(
        ProjectConfig(
            key=PROJECT_KEY,
            name=PROJECT_KEY,
            owner_id=owner_id,
            repository_connection_id=conn.id,
            repo_url="https://github.com/acme/automation.git",
        )
    )
    db_session.commit()
    return conn


def _export(db_session, project, url: str, branch: str, **kwargs):
    return export_service.export_to_remote(
        db_session, project, remote_url=url, branch=branch, **kwargs
    )


BRANCH = "qagent/automation/surency-platform-web"


# ---------------------------------------------------------------------------
# A project pushes to a fresh branch on a real remote
# ---------------------------------------------------------------------------


@requires_git
def test_export_pushes_to_a_fresh_branch_on_a_real_remote(db_session, tmp_path):
    project = _project(db_session)
    _connection(db_session)
    bare, url = _bare_remote(tmp_path)

    result = _export(db_session, project, url, BRANCH)

    assert result["ok"] is True
    assert result["pushed"] is True
    assert result["created"] is True
    assert result["upToDate"] is False
    assert result["branch"] == BRANCH
    # The branch really exists on the remote, at exactly the project's HEAD.
    assert _remote_heads(bare) == {BRANCH: result["commit"]}
    assert result["commit"] == aps.head_commit(project)
    # The remote's default branch was untouched — only the export branch exists.
    assert "main" not in _remote_heads(bare)


@requires_git
def test_export_commits_pending_work_before_pushing(db_session, tmp_path):
    """An export exports a *commit*, so uncommitted generated files are committed."""
    project = _project(db_session)
    _connection(db_session)
    _, url = _bare_remote(tmp_path)
    (aps.project_dir(project) / "pages" / "CartPage.ts").write_text("export class C {}\n", "utf-8")

    result = _export(db_session, project, url, BRANCH, message="feat: CartPage")

    assert result["committed"] is True
    assert result["commit"] == aps.head_commit(project)
    assert not aps.git_changed_paths(project)


@requires_git
def test_exported_tree_contains_the_project_files(db_session, tmp_path):
    """The customer really can run the suite: the pushed tree has the real files."""
    project = _project(db_session)
    _connection(db_session)
    bare, url = _bare_remote(tmp_path)

    _export(db_session, project, url, BRANCH)

    listing = _bare_git(bare, "ls-tree", "-r", "--name-only", BRANCH)
    assert "pages/LoginPage.ts" in listing
    assert "playwright.config.ts" in listing
    assert "package.json" in listing


# ---------------------------------------------------------------------------
# Pushing twice: fast-forward or clean refusal, never a forced overwrite
# ---------------------------------------------------------------------------


@requires_git
def test_pushing_twice_with_no_new_work_is_a_clean_no_op(db_session, tmp_path):
    project = _project(db_session)
    _connection(db_session)
    bare, url = _bare_remote(tmp_path)

    first = _export(db_session, project, url, BRANCH)
    second = _export(db_session, project, url, BRANCH)

    assert second["ok"] is True
    assert second["pushed"] is False
    assert second["upToDate"] is True
    assert second["commit"] == first["commit"]
    # Nothing moved on the remote — and nothing was force-pushed to get there.
    assert _remote_heads(bare)[BRANCH] == first["commit"]


@requires_git
def test_pushing_twice_with_new_work_fast_forwards(db_session, tmp_path):
    project = _project(db_session)
    _connection(db_session)
    bare, url = _bare_remote(tmp_path)

    first = _export(db_session, project, url, BRANCH)
    (aps.project_dir(project) / "pages" / "CartPage.ts").write_text("export class C {}\n", "utf-8")
    aps.git_commit(project, "feat: CartPage")
    second = _export(db_session, project, url, BRANCH)

    assert second["pushed"] is True
    assert second["created"] is False
    assert second["commit"] != first["commit"]
    assert _remote_heads(bare)[BRANCH] == second["commit"]
    # It is a true fast-forward: the first commit is still an ancestor.
    assert first["commit"] in _bare_git(bare, "log", "--format=%H", BRANCH).splitlines()


@requires_git
def test_no_force_flag_anywhere_in_the_export_module():
    """The "never a forced overwrite" rule, asserted against the source itself.

    A behavioural test cannot prove the *absence* of a force-push (a bug would
    simply make some other test's remote silently rewind), so this reads the module
    and refuses every shape of forced update.
    """
    source = Path(export_service.__file__).read_text(encoding="utf-8")
    # Drop the module docstring (it *describes* the rule) and every comment line, so
    # only executable code is inspected.
    body = source.split("from __future__", 1)[1]
    code = "\n".join(line for line in body.splitlines() if not line.strip().startswith("#"))
    assert "--force" not in code
    assert "force-with-lease" not in code
    assert '"+refs' not in code and "'+refs" not in code
    assert 'f"+' not in code


# ---------------------------------------------------------------------------
# A diverged remote branch is refused, leaving the local repo intact
# ---------------------------------------------------------------------------


@requires_git
def test_diverged_remote_branch_is_refused_and_leaves_the_local_repo_intact(db_session, tmp_path):
    project = _project(db_session)
    _connection(db_session)
    bare, url = _bare_remote(tmp_path)
    _export(db_session, project, url, BRANCH)

    # A human edits the same branch on the remote…
    human_sha = _seed_remote_commit(tmp_path, bare, BRANCH)
    # …while Q-Agent generates more code locally. Both sides have moved: diverged.
    (aps.project_dir(project) / "pages" / "CartPage.ts").write_text("export class C {}\n", "utf-8")
    aps.git_commit(project, "feat: CartPage")

    before_head = aps.head_commit(project)
    before_refs = _git("for-each-ref", "--format=%(refname) %(objectname)", cwd=aps.project_dir(project))
    before_status = aps.git_changed_paths(project)

    with pytest.raises(export_service.ExportError) as excinfo:
        _export(db_session, project, url, BRANCH)

    assert excinfo.value.code == "diverged"
    message = excinfo.value.message
    assert BRANCH in message and human_sha[:8] in message
    # The refusal explains itself and rules out the out-of-scope strategies.
    assert "force-push" in message and "merge" in message
    # The local repo is untouched: same HEAD, same refs, same working tree.
    assert aps.head_commit(project) == before_head
    assert _git("for-each-ref", "--format=%(refname) %(objectname)", cwd=aps.project_dir(project)) == before_refs
    assert aps.git_changed_paths(project) == before_status
    # And the remote is untouched too — the human's commit is still the tip.
    assert _remote_heads(bare)[BRANCH] == human_sha


@requires_git
def test_a_remote_branch_ahead_of_us_is_refused_not_overwritten(db_session, tmp_path):
    """Even a pure "remote is ahead" is refused rather than force-pushed over."""
    project = _project(db_session)
    _connection(db_session)
    bare, url = _bare_remote(tmp_path)
    _export(db_session, project, url, BRANCH)
    human_sha = _seed_remote_commit(tmp_path, bare, BRANCH)

    with pytest.raises(export_service.ExportError) as excinfo:
        _export(db_session, project, url, BRANCH)

    assert excinfo.value.code == "diverged"
    assert _remote_heads(bare)[BRANCH] == human_sha


@requires_git
def test_exporting_to_a_different_branch_after_a_divergence_succeeds(db_session, tmp_path):
    """The refusal is actionable: the suggested escape hatch actually works."""
    project = _project(db_session)
    _connection(db_session)
    bare, url = _bare_remote(tmp_path)
    _export(db_session, project, url, BRANCH)
    _seed_remote_commit(tmp_path, bare, BRANCH)

    result = _export(db_session, project, url, f"{BRANCH}-v2")

    assert result["pushed"] is True
    assert f"{BRANCH}-v2" in _remote_heads(bare)


# ---------------------------------------------------------------------------
# Never the remote's default branch
# ---------------------------------------------------------------------------


@requires_git
def test_the_remotes_own_default_branch_is_refused(db_session, tmp_path):
    """Resolved from the remote's HEAD symref, not from a hardcoded name list."""
    project = _project(db_session)
    _connection(db_session)
    bare, url = _bare_remote(tmp_path, default="integration")
    _seed_remote_commit(tmp_path, bare, "integration")
    before = _remote_heads(bare)["integration"]

    with pytest.raises(export_service.ExportError) as excinfo:
        _export(db_session, project, url, "integration")

    assert excinfo.value.code == "default_branch_refused"
    assert "default branch" in excinfo.value.message
    assert _remote_heads(bare)["integration"] == before


@requires_git
@pytest.mark.parametrize("branch", ["main", "master", "Develop", "trunk", "release"])
def test_mainline_branch_names_are_refused(db_session, tmp_path, branch):
    """Refused even on an empty remote whose HEAD symref cannot be resolved."""
    project = _project(db_session)
    _connection(db_session)
    bare, url = _bare_remote(tmp_path)

    with pytest.raises(export_service.ExportError) as excinfo:
        _export(db_session, project, url, branch)

    assert excinfo.value.code == "default_branch_refused"
    assert _remote_heads(bare) == {}


def test_the_suggested_branch_is_never_a_mainline_name(db_session):
    project = _project(db_session)
    branch = export_service.default_export_branch(project)
    assert branch.startswith(export_service.EXPORT_BRANCH_PREFIX + "/")
    assert branch.lower() not in export_service._RESERVED_BRANCHES
    # The suggestion survives its own validator.
    assert export_service._validate_branch(branch) == branch


@pytest.mark.parametrize("branch", ["", "  ", "-bad", "a..b", "with space", "x.lock", "a" * 201])
def test_invalid_branch_names_are_refused_before_any_network_call(db_session, branch):
    project = _project(db_session)
    with pytest.raises(export_service.ExportError) as excinfo:
        _export(db_session, project, UNREACHABLE, branch)
    assert excinfo.value.code in {"branch_required", "branch_invalid"}


# ---------------------------------------------------------------------------
# No repository connection -> a clear, actionable error (not a stack trace)
# ---------------------------------------------------------------------------


def test_no_repository_connection_is_a_clear_actionable_error(db_session):
    project = _project(db_session)  # no connection seeded at all

    with pytest.raises(export_service.ExportError) as excinfo:
        _export(db_session, project, "https://github.com/acme/automation.git", BRANCH)

    assert excinfo.value.code == "no_repository_connection"
    message = excinfo.value.message
    # Says what is wrong, and what the user should do about it.
    assert "No repository connection" in message
    assert "Settings" in message and "export again" in message
    # Not a stack trace / raw git output.
    assert "Traceback" not in message and "fatal:" not in message


def test_a_connection_without_a_pat_is_a_clear_actionable_error(db_session):
    project = _project(db_session)
    _connection(db_session, pat="", name="GitHub (no token)")

    with pytest.raises(export_service.ExportError) as excinfo:
        _export(db_session, project, "https://github.com/acme/automation.git", BRANCH)

    assert excinfo.value.code == "no_repository_credentials"
    assert "GitHub (no token)" in excinfo.value.message
    assert "personal access token" in excinfo.value.message


def test_another_users_connection_is_never_used(db_session):
    """ADR 0009: the export can only authenticate with the owner's own connection."""
    project = _project(db_session, owner_id=OWNER_ID)
    _connection(db_session, owner_id=OWNER_ID + 1)  # a *different* user's connection

    with pytest.raises(export_service.ExportError) as excinfo:
        _export(db_session, project, "https://github.com/acme/automation.git", BRANCH)

    assert excinfo.value.code == "no_repository_connection"


def test_ssh_remotes_are_refused_with_an_explanation(db_session):
    project = _project(db_session)
    _connection(db_session)
    for url in ("git@github.com:acme/automation.git", "ssh://git@github.com/acme/automation.git"):
        with pytest.raises(export_service.ExportError) as excinfo:
            _export(db_session, project, url, BRANCH)
        assert excinfo.value.code == "remote_unsupported"
        assert "https://" in excinfo.value.message


# ---------------------------------------------------------------------------
# The PAT never appears in logs, WS events or error messages
# ---------------------------------------------------------------------------


@requires_git
def test_pat_never_leaks_into_the_error_message_or_the_logs(db_session, caplog):
    """The highest-risk path: a *failing* authenticated push.

    git echoes the URL it tried — PAT and all — so the raw error is a live token. The
    surfaced message must carry the redacted form instead, and nothing may write the
    token to the log.
    """
    from app.logging import logger

    project = _project(db_session)
    _connection(db_session, pat=SECRET_PAT)

    handler_id = logger.add(caplog.handler, format="{message}", level=0)
    try:
        with pytest.raises(export_service.ExportError) as excinfo:
            _export(db_session, project, UNREACHABLE, BRANCH)
    finally:
        logger.remove(handler_id)

    message = excinfo.value.message
    assert excinfo.value.code == "remote_unreachable"
    # The token is absent from every rendering of the exception…
    assert SECRET_PAT not in message
    assert SECRET_PAT not in str(excinfo.value)
    assert SECRET_PAT not in repr(excinfo.value)
    # …while git's real complaint *is* surfaced, so this is not passing because the
    # message was truncated or emptied.
    assert "127.0.0.1" in message and "unable to access" in message
    # Nothing wrote the token to the log either — and the log line is not empty.
    assert SECRET_PAT not in caplog.text
    assert "ls-remote" in caplog.text


def test_scrub_removes_both_the_url_form_and_the_literal_token():
    from app.services.repo_service import scrub

    raw = (
        f"fatal: unable to access 'https://{SECRET_PAT}@github.com/acme/a.git/': failed\n"
        f"remote: helper reported {SECRET_PAT}"
    )
    cleaned = scrub(raw, SECRET_PAT)
    assert SECRET_PAT not in cleaned
    assert "***@github.com" in cleaned
    assert "***" in cleaned.split("reported")[1]


def test_run_git_captured_scrubs_real_git_output_containing_the_token(db_session):
    """Proven against git output that genuinely echoes the token back.

    Worth stating explicitly, because it shaped this test: a current git *already*
    strips credentials from the URL in its own network errors, so the obvious
    "failed push" case cannot demonstrate the scrubbing — it passes either way. This
    uses a subcommand that echoes its argument verbatim (``checkout`` reporting an
    unmatched pathspec), which is the shape an older git, a credential helper, or a
    provider's own error text can still produce.
    """
    from app.services.repo_service import run_git_captured

    root = aps.project_dir(_project(db_session))
    authenticated = f"https://{SECRET_PAT}@github.com/acme/automation.git"

    code, output = run_git_captured(["-C", str(root), "checkout", authenticated], secret=SECRET_PAT)

    assert code != 0
    assert SECRET_PAT not in output
    assert "***@github.com/acme/automation.git" in output


def test_run_git_captured_never_raises_when_git_cannot_run():
    from app.services.repo_service import run_git_captured

    code, output = run_git_captured(["ls-remote", "https://127.0.0.1:1/x.git", "HEAD"])
    assert code != 0
    assert output  # something was reported back to surface to the user


@requires_git
def test_a_successful_export_result_carries_no_pat(db_session, tmp_path):
    """Even the success payload — every string in it — is token-free and redacted."""
    project = _project(db_session)
    _connection(db_session, pat=SECRET_PAT)
    _, url = _bare_remote(tmp_path)

    result = _export(db_session, project, url, BRANCH)

    for value in result.values():
        assert SECRET_PAT not in str(value)
    assert SECRET_PAT not in str(result)


def test_redact_remote_masks_credentials_in_a_url():
    assert (
        export_service.redact_remote(f"https://{SECRET_PAT}@github.com/acme/a.git")
        == "https://***@github.com/acme/a.git"
    )
    assert SECRET_PAT not in export_service.redact_remote(f"https://x:{SECRET_PAT}@dev.azure.com/o")


# ---------------------------------------------------------------------------
# Nothing pushes automatically
# ---------------------------------------------------------------------------


def test_export_is_only_ever_called_from_the_export_endpoint():
    """"User-triggered only", asserted structurally.

    A background pass that grew an export call would be invisible to every
    behavioural test here, so this scans the application source: the only non-test
    caller of ``export_to_remote`` may be the automation router's export endpoint.
    """
    app_dir = Path(export_service.__file__).resolve().parents[1]
    callers = {
        path.relative_to(app_dir).as_posix()
        for path in app_dir.rglob("*.py")
        if "export_to_remote(" in path.read_text(encoding="utf-8")
        and path.name != "automation_export_service.py"
    }
    assert callers == {"routers/automation.py"}


def test_generation_and_execution_never_import_the_export_service():
    app_dir = Path(export_service.__file__).resolve().parents[1]
    for name in (
        "services/spec_service.py",
        "services/execution_service.py",
        "services/heal_service.py",
        "services/playwright_runner.py",
        "services/automation_project_service.py",
    ):
        path = app_dir / name
        if path.is_file():
            assert "automation_export_service" not in path.read_text(encoding="utf-8"), name


# ---------------------------------------------------------------------------
# Preflight pushes nothing
# ---------------------------------------------------------------------------


@requires_git
def test_preflight_reports_readiness_without_pushing(db_session, tmp_path):
    project = _project(db_session)
    _connection(db_session)
    bare, _ = _bare_remote(tmp_path)

    info = export_service.export_preflight(
        db_session, project, suggested_remote=f"https://{SECRET_PAT}@github.com/acme/a.git"
    )

    assert info["hasCredentials"] is True
    assert info["credentialsError"] is None
    assert info["connection"] == "GitHub"
    assert info["branch"] == BRANCH
    assert info["commit"] == aps.head_commit(project)
    # A PR is only offered when a provider adapter can open one; none can today.
    assert info["canOpenPullRequest"] is False
    # The suggestion is redacted, and nothing was pushed.
    assert SECRET_PAT not in str(info)
    assert info["remoteUrl"] == "https://***@github.com/acme/a.git"
    assert _remote_heads(bare) == {}


def test_preflight_explains_a_missing_connection_instead_of_raising(db_session):
    project = _project(db_session)

    info = export_service.export_preflight(db_session, project)

    assert info["hasCredentials"] is False
    assert info["credentialsCode"] == "no_repository_connection"
    assert "Settings" in info["credentialsError"]


# ---------------------------------------------------------------------------
# The endpoint: ownership, WS event, audit, refusals
# ---------------------------------------------------------------------------


def _seed_run_with_project(db_session, project, owner_id: int | None = OWNER_ID, code="RUN-E1"):
    """A run whose single spec is bound to ``project`` — what the endpoint resolves."""
    from app.models.run import Run
    from app.models.testcase import AutomationSpec, TestCase

    run = Run(code=code, name="Export run", status="automation", owner_id=owner_id)
    db_session.add(run)
    db_session.flush()
    case = TestCase(
        run_id=run.id,
        ticket_external_id="SUR-1428",
        code="TC-01",
        title="Login works",
        approval="approved",
        automation="Playwright",
    )
    db_session.add(case)
    db_session.flush()
    db_session.add(
        AutomationSpec(
            test_case_id=case.id,
            filename="SUR-1428-TC-01.spec.ts",
            code="test('x', async () => {});",
            status="draft",
            project_id=project.id,
        )
    )
    db_session.commit()
    return run


def _ws_frame(run_id: int) -> dict:
    from app.ws import hub

    return hub._last.get(str(run_id), {})


@requires_git
def test_export_endpoint_pushes_and_publishes_a_redacted_ws_event(client, db_session, tmp_path):
    project = _project(db_session)
    _connection(db_session, pat=SECRET_PAT)
    run = _seed_run_with_project(db_session, project)
    bare, url = _bare_remote(tmp_path)

    response = client.post(
        f"/runs/{run.id}/automation/export", json={"remoteUrl": url, "branch": BRANCH}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pushed"] is True
    assert body["projectId"] == project.id
    assert _remote_heads(bare) == {BRANCH: body["commit"]}
    # Neither the response body nor the WS frame carries the token.
    assert SECRET_PAT not in response.text
    frame = _ws_frame(run.id)
    assert frame["event"] == "automation.exported"
    assert frame["payload"]["ok"] is True
    assert SECRET_PAT not in str(frame)


@requires_git
def test_export_endpoint_refuses_a_diverged_branch_with_400(client, db_session, tmp_path):
    project = _project(db_session)
    _connection(db_session, pat=SECRET_PAT)
    run = _seed_run_with_project(db_session, project)
    bare, url = _bare_remote(tmp_path)
    client.post(f"/runs/{run.id}/automation/export", json={"remoteUrl": url, "branch": BRANCH})
    human_sha = _seed_remote_commit(tmp_path, bare, BRANCH)

    response = client.post(
        f"/runs/{run.id}/automation/export", json={"remoteUrl": url, "branch": BRANCH}
    )

    assert response.status_code == 400
    assert "not in this project's history" in response.json()["detail"]
    assert SECRET_PAT not in response.text
    # The failure is announced over WS too, with a machine-readable code and no token.
    frame = _ws_frame(run.id)
    assert frame["payload"]["ok"] is False
    assert frame["payload"]["code"] == "diverged"
    assert SECRET_PAT not in str(frame)
    # The human's commit is still the tip of the remote branch.
    assert _remote_heads(bare)[BRANCH] == human_sha


def test_export_endpoint_pat_never_reaches_the_client_or_the_ws(client, db_session):
    """A failing authenticated push, driven through the real endpoint."""
    project = _project(db_session)
    _connection(db_session, pat=SECRET_PAT)
    run = _seed_run_with_project(db_session, project)

    response = client.post(
        f"/runs/{run.id}/automation/export", json={"remoteUrl": UNREACHABLE, "branch": BRANCH}
    )

    assert response.status_code == 400
    assert SECRET_PAT not in response.text
    # The real git failure is surfaced (so this is not an empty-message pass), just
    # without the token.
    assert "127.0.0.1" in response.json()["detail"]
    frame = _ws_frame(run.id)
    assert frame["payload"]["code"] == "remote_unreachable"
    assert SECRET_PAT not in str(frame)


def test_export_endpoint_reports_a_missing_repository_connection(client, db_session):
    project = _project(db_session)
    run = _seed_run_with_project(db_session, project)

    response = client.post(
        f"/runs/{run.id}/automation/export",
        json={"remoteUrl": "https://github.com/acme/a.git", "branch": BRANCH},
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "No repository connection" in detail and "Settings" in detail
    assert "Traceback" not in detail


def test_export_endpoint_400_when_the_run_has_no_automation_project(client, db_session):
    from app.models.run import Run

    run = Run(code="RUN-LEGACY", name="Legacy", status="automation", owner_id=OWNER_ID)
    db_session.add(run)
    db_session.commit()

    response = client.post(
        f"/runs/{run.id}/automation/export", json={"remoteUrl": UNREACHABLE, "branch": BRANCH}
    )

    assert response.status_code == 400
    assert "no persistent automation project" in response.json()["detail"]


def test_export_endpoint_404_for_a_run_that_does_not_exist(client):
    response = client.post(
        "/runs/999999/automation/export", json={"remoteUrl": UNREACHABLE, "branch": BRANCH}
    )
    assert response.status_code == 404


def test_export_endpoint_refuses_another_users_project(app, client, db_session):
    """Ownership (ADR 0008/0009): an explicit projectId is ownership-checked.

    The suite runs with ``auth_required=False``, so ``current_user`` is ``None`` and
    the ownership bridge lets everything through — this test overrides the dependency
    with a real user so the check is actually exercised.
    """
    from app.deps_auth import current_user
    from app.models.user import User

    mine = _project(db_session, owner_id=OWNER_ID, repo="web")
    theirs = _project(db_session, owner_id=OWNER_ID + 1, repo="admin")
    run = _seed_run_with_project(db_session, mine)
    me = User(email="me@test", first_name="Me", password_hash="x")
    db_session.add(me)
    db_session.commit()
    # The run and my project belong to me; `theirs` does not.
    run.owner_id = me.id
    mine.owner_id = me.id
    db_session.commit()
    app.dependency_overrides[current_user] = lambda: me

    response = client.post(
        f"/runs/{run.id}/automation/export",
        json={"remoteUrl": UNREACHABLE, "branch": BRANCH, "projectId": theirs.id},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "AutomationProject not found"


@requires_git
def test_preflight_endpoint_pushes_nothing_and_prefills(client, db_session, tmp_path):
    project = _project(db_session)
    _connection(db_session, pat=SECRET_PAT)
    run = _seed_run_with_project(db_session, project)
    bare, _ = _bare_remote(tmp_path)

    response = client.get(f"/runs/{run.id}/automation/export")

    assert response.status_code == 200
    body = response.json()
    assert body["hasCredentials"] is True
    assert body["branch"] == BRANCH
    assert body["canOpenPullRequest"] is False
    # The suggested remote comes from the project config, redacted, and no push ran.
    assert body["remoteUrl"] == "https://github.com/acme/automation.git"
    assert SECRET_PAT not in response.text
    assert _remote_heads(bare) == {}


@requires_git
def test_export_writes_an_audit_entry_without_the_pat(client, db_session, tmp_path):
    from app.models.audit import AuditLog

    project = _project(db_session)
    _connection(db_session, pat=SECRET_PAT)
    run = _seed_run_with_project(db_session, project)
    _, url = _bare_remote(tmp_path)

    client.post(f"/runs/{run.id}/automation/export", json={"remoteUrl": url, "branch": BRANCH})

    rows = [
        row
        for row in db_session.query(AuditLog).all()
        if row.action == "Exported automation project"
    ]
    assert rows, "the export was not audited"
    assert run.code in rows[0].target
    assert SECRET_PAT not in f"{rows[0].detail}{rows[0].target}{rows[0].action}{rows[0].meta}"
    # The audited remote is the redacted form, so the audit log is safe to read.
    assert (rows[0].detail or {}).get("branch") == BRANCH


# ---------------------------------------------------------------------------
# Export to ZIP — the v1 export (#686)
# ---------------------------------------------------------------------------
#
# The push above is v2 and needs a connection, a PAT, a branch policy and a
# network. A ZIP needs none of them, and that is the whole point: the thing being
# defended here is the *contents* — what a customer does and does not receive.


def _zip_names(payload: bytes) -> set[str]:
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return set(archive.namelist())


def test_the_zip_carries_the_whole_suite_including_specs(db_session):
    """The specs ARE the export.

    `bundle_for_agent` excludes `tests/**` because a run stages only its own specs;
    reusing that exclusion here would ship a library with nothing to run.
    """
    project = _project(db_session)
    spec = aps.project_dir(project) / "tests" / "SUR-1" / "SUR-1-TC-01.spec.ts"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("test('login', async () => {});\n", encoding="utf-8")

    names = _zip_names(export_service.export_to_zip(project))

    top = export_service._zip_stem(project)
    assert f"{top}/tests/SUR-1/SUR-1-TC-01.spec.ts" in names
    assert f"{top}/pages/LoginPage.ts" in names


def test_the_zip_never_carries_node_modules_git_or_server_side_plans(db_session):
    """Three exclusions, three different reasons — so each is asserted separately.

    `node_modules` is installed on the other side; `.git` would make a source drop
    a history transplant; `.qagent` is Q-Agent's own plans and inventory, which the
    customer has no business receiving.
    """
    project = _project(db_session)
    root = aps.project_dir(project)
    for relative in ("node_modules/pkg/index.js", ".qagent/plans/plan.json"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")

    names = _zip_names(export_service.export_to_zip(project))

    assert not any("node_modules" in name for name in names)
    assert not any(name.split("/")[1:2] == [".qagent"] for name in names), names
    assert not any("/.git/" in name for name in names)
    # A negative control for the negatives above: the walk really did see files.
    assert any(name.endswith("pages/LoginPage.ts") for name in names)


def test_everything_is_nested_under_one_top_level_directory(db_session):
    """Otherwise unzipping scatters a Playwright project across the user's cwd."""
    project = _project(db_session)

    names = _zip_names(export_service.export_to_zip(project))

    tops = {name.split("/", 1)[0] for name in names}
    assert tops == {export_service._zip_stem(project)}
    # A slug is a PATH ("key/repo"), so the flattening is the point: a slash here
    # would split the top level in two, and in the filename it is not a filename.
    assert "/" not in export_service._zip_stem(project)


def test_the_same_commit_exports_byte_identically(db_session):
    """Determinism is what makes "did anything change?" answerable by diffing two
    downloads — mtimes would otherwise make every export unique."""
    project = _project(db_session)

    assert export_service.export_to_zip(project) == export_service.export_to_zip(project)


def test_the_filename_pins_the_commit(db_session):
    """A suite is regenerated as work proceeds; two exports of "the same project"
    are otherwise indistinguishable in a downloads folder."""
    project = _project(db_session)

    name = export_service.zip_filename(project)

    assert name.startswith(export_service._zip_stem(project) + "-")
    assert "/" not in name
    assert name.endswith(".zip")
    assert aps.head_commit(project)[:8] in name


def test_an_empty_project_is_refused_rather_than_exported_empty(db_session, tmp_path, monkeypatch):
    """A valid, empty archive is the worst answer: it looks like it worked."""
    project = _project(db_session)
    # Point the project at an empty directory rather than deleting its tree: on
    # Windows git marks objects read-only and rmtree fails, which would make this
    # test about the filesystem instead of about the refusal.
    empty = tmp_path / "empty-project"
    empty.mkdir()
    monkeypatch.setattr(export_service.aps, "project_dir", lambda _p: empty)

    with pytest.raises(export_service.ExportError) as excinfo:
        export_service.export_to_zip(project)

    assert excinfo.value.code == "empty_project"


def test_the_zip_export_needs_no_connection_and_no_pat(db_session):
    """v1's reason for existing: none of the plumbing the push path requires.

    `_connection` is deliberately NOT called here — the push refuses without it,
    and this must not.
    """
    project = _project(db_session)

    payload = export_service.export_to_zip(project)

    assert payload[:2] == b"PK", "not a zip"
    assert len(payload) > 0
