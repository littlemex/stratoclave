"""Stateful property test for the credit ledger (Fable formal design, main axis).

Drives the REAL moto-backed money path — reserve / settle / defensive re-settle /
release / crash+reap / reap-then-settle — in random interleavings, and checks the
ledger invariants against an independent in-memory reference model AND the live
budget counter after every step. This is the invariant coverage the example-based
test_credit_ledger.py could not give (interleaving of retries, reaper races, and
multiple concurrent holds).

Invariants checked (Phase 1 scope — SETTLE events only; RESERVE/RECLAIM are
Phase 2, so the ledger records ONLY settled-side value today):

  I1  pool_settled_microusd (counter) == Σ settled_delta (ledger)
  RES pool_reserved_microusd (counter) == Σ reserved of live (un-terminated) holds
      — makes release / reap non-vacuous: a reaper that fails to return reserved,
        or a double-return, breaks this.
  TERM each hold that reached a terminal money move has EXACTLY ONE terminal
      ledger event whose settled_delta and settle_reason match what the money
      path was asked to record; a hold that only reserved/released/was-reaped has
      NO terminal event. This replaces a storage-tautology sk-uniqueness check
      with a real detection of sk-overwrite double-counting and wrong values.
  I6  every SETTLE event has settled_delta >= 0
  ref settled total (independent model) == ledger settled total

Phase-1 facts encoded here (Fable review): a crashed+reaped hold writes NO ledger
event and charges NO spend (RECLAIM is Phase 2), so it contributes 0 to settled on
both sides AND returns its reserved. The settle-after-reap race takes the
settled-only txn (reserved_delta=0, settle_reason="reaper_race") because the reaper
already returned the reserved share.
"""
from __future__ import annotations

import time
import uuid

import pytest
from boto3.dynamodb.conditions import Key
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

COST = st.integers(min_value=1, max_value=500_000)   # micro-USD reserved per hold
# actual settle usage as a fraction of the reserved amount; may be 0 (cache-only).
# Kept <= reserved so Phase-1 I1 (no overshoot) holds exactly; overshoot semantics
# are out of Phase-1 scope and deliberately not asserted here.
ACTUAL_FRACTION = st.integers(min_value=0, max_value=100)


class _User:
    def __init__(self, user_id, org_id):
        self.user_id = user_id
        self.org_id = org_id
        self.email = "u@example.com"
        self.roles = ("user",)


