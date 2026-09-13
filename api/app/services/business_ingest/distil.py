"""Distil a project's ingested corpus into a brief and structured facts (#824).

Ingestion (:mod:`app.services.business_ingest.pipeline`) ends with normalized
markdown on disk. This module is the step after it: the corpus is read **once**,
here, and turned into the only two things retrieval ever uses.

- **The brief** — ``ProjectConfig.business_brief`` — a short prose document
  (:data:`BRIEF_TOKEN_BUDGET` tokens) saying what the product is, who uses it,
  how the core workflows run, what the vocabulary means and which rules a tester
  must know.
- **The facts** — :class:`~app.models.business.BusinessFact` rows —
  ``{category, term, statement, detail}``, atomic and independently retrievable.

**The brief is the answer to "what if there is a lot of it".** Raw documents are
never inlined into any prompt: this module is the only place they are read, and
everything downstream (#825) reads the brief and the facts instead. That is what
makes the design scale — the brief **does not grow with the corpus**. Twenty
documents and two hundred produce a brief of the same length; more documents make
it denser and the fact set larger, never the prompt longer. It is also why there
is no vector store here: at this corpus size a summarization layer is the honest
answer and an index would be ceremony (ADR 0016).

**Mechanism is borrowed wholesale, not invented.** ``claude_cli.run_json`` with a
dedicated skill, a daemon :class:`threading.Thread` and an in-process guard set —
the same three pieces as ``knowledge_service.build_knowledge_payload`` /
``start_build`` and as ``pipeline.start_sync``. There is no job scheduler in this
codebase (ADR 0016 records that as a constraint, not an oversight) and this slice
does not add one.

**Re-sync never destroys a human correction.** :func:`merge_facts` applies the
no-clobber rule that already exists in this codebase —
``knowledge_service.merge_verified_discovery``: *an existing entry that already
has a truthy ``verified_at_runtime`` is never overwritten; a discovery colliding
with an UN-verified entry upgrades it in place* — with ``pinned`` in place of
``verified_at_runtime``. The rule is the same rule; only the flag differs.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from app import db as db_module
from app.db import utcnow
from app.logging import logger
from app.models.business import BUSINESS_FACT_CATEGORIES, BusinessFact, BusinessSource
from app.models.project_config import ProjectConfig
from app.services import run_context
from app.services.business_ingest import storage
from app.services.claude_cli import run_json
from app.services.skills import BUSINESS_ANALYST
from app.services.workspace_scope import scoped_business_dir

__all__ = [
    "BRIEF_STATUSES",
    "BRIEF_TOKEN_BUDGET",
    "BRIEF_CHAR_BUDGET",
    "CorpusDocument",
    "collect_corpus",
    "corpus_hash",
    "build_distillation",
    "merge_facts",
    "distil_project",
    "start_distil",
    "is_distilling",
]

#: The states ``ProjectConfig.business_brief["status"]`` can hold. ``"pending"``
#: is the state of a project whose corpus has never been distilled; the other
#: three mirror ``BusinessSource.status`` so the SPA reads one vocabulary.
BRIEF_STATUSES = ("pending", "building", "ready", "error")

#: The brief's hard ceiling, stated in the unit the constraint is really in.
BRIEF_TOKEN_BUDGET = 1500

#: The ceiling enforced deterministically, at ~4 characters per token. The skill
#: asks for the limit; this *applies* it, because a prompt-only budget drifts.
BRIEF_CHAR_BUDGET = BRIEF_TOKEN_BUDGET * 4

#: How much corpus text is handed to one distillation call. Bounded because the
#: corpus is unbounded and the CLI's prompt is not; the overflow is dropped with
#: a marker rather than silently, so a truncated distillation is visible.
MAX_CORPUS_CHARS = 300_000

#: ``ProjectConfig.business_brief["last_error"]`` is JSON, but it is rendered in
#: a toast — a stack-trace-length message helps nobody.
_MAX_ERROR_CHARS = 1000

#: Projects with a distillation in flight in this process, keyed by
#: :func:`_guard_key`. Same shape and same guarantees as
#: ``knowledge_service._building`` and ``pipeline._syncing``: it de-duplicates
#: concurrent runs within a process, and it is what a test waits on instead of
#: polling an endpoint.
_distilling: set[str] = set()


def _guard_key(project_guid: str, owner_id: int | None) -> str:
    """The :data:`_distilling` key for one project *as seen by one owner*.

    The owner is part of the key because two users of the same project hold two
    independent corpora and two independent briefs (ADR 0009 §3): one user's
    distillation must not lock the other out of theirs.
    """
    return f"{project_guid}::{'' if owner_id is None else owner_id}"


def is_distilling(project_guid: str, owner_id: int | None = None) -> bool:
    """Whether a distillation for this project/owner is running in this process."""
    return _guard_key(project_guid, owner_id) in _distilling


@dataclass(frozen=True)
class CorpusDocument:
    """One normalized document of the corpus, with the provenance to cite it.

    :param source_id: The :class:`~app.models.business.BusinessSource` it came
        from — carried onto every fact distilled from this document.
    :param source_title: The source's human label (the wiki, the uploaded file).
    :param path: The document's path within that source's snapshot.
    :param markdown: The normalized text.
    """

    source_id: int
    source_title: str
    path: str
    markdown: str


def collect_corpus(db, project_guid: str, owner_id: int | None) -> list[CorpusDocument]:
    """Every in-context normalized document of one project's Business Knowledge.

    Only ``synced`` and non-``excluded`` sources contribute: an ``excluded``
    source keeps its snapshot (it stays attributable) but stops feeding new
    artifacts, which is precisely what excluding one means (epic #813). A source
    whose snapshot directory has gone missing is skipped with a warning rather
    than failing the whole distillation — one unreadable source must not cost the
    project its brief.

    :param db: Active session.
    :param project_guid: The owning project's GUID (ADR 0013 / #585).
    :param owner_id: The corpus owner; ``None`` reads the shared namespace.
    :returns: Documents ordered by source id then path, so the corpus — and
        therefore :func:`corpus_hash` — is stable across two identical reads.
    """
    sources = (
        db.query(BusinessSource)
        .filter(
            BusinessSource.project_guid == project_guid,
            BusinessSource.owner_id == owner_id,
            BusinessSource.status == "synced",
            BusinessSource.excluded.is_(False),
        )
        .order_by(BusinessSource.id)
        .all()
    )
    scope_root = scoped_business_dir(owner_id)
    documents: list[CorpusDocument] = []
    for source in sources:
        if not source.normalized_path:
            continue
        root = scope_root / source.normalized_path
        if not root.is_dir():
            logger.warning(
                "business distil: source {} has no snapshot at {}", source.id, root
            )
            continue
        for markdown_file in sorted(root.rglob("*.md")):
            try:
                text = markdown_file.read_text(encoding="utf-8")
            except OSError as exc:  # pragma: no cover - filesystem-dependent
                logger.warning("business distil: unreadable {} ({})", markdown_file, exc)
                continue
            if not text.strip():
                continue
            documents.append(
                CorpusDocument(
                    source_id=source.id,
                    source_title=source.title or "",
                    path=_relative_path(markdown_file, root),
                    markdown=text,
                )
            )
    return documents


def _relative_path(markdown_file: Path, root: Path) -> str:
    """``markdown_file`` as a POSIX path relative to its snapshot ``root``."""
    try:
        return markdown_file.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover - rglob results are always under root
        return markdown_file.name


def corpus_hash(documents: list[CorpusDocument]) -> str:
    """The digest of a corpus, for ``business_brief["hash"]``.

    Delegates to :func:`storage.content_hash_for` rather than hashing here — one
    hashing rule in this package, not two — keyed by ``<source id>/<path>`` so a
    document moving between sources changes the digest. Comparing it to the
    stored one is how a caller knows the brief is stale (#830) without re-running
    a distillation to find out.

    :param documents: The corpus, as returned by :func:`collect_corpus`.
    :returns: Lower-case SHA-256 hex digest; the digest of ``{}`` for an empty
        corpus.
    """
    return storage.content_hash_for(
        {f"{doc.source_id}/{doc.path}": doc.markdown for doc in documents}
    )


def _render_corpus(documents: list[CorpusDocument]) -> str:
    """The corpus as one labelled markdown blob, bounded by :data:`MAX_CORPUS_CHARS`.

    Every document is labelled with its source and path so the model can
    reconcile two documents that disagree (and so a reader debugging a bad fact
    can find where it came from). Overflow is cut at a document boundary and
    announced, because a corpus that was silently half-read would produce a brief
    that looks complete and is not.
    """
    chunks: list[str] = []
    used = 0
    dropped = 0
    for doc in documents:
        block = f"\n--- Source: {doc.source_title} | Document: {doc.path} ---\n{doc.markdown}\n"
        if used + len(block) > MAX_CORPUS_CHARS:
            dropped += 1
            continue
        chunks.append(block)
        used += len(block)
    if dropped:
        chunks.append(
            f"\n--- {dropped} further document(s) omitted: the corpus exceeds the "
            f"{MAX_CORPUS_CHARS}-character distillation budget ---\n"
        )
    return "".join(chunks)


def _distil_prompt(project_name: str, documents: list[CorpusDocument]) -> str:
    """The distillation prompt: the corpus, plus the JSON shape the backend parses.

    The skill carries the methodology (what a good brief covers, what makes a
    fact atomic); this pins the machine-readable output shape, exactly the
    division of labour ``skills.load_skill`` documents.
    """
    categories = ", ".join(BUSINESS_FACT_CATEGORIES)
    return (
        "Distil the business documents below into ONE brief and a set of structured "
        "facts, so a QC can write test cases grounded in this product's domain "
        "rather than in its code.\n\n"
        f"Project: {project_name or 'unknown'}\n"
        f"Documents: {len(documents)}\n\n"
        "=== BEGIN CORPUS ===\n"
        f"{_render_corpus(documents)}"
        "\n=== END CORPUS ===\n\n"
        "Return a JSON object with EXACTLY these keys:\n"
        '{"brief": string, "facts": [{"category": string, "term": string, '
        '"statement": string, "detail": string}]}\n'
        f"- brief: prose, at most {BRIEF_TOKEN_BUDGET} tokens (~{BRIEF_CHAR_BUDGET} "
        "characters): what this product is, who uses it, the core workflows, the "
        "domain vocabulary, and the rules a tester must know. It must stand alone — "
        "nothing downstream ever sees these documents.\n"
        f"- facts[].category: one of {categories}.\n"
        "- facts[].term: the thing defined or governed; the retrieval key, in the "
        "project's own wording, unique within its category.\n"
        "- facts[].statement: the fact in ONE sentence, usable verbatim in a prompt.\n"
        "- facts[].detail: supporting specifics (numbers, exceptions, the quoted "
        "sentence); may be empty.\n"
        "- Only what the documents support. Do not infer, do not fill gaps, and "
        "return empty values rather than inventing content."
    )


def _clamp_brief(text: str) -> str:
    """Trim a brief to :data:`BRIEF_CHAR_BUDGET`, on a word boundary where possible.

    The budget is enforced in code and not merely asked for in the skill: a
    prompt-stated limit drifts with the model, and this one is a prompt-budget
    guarantee every later stage depends on.
    """
    brief = (text or "").strip()
    if len(brief) <= BRIEF_CHAR_BUDGET:
        return brief
    cut = brief[:BRIEF_CHAR_BUDGET]
    boundary = cut.rfind(" ")
    return (cut[:boundary] if boundary > BRIEF_CHAR_BUDGET // 2 else cut).rstrip() + "…"


def _clean_facts(raw_facts) -> list[dict[str, str]]:
    """Keep the facts that are usable, normalized to the four stored fields.

    A fact is dropped when its category is not one of
    :data:`~app.models.business.BUSINESS_FACT_CATEGORIES` or when it carries no
    ``term`` or no ``statement`` — a fact with no retrieval key cannot be merged
    (:func:`merge_facts` keys on it) and one with no statement has nothing to
    say. Dropping is deliberate: a malformed fact stored anyway would be a
    permanent row that no later re-sync can correct.
    """
    cleaned: list[dict[str, str]] = []
    for raw in raw_facts or []:
        if not isinstance(raw, dict):
            continue
        category = str(raw.get("category", "") or "").strip().lower()
        term = str(raw.get("term", "") or "").strip()
        statement = str(raw.get("statement", "") or "").strip()
        if category not in BUSINESS_FACT_CATEGORIES or not term or not statement:
            continue
        cleaned.append(
            {
                "category": category,
                "term": term[:300],
                "statement": statement,
                "detail": str(raw.get("detail", "") or "").strip(),
            }
        )
    return cleaned


def build_distillation(
    project_name: str, documents: list[CorpusDocument], *, timeout: int | None = None
) -> dict:
    """Call Claude (business-analyst skill) over the corpus and normalize the result.

    The single point in the system where raw documents are read into a prompt.

    :param project_name: For the prompt's framing only.
    :param documents: The corpus, from :func:`collect_corpus`.
    :param timeout: Claude CLI budget; the caller's default otherwise.
    :returns: ``{"brief": str, "facts": [{category, term, statement, detail}]}``,
        with the brief clamped to :data:`BRIEF_CHAR_BUDGET` and every unusable
        fact dropped.
    """
    raw = run_json(
        _distil_prompt(project_name, documents),
        skill=BUSINESS_ANALYST,
        label=f"Distil business knowledge: {project_name}",
        timeout=timeout,
    )
    data = raw if isinstance(raw, dict) else {}
    return {
        "brief": _clamp_brief(str(data.get("brief", "") or "")),
        "facts": _clean_facts(data.get("facts")),
    }


def _fact_key(category: str, term: str) -> tuple[str, str]:
    """The identity a fact collides on: its category and its term, case-folded.

    Term alone would conflate a glossary entry and a rule that happen to be about
    the same noun — two genuinely different facts a prompt wants both of.
    """
    return (category.strip().lower(), term.strip().lower())


def _rank_text(term: str, statement: str, detail: str) -> str:
    """The denormalized text ``BusinessFact.rank_text`` is scored on.

    Materialized on the row because retrieval here is keyword overlap
    (``prompts._rank_by_relevance``) and there is no vector store anywhere in
    this codebase — so the searchable projection is stored, not recomputed per
    query (see ``app.models.business``).
    """
    return " ".join(part for part in (term, statement, detail) if part)


def merge_facts(
    db,
    project_guid: str,
    owner_id: int | None,
    facts: list[dict[str, str]],
    *,
    source_id: int | None = None,
) -> int:
    """Merge distilled facts into ``business_fact`` under the **no-clobber** rule.

    The rule is not a new one. ``knowledge_service.merge_verified_discovery``
    already states it for the code KB — *an existing entry that already has a
    truthy* ``verified_at_runtime`` *is never overwritten; a discovery colliding
    with an UN-verified entry upgrades it in place* — and this is the identical
    rule with ``pinned`` in place of ``verified_at_runtime``:

    * A colliding **pinned** row is left exactly as it is. A human correction
      survives every future re-sync; that is the whole point of the flag.
    * A colliding **unpinned** row is updated in place, keeping its identity (its
      id, and therefore anything referencing it) and its ``pinned`` / ``excluded``
      flags, so an excluded fact stays excluded after a re-sync.
    * A fact colliding with nothing is inserted.

    Facts are never deleted here. A fact that has dropped out of the corpus stops
    being refreshed, but reaping ingested rows is #827's call to make with
    ``origin``/``pinned`` in hand, not a side effect of a sync.

    :param db: Active session; committed by this function.
    :param project_guid: The owning project's GUID.
    :param owner_id: The facts' owner (ADR 0009 §3); part of the lookup, so one
        user's facts are never merged into another's.
    :param facts: Cleaned facts, as produced by :func:`build_distillation`.
    :param source_id: The source to attribute new/updated rows to, when the
        distillation covered exactly one.
    :returns: How many rows were inserted or updated — pinned collisions, being
        skipped, are not counted.
    """
    existing = (
        db.query(BusinessFact)
        .filter(
            BusinessFact.project_guid == project_guid,
            BusinessFact.owner_id == owner_id,
        )
        .all()
    )
    by_key: dict[tuple[str, str], BusinessFact] = {}
    for row in existing:
        by_key.setdefault(_fact_key(row.category, row.term), row)

    merged = 0
    for fact in facts:
        key = _fact_key(fact["category"], fact["term"])
        row = by_key.get(key)
        if row is not None and row.pinned:
            continue  # no-clobber: the human correction stands
        if row is None:
            row = BusinessFact(
                project_guid=project_guid,
                owner_id=owner_id,
                category=fact["category"],
                term=fact["term"],
                origin="ingested",
            )
            db.add(row)
            by_key[key] = row
        # Upgrade in place: statement/detail/provenance are refreshed, while
        # `pinned` and `excluded` — the human's decisions — are left untouched.
        row.statement = fact["statement"]
        row.detail = fact["detail"]
        row.rank_text = _rank_text(fact["term"], fact["statement"], fact["detail"])
        if source_id is not None:
            row.source_id = source_id
        merged += 1

    if merged:
        db.commit()
    return merged


def _brief_row(db, project_guid: str, owner_id: int | None) -> ProjectConfig | None:
    """The ``ProjectConfig`` the brief is stored on, for this project and owner.

    Looked up on ``project_guid`` (ADR 0013 / #585 — the identity that survives a
    rename) and scoped to ``owner_id``, so one user's brief is never written onto
    another user's same-keyed row (ADR 0009 §3).
    """
    return (
        db.query(ProjectConfig)
        .filter(ProjectConfig.project_guid == project_guid, ProjectConfig.owner_id == owner_id)
        .first()
    )


def _write_brief(row: ProjectConfig, **fields) -> None:
    """Patch ``business_brief``, reassigning so SQLAlchemy tracks the JSON change.

    Mutating a JSON column in place is not tracked (the same reason
    ``merge_verified_discovery`` reassigns ``row.knowledge``), so the dict is
    rebuilt rather than updated.
    """
    row.business_brief = {**(row.business_brief or {}), **fields}


def distil_project(
    db,
    project_guid: str,
    owner_id: int | None,
    *,
    project_name: str = "",
    timeout: int | None = None,
) -> dict:
    """Distil one project's corpus, synchronously, and persist both halves.

    The brief lands on ``ProjectConfig.business_brief`` as
    ``{brief, hash, built_at, status, last_error}``; the facts are merged under
    :func:`merge_facts`.

    Failure is a *state*, not an exception: a Claude error marks the brief
    ``"error"`` with the message and **leaves the previous brief text in place**,
    for the same reason a failed re-sync leaves the previous snapshot on disk —
    a failed rebuild must not cost the project the grounding it already had.

    An **empty corpus** is not a failure either. It ends ``"ready"`` with an empty
    brief, because "this project has nothing ingested yet" is a true and useful
    answer, and a project with no documents is the cold-start case the epic
    treats as normal (#826).

    :param db: Active session; committed by this function.
    :param project_guid: The owning project's GUID.
    :param owner_id: Whose corpus and whose brief.
    :param project_name: For the prompt's framing, and for the config row created
        when the project has none yet.
    :param timeout: Claude CLI budget.
    :returns: ``{"status", "hash", "facts_merged", "documents"}`` — a summary for
        the caller; the durable result is on the rows.
    """
    row = _brief_row(db, project_guid, owner_id)
    if row is None:
        row = ProjectConfig(
            project_guid=project_guid,
            key=project_name or project_guid,
            name=project_name or project_guid,
            owner_id=owner_id,
        )
        db.add(row)

    documents = collect_corpus(db, project_guid, owner_id)
    digest = corpus_hash(documents)

    _write_brief(row, status="building", last_error="")
    db.commit()

    if not documents:
        _write_brief(
            row,
            brief="",
            hash=digest,
            built_at=utcnow().isoformat(),
            status="ready",
            last_error="",
        )
        db.commit()
        return {"status": "ready", "hash": digest, "facts_merged": 0, "documents": 0}

    try:
        with run_context.owner_scope(owner_id):
            result = build_distillation(project_name, documents, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - the failure IS the brief's next state
        db.rollback()
        row = _brief_row(db, project_guid, owner_id) or row
        _write_brief(row, status="error", last_error=str(exc)[:_MAX_ERROR_CHARS])
        db.commit()
        logger.error("Business distillation failed for {}: {}", project_guid, exc)
        return {"status": "error", "hash": digest, "facts_merged": 0, "documents": len(documents)}

    source_ids = {doc.source_id for doc in documents}
    merged = merge_facts(
        db,
        project_guid,
        owner_id,
        result["facts"],
        source_id=next(iter(source_ids)) if len(source_ids) == 1 else None,
    )
    _write_brief(
        row,
        brief=result["brief"],
        hash=digest,
        built_at=utcnow().isoformat(),
        status="ready",
        last_error="",
    )
    db.commit()
    logger.info(
        "Distilled business knowledge for {}: {} document(s), {} fact(s) merged",
        project_guid, len(documents), merged,
    )
    return {
        "status": "ready",
        "hash": digest,
        "facts_merged": merged,
        "documents": len(documents),
    }


def start_distil(
    project_guid: str, owner_id: int | None, *, project_name: str = ""
) -> bool:
    """Kick off a distillation on a daemon thread (no-op if one is in flight).

    Mirrors ``knowledge_service.start_build`` and ``pipeline.start_sync``: the
    work runs off the request thread because a distillation is a full Claude
    call, and the client polls ``business_brief["status"]``. The worker opens its
    **own** session — it must not borrow the request's.

    :param project_guid: The owning project's GUID.
    :param owner_id: Whose corpus and whose brief.
    :param project_name: For the prompt's framing.
    :returns: ``True`` if a thread was started, ``False`` if one was already
        running for this project/owner.
    """
    key = _guard_key(project_guid, owner_id)
    if key in _distilling:
        return False
    _distilling.add(key)
    threading.Thread(
        target=_run_distil, args=(project_guid, owner_id, project_name), daemon=True
    ).start()
    return True


def _run_distil(project_guid: str, owner_id: int | None, project_name: str) -> None:
    """Thread body for :func:`start_distil`. Never propagates an exception."""
    db = db_module.SessionLocal()
    try:
        distil_project(db, project_guid, owner_id, project_name=project_name)
    except Exception as exc:  # noqa: BLE001 - a thread death would be silent
        db.rollback()
        logger.error("Business distillation thread failed for {}: {}", project_guid, exc)
    finally:
        _distilling.discard(_guard_key(project_guid, owner_id))
        db.close()
