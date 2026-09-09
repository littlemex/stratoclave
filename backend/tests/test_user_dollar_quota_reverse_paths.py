"""HANDOFF-PR3 I6 -- the three reverse paths (reaper reclaim, settle, release)
each give back the `UQ#{period}` counter, in the SAME `TransactWriteItems` as
the existing model-keyed adjustment.

WIRING ASSUMPTION, FLAGGED (report this to the integrator, priority-3 territory
even though this file is about I6 not G1): none of `reserve_credit`,
`settle_reservation_and_log`, `release_pool`, or `_sweep_expired_holds` gained
a new parameter in the interface section -- I1-I7 describe what the wall's OWN
module exposes and how the tenant row behaves, never a change to these four
existing `mvp._pipeline` call sites' signatures. The only reading under which
this wall's reservation and reversal are exercised by calling these four
functions EXACTLY as every other test in this suite already does (no new
kwarg) is that the admission path resolves/seals the tenant's config from
`user.org_id` / `user.user_id` / the request's own period ITSELF, the same way
it already resolves `TenantBudgetsRepository().get(user.org_id, period)`
without the caller supplying it. This file commits to that reading. If instead
the code author threads the new wall in through an explicit new parameter
(mirroring `quota_lines`/`quota_model`), every test below that calls
`reserve_credit` will still exercise the OTHER three counters (pool/token/
per-model quota, all pre-existing and unaffected) but will silently show
`_uq_used(...) == 0` throughout rather than failing loudly -- which is why
every test here asserts the reservation actually reserved something
(`_uq_used(...) == <expected> ` right after `_reserve`, not just after the
reversal) before asserting on the give-back.

Seeding the tenant's per-user dollar default is done by a RAW `update_item`
against the exact I3 schema (`user_dollar_defaults`, `user_dollar_defaults_version`),
never through the guessed setter -- so these tests are insulated from the I3
naming uncertainty documented in `test_user_dollar_quota_tenant_defaults.py`.
The effective period is seeded in the past (`"2020-01"`) so it resolves for
whatever period `current_period()` returns when this file runs, with no
dependency on the setter's "future only" policy.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal

import boto3
import pytest

pytest.importorskip("moto")

from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
from dynamo.tenant_budgets import hold_sk as _hold_sk
from dynamo.tenants import TenantsRepository
from dynamo.user_tenants import UserTenantsRepository
from mvp import _pipeline
from mvp.routing.user_dollar_quota import uq_pk, uq_sk

TENANT = "uq-reverse-tenant"
MODEL = "claude-sonnet-5"
DEFAULT_TOKENS = 2500
DEFAULT_COST = 1_000_000  # $1.00, the request's priced cost in micro-USD
UQ_CEILING = 100_000_000  # $100.00 -- comfortably above DEFAULT_COST


@dataclass
class _User:
    user_id: str
    org_id: str
    email: str = "u@example.com"


def _uq_table():
    import os

    name = os.getenv("DYNAMODB_MODEL_QUOTAS_TABLE", "stratoclave-model-quotas")
    return boto3.resource("dynamodb", region_name="us-east-1").Table(name)


def _uq_used(user_id: str, period: str) -> int:
    resp = _uq_table().get_item(Key={"pk": uq_pk(TENANT, user_id), "sk": uq_sk(period)})
    return int(resp.get("Item", {}).get("used", 0))


def _seed(*, pool_limit: int = 10**9, uq_default_microusd: int = UQ_CEILING) -> str:
    TenantsRepository().create(
        tenant_id=TENANT, name="UQ Reverse Paths", team_lead_user_id="admin-uq",
        default_credit=10**12, created_by="test")
    period = current_period()
    TenantBudgetsRepository().set_manual_limit(
        tenant_id=TENANT, period=period, manual_limit_microusd=pool_limit)
    # Raw seed of the I3 schema (bypasses the setter's own naming/policy
    # uncertainty entirely -- see module docstring).
    TenantsRepository()._table.update_item(
        Key={"tenant_id": TENANT},
        UpdateExpression="SET user_dollar_defaults = :m, user_dollar_defaults_version = :v",
        ExpressionAttributeValues={
            ":m": {"2020-01": Decimal(uq_default_microusd)},
            ":v": 1,
        },
    )
    return period


def _user(uid: str = "u-uq-reverse") -> _User:
    UserTenantsRepository().ensure(user_id=uid, tenant_id=TENANT, role="user",
                                    total_credit=10**12)
    return _User(user_id=uid, org_id=TENANT)


def _reserve(user: _User, *, tokens: int = DEFAULT_TOKENS, cost: int = DEFAULT_COST):
    return _pipeline.reserve_credit(
        user, tokens, pricing_key=None, cost_microusd=cost, selected_model=MODEL,
    )


def _age_hold_to_sweepable(period: str, hold_id: str, hold_sk: str) -> None:
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


# --------------------------------------------------------------------------- reaper reclaim


def test_reaper_reclaim_gives_back_the_uq_counter(dynamodb_mock):
    """I6 table, row 1: reaper reclaim's delta on `UQ` is `-reserved`, inside
    the SAME reclaim transaction that restores the pool and the credit
    counter (already covered for those two by
    `test_reaper_counter_giveback.py`). A crash between reserve and settle,
    simulated the same way that file does (never call settle/release; age the
    hold; sweep), must return the UQ counter to zero -- if the reaper's
    reclaim transaction never gained an item for this wall, `used` would stay
    pinned at the reserved amount for the rest of the period, permanently
    reducing this user's headroom by money that was never actually spent."""
    period = _seed()
    user = _user()
    ctx = _reserve(user)
    assert _uq_used(user.user_id, period) == DEFAULT_COST, (
        "the reservation itself never touched the UQ counter -- either the "
        "admission wiring assumption in this file's docstring is wrong, or "
        "the tenant's configured default was not picked up"
    )

    _age_hold_to_sweepable(period, ctx.hold_id, ctx.hold_sk)
    assert _sweep(period) == 1

    assert _uq_used(user.user_id, period) == 0