class CreditLedgerMachine(RuleBasedStateMachine):
    holds = Bundle("holds")

    @initialize()
    def setup(self):
        from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
        from dynamo.user_tenants import UserTenantsRepository

        # Hypothesis runs many examples inside ONE moto mock (function-scoped
        # fixture), so the DynamoDB tables persist across examples. Isolate each
        # example on its OWN tenant partition (uuid — robust even if the suite is
        # ever run under pytest-xdist) so the ledger/counter start empty and the
        # reference model (ref_settled=0) is valid.
        suffix = uuid.uuid4().hex[:12]
        self.tenant_id = f"acme-{suffix}"
        self.user = _User(f"user-{suffix}", self.tenant_id)
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
        # Independent reference model.
        self.ref_settled = 0                 # micro-USD we EXPECT recorded settled
        self._ctxs = {}                      # bundle token -> pipeline context
        self._live_reserved = {}             # bundle token -> reserved micro-USD (un-terminated)
        # hold_id -> (expected_settled_delta, expected_settle_reason); one entry
        # per hold that reached a terminal money move (settle / reaper_race).
        self._expected_terminal = {}
        self._seq = 0

    # -- helpers -------------------------------------------------------------

    def _ledger(self):
        from dynamo import CreditLedgerRepository

        return CreditLedgerRepository()

    def _pool_summary(self):
        from dynamo.tenant_budgets import TenantBudgetsRepository

        return TenantBudgetsRepository().pool_summary(self.tenant_id, self.period)

    def _pool_settled(self) -> int:
        return int(self._pool_summary()["pool_settled_microusd"])

    def _pool_reserved(self) -> int:
        return int(self._pool_summary()["pool_reserved_microusd"])

    def _ledger_settled(self) -> int:
        return self._ledger().sum_settled_microusd(
            tenant_id=self.tenant_id, period=self.period
        )

    def _all_events(self) -> list[dict]:
        """Every ledger event in this tenant/period partition, pagination-safe.

        (A verification helper that silently truncated at the 1MB page would let
        invariants pass on a partial view — the invariant code itself must not
        have that bug.)"""
        out: list[dict] = []
        led = self._ledger()
        kwargs = {
            "KeyConditionExpression": Key("pk").eq(
                f"TENANT#{self.tenant_id}#P#{self.period}"
            )
        }
        while True:
            resp = led._table.query(**kwargs)
            out.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                return out
            kwargs["ExclusiveStartKey"] = lek

    # -- rules ---------------------------------------------------------------

    @rule(target=holds, cost=COST)
    def reserve(self, cost):
        from mvp._pipeline import reserve_credit

        ctx = reserve_credit(self.user, 4000, pricing_key="opus", cost_microusd=cost)
        # setup guarantees a pool, so the pool path MUST engage — a silent skip
        # here would let the whole machine pass without touching the money path
        # (Fable review critical-1). Assert instead of dropping from the bundle.
        assert ctx.pool_active and ctx.hold_id, (
            "pool path did not engage despite a configured pool — regression"
        )
        # Independent check: the reserved amount recorded IS the cost we asked for
        # (so the reference model does not inherit a reserve-side unit bug).
        assert int(ctx.pool_reserved_microusd) == int(cost), (
            f"reserved {ctx.pool_reserved_microusd} != requested cost {cost}"
        )
        self._seq += 1
        tok = f"h{self._seq}"
        self._ctxs[tok] = ctx
        self._live_reserved[tok] = int(ctx.pool_reserved_microusd)
        return tok

    @rule(tok=consumes(holds), frac=ACTUAL_FRACTION)
    def settle(self, tok, frac):
        from mvp._pipeline import settle_reservation_and_log

        ctx = self._ctxs.pop(tok)
        # actual cost <= reserved (frac% of reserved), so Phase-1 I1 (no overshoot)
        # holds exactly. Priced directly via actual_cost_microusd.
        actual = int((ctx.pool_reserved_microusd * frac) // 100)
        settle_reservation_and_log(
            user=self.user,
            tenants_repo=ctx,
            reservation=ctx.reservation_tokens,
            actual_input_tokens=10,
            actual_output_tokens=5,
            model_id="us.anthropic.claude-opus-4-7",
            context=ctx,
            actual_cost_microusd=actual,
        )
        self.ref_settled += actual
        self._live_reserved.pop(tok)  # reserved returned by the settle txn
        self._expected_terminal[ctx.hold_id] = (actual, "completion")

    @rule(tok=consumes(holds))
    def resettle_defensively(self, tok):
        """A defensive double-settle (error handler + finally, OR a retry worker)
        must be a no-op: the terminal ledger sk dedupes it, so neither counter nor
        ledger moves the second time."""
        from mvp._pipeline import settle_reservation_and_log

        ctx = self._ctxs.pop(tok)
        actual = int(ctx.pool_reserved_microusd // 4)

        def _do_settle():
            settle_reservation_and_log(
                user=self.user, tenants_repo=ctx,
                reservation=ctx.reservation_tokens,
                actual_input_tokens=10, actual_output_tokens=5,
                model_id="us.anthropic.claude-opus-4-7",
                context=ctx, actual_cost_microusd=actual,
            )

        _do_settle()
        settled_after_first = self._pool_settled()
        # Defeat the in-process once-guard so the SECOND call actually re-runs the
        # transaction and the DB-level attribute_not_exists is what must dedupe.
        # Assert the guard attr exists first: a rename must FAIL loudly here rather
        # than silently create a new attribute and leave the guard alive, which
        # would turn "DB dedup verified" into "in-process guard verified" (Fable
        # review high — whitebox fragility).
        assert hasattr(ctx, "_pool_finalized"), (
            "once-guard attribute renamed — this test must be updated so it still "
            "exercises the DB-level dedup, not the in-process guard"
        )
        ctx._pool_finalized = False
        _do_settle()

        # DB dedup effect: exactly one terminal event, counter did not advance.
        assert self._pool_settled() == settled_after_first, (
            "re-settle advanced the counter — DB dedup failed"
        )
        self.ref_settled += actual  # counted ONCE despite two settle calls
        self._live_reserved.pop(tok)
        self._expected_terminal[ctx.hold_id] = (actual, "completion")

    @rule(tok=consumes(holds))
    def release(self, tok):
        """Invoke-time failure: release, never settle → no settled value, reserved
        returned, no terminal ledger event in Phase 1."""
        from mvp._pipeline import release_pool

        ctx = self._ctxs.pop(tok)
        release_pool(ctx)
        self._live_reserved.pop(tok)  # reserved returned; RES invariant checks it
        # no terminal event expected.

    @rule(tok=consumes(holds))
    def crash_then_reap(self, tok):
        """Owner crashes between reserve and settle; the lazy sweep reclaims the
        hold. Phase-1 reaper writes NO ledger event and charges NO spend, so the
        reaped hold contributes 0 to settled AND returns its reserved."""
        ctx = self._ctxs.pop(tok)
        self._force_reap(ctx)
        self._live_reserved.pop(tok)  # reaper returned reserved; no terminal event.

    @rule(tok=consumes(holds), frac=ACTUAL_FRACTION)
    def reap_then_settle(self, tok, frac):
        """The reaper reclaims the hold, THEN a late settle arrives (the owner was
        merely slow, not dead). settle finds the hold gone and must take the
        settled-only txn: record the spend with reserved_delta=0 /
        settle_reason='reaper_race', because the reaper already returned reserved
        (Fable review critical-2 — this branch was previously uncovered by the
        stateful machine)."""
        from mvp._pipeline import settle_reservation_and_log

        ctx = self._ctxs.pop(tok)
        actual = int((ctx.pool_reserved_microusd * frac) // 100)
        self._force_reap(ctx)               # reserved returned by the reaper
        self._live_reserved.pop(tok)
        settle_reservation_and_log(
            user=self.user, tenants_repo=ctx,
            reservation=ctx.reservation_tokens,
            actual_input_tokens=10, actual_output_tokens=5,
            model_id="us.anthropic.claude-opus-4-7",
            context=ctx, actual_cost_microusd=actual,
        )
        self.ref_settled += actual
        self._expected_terminal[ctx.hold_id] = (actual, "reaper_race")
        # Assert the settled-only shape directly on the written event.
        ev = [e for e in self._all_events()
              if e.get("hold_id") == ctx.hold_id and e["sk"].endswith("#TERMINAL")]
        assert len(ev) == 1, f"expected one terminal event, got {len(ev)}"
        assert int(ev[0]["reserved_delta_microusd"]) == 0, (
            "settled-only txn must NOT re-release reserved (reaper already did)"
        )
        assert ev[0].get("settle_reason") == "reaper_race"

    # -- shared reap mechanism ----------------------------------------------

    def _force_reap(self, ctx):
        """Rewrite the hold row's embedded expiry into the past and run the sweep,
        so the reaper reclaims it. Asserts the row existed and is gone afterwards
        — a schema drift that made this a no-op would otherwise pass vacuously
        (Fable review high)."""
        from dynamo.tenant_budgets import TenantBudgetsRepository, hold_sk as _hsk

        budgets = TenantBudgetsRepository()
        item = budgets._table.get_item(
            Key={"tenant_id": self.tenant_id, "sk": ctx.hold_sk}
        ).get("Item")
        assert item is not None, (
            f"hold row {ctx.hold_sk} not found — hold_sk convention drifted"
        )
        past = int(time.time()) - 10_000
        new_sk = _hsk(self.period, past, ctx.hold_id)
        item["sk"] = new_sk
        item["expires_at"] = past
        budgets._table.delete_item(
            Key={"tenant_id": self.tenant_id, "sk": ctx.hold_sk}
        )
        budgets._table.put_item(Item=item)
        # keep the context's view of where the (now past-dated) row lives.
        ctx.hold_sk = new_sk

        from mvp._pipeline import _sweep_expired_holds

        _sweep_expired_holds(budgets, self.tenant_id, self.period)
        gone = budgets._table.get_item(
            Key={"tenant_id": self.tenant_id, "sk": new_sk}
        ).get("Item")
        assert gone is None, "sweep did not reclaim the expired hold row"

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
    def reserved_counter_matches_live_holds(self):
        # RES: the reserved counter equals the sum of reserved over holds that
        # have NOT yet reached a terminal move. Catches a reaper/release that
        # fails to return reserved, or returns it twice.
        expected = sum(self._live_reserved.values())
        assert self._pool_reserved() == expected, (
            f"RES broken: pool_reserved={self._pool_reserved()} "
            f"!= Σ live reserved={expected}"
        )

    @invariant()
    def terminal_events_complete_and_correct(self):
        # TERM + I6: each hold that reached a terminal move has exactly one
        # terminal event with the expected settled_delta and settle_reason; no
        # extra/ghost terminal events; every SETTLE settled_delta >= 0. This is
        # the real replacement for the storage-tautology sk-uniqueness check.
        by_hold: dict[str, list[dict]] = {}
        for it in self._all_events():
            if not it["sk"].endswith("#TERMINAL"):
                continue
            by_hold.setdefault(it["hold_id"], []).append(it)

        for hold_id, evs in by_hold.items():
            assert len(evs) == 1, (
                f"TERM broken: hold {hold_id} has {len(evs)} terminal events"
            )
            ev = evs[0]
            assert int(ev["settled_delta_microusd"]) >= 0, "I6 broken: negative settled"
            assert hold_id in self._expected_terminal, (
                f"ghost terminal event for un-terminated hold {hold_id}"
            )
            exp_settled, exp_reason = self._expected_terminal[hold_id]
            assert int(ev["settled_delta_microusd"]) == exp_settled, (
                f"terminal settled {ev['settled_delta_microusd']} != expected {exp_settled}"
            )
            assert ev.get("settle_reason") == exp_reason, (
                f"settle_reason {ev.get('settle_reason')} != expected {exp_reason}"
            )
        # Every hold we terminated must have produced its event.
        missing = set(self._expected_terminal) - set(by_hold)
        assert not missing, f"expected terminal events missing for holds {missing}"


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
