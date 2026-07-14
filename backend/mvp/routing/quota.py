"""Per-model quota counter operations.

DynamoDB schema (one item per (scope, model, period)):
  PK = TENANT#{tenant_id}              SK = MQ#{model}#{period}
  PK = TENANT#{tenant_id}#USER#{user}  SK = MQ#{model}#{period}
  Attributes:
    used         (int)  — reserved-but-not-yet-settled + settled, in one number
    expires_at   (int)  — TTL epoch (period end + grace); DynamoDB reaps it

Design note (why ONE `used` counter, not reserved+settled):
  A DynamoDB ConditionExpression CANNOT do arithmetic across attributes —
  `reserved + settled <= :headroom` raises ValidationException on every call
  (verified against real DynamoDB; moto silently accepts it). So we keep a
  single monotonic `used = reserved_in_flight + settled` and gate with the
  no-arithmetic condition `attribute_not_exists(used) OR used <= :headroom`,
  where `:headroom = limit - amount` is computed client-side.

  reserve : ADD used += amount   (cond: used <= limit - amount)  → cancels if over
  settle  : ADD used += (actual - reserved)   (unconditional; actual<=reserved so
            this is <= 0 — releases the over-reservation, leaves settled recorded)
  release : ADD used += (-reserved)           (unconditional; invoke failed, no spend)

  Net: after settle, `used` == sum of settled actuals; after release, the
  reservation is fully removed. `used` never needs a separate reserved field.
"""
from __future__ import annotations

import calendar
import datetime
import os
from typing import Any, Optional

from dynamo.client import get_dynamodb_resource

_TABLE = os.getenv("DYNAMODB_MODEL_QUOTAS_TABLE", "stratoclave-model-quotas")
_TTL_GRACE_SECONDS = 3 * 24 * 3600  # keep a period's counters 3 days past its end


def _table():
    return get_dynamodb_resource().Table(_TABLE)


def _period_now() -> str:
    """Current monthly period key, YYYY-MM (UTC)."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")


def _period_expiry(period: str) -> int:
    """TTL epoch for a YYYY-MM period: end of that month + grace."""
    year, month = (int(x) for x in period.split("-"))
    last_day = calendar.monthrange(year, month)[1]
    end = datetime.datetime(year, month, last_day, 23, 59, 59, tzinfo=datetime.timezone.utc)
    return int(end.timestamp()) + _TTL_GRACE_SECONDS


def _pk_tenant(tenant_id: str) -> str:
    return f"TENANT#{tenant_id}"


def _pk_user(tenant_id: str, user_id: str) -> str:
    return f"TENANT#{tenant_id}#USER#{user_id}"


def _sk(model: str, period: str) -> str:
    return f"MQ#{model}#{period}"


def soft_check_exhausted(
    tenant_id: str,
    user_id: Optional[str],
    model: str,
    period: str,
    amount: int,
    tenant_limit: Optional[int],
    user_limit: Optional[int] = None,
) -> Optional[str]:
    """Eventually-consistent soft check. Returns the blocking scope or None.

    An optimization only — the atomic reserve transaction is ground truth. A
    missing item reads as used=0, so no initialization is needed here.
    """
    tbl = _table()
    for scope, pk, limit in (
        ("tenant_quota", _pk_tenant(tenant_id), tenant_limit),
        ("user_quota", _pk_user(tenant_id, user_id) if user_id else None, user_limit),
    ):
        if pk is None or limit is None:
            continue
        resp = tbl.get_item(Key={"pk": pk, "sk": _sk(model, period)}, ConsistentRead=False)
        used = int(resp.get("Item", {}).get("used", 0))
        if used + amount > limit:
            return scope
    return None


def _reserve_item(pk: str, sk: str, amount: int, limit: int, expires_at: int) -> dict[str, Any]:
    """One TransactWriteItems Update that reserves `amount` against `limit`.

    No cross-attribute arithmetic (DynamoDB forbids it): we gate on the
    client-computed `:headroom = limit - amount` with a plain `ADD used :amt`.

    The condition has TWO cases because a missing `used` reads as 0:
      - amount <= limit (headroom >= 0): a first reservation (no `used` yet) is
        fine, and an existing one is fine iff `used <= headroom`. Condition:
        `attribute_not_exists(used) OR used <= :headroom`.
      - amount  > limit (headroom  < 0): the request alone exceeds the whole
        limit; it must NEVER be admitted, not even as the first reservation. We
        DROP the `attribute_not_exists` branch so the condition is just
        `used <= :headroom` — false on a missing attribute AND on any real value
        (headroom is negative), so it always cancels. (Without this, the
        `attribute_not_exists` branch would short-circuit TRUE and over-admit a
        single oversized request past the limit.)
    Also sets the TTL on first touch via if_not_exists so counters self-expire.
    """
    headroom = limit - amount
    if headroom >= 0:
        condition = "attribute_not_exists(used) OR used <= :headroom"
    else:
        condition = "used <= :headroom"
    return {
        "Update": {
            "TableName": _TABLE,
            "Key": {"pk": {"S": pk}, "sk": {"S": sk}},
            "UpdateExpression": "ADD used :amt SET expires_at = if_not_exists(expires_at, :ttl)",
            "ConditionExpression": condition,
            "ExpressionAttributeValues": {
                ":amt": {"N": str(int(amount))},
                ":headroom": {"N": str(int(headroom))},
                ":ttl": {"N": str(int(expires_at))},
            },
        }
    }


def build_reserve_txn_items(
    tenant_id: str,
    user_id: Optional[str],
    model: str,
    period: str,
    amount: int,
    tenant_limit: Optional[int],
    user_limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Build the TransactWriteItems entries for a per-model quota reservation.

    Pure builder (no I/O / no side effects). Appended to the pooled-budget
    reserve transaction so quota + budget commit atomically. A model with no
    configured limit contributes no item (unlimited).
    """
    sk = _sk(model, period)
    expires_at = _period_expiry(period)
    items: list[dict[str, Any]] = []
    if tenant_limit is not None:
        items.append(_reserve_item(_pk_tenant(tenant_id), sk, amount, tenant_limit, expires_at))
    if user_id and user_limit is not None:
        items.append(_reserve_item(_pk_user(tenant_id, user_id), sk, amount, user_limit, expires_at))
    return items


def settle_quota(
    tenant_id: str,
    user_id: Optional[str],
    model: str,
    period: str,
    reserved_amount: int,
    actual_amount: int,
) -> None:
    """Settle: adjust `used` from the reserved estimate to the actual spend.

    `used` already includes `reserved_amount` from the reserve. actual<=reserved
    by construction, so we ADD (actual - reserved) (<= 0), leaving `used` equal
    to settled actuals. Unconditional; never fails on quota grounds.
    """
    delta = int(actual_amount) - int(reserved_amount)
    if delta == 0:
        return
    _adjust_used(tenant_id, user_id, model, period, delta)


def release_quota(
    tenant_id: str,
    user_id: Optional[str],
    model: str,
    period: str,
    reserved_amount: int,
) -> None:
    """Release a reservation without settling (invoke-time failure): used -= reserved."""
    _adjust_used(tenant_id, user_id, model, period, -int(reserved_amount))


def _adjust_used(tenant_id, user_id, model, period, delta: int) -> None:
    tbl = _table()
    sk = _sk(model, period)
    for pk in (_pk_tenant(tenant_id), _pk_user(tenant_id, user_id) if user_id else None):
        if pk is None:
            continue
        tbl.update_item(
            Key={"pk": pk, "sk": sk},
            UpdateExpression="ADD used :d",
            ExpressionAttributeValues={":d": delta},
        )
