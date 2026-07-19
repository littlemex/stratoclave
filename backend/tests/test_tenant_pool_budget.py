"""Integration tests for tenant pool (dollar) budgeting in the credit pipeline.

These exercise `reserve_credit()` / `settle_reservation_and_log()` end to end
against moto DynamoDB, proving Fable's A-1 design:

  - a request is admitted only when BOTH the per-user token balance AND the
    tenant dollar pool have room, in one atomic transaction;
  - pool exhaustion returns HTTP 402 with reason `tenant_pool_exhausted`;
  - per-user exhaustion returns 402 `personal_budget_exhausted`;
  - settle releases the pool reservation and records actual spend;
  - a tenant with no pool budget keeps the original per-user-token behaviour
    (backward compatibility).
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import HTTPException

from dynamo.tenant_budgets import TenantBudgetsRepository
from mvp._pipeline import reserve_credit, settle_reservation_and_log


@dataclass
class _User:
    user_id: str
    org_id: str
    email: str = "u@example.com"


def _user(seed) -> _User:
    return _User(user_id=seed["user_id"], org_id=seed["tenant_id"])


def _pool(seed):
    return TenantBudgetsRepository().pool_summary(seed["tenant_id"], seed["period"])


def test_reserve_within_pool_debits_pool(seed_tenant_with_pool):
    user = _user(seed_tenant_with_pool)
    ctx = reserve_credit(user, 1000, pricing_key="opus", cost_microusd=1_000_000)

    assert ctx.pool_active is True
    assert ctx.pool_reserved_microusd == 1_000_000
    summary = _pool(seed_tenant_with_pool)
    assert summary["pool_reserved_microusd"] == 1_000_000
    assert summary["remaining_microusd"] == 4_000_000  # $5 - $1


def test_reserve_blocked_when_pool_exhausted(seed_tenant_with_pool):
    user = _user(seed_tenant_with_pool)
    # Pool limit is $5.00. First reserve takes $4.50.
    reserve_credit(user, 1000, pricing_key="opus", cost_microusd=4_500_000)

    # A second request costing $1.00 would exceed the pool: reject with the
    # pool-specific reason, and the per-user tokens must NOT be debited.
    with pytest.raises(HTTPException) as exc:
        reserve_credit(user, 1000, pricing_key="opus", cost_microusd=1_000_000)
    assert exc.value.status_code == 402
    assert exc.value.detail["reason"] == "tenant_pool_exhausted"

    # Pool reserved stayed at the first reservation only.
    assert _pool(seed_tenant_with_pool)["pool_reserved_microusd"] == 4_500_000


def test_reserve_exact_pool_limit_is_allowed(seed_tenant_with_pool):
    user = _user(seed_tenant_with_pool)
    ctx = reserve_credit(user, 1000, pricing_key="opus", cost_microusd=5_000_000)
    assert ctx.pool_active is True
    assert _pool(seed_tenant_with_pool)["remaining_microusd"] == 0


def test_personal_token_cap_blocks_before_pool(seed_tenant_with_pool):
    """When the per-user token balance is the binding constraint, the 402
    reason is personal_budget_exhausted even though the pool has room.
    """
    from dynamo.user_tenants import UserTenantsRepository

    seed = seed_tenant_with_pool
    # Tighten the personal balance to 500 tokens.
    UserTenantsRepository().overwrite_credit(
        user_id=seed["user_id"], tenant_id=seed["tenant_id"], total_credit=500, reset_used=True
    )
    user = _user(seed)

    with pytest.raises(HTTPException) as exc:
        reserve_credit(user, 1000, pricing_key="opus", cost_microusd=1_000_000)
    assert exc.value.status_code == 402
    assert exc.value.detail["reason"] == "personal_budget_exhausted"

    # Neither side moved: the pool was not charged for a rejected request.
    assert _pool(seed)["pool_reserved_microusd"] == 0


def test_settle_releases_pool_reservation_and_records_actual(seed_tenant_with_pool):
    seed = seed_tenant_with_pool
    user = _user(seed)
    # Reserve $2.00 up front.
    ctx = reserve_credit(user, 4000, pricing_key="opus", cost_microusd=2_000_000)
    assert _pool(seed)["pool_reserved_microusd"] == 2_000_000

    # Settle: actual spend was only $0.50.
    settle_reservation_and_log(
        user=user,
        tenants_repo=ctx,
        reservation=4000,
        actual_input_tokens=100,
        actual_output_tokens=400,
        model_id="us.anthropic.claude-opus-4-7",
        context=ctx,
        actual_cost_microusd=500_000,
    )

    summary = _pool(seed)
    # The $2.00 reservation is released; $0.50 is now settled spend.
    assert summary["pool_reserved_microusd"] == 0
    assert summary["pool_settled_microusd"] == 500_000
    # Remaining reflects settled spend only: $5.00 - $0.50 = $4.50.
    assert summary["remaining_microusd"] == 4_500_000


def test_settled_spend_counts_against_future_reservations(seed_tenant_with_pool):
    """After settling real spend, a later reservation sees the reduced pool."""
    seed = seed_tenant_with_pool
    user = _user(seed)
    ctx = reserve_credit(user, 4000, pricing_key="opus", cost_microusd=2_000_000)
    settle_reservation_and_log(
        user=user,
        tenants_repo=ctx,
        reservation=4000,
        actual_input_tokens=1000,
        actual_output_tokens=3000,
        model_id="us.anthropic.claude-opus-4-7",
        context=ctx,
        actual_cost_microusd=4_800_000,  # $4.80 actually spent
    )
    # Only $0.20 remains; a $0.50 request must now be rejected on the pool.
    with pytest.raises(HTTPException) as exc:
        reserve_credit(user, 1000, pricing_key="opus", cost_microusd=500_000)
    assert exc.value.detail["reason"] == "tenant_pool_exhausted"


def test_no_pool_budget_is_backward_compatible(seed_active_tenant):
    """A tenant with no TenantBudgets row uses per-user token budgeting only,
    exactly as before pool budgeting existed. reserve_credit returns a context
    whose pool is inactive and the per-user balance is debited normally.
    """
    user = _user({"user_id": seed_active_tenant["user_id"], "tenant_id": seed_active_tenant["tenant_id"]})
    # Even if a cost is supplied, absence of a pool row means no pool debit.
    ctx = reserve_credit(user, 3000, pricing_key="opus", cost_microusd=1_000_000)
    assert ctx.pool_active is False
    assert ctx.tenants_repo.remaining_credit(user.user_id, user.org_id) == 7_000


def test_context_delegates_refund_to_repo(seed_tenant_with_pool):
    """The ReservationContext must be usable wherever the old code held the
    UserTenantsRepository and called .refund() directly.
    """
    seed = seed_tenant_with_pool
    user = _user(seed)
    ctx = reserve_credit(user, 4000, pricing_key="opus", cost_microusd=1_000_000)
    before = ctx.remaining_credit(user.user_id, user.org_id)
    ctx.refund(user_id=user.user_id, tenant_id=user.org_id, tokens=4000)
    after = ctx.remaining_credit(user.user_id, user.org_id)
    assert after == before + 4000


# ---------------------------------------------------------------------------
# Headroom counter (hot-path design, docs/design/ledger-hot-path.md).
# ---------------------------------------------------------------------------


def test_headroom_initialized_and_maintained(seed_tenant_with_pool):
    """set_pool_limit seeds pool_headroom = limit; a reserve decrements it; a
    settle returns the (reserved - actual) net — headroom == limit - reserved -
    settled holds throughout."""
    seed = seed_tenant_with_pool
    user = _user(seed)
    p0 = _pool(seed)
    assert p0["pool_headroom_microusd"] == 5_000_000  # == limit at seed

    ctx = reserve_credit(user, 1000, pricing_key="opus", cost_microusd=1_000_000)
    p1 = _pool(seed)
    assert p1["pool_headroom_microusd"] == 4_000_000
    assert p1["pool_headroom_microusd"] == (
        p1["pool_limit_microusd"] - p1["pool_reserved_microusd"] - p1["pool_settled_microusd"]
    )

    settle_reservation_and_log(
        user=user, tenants_repo=ctx, reservation=1000,
        actual_input_tokens=200, actual_output_tokens=100,
        model_id="us.anthropic.claude-opus-4-7", context=ctx,
        actual_cost_microusd=600_000,
    )
    p2 = _pool(seed)
    # settle returns net (reserved - actual) = 1_000_000 - 600_000 = +400_000 to
    # the headroom that the reserve had taken down to 4_000_000 => 4_400_000
    # (equivalently limit 5M - reserved 0 - settled 600_000).
    assert p2["pool_reserved_microusd"] == 0
    assert p2["pool_settled_microusd"] == 600_000
    assert p2["pool_headroom_microusd"] == 4_400_000
    assert p2["pool_headroom_microusd"] == (
        p2["pool_limit_microusd"] - p2["pool_reserved_microusd"] - p2["pool_settled_microusd"]
    )


def test_headroom_exhaustion_is_402_not_a_retry(seed_tenant_with_pool):
    """A reserve that would drive headroom negative is a clean 402 (genuine
    exhaustion), and headroom is left untouched — not a lost race that retried."""
    seed = seed_tenant_with_pool
    user = _user(seed)
    reserve_credit(user, 1000, pricing_key="opus", cost_microusd=5_000_000)  # exact fill
    assert _pool(seed)["pool_headroom_microusd"] == 0
    with pytest.raises(HTTPException) as exc:
        reserve_credit(user, 1000, pricing_key="opus", cost_microusd=1)
    assert exc.value.status_code == 402
    assert _pool(seed)["pool_headroom_microusd"] == 0  # unchanged


def test_many_reserves_all_succeed_within_headroom(seed_tenant_with_pool):
    """The design's core property: every reserve that fits in headroom commits —
    there is no spurious contention failure, only genuine 402 on exhaustion.
    (True parallelism is exercised in the live benchmark; moto is not
    thread-safe, so this drives the same many-reserves-one-row path serially and
    asserts none is falsely rejected and the counters stay exact.) 50 reserves of
    $0.01 against a $5 pool all commit; headroom lands exact."""
    seed = seed_tenant_with_pool
    user = _user(seed)
    n = 50
    amt = 10_000  # $0.01 each => 50 * $0.01 = $0.50, well within $5

    ok = 0
    for _ in range(n):
        try:
            reserve_credit(user, 1, pricing_key="opus", cost_microusd=amt)
            ok += 1
        except HTTPException:
            pass

    assert ok == n, "every reserve within headroom must succeed (no false rejection)"
    p = _pool(seed)
    assert p["pool_reserved_microusd"] == n * amt
    assert p["pool_headroom_microusd"] == 5_000_000 - n * amt


def test_set_pool_limit_shifts_headroom_by_delta_not_clobber(seed_tenant_with_pool):
    """Fable review finding 3: changing the ceiling mid-period must shift headroom
    by the ceiling DELTA and preserve reserved/settled — never rewrite headroom
    from a stale read (which would drop a concurrent reserve's move). Raising 5M
    -> 8M after a 1M reserve leaves headroom 4M -> 7M and reserved at 1M; lowering
    below reserved drives headroom negative (refuses admission)."""
    seed = seed_tenant_with_pool
    tid, period = seed["tenant_id"], seed["period"]
    repo = TenantBudgetsRepository()
    user = _user(seed)

    reserve_credit(user, 1000, pricing_key="opus", cost_microusd=1_000_000)
    assert _pool(seed)["pool_headroom_microusd"] == 4_000_000

    # RAISE 5M -> 8M: headroom shifts +3M to 7M, reserved untouched.
    repo.set_pool_limit(tenant_id=tid, period=period, pool_limit_microusd=8_000_000)
    p = _pool(seed)
    assert p["pool_limit_microusd"] == 8_000_000
    assert p["pool_reserved_microusd"] == 1_000_000
    assert p["pool_headroom_microusd"] == 7_000_000

    # LOWER 8M -> 0.5M (below the 1M reserved): headroom goes negative, remaining
    # clamps to 0, and a new reserve is refused (402) — no over-admission.
    repo.set_pool_limit(tenant_id=tid, period=period, pool_limit_microusd=500_000)
    p = _pool(seed)
    assert p["pool_reserved_microusd"] == 1_000_000  # still preserved
    raw = repo.get(tid, period)
    assert int(raw["pool_headroom_microusd"]) == -500_000
    assert p["remaining_microusd"] == 0
    with pytest.raises(HTTPException) as exc:
        reserve_credit(user, 1, pricing_key="opus", cost_microusd=1)
    assert exc.value.status_code == 402
