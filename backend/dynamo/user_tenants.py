"""UserTenants テーブル (クレジット残高管理 + Phase 2 status/credit_source/switch).

Phase 2 (v2.1) 変更:
- `status` フィールドを追加: "active" | "archived"
- `credit_source` フィールドを追加: "user_override" | "tenant_default" | "global_default"
- `get()` は `status == "active"` のみ返す (archived は履歴として残す)
- `ensure()` は archived レコードを active に昇格させる (A→B→A 再所属対応)
- `switch_tenant()` で TransactWriteItems による原子的切替 (+ Cognito Saga は呼び出し側が担当)

Phase 3 変更 (credit reservation):
- 旧 `deduct()` を廃止、代わりに `reserve()` / `refund()` の 2 つに分解
- `reserve()`: Bedrock 呼び出し**前**に atomic に max_tokens 相当を確保
    ConditionExpression は比較演算のみサポートのため、
    `credit_used <= max_allowed_used` かつ `total_credit = expected_total` を
    スナップショット整合でチェック。total_credit が admin overwrite で変動した
    場合は ConditionCheckFailed → 再読込して retry (concurrent admin change 対応)。
- `refund()`: 実消費が reservation より少ない分を戻す。
    `credit_used >= tokens` で underflow ガード。
- `_stream_messages` / non-stream 共に「事前 reserve → Bedrock → 実消費で差額 refund」
  のパターン。silent pass は禁止。
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
        allow_resurrection: bool = False,
    ) -> dict[str, Any]:
        """Create if missing, return as-is if already active.

        Archived rows are only flipped back to `active` when the caller
        opts in with `allow_resurrection=True`. Plain identity probes
        such as `/api/mvp/me` leave archived rows archived; only
        deliberate provisioning (admin re-add, SSO re-registration)
        should revive a membership. See P0-1 in SECURITY_REVIEW_2026-04
        for the incident that motivated this gate.

        total_credit precedence (§5.1):
          1. explicit `total_credit` argument  → user_override
          2. Tenants.default_credit            → tenant_default
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
            # P0-1 (2026-04 security review): silently flipping an
            # archived UserTenants row back to `active` meant that a
            # user whose tenant an admin had just archived could resume
            # full credit simply by calling `/api/mvp/me` once. The row
            # would be revived with `credit_used=0`, and the next
            # `/v1/messages` would happily spend against it.
            #
            # Resurrection is now gated behind an explicit opt-in used
            # only by intentional provisioning paths (admin re-adding a
            # member, SSO re-registration). Implicit identity probes
            # must not have this side effect.
            #
            # Belt-and-braces: even with `allow_resurrection=True`, if
            # the parent `Tenants` record is archived, refuse. Admins
            # archive a tenant deliberately; reviving membership into
            # an archived tenant is almost certainly a mistake.
            if not allow_resurrection:
                return existing
            from .tenants import TenantsRepository

            tenant_rec = TenantsRepository().get(tenant_id)
            if tenant_rec and tenant_rec.get("status") == "archived":
                return existing
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

    _RESERVE_MAX_RETRIES = 5

    def reserve(self, *, user_id: str, tenant_id: str, tokens: int) -> int:
        """原子的にクレジットを確保 (pessimistic reservation).

        Bedrock 呼び出し**前**に呼び、max_tokens 相当を先取りする。
        呼び出し完了後に refund() で差額を戻す。

        アトミック性:
          ConditionExpression は算術演算をサポートしないため、
          `get → ConditionExpression (credit_used <= max_allowed_used AND
          total_credit = expected_total) で update` のパターン。
          並列リクエスト/admin overwrite で total_credit が変動した場合は
          ConditionCheckFailed となり、再読込して retry する (最大 _RESERVE_MAX_RETRIES 回)。

          - 同一 total_credit 下での並列 reserve: ConditionExpression により
            合計 credit_used が total_credit を超えた瞬間から以降の reserve は失敗する。
            超過は発生しない。
          - admin の overwrite_credit と同時実行: reserve 側が CCF でリトライ、
            overwrite が active なら update_item は成功。
          - Tenant archived: (attribute_not_exists(#s) OR #s = :active) で排除。

        Args:
          tokens: 確保する token 数 (> 0)

        Raises:
          CreditExhaustedError: 残高不足、または concurrent update で retry 上限到達。
            Tenant が存在しない / archived の場合も同じ例外。

        Returns:
          確保後の残高 (remaining = total_credit - credit_used)
        """
        if tokens <= 0:
            return self.remaining_credit(user_id, tenant_id)

        last_total: Optional[int] = None
        last_used: Optional[int] = None
        for attempt in range(self._RESERVE_MAX_RETRIES):
            item = self.get(user_id, tenant_id)
            if not item:
                raise CreditExhaustedError(
                    f"Active UserTenant not found for user_id={user_id} "
                    f"tenant_id={tenant_id}"
                )
            total = int(item.get("total_credit", 0))
            used = int(item.get("credit_used", 0))
            last_total, last_used = total, used
            max_allowed_used = total - tokens

            if used > max_allowed_used:
                # 事前チェックで既に超過 → 並列の他リクエストが埋めた場合でも
                # ここで即座に残高不足を返す (retry しても意味がない)
                raise CreditExhaustedError(
                    f"Insufficient credit for user_id={user_id} tenant_id={tenant_id} "
                    f"(total={total}, used={used}, requested={tokens})"
                )

            try:
                resp = self._table.update_item(
                    Key={"user_id": user_id, "tenant_id": tenant_id},
                    UpdateExpression=(
                        "ADD credit_used :tokens SET updated_at = :now"
                    ),
                    ConditionExpression=(
                        "credit_used <= :max_allowed_used AND "
                        "total_credit = :expected_total AND "
                        "(attribute_not_exists(#s) OR #s = :active)"
                    ),
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":tokens": Decimal(tokens),
                        ":max_allowed_used": Decimal(max_allowed_used),
                        ":expected_total": Decimal(total),
                        ":active": "active",
                        ":now": _now_iso(),
                    },
                    ReturnValues="ALL_NEW",
                )
            except ClientError as e:
                if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                    # snapshot が古い (他の reserve / admin overwrite) → 再読込で retry
                    continue
                raise

            attrs = resp.get("Attributes", {})
            total_new = int(attrs.get("total_credit", 0))
            used_new = int(attrs.get("credit_used", 0))
            return max(total_new - used_new, 0)

        raise CreditExhaustedError(
            f"Credit reservation failed after {self._RESERVE_MAX_RETRIES} retries "
            f"for user_id={user_id} tenant_id={tenant_id} "
            f"(last_total={last_total}, last_used={last_used}, requested={tokens})"
        )

    def refund(self, *, user_id: str, tenant_id: str, tokens: int) -> int:
        """確保済みクレジットを戻す (reserve の逆操作).

        reserve で確保した額のうち実消費を差し引いた余剰を atomic に戻す。
        credit_used がアンダーフローしないよう ConditionExpression で守る。
        Tenant が archived でも refund は許可する (over-charge を避けるため)。

        Args:
          tokens: 戻す token 数 (> 0)

        Returns:
          refund 後の残高
        """
        if tokens <= 0:
            return self.remaining_credit(user_id, tenant_id)

        try:
            resp = self._table.update_item(
                Key={"user_id": user_id, "tenant_id": tenant_id},
                UpdateExpression="ADD credit_used :neg_tokens SET updated_at = :now",
                ConditionExpression="credit_used >= :tokens",
                ExpressionAttributeValues={
                    ":neg_tokens": Decimal(-tokens),
                    ":tokens": Decimal(tokens),
                    ":now": _now_iso(),
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # underflow ガード: credit_used < tokens → 0 にクランプ
                item = self.get_including_archived(user_id, tenant_id)
                if item:
                    return max(
                        int(item.get("total_credit", 0))
                        - int(item.get("credit_used", 0)),
                        0,
                    )
                return 0
            raise

        attrs = resp.get("Attributes", {})
        total_new = int(attrs.get("total_credit", 0))
        used_new = int(attrs.get("credit_used", 0))
        return max(total_new - used_new, 0)

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
