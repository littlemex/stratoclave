"""Stateful property-based tests for Stratoclave's billing ledger.

Complements test_billing_formal_z3.py: the Z3 suite proves three small
algebraic kernels exhaustively over unbounded integers, but against
hand-written *models* of the code.  This suite hammers the actual
implementation with randomized interleavings of the full API surface and
checks global invariants after every single step against an independently
maintained reference ledger.

Required SUT surface (backend.billing.ledger.BillingLedger):

    reserve(amount) -> hold_id          raises LimitExceeded if it won't fit
    settle(hold_id, actual)             raises HoldNotFound if hold is gone
    release(hold_id)                    raises HoldNotFound if hold is gone
    set_limit(new_limit)
    expire_lease(hold_id)               TEST HOOK: force lease into the past
    reap_expired() -> {hold_id: charged}
    reserved() -> int                   current R
    settled_total() -> int              current S
    hold_state(hold_id) -> str          "live" | "settled" | "released" | "reaped"

Invariants checked after every rule:

    1. R == sum(live holds)                     (reference vs SUT counter)
    2. R >= 0
    3. S == sum(settled actuals) + sum(reaped fallback charges)
    4. R + S <= L + overshoot_debt              (strict ceiling is provably
                                                 false; see the Z3 CE test)
    5. no hold is ever both settled and reaped
"""

import pytest
from hypothesis import settings, strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    consumes,
    initialize,
    invariant,
    multiple,
    precondition,
    rule,
)

from billing.ledger import BillingLedger, HoldNotFound, LimitExceeded

AMOUNTS = st.integers(min_value=1, max_value=1_000)
USAGE = st.integers(min_value=0, max_value=2_000)   # deliberately allows overshoot
LIMITS = st.integers(min_value=0, max_value=10_000)


class BillingMachine(RuleBasedStateMachine):
    holds = Bundle("holds")

    # ------------------------------------------------------------- setup ---

    @initialize(limit=LIMITS)
    def init(self, limit):
        self.ledger = BillingLedger(limit=limit)
        self.limit = limit
        # reference ledger
        self.live = {}            # hold_id -> reserved amount
        self.settled = {}         # hold_id -> settled actual
        self.reaped = {}          # hold_id -> reaper fallback charge
        self.released = set()     # hold_ids released for 0
        self.overshoot_debt = 0   # lawful excess over the ceiling

    def _R(self):
        return sum(self.live.values())

    def _S(self):
        return sum(self.settled.values()) + sum(self.reaped.values())

    # ------------------------------------------------------------- rules ---

    @rule(target=holds, amount=AMOUNTS)
    def reserve(self, amount):
        fits = self._R() + self._S() + amount <= self.limit
        if fits:
            hold_id = self.ledger.reserve(amount)
            assert hold_id is not None
            assert hold_id not in self.live, "hold_id reuse"
            self.live[hold_id] = amount
            return hold_id
        with pytest.raises(LimitExceeded):
            self.ledger.reserve(amount)
        return multiple()  # rejected: contribute nothing to the bundle

    @rule(hold_id=consumes(holds), actual=USAGE)
    def settle(self, hold_id, actual):
        # `actual` MAY exceed the reservation: the reserve is a heuristic and
        # cache tokens are settled but never reserved, so a real settle can bill
        # more than it held. The excess becomes bounded ceiling overshoot (the
        # same class as an admin lowering the limit) — fold it into the debt so
        # the ceiling-with-debt invariant stays exact.
        reserved = self.live.pop(hold_id)
        self.ledger.settle(hold_id, actual)
        self.settled[hold_id] = actual
        self.overshoot_debt += max(0, actual - reserved)

    @rule(hold_id=consumes(holds))
    def release(self, hold_id):
        del self.live[hold_id]
        self.ledger.release(hold_id)
        self.released.add(hold_id)

    @rule(hold_id=consumes(holds))
    def crash_then_reap(self, hold_id):
        """Owner crashes mid-stream; the reaper sweep reclaims the hold.

        Faithful to the real reaper: it FREES the reservation (R -= amount) and
        records `reclaimed` for the operator, but does NOT charge spend — a
        crashed request's usage is not billed. So a reap moves nothing into S
        and creates no overshoot; it only returns headroom to the pool.
        """
        reserved = self.live.pop(hold_id)
        self.ledger.expire_lease(hold_id)          # simulate the crash
        reaped = self.ledger.reap_expired()
        assert hold_id in reaped
        assert reaped[hold_id] == reserved, "reaper returns the reserved amount"
        # reaped holds contribute 0 to settled and 0 to overshoot.
        self.reaped[hold_id] = 0

    @precondition(lambda self: self.reaped)
    @rule()
    def reap_again_is_noop(self):
        """The reaper re-running (or racing itself) must be idempotent."""
        before = (self.ledger.reserved(), self.ledger.settled_total())
        assert self.ledger.reap_expired() == {}
        assert (self.ledger.reserved(), self.ledger.settled_total()) == before

    @precondition(lambda self: self.reaped)
    @rule(data=st.data(), actual=USAGE)
    def zombie_settle_after_reap(self, data, actual):
        """A zombie owner waking up after its hold was reaped must be
        rejected with no money movement (the conditioned-Delete theorem)."""
        hold_id = data.draw(st.sampled_from(sorted(self.reaped)))
        before = (self.ledger.reserved(), self.ledger.settled_total())
        with pytest.raises(HoldNotFound):
            self.ledger.settle(hold_id, actual)
        assert (self.ledger.reserved(), self.ledger.settled_total()) == before

    @rule(new_limit=LIMITS)
    def set_limit(self, new_limit):
        self.ledger.set_limit(new_limit)
        self.limit = new_limit
        # An admin cutting the limit below already-committed usage is the
        # second lawful source of usage-above-ceiling (the first is metered
        # overshoot).  Fold the shortfall into the debt so invariant 4 stays
        # exact rather than merely eventually-true.
        shortfall = (self._R() + self._S()) - (new_limit + self.overshoot_debt)
        if shortfall > 0:
            self.overshoot_debt += shortfall

    # --------------------------------------------------------- invariants ---

    @invariant()
    def inv_reserved_equals_live_holds(self):
        assert self.ledger.reserved() == self._R()

    @invariant()
    def inv_reserved_nonnegative(self):
        assert self.ledger.reserved() >= 0

    @invariant()
    def inv_settled_equals_charges(self):
        assert self.ledger.settled_total() == self._S()

    @invariant()
    def inv_ceiling_with_debt(self):
        assert (
            self.ledger.reserved() + self.ledger.settled_total()
            <= self.limit + self.overshoot_debt
        )

    @invariant()
    def inv_no_hold_both_settled_and_reaped(self):
        assert not (set(self.settled) & set(self.reaped))
        for hold_id, expected in (
            [(h, "settled") for h in self.settled]
            + [(h, "reaped") for h in self.reaped]
            + [(h, "released") for h in self.released]
            + [(h, "live") for h in self.live]
        ):
            assert self.ledger.hold_state(hold_id) == expected


BillingMachine.TestCase.settings = settings(
    max_examples=200,
    stateful_step_count=50,
    deadline=None,
)

TestBillingStateful = BillingMachine.TestCase
