"""Has the upstream document moved since we took the snapshot? (#830, ADR 0016 §4)

Snapshot-with-manual-resync is only defensible if the user can **see** that a
snapshot has gone stale. Otherwise "we took a snapshot" quietly becomes "your
test cases are grounded in a document that changed three weeks ago and nobody
said so". This module is the machinery behind that badge.

Two kinds of answer, and keeping them apart is the entire point:

* **A revision probe** — one cheap request that asks the upstream side for its
  own version marker. GitHub answers with the commit SHA touching the source's
  path; an Azure DevOps wiki with a digest of its pages' ``eTag``s; a generic
  URL with its ``ETag`` or ``Last-Modified``. Comparing that against the value
  recorded when the snapshot was taken is the *server's* verdict, not our guess,
  and it is a real "changed" / "unchanged".
* **Nothing** — an upload has no address at all, and a great many web pages send
  no validator. There is then no cheap way to know, and the honest output is the
  snapshot's **age**: "last fetched 34 days ago". It must be rendered as an age
  and never as a confident "changed", because claiming knowledge we do not have
  is worse than admitting the gap — that is the whole of the slice.

Which one applies is not inferred by the caller: :func:`probe_state` returns it.

**A probe never raises into the caller.** Every failure — no adapter probe, no
validator, a 403, a timeout — lands on the row as ``probe_error`` with
``stale`` left alone, because a staleness check that takes down the page it
annotates is worse than one that says "could not check".
"""

from __future__ import annotations

from dataclasses import dataclass

from app.db import utcnow
from app.models.business import BusinessSource
from app.services.business_ingest import adapters
from app.services.business_ingest.base import BusinessIngestError, SourceCredential

__all__ = [
    "ProbeState",
    "PROBE_KINDS",
    "probe_supported",
    "probe_revision",
    "probe_state",
    "record_revision",
    "refresh_staleness",
    "NO_PROBE_MESSAGE",
]

#: Source kinds whose adapter can answer "what version is upstream on now?".
#: ``upload`` is absent because an uploaded file has no address — re-uploading
#: is how its version changes, and there is nobody to ask.
PROBE_KINDS = ("url", "github_md", "ado_wiki")

#: Shown when a source's kind has no probe at all. Phrased as what the user can
#: do, because "unsupported" on its own reads as a defect rather than a fact
#: about uploaded files.
NO_PROBE_MESSAGE = (
    "an uploaded file has no address to check against — upload the new version "
    "to replace it"
)

#: ``BusinessSource.probe_error`` is ``String(1000)``.
_MAX_ERROR_CHARS = 1000
#: ``BusinessSource.upstream_rev`` is ``String(200)``.
_MAX_REV_CHARS = 200


@dataclass(frozen=True)
class ProbeState:
    """What we are entitled to say about one source's freshness.

    :param mode: ``"revision"`` when a probe has actually compared upstream
        versions, ``"age"`` when no probe can answer and only the snapshot's age
        is known, ``"unknown"`` when a probe exists but has never been run.
    :param stale: Meaningful **only** when ``mode == "revision"``.
    :param detail: The reason a probe could not answer, verbatim from the
        adapter; empty otherwise.
    """

    mode: str
    stale: bool = False
    detail: str = ""


def probe_supported(kind: str) -> bool:
    """Whether sources of ``kind`` have a cheap upstream-version probe.

    :param kind: One of ``app.models.business.BUSINESS_SOURCE_KINDS``.
    :returns: True for the link kinds, False for ``upload``.
    """
    return kind in PROBE_KINDS


def probe_state(source: BusinessSource) -> ProbeState:
    """Classify what the stored columns entitle the UI to claim about ``source``.

    Deliberately a function over the row rather than three booleans the client
    re-combines: "we have never looked" and "we looked and it is unchanged" are
    the pair a staleness badge most easily conflates, and they must be decided
    once, here.

    :param source: The row.
    :returns: The :class:`ProbeState` to render.
    """
    if not probe_supported(source.kind or ""):
        return ProbeState(mode="age", detail=NO_PROBE_MESSAGE)
    if source.probe_error:
        return ProbeState(mode="age", detail=source.probe_error)
    if source.probed_at is None:
        return ProbeState(mode="unknown")
    return ProbeState(mode="revision", stale=bool(source.stale))


