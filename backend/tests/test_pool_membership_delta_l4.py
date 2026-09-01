"""L4 (docs/design/limits.md (C14)): membership changes apply a guarded
±SEAT_MONTHLY_USD delta to a `sizing="per_seat"` pool's limit and headroom, and
`sizing="fixed"` (set by `set_pool_limit`) stops the auto-adjust.

Spec, from the Interface section only:

    "Membership delta. Adding or removing a member on a `sizing = "per_seat"`
    pool applies `ADD pool_limit_microusd ±seat, pool_headroom_microusd
    ±seat` under the existing guard. On a `sizing = "fixed"` pool it applies
    nothing. `set_pool_limit` sets `sizing = "fixed"`."

and the L4 row's "Verified by": "Unit: add and remove a member, limit and
headroom move by exactly one seat and the identity holds. An explicit
`set_pool_limit` flips `sizing` to `fixed` and a subsequent hire moves
nothing."

These tests seed the seat-tracked precondition on the TenantBudgets row
directly (rather than through the L3 tenant-creation route), so a failure here
is evidence about L4 specifically and does not depend on whether L3 has landed.

The `sizing` attribute the spec above names no longer exists. A row follows the
seat count exactly when it carries NO operator figure, so the precondition these
tests need is seeded by `create_seat_tracked_pool` rather than by writing a mode
attribute, and there is no mode attribute left to assert.
"""
from __future__ import annotations

import boto3

from dynamo import TenantBudgetsRepository, UserTenantsRepository, current_period

_SEAT_MICROUSD = 200 * 1_000_000  # $200/seat, the interface's stated default


