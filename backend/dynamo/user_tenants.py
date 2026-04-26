"""UserTenants テーブル (クレジット残高管理 + Phase 2 status/credit_source/switch).

Phase 2 (v2.1) 変更:
- `status` フィールドを追加: "active" | "archived"
- `credit_source` フィールドを追加: "user_override" | "tenant_default" | "global_default"
- `get()` は `status == "active"` のみ返す (archived は履歴として残す)
- `ensure()` は archived レコードを active に昇格させる (A→B→A 再所属対応)
- `switch_tenant()` で TransactWriteItems による原子的切替 (+ Cognito Saga は呼び出し側が担当)
- `deduct()` は `status = "active"` ConditionExpression を追加し、切替中の旧 tenant 取り違えを防止
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from botocore.exceptions import ClientError

from .client import get_dynamodb_resource, user_tenants_table_name


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CreditExhaustedError(Exception):
    """クレジット残高が不足している場合に raise."""


class UserTenantsRepository:
    DEFAULT_CREDIT = 100_000  # 最終フォールバック (Tenant 未指定 + 個別未指定)

    def __init__(self, table_name: Optional[str] = None) -> None:
        self._table = get_dynamodb_resource().Table(
            table_name or user_tenants_table_name()
        )

    # ----- read -----
    def get(self, user_id: str, tenant_id: str) -> Optional[dict[str, Any]]:
        """active なレコードのみ返す (archived は get_including_archived() で)."""
        resp = self._table.get_item(Key={"user_id": user_id, "tenant_id": tenant_id})
        item = resp.get("Item")
        if not item:
            return None
        # 既存データとの互換: status フィールドが無いレコードは active 扱い
        if item.get("status", "active") != "active":
            return None
        return item

    def get_including_archived(
        self, user_id: str, tenant_id: str
    ) -> Optional[dict[str, Any]]:
        resp = self._table.get_item(Key={"user_id": user_id, "tenant_id": tenant_id})
        return resp.get("Item")

    # ----- write -----
    def ensure(
        self,
        *,
        user_id: str,
        tenant_id: str,
        role: str = "user",
        total_credit: Optional[int] = None,
    ) -> dict[str, Any]:
        """存在しなければ作成、archived なら active に昇格、active ならそのまま返す。

        total_credit の優先順位 (§5.1):
          1. user_total_credit 指定あり → user_override
          2. Tenants.default_credit → tenant_default
          3. UserTenantsRepository.DEFAULT_CREDIT → global_default
        """
        existing = self.get_including_archived(user_id, tenant_id)

        credit: int
        credit_source: str
        if total_credit is not None:
            credit = int(total_credit)
            credit_source = "user_override"
        else:
            credit, credit_source = self._resolve_tenant_default(tenant_id)

        now = _now_iso()

        if existing is None:
            # 新規
            item: dict[str, Any] = {
                "user_id": user_id,
                "tenant_id": tenant_id,
                "role": role,
                "status": "active",
                "total_credit": Decimal(credit),
                "credit_used": Decimal(0),
                "credit_source": credit_source,
                "created_at": now,
                "updated_at": now,
            }
            self._table.put_item(Item=item)
            return item

        if existing.get("status") == "archived":
            # archived を active に昇格、Credit をリセット
            resp = self._table.update_item(
                Key={"user_id": user_id, "tenant_id": tenant_id},
                UpdateExpression=(
                    "SET #s = :active, role = :role, total_credit = :total, "
                    "credit_used = :zero, credit_source = :src, updated_at = :now"
                ),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":active": "active",
                    ":role": role,
                    ":total": Decimal(credit),
                    ":zero": Decimal(0),
                    ":src": credit_source,
                    ":now": now,
                },
                ReturnValues="ALL_NEW",
            )
            return resp.get("Attributes", {})

        # active のまま、現状を返す (Credit は変更しない)
        return existing

    def _resolve_tenant_default(self, tenant_id: str) -> tuple[int, str]:
        """Tenants.default_credit を参照、無ければグローバルフォールバック."""
        try:
            from .tenants import TenantsRepository
            tenant = TenantsRepository().get(tenant_id)
        except Exception:
            tenant = None
        if tenant:
            default_credit = tenant.get("default_credit")
            if default_credit is not None:
                return int(default_credit), "tenant_default"
        return self.DEFAULT_CREDIT, "global_default"

    # ----- credit operations -----
    def remaining_credit(self, user_id: str, tenant_id: str) -> int:
        item = self.get(user_id, tenant_id)
        if not item:
            return 0
        total = int(item.get("total_credit", 0))
        used = int(item.get("credit_used", 0))
        return max(total - used, 0)

    def deduct(self, *, user_id: str, tenant_id: str, tokens: int) -> int:
        """クレジットを減算。残高不足 / 切替中は CreditExhaustedError.

        v2.1 変更:
          - ConditionExpression に `status = "active"` を追加
          - 旧 tenant が archived になっている場合は即 CreditExhaustedError
        """
        if tokens <= 0:
            return self.remaining_credit(user_id, tenant_id)

        max_retries = 3
        for attempt in range(max_retries):
            item = self.get(user_id, tenant_id)
            if not item:
                raise CreditExhaustedError(
                    f"Active UserTenant not found for user_id={user_id} tenant_id={tenant_id}"
                )
            total = int(item.get("total_credit", 0))
            used = int(item.get("credit_used", 0))
            new_used = used + tokens
            if new_used > total:
                raise CreditExhaustedError(
                    f"Credit exhausted for user_id={user_id} tenant_id={tenant_id} "
                    f"(total={total}, used={used}, requested={tokens})"
                )
            try:
                resp = self._table.update_item(
                    Key={"user_id": user_id, "tenant_id": tenant_id},
                    UpdateExpression="SET credit_used = :new_used, updated_at = :now",
                    ConditionExpression=(
                        "credit_used = :old_used AND "
                        "(attribute_not_exists(#s) OR #s = :active)"
                    ),
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":new_used": Decimal(new_used),
                        ":old_used": Decimal(used),
                        ":active": "active",
                        ":now": _now_iso(),
                    },
                    ReturnValues="ALL_NEW",
                )
            except ClientError as e:
                if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                    if attempt < max_retries - 1:
                        continue
                    raise CreditExhaustedError(
                        f"Concurrent credit update conflict (or tenant switched) "
                        f"for user_id={user_id} tenant_id={tenant_id}"
                    )
                raise
            attrs = resp.get("Attributes", {})
            total_new = int(attrs.get("total_credit", 0))
            used_new = int(attrs.get("credit_used", 0))
            return max(total_new - used_new, 0)

        raise CreditExhaustedError("Unexpected credit deduction failure")

    def credit_summary(self, user_id: str, tenant_id: str) -> dict[str, int]:
        item = self.get(user_id, tenant_id)
        if not item:
            return {"total_credit": 0, "credit_used": 0, "remaining_credit": 0}
        total = int(item.get("total_credit", 0))
        used = int(item.get("credit_used", 0))
        return {
            "total_credit": total,
            "credit_used": used,
            "remaining_credit": max(total - used, 0),
        }

    def overwrite_credit(
        self, *, user_id: str, tenant_id: str, total_credit: int, reset_used: bool = False
    ) -> dict[str, Any]:
        """Admin による Credit 上書き (user_override としてマーク)."""
        update_expr_parts = [
            "total_credit = :total",
            "credit_source = :src",
            "updated_at = :now",
        ]
        values: dict[str, Any] = {
            ":total": Decimal(total_credit),
            ":src": "user_override",
            ":now": _now_iso(),
            ":active": "active",
        }
        if reset_used:
            update_expr_parts.append("credit_used = :zero")
            values[":zero"] = Decimal(0)

        try:
            resp = self._table.update_item(
                Key={"user_id": user_id, "tenant_id": tenant_id},
                UpdateExpression="SET " + ", ".join(update_expr_parts),
                ExpressionAttributeNames={"#s": "status"},
                ConditionExpression="attribute_exists(user_id) AND (attribute_not_exists(#s) OR #s = :active)",
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise CreditExhaustedError(
                    f"UserTenant not active for user_id={user_id} tenant_id={tenant_id}"
                )
            raise
        return resp.get("Attributes", {})

    # ----- tenant switch -----
    def switch_tenant(
        self,
        *,
        user_id: str,
        old_tenant_id: str,
        new_tenant_id: str,
        new_role: str = "user",
        new_total_credit: Optional[int] = None,
    ) -> dict[str, Any]:
        """User を old_tenant から new_tenant に原子的に切替。

        TransactWriteItems で実行 (v2.1 §4.3):
          1. UserTenants[user_id, old_tenant_id] status=archived (active 限定)
          2. UserTenants[user_id, new_tenant_id] を active で put (存在なければ)
          3. Users[user_id] org_id を更新

        呼び出し側は本メソッド成功後に:
          - Cognito AdminUpdateUserAttributes(custom:org_id=new_tenant_id)
          - Cognito AdminUserGlobalSignOut(sub)  # JWT 即時失効
        を実行する (Saga パターン、失敗時は Admin に再実行を促す)。

        Returns:
          新 UserTenants レコード (dict)
        """
        from .client import users_table_name

        if old_tenant_id == new_tenant_id:
            raise ValueError("old_tenant_id == new_tenant_id")

        # 新 Tenant の default_credit 解決
        if new_total_credit is not None:
            credit = int(new_total_credit)
            credit_source = "user_override"
        else:
            credit, credit_source = self._resolve_tenant_default(new_tenant_id)

        now = _now_iso()
        users_tbl = users_table_name()
        user_tenants_tbl = self._table.name
        # low-level client for TransactWriteItems (resource.meta.client でも可だが、
        # 明示的に新規 client を作ると ResourceSerialization の副作用を避けられる)
        import os as _os
        import boto3 as _boto3
        region = _os.getenv("AWS_REGION", "us-east-1")
        dynamo = _boto3.client("dynamodb", region_name=region)

        transact_items: list[dict[str, Any]] = [
            # (1) 旧 UserTenants を archived に
            {
                "Update": {
                    "TableName": user_tenants_tbl,
                    "Key": {
                        "user_id": {"S": user_id},
                        "tenant_id": {"S": old_tenant_id},
                    },
                    "UpdateExpression": "SET #s = :archived, updated_at = :now",
                    "ConditionExpression": "attribute_exists(user_id) AND (#s = :active OR attribute_not_exists(#s))",
                    "ExpressionAttributeNames": {"#s": "status"},
                    "ExpressionAttributeValues": {
                        ":archived": {"S": "archived"},
                        ":active": {"S": "active"},
                        ":now": {"S": now},
                    },
                }
            },
            # (2) 新 UserTenants を active で put (既存 archived を上書き、active を上書きは阻止)
            {
                "Put": {
                    "TableName": user_tenants_tbl,
                    "Item": {
                        "user_id": {"S": user_id},
                        "tenant_id": {"S": new_tenant_id},
                        "role": {"S": new_role},
                        "status": {"S": "active"},
                        "total_credit": {"N": str(credit)},
                        "credit_used": {"N": "0"},
                        "credit_source": {"S": credit_source},
                        "created_at": {"S": now},
                        "updated_at": {"S": now},
                    },
                    "ConditionExpression": "attribute_not_exists(user_id) OR #s = :archived",
                    "ExpressionAttributeNames": {"#s": "status"},
                    "ExpressionAttributeValues": {
                        ":archived": {"S": "archived"},
                    },
                }
            },
            # (3) Users.org_id を更新
            {
                "Update": {
                    "TableName": users_tbl,
                    "Key": {
                        "user_id": {"S": user_id},
                        "sk": {"S": "PROFILE"},
                    },
                    "UpdateExpression": "SET org_id = :new_org, updated_at = :now",
                    "ConditionExpression": "attribute_exists(user_id)",
                    "ExpressionAttributeValues": {
                        ":new_org": {"S": new_tenant_id},
                        ":now": {"S": now},
                    },
                }
            },
        ]

        try:
            dynamo.transact_write_items(TransactItems=transact_items)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "TransactionCanceledException":
                # ConditionCheckFailed 等。呼び出し側は 409 で再試行を促す
                reasons = e.response.get("CancellationReasons", [])
                raise ValueError(
                    f"Tenant switch transaction failed: reasons={reasons}"
                )
            raise

        # 返却用: 新 UserTenants レコードを取得
        resp = self._table.get_item(
            Key={"user_id": user_id, "tenant_id": new_tenant_id}
        )
        return resp.get("Item", {})
