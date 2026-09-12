"""Where a Business Knowledge source's secret comes from, and why it may not (#822).

The adapter contract says an adapter never resolves its own secret
(:mod:`app.services.business_ingest.base`). This module is the resolver, and it
is deliberately source-agnostic: ``upload`` and ``url`` are credential-free,
everything else needs a token, and the *order* it is looked for in is the
product decision this slice was opened to make.

**A per-source token is the primary path, not a fallback.** That inverts the
obvious design ("reuse the project's Azure DevOps connection"), so the reasoning
is recorded here rather than in a commit message, because both halves of it are
properties of the code and either one alone would sink the feature:

1. **A hub-backed connection holds no credential at all.**
   ``ProviderConnection.hub_connection_id`` marks a row that mirrors a
   connection EmeHub owns, and its ``secrets`` are empty *and always will be* —
   the hub returns ``hasPat`` only and never releases the PAT (#501).
   :mod:`app.services.hub_client` exposes no wiki endpoint either (tickets,
   projects, connections, knowledge, credential grants — and nothing else), so
   there is no indirect route through the hub. For such a connection, wiki
   ingestion is not "slow" or "degraded": it is impossible, and the only honest
   thing to do is say so in words that name the fix.
2. **Even a locally-held ADO PAT is probably scoped wrong.** Existing
   connections were provisioned for work items (``vso.work``) and code
   (``vso.code``). Reading a wiki needs ``vso.wiki``, so an otherwise perfectly
   healthy connection simply answers 401 on ``/_apis/wiki/wikis``. Telling that
   user "connection broken" would send them to re-do a connection that is fine.

So: the source's own token wins, a **local** connection is used when it has one,
and a hub-backed connection is refused with its own message instead of being
tried and failing generically.

The token is stored on ``BusinessSource.secrets`` — the same column name, the
same JSON-of-encrypted-values shape and the same :mod:`app.crypto` helpers that
``ProviderConnection.secrets`` already uses. No new secret-storage mechanism is
introduced here, and nothing in this module logs a token or puts one in an
exception message.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app import crypto
from app.models.business import BusinessSource
from app.models.provider_connection import ProviderConnection
from app.services.business_ingest.base import SourceCredential, SourceFetchError

__all__ = [
    "CREDENTIAL_FREE_KINDS",
    "HUB_BACKED_MESSAGE",
    "NO_CREDENTIAL_MESSAGE",
    "CONNECTION_MISSING_MESSAGE",
    "CONNECTION_HAS_NO_TOKEN_MESSAGE",
    "credential_origin",
    "clear_source_token",
    "has_source_token",
    "resolve_credential",
    "set_source_token",
    "source_token",
]

#: Kinds that are fetched without any secret. Stated as the exception list so a
#: new credentialed kind (#821's ``github_md``) needs no edit here at all.
CREDENTIAL_FREE_KINDS = ("upload", "url")

#: The key inside ``BusinessSource.secrets``. Mirrors ``ProviderConnection``'s
#: ``secrets["pat"]`` so there is one spelling for "the Azure DevOps token".
_TOKEN_KEY = "pat"

#: The whole point of the slice, in one sentence the user can act on. A hub
#: connection is *working*; it just cannot hand its PAT to anyone, so the fix is
#: a token on this source and never "reconnect Azure DevOps".
HUB_BACKED_MESSAGE = (
    "Wiki pages can't be read through the shared EmeHub connection — it never "
    "releases its Azure DevOps token. Add a wiki-scoped token for this project."
)

#: No token anywhere. Names the scope, because a token without ``Wiki (Read)``
#: is the next thing that goes wrong (see the 401 message in the adapter).
NO_CREDENTIAL_MESSAGE = (
    "This Azure DevOps wiki needs its own access token. Add a personal access "
    "token with Wiki (Read) scope to this source."
)

CONNECTION_MISSING_MESSAGE = (
    "The Azure DevOps connection this source was linked to no longer exists. "
    "Add a personal access token with Wiki (Read) scope to this source."
)

CONNECTION_HAS_NO_TOKEN_MESSAGE = (
    "The Azure DevOps connection this source is linked to holds no access "
    "token. Add a personal access token with Wiki (Read) scope to this source."
)


def set_source_token(db: Session, source: BusinessSource, token: str) -> BusinessSource:
    """Store ``token`` encrypted on ``source``, replacing any previous one.

    :param db: Active session; committed by this function.
    :param source: The row to attach the token to.
    :param token: The plaintext PAT. Blank clears it, so one code path serves
        "set" and "remove" and a blank submission can never store an empty
        secret that then fails as though it were a scope problem.
    :returns: ``source``.
    """
    cleaned = (token or "").strip()
    secrets = dict(source.secrets or {})
    if cleaned:
        secrets[_TOKEN_KEY] = crypto.encrypt(cleaned)
    else:
        secrets.pop(_TOKEN_KEY, None)
    # Reassigned rather than mutated in place: SQLAlchemy does not track
    # mutation of a plain JSON dict, so an in-place edit would not be persisted.
    source.secrets = secrets
    db.commit()
    return source


def clear_source_token(db: Session, source: BusinessSource) -> BusinessSource:
    """Remove the stored token from ``source``."""
    return set_source_token(db, source, "")


def source_token(source: BusinessSource) -> str:
    """The decrypted per-source token, or ``""`` when there is none.

    A value that cannot be decrypted (the deployment's ``secret_key`` changed)
    returns ``""`` rather than raising, so the source fails with
    :data:`NO_CREDENTIAL_MESSAGE` — which names the fix — instead of an
    ``InvalidToken`` traceback that names nothing.
    """
    stored = (source.secrets or {}).get(_TOKEN_KEY)
    return crypto.decrypt(stored) or ""


def has_source_token(source: BusinessSource) -> bool:
    """Whether ``source`` carries its own usable token."""
    return bool(source_token(source))


def credential_origin(db: Session, source: BusinessSource) -> str:
    """Where this source's token *would* come from, without resolving it.

    Answers the question the UI asks before anything is fetched, so the hub
    constraint is visible at the point the user is deciding, not hours later in
    a failed sync.

    :returns: ``"none"`` (credential-free kind), ``"source"`` (its own token),
        ``"connection"`` (a usable local connection), ``"hub"`` (a hub-backed
        connection, which can never supply one) or ``"missing"``.
    """
    if source.kind in CREDENTIAL_FREE_KINDS:
        return "none"
    if has_source_token(source):
        return "source"
    if source.connection_id:
        connection = db.get(ProviderConnection, source.connection_id)
        if connection is None:
            return "missing"
        if connection.is_hub_backed:
            return "hub"
        if crypto.decrypt((connection.secrets or {}).get(_TOKEN_KEY)):
            return "connection"
    return "missing"


def resolve_credential(db: Session, source: BusinessSource) -> SourceCredential | None:
    """The secret ``source`` should be fetched with.

    :param db: Active session.
    :param source: The row about to be synced.
    :returns: ``None`` for a credential-free kind, else a
        :class:`~app.services.business_ingest.base.SourceCredential` whose
        ``extra["origin"]`` records which of the two paths supplied it — the
        branch a test pins rather than inferring from a status.
    :raises SourceFetchError: with the message for *this* refusal, never a
        generic one. Which of the four it is, is the deliverable.
    """
    if source.kind in CREDENTIAL_FREE_KINDS:
        return None

    token = source_token(source)
    if token:
        return SourceCredential(token=token, extra={"origin": "source"})

    if source.connection_id:
        connection = db.get(ProviderConnection, source.connection_id)
        if connection is None:
            raise SourceFetchError(CONNECTION_MISSING_MESSAGE)
        if connection.is_hub_backed:
            raise SourceFetchError(HUB_BACKED_MESSAGE)
        stored = crypto.decrypt((connection.secrets or {}).get(_TOKEN_KEY))
        if not stored:
            raise SourceFetchError(CONNECTION_HAS_NO_TOKEN_MESSAGE)
        return SourceCredential(token=stored, extra={"origin": "connection"})

    raise SourceFetchError(NO_CREDENTIAL_MESSAGE)
