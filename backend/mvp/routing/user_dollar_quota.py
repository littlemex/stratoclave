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

from dynamo.client import get_dynamodb_resource

from . import quota as _quota

# The table this wall's row lives on. NOT re-derived from `quota._TABLE`
# (that name is private to that module and this module owns its own reads/
# writes) -- duplicating the one-line env lookup is cheaper than reaching
# into a sibling module's underscore-prefixed constant, and I2 pins both
# walls to the SAME physical table regardless.
_TABLE = os.getenv("DYNAMODB_MODEL_QUOTAS_TABLE", "stratoclave-model-quotas")


def _table():
    return get_dynamodb_resource().Table(_TABLE)


def uq_pk(tenant_id: str, user_id: str) -> str:
    return f"TENANT#{tenant_id}#USER#{user_id}"


def uq_sk(period: str) -> str:
    return f"UQ#{period}"


def read_granted_microusd(tenant_id: str, user_id: str, period: str) -> int:
    """The per-user row's OWN `granted_microusd`, read ONCE per admission
    attempt (P4.6/I7).

    Zero when the row does not exist (a member who has never had a raise
    approved) or when the attribute is absent on an existing row (a member
    whose row exists only because a reservation created it) -- the same
    "missing reads as zero" convention `used` already carries on this row.

    The caller hands this SAME integer to two places that must agree: the
    ceiling arithmetic (`base + granted`, composed by `mvp._pipeline
    ._uq_ceiling`) and the pin this module's own `build_reserve_txn_items`
    puts in the admission's ConditionExpression. Reading it once here, before
    either, is what makes them agree by construction rather than by both
    happening to re-read the same row and getting lucky.
    """
    resp = _table().get_item(
        Key={"pk": uq_pk(tenant_id, user_id), "sk": uq_sk(period)},
        ConsistentRead=True)
    item = resp.get("Item")
    if not item:
        return 0
    return int(item.get("granted_microusd", 0) or 0)


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


def read_row_figures(tenant_id: str, user_id: str, period: str) -> tuple[int, int]:
    """`(granted_microusd, used)` off the per-user row in one read, both zero when
    the row does not exist.

    For READERS that want both -- the requester-facing wall status. Deliberately
    NOT a widening of `read_granted_microusd`: that function's contract is about
    one integer read once per admission attempt and pinned into the admission's
    condition, and adding a second return value to it would put a display concern
    inside the enforcement path. Two callers, two shapes, one row format.

    Not `ConsistentRead`: this is a figure a person reads on a page, where a
    strongly-consistent read buys nothing a page refresh does not, unlike the
    admission path where the value is pinned into a condition.
    """
    resp = _table().get_item(
        Key={"pk": uq_pk(tenant_id, user_id), "sk": uq_sk(period)})
    item = resp.get("Item")
    if not item:
        return 0, 0
    return int(item.get("granted_microusd", 0) or 0), int(item.get("used", 0) or 0)


def build_reserve_txn_items(
    *,
    tenant_id: str,
    user_id: Optional[str],
    period: str,
    amount: int,
    ceiling: Optional[int],
    granted_read: int = 0,
) -> list[dict[str, Any]]:
    """Build the (0 or 1) TransactWriteItems entries admitting `amount` against
    this user's per-period money ceiling.

    `ceiling` is the value `dynamo.tenants.TenantsRepository.
    seal_user_dollar_base` already resolved-and-sealed for `period`, ADDED to
    the caller's own `read_granted_microusd` read (PR 4's own arithmetic,
    `mvp._pipeline._uq_ceiling`) -- passed in rather than re-read here, which
    is the whole point of I5's "one snapshot, one decision point": a caller
    that read the base and the grant ONCE, decided `configured_when` and
    priced this call from them, and then had this builder re-read either
    could see a DIFFERENT answer at build time. `ceiling is None`
    (unconfigured, or no `user_id` to key the row on) is this builder's OWN
    half of that contract: it returns no item, the same "not configured"
    answer `configured_when` gave the caller a moment earlier from the
    identical value.

    `granted_read` (P4.6/I7) is the SAME grant figure the caller folded into
    `ceiling` -- handed here SEPARATELY, not decomposed from `ceiling`, so
    this builder can pin the exact number it was told was live rather than
    a base/granted split it would otherwise have to reconstruct.
    """
    if ceiling is None or not user_id:
        return []
    sk = uq_sk(period)
    expires_at = _quota._period_expiry(period)
    return [_reserve_item(
        uq_pk(tenant_id, user_id), sk, int(amount), ceiling, expires_at,
        int(granted_read))]


