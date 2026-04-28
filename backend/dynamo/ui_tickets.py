"""Short-lived, single-use UI handoff tickets.

The CLI mints an opaque nonce, bound to the authenticated session's
tokens, so the user can open the web SPA with
``?ui_ticket=<nonce>`` in the URL instead of the access token itself.
The SPA POSTs the ticket to ``/api/mvp/auth/ui-ticket/consume`` to
recover the tokens and then strips the ``ui_ticket`` query parameter
from the URL immediately.

Why not reuse ``?token=<access_token>``? See P0-8 in the 2026-04
security review: any URL-borne token is a session-fixation primitive
(attacker crafts a link with their own token, victim clicks, victim's
browser is now pinned to the attacker's identity). A ticket closes
that: the nonce on its own has no API authority, expires in 30 s, and
can only be exchanged once.

Why bind tokens at mint time (instead of looking up the caller inside
the consume handler)? So that CLI → browser transport is a pure hand
off: the consume endpoint does not need its own authenticated
session, and we do not have to re-derive the access token from a
long-lived refresh on the consume path.

Schema:
  PK:          ticket_hash  (String, SHA-256 hex of the plaintext
                              nonce — the plaintext only lives in the
                              CLI stdout and the URL bar, never on
                              disk on the backend)
  Attributes:  access_token  (String, the bearer the SPA will receive)
               id_token      (String, optional)
               refresh_token (String, optional)
               expires_in    (Number, seconds)
               token_type    (String, usually ``Bearer``)
               user_id       (String, audit trail only — not returned)
               created_at    (Number, epoch seconds)
               expires_at    (Number, epoch seconds — DynamoDB TTL)

Threat model at consume time:
  * A passive attacker who sees the plaintext nonce in a browser
    access log can race the legitimate user, but the ticket is
    single-use: whichever POST wins gets the tokens, the other gets
    404. Logs are strongly discouraged for any ``?ui_ticket=`` URL;
    the SPA strips the query before any third-party script can fetch
    or observe it.
  * An attacker who captures a DynamoDB write (ECR image compromise)
    sees the hash and the tokens but not the plaintext nonce, and the
    ticket is already 30 s from auto-deletion. The exposure window is
    bounded by the TTL even without ConsumedMarker churn.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any, Optional

from botocore.exceptions import ClientError

from .client import get_dynamodb_resource, ui_tickets_table_name


# 30 s is long enough for the user to alt-tab to the browser and for the
# SPA to complete the `/consume` round trip, short enough that a stolen
# link goes stale by the time most log pipelines ingest it.
_DEFAULT_TTL_SECONDS = 30

# 32 bytes of CSPRNG, base64url-encoded (`secrets.token_urlsafe(32)`
# gives ~43 chars, ~256 bits entropy). Prefix so ops tooling can grep
# tickets out of access logs during incident response.
_TICKET_PREFIX = "stt_"


def generate_plaintext() -> str:
    """Mint a new plaintext ticket. Only the CLI and the consuming
    browser tab ever see this value."""
    return _TICKET_PREFIX + secrets.token_urlsafe(32)


def _hash(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


class TicketNotFoundError(Exception):
    """Raised when a consume call presents a ticket that is missing,
    already consumed, or past its TTL."""


class UiTicketsRepository:
    def __init__(self, table_name: Optional[str] = None) -> None:
        self._table = get_dynamodb_resource().Table(
            table_name or ui_tickets_table_name()
        )

    def mint(
        self,
        *,
        user_id: str,
        access_token: str,
        id_token: Optional[str],
        refresh_token: Optional[str],
        expires_in: Optional[int],
        token_type: Optional[str] = "Bearer",
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> tuple[str, int]:
        """Mint a ticket and return ``(plaintext, expires_at)``.

        The plaintext is the nonce the CLI places in the ``ui_ticket``
        URL parameter. It is NOT stored on the backend; only its
        SHA-256 hash is. Losing the plaintext == losing the ticket,
        which is the whole point.
        """
        plaintext = generate_plaintext()
        ticket_hash = _hash(plaintext)
        now = int(time.time())
        expires_at = now + ttl_seconds

        item: dict[str, Any] = {
            "ticket_hash": ticket_hash,
            "user_id": user_id,
            "access_token": access_token,
            "created_at": now,
            "expires_at": expires_at,
        }
        if id_token:
            item["id_token"] = id_token
        if refresh_token:
            item["refresh_token"] = refresh_token
        if expires_in is not None:
            item["expires_in"] = int(expires_in)
        if token_type:
            item["token_type"] = token_type

        # Defence in depth: there is no scenario where two CLI
        # invocations would mint the same ticket (256-bit CSPRNG
        # collisions are astronomically small), but refuse the write
        # on collision so we fail loud instead of silently overwriting
        # a live ticket.  `ticket_hash` is not a reserved word so we
        # can inline it in the ConditionExpression without an
        # ExpressionAttributeNames mapping (an empty `{}` is rejected
        # by DynamoDB with a ValidationException).
        self._table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(ticket_hash)",
        )
        return plaintext, expires_at

    def consume(self, plaintext: str) -> dict[str, Any]:
        """Atomically delete the ticket and return its tokens payload.

        Returns a dict with ``access_token`` / ``id_token`` /
        ``refresh_token`` / ``expires_in`` / ``token_type``. Raises
        ``TicketNotFoundError`` if the ticket does not exist, has
        already been consumed, or if its ``expires_at`` is in the past.

        The delete-returns-old-image pattern lets us do the "hand over
        the payload then destroy the record" step in one DynamoDB call
        so two racing consumers cannot both succeed. Whoever's
        DeleteItem lands first takes the tokens; the other gets back
        ``Attributes=None`` and receives a TicketNotFoundError.
        """
        ticket_hash = _hash(plaintext)
        try:
            resp = self._table.delete_item(
                Key={"ticket_hash": ticket_hash},
                ConditionExpression="attribute_exists(ticket_hash)",
                ReturnValues="ALL_OLD",
            )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                raise TicketNotFoundError(
                    "ticket not found or already consumed"
                )
            raise

        attrs = resp.get("Attributes") or {}
        if not attrs:
            # Shouldn't happen given the ConditionExpression above,
            # but fail closed anyway.
            raise TicketNotFoundError("ticket not found or already consumed")

        # TTL sweeps are eventually consistent on DynamoDB, so an item
        # whose `expires_at` is already in the past can still be
        # present for up to 48 h before the background worker removes
        # it. Treat that case as expired at the application layer.
        now = int(time.time())
        if int(attrs.get("expires_at", 0)) <= now:
            raise TicketNotFoundError("ticket expired")

        return attrs