def test_repeated_crash_and_sweep_does_not_accumulate_uq_lockout(dynamodb_mock):
    """The UQ counterpart of the credit-lockout regression test: several
    independent crashed requests, each swept separately, must each return the
    UQ counter to baseline. A partial or missing give-back would accumulate
    every cycle, eventually refusing every request from this user even though
    none of the reserved money was ever actually spent."""
    period = _seed()
    user = _user()
    for cycle in range(3):
        ctx = _reserve(user)
        assert _uq_used(user.user_id, period) == DEFAULT_COST, cycle
        _age_hold_to_sweepable(period, ctx.hold_id, ctx.hold_sk)
        assert _sweep(period) == 1, cycle
        assert _uq_used(user.user_id, period) == 0, cycle


# --------------------------------------------------------------------------- settle


def test_settle_at_or_under_reservation_gives_back_the_difference(dynamodb_mock):
    """I6 table, row 2: settle's delta on `UQ` is `actual - reserved`. An
    ordinary settle where the actual cost is LESS than the reservation must
    leave `used` at exactly the actual amount, not at the full reservation --
    the unused portion of the reservation must be given back the same way the
    per-model quota counter already is."""
    period = _seed()
    user = _user()
    ctx = _reserve(user)
    assert _uq_used(user.user_id, period) == DEFAULT_COST

    actual = DEFAULT_COST - 300_000  # actual < reserved
    _pipeline.settle_reservation_and_log(
        user=user, tenants_repo=ctx.tenants_repo, reservation=DEFAULT_TOKENS,
        actual_input_tokens=1000, actual_output_tokens=200,
        model_id=MODEL, context=ctx, actual_cost_microusd=actual,
    )
    assert _uq_used(user.user_id, period) == actual


def test_settle_overrunning_the_reservation_lands_a_positive_delta(dynamodb_mock):
    """The priority case, G3's own stated exception to admission-time bounding:
    'a settle delta can be positive... `used` can end above the ceiling.' An
    `actual_cost_microusd` GREATER than what was reserved must leave `used`
    at the ACTUAL amount, which here is deliberately ABOVE the configured
    ceiling (`UQ_CEILING`) -- proving the settle adjustment is unconditional
    (no ceiling re-check at settle time), exactly like `quota._adjust_item`
    already is for the per-model wall. A settle that clamped the delta at
    zero, or refused to exceed the ceiling, would leave `used` at
    `DEFAULT_COST` here instead of the larger actual figure asserted below.
    """
    period = _seed(uq_default_microusd=DEFAULT_COST + 500_000)  # tight ceiling
    user = _user()
    ctx = _reserve(user)
    assert _uq_used(user.user_id, period) == DEFAULT_COST

    actual = DEFAULT_COST + 5_000_000  # actual > reserved, and > the ceiling
    _pipeline.settle_reservation_and_log(
        user=user, tenants_repo=ctx.tenants_repo, reservation=DEFAULT_TOKENS,
        actual_input_tokens=5000, actual_output_tokens=2000,
        model_id=MODEL, context=ctx, actual_cost_microusd=actual,
    )
    assert _uq_used(user.user_id, period) == actual


# --------------------------------------------------------------------------- release


def test_release_without_settle_gives_back_the_full_reservation(dynamodb_mock):
    """I6 table, row 3: release's delta on `UQ` is `-reserved`, unconditional
    (the invoke-time-failure path, no spend at all). `used` must return
    exactly to its pre-reservation value -- here, zero, since this is the
    user's only reservation of the period."""
    period = _seed()
    user = _user()
    ctx = _reserve(user)
    assert _uq_used(user.user_id, period) == DEFAULT_COST

    _pipeline.release_pool(ctx)
    assert _uq_used(user.user_id, period) == 0


def test_double_release_does_not_double_give_back(dynamodb_mock):
    """The idempotency counterpart already proven for credit/quota in
    `test_reaper_counter_giveback.py`'s settle-after-observation test: a
    defensive double-release must not drive `used` negative or double-count
    the give-back, mirroring quota's `attribute_exists(used)` no-phantom-row
    guard (I6 requirement 2) applied twice to the same row."""
    period = _seed()
    user = _user()
    ctx = _reserve(user)

    _pipeline.release_pool(ctx)
    assert _uq_used(user.user_id, period) == 0
    _pipeline.release_pool(ctx)
    assert _uq_used(user.user_id, period) == 0
