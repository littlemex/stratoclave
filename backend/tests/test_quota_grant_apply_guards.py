"""F2 (CONTRACT-F2-grant.md): R3, R11, R32 — the guards on what an approval
may grant.

Today none of `dynamo.quota_events`, `mvp.grants` exist at all, so every test
below fails at collection/import with `ModuleNotFoundError`. That import
failure IS the correct "fails against current code" state for a brand-new
module — see design-F2.md section 1 for the exact signatures these tests
pin down, including `approve_limit_raise`'s `now_epoch` override (mirroring
`run_sweep`'s own `now_epoch` parameter) so the 300-second window edges below
are exact rather than racing the wall clock.

R3 — `approved_amount_microusd` must be > 0 and <= the pool maximum.
    DynamoDB's `ADD` is not floored at zero (measured fact motivating the
    guard, reproduced here directly against moto so the guard's *reason*
    is not just asserted but shown): a negative amount reaching
    `grant_apply_pool_txn_item` would silently LOWER `pool_limit`, which is
    the one thing a grant must never do.

R11 — approval refuses an unsatisfiable expiry window with
    422 `grant_window_too_short`.

R32 — the aggregate cap is a condition on the POOL row
    (`pool_granted_microusd <= :cap_minus_G`), because a DynamoDB condition
    can only see the item it writes. Tested twice: once directly against
    the DynamoDB expression (independent of any service-layer glue), and
    once through `approve_limit_raise` end to end.

Seam amendment B1 (SEAMS.md S2): nobody seeds `grant_cap_microusd` and
nobody backfills it — its ABSENCE means "derived from the baseline,
evaluated now", the same sentinel convention F1 already uses for an absent
`manual_limit`. A materialised default would freeze at backfill time and go
quietly wrong (too tight) the moment a tenant hires. Concretely, and
self-contained within F2's own maintained invariant (no dependency on how
F1 stores/derives baseline internally): because `grant_apply_pool_txn_item`
always moves `pool_limit_microusd` and `pool_granted_microusd` by the SAME
amount `G`, and F1's seat-delta/manual-set writers never touch
`pool_granted_microusd` at all, `pool_limit_microusd - pool_granted_microusd`
is exactly the baseline at every instant — a pure, live read, never cached.
So `effective_grant_cap_microusd(tenant_id, period)` is:

    grant_cap_microusd if present, else (pool_limit_microusd - pool_granted_microusd)

read via a consistent GetItem at approval time. The cost B1 requires this
contract to state: the cap can no longer be a row-side DynamoDB condition
(a condition referencing an absent attribute fails outright — measured, not
assumed), so it is computed CALLER-SIDE from this read, and a concurrent
seat change between that read and the transaction's commit opens a small
window R32's daily reconciler assertion closes a day late — the same
lateness class the rest of the reconciler already accepts elsewhere.
"""
from __future__ import annotations

from datetime import datetime, timezone

import boto3
import pytest
from botocore.exceptions import ClientError
from hypothesis import HealthCheck, given, settings, strategies as st

from tests.quota_events_fixtures import (
    quota_events_table,
    seed_pool_with_grant_fields,
    seed_request,
)

assert quota_events_table  # imported for its pytest-fixture side effect (used by name below)

TENANT = "grantcap-org"
PERIOD = "2026-09"
# A now_epoch whose UTC (year, month) is exactly PERIOD, comfortably clear of
# either period boundary, so `approve_limit_raise`'s period resolution
# (design-F2.md: the period the approval targets is derived from the SAME
# `now_epoch` the window check uses, not a separate wall-clock read) lands on
# the pool row these tests seed at PERIOD.
_MID_PERIOD_EPOCH = int(datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp())


def _table():
    return boto3.resource("dynamodb", region_name="us-east-1").Table(
        "stratoclave-quota-events")


