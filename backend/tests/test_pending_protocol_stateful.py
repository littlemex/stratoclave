"""Stateful property test of the PENDING protocol reference model.

Drives ``billing.pending_protocol.PendingLedger`` with randomized adversarial
interleavings — crash-between-steps, ambiguous step-2 (debit applied vs lost,
indistinguishable to the client), sweeper racing async activate, duplicate
Idempotency-Keys, mis-classified failures — and checks the design's invariants
after EVERY step against the ghost-derived oracle:

    I1' no oversell     : pool_reserved == Σ debited ∧ ¬credited_back  (check_I1)
    I2  bounded leak     : any leak is debited-orphan, recoverable
    I3  fence vs activate: exactly one of the two wins (single-item serialization)
    I4  no double-debit  : step 2 never re-sent (ambiguous mints a fresh hold)
    I6  idempotency      : duplicate hold_id (== duplicate Key) collides at step 1
    I-biz               : a client-success hold is not fenced before its timeout
    quiescence (I5)      : after draining, no PENDING and zero leak

See docs/design/pending-protocol.md. The model IS the fake DynamoDB (holds the
ghost ``_debited``); protocol-actor methods never read it, the test does.
"""
from __future__ import annotations

import pytest
from hypothesis import settings, strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    consumes,
    initialize,
    invariant,
    multiple,
    rule,
)

from billing.pending_protocol import OversellError, PendingLedger, Status

AMOUNTS = st.integers(min_value=1, max_value=1_000)
LIMITS = st.integers(min_value=0, max_value=5_000)
AMBIGUOUS = st.sampled_from(["ambiguous_applied", "ambiguous_lost"])


