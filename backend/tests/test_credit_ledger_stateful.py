"""Stateful property test for the credit ledger (Fable formal design, main axis).

Drives the REAL moto-backed money path — reserve / settle / release / crash+reap
— in random interleavings, and checks the ledger invariants against both an
independent in-memory reference model and the live budget counter after every
step. This is the invariant coverage the example-based test_credit_ledger.py
could not give (interleaving of retries, reaper races, and multiple concurrent
holds).

Invariants checked (Phase 1 scope — SETTLE events only; RESERVE/RECLAIM are
Phase 2, so the ledger records ONLY settled-side value today):

  I1  pool_settled_microusd (counter) == Σ settled_delta (ledger)
  I3  each hold has at most ONE terminal ledger event (the sk is unique)
  I6  every SETTLE event has settled_delta >= 0
  ref settled total (independent model) == ledger settled total

A crashed+reaped hold contributes 0 to settled on BOTH sides (Phase 1 reaper
writes no ledger event and charges no spend), so I1 must survive crashes too.
"""
from __future__ import annotations

import time

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    consumes,
    initialize,
    invariant,
    multiple,
    rule,
)

COST = st.integers(min_value=1, max_value=500_000)   # micro-USD reserved per hold
# actual settle usage in micro-USD; may be 0 (cache-only) but not exceed reserve
# for this Phase-1 test (overshoot accounting is exercised elsewhere).
ACTUAL_FRACTION = st.integers(min_value=0, max_value=100)


class _User:
    def __init__(self, user_id, org_id):
        self.user_id = user_id
        self.org_id = org_id
        self.email = "u@example.com"
        self.roles = ("user",)


