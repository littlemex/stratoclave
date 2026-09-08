"""`mvp.routing.user_dollar_quota` — the fourth admission wall's pure builders.

HANDOFF-PR3 I1/I2. This module is new; nothing here exists on `origin/main`, so
every test in this file is expected to fail at IMPORT time until the code
author lands the module. That is the point: these are the executable form of
I1/I2, written from the interface section alone.

Signature note (report this back to the integrator): I1 gives
`build_reserve_txn_items(...) -> list[dict]` with the argument list elided.
I2 names the ingredients ("headroom = ceiling - amount", "tenant_id"/"user_id"/
"period" run through the whole interface for this wall) but never spells the
parameter names. This file calls every builder with EXPLICIT KEYWORDS —
`tenant_id`, `user_id`, `period`, `amount`, `ceiling` for the reserve builder;
`tenant_id`/`user_id`/`period`/`amount` for the reverse builder;
`tenant_id`/`user_id`/`period`/`delta` for the adjust builder — inferred from
the sibling `mvp.routing.quota` module's own `build_reserve_txn_items(tenant_id,
user_id, model, period, amount, tenant_limit, user_limit=None)` and
`quota._adjust_item`'s `delta` naming. If the shipped names differ, every call site
below fails with a `TypeError` on an unexpected keyword — which is the
observable form of the divergence this split is designed to surface, not a
defect in these tests.

The table is NOT a guess: I2 states outright "table: the model-quotas table
(the same table `mvp/routing/quota.py` uses)", so these tests use the same
`dynamodb_mock`-provisioned `stratoclave-model-quotas` table quota.py's own
tests use, with no new table to seed.
"""
from __future__ import annotations

import os

import boto3
import pytest
from botocore.exceptions import ClientError

pytest.importorskip("moto")

from mvp.routing.user_dollar_quota import (  # noqa: E402
    build_adjust_txn_item,
    build_reserve_txn_items,
    build_reverse_txn_item,
    uq_pk,
    uq_sk,
)

_TABLE = os.getenv("DYNAMODB_MODEL_QUOTAS_TABLE", "stratoclave-model-quotas")


@pytest.fixture
def uq_table(dynamodb_mock):
    """The model-quotas table, shared with `mvp.routing.quota` (I2)."""
    return boto3.resource("dynamodb", region_name="us-east-1").Table(_TABLE)


def _get_used(uq_table, tenant_id: str, user_id: str, period: str) -> int:
    resp = uq_table.get_item(
        Key={"pk": uq_pk(tenant_id, user_id), "sk": uq_sk(period)}
    )
    return int(resp.get("Item", {}).get("used", 0))


def _row_exists(uq_table, tenant_id: str, user_id: str, period: str) -> bool:
    resp = uq_table.get_item(
        Key={"pk": uq_pk(tenant_id, user_id), "sk": uq_sk(period)}
    )
    return "Item" in resp


# --------------------------------------------------------------------------- I1 key shape


def test_uq_sk_is_period_keyed_with_the_stated_prefix():
    """I1 states the exact string: `uq_sk(period)` == `"UQ#{period}"`. A wrong
    prefix (or a different separator) would collide the new wall's rows with
    `mvp.routing.quota`'s `MQ#{model}#{period}` rows on the SAME table, since
    both key off the same `pk` shapes — this pins the one thing that keeps
    them apart on the sort key."""
    assert uq_sk("2026-09") == "UQ#2026-09"


def test_uq_pk_is_tenant_and_user_keyed_with_the_stated_prefix():
    """I1 states the exact string: `uq_pk(tenant, user)` == `"TENANT#{t}#USER#{u}"`
    — deliberately the SAME shape `quota._pk_user` already uses, which is what
    lets this wall and the per-model user wall share a partition-key pattern on
    one table without a collision (the sort key, `UQ#` vs `MQ#`, is what
    disambiguates them)."""
    assert uq_pk("acme", "u1") == "TENANT#acme#USER#u1"


# --------------------------------------------------------------------------- I2 cardinality: 0 or 1 items


def test_build_reserve_txn_items_emits_nothing_for_an_absent_ceiling():
    """I3: 'An empty history means unconfigured, not an error... nothing is
    sealed and no item is emitted.' The pure builder's side of that contract is
    that a `None` ceiling (the caller's signal for 'this tenant has no sealed
    base') must yield an EMPTY list, exactly like `quota.build_reserve_txn_items`
    returns `[]` for `tenant_limit=None`. A builder that always emits an item
    regardless of `ceiling` would admit every request against a wall the
    operator never configured."""
    items = build_reserve_txn_items(
        tenant_id="acme", user_id="u1", period="2026-09", amount=1000, ceiling=None,
    )
    assert items == []


def test_build_reserve_txn_items_emits_exactly_one_item_when_configured():
    """I1: 'the admission item, 0 or 1 of them' — never more than one, since
    this wall has exactly one row per (tenant, user, period), unlike the
    per-model wall's optional tenant+user pair."""
    items = build_reserve_txn_items(
        tenant_id="acme", user_id="u1", period="2026-09", amount=1000, ceiling=5_000_000,
    )
    assert len(items) == 1
    key = items[0]["Update"]["Key"]
    assert key["pk"]["S"] == "TENANT#acme#USER#u1"
    assert key["sk"]["S"] == "UQ#2026-09"