def probe_revision(source: BusinessSource, credential: SourceCredential | None) -> str:
    """Ask the upstream side for its current version marker.

    :param source: The row to probe; the adapter reads only its ``url``.
    :param credential: The resolved secret, or ``None`` for a credential-free
        kind. Resolved by the caller — never by the adapter.
    :returns: The adapter's revision string, non-empty.
    :raises BusinessIngestError: when the probe cannot answer, including when
        the kind has no probe at all.
    """
    if not probe_supported(source.kind or ""):
        raise BusinessIngestError(NO_PROBE_MESSAGE)
    adapter = adapters.get_adapter(source.kind)
    probe = getattr(adapter, "probe_revision", None)
    if probe is None:  # pragma: no cover - every PROBE_KINDS adapter has one
        raise BusinessIngestError(NO_PROBE_MESSAGE)
    revision = str(probe(source, credential) or "")
    if not revision:  # pragma: no cover - adapters raise rather than return ""
        raise BusinessIngestError("the source did not report a version")
    return revision[:_MAX_REV_CHARS]


def record_revision(source: BusinessSource, credential: SourceCredential | None = None) -> None:
    """Stamp the upstream version **as it stood when this snapshot was taken**.

    Called by the pipeline immediately after a successful ingest, so the value
    stored is the one a later probe is compared against. It costs one extra
    cheap request per sync, and that is the price of the comparison being
    against a marker produced by the same function that will produce the next
    one — a stored value in one format and a probed value in another would
    report "changed" forever.

    Best-effort by design: a source whose probe fails is still perfectly
    ingested, so the failure is recorded on the row (which makes the UI fall
    back to the age label) and never raised.

    :param source: The freshly ingested row. Mutated, not committed — the
        caller's transaction owns that.
    :param credential: The same credential the fetch used.
    """
    source.probed_at = utcnow()
    source.stale = False
    try:
        source.upstream_rev = probe_revision(source, credential)
        source.probe_error = ""
    except BusinessIngestError as exc:
        source.upstream_rev = ""
        source.probe_error = str(exc)[:_MAX_ERROR_CHARS]
    except Exception as exc:  # noqa: BLE001 - a probe must never fail an ingest
        source.upstream_rev = ""
        source.probe_error = f"could not check for updates ({exc})"[:_MAX_ERROR_CHARS]


def refresh_staleness(
    db, source: BusinessSource, credential: SourceCredential | None = None
) -> BusinessSource:
    """Probe ``source`` now and record whether the upstream version has moved.

    ``stale`` is only ever written by a probe that **answered**. A failed probe
    leaves the previous verdict alone and writes ``probe_error`` instead: the
    last real answer is better information than a fabricated one, and the UI
    shows the age label while the error stands.

    A source that has never been synced has no ``upstream_rev`` to compare
    against, so it is not reported stale — there is no snapshot for anything to
    have drifted from. That is why the comparison is guarded rather than being a
    bare ``!=``, which would call every ``pending`` row stale.

    :param db: Active session; committed by this function.
    :param source: The row to check.
    :param credential: Resolved by the caller.
    :returns: ``source``, updated and committed.
    """
    source.probed_at = utcnow()
    try:
        current = probe_revision(source, credential)
    except BusinessIngestError as exc:
        source.probe_error = str(exc)[:_MAX_ERROR_CHARS]
        db.commit()
        return source
    except Exception as exc:  # noqa: BLE001 - never surface as a 500
        source.probe_error = f"could not check for updates ({exc})"[:_MAX_ERROR_CHARS]
        db.commit()
        return source

    source.probe_error = ""
    source.stale = bool(source.upstream_rev) and current != source.upstream_rev
    db.commit()
    return source
