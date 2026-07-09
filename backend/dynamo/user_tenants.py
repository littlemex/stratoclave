"""UserTenants table (credit balance management + Phase 2 status/credit_source/switch).

Phase 2 (v2.1) changes:
- Added `status` field: "active" | "archived"
- Added `credit_source` field: "user_override" | "tenant_default" | "global_default"
- `get()` returns only rows where `status == "active"` (archived rows are kept as history)
- `ensure()` promotes archived rows back to active (supports A→B→A re-membership)
- `switch_tenant()` performs atomic tenant switching via TransactWriteItems
  (Cognito Saga steps are handled by the caller)

Phase 3 changes (credit reservation):
- Removed `deduct()`; replaced with `reserve()` / `refund()` pair
- `reserve()`: atomically reserves max_tokens worth of credit **before** calling Bedrock.
    Because ConditionExpression supports only comparisons, uses snapshot consistency:
    checks `credit_used <= max_allowed_used` AND `total_credit = expected_total`.
    If total_credit was changed by an admin concurrently, ConditionCheckFailed
    triggers a re-read and retry (handles concurrent admin overwrites).
- `refund()`: returns the unused portion when actual consumption is less than reserved.
    Guards against underflow with `credit_used >= tokens`.
- Both streaming and non-streaming paths follow the pattern:
  reserve upfront → call Bedrock → refund the difference. Silent pass is prohibited.
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
    """Raised when the credit balance is insufficient."""


class UserTenantsRepository:
    DEFAULT_CREDIT = 100_000  # Last-resort fallback (no tenant default and no individual override)

    def __init__(self, table_name: Optional[str] = None) -> None:
        self._table = get_dynamodb_resource().Table(
            table_name or user_tenants_table_name()
        )

    # ----- read -----
    def get(self, user_id: str, tenant_id: str) -> Optional[dict[str, Any]]:
        """Return only active records (use get_including_archived() for archived rows)."""
        resp = self._table.get_item(Key={"user_id": user_id, "tenant_id": tenant_id})
        item = resp.get("Item")
        if not item:
            return None
        # Backwards compatibility: rows without a status field are treated as active.
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
            # New row.
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

        # Already active — return as-is (credit is not modified).
        return existing

    def _resolve_tenant_default(self, tenant_id: str) -> tuple[int, str]:
        """Look up Tenants.default_credit; fall back to the global default if absent."""
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
        """Atomically reserve credit (pessimistic reservation).

        Call this **before** invoking Bedrock to pre-claim max_tokens worth of credit.
        Call refund() afterward to return the unused portion.

        Atomicity:
          ConditionExpression does not support arithmetic, so we use the pattern:
          get → update with ConditionExpression (credit_used <= max_allowed_used AND
          total_credit = expected_total).
          If total_credit changes due to concurrent requests or an admin overwrite,
          ConditionCheckFailed triggers a re-read and retry (up to _RESERVE_MAX_RETRIES times).

          - Concurrent reserves under the same total_credit: ConditionExpression ensures
            credit_used never exceeds total_credit; the first request to breach the cap fails.
          - Concurrent admin overwrite_credit: the reserve side retries on CCF; the overwrite
            succeeds if the row is still active.
          - Archived tenant: excluded by (attribute_not_exists(#s) OR #s = :active).

        Args:
          tokens: Number of tokens to reserve (> 0).

        Raises:
          CreditExhaustedError: Insufficient balance, or retry limit reached due to
            concurrent updates. Also raised when the tenant does not exist or is archived.

        Returns:
          Remaining balance after reservation (total_credit - credit_used).
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
                # Pre-check already shows exhaustion — return immediately even if
                # another concurrent request filled the gap (retry is pointless).
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
                    # Snapshot is stale (another reserve or admin overwrite) — re-read and retry.
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
        """Return reserved credit (inverse of reserve).

        Atomically returns the unused portion of the reservation (reserved minus
        actual consumption). ConditionExpression guards against credit_used underflow.
        Refund is permitted even on archived tenants to avoid overcharging.

        Args:
          tokens: Number of tokens to return (> 0).

        Returns:
          Remaining balance after the refund.
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
                # Underflow guard: credit_used < tokens → clamp to 0.
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

    # ----- transaction item builders (for atomic pool + per-user reserve) -----
    # These mirror reserve()/refund() but emit low-level TransactWriteItems
    # fragments so the pipeline can debit the per-user balance and the tenant
    # pool in a single all-or-nothing transaction. `reserve()` above stays the
    # single-table fast path used when a tenant has no pool budget.

    def reserve_txn_item(
        self, *, user_id: str, tenant_id: str, tokens: int, expected_total: int
    ) -> dict[str, Any]:
        """Transaction item reserving `tokens` against the per-user balance.

        `expected_total` is the caller's snapshot of `total_credit`; the same
        optimistic precondition as reserve() (unchanged total, room under it,
        active status) is enforced, so a lost race cancels the transaction and
        the caller retries with a fresh snapshot.
        """
        max_allowed_used = int(expected_total) - int(tokens)
        return {
            "Update": {
                "TableName": self._table.name,
                "Key": {
                    "user_id": {"S": user_id},
                    "tenant_id": {"S": tenant_id},
                },
                "UpdateExpression": "ADD credit_used :tokens SET updated_at = :now",
                "ConditionExpression": (
                    "credit_used <= :max_allowed_used AND "
                    "total_credit = :expected_total AND "
                    "(attribute_not_exists(#s) OR #s = :active)"
                ),
                "ExpressionAttributeNames": {"#s": "status"},
                "ExpressionAttributeValues": {
                    ":tokens": {"N": str(int(tokens))},
                    ":max_allowed_used": {"N": str(max_allowed_used)},
                    ":expected_total": {"N": str(int(expected_total))},
                    ":active": {"S": "active"},
                    ":now": {"S": _now_iso()},
                },
            }
        }

    def settle_txn_item(
        self, *, user_id: str, tenant_id: str, delta_tokens: int
    ) -> dict[str, Any]:
        """Transaction item adjusting per-user `credit_used` by `delta_tokens`.

        Positive delta tops up (actual > reservation), negative refunds. No
        ConditionExpression: settlement of a live request must not fail, and
        the amount is bounded by the prior reservation by construction.
        """
        return {
            "Update": {
                "TableName": self._table.name,
                "Key": {
                    "user_id": {"S": user_id},
                    "tenant_id": {"S": tenant_id},
                },
                "UpdateExpression": "ADD credit_used :delta SET updated_at = :now",
                "ExpressionAttributeValues": {
                    ":delta": {"N": str(int(delta_tokens))},
                    ":now": {"S": _now_iso()},
                },
            }
        }

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
        """Admin credit overwrite (marks the record as user_override)."""
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
        """Atomically move a user from old_tenant to new_tenant.

        Executed as a TransactWriteItems operation (v2.1 §4.3):
          1. Set UserTenants[user_id, old_tenant_id] status=archived (active rows only).
          2. Put UserTenants[user_id, new_tenant_id] as active (if not already present).
          3. Update Users[user_id] org_id.

        After this method returns successfully, the caller must:
          - Cognito AdminUpdateUserAttributes(custom:org_id=new_tenant_id)
          - Cognito AdminUserGlobalSignOut(sub)  # immediately invalidate existing JWTs
        These steps follow the Saga pattern; on failure the admin is responsible for
        retrying.

        Returns:
          The new UserTenants record (dict).
        """
        from .client import users_table_name

        if old_tenant_id == new_tenant_id:
            raise ValueError("old_tenant_id == new_tenant_id")

        # Resolve default_credit for the new tenant.
        if new_total_credit is not None:
            credit = int(new_total_credit)
            credit_source = "user_override"
        else:
            credit, credit_source = self._resolve_tenant_default(new_tenant_id)

        now = _now_iso()
        users_tbl = users_table_name()
        user_tenants_tbl = self._table.name
        # Use a low-level DynamoDB client for TransactWriteItems.
        # (resource.meta.client also works, but a fresh client avoids ResourceSerialization side effects.)
        import os as _os
        import boto3 as _boto3
        region = _os.getenv("AWS_REGION", "us-east-1")
        dynamo = _boto3.client("dynamodb", region_name=region)

        transact_items: list[dict[str, Any]] = [
            # (1) Archive the old UserTenants row.
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
            # (2) Put the new UserTenants row as active (overwrites an existing archived row; blocks overwriting an active row).
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
            # (3) Update Users.org_id.
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
                # e.g. ConditionCheckFailed — caller should surface a 409 to prompt a retry.
                reasons = e.response.get("CancellationReasons", [])
                raise ValueError(
                    f"Tenant switch transaction failed: reasons={reasons}"
                )
            raise

        # Fetch the newly written UserTenants record to return.
        resp = self._table.get_item(
            Key={"user_id": user_id, "tenant_id": new_tenant_id}
        )
        return resp.get("Item", {})
