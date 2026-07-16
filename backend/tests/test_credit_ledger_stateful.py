"""Stateful property test for the credit ledger — Phase 2 (Fable formal design).

Drives the REAL moto-backed money path — reserve / settle / defensive re-settle /
release / crash+reap / reap-then-late-settle / late-settle retry / release-then-
settle — in random interleavings, and checks the Phase-2 ledger invariants against
an independent in-memory reference model AND the live budget counters after every
step.

Phase 2 ledger model (vs Phase 1's SETTLE-only ledger):
  - RESERVE event (own sk, positive reserved_delta) on every pooled reserve.
  - ONE terminal per hold on the shared sk EV#HOLD#<id>#TERMINAL, event_type in
    {SETTLE, RELEASE, RECLAIM}, reserved_delta = -reserved (returns the reserve).
  - LATE_SETTLE event (DISTINCT sk, reserved_delta ≡ 0) recovers the spend when a
    settle loses the terminal race to the reaper's RECLAIM — the revenue-leak fix.

Invariants (Fable design C, Phase-2 forms):
  I1'  pool_settled  == Σ SETTLE.settled_delta + Σ LATE_SETTLE.settled_delta
  I2   pool_reserved == Σ RESERVE.reserved_delta + Σ terminal.reserved_delta
                     (= Σ reserved of live holds)
  I3   pool_reclaimed == Σ RECLAIM.reserved returned (= -Σ RECLAIM.reserved_delta)
  TERM' each hold: ≤1 terminal (type∈{SETTLE,RELEASE,RECLAIM}); ≤1 LATE_SETTLE;
                   LATE_SETTLE exists ⇒ terminal is RECLAIM; per-event values match
                   the recorded expectation; no ghost / missing events.
  NONNEG all three counters ≥ 0.
  EXACTLY-ONCE-SPEND  the model of "settles that returned success" equals the
                   ledger's derived settled total (the direct detector of the
                   Phase-1 revenue-leak this phase closes).

Out of scope, deliberately NOT asserted (documented): overshoot (actual >
reserved) semantics; token→price derivation (settle is priced directly).
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
        # fixture), so tables persist across examples. Isolate each example on its
        # OWN tenant partition (uuid) so counters/ledger start empty.
        suffix = uuid.uuid4().hex[:12]
        self.tenant_id = f"acme-{suffix}"
        self.user = _User(f"user-{suffix}", self.tenant_id)
        self.period = current_period()
        UserTenantsRepository().ensure(
            user_id=self.user.user_id, tenant_id=self.tenant_id,
            role="user", total_credit=1_000_000_000,
        )
        TenantBudgetsRepository().set_pool_limit(
            tenant_id=self.tenant_id, period=self.period,
            pool_limit_microusd=10_000_000_000,
        )
        # Independent reference model.
        self.ref_settled = 0                 # micro-USD we EXPECT recorded settled
        self._ctxs = {}                      # bundle token -> pipeline context
        self._live_reserved = {}             # bundle token -> reserved (un-terminated)
        # hold_id -> (terminal_type, reserved_returned, settled_on_terminal)
        self._expected_terminal = {}
        # hold_id -> expected LATE_SETTLE settled amount
        self._expected_late = {}
        # every reserved hold_id (RESERVE event must exist for each) -> reserved
        self._reserved_events = {}
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

    def _pool_reclaimed(self) -> int:
        # pool_summary does not surface reclaimed; read the row directly.
        from dynamo.tenant_budgets import TenantBudgetsRepository, budget_sk

        row = TenantBudgetsRepository()._table.get_item(
            Key={"tenant_id": self.tenant_id, "sk": budget_sk(self.period)}
        ).get("Item") or {}
        return int(row.get("pool_reclaimed_microusd", 0))

    def _all_events(self) -> list[dict]:
        """Every ledger event in this tenant/period partition, pagination-safe."""
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

    def _events_by_kind(self):
        """Split the partition into (reserve, terminal, late) dicts keyed by
        hold_id, asserting sk-namespace uniqueness within each kind."""
        reserve, terminal, late = {}, {}, {}
        for e in self._all_events():
            hid = e["hold_id"]
            sk = e["sk"]
            if sk.endswith("#RESERVE"):
                assert hid not in reserve, f"duplicate RESERVE for {hid}"
                reserve[hid] = e
            elif sk.endswith("#TERMINAL"):
                assert hid not in terminal, f"duplicate TERMINAL for {hid}"
                terminal[hid] = e
            elif sk.endswith("#LATE_SETTLE"):
                assert hid not in late, f"duplicate LATE_SETTLE for {hid}"
                late[hid] = e
            else:
                raise AssertionError(f"unknown ledger event sk {sk}")
        return reserve, terminal, late

    # -- rules ---------------------------------------------------------------

    @rule(target=holds, cost=COST)
    def reserve(self, cost):
        from mvp._pipeline import reserve_credit

        ctx = reserve_credit(self.user, 4000, pricing_key="opus", cost_microusd=cost)
        assert ctx.pool_active and ctx.hold_id, (
            "pool path did not engage despite a configured pool — regression"
        )
        assert int(ctx.pool_reserved_microusd) == int(cost), (
            f"reserved {ctx.pool_reserved_microusd} != requested cost {cost}"
        )
        self._seq += 1
        tok = f"h{self._seq}"
        self._ctxs[tok] = ctx
        self._live_reserved[tok] = int(ctx.pool_reserved_microusd)
        self._reserved_events[ctx.hold_id] = int(ctx.pool_reserved_microusd)
        return tok

    @rule(tok=consumes(holds), frac=ACTUAL_FRACTION)
    def settle(self, tok, frac):
        from mvp._pipeline import settle_reservation_and_log

        ctx = self._ctxs.pop(tok)
        actual = int((ctx.pool_reserved_microusd * frac) // 100)
        settle_reservation_and_log(
            user=self.user, tenants_repo=ctx,
            reservation=ctx.reservation_tokens,
            actual_input_tokens=10, actual_output_tokens=5,
            model_id="us.anthropic.claude-opus-4-7",
            context=ctx, actual_cost_microusd=actual,
        )
        self.ref_settled += actual
        self._live_reserved.pop(tok)
        self._expected_terminal[ctx.hold_id] = (
            "SETTLE", int(ctx.pool_reserved_microusd), actual
        )

    @rule(tok=consumes(holds))
    def resettle_defensively(self, tok):
        """Defensive double-settle (error handler + finally / retry) is a no-op:
        the terminal sk dedupes it — counter and ledger move once."""
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
        assert hasattr(ctx, "_pool_finalized"), (
            "once-guard attribute renamed — update this test so it still exercises "
            "the DB-level dedup, not the in-process guard"
        )
        ctx._pool_finalized = False
        _do_settle()
        assert self._pool_settled() == settled_after_first, (
            "re-settle advanced the counter — DB dedup failed"
        )
        self.ref_settled += actual
        self._live_reserved.pop(tok)
        self._expected_terminal[ctx.hold_id] = (
            "SETTLE", int(ctx.pool_reserved_microusd), actual
        )

    @rule(tok=consumes(holds))
    def release(self, tok):
        """Invoke-time failure: release, never settle → reserved returned, a
        RELEASE terminal recorded (Phase 2), no settled value."""
        from mvp._pipeline import release_pool

        ctx = self._ctxs.pop(tok)
        release_pool(ctx)
        self._live_reserved.pop(tok)
        self._expected_terminal[ctx.hold_id] = (
            "RELEASE", int(ctx.pool_reserved_microusd), 0
        )

    @rule(tok=consumes(holds))
    def crash_then_reap(self, tok):
        """Owner crashes between reserve and settle; the lazy sweep reclaims the
        hold. Phase 2: the reaper writes a RECLAIM terminal (reserved returned,
        settled 0) in the same txn as the counter move."""
        ctx = self._ctxs.pop(tok)
        self._force_reap(ctx)
        self._live_reserved.pop(tok)
        self._expected_terminal[ctx.hold_id] = (
            "RECLAIM", int(ctx.pool_reserved_microusd), 0
        )

    @rule(tok=consumes(holds), frac=ACTUAL_FRACTION)
    def reap_then_late_settle(self, tok, frac):
        """The reaper reclaims the hold (RECLAIM terminal, reserved returned), THEN
        a late settle arrives. settle finds the terminal is a RECLAIM and records
        the spend via a LATE_SETTLE on a DISTINCT sk (reserved_delta=0) — the
        Phase-2 revenue-leak fix. The RECLAIM terminal stays; a LATE_SETTLE is
        added."""
        from mvp._pipeline import settle_reservation_and_log

        ctx = self._ctxs.pop(tok)
        actual = int((ctx.pool_reserved_microusd * frac) // 100)
        self._force_reap(ctx)               # RECLAIM terminal, reserved returned
        self._live_reserved.pop(tok)
        self._expected_terminal[ctx.hold_id] = (
            "RECLAIM", int(ctx.pool_reserved_microusd), 0
        )
        settle_reservation_and_log(
            user=self.user, tenants_repo=ctx,
            reservation=ctx.reservation_tokens,
            actual_input_tokens=10, actual_output_tokens=5,
            model_id="us.anthropic.claude-opus-4-7",
            context=ctx, actual_cost_microusd=actual,
        )
        self.ref_settled += actual
        self._expected_late[ctx.hold_id] = actual
        # Assert the LATE_SETTLE shape directly.
        _, terminal, late = self._events_by_kind()
        assert terminal[ctx.hold_id]["event_type"] == "RECLAIM"
        assert ctx.hold_id in late, "LATE_SETTLE not written after reaper race"
        lev = late[ctx.hold_id]
        assert int(lev["reserved_delta_microusd"]) == 0, (
            "LATE_SETTLE must not touch reserved (reaper already returned it)"
        )
        assert int(lev["settled_delta_microusd"]) == actual

    @rule(tok=consumes(holds), frac=ACTUAL_FRACTION)
    def reap_then_late_settle_retry(self, tok, frac):
        """A late settle after a RECLAIM, invoked TWICE (retry worker): exactly one
        LATE_SETTLE, settled counted once. Proves the LATE_SETTLE sk dedup."""
        from mvp._pipeline import settle_reservation_and_log

        ctx = self._ctxs.pop(tok)
        actual = int((ctx.pool_reserved_microusd * frac) // 100)
        self._force_reap(ctx)
        self._live_reserved.pop(tok)
        self._expected_terminal[ctx.hold_id] = (
            "RECLAIM", int(ctx.pool_reserved_microusd), 0
        )

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
        ctx._pool_finalized = False   # force the second invocation to re-run
        _do_settle()
        assert self._pool_settled() == settled_after_first, (
            "LATE_SETTLE retry advanced the counter — sk dedup failed"
        )
        self.ref_settled += actual
        self._expected_late[ctx.hold_id] = actual

    @rule(tok=consumes(holds), frac=ACTUAL_FRACTION)
    def release_then_settle(self, tok, frac):
        """A settle after an explicit RELEASE is a protocol violation: it must NOT
        be billed. The terminal is RELEASE, so no LATE_SETTLE is written and the
        counter does not move."""
        from mvp._pipeline import release_pool, settle_reservation_and_log

        ctx = self._ctxs.pop(tok)
        release_pool(ctx)
        self._live_reserved.pop(tok)
        self._expected_terminal[ctx.hold_id] = (
            "RELEASE", int(ctx.pool_reserved_microusd), 0
        )
        settled_before = self._pool_settled()
        ctx._pool_finalized = False   # force the late settle to attempt
        actual = int((ctx.pool_reserved_microusd * frac) // 100)
        settle_reservation_and_log(
            user=self.user, tenants_repo=ctx,
            reservation=ctx.reservation_tokens,
            actual_input_tokens=10, actual_output_tokens=5,
            model_id="us.anthropic.claude-opus-4-7",
            context=ctx, actual_cost_microusd=actual,
        )
        # No billing after release.
        assert self._pool_settled() == settled_before, (
            "settle after release charged spend — protocol violation"
        )
        _, _, late = self._events_by_kind()
        assert ctx.hold_id not in late, "LATE_SETTLE written after a RELEASE"

    @rule(tok=consumes(holds), frac=ACTUAL_FRACTION)
    def expire_then_settle_then_sweep(self, tok, frac):
        """The hold expires but the (slow) owner settles BEFORE the sweep runs;
        then the sweep runs. settle wins the terminal race → SETTLE terminal; the
        later reaper finds the terminal taken → its RECLAIM Put CCFs → no-op (no
        double-charge, no double-return, no RECLAIM terminal)."""
        from mvp._pipeline import _sweep_expired_holds, settle_reservation_and_log
        from dynamo.tenant_budgets import TenantBudgetsRepository

        ctx = self._ctxs.pop(tok)
        self._expire_hold_row(ctx)  # past-date the lease, DO NOT sweep yet
        actual = int((ctx.pool_reserved_microusd * frac) // 100)
        settle_reservation_and_log(
            user=self.user, tenants_repo=ctx,
            reservation=ctx.reservation_tokens,
            actual_input_tokens=10, actual_output_tokens=5,
            model_id="us.anthropic.claude-opus-4-7",
            context=ctx, actual_cost_microusd=actual,
        )
        self.ref_settled += actual
        self._live_reserved.pop(tok)
        self._expected_terminal[ctx.hold_id] = (
            "SETTLE", int(ctx.pool_reserved_microusd), actual
        )
        settled_before = self._pool_settled()
        reserved_before = self._pool_reserved()
        reclaimed_before = self._pool_reclaimed()
        budgets = TenantBudgetsRepository()
        _sweep_expired_holds(budgets, self.tenant_id, self.period)
        assert self._pool_settled() == settled_before, "sweep double-charged"
        assert self._pool_reserved() == reserved_before, "sweep double-returned reserved"
        assert self._pool_reclaimed() == reclaimed_before, (
            "sweep reclaimed an already-settled hold"
        )
        _, terminal, _ = self._events_by_kind()
        assert terminal[ctx.hold_id]["event_type"] == "SETTLE", (
            "reaper overwrote the SETTLE terminal"
        )

    @rule(tok=consumes(holds))
    def double_reap(self, tok):
        """The sweep runs twice on the same expired hold: reclaimed increments
        once, one RECLAIM terminal (the second sweep's Put CCFs / hold is gone)."""
        from mvp._pipeline import _sweep_expired_holds
        from dynamo.tenant_budgets import TenantBudgetsRepository

        ctx = self._ctxs.pop(tok)
        self._expire_hold_row(ctx)
        budgets = TenantBudgetsRepository()
        reclaimed_before = self._pool_reclaimed()
        _sweep_expired_holds(budgets, self.tenant_id, self.period)
        after_first = self._pool_reclaimed()
        _sweep_expired_holds(budgets, self.tenant_id, self.period)
        assert self._pool_reclaimed() == after_first, "second sweep reclaimed again"
        assert after_first == reclaimed_before + int(ctx.pool_reserved_microusd)
        self._live_reserved.pop(tok)
        self._expected_terminal[ctx.hold_id] = (
            "RECLAIM", int(ctx.pool_reserved_microusd), 0
        )

    # -- shared hold-expiry mechanism ---------------------------------------

    def _expire_hold_row(self, ctx):
        """Past-date the hold row's embedded expiry WITHOUT sweeping, so a
        subsequent settle/sweep sees a live-but-expired hold. Keeps ctx.hold_sk
        pointing at the re-keyed row.

        On why ctx.hold_sk is updated: a genuinely slow owner holds a ctx whose sk
        embeds an expiry that has simply passed; re-keying to the past-dated sk
        reproduces that exact state."""
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
        ctx.hold_sk = new_sk
        return new_sk

    def _force_reap(self, ctx):
        """Expire the hold and immediately sweep, so the reaper reclaims it and
        writes a RECLAIM terminal. Asserts the row existed and is gone afterwards
        (a schema drift that made this a no-op would otherwise pass vacuously)."""
        from dynamo.tenant_budgets import TenantBudgetsRepository
        from mvp._pipeline import _sweep_expired_holds

        new_sk = self._expire_hold_row(ctx)
        budgets = TenantBudgetsRepository()
        _sweep_expired_holds(budgets, self.tenant_id, self.period)
        gone = budgets._table.get_item(
            Key={"tenant_id": self.tenant_id, "sk": new_sk}
        ).get("Item")
        assert gone is None, "sweep did not reclaim the expired hold row"

    # -- invariants ----------------------------------------------------------

    @invariant()
    def i1_settled_counter_equals_ledger(self):
        # I1': settled counter == Σ SETTLE.settled + Σ LATE_SETTLE.settled.
        _, terminal, late = self._events_by_kind()
        ledger_settled = sum(
            int(e["settled_delta_microusd"]) for e in terminal.values()
        ) + sum(int(e["settled_delta_microusd"]) for e in late.values())
        assert self._pool_settled() == ledger_settled, (
            f"I1' broken: counter={self._pool_settled()} ledger={ledger_settled}"
        )

    @invariant()
    def i2_reserved_derivation(self):
        # I2: reserved counter == Σ RESERVE.reserved_delta + Σ terminal.reserved_delta
        # (RESERVE is +R, each terminal returns -R; open holds have no terminal).
        reserve, terminal, _ = self._events_by_kind()
        derived = sum(int(e["reserved_delta_microusd"]) for e in reserve.values())
        derived += sum(int(e["reserved_delta_microusd"]) for e in terminal.values())
        assert self._pool_reserved() == derived, (
            f"I2 broken: counter={self._pool_reserved()} ledger-derived={derived}"
        )
        # Cross-check against the live-hold bookkeeping.
        assert self._pool_reserved() == sum(self._live_reserved.values()), (
            "I2 cross-check: reserved counter != Σ live reserved"
        )

    @invariant()
    def i3_reclaimed_derivation(self):
        # I3: reclaimed counter == Σ over RECLAIM terminals of (reserved returned).
        _, terminal, _ = self._events_by_kind()
        reclaimed = sum(
            -int(e["reserved_delta_microusd"])
            for e in terminal.values()
            if e["event_type"] == "RECLAIM"
        )
        assert self._pool_reclaimed() == reclaimed, (
            f"I3 broken: counter={self._pool_reclaimed()} ledger={reclaimed}"
        )

    @invariant()
    def nonneg_counters(self):
        assert self._pool_settled() >= 0
        assert self._pool_reserved() >= 0
        assert self._pool_reclaimed() >= 0

    @invariant()
    def exactly_once_spend(self):
        # The direct detector of the Phase-1 revenue leak: every settle that
        # returned success must have its spend in the ledger exactly once.
        _, terminal, late = self._events_by_kind()
        ledger_settled = sum(
            int(e["settled_delta_microusd"]) for e in terminal.values()
        ) + sum(int(e["settled_delta_microusd"]) for e in late.values())
        assert self.ref_settled == ledger_settled, (
            f"EXACTLY-ONCE-SPEND broken: expected={self.ref_settled} "
            f"ledger={ledger_settled}"
        )

    @invariant()
    def term_prime(self):
        # TERM': structure + per-event value correctness + no ghost/missing.
        reserve, terminal, late = self._events_by_kind()
        # RESERVE completeness: every reserved hold has exactly its RESERVE event.
        assert set(reserve) == set(self._reserved_events), (
            f"RESERVE set mismatch: ledger={set(reserve)} "
            f"expected={set(self._reserved_events)}"
        )
        for hid, exp_reserved in self._reserved_events.items():
            assert int(reserve[hid]["reserved_delta_microusd"]) == exp_reserved, (
                f"RESERVE reserved_delta for {hid} != {exp_reserved}"
            )
        # Terminal completeness + correctness.
        assert set(terminal) == set(self._expected_terminal), (
            f"terminal set mismatch: ledger={set(terminal)} "
            f"expected={set(self._expected_terminal)}"
        )
        for hid, (exp_type, exp_returned, exp_settled) in self._expected_terminal.items():
            ev = terminal[hid]
            assert ev["event_type"] == exp_type, (
                f"terminal type for {hid}: {ev['event_type']} != {exp_type}"
            )
            assert int(ev["reserved_delta_microusd"]) == -exp_returned, (
                f"terminal reserved_delta for {hid} != -{exp_returned}"
            )
            assert int(ev["settled_delta_microusd"]) == exp_settled, (
                f"terminal settled_delta for {hid} != {exp_settled}"
            )
            assert int(ev["settled_delta_microusd"]) >= 0, "negative settled"
        # LATE_SETTLE completeness + the LATE ⇒ terminal=RECLAIM dependency.
        assert set(late) == set(self._expected_late), (
            f"LATE set mismatch: ledger={set(late)} expected={set(self._expected_late)}"
        )
        for hid, exp_settled in self._expected_late.items():
            assert terminal[hid]["event_type"] == "RECLAIM", (
                f"LATE_SETTLE for {hid} but terminal is {terminal[hid]['event_type']}"
            )
            lev = late[hid]
            assert int(lev["reserved_delta_microusd"]) == 0
            assert int(lev["settled_delta_microusd"]) == exp_settled


TestCreditLedgerStateful = CreditLedgerMachine.TestCase
# 20×25 keeps the whole 10-rule Phase-2 protocol deeply interleaved while staying
# CI-reasonable (~4-5 min); the money invariants make every step do several
# consistent-read Queries, so this is I/O-bound, not shallow. Raise for nightly.
TestCreditLedgerStateful.settings = settings(
    max_examples=20,
    stateful_step_count=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@pytest.fixture(autouse=True)
def _bind_mock(dynamodb_mock):
    # Ensures moto tables exist for every stateful example.
    yield
