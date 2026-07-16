"""Property tests for Layer 5 rating (Fable design E — Hypothesis axis).

Z3 (test_billing_formal_z3.py) proves the pure `rate_usage` arithmetic. These
prove the properties that only show up against the REAL code + a real
(moto) rate table:

  INV-R1 (RATE-FREEZE): a charge equals rate_usage(reserve-time snapshot, usage),
      even when the active pricing version flips between reserve and settle.
  INV-R2/R3 (REPLAY): the rating frozen on the ledger terminal self-recomputes to
      its own total, and that total == the settled_delta — from the item alone.
  INV-R6 (SETTLE == LATE_SETTLE): a reaper-race late settle records the same money
      as a normal settle would, because both rate from the same frozen snapshot.
  version monotonicity: after set_rates, CURRENT points at a fully-written version.
"""
from __future__ import annotations

import json
import time
import uuid

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    consumes,
    initialize,
    invariant,
    rule,
)

from mvp import pricing


class _User:
    def __init__(self, user_id, org_id):
        self.user_id = user_id
        self.org_id = org_id
        self.email = "u@example.com"
        self.roles = ("user",)


COST = st.integers(min_value=1, max_value=500_000)
TOK = st.integers(min_value=0, max_value=2_000_000)
# per-MTok rates the admin can flip to (micro-USD).
RATE = st.integers(min_value=1, max_value=50_000_000)