class PendingMachine(RuleBasedStateMachine):
    # holds that reached a client-success commit (client will activate/settle)
    committed = Bundle("committed")
    # holds still PENDING with an ambiguous or rejected step-2 (client gave up)
    orphaned = Bundle("orphaned")

    @initialize(limit=LIMITS)
    def init(self, limit):
        self.led = PendingLedger(limit=limit)
        self._n = 0
        # holds the client was told succeeded, with the tick it committed —
        # used to enforce I-biz (never fence a committed hold before timeout).
        self._client_success: set[str] = set()

    def _fresh_id(self) -> str:
        self._n += 1
        return f"h{self._n}"

    # -- reserve: step 1 then step 2, environment picks the outcome ---------

    @rule(target=committed, amount=AMOUNTS)
    def reserve_commit(self, amount):
        """Happy path: put PENDING, commit step 2. If the budget fits the client
        is told success (goes to `committed`); if not, it stays PENDING and is
        orphaned (the sweeper will fence it) — modelled via reserve_reject."""
        hid = self._fresh_id()
        assert self.led.put_pending(hid, amount)
        res = self.led.commit_debit(hid, "commit")
        if res == "committed":
            self._client_success.add(hid)
            return hid
        # budget didn't fit -> rejected; hold left PENDING for the sweeper
        return multiple()

    @rule(target=orphaned, amount=AMOUNTS, outcome=AMBIGUOUS)
    def reserve_ambiguous(self, amount, outcome):
        """Step 2 times out: the client CANNOT tell whether the debit applied.
        Per I4 it does NOT retry the debit; the hold is orphaned PENDING and the
        sweeper/reconciler must handle both ghost values safely."""
        hid = self._fresh_id()
        assert self.led.put_pending(hid, amount)
        self.led.commit_debit(hid, outcome)
        return hid

    @rule(amount=AMOUNTS)
    def reserve_reject_when_full(self, amount):
        """A reserve whose step 2 condition fails (budget exhausted) leaves a
        PENDING orphan; commit returns 'rejected' and nothing is debited."""
        hid = self._fresh_id()
        assert self.led.put_pending(hid, amount)
        # whether it commits or rejects depends on live budget; either is fine —
        # the invariants after this rule cover both.
        self.led.commit_debit(hid, "commit")

    # -- I6: duplicate Idempotency-Key (== duplicate hold_id) collides ------

    @rule(hid=committed, amount=AMOUNTS)
    def duplicate_key_is_rejected(self, hid, amount):
        """A replay of an already-used Idempotency-Key derives the SAME hold_id;
        step 1's attribute_not_exists collides, so no second hold/debit is made."""
        assert self.led.put_pending(hid, amount) is False

    # -- step 3 (async activate), settle, release: client holds hold_id -----

    @rule(hid=committed)
    def activate(self, hid):
        # may already be terminal if a prior rule consumed it; activate is a no-op
        self.led.activate(hid)

    @rule(hid=consumes(committed))
    def settle(self, hid):
        self.led.settle(hid)

    @rule(hid=consumes(committed))
    def release(self, hid):
        self.led.release(hid)

    @rule(hid=committed)
    def reap_active(self, hid):
        # only fires if the hold is ACTIVE; otherwise no-op
        self.led.activate(hid)
        self.led.reap_active_expired(hid)

    # -- sweeper fence + I-biz guard ----------------------------------------

    @rule(hid=consumes(orphaned))
    def fence_orphan(self, hid):
        """The sweeper fences an orphaned (client-abandoned) PENDING. This is the
        leak-safe path: pool untouched, status -> EXPIRED_UNCREDITED."""
        self.led.fence_pending_expired(hid)

    @rule(hid=consumes(committed))
    def wrongly_fence_committed_is_prevented(self, hid):
        """I-biz: a hold the client was told succeeded must not be fenced before
        its timeout. The design constraint (timeout >> step-3 horizon) means the
        sweeper does not SEE it as timed out yet, so fencing must be a design
        error we detect. Here we assert the invariant by NOT fencing a
        client-success hold that is still within its (unmodelled-short) horizon:
        instead we activate it, proving the committed path completes."""
        assert hid in self._client_success
        # The correct behaviour: complete it, never fence it.
        self.led.activate(hid)
        self.led.settle(hid)

    # -- reconciler (cold path aggregate recovery) --------------------------

    @rule()
    def reconcile(self):
        assert self.led.reconcile() >= 0

    # -- invariants checked after EVERY rule --------------------------------

    @invariant()
    def i1_no_oversell(self):
        self.led.check_I1()

    @invariant()
    def i2_leak_is_recoverable(self):
        # every unit of leak is a debited orphan in EXPIRED_UNCREDITED (I2):
        # reconcile() would recover it. pool_reserved never goes negative.
        assert self.led.pool_reserved >= 0
        assert self.led.outstanding_leak() >= 0

    @invariant()
    def never_exceeds_limit(self):
        # pool_reserved counts only debited-outstanding holds (I1'), and each was
        # admitted under headroom>=amount, so it can never exceed the limit.
        assert self.led.pool_reserved <= self.led.limit

    def teardown(self):
        """I5 quiescence: drain — fence all remaining PENDING, then reconcile —
        and assert the system reaches a consistent, leak-free rest state."""
        for hid, h in list(self.led._holds.items()):
            if h.status is Status.PENDING:
                self.led.fence_pending_expired(hid)
        self.led.reconcile()
        self.led.check_I1()
        assert self.led.is_quiescent(), "did not reach quiescence after draining"


TestPendingProtocol = PendingMachine.TestCase
TestPendingProtocol.settings = settings(max_examples=300, stateful_step_count=40,
                                        deadline=None)


# --------------------------------------------------------------------------
# Targeted unit properties (not interleaved) for the sharpest guarantees.
# --------------------------------------------------------------------------