def _period_end_epoch(period: str) -> int:
    """Epoch of the first instant OUTSIDE `period` (e.g. "2026-09" ->
    2026-10-01T00:00:00Z), computed independently of any production code so
    this test does not depend on `dynamo.tenant_budgets` growing a matching
    helper."""
    year, month = (int(x) for x in period.split("-"))
    nxt_year, nxt_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return int(datetime(nxt_year, nxt_month, 1, tzinfo=timezone.utc).timestamp())


# ---------------------------------------------------------------------------
# R32 — the exact DynamoDB expression, exercised directly (no service layer)
# ---------------------------------------------------------------------------

def test_r32_grant_apply_cap_condition_refuses_over_cap(dynamodb_mock, quota_events_table):
    """design-F2.md R32: `grant_apply_pool_txn_item`'s ConditionExpression is
    `attribute_exists(pool_limit_microusd) AND pool_granted_microusd <=
    :cap_minus_g`. Seed a pool at granted=90, cap=100; a grant of G=20 must be
    refused (90 + 20 > 100) by the condition itself, not by an earlier
    application-level check — because the whole point of R32 is that this
    condition is the only thing that can see both `pool_granted_microusd` and
    the cap on the SAME item at the SAME instant.
    """
    seed_pool_with_grant_fields(
        TENANT, PERIOD, pool_limit_microusd=1_000_000,
        pool_granted_microusd=90, grant_cap_microusd=100,
    )
    from dynamo.quota_events import QuotaEventsRepository
    from dynamo.tenant_budgets import budget_sk

    repo = QuotaEventsRepository()
    item = repo.grant_apply_pool_txn_item(
        target_pk=TENANT, target_sk=budget_sk(PERIOD),
        approved_amount_microusd=20, cap_minus_amount=100 - 20,
    )
    assert item["Update"]["ConditionExpression"] == (
        "attribute_exists(pool_limit_microusd) AND "
        "pool_granted_microusd <= :cap_minus_g"
    )
    client = boto3.client("dynamodb", region_name="us-east-1")
    with pytest.raises(ClientError) as exc:
        client.transact_write_items(TransactItems=[item])
    assert exc.value.response["Error"]["Code"] == "TransactionCanceledException"
    reasons = [r.get("Code") for r in exc.value.response.get("CancellationReasons", [])]
    assert reasons == ["ConditionalCheckFailed"]

    # And the pool's counters must be untouched — a cancelled transaction
    # commits nothing, which is exactly why the condition lives on the item
    # the money moves on rather than on a separate check.
    from dynamo.tenant_budgets import TenantBudgetsRepository
    row = TenantBudgetsRepository().get(TENANT, PERIOD, consistent_read=True)
    assert int(row["pool_granted_microusd"]) == 90


def test_r32_grant_apply_cap_condition_admits_up_to_cap(dynamodb_mock, quota_events_table):
    """The same condition ADMITS a grant that lands exactly ON the cap
    (90 + 10 == 100), proving the guard is `<=` and not an off-by-one `<`."""
    seed_pool_with_grant_fields(
        TENANT, PERIOD, pool_limit_microusd=1_000_000,
        pool_granted_microusd=90, grant_cap_microusd=100,
    )
    from dynamo.quota_events import QuotaEventsRepository
    from dynamo.tenant_budgets import TenantBudgetsRepository, budget_sk

    repo = QuotaEventsRepository()
    item = repo.grant_apply_pool_txn_item(
        target_pk=TENANT, target_sk=budget_sk(PERIOD),
        approved_amount_microusd=10, cap_minus_amount=100 - 10,
    )
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.transact_write_items(TransactItems=[item])
    row = TenantBudgetsRepository().get(TENANT, PERIOD, consistent_read=True)
    assert int(row["pool_granted_microusd"]) == 100
    assert int(row["pool_limit_microusd"]) == 1_000_010
    assert int(row["pool_headroom_microusd"]) == 1_000_010


