"""The human overlay on Business Knowledge facts (#827, ADR 0016 §5).

Ingested content is **immutable**. This module is the only place a human changes
what the prompts see, and it does so by writing rows *around* the ingested ones
rather than over them. Three affordances, deliberately, instead of a free-text
blob editor:

* **Correct a fact** (:func:`correct_fact`) — a *new* row, ``origin="manual"``,
  ``pinned=True``, with the ingested row's ``superseded_by`` pointed at it. The
  original stays and is rendered struck through, so the disagreement with the
  source document is **visible** rather than hidden. A blob editor would have
  destroyed exactly that.
* **Add a fact** (:func:`add_fact`) the documents never stated. Not pinned:
  pinning marks a row as *overriding a source*, and an addition overrides
  nothing. It still outranks ingested content — see the ladder below.
* **Exclude** a fact (:func:`update_fact` with ``excluded=True``) — out of
  context, still on the row, one click to restore. The "that wiki page is wrong
  but I am not deleting it" case.

The precedence ladder (ADR 0016 §5), which :func:`facts_in_context` materializes
as an ordering:

===  ==========================  ==========================================
  1  human pinned corrections    ``origin="manual"``, ``pinned=True``
  2  human additions             ``origin="manual"``
  3  ingested business facts     ``origin="ingested"``
===  ==========================  ==========================================

Layers 4 and 5 of that ladder (runtime-verified and source-inferred **code** KB
entries) are not rows — they live in ``ProjectKnowledge.knowledge`` and their
half of the same rule is enforced in :mod:`app.services.knowledge_service`.

**Versioning is cheap on purpose.** :attr:`BusinessFact.revision` counts human
edits and :attr:`BusinessFact.updated_by` records who made the last one; the
previous normalized markdown of a source is kept on disk as
``normalized.<hash>.md`` (:mod:`app.services.business_ingest.storage`) so a diff
is inspectable. There is no history table: a diff/restore UI is a real feature
with no stated demand, and ``revision`` is the hook if it is ever wanted.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.business import BUSINESS_FACT_CATEGORIES, BusinessFact
from app.models.user import User
from app.services.ownership import check_owned_or_404, stamp_owner

__all__ = [
    "MAX_TERM_CHARS",
    "precedence_rank",
    "visible_facts",
    "facts_in_context",
    "fact_or_404",
    "add_fact",
    "correct_fact",
    "update_fact",
    "validate_category",
]

#: ``BusinessFact.term`` is ``String(300)``.
MAX_TERM_CHARS = 300


def validate_category(category: str) -> str:
    """Return ``category`` if it is a known one, else raise a 400.

    Args:
        category: The caller-supplied category.

    Returns:
        The trimmed category.

    Raises:
        HTTPException: 400 naming every accepted category, because the list is
            short enough to be the whole answer.
    """
    value = (category or "").strip()
    if value not in BUSINESS_FACT_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown fact category '{value}'. Expected one of: "
            + ", ".join(BUSINESS_FACT_CATEGORIES),
        )
    return value


def precedence_rank(fact: BusinessFact) -> int:
    """Where ``fact`` sits on the ADR 0016 §5 ladder — lower wins.

    Args:
        fact: The row to rank.

    Returns:
        ``0`` for a pinned human correction, ``1`` for a human addition, ``2``
        for an ingested fact.
    """
    if fact.origin == "manual":
        return 0 if fact.pinned else 1
    return 2


def _rank_text(term: str, statement: str, detail: str) -> str:
    """The denormalized text ``BusinessFact.rank_text`` is scored on.

    Kept identical to ``business_ingest.distil._rank_text`` on purpose: a
    corrected fact must be retrievable by exactly the keywords the ingested one
    was, or correcting a fact would quietly drop it out of every ranked prompt
    block. Duplicated as two lines rather than imported because importing the
    ingestion pipeline into the CRUD path would make the overlay depend on the
    distiller it is meant to outrank.
    """
    return " ".join(part for part in (term, statement, detail) if part)


def visible_facts(
    db: Session,
    project_guid: str,
    user: User | None,
    *,
    include_excluded: bool = True,
) -> list[BusinessFact]:
    """Every fact of ``project_guid`` that ``user`` may see, in precedence order.

    Scoped the way :func:`business_source_service.visible_sources` is: the
    caller's own rows plus the unowned/shared ones (ADR 0009 §3), never another
    user's.

    This is the **management** view — superseded rows are included, because the
    Business tab has to render the struck-through original next to the
    correction that beat it. :func:`facts_in_context` is the prompt view.

    Args:
        db: Active session.
        project_guid: The owning project's GUID (ADR 0013 / #585).
        user: The caller, or ``None`` under the #91 ownership bridge.
        include_excluded: Keep rows the user took out of context.

    Returns:
        The matching rows, ordered by :func:`precedence_rank`, then category and
        term, so the list reads the same way twice.
    """
    query = db.query(BusinessFact).filter(BusinessFact.project_guid == project_guid)
    if user is not None:
        query = query.filter(
            (BusinessFact.owner_id == user.id) | (BusinessFact.owner_id.is_(None))
        )
    if not include_excluded:
        query = query.filter(BusinessFact.excluded.is_(False))
    rows = query.all()
    return sorted(rows, key=lambda row: (precedence_rank(row), row.category, row.term.lower()))


def facts_in_context(
    db: Session, project_guid: str, owner_id: int | None
) -> list[BusinessFact]:
    """The facts that actually ground a prompt, highest precedence first.

    Two rows are dropped relative to :func:`visible_facts`, and each for its own
    reason:

    * **excluded** — the user said this must not ground anything.
    * **superseded** — a correction beat it. Both rows are kept on disk so the
      disagreement stays visible in the UI, but shipping both to a prompt would
      hand the model the wrong fact and the right one side by side.

    Args:
        db: Active session.
        project_guid: The owning project's GUID.
        owner_id: Whose facts — an exact match, not the "own or shared" widening
            :func:`visible_facts` does, because a prompt is assembled for one
            owner's corpus.

    Returns:
        The in-context rows, ordered by :func:`precedence_rank`.
    """
    rows = (
        db.query(BusinessFact)
        .filter(
            BusinessFact.project_guid == project_guid,
            BusinessFact.owner_id == owner_id,
            BusinessFact.excluded.is_(False),
            BusinessFact.superseded_by.is_(None),
        )
        .all()
    )
    return sorted(rows, key=lambda row: (precedence_rank(row), row.category, row.term.lower()))


def fact_or_404(
    db: Session, project_guid: str, fact_id: int, user: User | None
) -> BusinessFact:
    """The fact ``fact_id``, proven to belong to ``project_guid`` and to ``user``.

    The same three checks, in the same order and with the same 404-for-
    everything answer, as
    :func:`business_source_service.source_or_404` — a 403 would confirm the row
    exists to somebody not allowed to know that (ADR 0008/0009).

    Args:
        db: Active session.
        project_guid: The resolved project GUID.
        fact_id: The ``BusinessFact`` id, from the path.
        user: The caller, or ``None`` under the #91 ownership bridge.

    Returns:
        The resolved :class:`BusinessFact`.

    Raises:
        HTTPException: 404 in every failing case.
    """
    row = db.get(BusinessFact, fact_id)
    if row is None or row.project_guid != project_guid:
        raise HTTPException(status_code=404, detail="Business fact not found")
    check_owned_or_404(row, user, not_found="Business fact not found")
    return row


def add_fact(
    db: Session,
    *,
    project_guid: str,
    owner_id: int | None,
    category: str,
    term: str,
    statement: str,
    detail: str = "",
    updated_by: int | None = None,
    user: User | None = None,
) -> BusinessFact:
    """Record a fact the documents never stated (ladder layer 2).

    **Not pinned.** ``pinned`` means "this overrides what a source says", and an
    addition overrides nothing — it simply exists. It still survives a re-sync,
    because :func:`business_ingest.distil.merge_facts` refuses to write over any
    ``origin="manual"`` row, not merely a pinned one.

    Args:
        db: Active session; committed by this function.
        project_guid: The owning project's GUID.
        owner_id: The row's owner, when no ``user`` is given.
        category: One of ``BUSINESS_FACT_CATEGORIES``; validated by the caller.
        term: What the fact is about.
        statement: The fact, in one sentence.
        detail: Supporting detail.
        updated_by: The authoring user's id, for attribution.
        user: The caller, when there is one — used to stamp ownership through
            the shared :func:`~app.services.ownership.stamp_owner` helper.

    Returns:
        The new, committed row.
    """
    row = BusinessFact(
        project_guid=project_guid,
        category=category,
        term=term[:MAX_TERM_CHARS],
        statement=statement,
        detail=detail,
        origin="manual",
        pinned=False,
        rank_text=_rank_text(term, statement, detail),
        revision=1,
        updated_by=updated_by,
    )
    if user is not None:
        stamp_owner(row, user)
    else:
        row.owner_id = owner_id
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def correct_fact(
    db: Session,
    original: BusinessFact,
    *,
    statement: str,
    detail: str = "",
    updated_by: int | None = None,
) -> BusinessFact:
    """Override ``original`` with a pinned correction (ladder layer 1).

    The correction is a **new row** carrying the same ``category``/``term`` (so
    it collides with the same distilled fact on the next re-sync and is skipped
    there), and ``original.superseded_by`` is pointed at it. Nothing about
    ``original``'s content changes: it is still on the row, still attributable,
    and rendered struck through as *"superseded by your correction"*.

    Correcting an already-corrected fact supersedes **the correction**, not the
    ingested original — otherwise two pinned rows would claim the same term and
    both would reach the prompt.

    Args:
        db: Active session; committed by this function.
        original: The row being overridden — ingested or a previous correction.
        statement: The corrected sentence.
        detail: Supporting detail for the correction (why the source is wrong).
        updated_by: The correcting user's id.

    Returns:
        The new, committed correction row.
    """
    correction = BusinessFact(
        project_guid=original.project_guid,
        owner_id=original.owner_id,
        source_id=original.source_id,
        category=original.category,
        term=original.term,
        statement=statement,
        detail=detail,
        origin="manual",
        pinned=True,
        rank_text=_rank_text(original.term, statement, detail),
        revision=1,
        updated_by=updated_by,
    )
    db.add(correction)
    db.flush()  # the id the pointer needs
    original.superseded_by = correction.id
    db.commit()
    db.refresh(correction)
    return correction


def update_fact(
    db: Session,
    row: BusinessFact,
    *,
    statement: str | None = None,
    detail: str | None = None,
    excluded: bool | None = None,
    updated_by: int | None = None,
) -> BusinessFact:
    """Edit a manual fact's content, and/or take any fact out of context.

    **Only content edits bump ``revision``.** Excluding a fact is a decision
    about where it applies, not a new version of what it says, and a counter
    that moved every time somebody toggled visibility would tell nobody anything.

    Editing the *content* of an ``origin="ingested"`` row is refused: ingested
    content is immutable, and the way to disagree with it is
    :func:`correct_fact`, which keeps the disagreement visible.

    Args:
        db: Active session; committed by this function.
        row: The fact to update.
        statement: New statement, or ``None`` to leave alone.
        detail: New detail, or ``None`` to leave alone.
        excluded: New exclusion state, or ``None`` to leave alone.
        updated_by: The editing user's id; recorded only on a content edit.

    Returns:
        ``row``, updated and committed.

    Raises:
        HTTPException: 400 when the caller tries to edit ingested content.
    """
    edited = statement is not None or detail is not None
    if edited and row.origin != "manual":
        raise HTTPException(
            status_code=400,
            detail="An ingested fact cannot be edited — correct it instead, so the "
            "original stays visible next to your correction.",
        )
    if statement is not None:
        row.statement = statement
    if detail is not None:
        row.detail = detail
    if edited:
        row.rank_text = _rank_text(row.term, row.statement, row.detail)
        row.revision = (row.revision or 1) + 1
        row.updated_by = updated_by
    if excluded is not None:
        row.excluded = excluded
    db.commit()
    db.refresh(row)
    return row
