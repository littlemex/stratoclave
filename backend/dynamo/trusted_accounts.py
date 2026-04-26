"""TrustedAccounts テーブル (Phase S).

SSO / STS 経由ログイン時、どの AWS Account ID からの principal を許可するかの allowlist.

テーブル設計 (iac/lib/dynamodb-stack.ts):
  PK: account_id
  属性:
    account_id: str                    12 桁の AWS Account ID
    description: str
    provisioning_policy: str           "invite_only" | "auto_provision"
    allowed_role_patterns: list[str]   glob (空 list なら全 role 許可)
    allow_iam_user: bool               default False
    allow_instance_profile: bool       default False
    default_tenant_id: str | None
    default_credit: int | None
    created_at / updated_at: str (ISO 8601)
    created_by: str
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from botocore.exceptions import ClientError

from .client import get_dynamodb_resource


_VALID_POLICIES = {"invite_only", "auto_provision"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_name() -> str:
    return os.getenv("DYNAMODB_TRUSTED_ACCOUNTS_TABLE", "stratoclave-trusted-accounts")


class TrustedAccountNotFoundError(Exception):
    """指定 account_id の trusted_account が存在しない."""


class TrustedAccountsRepository:
    """AWS account ごとの SSO 受入ポリシーを CRUD."""

    def __init__(self, table_name: Optional[str] = None) -> None:
        self._table = get_dynamodb_resource().Table(table_name or _table_name())

    # ----- read -----
    def get(self, account_id: str) -> Optional[dict[str, Any]]:
        resp = self._table.get_item(Key={"account_id": account_id})
        return resp.get("Item")

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
    def put(
        self,
        *,
        account_id: str,
        description: str,
        provisioning_policy: str,
        allowed_role_patterns: list[str],
        allow_iam_user: bool = False,
        allow_instance_profile: bool = False,
        default_tenant_id: Optional[str] = None,
        default_credit: Optional[int] = None,
        created_by: str,
    ) -> dict[str, Any]:
        if provisioning_policy not in _VALID_POLICIES:
            raise ValueError(
                f"invalid provisioning_policy: {provisioning_policy}"
                f" (must be one of {_VALID_POLICIES})"
            )
        if not account_id.isdigit() or len(account_id) != 12:
            raise ValueError(f"account_id must be 12-digit AWS Account ID: {account_id}")

        now = _now_iso()
        item: dict[str, Any] = {
            "account_id": account_id,
            "description": description,
            "provisioning_policy": provisioning_policy,
            "allowed_role_patterns": list(allowed_role_patterns),
            "allow_iam_user": bool(allow_iam_user),
            "allow_instance_profile": bool(allow_instance_profile),
            "default_tenant_id": default_tenant_id,
            "default_credit": Decimal(default_credit) if default_credit is not None else None,
            "created_at": now,
            "updated_at": now,
            "created_by": created_by,
        }
        # None は DynamoDB で書けないので除去
        item = {k: v for k, v in item.items() if v is not None}
        self._table.put_item(Item=item)
        return item

    def update(
        self,
        *,
        account_id: str,
        description: Optional[str] = None,
        provisioning_policy: Optional[str] = None,
        allowed_role_patterns: Optional[list[str]] = None,
        allow_iam_user: Optional[bool] = None,
        allow_instance_profile: Optional[bool] = None,
        default_tenant_id: Optional[str] = None,
        default_credit: Optional[int] = None,
    ) -> dict[str, Any]:
        updates: list[str] = []
        values: dict[str, Any] = {":now": _now_iso()}
        names: dict[str, str] = {}
        if description is not None:
            updates.append("description = :d")
            values[":d"] = description
        if provisioning_policy is not None:
            if provisioning_policy not in _VALID_POLICIES:
                raise ValueError(f"invalid provisioning_policy: {provisioning_policy}")
            updates.append("provisioning_policy = :p")
            values[":p"] = provisioning_policy
        if allowed_role_patterns is not None:
            updates.append("allowed_role_patterns = :rp")
            values[":rp"] = list(allowed_role_patterns)
        if allow_iam_user is not None:
            updates.append("allow_iam_user = :iu")
            values[":iu"] = bool(allow_iam_user)
        if allow_instance_profile is not None:
            updates.append("allow_instance_profile = :ip")
            values[":ip"] = bool(allow_instance_profile)
        if default_tenant_id is not None:
            updates.append("default_tenant_id = :dt")
            values[":dt"] = default_tenant_id
        if default_credit is not None:
            updates.append("default_credit = :dc")
            values[":dc"] = Decimal(default_credit)
        if not updates:
            existing = self.get(account_id)
            if not existing:
                raise TrustedAccountNotFoundError(account_id)
            return existing
        updates.append("updated_at = :now")
        kwargs: dict[str, Any] = {
            "Key": {"account_id": account_id},
            "UpdateExpression": "SET " + ", ".join(updates),
            "ExpressionAttributeValues": values,
            "ConditionExpression": "attribute_exists(account_id)",
            "ReturnValues": "ALL_NEW",
        }
        if names:
            kwargs["ExpressionAttributeNames"] = names
        try:
            resp = self._table.update_item(**kwargs)
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise TrustedAccountNotFoundError(account_id)
            raise
        return resp.get("Attributes", {})

    def delete(self, account_id: str) -> None:
        self._table.delete_item(Key={"account_id": account_id})