def _create_users_table_and_row(user_id: str) -> None:
    """conftest.dynamodb_mock does not create the Users table (see the same
    helper in test_new_models_and_credit_ops.py); `switch_tenant` transacts a
    Users.org_id update guarded by `attribute_exists(user_id)`, so both the
    table and a PROFILE row for the moved user must exist."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    dynamodb.create_table(
        TableName="stratoclave-users",
        KeySchema=[
            {"AttributeName": "user_id", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "user_id", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.Table("stratoclave-users").put_item(
        Item={"user_id": user_id, "sk": "PROFILE", "org_id": "acme-eng"}
    )


def _seed_seat_tracked_pool(tenant_id: str, period: str, seats: int) -> None:
    """Seed a BUDGET row that follows the seat count, through the SAME creation
    call L3's tenant-creation path uses, so this file's evidence is about the
    membership-delta mechanism alone. `seats` seats at the stated default rate,
    and no operator figure -- which is what being seat-tracked now consists of."""
    TenantBudgetsRepository().create_seat_tracked_pool(
        tenant_id=tenant_id, period=period, seat_count=seats
    )


def test_adding_a_member_moves_limit_and_headroom_by_exactly_one_seat(dynamodb_mock):
    tenant_id, period = "acme-eng", current_period()
    _seed_seat_tracked_pool(tenant_id, period, 1)  # 1-seat pool, $200

    UserTenantsRepository().ensure(user_id="user-new-hire", tenant_id=tenant_id, role="user")

    row = TenantBudgetsRepository().get(tenant_id, period)
    assert int(row["pool_limit_microusd"]) == 2 * _SEAT_MICROUSD
    assert int(row["pool_headroom_microusd"]) == 2 * _SEAT_MICROUSD
    # the invariant limit == headroom + reserved + settled must still hold
    assert int(row["pool_headroom_microusd"]) == (
        int(row["pool_limit_microusd"])
        - int(row.get("pool_reserved_microusd", 0))
        - int(row.get("pool_settled_microusd", 0))
    )


def test_removing_a_member_via_switch_tenant_moves_both_pools_by_one_seat(dynamodb_mock):
    """`switch_tenant` archives the old membership and creates the new one in a
    single TransactWriteItems — one function that is simultaneously a removal
    from the old tenant and an addition to the new one, so both deltas must
    land."""
    old_tenant, new_tenant, period = "acme-eng", "beta-co", current_period()
    _create_users_table_and_row("user-mobile")
    _seed_seat_tracked_pool(old_tenant, period, 2)      # 2 seats
    _seed_seat_tracked_pool(new_tenant, period, 1)      # 1 seat

    uts = UserTenantsRepository()
    # This `ensure` is itself a membership addition, so it applies +1 seat to the
    # old tenant before the switch removes it again. Reading the seeded figure as
    # the pre-switch state is the arithmetic slip this comment exists to stop: the
    # old pool stands at THREE seats when `switch_tenant` is called.
    uts.ensure(user_id="user-mobile", tenant_id=old_tenant, role="user")
    assert int(TenantBudgetsRepository().get(old_tenant, period)["pool_limit_microusd"]) == (
        3 * _SEAT_MICROUSD
    )

    uts.switch_tenant(user_id="user-mobile", old_tenant_id=old_tenant, new_tenant_id=new_tenant)

    old_row = TenantBudgetsRepository().get(old_tenant, period)
    new_row = TenantBudgetsRepository().get(new_tenant, period)
    # Old tenant: 2 seeded + 1 (ensure) − 1 (switch) = 2.
    assert int(old_row["pool_limit_microusd"]) == 2 * _SEAT_MICROUSD
    # New tenant: 1 seeded + 1 (switch) = 2.
    assert int(new_row["pool_limit_microusd"]) == 2 * _SEAT_MICROUSD
    # Both rows keep the invariant.
    for row in (old_row, new_row):
        assert int(row["pool_headroom_microusd"]) == (
            int(row["pool_limit_microusd"])
            - int(row.get("pool_reserved_microusd", 0))
            - int(row.get("pool_settled_microusd", 0))
        )


# REMOVED: "an explicit set_pool_limit flips `sizing` to `fixed`". There is no
# `sizing` attribute to flip. The requirement it stood for -- that an operator's
# figure ends seat tracking -- is now carried by the presence of
# `manual_limit_microusd`, and asserting that here instead would be a different
# claim than the one this test made.


# NOTE (ambiguity, not a test): a pool with NO `sizing` attribute at all — the
# shape every row written before this change has — is not covered by an
# assertion here. "Adding ... a member ... on a sizing="per_seat" pool applies
# [a delta] ... On a sizing="fixed" pool it applies nothing" names exactly two
# values of `sizing`; it does not say what a membership change does to a row
# that predates the attribute. Reading A: absent is treated as "fixed" (no
# delta) — the conservative choice, and consistent with `resolve_bound_mode`'s
# existing "any unrecognised value fails closed" convention elsewhere in
# `dynamo/tenants.py`. Reading B: absent is treated as "per_seat" (the delta
# still applies) on the theory that every OTHER pool-mutating path in this
# module already treats a legacy row as eligible for the new mechanics once
# touched (e.g. `set_pool_limit`'s own legacy-headroom-backfill branch).
# A test asserting either reading would pass today for the wrong reason (no
# membership-delta code exists yet, so nothing moves regardless of `sizing`),
# so it is flagged here rather than encoded as an assertion.


def test_a_pool_row_with_no_sizing_attribute_does_not_follow_seats(dynamodb_mock):
    """The reading Amendment 1 of the contract settled, and the one nothing else pins.

    Every pool row that existed before this change has no `sizing` attribute, and
    each one is a figure an operator set by hand. Reading absence as `per_seat`
    would make those ceilings start following the seat count behind the
    operator's back — the same failure class as an audit line recording a change
    nobody made. So absence is `fixed`.

    Written because a mutation check found the default was load-bearing in the
    code and asserted nowhere: flipping it to `per_seat` broke no test. This is
    that test.
    """
    tenant_id, period = "legacy-co", current_period()
    repo = TenantBudgetsRepository()
    repo.set_manual_limit(
        tenant_id=tenant_id, period=period, manual_limit_microusd=_SEAT_MICROUSD
    )
    # Reproduce the legacy row shape: no `sizing` attribute at all.
    repo._table.update_item(
        Key={"tenant_id": tenant_id, "sk": f"BUDGET#{period}"},
        UpdateExpression="REMOVE sizing",
    )
    before = repo.get(tenant_id, period)
    assert "sizing" not in before, "the fixture must have no sizing attribute"

    UserTenantsRepository().ensure(
        user_id="user-late-hire", tenant_id=tenant_id, role="user"
    )

    after = repo.get(tenant_id, period)
    assert int(after["pool_limit_microusd"]) == _SEAT_MICROUSD, (
        "a hire moved a pool row that carries no `sizing` attribute; absence must "
        "be read as fixed, so an operator's hand-set figure is never auto-adjusted"
    )
    assert int(after["pool_headroom_microusd"]) == _SEAT_MICROUSD
