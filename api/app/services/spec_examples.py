"""Few-shot example selection for grounded spec generation.

Picks a small number of *proven* Playwright specs from the SAME project + repo to
show the generator as worked examples. Only specs that actually PASSED at runtime
qualify, so the model learns from code that ran green against the real app — its
conventions, imports, locator strategy and assertion style.

Selection rules (all enforced):

- **Passed only** — the example's latest execution result was ``pass``.
- **Same project + repo** — never cross-project; the example's work item must
  resolve to the given ``project_key`` and its run-ticket repo must equal ``repo``.
- **Never the target itself** — the case we are generating for is excluded, and so
  are drafts / currently-failing specs (they are not in the passed set anyway).
- **Relevance-ranked** — examples are ordered by keyword overlap between the target
  ``case`` (title + steps) and each example's filename + code, so a relevant proven
  spec is preferred over an arbitrary one.

When fewer than ``limit`` proven specs exist — most sharply on a brand-new
project, where there are none at all until the first spec runs green — the
remainder is topped up from the target repo's OWN pre-existing e2e specs (#868),
read straight off the configured local checkout. Those files never ran through
Q-Agent, so they are strictly the weaker source and always rank last; what they
are good for is the house style a team already writes in (locator strategy,
assertion style, naming), which nothing else in the pipeline carries: the Project
Knowledge Base keeps only asset *names* and counts, and the automation project's
inventory is deliberately Q-Agent's own tree, never a scrape of the app repo.

Every returned example is tagged with a ``source`` (``"proven"`` / ``"repo"``) so
``spec_service._render_examples`` can caption the two differently — a repo spec
lives in another tree with its own imports, config and login, none of which a
generated spec may copy.

Best-effort and defensive: any resolution error yields ``[]`` rather than raising,
since example selection is an optimization, not a correctness requirement.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.logging import logger
from app.models.execution import ExecutionResult
from app.models.run import RunTicket
from app.models.testcase import AutomationSpec, TestCase
from app.models.ticket import Ticket
from app.services import project_config_service, repo_assets

#: The shared scoring vocabulary (``prompts`` imports this name from here).
_keywords = repo_assets.keywords


def _case_query(case: Any) -> str:
    """The relevance-ranking text describing the target case: title + steps."""
    parts: list[str] = [getattr(case, "title", "") or ""]
    for step in getattr(case, "steps", None) or []:
        if isinstance(step, dict):
            parts.append(step.get("a", ""))
            parts.append(step.get("e", ""))
    return " ".join(part for part in parts if part)


def _case_keywords(case: Any) -> set[str]:
    """Build the keyword set describing the target case (title + steps)."""
    return _keywords(_case_query(case))


def _repo_for_case(db: Session, test_case: TestCase) -> str:
    """Resolve the target repo of a test case via its RunTicket ("" if none)."""
    run_ticket = (
        db.query(RunTicket)
        .filter(
            RunTicket.run_id == test_case.run_id,
            RunTicket.ticket_external_id == test_case.ticket_external_id,
        )
        .first()
    )
    return run_ticket.repo if run_ticket else ""


def _project_key_for_case(db: Session, test_case: TestCase) -> str | None:
    """Resolve the project key a test case belongs to via its ticket's provider."""
    ticket = (
        db.query(Ticket)
        .filter(Ticket.external_id == test_case.ticket_external_id)
        .first()
    )
    if ticket is None:
        return None
    return project_config_service.project_key_for_ticket(db, ticket)


