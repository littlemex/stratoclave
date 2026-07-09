"""ApiKeys table (Phase C).

Long-lived API keys issued to Gateway clients such as cowork.
The plaintext key (`sk-stratoclave-<32chars>`) is never stored server-side;
only the SHA-256 hash is persisted in DynamoDB.

Table design (iac/lib/dynamodb-stack.ts):
  PK: key_hash (String, sha256 hex)
  GSI user-id-index: PK user_id, SK created_at
  Attributes:
    key_hash: str            hex of sha256(plaintext)
    key_id: str              masked display ID "sk-stratoclave-XXXX…YYYY"
    user_id: str             owner's Cognito sub
    name: str                user-supplied label (optional)
    scopes: list[str]        granted permission strings
    created_at: str (ISO)
    expires_at: str or None  None = no expiry
    revoked_at: str or None  None = active
    last_used_at: str or None
    created_by: str          normally user_id; actor.user_id for admin-issued keys

Constraints:
  - Maximum 5 active keys per user (revoked_at is None and expires_at > now)
  - Revocation is logical (sets revoked_at, does not delete the row)
"""
from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from .client import get_dynamodb_resource


MAX_ACTIVE_KEYS_PER_USER = 5
KEY_PREFIX = "sk-stratoclave-"
# Length of the random portion (excluding the prefix). 32 bytes base58 ≈ 43 chars,
# but 32 base64url chars provide sufficient entropy (192 bits).
KEY_RANDOM_LEN = 32


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_name() -> str:
    return os.getenv("DYNAMODB_API_KEYS_TABLE", "stratoclave-api-keys")


class ApiKeyLimitExceededError(Exception):
    """Raised when a user has reached the active API key limit."""


class ApiKeyNotFoundError(Exception):
    """Raised when the specified key does not exist or is out of scope."""


# ---------------------------------------------------------------
# Key generation / hashing helpers
# ---------------------------------------------------------------
def generate_plain_key() -> str:
    """Generate a new plaintext key. Return it to the client once and then discard."""
    # base64url: 32 bytes = 43 chars (no padding), characters are [A-Za-z0-9_-].
    raw = secrets.token_urlsafe(KEY_RANDOM_LEN)
    # Truncate to ensure consistent length regardless of the platform.
    raw = raw[:KEY_RANDOM_LEN]
    return f"{KEY_PREFIX}{raw}"


def hash_key(plain: str) -> str:
    """SHA-256 hex."""
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def build_key_id(plain: str) -> str:
    """Build a display-safe identifier by keeping the first 4 and last 4 characters, masking the middle."""
    body = plain[len(KEY_PREFIX):]
    if len(body) <= 8:
        return f"{KEY_PREFIX}{body}"
    return f"{KEY_PREFIX}{body[:4]}...{body[-4:]}"


def is_api_key(token: str) -> bool:
    return isinstance(token, str) and token.startswith(KEY_PREFIX)