# --------------------------------------------------------------------------- I2 the two-branch condition, executed for real


def test_reserve_admits_under_headroom_against_an_absent_row(uq_table):
    """I2's UpdateExpression is `ADD used :amt SET expires_at = if_not_exists(...)`
    with `ConditionExpression = attribute_not_exists(used) OR used <= :headroom`
    when `headroom >= 0` — the SAME shape as `quota.py:136-141`. A first-ever
    reservation against a row that does not exist yet must be admitted (the
    `attribute_not_exists(used)` disjunct), exactly as it is for the per-model
    wall."""
    items = build_reserve_txn_items(
        tenant_id="acme", user_id="u1", period="2026-09", amount=1000, ceiling=5000,
    )
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.transact_write_items(TransactItems=items)
    assert _get_used(uq_table, "acme", "u1", "2026-09") == 1000


def test_reserve_accumulates_and_then_rejects_once_headroom_is_gone(uq_table):
    """Two admissions that together exceed the ceiling: the first is admitted
    (`used` 0 -> 4000, headroom 5000-4000=1000 satisfied since attribute absent),
    the second (`amount=1500` against `used=4000`, `ceiling=5000`, headroom=3500)
    must be REFUSED — `used <= headroom` is `4000 <= 3500`, false — and `used`
    must be left at 4000, not partially applied. A condition that dropped the
    upper branch (always admitting once the row exists) would let the second
    request through and this assertion would see 5500, not a
    `ConditionalCheckFailedException`."""
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.transact_write_items(TransactItems=build_reserve_txn_items(
        tenant_id="acme", user_id="u1", period="2026-09", amount=4000, ceiling=5000,
    ))
    assert _get_used(uq_table, "acme", "u1", "2026-09") == 4000

    with pytest.raises(ClientError) as ei:
        client.transact_write_items(TransactItems=build_reserve_txn_items(
            tenant_id="acme", user_id="u1", period="2026-09", amount=1500, ceiling=5000,
        ))
    assert ei.value.response["Error"]["Code"] == "TransactionCanceledException"
    assert _get_used(uq_table, "acme", "u1", "2026-09") == 4000


def test_negative_headroom_request_is_never_admitted_against_an_absent_row(uq_table):
    """The priority-1 negative-headroom case, spelled out in I2: 'dropping the
    `attribute_not_exists` disjunct when headroom is negative is what stops one
    request larger than the whole ceiling being admitted against an absent
    row.' `amount=6000 > ceiling=5000` => `headroom=-1000`. If the builder kept
    the OR-absent branch here (the bug this clause exists to prevent), a
    missing row would short-circuit the condition to TRUE and this single
    oversized request would be admitted as the tenant's very first reservation
    of the period — this test's whole point is that it must not be, with
    NOTHING previously reserved."""
    items = build_reserve_txn_items(
        tenant_id="acme", user_id="u1", period="2026-09", amount=6000, ceiling=5000,
    )
    client = boto3.client("dynamodb", region_name="us-east-1")
    with pytest.raises(ClientError) as ei:
        client.transact_write_items(TransactItems=items)
    assert ei.value.response["Error"]["Code"] == "TransactionCanceledException"
    # Refused, and the row must still be absent -- a partially-applied ADD
    # inside a cancelled transaction would be the worse failure mode.
    assert not _row_exists(uq_table, "acme", "u1", "2026-09")


def test_negative_headroom_condition_has_no_attribute_not_exists_disjunct():
    """Direct pin on the CONDITION TEXT for the headroom<0 branch, mirroring
    `test_quota.py`'s own 'no cross-attribute arithmetic' check: the shipped
    shape at `quota.py:136-141` drops the `attribute_not_exists(used) OR`
    prefix entirely rather than appending a redundant always-false clause, so a
    regression that re-adds it (even in a form that happens to still fail
    today) is visible in the expression text, not just in one behavioural
    sample."""
    items = build_reserve_txn_items(
        tenant_id="acme", user_id="u1", period="2026-09", amount=6000, ceiling=5000,
    )
    cond = items[0]["Update"]["ConditionExpression"]
    assert "attribute_not_exists" not in cond
    assert cond.strip() == "used <= :headroom"


def test_reserve_item_ttl_is_derived_from_period_not_the_clock():
    """I2: '`:ttl` is `_period_expiry(period)` -- derived from the period, so it
    carries NO wall-clock value (see I6).' Two builder calls for the SAME
    period, built moments apart, must produce byte-identical `:ttl` values --
    if the builder read `time.time()` or `datetime.now()` anywhere, this
    assertion would be the one to catch a build that happened to straddle a
    clock tick."""
    a = build_reserve_txn_items(
        tenant_id="acme", user_id="u1", period="2026-09", amount=1, ceiling=100,
    )
    b = build_reserve_txn_items(
        tenant_id="acme", user_id="u1", period="2026-09", amount=1, ceiling=100,
    )
    ttl_a = a[0]["Update"]["ExpressionAttributeValues"][":ttl"]["N"]
    ttl_b = b[0]["Update"]["ExpressionAttributeValues"][":ttl"]["N"]
    assert ttl_a == ttl_b


