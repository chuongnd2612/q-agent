"""Retire the executions a re-run supersedes (#706).

A re-run creates a new :class:`Execution`; the previous one is never deleted, and its
screenshots, videos, traces and DOM captures stay on disk forever. RUN-207 had five
executions of a single test case, which is how its ticket comment ended up listing that
one case six times — including a FAIL from an attempt superseded hours earlier — and
attaching every artifact of all five to the work item.

**Nothing in the product could see those executions anyway.** The Evidence screen, the
Execution screen and the report all read the run's *latest* execution
(``_latest_execution`` in three separate modules). So a superseded execution is already
invisible: it contributes disk, and — until #706 — one wrong comment.

Two properties are deliberate:

* **It runs when a re-run STARTS, not when one finishes.** The new execution is the
  current one from the moment it exists, so pruning then is what stops a comment
  prepared mid-run from mixing the two. Waiting for the end would leave the overlap
  open for exactly as long as the run takes.
* **The database row is deleted, and the files with it.** Deleting files while keeping
  rows would leave the Evidence screen offering artifacts that 404 — which reads as
  data loss rather than as retention.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from sqlalchemy.orm import Session

from app.logging import logger
from app.models.execution import Evidence, Execution, ExecutionResult
from app.services.workspace_scope import scoped_evidence_dir

__all__ = ["prune_superseded"]


def prune_superseded(db: Session, run_id: int, keep_execution_id: int, owner_id: int | None) -> int:
    """Delete every execution of ``run_id`` except ``keep_execution_id``. Returns the count.

    Best-effort on the filesystem and never fatal: a file that cannot be removed leaves
    an orphan on disk, which costs space. Failing the re-run over it would cost the run.
    """
    superseded = (
        db.query(Execution)
        .filter(Execution.run_id == run_id, Execution.id != keep_execution_id)
        .all()
    )
    if not superseded:
        return 0

    root = scoped_evidence_dir(owner_id)
    ids = [execution.id for execution in superseded]
    paths = [
        path
        for (path,) in db.query(Evidence.path)
        .join(ExecutionResult, Evidence.result_id == ExecutionResult.id)
        .filter(ExecutionResult.execution_id.in_(ids))
        .all()
        if path
    ]

    for execution in superseded:
        db.delete(execution)  # cascades to results, and results to evidence rows
    db.commit()

    for relative in paths:
        _remove(root / relative)
        # The annotated copy sits beside the original under a `-annotated` suffix and
        # has no row of its own, so it would otherwise outlive everything that
        # referenced it.
        _remove(Path(str(root / relative)).with_name(Path(relative).stem + "-annotated.png"))

    logger.info(
        "run {} pruned {} superseded execution(s) and {} artifact(s)",
        run_id,
        len(superseded),
        len(paths),
    )
    return len(superseded)


def _remove(path: Path) -> None:
    """Delete a file or directory if it is there. Never raises."""
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover - permissions/locking; not fatal
        logger.warning("could not remove superseded artifact {}: {}", path, exc)