def test_r32_negative_ADD_is_not_floored_by_dynamodb(dynamodb_mock, quota_events_table):
    """Measured fact the R3 guard exists to prevent (design-F2.md R3): if a
    negative amount ever reached this transaction item, DynamoDB's `ADD`
    would drive `pool_limit_microusd` below zero without complaint — there
    is no server-side floor. Demonstrated directly against moto so the
    guard's necessity is not merely asserted in prose.
    """
    seed_pool_with_grant_fields(
        TENANT, PERIOD, pool_limit_microusd=1_000_000,
        pool_granted_microusd=0, grant_cap_microusd=10_000_000,
    )
    from dynamo.quota_events import QuotaEventsRepository
    from dynamo.tenant_budgets import TenantBudgetsRepository, budget_sk

    repo = QuotaEventsRepository()
    # A NEGATIVE "grant" — exactly what R3's guard must reject before this
    # item is ever built by `approve_limit_raise`.
    bad_item = repo.grant_apply_pool_txn_item(
        target_pk=TENANT, target_sk=budget_sk(PERIOD),
        approved_amount_microusd=-500_000, cap_minus_amount=10_000_000 - (-500_000),
    )
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.transact_write_items(TransactItems=[bad_item])  # DynamoDB does not refuse this
    row = TenantBudgetsRepository().get(TENANT, PERIOD, consistent_read=True)
    assert int(row["pool_limit_microusd"]) == 500_000, (
        "DynamoDB silently lowered pool_limit via a negative ADD — this is "
        "exactly why R3's amount>0 guard must run in Python before this "
        "transaction item is ever constructed, not rely on any DynamoDB "
        "condition to catch it."
    )


# ---------------------------------------------------------------------------
# R3 — the amount guard, at the service layer
# ---------------------------------------------------------------------------

@given(amount=st.integers(min_value=-(10**15), max_value=0))
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_r3_property_no_accepted_amount_is_non_positive(amount, dynamodb_mock, quota_events_table):
    """Property: for every amount <= 0, `approve_limit_raise` must refuse
    before it ever builds a pool-mutating transaction item — so no accepted
    amount can ever lower `pool_limit` (R3's own framing)."""
    from mvp import grants

    seed_pool_with_grant_fields(
        TENANT, PERIOD, pool_limit_microusd=1_000_000,
        pool_granted_microusd=0, grant_cap_microusd=10_000_000,
    )
    seed_request(
        _table(), request_id="req-neg", tenant_id=TENANT, user_id="user-neg",
        requested_amount_microusd=5_000_000, requested_expires_at=9_999_999_999,
    )
    with pytest.raises(grants.GrantAmountInvalid):
        grants.approve_limit_raise(
            actor=_admin_actor(), request_id="req-neg",
            approved_amount_microusd=amount, expires_at=_MID_PERIOD_EPOCH + 300,
            now_epoch=_MID_PERIOD_EPOCH,
        )


def test_r3_zero_amount_refused(dynamodb_mock, quota_events_table):
    from mvp import grants

    seed_pool_with_grant_fields(
        TENANT, PERIOD, pool_limit_microusd=1_000_000,
        pool_granted_microusd=0, grant_cap_microusd=10_000_000,
    )
    seed_request(
        _table(), request_id="req-zero", tenant_id=TENANT, user_id="user-zero",
        requested_amount_microusd=5_000_000, requested_expires_at=9_999_999_999,
    )
    with pytest.raises(grants.GrantAmountInvalid):
        grants.approve_limit_raise(
            actor=_admin_actor(), request_id="req-zero",
            approved_amount_microusd=0, expires_at=_MID_PERIOD_EPOCH + 300,
            now_epoch=_MID_PERIOD_EPOCH,
        )


