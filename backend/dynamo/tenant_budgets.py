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

from boto3.dynamodb.conditions import Key

from .client import get_dynamodb_resource, tenant_budgets_table_name


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def budget_sk(period: str) -> str:
    return f"BUDGET#{period}"


def hold_sk_prefix(period: str) -> str:
    """SK prefix that groups a period's per-reservation hold items under the
    tenant's partition (so they can be range-Queried)."""
    return f"HOLD#{period}#"


# Width of the zero-padded epoch-seconds field embedded in a hold's SK. Ten
# digits covers all epochs through the year 2286, so lexical SK order == expiry
# order for the lifetime of this system. The reaper relies on that ordering: it
# range-scans holds whose embedded expiry is <= now, which lets DynamoDB's Limit
# bound the scan *by expiry* (oldest orphans first) instead of by arbitrary key
# order — the fix for the "orphan buried behind live holds, never swept" leak.
_EXPIRES_WIDTH = 10


def hold_sk(period: str, expires_at_epoch: int, hold_id: str) -> str:
    """Build a hold's sort key with the expiry embedded so SK order is expiry
    order: ``HOLD#<period>#<expires_at:010d>#<hold_id>``."""
    return f"{hold_sk_prefix(period)}{int(expires_at_epoch):0{_EXPIRES_WIDTH}d}#{hold_id}"


def hold_sk_expiry_ceiling(period: str, now_epoch: int) -> str:
    """Upper bound (inclusive) for a range scan of holds expired at/-before
    `now_epoch`: every SK whose embedded expiry is <= now sorts <= this string.

    The trailing high sentinel (``#￿``) makes the bound inclusive of the
    whole `now_epoch` second regardless of the hold_id suffix.
    """
    return f"{hold_sk_prefix(period)}{int(now_epoch):0{_EXPIRES_WIDTH}d}#￿"


def current_period() -> str:
    """Return the current billing period key (calendar month, UTC)."""
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def previous_period(period: str) -> str:
    """Return the calendar month immediately before `period` ("2026-07"->"2026-06").

    The reaper sweeps this alongside the current period so a hold orphaned by a
    crash in the final moments of a month is still reclaimed after the boundary
    rolls over (otherwise last month's `pool_reserved` would stay inflated and
    the hold row would linger forever, since native TTL is intentionally unused).
    """
    year, month = (int(x) for x in period.split("-"))
    if month == 1:
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{month - 1:02d}"