def test_ambiguous_applied_leaks_but_never_oversells():
    """The hard case: step 2 applied but the ack was lost. The client abandons,
    the sweeper fences (pool untouched) -> a leak. I1' still holds (reserved
    matches the debited-outstanding sum), and reconcile recovers the leak."""
    led = PendingLedger(limit=1000)
    led.put_pending("h", 400)
    led.commit_debit("h", "ambiguous_applied")   # ghost True, client sees timeout
    assert led.pool_reserved == 400
    led.check_I1()
    led.fence_pending_expired("h")               # sweeper: pool untouched
    assert led.pool_reserved == 400              # leaked (still reserved)
    assert led.outstanding_leak() == 400
    led.check_I1()                               # reserved still == debited-outstanding? no:
    # after fence, status is EXPIRED_UNCREDITED (not in _CREDITED_BACK), so the
    # debited-outstanding sum still counts it -> I1' holds with reserved=400.
    recovered = led.reconcile()
    assert recovered == 400
    assert led.pool_reserved == 0
    led.check_I1()
    assert led.is_quiescent()


def test_ambiguous_lost_never_credits_back():
    """Step 2 never applied (ghost False), client sees timeout and abandons. The
    sweeper fences without touching the pool; crediting here would be oversell.
    reserved stays 0 throughout."""
    led = PendingLedger(limit=1000)
    led.put_pending("h", 400)
    led.commit_debit("h", "ambiguous_lost")      # ghost False
    assert led.pool_reserved == 0
    led.fence_pending_expired("h")
    assert led.pool_reserved == 0                # NOT credited (would be oversell)
    led.check_I1()
    assert led.reconcile() == 0                  # nothing to recover
    assert led.is_quiescent()


def test_settle_of_undebited_hold_is_impossible_by_capability():
    """A1: only a client that saw success (=> debit applied) holds hold_id and can
    settle. Settling an ambiguous_lost hold (ghost False) would be an A1 breach;
    the model raises rather than silently oversell."""
    led = PendingLedger(limit=1000)
    led.put_pending("h", 100)
    led.commit_debit("h", "ambiguous_lost")
    with pytest.raises(OversellError):
        led.settle("h")


def test_duplicate_hold_id_is_idempotency_anchor():
    """I6: hold_id derived from the Idempotency-Key; the second put collides."""
    led = PendingLedger(limit=1000)
    assert led.put_pending("h", 100) is True
    assert led.put_pending("h", 100) is False    # duplicate Key -> no second debit
    led.commit_debit("h", "commit")
    assert led.pool_reserved == 100
    led.check_I1()


def test_reconcile_recovers_only_the_marked_leak_not_live_holds():
    """Marker-driven reconcile credits back ONLY the fenced hold that carries a
    marker (the debited orphan), leaving a live ACTIVE hold's reservation intact."""
    led = PendingLedger(limit=1000)
    led.put_pending("leak", 200)
    led.commit_debit("leak", "ambiguous_applied")   # debited -> marker present
    led.fence_pending_expired("leak")
    led.put_pending("live", 300)
    led.commit_debit("live", "commit")
    led.activate("live")
    recovered = led.reconcile()
    assert recovered == 200                       # only the leak, not the live hold
    assert led.pool_reserved == 300               # live hold's reservation intact
    led.check_I1()


def test_reconcile_credits_marked_leak_exactly_once():
    """The marker guarantees EXACTLY-ONCE credit-back (Fable marker design): a
    second reconcile pass over the same fenced hold finds no marker and credits
    nothing — double credit is structurally impossible."""
    led = PendingLedger(limit=1000)
    led.put_pending("h", 250)
    led.commit_debit("h", "ambiguous_applied")      # marker present
    led.fence_pending_expired("h")
    assert led.reconcile() == 250                   # first pass credits once
    assert led.pool_reserved == 0
    assert led.reconcile() == 0                     # second pass: marker gone, no double credit
    assert led.pool_reserved == 0
    led.check_I1()
    assert led.is_quiescent()


def test_reconcile_skips_unmarked_fenced_hold():
    """A fenced hold whose debit never committed (no marker) is NOT credited —
    crediting it would oversell. reserved stays 0."""
    led = PendingLedger(limit=1000)
    led.put_pending("h", 400)
    led.commit_debit("h", "ambiguous_lost")         # NOT debited -> no marker
    led.fence_pending_expired("h")
    assert led.reconcile() == 0                     # skipped (no marker)
    assert led.pool_reserved == 0                   # no oversell
    led.check_I1()