def test_r3_over_pool_maximum_refused(dynamodb_mock, quota_events_table):
    from mvp import grants
    from dynamo.tenant_budgets import PoolLimitExceedsMaximumError

    seed_pool_with_grant_fields(
        TENANT, PERIOD, pool_limit_microusd=1_000_000,
        pool_granted_microusd=0, grant_cap_microusd=10**18,
    )
    seed_request(
        _table(), request_id="req-huge", tenant_id=TENANT, user_id="user-huge",
        requested_amount_microusd=10**17, requested_expires_at=9_999_999_999,
    )
    with pytest.raises((grants.GrantAmountInvalid, PoolLimitExceedsMaximumError, ValueError)):
        grants.approve_limit_raise(
            actor=_admin_actor(), request_id="req-huge",
            approved_amount_microusd=10**17, expires_at=_MID_PERIOD_EPOCH + 300,
            now_epoch=_MID_PERIOD_EPOCH,
        )


# ---------------------------------------------------------------------------
# R11 — the window bounds
# ---------------------------------------------------------------------------

def test_r11_window_exactly_300s_out_is_admitted(dynamodb_mock, quota_events_table):
    """The boundary is inclusive: `expires_at == now + 300` must be admitted,
    not refused as 'too short'."""
    from mvp import grants

    now = _MID_PERIOD_EPOCH
    _seed_pending_request_for_approval("req-300", tenant=TENANT, period=PERIOD)
    grant = grants.approve_limit_raise(
        actor=_admin_actor(), request_id="req-300",
        approved_amount_microusd=1_000, expires_at=now + 300, now_epoch=now,
    )
    assert grant["expires_at"] == now + 300


def test_r11_window_short_of_300s_refused(dynamodb_mock, quota_events_table):
    from mvp import grants

    now = _MID_PERIOD_EPOCH
    _seed_pending_request_for_approval("req-299", tenant=TENANT, period=PERIOD)
    with pytest.raises(grants.GrantWindowTooShort):
        grants.approve_limit_raise(
            actor=_admin_actor(), request_id="req-299",
            approved_amount_microusd=1_000, expires_at=now + 299, now_epoch=now,
        )


def test_r11_last_300_seconds_of_period_makes_every_window_too_short(
    dynamodb_mock, quota_events_table,
):
    """When fewer than 300s remain in the period, NO expires_at can satisfy
    both `>= now+300` and `<= period end` — every attempt refuses, per
    design-F2.md's `ceiling = min(now+7d, period_end)` framing."""
    from mvp import grants

    period = "2026-09"
    period_end = _period_end_epoch(period)
    now = period_end - 60  # 60 seconds left in the period
    _seed_pending_request_for_approval("req-tail", tenant=TENANT, period=period)
    with pytest.raises(grants.GrantWindowTooShort):
        grants.approve_limit_raise(
            actor=_admin_actor(), request_id="req-tail",
            approved_amount_microusd=1_000, expires_at=now + 300, now_epoch=now,
        )


def _admin_actor():
    from mvp.deps import AuthenticatedUser

    return AuthenticatedUser(
        user_id="admin-1", email="admin@example.com", org_id="admin-1",
        roles=["admin"], raw_claims={}, auth_kind="cognito",
    )


def _seed_pending_request_for_approval(request_id: str, *, tenant: str, period: str) -> None:
    """Seed a pending Request plus a pool row wide enough that R11's own
    window check is the only thing under test (amount/cap are not binding)."""
    seed_request(
        _table(), request_id=request_id, tenant_id=tenant, user_id="user-window",
        requested_amount_microusd=1_000, requested_expires_at=9_999_999_999,
    )
    seed_pool_with_grant_fields(
        tenant, period, pool_limit_microusd=1_000_000,
        pool_granted_microusd=0, grant_cap_microusd=10_000_000,
    )


# ---------------------------------------------------------------------------
# B1 — absence of grant_cap_microusd means "derive from baseline, evaluated
# now"; no backfill, no enablement gate, no row-side condition on the cap.
# ---------------------------------------------------------------------------

