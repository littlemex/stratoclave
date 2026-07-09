"""TenantBudgets: dollar pool shared across all users of a tenant.

Layout:
    PK = tenant_id, SK = "BUDGET#<period>"  (period e.g. "2026-07")
    attributes:
        pool_limit_microusd    : hard ceiling for the period (int micro-USD)
        pool_reserved_microusd  : sum of in-flight reservations not yet settled
        pool_settled_microusd   : sum of settled (actual) spend
        status                  : "active" | "suspended"
        version                 : schema/version marker

Invariant enforced at reserve time (inside a DynamoDB transaction):

    pool_reserved + pool_settled + amount <= pool_limit  AND  status = active

Because the reservation is one conditional `ADD` inside a `TransactWriteItems`
that also debits the per-user balance, a tenant can never overspend its pool
even when many users race — losers get a TransactionCanceledException and are
translated to HTTP 402 with reason `tenant_pool_exhausted`.

A tenant with no BUDGET row for the period is *unlimited at the pool level*:
the pipeline then falls back to per-user token budgeting only, preserving the
pre-pool behaviour. Pool budgeting is opt-in per tenant/period.

All amounts are integer micro-USD; this module never introduces a float.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from .client import get_dynamodb_resource, tenant_budgets_table_name


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def budget_sk(period: str) -> str:
    return f"BUDGET#{period}"


def current_period() -> str:
    """Return the current billing period key (calendar month, UTC)."""
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


class TenantBudgetsRepository:
    def __init__(self, table_name: Optional[str] = None) -> None:
        self._name = table_name or tenant_budgets_table_name()
        self._table = get_dynamodb_resource().Table(self._name)

    @property
    def table_name(self) -> str:
        return self._name

    # ----- read -----
    def get(self, tenant_id: str, period: str) -> Optional[dict[str, Any]]:
        resp = self._table.get_item(
            Key={"tenant_id": tenant_id, "sk": budget_sk(period)}
        )
        return resp.get("Item")

    def pool_summary(self, tenant_id: str, period: str) -> Optional[dict[str, int]]:
        """Return the pool's limit/reserved/settled/remaining in micro-USD,
        or None when the tenant has no pool budget for the period.
        """
        item = self.get(tenant_id, period)
        if not item:
            return None
        limit = int(item.get("pool_limit_microusd", 0))
        reserved = int(item.get("pool_reserved_microusd", 0))
        settled = int(item.get("pool_settled_microusd", 0))
        return {
            "pool_limit_microusd": limit,
            "pool_reserved_microusd": reserved,
            "pool_settled_microusd": settled,
            "remaining_microusd": max(limit - reserved - settled, 0),
            "status": item.get("status", "active"),
        }

    # ----- write (admin) -----
    def set_pool_limit(
        self,
        *,
        tenant_id: str,
        period: str,
        pool_limit_microusd: int,
        status: str = "active",
    ) -> dict[str, Any]:
        """Create or update a tenant's pool limit for a period.

        Preserves the running `pool_reserved`/`pool_settled` counters if the
        row already exists (so changing the ceiling mid-period does not reset
        spend); initialises them to 0 on first creation.
        """
        existing = self.get(tenant_id, period)
        reserved = int(existing.get("pool_reserved_microusd", 0)) if existing else 0
        settled = int(existing.get("pool_settled_microusd", 0)) if existing else 0
        item = {
            "tenant_id": tenant_id,
            "sk": budget_sk(period),
            "pool_limit_microusd": Decimal(int(pool_limit_microusd)),
            "pool_reserved_microusd": Decimal(reserved),
            "pool_settled_microusd": Decimal(settled),
            "status": status,
            "version": "1",
            "updated_at": _now_iso(),
        }
        self._table.put_item(Item=item)
        return item

    # ----- transaction item builders -----
    # These return the Update fragments the pipeline composes into a single
    # TransactWriteItems alongside the per-user balance debit. Building them
    # here keeps the pool's ConditionExpression in one place.

    def reserve_txn_item(
        self,
        *,
        tenant_id: str,
        period: str,
        amount_microusd: int,
        expected_reserved: int,
        expected_settled: int,
    ) -> dict[str, Any]:
        """Transaction item that reserves `amount_microusd` from the pool.

        Uses the same snapshot optimistic-lock pattern as
        `UserTenantsRepository.reserve()`: the caller pre-reads
        `pool_reserved`/`pool_settled`, checks room in Python, and this item
        commits only if those two counters are unchanged (and status active).
        A concurrent reserve/settle changes a counter → the condition fails →
        the whole transaction cancels and the caller retries with a fresh
        snapshot.

        DynamoDB's ConditionExpression cannot do arithmetic across attributes
        portably, so the ceiling check lives in the caller; equality on the
        snapshot values makes the commit atomic and race-safe regardless.
        """
        return {
            "Update": {
                "TableName": self._name,
                "Key": {
                    "tenant_id": {"S": tenant_id},
                    "sk": {"S": budget_sk(period)},
                },
                "UpdateExpression": (
                    "ADD pool_reserved_microusd :amt SET updated_at = :now"
                ),
                "ConditionExpression": (
                    "attribute_exists(tenant_id) AND #st = :active AND "
                    "pool_reserved_microusd = :exp_reserved AND "
                    "pool_settled_microusd = :exp_settled"
                ),
                "ExpressionAttributeNames": {"#st": "status"},
                "ExpressionAttributeValues": {
                    ":amt": {"N": str(int(amount_microusd))},
                    ":exp_reserved": {"N": str(int(expected_reserved))},
                    ":exp_settled": {"N": str(int(expected_settled))},
                    ":active": {"S": "active"},
                    ":now": {"S": _now_iso()},
                },
            }
        }

    def settle_txn_item(
        self,
        *,
        tenant_id: str,
        period: str,
        reserved_microusd: int,
        actual_microusd: int,
    ) -> dict[str, Any]:
        """Transaction item that settles a reservation against actual spend.

        Moves `reserved` out of `pool_reserved` and `actual` into
        `pool_settled` in one update. No ConditionExpression: settlement must
        never fail a live request (a refund/top-up cannot exceed the pool by
        construction because the original reserve already fit).
        """
        delta_reserved = -int(reserved_microusd)
        return {
            "Update": {
                "TableName": self._name,
                "Key": {
                    "tenant_id": {"S": tenant_id},
                    "sk": {"S": budget_sk(period)},
                },
                "UpdateExpression": (
                    "ADD pool_reserved_microusd :dr, "
                    "pool_settled_microusd :actual SET updated_at = :now"
                ),
                "ExpressionAttributeValues": {
                    ":dr": {"N": str(delta_reserved)},
                    ":actual": {"N": str(int(actual_microusd))},
                    ":now": {"S": _now_iso()},
                },
            }
        }
