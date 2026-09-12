"""Resolve the secret a credentialed source needs — on the caller's side (#821).

The adapter contract is deliberate about this: an adapter turns an address plus
a credential into bytes. It never touches the database, so it can be exercised
in a test with a literal token. Something still has to look the token up, and
this is that something.

One rule, and it is the ADR 0009 rule: the credential comes from the *source's
own* ``connection_id``, so a per-user source can only ever be fetched with that
user's connection. There is no "first GitHub connection" fallback — inheriting a
credential from another row is how a private repository ends up read through
somebody else's token.

A source with no connection is not an error. A public GitHub repository (and
every generic URL) needs no token at all, and that is the common case; the
adapter's 404 message is what tells the user to connect an account when the
repository turns out to be private.
"""

from __future__ import annotations

from app import crypto
from app.models.provider_connection import ProviderConnection
from app.services.business_ingest.base import SourceCredential

__all__ = ["resolve_credential"]


def resolve_credential(db, source) -> SourceCredential | None:
    """The token for ``source``, or ``None`` when it needs none / has none.

    :param db: An open session.
    :param source: The ``BusinessSource`` row; ``connection_id`` and ``owner_id``
        are read.
    :returns: A :class:`~...base.SourceCredential` carrying the decrypted PAT and
        the connection's ``kind`` in ``extra``, or ``None``.
    """
    connection_id = getattr(source, "connection_id", None)
    if not connection_id:
        return None
    connection = db.get(ProviderConnection, connection_id)
    if connection is None:
        return None
    # ADR 0009: a per-user source never borrows another user's connection.
    owner_id = getattr(source, "owner_id", None)
    if owner_id is not None and connection.owner_id not in (None, owner_id):
        return None
    # A hub-backed mirror holds no PAT and never will (#514) — the hub does not
    # release it. Treating it as "no credential" is honest: the fetch then fails
    # with the "connect an account with read access" message rather than with a
    # confusing empty-bearer 401.
    if connection.is_hub_backed:
        return None
    token = crypto.decrypt((connection.secrets or {}).get("pat")) or ""
    if not token:
        return None
    return SourceCredential(token=token, extra={"kind": connection.kind})