def test_b1_baseline_microusd_is_pool_limit_minus_pool_granted(dynamodb_mock, quota_events_table):
    """The self-contained derivation (design-F2.md / this file's own
    docstring): baseline = pool_limit_microusd - pool_granted_microusd, a
    pure live read requiring no knowledge of F1's internal seat/manual-limit
    storage — it falls out of the invariant F2's own apply/revoke maintain."""
    from dynamo.tenant_budgets import TenantBudgetsRepository

    seed_pool_with_grant_fields(
        TENANT, PERIOD, pool_limit_microusd=1_500_000, pool_granted_microusd=500_000,
    )
    baseline = TenantBudgetsRepository().baseline_microusd(TENANT, PERIOD)
    assert baseline == 1_000_000


def test_b1_effective_cap_derives_from_baseline_when_grant_cap_absent(dynamodb_mock, quota_events_table):
    from mvp.grants import effective_grant_cap_microusd

    seed_pool_with_grant_fields(
        TENANT, PERIOD, pool_limit_microusd=1_000_000, pool_granted_microusd=0,
        grant_cap_microusd=None,  # absent — the case every OTHER test in this
        # file does not exercise (quota_events_fixtures.py's default), which
        # is exactly the gap B2 names: every existing cap test would still
        # pass even if migration left every row uncapped.
    )
    assert effective_grant_cap_microusd(TENANT, PERIOD) == 1_000_000  # == baseline


def test_b1_effective_cap_prefers_the_explicit_figure_when_present(dynamodb_mock, quota_events_table):
    from mvp.grants import effective_grant_cap_microusd

    seed_pool_with_grant_fields(
        TENANT, PERIOD, pool_limit_microusd=1_000_000, pool_granted_microusd=0,
        grant_cap_microusd=250_000,
    )
    assert effective_grant_cap_microusd(TENANT, PERIOD) == 250_000


def test_b1_apply_admits_exactly_up_to_the_absent_caps_derived_baseline(
    dynamodb_mock, quota_events_table,
):
    """No `grant_cap_microusd` at all: the aggregate ceiling is the baseline
    itself (1,000,000). A grant landing exactly there succeeds; one microusd
    over refuses — through `approve_limit_raise` end to end, not just the
    repository-level condition, since the guard is now caller-side (B1's own
    cost) and there is no DynamoDB attribute to condition on at all."""
    from mvp import grants

    seed_pool_with_grant_fields(
        TENANT, PERIOD, pool_limit_microusd=1_000_000, pool_granted_microusd=0,
    )
    seed_request(
        _table(), request_id="req-b1-ok", tenant_id=TENANT, user_id="u1",
        requested_amount_microusd=1_000_000, requested_expires_at=9_999_999_999,
    )
    result = grants.approve_limit_raise(
        actor=_admin_actor(), request_id="req-b1-ok",
        approved_amount_microusd=1_000_000,
        expires_at=_MID_PERIOD_EPOCH + 300, now_epoch=_MID_PERIOD_EPOCH,
    )
    assert result["status"] == "ACTIVE"

    seed_request(
        _table(), request_id="req-b1-over", tenant_id=TENANT, user_id="u1",
        requested_amount_microusd=1, requested_expires_at=9_999_999_999,
    )
    with pytest.raises(grants.GrantCapExceeded):
        grants.approve_limit_raise(
            actor=_admin_actor(), request_id="req-b1-over",
            approved_amount_microusd=1,
            expires_at=_MID_PERIOD_EPOCH + 300, now_epoch=_MID_PERIOD_EPOCH,
        )


