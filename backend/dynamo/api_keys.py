"""ApiKeys テーブル (Phase C).

cowork 等の Gateway クライアント向けに発行する、期限付きの長期 API Key.
プレーンテキスト (`sk-stratoclave-<32chars>`) はサーバ側に保存せず、
SHA-256 ハッシュのみを DB に置く.

テーブル設計 (iac/lib/dynamodb-stack.ts):
  PK: key_hash (String, sha256 hex)
  GSI user-id-index: PK user_id, SK created_at
  属性:
    key_hash: str            sha256(plaintext) の hex
    key_id: str              UI 表示用 "sk-stratoclave-XXXX…YYYY"
    user_id: str             所有者 Cognito sub
    name: str                ユーザーラベル (任意)
    scopes: list[str]        付与された permission 文字列
    created_at: str (ISO)
    expires_at: str or None  None = 無期限
    revoked_at: str or None  None = 有効
    last_used_at: str or None
    created_by: str          通常は user_id、Admin 代理発行時は actor.user_id

制約:
  - 1 ユーザーあたり active (revoked_at None かつ expires_at > now) は 5 個まで
  - revoke は論理削除 (revoked_at を埋める)
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
# プレーンキー部分の長さ (prefix 除く). 32 bytes base58 ≒ 43 chars 相当だが
# base64url 32 chars で十分なエントロピ (192 bits)
KEY_RANDOM_LEN = 32


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_name() -> str:
    return os.getenv("DYNAMODB_API_KEYS_TABLE", "stratoclave-api-keys")


class ApiKeyLimitExceededError(Exception):
    """ユーザーの active キー上限超過."""


class ApiKeyNotFoundError(Exception):
    """指定 key が存在しない or 権限外."""


# ---------------------------------------------------------------
# Key generation / hashing helpers
# ---------------------------------------------------------------
def generate_plain_key() -> str:
    """新規プレーンテキストキーを作成. 返り値はクライアントに 1 回だけ返して破棄."""
    # base64url: 32 bytes = 43 chars (パディングなし), 含まれる文字は [A-Za-z0-9_-]
    raw = secrets.token_urlsafe(KEY_RANDOM_LEN)
    # 長さが環境依存で変わらないよう切り詰め
    raw = raw[:KEY_RANDOM_LEN]
    return f"{KEY_PREFIX}{raw}"


def hash_key(plain: str) -> str:
    """SHA-256 hex."""
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def build_key_id(plain: str) -> str:
    """表示用の識別子: 先頭 4 + 末尾 4 文字を残し中間を伏せる."""
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
            ScanIndexForward=False,  # 新しい順
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
        """新規キーを作成. 返り値は (DB item, plaintext) の tuple.

        plaintext は一度しか返せないため、API レスポンスで即ユーザに表示した後に
        破棄すること. DB には key_hash のみ保存.

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
        # Only non-ephemeral keys count against the per-user cap.
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
            "expires_at": expires_at,  # None 可
            "revoked_at": None,
            "last_used_at": None,
            "created_by": created_by,
            "ephemeral": ephemeral,
        }
        # DynamoDB は None を書けないので、expires_at / revoked_at / last_used_at は None のときキーを落とす
        db_item = {k: v for k, v in item.items() if v is not None}
        try:
            self._table.put_item(
                Item=db_item,
                ConditionExpression="attribute_not_exists(key_hash)",
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                # ほぼありえないが、生成したキーが既存と衝突 (1 in 2^192)
                raise RuntimeError("api key collision, regenerate")
            raise
        return item, plain

    def revoke(self, key_hash: str, *, actor_user_id: str) -> dict[str, Any]:
        """論理削除. 既に revoke されていれば idempotent に成功."""
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
        """認証成功時に last_used_at を更新. 失敗は黙殺 (高頻度のため)."""
        try:
            self._table.update_item(
                Key={"key_hash": key_hash},
                UpdateExpression="SET last_used_at = :now",
                ExpressionAttributeValues={":now": _now_iso()},
            )
        except ClientError:
            # last_used_at の更新失敗で認証自体を落とさない
            pass


# ---------------------------------------------------------------
# 表示用 shape (UI/CLI 共通).
# plaintext は作成時のレスポンスにのみ含む (本 dict には出さない)
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
