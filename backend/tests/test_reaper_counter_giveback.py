"""The reaper's reclaim only ever knew how to give back the pool's own share
of a crashed reservation. The SAME admission transaction also debits a
per-user token counter (`UserTenants.credit_used`, a DIFFERENT table keyed by
user+tenant) and, when the tenant configures one, a per-model quota counter
(`used`, keyed `pk=tenant#...` or `pk=tenant#user#...`). Neither counter
survived a crash between reserve and settle: the HOLD row the reaper reads
recorded only the pool amount, so it had no way to know what to give back on
the other two.

`credit_used` is the worst of the two -- it is not period-scoped and carries
no TTL, so a leaked debit accumulates across every crashed request forever and
eventually locks the user out permanently. The per-model quota counter
self-heals at the row's own TTL (period end + grace), so it is wrong only
within the period.

This closes both by freezing the missing facts on the HOLD row at reserve
time (`dynamo.tenant_budgets.hold_put_txn_item`'s `reserved_tokens` /
`user_id` / `model_id` / `quota_period` / `quota_amount` /
`quota_tenant_scope` / `quota_user_scope`) and reversing them, atomically with
the pool restore and the hold delete, in the SAME reclaim transaction the
reaper already runs (`mvp._pipeline._sweep_one_period`).
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import boto3
import pytest
from botocore.exceptions import ClientError

from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
from dynamo.tenant_budgets import hold_sk as _hold_sk
from dynamo.tenants import TenantsRepository
from dynamo.user_tenants import UserTenantsRepository
from mvp import _pipeline
from mvp.routing import quota as _quota


TENANT = "reaper-giveback-tenant"
MODEL = "claude-sonnet-5"
DEFAULT_TOKENS = 2500
DEFAULT_COST = 28_502


@dataclass
class _User:
    user_id: str
    org_id: str
    email: str = "u@example.com"


def _seed(*, pool_limit: int = 10**9) -> str:
    TenantsRepository().create(
        tenant_id=TENANT, name="Reaper Giveback", team_lead_user_id="admin-giveback",
        default_credit=10**12, created_by="test")
    period = current_period()
    TenantBudgetsRepository().set_pool_limit(
        tenant_id=TENANT, period=period, pool_limit_microusd=pool_limit)
    return period


def _user(uid: str = "u-giveback") -> _User:
    UserTenantsRepository().ensure(user_id=uid, tenant_id=TENANT, role="user",
                                   total_credit=10**12)
    return _User(user_id=uid, org_id=TENANT)


def _credit_used(user_id: str) -> int:
    item = UserTenantsRepository().get(user_id, TENANT)
    return int((item or {}).get("credit_used", 0))


def _quota_used(*, period: str, model: str = MODEL, user: str | None = None) -> int:
    tbl = boto3.resource("dynamodb", region_name="us-east-1").Table("stratoclave-model-quotas")
    pk = _quota._pk_user(TENANT, user) if user else _quota._pk_tenant(TENANT)
    resp = tbl.get_item(Key={"pk": pk, "sk": _quota._sk(model, period)})
    return int(resp.get("Item", {}).get("used", 0))


def _reserve(
    user: _User, *, tokens: int = DEFAULT_TOKENS, cost: int = DEFAULT_COST,
    tenant_limit: int | None = None, user_limit: int | None = None, model: str = MODEL,
):
    """Drive the real `reserve_credit` chokepoint directly.

    Not `reserve_credit_for_model`'s cascade: today that cascade only ever
    builds a TENANT-scope quota line (`_reserve_over_candidates` never passes
    `user_limit`), so it cannot exercise the user-only / both-scopes
    configurations the reversal builder has to handle correctly. Calling
    `reserve_credit` directly with an explicit `quota_lines` exercises the
    SAME pool+quota+hold transaction and hold-enrichment code the cascade
    uses, for all three configurations.
    """
    period = current_period()
    quota_lines = None
    if tenant_limit is not None or user_limit is not None:
        quota_lines = _quota.build_reserve_txn_items(
            tenant_id=TENANT, user_id=user.user_id, model=model, period=period,
            amount=cost, tenant_limit=tenant_limit, user_limit=user_limit)
    return _pipeline.reserve_credit(
        user, tokens, pricing_key=None, cost_microusd=cost,
        quota_lines=quota_lines, quota_model=model if quota_lines else None,
        selected_model=model,
    )


def _age_hold_to_sweepable(period: str, hold_id: str, hold_sk: str) -> None:
    """Move a hold's expiry into the past, keeping every other attribute, so
    the inline sweep is willing to reclaim it -- simulating the crash the
    reaper exists for without actually killing a process."""
    budgets = TenantBudgetsRepository()
    item = budgets._table.get_item(Key={"tenant_id": TENANT, "sk": hold_sk}).get("Item")
    assert item is not None, "the reservation should have left its hold in place"
    past = int(time.time()) - 100_000
    budgets._table.delete_item(Key={"tenant_id": TENANT, "sk": hold_sk})
    item["sk"] = _hold_sk(period, past, hold_id)
    item["expires_at"] = past
    budgets._table.put_item(Item=item)


def _sweep(period: str) -> int:
    return _pipeline._sweep_expired_holds(TenantBudgetsRepository(), TENANT, period)


# --------------------------------------------------------- credit_used (headline)


def test_crash_then_sweep_restores_credit_used(dynamodb_mock):
    """A crash between reserve and settle -- simulated by simply never
    calling settle/release, exactly as a killed process would -- must not
    leave `credit_used` debited once the reaper sweeps the orphaned hold.

    MUTATION CHECK: reverting the credit-reversal item in
    `_pipeline._hold_counter_reversal_items` (so it always returns no credit
    item) makes this assertion fail with `credit_used == before + 2500`
    instead of `before` -- confirmed by hand.
    """
    period = _seed()
    user = _user()
    before = _credit_used(user.user_id)
    ctx = _reserve(user)
    assert _credit_used(user.user_id) == before + DEFAULT_TOKENS
    _age_hold_to_sweepable(period, ctx.hold_id, ctx.hold_sk)
    assert _sweep(period) == 1
    assert _credit_used(user.user_id) == before


def test_repeated_crash_and_sweep_does_not_accumulate_lockout(dynamodb_mock):
    """The lockout scenario, made the headline: several independent crashed
    requests, each swept separately, must each return `credit_used` to
    baseline. If a single cycle's reversal were skipped or wrong,
    `credit_used` would drift upward every cycle until the user could no
    longer reserve anything -- the permanent-lockout bug this closes.
    """
    period = _seed()
    user = _user()
    baseline = _credit_used(user.user_id)
    for cycle in range(5):
        ctx = _reserve(user)
        assert _credit_used(user.user_id) == baseline + DEFAULT_TOKENS, cycle
        _age_hold_to_sweepable(period, ctx.hold_id, ctx.hold_sk)
        assert _sweep(period) == 1, cycle
        assert _credit_used(user.user_id) == baseline, cycle


# ------------------------------------------------------------- quota `used`


@pytest.mark.parametrize("tenant_limit,user_limit,label", [
    (10**9, None, "tenant-only"),
    (None, 10**9, "user-only"),
    (10**9, 10**9, "both"),
])
def test_crash_then_sweep_restores_quota_used(dynamodb_mock, tenant_limit, user_limit, label):
    """Every quota configuration `build_reserve_txn_items` supports must be
    reversed correctly by the reaper: tenant-scope only, user-scope only, or
    both. Reversing the WRONG scope (or both when only one was reserved)
    would either miss a real leak or wrongly deflate a counter this
    reservation never touched.

    MUTATION CHECK: forcing `quota.reserved_scopes` to always return
    `{"tenant": False, "user": False}` (as if the hold never recorded which
    scope it reserved against) makes ALL THREE parametrizations of this test
    fail: `used` stays at `cost_microusd` instead of dropping to 0, because
    `_hold_counter_reversal_items` then sees `quota_tenant_scope`/
    `quota_user_scope` both false on the hold and builds no reversal item at
    all -- confirmed by hand (all three parametrizations failed together, and
    the accumulation test below failed too).
    """
    period = _seed()
    user = _user()
    ctx = _reserve(user, tenant_limit=tenant_limit, user_limit=user_limit)
    cost = ctx.pool_reserved_microusd
    assert cost > 0
    if tenant_limit is not None:
        assert _quota_used(period=period) == cost
    if user_limit is not None:
        assert _quota_used(period=period, user=user.user_id) == cost

    _age_hold_to_sweepable(period, ctx.hold_id, ctx.hold_sk)
    assert _sweep(period) == 1

    assert _quota_used(period=period) == 0
    assert _quota_used(period=period, user=user.user_id) == 0


def test_repeated_crash_and_sweep_does_not_accumulate_quota_used(dynamodb_mock):
    """The quota counterpart of the lockout test: repeated crash+sweep cycles
    against a both-scopes quota configuration must return `used` to zero
    every time, not drift upward."""
    period = _seed()
    user = _user()
    for cycle in range(3):
        ctx = _reserve(user, tenant_limit=10**9, user_limit=10**9)
        cost = ctx.pool_reserved_microusd
        assert _quota_used(period=period) == cost, cycle
        assert _quota_used(period=period, user=user.user_id) == cost, cycle
        _age_hold_to_sweepable(period, ctx.hold_id, ctx.hold_sk)
        assert _sweep(period) == 1, cycle
        assert _quota_used(period=period) == 0, cycle
        assert _quota_used(period=period, user=user.user_id) == 0, cycle


# --------------------------------------------------------------- legacy holds


def test_legacy_hold_without_new_facts_still_reclaims_cleanly(dynamodb_mock):
    """A hold written before this enrichment shipped carries none of the new
    facts. The reaper must still heal the pool, must not crash, and -- since
    it has nothing to reverse -- must leave `credit_used` exactly where a
    pre-enrichment reclaim would have left it (still debited; this is the
    documented residual the fix does not retroactively repair).
    """
    period = _seed()
    user = _user()
    before_credit = _credit_used(user.user_id)
    ctx = _reserve(user, tenant_limit=10**9)
    budgets = TenantBudgetsRepository()
    budgets._table.update_item(
        Key={"tenant_id": TENANT, "sk": ctx.hold_sk},
        UpdateExpression=(
            "REMOVE reserved_tokens, user_id, model_id, quota_period, "
            "quota_amount, quota_tenant_scope, quota_user_scope"
        ),
    )
    legacy_hold = budgets._table.get_item(
        Key={"tenant_id": TENANT, "sk": ctx.hold_sk}).get("Item")
    assert "reserved_tokens" not in legacy_hold and "model_id" not in legacy_hold

    _age_hold_to_sweepable(period, ctx.hold_id, ctx.hold_sk)
    assert _sweep(period) == 1  # no crash, and the pool still heals

    pool = budgets.pool_summary(TENANT, period)
    assert int(pool["pool_reserved_microusd"]) == 0
    # Nothing to reverse -> nothing reversed. Still debited/inflated, exactly
    # the residual the ticket names rather than silently repairs.
    assert _credit_used(user.user_id) == before_credit + DEFAULT_TOKENS
    assert _quota_used(period=period) == DEFAULT_COST


# ------------------------------------------------------------- underflow guard


def test_underflow_guard_blocks_a_reversal_larger_than_credit_used(dynamodb_mock):
    """`reverse_reservation_txn_item` reuses `refund()`'s underflow guard: a
    reversal for more than the row currently holds must fail closed rather
    than driving `credit_used` negative."""
    user = _user()
    repo = UserTenantsRepository()
    repo.reserve(user_id=user.user_id, tenant_id=TENANT, tokens=100)
    assert _credit_used(user.user_id) == 100

    item = repo.reverse_reservation_txn_item(
        user_id=user.user_id, tenant_id=TENANT, tokens=500)
    client = boto3.client("dynamodb", region_name="us-east-1")
    with pytest.raises(ClientError) as ei:
        client.transact_write_items(
            TransactItems=[item], ClientRequestToken="test-underflow-guard-token")
    assert ei.value.response["Error"]["Code"] == "TransactionCanceledException"
    assert _credit_used(user.user_id) == 100  # unchanged -- not driven negative


def test_reaper_heals_the_pool_even_when_the_credit_reversal_is_blocked(dynamodb_mock):
    """The underflow guard firing on a reclaim's OWN reversal must not cancel
    the whole reclaim: the pool has to heal regardless, with the blocked
    give-back logged for an operator to reconcile by hand.

    Triggered here the one realistic way it can happen: an admin resets
    `credit_used` (`overwrite_credit(reset_used=True)`) between reserve and
    the reclaim, so the reversal's `credit_used >= tokens` condition fails.
    """
    period = _seed()
    user = _user()
    ctx = _reserve(user)
    UserTenantsRepository().overwrite_credit(
        user_id=user.user_id, tenant_id=TENANT, reset_used=True)
    assert _credit_used(user.user_id) == 0

    _age_hold_to_sweepable(period, ctx.hold_id, ctx.hold_sk)
    assert _sweep(period) == 1  # does not raise, does not skip the pool heal

    pool = TenantBudgetsRepository().pool_summary(TENANT, period)
    assert int(pool["pool_reserved_microusd"]) == 0
    assert _credit_used(user.user_id) == 0  # still 0 -- not driven negative


# --------------------------------------------------------- settle: no double-adjust


def test_settle_after_observation_still_ends_with_correct_counters(dynamodb_mock):
    """The settle path already gives back `credit_used` and quota `used`
    correctly on its own. The hold's newly-frozen facts must not make it
    double-adjust them: they are only ever consumed by the reaper's reclaim
    (and a released retention), never by a normal settle.
    """
    period = _seed()
    user = _user()
    before = _credit_used(user.user_id)
    ctx = _reserve(user, tenant_limit=10**9)

    _pipeline.settle_reservation_and_log(
        user=user, tenants_repo=ctx.tenants_repo, reservation=DEFAULT_TOKENS,
        actual_input_tokens=1000, actual_output_tokens=200,
        model_id=MODEL, context=ctx, actual_cost_microusd=10_000,
    )

    # actual=1200 tokens settled; the unused 1300 was refunded exactly once.
    assert _credit_used(user.user_id) == before + 1200
    assert _quota_used(period=period) == 10_000

    # A defensive double-settle/release must not double-adjust either counter
    # (pre-existing idempotency; verified still true after this enrichment).
    _pipeline.release_pool(ctx)
    assert _credit_used(user.user_id) == before + 1200
    assert _quota_used(period=period) == 10_000


# --------------------------------------------------- retained-hold release (bonus)


def test_releasing_a_retained_hold_also_gives_back_credit_and_quota(dynamodb_mock):
    """A hold an operator RETAINS (`resolve_retained_hold`'s admin path) never
    ran the request's own settle/release, so `credit_used` and quota `used`
    were left exactly where the original reserve put them -- same gap as an
    unreclaimed crash, opened on purpose instead of by a crash. Releasing that
    retention (`release=True`, "the provider's bill shows no charge") must
    give back all three counters, not just the pool's.
    """
    period = _seed()
    user = _user()
    before_credit = _credit_used(user.user_id)
    ctx = _reserve(user, tenant_limit=10**9)

    # Put the hold into RETAINED the same way the reaper's C8.3 branch does,
    # without needing the full request-path machinery: a status flip plus the
    # RETAINED ledger row that documents it.
    budgets = TenantBudgetsRepository()
    hold = budgets._table.get_item(
        Key={"tenant_id": TENANT, "sk": ctx.hold_sk}).get("Item")
    assert budgets.hold_retain(tenant_id=TENANT, sk=ctx.hold_sk)
    _pipeline._reaper_ledger().put_retained(
        tenant_id=TENANT, period=period, hold_id=ctx.hold_id,
        amount_microusd=int(hold["amount_microusd"]), model_id=hold.get("model_id"))

    terminal, settled = _pipeline.resolve_retained_hold(
        TENANT, period, ctx.hold_id, release=True)
    assert (terminal, settled) == ("RELEASE", 0)

    pool = budgets.pool_summary(TENANT, period)
    assert int(pool["pool_reserved_microusd"]) == 0
    assert _credit_used(user.user_id) == before_credit
    assert _quota_used(period=period) == 0
