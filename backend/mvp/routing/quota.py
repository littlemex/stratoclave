"""Per-model quota counter operations.

DynamoDB schema:
  PK = TENANT#{tenant_id}              SK = MQ#{model}#{period}
  PK = TENANT#{tenant_id}#USER#{user}  SK = MQ#{model}#{period}
  Attributes: reserved (int), settled (int), ttl (epoch)

Quota counters track reservation and settlement per model per period.
The limit is NOT stored on the counter — it comes from routing config
and is passed as a condition expression parameter at reserve time.
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional

from dynamo.client import get_dynamodb_resource

_TABLE = os.getenv("DYNAMODB_MODEL_QUOTAS_TABLE", "stratoclave-model-quotas")

_ensured: set[str] = set()


def _table():
    return get_dynamodb_resource().Table(_TABLE)


def _period_now(timezone: str = "UTC") -> str:
    """Current period key (monthly). Always YYYY-MM."""
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")


def _pk_tenant(tenant_id: str) -> str:
    return f"TENANT#{tenant_id}"


def _pk_user(tenant_id: str, user_id: str) -> str:
    return f"TENANT#{tenant_id}#USER#{user_id}"


def _sk(model: str, period: str) -> str:
    return f"MQ#{model}#{period}"


def ensure_counter(pk: str, sk: str) -> None:
    """Idempotently initialize a counter item if it doesn't exist yet."""
    key = f"{pk}|{sk}"
    if key in _ensured:
        return
    _table().update_item(
        Key={"pk": pk, "sk": sk},
        UpdateExpression="SET reserved = if_not_exists(reserved, :zero), settled = if_not_exists(settled, :zero)",
        ExpressionAttributeValues={":zero": 0},
    )
    _ensured.add(key)


def soft_check_exhausted(
    tenant_id: str,
    user_id: Optional[str],
    model: str,
    period: str,
    amount: int,
    tenant_limit: Optional[int],
    user_limit: Optional[int] = None,
) -> Optional[str]:
    """Eventually-consistent soft check. Returns blocked_by or None.

    This is an optimization — not a correctness mechanism. The atomic
    reserve transaction is the ground truth.
    """
    if tenant_limit is not None:
        pk = _pk_tenant(tenant_id)
        sk = _sk(model, period)
        ensure_counter(pk, sk)
        resp = _table().get_item(Key={"pk": pk, "sk": sk}, ConsistentRead=False)
        item = resp.get("Item", {})
        current = int(item.get("reserved", 0)) + int(item.get("settled", 0))
        if current + amount > tenant_limit:
            return "tenant_quota"

    if user_id and user_limit is not None:
        pk = _pk_user(tenant_id, user_id)
        sk = _sk(model, period)
        ensure_counter(pk, sk)
        resp = _table().get_item(Key={"pk": pk, "sk": sk}, ConsistentRead=False)
        item = resp.get("Item", {})
        current = int(item.get("reserved", 0)) + int(item.get("settled", 0))
        if current + amount > user_limit:
            return "user_quota"

    return None


def build_reserve_txn_items(
    tenant_id: str,
    user_id: Optional[str],
    model: str,
    period: str,
    amount: int,
    tenant_limit: Optional[int],
    user_limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Build TransactWriteItems entries for quota reservation.

    Returns a list of transaction items to append to the existing
    reserve transaction. Each item conditionally adds to `reserved`
    only if (reserved + settled + amount) <= limit.
    """
    table_name = _TABLE
    items = []

    if tenant_limit is not None:
        pk = _pk_tenant(tenant_id)
        sk = _sk(model, period)
        ensure_counter(pk, sk)
        headroom = max(tenant_limit - amount, 0)
        items.append({
            "Update": {
                "TableName": table_name,
                "Key": {"pk": {"S": pk}, "sk": {"S": sk}},
                "UpdateExpression": "SET reserved = reserved + :amt",
                "ConditionExpression": "reserved + settled <= :headroom",
                "ExpressionAttributeValues": {
                    ":amt": {"N": str(amount)},
                    ":headroom": {"N": str(headroom)},
                },
            }
        })

    if user_id and user_limit is not None:
        pk = _pk_user(tenant_id, user_id)
        sk = _sk(model, period)
        ensure_counter(pk, sk)
        headroom = max(user_limit - amount, 0)
        items.append({
            "Update": {
                "TableName": table_name,
                "Key": {"pk": {"S": pk}, "sk": {"S": sk}},
                "UpdateExpression": "SET reserved = reserved + :amt",
                "ConditionExpression": "reserved + settled <= :headroom",
                "ExpressionAttributeValues": {
                    ":amt": {"N": str(amount)},
                    ":headroom": {"N": str(headroom)},
                },
            }
        })

    return items


def settle_quota(
    tenant_id: str,
    user_id: Optional[str],
    model: str,
    period: str,
    reserved_amount: int,
    actual_amount: int,
) -> None:
    """Settle quota counters: move from reserved to settled (unconditional).

    Called after the request completes. Never fails on quota grounds.
    """
    table = _table()

    pk = _pk_tenant(tenant_id)
    sk = _sk(model, period)
    table.update_item(
        Key={"pk": pk, "sk": sk},
        UpdateExpression="SET reserved = reserved - :res, settled = settled + :actual",
        ExpressionAttributeValues={
            ":res": reserved_amount,
            ":actual": actual_amount,
        },
    )

    if user_id:
        pk = _pk_user(tenant_id, user_id)
        table.update_item(
            Key={"pk": pk, "sk": sk},
            UpdateExpression="SET reserved = reserved - :res, settled = settled + :actual",
            ExpressionAttributeValues={
                ":res": reserved_amount,
                ":actual": actual_amount,
            },
        )


def release_quota(
    tenant_id: str,
    user_id: Optional[str],
    model: str,
    period: str,
    reserved_amount: int,
) -> None:
    """Release quota reservation without settling (invoke-time failure).

    Decrements reserved without adding to settled — the request never
    consumed any tokens on this model.
    """
    table = _table()

    pk = _pk_tenant(tenant_id)
    sk = _sk(model, period)
    table.update_item(
        Key={"pk": pk, "sk": sk},
        UpdateExpression="SET reserved = reserved - :res",
        ExpressionAttributeValues={":res": reserved_amount},
    )

    if user_id:
        pk = _pk_user(tenant_id, user_id)
        table.update_item(
            Key={"pk": pk, "sk": sk},
            UpdateExpression="SET reserved = reserved - :res",
            ExpressionAttributeValues={":res": reserved_amount},
        )
