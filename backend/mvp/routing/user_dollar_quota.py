"""The per-user money ceiling (P3.1): a fourth admission wall, checked in the
same `TransactWriteItems` as the other three, that bounds what ONE member of
a tenant may spend in a period -- in dollars, not tokens.

DynamoDB schema (one item per (tenant, user, period), on the SAME table
`mvp.routing.quota` uses -- see that module's own docstring for why a single
`used` counter and not `reserved`+`settled`):
  PK = TENANT#{tenant_id}#USER#{user_id}   SK = UQ#{period}
  Attributes:
    used         (int)  -- reserved-but-not-yet-settled + settled, in micro-USD
    expires_at   (int)  -- TTL epoch (period end + grace); DynamoDB reaps it

The row's PK deliberately matches `quota._pk_user`'s shape
(`TENANT#{t}#USER#{u}`) but its OWN function (`uq_pk`) rather than an import
of `quota._pk_user`: the two counters key on the same identity for an
unrelated reason (both are per-user), and a shared helper would make a future
change to one's key shape silently also move the other's.

This wall's base comes from `dynamo.tenants` -- a per-tenant, period-keyed
default (`user_dollar_defaults`) that the first admission needing a period
SEALS (`sealed_user_dollar_base`), making the base uniform across every
member and every host without any clock agreement (P3.3). `ceiling = base +
coalesce(granted, 0)`, and `granted` is always zero in PR 3 (O3.2): nothing
writes a per-wall grant for this wall yet, so the identity carries the
coalesce from day one exactly the way `dynamo.tenant_budgets.granted_microusd`
does for the tenant pool, and PR 4 only has to add a writer, never edit this
arithmetic.

**The name of the builder below is forced, not chosen** (I1). `reserve_limits`
sweeps every `.py` under `mvp/` and `dynamo/` for a function matching
`^(?:build_)?reserve_txn_items?$` and expects a matching declared
`LimitKind` for it -- so a builder under any other name is invisible to that
closure test, and it is already taken (per-model quota) in `mvp.routing.quota`,
which is why this wall needs its own module rather than a new function in
that one.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from . import quota as _quota

# The table this wall's row lives on. NOT re-derived from `quota._TABLE`
# (that name is private to that module and this module owns its own reads/
# writes) -- duplicating the one-line env lookup is cheaper than reaching
# into a sibling module's underscore-prefixed constant, and I2 pins both
# walls to the SAME physical table regardless.
_TABLE = os.getenv("DYNAMODB_MODEL_QUOTAS_TABLE", "stratoclave-model-quotas")


def uq_pk(tenant_id: str, user_id: str) -> str:
    return f"TENANT#{tenant_id}#USER#{user_id}"


def uq_sk(period: str) -> str:
    return f"UQ#{period}"


def configured_when(base_microusd: Optional[int]) -> bool:
    """`mvp.reserve_limits`'s `configured_when` for this wall (P3.6): is the
    per-user money ceiling configured, given the SAME resolved base
    `build_reserve_txn_items` below will be handed as `base_microusd`?

    The snapshot for THIS wall (I5's `ConfigSnapshot`) is exactly that one
    already-resolved value -- not the tenant row, not a period, a single
    `Optional[int]` -- because that is the one fact both this predicate and
    the builder need, and handing them the SAME value (computed once, by
    `dynamo.tenants.TenantsRepository.seal_user_dollar_base`, by the admission
    path) is what makes "decided here" and "built there" agree by
    construction rather than by both happening to re-read the same row.
    """
    return base_microusd is not None


def build_reserve_txn_items(
    *,
    tenant_id: str,
    user_id: Optional[str],
    period: str,
    amount: int,
    ceiling: Optional[int],
) -> list[dict[str, Any]]:
    """Build the (0 or 1) TransactWriteItems entries admitting `amount` against
    this user's per-period money ceiling.

    `ceiling` is the value `dynamo.tenants.TenantsRepository.
    seal_user_dollar_base` already resolved-and-sealed for `period` -- passed
    in rather than re-read here, which is the whole point of I5's "one
    snapshot, one decision point": a caller that read the base ONCE, decided
    `configured_when` from it, and then had this builder re-read it could see
    a DIFFERENT answer at build time (a default that was absent a moment ago
    and is now present admits the request against a ceiling this call never
    priced). `ceiling is None` (unconfigured, or no `user_id` to key the
    row on) is this builder's OWN half of that contract: it returns no item,
    the same "not configured" answer `configured_when` gave the caller a
    moment earlier from the identical value.

    The grant is added by the CALLER, not here. This builder conditions on the
    number it was handed, so PR 4's raise path changes what the caller resolves
    and leaves this signature alone. A `granted` parameter here would be a
    surface with no writer in this change, which is the same defect as the pin
    clause O3.2 defers for that reason.
    """
    if ceiling is None or not user_id:
        return []
    sk = uq_sk(period)
    expires_at = _quota._period_expiry(period)
    return [_reserve_item(uq_pk(tenant_id, user_id), sk, int(amount), ceiling, expires_at)]


def _reserve_item(pk: str, sk: str, amount: int, ceiling: int, expires_at: int) -> dict[str, Any]:
    """One TransactWriteItems Update admitting `amount` against `ceiling`.

    Byte-for-byte the same two-branch shape as `quota._reserve_item`
    (I2 pins it there: "the shipped shape at quota.py:136-141"), because the
    reason for the two branches is identical here: a missing `used` reads as
    0, so `attribute_not_exists(used) OR used <= :headroom` is fine for an
    ordinary first reservation, but a request LARGER than the whole ceiling
    (`headroom < 0`) must never be admitted even as a first reservation --
    dropping the `attribute_not_exists` disjunct in that case is what stops
    that (the disjunct would otherwise short-circuit TRUE on the missing-row
    case and over-admit a single oversized request past the ceiling).
    """
    headroom = ceiling - amount
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


def build_reverse_txn_item(
    *, tenant_id: str, user_id: str, period: str, amount: int
) -> dict[str, Any]:
    """The reaper's give-back item for an orphaned reservation: `used -= amount`
    on this (user, period) row.

    Deliberately named OUTSIDE the `reserve_txn_item(s)` convention (I1): it
    is not a RESERVE-direction builder, so a name that matched would make the
    closure test in `tests/test_reserve_limits_registry.py` demand a declared
    `LimitKind` for a builder that has no config source of its own to point
    at -- reversal is not a limit, it is what undoes one.

    Gated on `attribute_exists(used)`, the SAME no-phantom-row guard
    `quota._reverse_item`/`quota._adjust_used` use: a reservation this
    specific row never actually admitted has no `used` attribute to exist, so
    the condition fails closed rather than creating a negative-`used` row for
    a (user, period) this reservation never touched.
    """
    return {
        "Update": {
            "TableName": _TABLE,
            "Key": {
                "pk": {"S": uq_pk(tenant_id, user_id)},
                "sk": {"S": uq_sk(period)},
            },
            "UpdateExpression": "ADD used :d",
            "ConditionExpression": "attribute_exists(used)",
            "ExpressionAttributeValues": {":d": {"N": str(-int(amount))}},
        }
    }


def build_adjust_txn_item(
    *, tenant_id: str, user_id: str, period: str, delta: int
) -> dict[str, Any]:
    """Settle and release's item: `used += delta` on this (user, period) row,
    `delta` SIGNED (settle's overrun can be positive -- see G3 / P3.7).

    Carries no wall-clock value (I6, point 3): the shipped settle generates
    ONE idempotency token reused across its bounded retries and keeps every
    item in that transaction timestamp-free for exactly that reason -- a
    retry with the same token and DIFFERENT bytes (e.g. a fresh `updated_at`)
    is what `_fresh_idempotency_token`'s own docstring in `mvp._pipeline`
    warns turns into `IdempotentParameterMismatchException` against real
    DynamoDB. This builder therefore sets nothing but `used`.

    Same `attribute_exists(used)` guard as `build_reverse_txn_item`, for the
    same reason: a (user, period) this reservation never actually reserved
    against (the wall was not configured when it reserved, and became
    configured only later) must not be adjusted into existence.
    """
    return {
        "Update": {
            "TableName": _TABLE,
            "Key": {
                "pk": {"S": uq_pk(tenant_id, user_id)},
                "sk": {"S": uq_sk(period)},
            },
            "UpdateExpression": "ADD used :d",
            "ConditionExpression": "attribute_exists(used)",
            "ExpressionAttributeValues": {":d": {"N": str(int(delta))}},
        }
    }