# --------------------------------------------------------------------------- I6: the reverse-direction builders, at the item level


def test_build_reverse_txn_item_is_guarded_on_attribute_exists_used(uq_table):
    """I6 requirement 2: 'The `attribute_exists(used)` guard is kept on every
    reversal... an unconditional adjustment would create a phantom row with a
    negative `used`.' A reclaim against a scope this reservation never
    actually touched (no row at all) must be refused, not create a
    negative-`used` phantom that a later admission would read as free
    headroom."""
    item = build_reverse_txn_item(
        tenant_id="acme", user_id="u1", period="2026-09", amount=500,
    )
    client = boto3.client("dynamodb", region_name="us-east-1")
    with pytest.raises(ClientError) as ei:
        client.transact_write_items(TransactItems=[item])
    assert ei.value.response["Error"]["Code"] == "TransactionCanceledException"
    assert not _row_exists(uq_table, "acme", "u1", "2026-09")


def test_build_reverse_txn_item_gives_back_exactly_the_reserved_amount(uq_table):
    """The reaper-reclaim delta (I6 table): `-reserved`. Reserve 4000, then
    reverse 4000 (a crashed request's own reservation, its exact amount) —
    `used` must return to 0, not merely decrease."""
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.transact_write_items(TransactItems=build_reserve_txn_items(
        tenant_id="acme", user_id="u1", period="2026-09", amount=4000, ceiling=10_000,
    ))
    assert _get_used(uq_table, "acme", "u1", "2026-09") == 4000

    client.transact_write_items(TransactItems=[build_reverse_txn_item(
        tenant_id="acme", user_id="u1", period="2026-09", amount=4000,
    )])
    assert _get_used(uq_table, "acme", "u1", "2026-09") == 0


def test_build_adjust_txn_item_accepts_a_positive_delta(uq_table):
    """G3: 'a settle delta can be positive... `used` can end above the
    ceiling.' The settle path's delta is `actual - reserved`, which is
    positive whenever the actual overran the reservation. `build_adjust_txn_item`
    must accept and apply a POSITIVE delta unconditionally (guarded only on
    `attribute_exists(used)`, never on staying under any ceiling) -- a builder
    that silently clamped a positive delta to zero would make G3's stated
    'not bounded' claim false in the one place it is supposed to be true."""
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.transact_write_items(TransactItems=build_reserve_txn_items(
        tenant_id="acme", user_id="u1", period="2026-09", amount=1000, ceiling=1000,
    ))
    assert _get_used(uq_table, "acme", "u1", "2026-09") == 1000

    # actual=1500, reserved=1000 -> delta=+500 (an overrun settle).
    client.transact_write_items(TransactItems=[build_adjust_txn_item(
        tenant_id="acme", user_id="u1", period="2026-09", delta=500,
    )])
    assert _get_used(uq_table, "acme", "u1", "2026-09") == 1500


def test_build_adjust_txn_item_accepts_a_negative_delta_for_release(uq_table):
    """The release path's delta (I6 table): `-reserved`, unconditional (never
    fails on quota grounds, mirroring `quota.release_quota`)."""
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.transact_write_items(TransactItems=build_reserve_txn_items(
        tenant_id="acme", user_id="u1", period="2026-09", amount=2000, ceiling=10_000,
    ))
    client.transact_write_items(TransactItems=[build_adjust_txn_item(
        tenant_id="acme", user_id="u1", period="2026-09", delta=-2000,
    )])
    assert _get_used(uq_table, "acme", "u1", "2026-09") == 0


def test_build_adjust_txn_item_is_a_noop_on_a_row_with_no_used_attribute(uq_table):
    """The same `attribute_exists(used)` guard `quota._adjust_item` uses,
    stated in I6 requirement 2 for BOTH reversal directions. Settling a
    request that never actually reserved against this wall (e.g. the tenant
    had no default at the time it was priced, then got one before settle) must
    be a clean no-op, never a phantom negative row and never an unhandled
    exception the caller has to guard against by hand."""
    item = build_adjust_txn_item(
        tenant_id="acme", user_id="never-reserved", period="2026-09", delta=-500,
    )
    client = boto3.client("dynamodb", region_name="us-east-1")
    # Must not raise -- either the builder's own condition swallows the
    # ConditionalCheckFailed the way `quota._adjust_item` does, or (if this
    # builder is a pure item-builder with no I/O, matching `build_reverse_txn_item`
    # / `build_reserve_txn_items`) the SAME condition-failure contract applies
    # and it is the CALLER's job to swallow it -- either way, the row must stay
    # absent afterward.
    try:
        client.transact_write_items(TransactItems=[item])
    except ClientError as e:
        assert e.response["Error"]["Code"] == "TransactionCanceledException"
    assert not _row_exists(uq_table, "acme", "never-reserved", "2026-09")
