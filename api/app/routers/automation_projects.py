"""Project-keyed automation repo browsing (#768, part of #765).

``GET /runs/{id}/automation`` embeds every file's full ``code``. That is right
for the run overlay — one project, one screen, already loaded — and **wrong for
a browser**: a 200-file project is megabytes of source the user never opens.
This router splits that into a metadata tree plus lazy per-file content, keyed on
the **project GUID** rather than on a run, so ADR 0014's *"the run is an event;
the artifacts it produces are not"* finally has a read path.

Why a separate module rather than an addition to an existing one:

* Not ``routers/projects.py`` — every path there is keyed on ``{key}``, the
  project **name**. Mixing two identifiers in one path space is a footgun for
  the next person adding a route.
* Not ``routers/automation.py`` — that router is prefix-less and run-keyed by
  design, and its ownership helper (``_export_project_or_404``) resolves through
  a ``Run``, which is exactly the coupling this slice removes.
* A new file is also file-disjoint from the rest of #765, which is what lets the
  remaining slices land in parallel.

**Resolution never goes through ``AutomationProject.project_key``.** That column
holds the *provider* project key, so a q-agent project named ``demo`` can
legitimately own an automation repo keyed ``surency`` — matching on the name
resolves nothing. Everything here resolves via
``automation_project_service.projects_for_guid``, whose two legs (the stamped
``project_guid`` column, and the ``automation_specs -> test_cases ->
runs.project_guid`` join) between them find both a freshly scaffolded repo with
no specs yet and a pre-#766 repo whose column was never stamped. One repo can be
shared by several projects, and is then reachable from **every** one of them.

Content is served from the ``automation_files`` **mirror, never from disk**:
the tree and the viewer can then never disagree about what exists, a read
endpoint touches no filesystem, and path traversal is structurally impossible
because the only path that resolves is one matching a mirror row exactly.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps_auth import current_user
from app.models.automation_project import AutomationFile, AutomationProject
from app.models.execution import Execution, ExecutionResult
from app.models.run import Run
from app.models.testcase import AutomationSpec, TestCase
from app.models.user import User
from app.schemas import (
    AutomationFileOut,
    AutomationRepoOut,
    AutomationTreeFileOut,
    AutomationTreeOut,
    ProjectExecutionStart,
    SpecProvenanceEntryOut,
    SpecProvenanceOut,
)
from app.routers.execution import _require_paired_device, _resolve_target
from app.services import audit_service, automation_export_service, project_execution
from app.services import automation_project_service as aps
from app.services.ownership import check_owned_or_404, owned

router = APIRouter(prefix="/projects/{project_guid}/automation", tags=["automation"])

#: A Windows drive-letter prefix (``C:/…``). ``PurePosixPath.is_absolute()`` does
#: not consider this absolute, so it is checked separately — the mirror lookup
#: would miss it anyway, but a malformed path should say so rather than read as
#: "no such file".
_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")


def _project_or_404(
    db: Session, project_guid: str, project_id: int, user: User | None
) -> AutomationProject:
    """The automation repo ``project_id``, proven to belong to ``project_guid``.

    Three checks, and all three have to be here rather than at the call sites:

    1. The row is one of the repos ``projects_for_guid`` resolves for this GUID,
       so a ``project_id`` that exists but belongs to a **different** project
       cannot be read by naming it in the path. It 404s; it never hops.
    2. ``owner_id`` matches the caller. ``projects_for_guid`` already filters on
       it, and :func:`ownership.check_owned_or_404` re-states it so the guarantee
       does not live only inside a query this module does not own.
    3. Missing rows and forbidden rows are indistinguishable — both 404, per
       ADR 0008/0009. A 403 would confirm the row exists to someone who is not
       allowed to know that.

    Args:
        db: Active session.
        project_guid: The owning project's GUID, from the path.
        project_id: The ``AutomationProject`` id, from the path.
        user: The caller, or ``None`` when auth enforcement is off (the #91
            ownership bridge) — in which case the un-owned/shared namespace is
            what resolves.

    Returns:
        The resolved :class:`AutomationProject`.

    Raises:
        HTTPException: 404 when the repo does not exist, is not reachable from
            this ``project_guid``, or belongs to another user.
    """
    owner_id = user.id if user is not None else None
    reachable = aps.projects_for_guid(db, project_guid, owner_id)
    project = next((row for row in reachable if row.id == project_id), None)
    if project is None:
        raise HTTPException(status_code=404, detail="AutomationProject not found")
    check_owned_or_404(project, user, not_found="AutomationProject not found")
    return project


def _validated_path(path: str) -> str:
    """A project-relative POSIX path, or a 400 that names what was wrong.

    Rejected **before** the mirror is queried, deliberately. The lookup is an
    exact match against a row whose ``path`` is always project-relative POSIX, so
    ``../../etc/passwd`` could only ever miss — and a silent 404 on malformed
    input tells a caller "no such file" when the truth is "that is not a path
    this API accepts". Absolute paths, ``..`` segments and backslashes are the
    three shapes that mean the caller is addressing the filesystem rather than
    the project.

    Args:
        path: The raw ``?path=`` query value.

    Returns:
        The path, unchanged, when it is acceptable.

    Raises:
        HTTPException: 400 with an actionable message.
    """
    value = (path or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="A file path is required.")
    if "\\" in value:
        raise HTTPException(
            status_code=400,
            detail="Automation file paths are POSIX-style; backslashes are not accepted.",
        )
    if value.startswith("/") or _DRIVE_PREFIX_RE.match(value):
        raise HTTPException(
            status_code=400,
            detail="Automation file paths are project-relative; an absolute path is not accepted.",
        )
    if ".." in value.split("/"):
        raise HTTPException(
            status_code=400, detail="Automation file paths may not contain '..' segments."
        )
    return value


def _provenance_entry(
    spec: AutomationSpec, case: TestCase, run: Run, *, stale: bool
) -> SpecProvenanceEntryOut:
    """One ``(spec, case, run)`` triple as the wire shape the client types expect.

    ``history`` entries get the **same** full shape as ``latest`` — the only
    difference is ``stale`` — because the frontend renders them with the same
    component (#767). A narrower object for history would force the client to
    branch on which list a row came from, and would make "show the run that
    actually produced this" impossible for the overwritten rows, which is the one
    thing history exists to answer.

    ``block_reason`` is stored as ``""`` rather than NULL, so it is normalized to
    ``None`` here: "no reason" and "the empty reason" are the same fact, and the
    client's optional field is what says so.

    Args:
        spec: The ``AutomationSpec`` row claiming the file path.
        case: The test case the spec was generated for.
        run: The run that generated it.
        stale: True when a **later** run overwrote the file, so this row's own
            ``code`` is no longer what is on screen (ADR 0014's overwrite rule).

    Returns:
        A populated :class:`SpecProvenanceEntryOut`.
    """
    return SpecProvenanceEntryOut(
        spec_id=spec.id,
        spec_status=spec.status or "",
        block_reason=spec.block_reason or None,
        test_case_id=case.id,
        case_code=case.code or "",
        case_title=case.title or "",
        ticket_external_id=case.ticket_external_id or "",
        run_id=run.id,
        run_code=run.code or "",
        run_name=run.name or "",
        run_status=run.status or "",
        run_created_at=run.created_at,
        run_finished_at=run.finished_at,
        stale=stale,
    )


def _spec_provenance(
    db: Session, project: AutomationProject, path: str, user: User | None
) -> SpecProvenanceOut | None:
    """Which run / ticket / case produced the spec at ``path`` — or ``None``.

    **The join key is ``AutomationSpec.filename``, not ``.path``.**
    ``_project_spec_relpath`` (``routers/automation.py``) documents that
    ``filename`` holds the *project-relative POSIX* path for a project-backed
    spec (``tests/SUR-1428/SUR-1428-TC-01.spec.ts``) — the identical shape to
    ``AutomationFile.path``. ``AutomationSpec.path`` is the **absolute on-disk**
    path and goes stale the moment the workspace directory moves, so joining on it
    would silently return no provenance on any relocated install.

    ``project_id == project.id`` is what keeps the legacy, per-run specs out.
    Those rows predate #538, carry ``project_id IS NULL`` and a **bare basename**
    ``filename`` (``SUR-1428-TC-01.spec.ts``), so a query that omitted the
    ``project_id`` predicate could match a basename from an entirely unrelated
    project and attribute this project's file to that project's run.

    Several rows can legitimately claim one path: ADR 0014 lets run #2 rewrite the
    file while run #1's ``AutomationSpec`` keeps its own copy of the older code.
    The newest run is therefore returned as ``latest`` — it produced the bytes the
    viewer is showing — and the rest as explicit ``history`` with ``stale: true``.
    Neither hiding them (which misrepresents lineage) nor flattening them (which
    misrepresents which one is current) would be honest.

    **Non-spec files return ``None`` on purpose**, and that is a decision rather
    than a gap: pages, components and fixtures are edited across many runs, so
    "the run that made it" is not a fact that exists. Inferring one from
    ``updated_at`` proximity would render a guess as fact — the #178 failure mode.

    Args:
        db: Active session.
        project: The already-resolved, already-ownership-checked repo.
        path: The validated project-relative POSIX path being read.
        user: The caller, or ``None`` under the #91 ownership bridge.

    Returns:
        A :class:`SpecProvenanceOut`, or ``None`` when no ``AutomationSpec`` row
        in this repo claims ``path`` (a shared asset, or a file written before the
        spec row existed).
    """
    stmt = (
        select(AutomationSpec, TestCase, Run)
        .join(TestCase, AutomationSpec.test_case_id == TestCase.id)
        .join(Run, TestCase.run_id == Run.id)
        .where(
            AutomationSpec.project_id == project.id,
            AutomationSpec.filename == path,
        )
        # Newest run first. `AutomationSpec.id` breaks the tie deterministically —
        # `created_at` has second-level granularity in practice, and two runs
        # created inside the same second would otherwise order arbitrarily,
        # making which entry is `latest` a coin flip between requests.
        .order_by(Run.created_at.desc(), AutomationSpec.id.desc())
    )
    # Defence in depth: `_project_or_404` already proved the *repo* is the
    # caller's, but the runs reached through this join are separate owned rows.
    rows = db.execute(owned(stmt, Run, user)).all()
    if not rows:
        return None
    latest = _provenance_entry(*rows[0], stale=False)
    history = [_provenance_entry(*row, stale=True) for row in rows[1:]]
    return SpecProvenanceOut(
        kind="spec",
        overwritten=bool(history),
        latest=latest,
        history=history,
    )


@router.get("/repos", response_model=list[AutomationRepoOut])
def list_project_automation_repos(
    project_guid: str,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> list[AutomationRepoOut]:
    """Every automation repo this project has accumulated — the selector's list.

    Two queries and **no disk, no git**: the resolution query, then one grouped
    aggregate over the mirror::

        SELECT project_id, COUNT(*), SUM(kind = 'spec'), MAX(updated_at)
        FROM automation_files WHERE project_id IN (:ids) GROUP BY project_id

    A repo with no mirrored files still appears (it was scaffolded, it is real,
    and it is the case the empty state is written for); it reports zero counts and
    falls back to the repo row's own ``updated_at``, since ``MAX`` over no rows is
    NULL and the client's ``updatedAt`` is not nullable.

    Returns:
        One entry per repo, ordered by ``repo`` so the default (``""``) repo is
        first. Empty — never a 404 — for a project that has generated nothing:
        "no automation yet" is a state, not an error.
    """
    owner_id = user.id if user is not None else None
    projects = aps.projects_for_guid(db, project_guid, owner_id)
    if not projects:
        return []

    aggregates = {
        row.project_id: row
        for row in db.execute(
            select(
                AutomationFile.project_id.label("project_id"),
                func.count(AutomationFile.id).label("file_count"),
                func.sum(case((AutomationFile.kind == "spec", 1), else_=0)).label("spec_count"),
                func.max(AutomationFile.updated_at).label("updated_at"),
            )
            .where(AutomationFile.project_id.in_([p.id for p in projects]))
            .group_by(AutomationFile.project_id)
        ).all()
    }
    return [
        AutomationRepoOut(
            id=project.id,
            repo=project.repo or "",
            repo_label=project.repo or "default",
            slug=project.slug or "",
            base_version=project.base_version or "",
            file_count=int(getattr(aggregates.get(project.id), "file_count", 0) or 0),
            spec_count=int(getattr(aggregates.get(project.id), "spec_count", 0) or 0),
            updated_at=getattr(aggregates.get(project.id), "updated_at", None)
            or project.updated_at,
        )
        for project in projects
    ]


@router.get("/repos/{project_id}/files", response_model=AutomationTreeOut)
def get_project_automation_tree(
    project_guid: str,
    project_id: int,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> AutomationTreeOut:
    """The selected repo's file tree — **metadata only, no ``code``**.

    That omission is the whole point of the slice: a 200-file project answers in
    ~25KB instead of megabytes, and the source is fetched one file at a time by
    :func:`get_project_automation_file` as the user actually opens it.

    ``size`` is the byte length of the mirror's copy, which is what the viewer
    will render — computed here rather than in SQL because ``length()`` counts
    characters, and a UTF-8 byte count is what a "size" claim has to mean.

    ``head_commit`` **is** resolved here: one ``git rev-parse`` for the one
    selected repo, on a request already doing real work, and the only place the
    commit is displayed. ``""`` when the repo has no commits or git is
    unavailable — a missing commit is not an error worth failing a read over.
    """
    project = _project_or_404(db, project_guid, project_id, user)
    rows = db.execute(
        select(
            AutomationFile.path,
            AutomationFile.kind,
            AutomationFile.code,
            AutomationFile.updated_at,
        )
        .where(AutomationFile.project_id == project.id)
        .order_by(AutomationFile.path)
    ).all()
    files = [
        AutomationTreeFileOut(
            path=row.path,
            kind=row.kind,
            size=len((row.code or "").encode("utf-8")),
            updated_at=row.updated_at,
        )
        for row in rows
    ]
    return AutomationTreeOut(
        project_id=project.id,
        repo=project.repo or "",
        slug=project.slug or "",
        base_version=project.base_version or "",
        head_commit=aps.head_commit(project) or "",
        file_count=len(files),
        updated_at=max((f.updated_at for f in files), default=project.updated_at),
        files=files,
    )


@router.get("/repos/{project_id}/file", response_model=AutomationFileOut)
def get_project_automation_file(
    project_guid: str,
    project_id: int,
    path: str = Query(..., description="Project-relative POSIX path of the file to read"),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> AutomationFileOut:
    """One file's content, served from the mirror.

    ``path`` is a **query param, not a path segment**. A FastAPI ``{path:path}``
    catch-all would collide with the sibling ``files``/``export`` routes and
    mangle percent-encoded slashes — and automation paths always contain ``/``.

    The bytes come from ``AutomationFile.code``, so the tree and the viewer can
    never disagree about what exists and no filesystem is touched on a read.
    Malformed paths are refused by :func:`_validated_path` before the query runs;
    a well-formed path with no mirror row is a plain 404.

    ``provenance`` is populated by :func:`_spec_provenance` (#769) and **only for
    ``kind == "spec"``**. The join is skipped entirely for a shared asset, which
    is both the cheap and the honest thing to do: a page or fixture is edited
    across many runs, so there is no single run that "made it".
    """
    project = _project_or_404(db, project_guid, project_id, user)
    relative = _validated_path(path)
    row = db.scalar(
        select(AutomationFile).where(
            AutomationFile.project_id == project.id, AutomationFile.path == relative
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="File not found in this automation project.")
    code = row.code or ""
    return AutomationFileOut(
        path=row.path,
        kind=row.kind,
        code=code,
        size=len(code.encode("utf-8")),
        updated_at=row.updated_at,
        sha256=row.sha256 or "",
        provenance=(_spec_provenance(db, project, relative, user) if row.kind == "spec" else None),
    )


@router.get("/repos/{project_id}/export/zip")
def export_project_automation_zip(
    project_guid: str,
    project_id: int,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> Response:
    """Download the automation project as a ZIP — the project-keyed twin of #686.

    Byte-for-byte the same archive as ``GET /runs/{id}/automation/export/zip``;
    the only difference is how the project is reached, so the run-keyed route is
    left exactly as it is (the overlap is five lines of header construction, and
    a shared helper would couple two routers to save nothing).

    No repository connection, no PAT, no branch policy, **no network** — the
    remote push stays run-keyed and out of scope here (ADR 0014 slices 2-5).
    The archive is returned in one body rather than streamed so ``Content-Length``
    — and with it the browser's progress bar and the client's error handling —
    stays honest.
    """
    project = _project_or_404(db, project_guid, project_id, user)
    try:
        payload = automation_export_service.export_to_zip(project)
    except automation_export_service.ExportError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    filename = automation_export_service.zip_filename(project)
    audit_service.record(
        category="automation",
        action="Exported the automation project as a ZIP",
        target=project.slug,
        detail={"filename": filename, "bytes": len(payload), "projectGuid": project_guid},
    )
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # The SPA reads the filename off this header, and it is not exposed to
            # cross-origin JS by default — the download would otherwise be saved
            # under a generated name that says nothing about which project it is.
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )


#: Upper bound on one project execution's selection. Not a performance limit —
#: Playwright's own `--workers` handles volume — but a guard against a client
#: posting an unbounded list, which would create that many rows and stage that
#: many files before anything could go wrong more cheaply.
_MAX_SELECTED_SPECS = 200


def _selected_specs(db: Session, project: AutomationProject, raw: list[str] | None) -> list[str]:
    """The requested spec paths, validated against the mirror, order preserved.

    **There is no "run everything" default here, by construction.** An automation
    repo is keyed on ``(owner, provider project key, repo)`` and is legitimately
    written to by runs from *several* q-agent projects, so "every spec in the
    repo" is not the same set as "this project's specs" — which is exactly why
    the Automation tab's tree is unfiltered and the selection is explicit (#795).
    An empty selection is therefore a 400, never an implicit everything.

    Every path must resolve to a mirror row of ``kind == "spec"`` for **this**
    repo. That single check covers three distinct refusals at once: a path from
    another repo, a shared asset (a page object or fixture is not runnable on its
    own), and a path that no longer exists. The mirror is also what the tab
    listed, so what runs is what the user saw.

    Raises:
        HTTPException: 400, naming the offending paths rather than the count.
    """
    if not raw:
        raise HTTPException(
            status_code=400, detail="Select at least one spec to run."
        )
    seen: set[str] = set()
    paths: list[str] = []
    for entry in raw:
        value = _validated_path(entry)
        if value in seen:
            continue
        seen.add(value)
        paths.append(value)
    if len(paths) > _MAX_SELECTED_SPECS:
        raise HTTPException(
            status_code=400,
            detail=f"Select at most {_MAX_SELECTED_SPECS} specs in one run.",
        )
    runnable = {
        row.path
        for row in db.execute(
            select(AutomationFile.path).where(
                AutomationFile.project_id == project.id,
                AutomationFile.path.in_(paths),
                AutomationFile.kind == "spec",
            )
        ).all()
    }
    unknown = [p for p in paths if p not in runnable]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                "Not runnable specs in this automation repo: " + ", ".join(unknown[:10])
            ),
        )
    return paths


@router.post("/repos/{project_id}/executions")
def start_project_automation_execution(
    project_guid: str,
    project_id: int,
    payload: ProjectExecutionStart,
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> dict:
    """Run the selected specs out of this automation repo (#797).

    The project-scoped counterpart of ``POST /runs/{id}/execution``, and
    deliberately **not** a branch inside it: there is no run to put into the
    ``executing`` stage, no approved-case filter to apply, and no ticket or case
    to attribute a result to. The specs come from the request, their code from
    the mirror, and the whole lifecycle is the Execution row's own ``status``.

    Both targets are supported since #798. ``target`` comes from the request when
    it names a valid target, else from the workspace-wide ``executionTarget``
    setting — the same resolution ``POST /runs/{id}/execution`` does, so the
    shipped ``local-agent`` default (#161) applies here too. A ``local-agent``
    execution is created ``queued`` with no worker thread; a paired device claims
    it via ``POST /agent/jobs/next``, which is why the paired-device check runs
    first: queueing a row no device will ever claim would spin the tab forever.
    """
    project = _project_or_404(db, project_guid, project_id, user)
    target = _resolve_target({"target": payload.target})
    if target == "local-agent":
        _require_paired_device(db, project.owner_id)
    spec_paths = _selected_specs(db, project, payload.spec_paths)
    workers = max(1, min(int(payload.workers or 2), 16))
    execution = project_execution.create(
        db,
        project,
        spec_paths,
        workers=workers,
        env=(payload.env or "").strip(),
        target=target,
        owner_id=project.owner_id,
    )
    audit_service.record(
        category="execution",
        action="Started a project automation run",
        target=f"{project.slug} · {len(spec_paths)} specs",
        detail={"projectGuid": project_guid, "executionId": execution.id},
    )
    # A local-agent execution stays queued for a device to claim — the same
    # "no in-process thread" contract the run path has.
    if target == "server":
        project_execution.start_in_thread(execution.id)
    return _project_execution_out(db, execution, with_results=True)


@router.get("/repos/{project_id}/executions")
def list_project_automation_executions(
    project_guid: str,
    project_id: int,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User | None = Depends(current_user),
) -> list[dict]:
    """This repo's execution history for this project, newest first.

    Summaries only — no per-spec results, which is what makes the list cheap
    enough to load with the tab. The detail comes from
    ``GET /executions/{id}``, which serves a run-less execution since #797.
    """
    project = _project_or_404(db, project_guid, project_id, user)
    executions = (
        db.query(Execution)
        .filter(Execution.automation_project_id == project.id, Execution.run_id.is_(None))
        .order_by(Execution.id.desc())
        .limit(limit)
        .all()
    )
    return [_project_execution_out(db, e, with_results=False) for e in executions]


def _project_execution_out(db: Session, execution: Execution, *, with_results: bool) -> dict:
    """Wire shape for a project-scoped execution.

    Matches the field names ``routers/execution.py`` already uses for a run-scoped
    execution (``total``/``passed``/``failed``/``progress``/``startedAt``), so the
    Automation tab's progress rendering is the same code as the run screen's.
    """
    out = {
        "id": execution.id,
        "runId": None,
        "automationProjectId": execution.automation_project_id,
        "status": execution.status,
        "target": execution.target,
        "env": execution.env,
        "workers": execution.workers,
        "total": execution.total,
        "passed": execution.passed,
        "failed": execution.failed,
        "progress": execution.progress,
        "startedAt": execution.started_at,
        "finishedAt": execution.finished_at,
    }
    if with_results:
        results = (
            db.query(ExecutionResult)
            .filter(ExecutionResult.execution_id == execution.id)
            .order_by(ExecutionResult.id)
            .all()
        )
        out["log"] = execution.log
        out["results"] = [
            {
                "id": r.id,
                "specPath": r.spec_path,
                "title": r.title,
                "status": r.status,
                "durationMs": r.duration_ms,
                "errorMessage": r.error_message,
            }
            for r in results
        ]
    return out
