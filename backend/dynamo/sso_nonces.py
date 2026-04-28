"""Replay-protection nonce store for the Vouch-by-STS flow.

Each successfully verified signed `sts:GetCallerIdentity` call is
fingerprinted (SHA-256 of the Authorization header + X-Amz-Date) and
written to this table with a TTL that matches the maximum allowed
skew. A conditional put with ``attribute_not_exists(nonce)`` makes
replaying the same signature within the window impossible.

Schema:
  PK: nonce (String, 64-char hex SHA-256 of the signed request)
  TTL attribute: ``expires_at`` (Number, epoch seconds)
"""
from __future__ import annotations

import hashlib
import os
import time
from typing import Optional

from botocore.exceptions import ClientError

from .client import get_dynamodb_resource, sso_nonces_table_name


# Upper bound on the replay window. Matches the ±5-minute X-Amz-Date
# skew enforced by sso_sts.py so any signature accepted there is also
# covered by a nonce with room to spare.
_DEFAULT_TTL_SECONDS = 600  # 10 minutes


class NonceReplayError(Exception):
    """Raised when the same signed request is submitted twice."""


def fingerprint(authorization: str, x_amz_date: str) -> str:
    """Compute the SHA-256 hex fingerprint used as the nonce key.

    Combining Authorization and X-Amz-Date deterministically identifies
    a single signed request. Reusing either value would produce a new
    Authorization header (SigV4 includes X-Amz-Date in the signed
    headers), so this is a stable identifier for the full signed body.
    """
    h = hashlib.sha256()
    h.update(authorization.encode("utf-8", "replace"))
    h.update(b"\x00")
    h.update(x_amz_date.encode("utf-8", "replace"))
    return h.hexdigest()


class SsoNoncesRepository:
    """DynamoDB-backed nonce store. Safe to re-instantiate; no internal
    state beyond a cached boto3 Table.
    """

    def __init__(self, table_name: Optional[str] = None) -> None:
        self._table = get_dynamodb_resource().Table(
            table_name or sso_nonces_table_name()
        )

    def consume(
        self,
        *,
        authorization: str,
        x_amz_date: str,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> str:
        """Record a newly accepted signature, or raise if it already exists.

        Returns the fingerprint for logging so callers do not have to
        compute it twice.
        """
        nonce = fingerprint(authorization, x_amz_date)
        now = int(time.time())
        expires_at = now + ttl_seconds

        try:
            self._table.put_item(
                Item={
                    "nonce": nonce,
                    "created_at": now,
                    "expires_at": expires_at,
                },
                ConditionExpression="attribute_not_exists(#n)",
                ExpressionAttributeNames={"#n": "nonce"},
            )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                raise NonceReplayError(nonce)
            raise

        return nonce


def nonce_storage_available() -> bool:
    """Return True if the nonces table is usable (env var set and
    DescribeTable succeeds). The SSO exchange path falls back to the
    legacy behavior (±5 min skew only) when this returns False so that
    environments that haven't run the new IaC don't start failing all
    logins.
    """
    if not os.getenv("DYNAMODB_SSO_NONCES_TABLE"):
        # If the env var is not even set, skip without hitting DynamoDB.
        return False
    try:
        SsoNoncesRepository()._table.table_status  # noqa: B018 (probe call)
        return True
    except Exception:
        return False