class TenantBudgetsRepository:
    def __init__(self, table_name: Optional[str] = None) -> None:
        self._name = table_name or tenant_budgets_table_name()
        self._table = get_dynamodb_resource().Table(self._name)

    @property
    def table_name(self) -> str:
        return self._name

    # ----- read -----
    def get(
        self, tenant_id: str, period: str, *, consistent_read: bool = False
    ) -> Optional[dict[str, Any]]:
        """Read a tenant's pool row for a period.

        `consistent_read=True` forces a strongly-consistent GetItem, used by the
        reserve loop so the optimistic snapshot lock is taken against the
        current counters (a stale read makes the equality condition fail
        forever). Admin/read-only callers keep the cheaper eventually-consistent
        default.
        """
        resp = self._table.get_item(
            Key={"tenant_id": tenant_id, "sk": budget_sk(period)},
            ConsistentRead=consistent_read,
        )
        return resp.get("Item")

    def get_hold(
        self, *, tenant_id: str, sk: str, consistent_read: bool = True
    ) -> Optional[dict[str, Any]]:
        """Strongly-consistent read of one hold row by exact `sk` (or None).

        Used by the external-authorize rehydrate path to confirm the hold still
        exists (not yet captured/voided/reclaimed) and read its `amount_microusd`.
        ConsistentRead by default so a capture immediately after authorize sees
        its own just-written hold."""
        resp = self._table.get_item(
            Key={"tenant_id": tenant_id, "sk": sk},
            ConsistentRead=consistent_read,
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

    # ----- hold items (orphan-reservation reaper) -----
    # Every in-flight reservation writes a sibling HOLD row in the same
    # transaction as the aggregate `pool_reserved += cost`. The HOLD records how
    # much *this specific request* is holding and when the hold expires. settle
    # and release delete the HOLD in the same transaction that decrements the
    # aggregate, so a HOLD outlives its reservation only when the process died
    # between reserve and settle (task kill / OOM / drain). The lazy sweep then
    # reclaims those orphans: `pool_reserved -= amount` plus a conditional
    # Delete(hold), the condition making the reclaim idempotent (a HOLD is
    # reclaimed at most once, so the aggregate can never be double-subtracted or
    # driven negative). This is the ONLY reaper — native DynamoDB TTL is
    # deliberately NOT used on HOLDs, because a TTL delete would drop the row
    # without decrementing the aggregate, converting a transient leak into a
    # permanent one.

    def hold_put_txn_item(
        self,
        *,
        tenant_id: str,
        period: str,
        hold_id: str,
        amount_microusd: int,
        expires_at_epoch: int,
    ) -> dict[str, Any]:
        """Transaction item that records a per-reservation hold.

        Written in the SAME TransactWriteItems as the aggregate reserve, so a
        hold exists iff its share of `pool_reserved_microusd` is outstanding.
        The SK embeds the expiry (see `hold_sk`) so the reaper can range-scan by
        expiry; `attribute_not_exists(sk)` guards against a hold_id collision.
        """
        return {
            "Put": {
                "TableName": self._name,
                "Item": {
                    "tenant_id": {"S": tenant_id},
                    "sk": {"S": hold_sk(period, expires_at_epoch, hold_id)},
                    "hold_id": {"S": hold_id},
                    "period": {"S": period},
                    "amount_microusd": {"N": str(int(amount_microusd))},
                    "expires_at": {"N": str(int(expires_at_epoch))},
                    "created_at": {"S": _now_iso()},
                },
                "ConditionExpression": "attribute_not_exists(sk)",
            }
        }

    def hold_delete_txn_item(
        self, *, tenant_id: str, sk: str, require_exists: bool = True
    ) -> dict[str, Any]:
        """Transaction item that deletes a hold by its exact `sk`.

        Composed alongside the aggregate settle/release so the hold and its
        aggregate share disappear together. With `require_exists=True` (the
        default) the Delete is gated on `attribute_exists(sk)`: this is the
        latch that keeps the paired aggregate decrement from applying twice. If
        the reaper already reclaimed this hold (and already returned its
        reserved share), the condition fails, the whole transaction cancels, and
        the caller falls back to recording spend WITHOUT decrementing reserved
        again — the fix for the settle/reclaim double-subtract.
        """
        item: dict[str, Any] = {
            "Delete": {
                "TableName": self._name,
                "Key": {
                    "tenant_id": {"S": tenant_id},
                    "sk": {"S": sk},
                },
            }
        }
        if require_exists:
            item["Delete"]["ConditionExpression"] = "attribute_exists(sk)"
        return item

    def reclaim_hold_txn_item(
        self, *, tenant_id: str, sk: str
    ) -> dict[str, Any]:
        """Transaction item that deletes an expired hold by exact `sk` *only if
        it still exists*, so the paired aggregate decrement happens at most once.

        The sweep composes this Delete with an aggregate
        `pool_reserved_microusd -= amount`. The `attribute_exists(sk)` condition
        is the idempotency latch: if a concurrent sweep or a late settle already
        removed the hold, the whole transaction cancels and no double-subtract
        occurs.
        """
        return {
            "Delete": {
                "TableName": self._name,
                "Key": {
                    "tenant_id": {"S": tenant_id},
                    "sk": {"S": sk},
                },
                "ConditionExpression": "attribute_exists(sk)",
            }
        }

    def query_expired_holds(
        self, *, tenant_id: str, period: str, now_epoch: int, limit: int = 25
    ) -> list[dict[str, Any]]:
        """Return up to `limit` holds for the period whose embedded expiry has
        passed, **oldest-expiry first**. Strongly consistent so the sweep does
        not act on a stale view and try to reclaim a hold a settle just deleted.

        Because the SK embeds the (zero-padded) expiry, this is a pure key range
        scan — `between(prefix, expiry-ceiling(now))` — with NO FilterExpression.
        That matters: DynamoDB's `Limit` bounds items *evaluated*, and a filter
        is applied after. The previous begins_with + expires_at filter let `Limit`
        cut the page across live holds (arbitrary uuid order) so an expired
        orphan sitting behind `Limit` live holds was never returned and leaked
        forever. Ranging by embedded expiry makes `Limit` count only already-
        expired holds, oldest first, so bounded sweeps drain the backlog.
        """
        resp = self._table.query(
            KeyConditionExpression=(
                Key("tenant_id").eq(tenant_id)
                & Key("sk").between(
                    hold_sk_prefix(period),
                    hold_sk_expiry_ceiling(period, now_epoch),
                )
            ),
            ConsistentRead=True,
            Limit=int(limit),
        )
        return resp.get("Items", [])