def _reserve_item(
    pk: str, sk: str, amount: int, ceiling: int, expires_at: int,
    granted_read: int = 0,
) -> dict[str, Any]:
    """One TransactWriteItems Update admitting `amount` against `ceiling`.

    The `used` clause is byte-for-byte the same two-branch shape as
    `quota._reserve_item` (I2 pins it there: "the shipped shape at
    quota.py:136-141"), because the reason for the two branches is identical
    here: a missing `used` reads as 0, so
    `attribute_not_exists(used) OR used <= :headroom` is fine for an ordinary
    first reservation, but a request LARGER than the whole ceiling
    (`headroom < 0`) must never be admitted even as a first reservation --
    dropping the `attribute_not_exists` disjunct in that case is what stops
    that (the disjunct would otherwise short-circuit TRUE on the missing-row
    case and over-admit a single oversized request past the ceiling).

    The `granted_microusd` clause (P4.6, exact form I7) is ANDed onto it. It
    pins the grant figure the caller read when it composed `ceiling`: a
    revoke landing between that read and this commit lowers
    `granted_microusd` under `:granted_read` and trips the clause, refusing a
    request priced against capacity that no longer exists; an approval
    landing in that same window only RAISES `granted_microusd`, so the `>=`
    (not `=`) lets it through rather than refusing a member at the exact
    moment their raise landed. The `attribute_not_exists` branch is for the
    ordinary unraised member (no `granted_microusd` at all): it must fail
    unless the caller's own read also saw nothing (`:granted_read = :zero`),
    which is the same "the read and the write must agree about absence"
    shape the `used` clause already carries.
    """
    headroom = ceiling - amount
    if headroom >= 0:
        used_condition = "attribute_not_exists(used) OR used <= :headroom"
    else:
        used_condition = "used <= :headroom"
    granted_condition = (
        "((attribute_not_exists(granted_microusd) AND :granted_read = :zero) "
        "OR granted_microusd >= :granted_read)"
    )
    condition = f"({used_condition}) AND {granted_condition}"
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
                ":granted_read": {"N": str(int(granted_read))},
                ":zero": {"N": "0"},
            },
        }
    }


def row_ttl_for_period(period: str) -> int:
    """The SAME period-end-plus-grace TTL `build_reserve_txn_items` already
    sets on this row (`quota._period_expiry`), exposed for `mvp.grants`'
    apply/revoke builders (I5) to pass as their own `expires_at` -- reached
    through this module's own public surface rather than by a sibling module
    importing `mvp.routing.quota`'s underscore-prefixed function directly,
    which is the same "this module owns its own reads/writes" boundary this
    file's docstring already draws for `_TABLE`."""
    return _quota._period_expiry(period)


def build_grant_apply_txn_item(
    *, target_pk: str, target_sk: str, approved_amount_microusd: int,
    expires_at: int,
) -> dict[str, Any]:
    """P4.3/P4.4's per-user apply: `granted_microusd += approved_amount_microusd`
    on the (user, period) row a raise was just approved against.

    No `ConditionExpression`, and that absence is deliberate rather than an
    omission (unlike the revoke builder below, which floors). The member may
    not have spent in this period at all -- I4 point 3's own reasoning for why
    `target_pk`/`target_sk` are COMPUTED rather than read off an existing row
    -- so this write must be able to CREATE the row, and a condition that
    required anything to already exist would refuse exactly the member this
    grant is for.

    `expires_at` (caller-computed, the same period-end-plus-grace TTL
    `build_reserve_txn_items` already sets) is written with `if_not_exists`
    for the reason stated in the handoff: a row an approval creates for a
    member who has not spent yet must still expire, and `if_not_exists` is
    what keeps a LATER first reservation's own `if_not_exists(expires_at, ...)`
    from being the only writer that ever tried.
    """
    amount = int(approved_amount_microusd)
    return {
        "Update": {
            "TableName": _TABLE,
            "Key": {"pk": {"S": target_pk}, "sk": {"S": target_sk}},
            "UpdateExpression": (
                "ADD granted_microusd :g SET "
                "expires_at = if_not_exists(expires_at, :ttl)"
            ),
            "ExpressionAttributeValues": {
                ":g": {"N": str(amount)},
                ":ttl": {"N": str(int(expires_at))},
            },
        }
    }


def build_grant_revoke_txn_item(
    *, target_pk: str, target_sk: str, approved_amount_microusd: int,
    expires_at: int,
) -> dict[str, Any]:
    """I5: the per-user revoke, selected by the grant's `limit_kind` in
    `mvp.grants._revoke_txn_items` -- NOT `TenantBudgetsRepository
    .grant_revoke_txn_item`, which is pool-shaped despite its generic
    parameter names (it moves three POOL attributes, conditions on a pool
    attribute, and writes to the tenant-budgets table) and cannot address a
    `UQ#{period}` row on this wall's table.

    `ADD granted_microusd :neg SET expires_at = if_not_exists(expires_at,
    :ttl)`, gated on `attribute_exists(granted_microusd) AND
    granted_microusd >= :g` -- the exact floor I5 specifies, and the exact
    reason the pool builder's own floor exists: an absent `granted_microusd`
    means nothing was ever granted, so a revoke against it must fail rather
    than treat the absence as zero and subtract, which would drive the term
    (and with it this wall's ceiling) negative. `coalesce` is not a DynamoDB
    function and does not appear here; this is the shipped idiom (assert
    existence, then compare) the pool builder already uses.

    `expires_at` with `if_not_exists`, same as the apply item and for the
    same reason -- this write must never be the reason a row's TTL is unset.
    """
    amount = int(approved_amount_microusd)
    return {
        "Update": {
            "TableName": _TABLE,
            "Key": {"pk": {"S": target_pk}, "sk": {"S": target_sk}},
            "UpdateExpression": (
                "ADD granted_microusd :neg SET "
                "expires_at = if_not_exists(expires_at, :ttl)"
            ),
            "ConditionExpression": (
                "attribute_exists(granted_microusd) AND "
                "granted_microusd >= :g"
            ),
            "ExpressionAttributeValues": {
                ":g": {"N": str(amount)},
                ":neg": {"N": str(-amount)},
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
    `quota._reverse_item`/`quota._adjust_item` use: a reservation this
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