def _proven_examples(
    db: Session, project_key: str, repo: str, case: Any, limit: int
) -> list[dict]:
    """Proven, already-passing Q-Agent specs for this project + repo, best first.

    The original (and strongest) example source: specs this pipeline generated and
    then watched pass against the real app, so their imports, fixtures and locator
    choices are known to work rather than merely plausible.

    :param db: Active session.
    :param project_key: Project scope; examples never cross projects.
    :param repo: Target repository NAME ("" matches specs whose run-ticket repo is "").
    :param case: The target :class:`TestCase`; excluded from its own examples and
        used as the relevance query.
    :param limit: Max examples to return.
    :returns: ``[{"filename", "code", "source": "proven"}]``, most relevant first.
    """
    target_case_id = getattr(case, "id", None)
    # Passed execution results joined to their (populated) spec, same-project scope.
    rows = (
        db.query(ExecutionResult.test_case_id, AutomationSpec)
        .join(AutomationSpec, AutomationSpec.test_case_id == ExecutionResult.test_case_id)
        .filter(ExecutionResult.status == "pass")
        .filter(AutomationSpec.code != "")
        .all()
    )

    target_keywords = _case_keywords(case)
    seen_case_ids: set[int] = set()
    candidates: list[tuple[int, dict]] = []  # (score, {filename, code, source})

    for test_case_id, spec in rows:
        if test_case_id == target_case_id or test_case_id in seen_case_ids:
            continue
        seen_case_ids.add(test_case_id)

        test_case = db.get(TestCase, test_case_id)
        if test_case is None:
            continue
        # Same project + repo only.
        if _repo_for_case(db, test_case) != (repo or ""):
            continue
        if _project_key_for_case(db, test_case) != project_key:
            continue

        example_keywords = _keywords(f"{spec.filename} {test_case.title}")
        score = len(target_keywords & example_keywords)
        candidates.append(
            (score, {"filename": spec.filename, "code": spec.code, "source": "proven"})
        )

    # Prefer higher relevance; stable order keeps arbitrary ties deterministic.
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [payload for _, payload in candidates[:limit]]


def _repo_examples(db: Session, project_key: str, repo: str, case: Any, limit: int) -> list[dict]:
    """Pre-existing e2e specs from the app repo itself, relevance-ranked (#868).

    The weaker of the two sources — nothing here has run under Q-Agent — so it only
    ever fills the slots :func:`_proven_examples` left empty. Its value is the house
    style a team already writes in, which no other grounding source carries: the
    Project Knowledge Base keeps only asset names and counts, and the automation
    project's inventory is deliberately Q-Agent's own tree.

    :param db: Active session.
    :param project_key: Project scope, used to resolve the checkout.
    :param repo: Target repository NAME ("" means "the default repo").
    :param case: The target :class:`TestCase`; its title + steps are the query.
    :param limit: Max examples to return.
    :returns: ``[{"filename", "code", "source": "repo"}]``, most relevant first.
    """
    return repo_assets.collect(
        db, project_key, repo, repo_assets.is_spec_file, _case_query(case), limit
    )


def select_examples(
    db: Session, project_key: str, repo: str, case: Any, limit: int = 2
) -> list[dict]:
    """Return up to ``limit`` spec examples for grounded generation.

    Proven specs (passed under Q-Agent) fill the slots first; whatever is left over
    is topped up from the repo's own pre-existing e2e specs (#868). The ordering is
    the point, not merely the count — the prompt reads the list in order, and a spec
    that actually ran green against this app outranks one that merely exists.

    Args:
        db: Active session.
        project_key: The resolved project key to scope examples to (same-project only).
        repo: The target repository NAME to scope examples to ("" matches specs whose
            run-ticket repo is also "", and selects the project's default repo when
            reading pre-existing specs off disk).
        case: The target :class:`TestCase` (ORM object) we are generating for; its
            own spec is always excluded, and its title/steps drive relevance ranking.
        limit: Max examples to return, across both sources.

    Returns:
        A list of ``{"filename": str, "code": str, "source": "proven" | "repo"}``
        dicts, proven first and relevance-ranked within each source. Empty when
        nothing qualifies. Never raises.
    """
    if limit <= 0 or not project_key:
        return []
    examples: list[dict] = []
    try:
        examples = _proven_examples(db, project_key, repo, case, limit)
    except Exception as exc:  # noqa: BLE001 - selection is best-effort
        logger.warning("spec_examples.select_examples failed: {}", exc)
    if len(examples) >= limit:
        return examples
    try:
        examples.extend(_repo_examples(db, project_key, repo, case, limit - len(examples)))
    except Exception as exc:  # noqa: BLE001 - reading the checkout is best-effort
        logger.warning("spec_examples: repo spec scan failed: {}", exc)
    return examples