def test_b1_effective_cap_is_evaluated_live_not_frozen_at_any_earlier_instant(
    dynamodb_mock, quota_events_table,
):
    """The property a materialised/backfilled default would have broken: a
    baseline change (standing in for F1's seat-delta ADD, which this test
    reproduces directly rather than depending on F1's code existing) between
    two reads moves the derived cap immediately — nothing about it is
    computed once and cached."""
    from dynamo.tenant_budgets import TenantBudgetsRepository, budget_sk
    from mvp.grants import effective_grant_cap_microusd

    seed_pool_with_grant_fields(
        TENANT, PERIOD, pool_limit_microusd=1_000_000, pool_granted_microusd=0,
    )
    assert effective_grant_cap_microusd(TENANT, PERIOD) == 1_000_000

    # A tenant hires: F1's per-seat writer ADDs to pool_limit_microusd (and
    # headroom) without touching pool_granted_microusd — the exact shape
    # `adjust_pool_for_seat_delta` already uses elsewhere in this codebase.
    TenantBudgetsRepository()._table.update_item(
        Key={"tenant_id": TENANT, "sk": budget_sk(PERIOD)},
        UpdateExpression="ADD pool_limit_microusd :d, pool_headroom_microusd :d",
        ExpressionAttributeValues={":d": 200_000_000},
    )
    assert effective_grant_cap_microusd(TENANT, PERIOD) == 1_000_000 + 200_000_000, (
        "the derived cap must move the INSTANT the baseline does, with no "
        "intervening backfill/materialisation step to go stale"
    )


# ---------------------------------------------------------------------------
# U1 — `latest_permissible_expiry_for_period` is R11's rule expressed as a
# function, so F3's DISPLAY of it computes rather than reimplements the
# calendar arithmetic. Until now R11 was proven only through
# `approve_limit_raise`'s refusal (test_r11_*, above); a rule another part
# calls as a function needs to be tested as one.
# ---------------------------------------------------------------------------

def test_u1_latest_permissible_expiry_is_seven_days_out_mid_period(dynamodb_mock, quota_events_table):
    from mvp.grants import latest_permissible_expiry_for_period

    now = _MID_PERIOD_EPOCH
    assert latest_permissible_expiry_for_period(now, PERIOD) == now + 7 * 24 * 3600


def test_u1_latest_permissible_expiry_is_capped_at_period_end_near_the_boundary(
    dynamodb_mock, quota_events_table,
):
    period = "2026-09"
    period_end = _period_end_epoch(period)
    now = period_end - 60  # 60 seconds left in the period
    from mvp.grants import latest_permissible_expiry_for_period

    assert latest_permissible_expiry_for_period(now, period) == period_end, (
        "with fewer than 7 days left in the period, the ceiling is the "
        "period's own end, not now+7d — the same min() approve_limit_raise "
        "enforces as a refusal (test_r11_last_300_seconds_of_period_..., above)"
    )


def test_u1_approve_limit_raises_own_ceiling_is_exactly_this_function(
    dynamodb_mock, quota_events_table,
):
    """The two must never drift apart: `approve_limit_raise`'s own R11 upper
    bound is computed by calling `latest_permissible_expiry_for_period`, not
    by a second, independent calendar computation. Proven by making the
    function return an intentionally-wrong value and observing the refusal
    boundary move with it."""
    from mvp import grants

    _seed_pending_request_for_approval("req-u1", tenant=TENANT, period=PERIOD)
    original = grants.latest_permissible_expiry_for_period
    try:
        grants.latest_permissible_expiry_for_period = lambda now_epoch, period: now_epoch + 1_000
        with pytest.raises(grants.GrantWindowTooShort):
            # 1_001s out would be fine under the REAL 7-day ceiling, but the
            # monkeypatched ceiling above (now+1_000) refuses it — proving
            # approve_limit_raise reads the ceiling from this exact function
            # rather than from an independently inlined 7-day constant.
            grants.approve_limit_raise(
                actor=_admin_actor(), request_id="req-u1",
                approved_amount_microusd=1_000,
                expires_at=_MID_PERIOD_EPOCH + 1_001, now_epoch=_MID_PERIOD_EPOCH,
            )
    finally:
        grants.latest_permissible_expiry_for_period = original