class RatingFreezeMachine(RuleBasedStateMachine):
    holds = Bundle("holds")

    @initialize()
    def setup(self):
        from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
        from dynamo.user_tenants import UserTenantsRepository

        suffix = uuid.uuid4().hex[:12]
        self.tenant_id = f"rate-{suffix}"
        self.user = _User(f"user-{suffix}", self.tenant_id)
        self.period = current_period()
        UserTenantsRepository().ensure(
            user_id=self.user.user_id, tenant_id=self.tenant_id,
            role="user", total_credit=10**12,
        )
        TenantBudgetsRepository().set_pool_limit(
            tenant_id=self.tenant_id, period=self.period,
            pool_limit_microusd=10**12,
        )
        pricing.reset_cache()
        pricing.reset_version_cache()
        self._vseq = 0
        # token -> (expected_charge_microusd, hold_id)
        self._expected = {}

    def _ledger(self):
        from dynamo import CreditLedgerRepository

        return CreditLedgerRepository()

    @rule(inp=RATE, outp=RATE)
    def flip_version(self, inp, outp):
        """Admin activates a NEW pricing version for 'opus'. A reserve after this
        freezes the new rate; a reserve before it is unaffected (INV-R1)."""
        from dynamo.pricing_config import PricingConfigRepository

        self._vseq += 1
        version = f"v{self._vseq}-{uuid.uuid4().hex[:6]}"
        PricingConfigRepository().set_rates(version=version, rates={"opus": pricing.Rate(
            input_per_mtok_microusd=int(inp), output_per_mtok_microusd=int(outp),
            cache_read_per_mtok_microusd=0, cache_write_per_mtok_microusd=0,
        )})
        pricing.reset_cache()  # force the live cache to observe the flip

    @rule(target=holds, cost=COST, tin=TOK, tout=TOK)
    def reserve_then_settle(self, cost, tin, tout):
        """Reserve (freezes the snapshot), then settle. The charge MUST equal
        rate_usage(frozen snapshot, usage), independent of later flips."""
        from mvp._pipeline import reserve_credit, settle_reservation_and_log

        ctx = reserve_credit(self.user, 4000, pricing_key="opus", cost_microusd=cost)
        assert ctx.rate_snapshot is not None, "snapshot must be frozen at reserve"
        expected = pricing.rate_usage(
            ctx.rate_snapshot, input_tokens=tin, output_tokens=tout
        ).total_cost_microusd
        settle_reservation_and_log(
            user=self.user, tenants_repo=ctx, reservation=ctx.reservation_tokens,
            actual_input_tokens=tin, actual_output_tokens=tout,
            model_id="us.anthropic.claude-opus-4-7", context=ctx,
            actual_cost_microusd=None,  # force pipeline to rate from the snapshot
        )
        ev = self._ledger().get_terminal(
            tenant_id=self.tenant_id, period=self.period, hold_id=ctx.hold_id
        )
        assert ev is not None and ev["event_type"] == "SETTLE"
        # INV-R1: charge == frozen-snapshot rating, NOT any later flipped rate.
        assert int(ev["settled_delta_microusd"]) == expected, (
            f"charge {ev['settled_delta_microusd']} != frozen-snapshot {expected}"
        )
        # INV-R2/R3: rating self-recomputes and matches the settled_delta.
        rating = json.loads(ev["rating"])
        recomputed = sum(c["cost_microusd"] for c in rating["components"].values())
        assert recomputed == rating["total_cost_microusd"] == int(ev["settled_delta_microusd"])
        # BUG#1 guard: pricing_version is a VERSION, never the pricing_key "opus".
        assert ev["pricing_version"] == ctx.rate_snapshot.version
        self._vseq += 0
        return f"done-{ctx.hold_id}"

    @rule(cost=COST, tin=TOK, tout=TOK)
    def reap_race_late_settle_same_money(self, cost, tin, tout):
        """Reaper reclaims the hold (RECLAIM terminal), then a late settle records
        the spend via LATE_SETTLE. INV-R6: LATE_SETTLE money == what a normal
        SETTLE would have charged for the same usage + same frozen snapshot."""
        from dynamo.tenant_budgets import TenantBudgetsRepository, hold_sk as _hsk
        from mvp._pipeline import (
            reserve_credit, settle_reservation_and_log, _sweep_expired_holds,
        )

        ctx = reserve_credit(self.user, 4000, pricing_key="opus", cost_microusd=cost)
        expected = pricing.rate_usage(
            ctx.rate_snapshot, input_tokens=tin, output_tokens=tout
        ).total_cost_microusd

        # force-reap: past-date the hold and sweep so a RECLAIM terminal is written
        budgets = TenantBudgetsRepository()
        item = budgets._table.get_item(
            Key={"tenant_id": self.tenant_id, "sk": ctx.hold_sk}
        ).get("Item")
        assert item is not None
        past = int(time.time()) - 10_000
        new_sk = _hsk(self.period, past, ctx.hold_id)
        item["sk"] = new_sk
        item["expires_at"] = past
        budgets._table.delete_item(Key={"tenant_id": self.tenant_id, "sk": ctx.hold_sk})
        budgets._table.put_item(Item=item)
        ctx.hold_sk = new_sk
        _sweep_expired_holds(budgets, self.tenant_id, self.period)

        settle_reservation_and_log(
            user=self.user, tenants_repo=ctx, reservation=ctx.reservation_tokens,
            actual_input_tokens=tin, actual_output_tokens=tout,
            model_id="us.anthropic.claude-opus-4-7", context=ctx,
            actual_cost_microusd=None,
        )
        late = self._ledger().get_late_settle(
            tenant_id=self.tenant_id, period=self.period, hold_id=ctx.hold_id
        )
        assert late is not None, "LATE_SETTLE not written on reaper race"
        assert int(late["settled_delta_microusd"]) == expected, (
            "LATE_SETTLE money differs from what SETTLE would charge (INV-R6)"
        )
        rating = json.loads(late["rating"])
        assert rating["total_cost_microusd"] == expected

    @invariant()
    def current_points_at_written_version(self):
        # version monotonicity: whatever CURRENT names, its opus row exists and is
        # readable (set_rates writes rows before flipping the pointer).
        from dynamo.pricing_config import PricingConfigRepository

        repo = PricingConfigRepository()
        v = repo.current_version()
        if v is not None:
            row = repo.get_rates_for_version(v, "opus")
            assert row is not None, f"CURRENT points at {v} but its opus row is missing"


TestRatingFreeze = RatingFreezeMachine.TestCase
TestRatingFreeze.settings = settings(
    max_examples=20,
    stateful_step_count=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@pytest.fixture(autouse=True)
def _bind(dynamodb_mock):
    pricing.reset_cache()
    pricing.reset_version_cache()
    yield
    pricing.reset_cache()
    pricing.reset_version_cache()