# ---------------------------------------------------------------
# Repository
# ---------------------------------------------------------------
class ApiKeysRepository:
    def __init__(self, table_name: Optional[str] = None) -> None:
        self._table = get_dynamodb_resource().Table(table_name or _table_name())

    # ----- read -----
    def get_by_hash(self, key_hash: str) -> Optional[dict[str, Any]]:
        resp = self._table.get_item(Key={"key_hash": key_hash})
        return resp.get("Item")

    def list_by_user(
        self, user_id: str, *, include_revoked: bool = False
    ) -> list[dict[str, Any]]:
        resp = self._table.query(
            IndexName="user-id-index",
            KeyConditionExpression=Key("user_id").eq(user_id),
            ScanIndexForward=False,  # newest first
        )
        items = resp.get("Items", [])
        if include_revoked:
            return items
        return [it for it in items if not it.get("revoked_at")]

    def find_by_user_and_key_id(
        self, user_id: str, key_id: str
    ) -> Optional[dict[str, Any]]:
        """Resolve an owner's key by its masked `key_id` (the value shown
        in `api-key list` output).

        This indirection exists so that revoke endpoints do not need to
        put the SHA-256 `key_hash` in the URL path — the hash is the
        primary lookup key and putting it in ALB / CloudFront access
        logs creates a long-lived enumeration material even after the
        key is rotated.

        Returns None if no key matches — callers must always check ownership
        via this function before doing anything that reveals the row.
        """
        for item in self.list_by_user(user_id, include_revoked=True):
            if str(item.get("key_id")) == key_id:
                return item
        return None

    def find_by_key_id_under_user(
        self, *, user_id: str, key_id: str
    ) -> Optional[dict[str, Any]]:
        """Scoped lookup by masked `key_id` within a single owner.

        C-D (2026-04 critical sweep): the pre-C-D implementation did a
        full `Scan` of the api-keys table to let an admin revoke by
        `key_id` alone. That left the entire credential ledger (every
        SHA-256 key hash + owner + scopes) reachable from any backend
        RCE. The new admin flow requires the owning `user_id` in the
        URL — it is already shown in the admin UI next to every key —
        and we Query the existing `user-id-index` GSI instead.

        Linear over the user's own keys (≤ 5 active + a few revoked)
        so the cost is bounded.
        """
        for item in self.list_by_user(user_id, include_revoked=True):
            if str(item.get("key_id")) == key_id:
                return item
        return None

    def count_active(self, user_id: str) -> int:
        """Count the user's *persistent* active keys.

        P1-B: ephemeral wrapper keys (minted by `stratoclave claude`)
        are excluded from this count. They self-revoke on child exit
        and carry a short TTL, so counting them against the cap would
        lock humans out of minting their own keys whenever a claude
        session was alive.
        """
        now = _now_iso()
        active = 0
        for it in self.list_by_user(user_id, include_revoked=False):
            expires_at = it.get("expires_at")
            if expires_at and expires_at <= now:
                continue
            if it.get("ephemeral"):
                continue
            active += 1
        return active

    def list_all(
        self, *, cursor: Optional[dict[str, Any]] = None, limit: int = 100
    ) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
        kwargs: dict[str, Any] = {"Limit": min(limit, 200)}
        if cursor:
            kwargs["ExclusiveStartKey"] = cursor
        resp = self._table.scan(**kwargs)
        return resp.get("Items", []), resp.get("LastEvaluatedKey")

    def find_any_by_key_id(self, key_id: str) -> Optional[dict[str, Any]]:
        """Admin-scope lookup by masked `key_id` across every user.

        Sweep-4 C-Latent-1 (2026-04-30 round-5 review): this method used
        to exist pre-sweep-3 but was lost during a server-side squash,
        leaving ``admin_api_keys.revoke_any_api_key_by_key_id`` calling
        a NameError at runtime. Admins therefore could not revoke a
        compromised key via the supported ``DELETE /api/mvp/admin/
        api-keys/by-key-id/{key_id}`` path.

        Implementation notes:
          * Uses a full-table Scan with FilterExpression on ``key_id``.
            The ``api-keys`` table is in the backend task role's Scan
            allowlist (see ``iac/lib/ecs-stack.ts`` sweep-4 C-D comment)
            so this is authorised.
          * Scan is paginated (via ``ExclusiveStartKey``) to guarantee
            correctness if the table grows past 1 MB. In practice a
            Stratoclave deployment holds O(100) active keys, so the
            common case is a single page.
          * Auditors (us) chose Scan + Filter over a dedicated
            ``key-id-index`` GSI because the key-id is only ~16 chars
            of masked prefix / suffix and the administrative revoke
            path is rare enough that the Scan cost is acceptable. A
            future optimisation could add a GSI; we pin the Scan
            behaviour via a regression test in
            ``tests/test_api_keys.py``.
          * Returns None if no key matches.
        """
        from boto3.dynamodb.conditions import Attr

        kwargs: dict[str, Any] = {
            "FilterExpression": Attr("key_id").eq(key_id),
            "Limit": 200,
        }
        while True:
            resp = self._table.scan(**kwargs)
            for item in resp.get("Items", []):
                # Filter is server-side; but defensively re-check here
                # to avoid an empty-string collision on an unindexed
                # item shape.
                if str(item.get("key_id") or "") == key_id:
                    return item
            last = resp.get("LastEvaluatedKey")
            if not last:
                return None
            kwargs["ExclusiveStartKey"] = last

    # ----- write -----
    def create(
        self,
        *,
        user_id: str,
        name: str,
        scopes: list[str],
        expires_at: Optional[str],
        created_by: str,
        ephemeral: bool = False,
    ) -> tuple[dict[str, Any], str]:
        """Create a new API key. Returns a (DB item, plaintext) tuple.

        The plaintext can only be returned once — surface it immediately in
        the API response and then discard it. Only the key_hash is stored in DynamoDB.

        ``ephemeral=True`` (P1-B): caller is the CLI `claude` wrapper
        minting a throwaway key for a single child-process lifetime.
        Ephemeral keys:
            * do NOT count against the per-user ``MAX_ACTIVE_KEYS_PER_USER``
              cap (so running ``stratoclave claude`` repeatedly never
              locks out a human's ability to mint their own keys);
            * are marked ``ephemeral: true`` in DynamoDB so audit
              tooling can distinguish them from human-held keys;
            * should always carry a short ``expires_at`` (minutes-level)
              so even a missed revoke on abnormal exit decays quickly.
        """
        if not scopes:
            raise ValueError("scopes must not be empty")
        if not ephemeral and self.count_active(user_id) >= MAX_ACTIVE_KEYS_PER_USER:
            raise ApiKeyLimitExceededError(
                f"user {user_id} already has {MAX_ACTIVE_KEYS_PER_USER} active api keys"
            )
        plain = generate_plain_key()
        key_hash = hash_key(plain)
        now = _now_iso()
        item: dict[str, Any] = {
            "key_hash": key_hash,
            "key_id": build_key_id(plain),
            "user_id": user_id,
            "name": name or "",
            "scopes": list(scopes),
            "created_at": now,
            "expires_at": expires_at,  # None = no expiry
            "revoked_at": None,
            "last_used_at": None,
            "created_by": created_by,
            "ephemeral": ephemeral,
        }
        # DynamoDB cannot store None; drop keys whose value is None.
        db_item = {k: v for k, v in item.items() if v is not None}
        try:
            self._table.put_item(
                Item=db_item,
                ConditionExpression="attribute_not_exists(key_hash)",
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                # Extremely unlikely, but the generated key collided with an existing one (1 in 2^192).
                raise RuntimeError("api key collision, regenerate")
            raise
        return item, plain

    def revoke(self, key_hash: str, *, actor_user_id: str) -> dict[str, Any]:
        """Soft-delete the key. Succeeds idempotently if already revoked."""
        now = _now_iso()
        try:
            resp = self._table.update_item(
                Key={"key_hash": key_hash},
                UpdateExpression="SET revoked_at = if_not_exists(revoked_at, :now)",
                ExpressionAttributeValues={":now": now},
                ConditionExpression="attribute_exists(key_hash)",
                ReturnValues="ALL_NEW",
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise ApiKeyNotFoundError(key_hash)
            raise
        return resp.get("Attributes", {})

    def revoke_all_for_user(self, user_id: str, *, actor_user_id: str) -> int:
        """Revoke every non-revoked API key owned by ``user_id``.

        Z-1 (2026-04 third blind review): when an admin deletes /
        reassigns a user, their access_token is invalidated via the
        ``token_revoked_after`` watermark plus Cognito
        ``global_sign_out``. The user's ``sk-stratoclave-*`` API keys,
        however, live in a separate table keyed on ``key_hash`` and
        are not addressed by either mechanism. Explicitly sweeping
        them here closes the gap: the watermark check in deps.py
        already stops pre-watermark keys at auth time (defence in
        depth), but revoking the row makes the state observable in
        admin listings and immediately idempotent on restore.

        Returns the count of rows transitioned from active to
        revoked (rows that were already revoked are left alone).
        """
        revoked = 0
        for item in self.list_by_user(user_id, include_revoked=False):
            key_hash = str(item.get("key_hash") or "")
            if not key_hash or item.get("revoked_at"):
                continue
            try:
                self.revoke(key_hash, actor_user_id=actor_user_id)
                revoked += 1
            except ApiKeyNotFoundError:
                # Row vanished between scan and revoke — skip quietly.
                continue
        return revoked

    def touch_last_used(self, key_hash: str) -> None:
        """Update last_used_at on successful authentication. Failures are silently ignored (high-frequency path)."""
        try:
            self._table.update_item(
                Key={"key_hash": key_hash},
                UpdateExpression="SET last_used_at = :now",
                ExpressionAttributeValues={":now": _now_iso()},
            )
        except ClientError:
            # A last_used_at update failure must not fail the authentication itself.
            pass


# ---------------------------------------------------------------
# Public shape for UI/CLI (shared).
# Plaintext is only included in the create response, not in this dict.
# ---------------------------------------------------------------
def to_public_dict(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "key_id": item.get("key_id"),
        "name": item.get("name") or "",
        "user_id": item.get("user_id"),
        "scopes": list(item.get("scopes") or []),
        "created_at": item.get("created_at"),
        "expires_at": item.get("expires_at"),
        "revoked_at": item.get("revoked_at"),
        "last_used_at": item.get("last_used_at"),
        "created_by": item.get("created_by"),
    }
