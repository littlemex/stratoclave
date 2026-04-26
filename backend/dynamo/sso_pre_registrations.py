"""SsoPreRegistrations テーブル (Phase S).

`invite_only` プロビジョニング用、Admin が email を事前登録するテーブル.

テーブル設計 (iac/lib/dynamodb-stack.ts):
  PK: email  (lowercase)
  GSI iam-user-index: PK iam_user_lookup_key  ("<account_id>#<iam_user_name>")
  属性:
    email: str (lowercase)
    account_id: str
    invited_role: "user" | "team_lead"
    tenant_id: str | None
    total_credit: int | None
    iam_user_lookup_key: str | None   "<account_id>#<iam_user_name>" (IAM user 招待時)
    invited_by: str  (Admin user_id)
    invited_at: str (ISO 8601)
    consumed_at: str | None           初回 SSO login で consume、None なら未使用
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from .client import get_dynamodb_resource


_VALID_ROLES = {"user", "team_lead"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_name() -> str:
    return os.getenv(
        "DYNAMODB_SSO_PRE_REGISTRATIONS_TABLE",
        "stratoclave-sso-pre-registrations",
    )


class SsoInviteNotFoundError(Exception):
    """指定 email の SSO 招待が存在しない."""


def build_iam_user_lookup_key(account_id: str, iam_user_name: str) -> str:
    return f"{account_id}#{iam_user_name}"


class SsoPreRegistrationsRepository:
    """SSO 事前招待 (invite_only ポリシー専用) の CRUD."""

    def __init__(self, table_name: Optional[str] = None) -> None:
        self._table = get_dynamodb_resource().Table(table_name or _table_name())

    # ----- read -----
    def get(self, email: str) -> Optional[dict[str, Any]]:
        resp = self._table.get_item(Key={"email": email.lower()})
        return resp.get("Item")

    def find_by_iam_user(self, account_id: str, iam_user_name: str) -> Optional[dict[str, Any]]:
        key = build_iam_user_lookup_key(account_id, iam_user_name)
        resp = self._table.query(
            IndexName="iam-user-index",
            KeyConditionExpression=Key("iam_user_lookup_key").eq(key),
            Limit=1,
        )
        items = resp.get("Items", [])
        return items[0] if items else None

    def list_by_account(
        self, account_id: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        # email の PK に account_id がないため Scan + filter (件数限定なので OK)
        resp = self._table.scan(
            FilterExpression=Key("account_id").eq(account_id),
            Limit=min(limit, 200),
        )
        return resp.get("Items", [])

    def list_all(
        self,
        *,
        cursor: Optional[dict[str, Any]] = None,
        limit: int = 100,
    ) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
        kwargs: dict[str, Any] = {"Limit": min(limit, 100)}
        if cursor:
            kwargs["ExclusiveStartKey"] = cursor
        resp = self._table.scan(**kwargs)
        return resp.get("Items", []), resp.get("LastEvaluatedKey")

    # ----- write -----
    def invite(
        self,
        *,
        email: str,
        account_id: str,
        invited_role: str = "user",
        tenant_id: Optional[str] = None,
        total_credit: Optional[int] = None,
        iam_user_name: Optional[str] = None,
        invited_by: str,
    ) -> dict[str, Any]:
        if invited_role not in _VALID_ROLES:
            raise ValueError(f"invited_role must be one of {_VALID_ROLES}")
        email_lower = email.lower().strip()
        if "@" not in email_lower:
            raise ValueError(f"email must contain '@': {email}")

        item: dict[str, Any] = {
            "email": email_lower,
            "account_id": account_id,
            "invited_role": invited_role,
            "tenant_id": tenant_id,
            "total_credit": Decimal(total_credit) if total_credit is not None else None,
            "iam_user_lookup_key": (
                build_iam_user_lookup_key(account_id, iam_user_name)
                if iam_user_name
                else None
            ),
            "invited_by": invited_by,
            "invited_at": _now_iso(),
            "consumed_at": None,
        }
        item = {k: v for k, v in item.items() if v is not None}
        self._table.put_item(Item=item)
        return item

    def mark_consumed(self, email: str) -> None:
        """初回 SSO login 成功時、consumed_at に現在時刻を記録."""
        try:
            self._table.update_item(
                Key={"email": email.lower()},
                UpdateExpression="SET consumed_at = :now",
                ExpressionAttributeValues={":now": _now_iso()},
                ConditionExpression="attribute_exists(email)",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise SsoInviteNotFoundError(email)
            raise

    def delete(self, email: str) -> None:
        self._table.delete_item(Key={"email": email.lower()})