class CreditLedgerMachine(RuleBasedStateMachine):
    holds = Bundle("holds")

    _run_counter = 0

    @initialize()
    def setup(self):
        from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
        from dynamo.user_tenants import UserTenantsRepository

        # Hypothesis runs many examples inside ONE moto mock (function-scoped
        # fixture), so the DynamoDB tables persist across examples. Isolate each
        # example on its OWN tenant partition so the ledger/counter start empty
        # and the reference model (ref_settled=0) is valid.
        CreditLedgerMachine._run_counter += 1
        self.tenant_id = f"acme-{CreditLedgerMachine._run_counter}"
        self.user = _User(f"user-{CreditLedgerMachine._run_counter}", self.tenant_id)
        self.period = current_period()
        UserTenantsRepository().ensure(
            user_id=self.user.user_id, tenant_id=self.tenant_id,
            role="user", total_credit=1_000_000_000,
        )
        # A generous pool so the ceiling is not the thing under test here.
        TenantBudgetsRepository().set_pool_limit(
            tenant_id=self.tenant_id, period=self.period,
            pool_limit_microusd=10_000_000_000,
        )
        # Independent reference: settled micro-USD we EXPECT recorded.
        self.ref_settled = 0
        # ctxs by a synthetic id so the bundle can carry a hashable token.
        self._ctxs = {}
        self._seq = 0

    # -- helpers -------------------------------------------------------------

    def _ledger(self):
        from dynamo import CreditLedgerRepository

        return CreditLedgerRepository()

    def _pool_settled(self) -> int:
        from dynamo.tenant_budgets import TenantBudgetsRepository

        s = TenantBudgetsRepository().pool_summary(self.tenant_id, self.period)
        return int(s["pool_settled_microusd"])

    def _ledger_settled(self) -> int:
        return self._ledger().sum_settled_microusd(
            tenant_id=self.tenant_id, period=self.period
        )

    # -- rules ---------------------------------------------------------------

    @rule(target=holds, cost=COST)
    def reserve(self, cost):
        from mvp._pipeline import reserve_credit

        ctx = reserve_credit(self.user, 4000, pricing_key="opus", cost_microusd=cost)
        # A pooled reserve must produce a hold; if the pool path was skipped the
        # ledger contract does not apply, so drop it from the bundle.
        if not (ctx.pool_active and ctx.hold_id):
            return multiple()
        self._seq += 1
        tok = f"h{self._seq}"
        self._ctxs[tok] = ctx
        return tok

    @rule(tok=consumes(holds), frac=ACTUAL_FRACTION)
    def settle(self, tok, frac):
        from mvp._pipeline import settle_reservation_and_log

        ctx = self._ctxs.pop(tok)
        # Choose an actual cost <= reserved (frac% of the reserved amount), so
        # Phase-1 I1 (no overshoot) holds exactly. Priced directly, bypassing
        # token→price derivation, by passing actual_cost_microusd.
        actual = (ctx.pool_reserved_microusd * frac) // 100
        settle_reservation_and_log(
            user=self.user,
            tenants_repo=ctx,
            reservation=ctx.reservation_tokens,
            actual_input_tokens=10,
            actual_output_tokens=5,
            model_id="us.anthropic.claude-opus-4-7",
            context=ctx,
            actual_cost_microusd=int(actual),
        )
        self.ref_settled += int(actual)

    @rule(tok=consumes(holds))
    def resettle_defensively(self, tok):
        """A defensive double-settle (error handler + finally) must be a no-op:
        the terminal ledger sk dedupes it, so neither counter nor ledger moves."""
        from mvp._pipeline import settle_reservation_and_log

        ctx = self._ctxs[tok]  # keep in bundle: settle once, then again
        actual = ctx.pool_reserved_microusd // 4
        for i in range(2):
            if i == 1:
                ctx._pool_finalized = False  # defeat the once-guard
            settle_reservation_and_log(
                user=self.user, tenants_repo=ctx,
                reservation=ctx.reservation_tokens,
                actual_input_tokens=10, actual_output_tokens=5,
                model_id="us.anthropic.claude-opus-4-7",
                context=ctx, actual_cost_microusd=int(actual),
            )
        self._ctxs.pop(tok)
        self.ref_settled += int(actual)  # counted ONCE despite two settle calls

    @rule(tok=consumes(holds))
    def release(self, tok):
        """Invoke-time failure: release, never settle → no settled value."""
        from mvp._pipeline import release_pool

        ctx = self._ctxs.pop(tok)
        release_pool(ctx)
        # contributes 0 to settled on both sides.

    @rule(tok=consumes(holds))
    def crash_then_reap(self, tok):
        """Owner crashes between reserve and settle; the lazy sweep reclaims the
        hold. Phase-1 reaper writes NO ledger event and charges NO spend, so the
        reaped hold contributes 0 to settled on both sides — I1 must survive."""
        from dynamo.tenant_budgets import TenantBudgetsRepository, budget_sk
        from mvp._pipeline import _sweep_expired_holds

        ctx = self._ctxs.pop(tok)
        budgets = TenantBudgetsRepository()
        # Force the hold's embedded expiry into the past so the sweep reclaims it.
        # (The hold row's sk embeds expires_at; rewrite the row with a past epoch.)
        past = int(time.time()) - 10_000
        item = budgets._table.get_item(
            Key={"tenant_id": self.tenant_id, "sk": ctx.hold_sk}
        ).get("Item")
        if item:
            from dynamo.tenant_budgets import hold_sk as _hsk

            new_sk = _hsk(self.period, past, ctx.hold_id)
            item["sk"] = new_sk
            item["expires_at"] = past
            budgets._table.delete_item(
                Key={"tenant_id": self.tenant_id, "sk": ctx.hold_sk}
            )
            budgets._table.put_item(Item=item)
        _sweep_expired_holds(budgets, self.tenant_id, self.period)
        # reaped hold: 0 settled contribution.

    # -- invariants ----------------------------------------------------------

    @invariant()
    def i1_counter_equals_ledger(self):
        assert self._pool_settled() == self._ledger_settled(), (
            f"I1 broken: counter={self._pool_settled()} "
            f"ledger={self._ledger_settled()}"
        )

    @invariant()
    def ref_matches_ledger(self):
        assert self.ref_settled == self._ledger_settled(), (
            f"reference settled={self.ref_settled} != ledger={self._ledger_settled()}"
        )

    @invariant()
    def i3_i6_terminal_unique_and_nonneg(self):
        # Every event: at most one terminal sk per hold (unique sk = uniqueness
        # by construction), and settled_delta >= 0 (I6).
        from boto3.dynamodb.conditions import Key

        led = self._ledger()
        resp = led._table.query(
            KeyConditionExpression=Key("pk").eq(
                f"TENANT#{self.tenant_id}#P#{self.period}"
            )
        )
        seen = set()
        for it in resp.get("Items", []):
            sk = it["sk"]
            assert sk not in seen, f"I3 broken: duplicate ledger sk {sk}"
            seen.add(sk)
            assert int(it["settled_delta_microusd"]) >= 0, "I6 broken: negative settled"


# moto + the per-example fixture reset: hypothesis stateful needs the DynamoDB
# mock fresh per run. Bind the dynamodb_mock fixture so each machine run gets
# clean tables (the suppress lets the function-scoped fixture drive the mock).
TestCreditLedgerStateful = CreditLedgerMachine.TestCase
TestCreditLedgerStateful.settings = settings(
    max_examples=25,
    stateful_step_count=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@pytest.fixture(autouse=True)
def _bind_mock(dynamodb_mock):
    # Ensures moto tables exist for every stateful example.
    yield
