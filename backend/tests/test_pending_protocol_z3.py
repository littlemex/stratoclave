"""Formal (SMT) verification of the PENDING protocol invariants with Z3.

Companion to test_pending_protocol_stateful.py (which hammers interleavings). Z3
proves the harder thing the stateful runner only samples: that each transition
PRESERVES the invariant for EVERY value of the unbounded symbolic state — in
particular that the ambiguous step-2 transition is safe for BOTH ghost values.

METHOD (same as test_billing_formal_z3): each obligation is proved by asserting
the NEGATION is UNSAT. A paired `sat` sanity test removes the guard under proof
and confirms Z3 finds the bug, so the harness is not vacuous.

STATE ENCODING (single hold in flight over one pool, which is enough for an
inductive-preservation argument: the per-hold transition either touches the
counter by exactly its amount or not, and I1' is a sum, so the one-hold step
generalises). Symbols, all unbounded ints / bools:

    reserved       : pool_reserved BEFORE the step
    amount (> 0)   : the hold's amount
    limit (>= 0)
    debited        : ghost — was this hold's debit already applied (before step)
    outstanding_others : Σ debited-outstanding amounts of all OTHER holds
                         (so I1' is reserved == outstanding_others + this_hold's
                          contribution)

I1'(before): reserved == outstanding_others + (amount if debited else 0)
We prove I1'(after) for each transition.

ASSUMPTIONS: money is unbounded ints (A1 of the billing suite); single-item
conditional writes serialize (A2); status transitions are the model's.
"""
import pytest
import z3

Z3_TIMEOUT_MS = 60_000
z3.set_param("smt.random_seed", 0)
z3.set_param("sat.random_seed", 0)


def _solver() -> z3.Solver:
    s = z3.Solver()
    s.set("timeout", Z3_TIMEOUT_MS)
    return s


def _check(s):
    res = s.check()
    if res == z3.unknown:
        pytest.fail(f"Z3 unknown: {s.reason_unknown()}")
    return res


def assert_proved(s, what):
    assert _check(s) == z3.unsat, f"NOT PROVED: {what} (Z3 found a counterexample)"


def assert_has_bug(s, what):
    assert _check(s) == z3.sat, f"VACUOUS: {what} (removing the guard found no bug)"


# I1' contribution of the hold under study: amount if debited else 0.
def _contrib(amount, debited):
    return z3.If(debited, amount, z3.IntVal(0))


def _base_syms():
    reserved = z3.Int("reserved")
    amount = z3.Int("amount")
    limit = z3.Int("limit")
    others = z3.Int("outstanding_others")   # Σ debited-outstanding of OTHER holds
    debited = z3.Bool("debited")
    s = _solver()
    s.add(amount > 0, limit >= 0, others >= 0, reserved >= 0)
    # I1'(before): reserved == others + contribution of this hold
    s.add(reserved == others + _contrib(amount, debited))
    return s, reserved, amount, limit, others, debited


# ---------------------------------------------------------------------------
# Transition: step 2 COMMIT (condition holds -> apply). Before: PENDING,
# not-yet-debited (debited == False). Guard: headroom >= amount AND active.
# After: reserved' = reserved + amount, debited' = True.
# I1'(after): reserved' == others + amount (hold now debited-outstanding).
# ---------------------------------------------------------------------------
def test_commit_preserves_I1():
    s, reserved, amount, limit, others, debited = _base_syms()
    s.add(z3.Not(debited))                       # a PENDING commit sees no prior debit
    headroom = limit - reserved
    s.add(headroom >= amount)                     # guard true -> it applies
    reserved_after = reserved + amount
    # I1'(after): hold is now debited & outstanding
    s.add(z3.Not(reserved_after == others + amount))
    assert_proved(s, "commit preserves I1'")


def test_commit_never_exceeds_limit():
    # committed-outstanding (reserved') <= limit, given the headroom guard.
    s, reserved, amount, limit, others, debited = _base_syms()
    s.add(z3.Not(debited))
    s.add(limit - reserved >= amount)             # guard
    reserved_after = reserved + amount
    s.add(reserved_after > limit)                 # negate "within limit"
    assert_proved(s, "commit keeps reserved <= limit")


def test_commit_without_guard_can_oversell_SANITY():
    # Remove the headroom guard: Z3 must find reserved' > limit (harness alive).
    s, reserved, amount, limit, others, debited = _base_syms()
    s.add(z3.Not(debited))
    # no guard
    reserved_after = reserved + amount
    s.add(reserved_after > limit)
    assert_has_bug(s, "commit without headroom guard oversells")


# ---------------------------------------------------------------------------
# Transition: AMBIGUOUS step 2. The client cannot observe the ghost. Proven for
# BOTH ghost outcomes:
#   applied  -> reserved' = reserved + amount, debited' = True
#   lost     -> reserved' = reserved,          debited' = False
# In both, the hold stays PENDING (no credit path taken here), and I1'(after)
# must hold. This is the crux: the SAME client-visible transition is safe either
# way.
# ---------------------------------------------------------------------------
def test_ambiguous_preserves_I1_both_ghost_values():
    for name, applied in (("applied", True), ("lost", False)):
        s, reserved, amount, limit, others, debited = _base_syms()
        s.add(z3.Not(debited))                   # PENDING, not yet debited
        if applied:
            s.add(limit - reserved >= amount)     # could only apply if guard held
            reserved_after = reserved + amount
            debited_after = z3.BoolVal(True)
        else:
            reserved_after = reserved
            debited_after = z3.BoolVal(False)
        # hold stays PENDING (not credited): its contribution is per debited_after
        s.add(z3.Not(reserved_after == others + _contrib(amount, debited_after)))
        assert_proved(s, f"ambiguous ({name}) preserves I1'")


# ---------------------------------------------------------------------------
# Transition: sweeper FENCE of a PENDING hold. Pool UNTOUCHED, status ->
# EXPIRED_UNCREDITED (still NOT credited back, so still contributes if debited).
# reserved' = reserved. I1'(after) must hold for both ghost values.
# The point: fencing NEVER changes reserved, so it cannot oversell regardless of
# whether the debit happened.
# ---------------------------------------------------------------------------
def test_fence_preserves_I1_both_ghost_values():
    for applied in (True, False):
        s, reserved, amount, limit, others, debited = _base_syms()
        s.add(debited == bool(applied))
        reserved_after = reserved                 # pool untouched
        # EXPIRED_UNCREDITED is NOT in _CREDITED_BACK -> contribution unchanged
        s.add(z3.Not(reserved_after == others + _contrib(amount, debited)))
        assert_proved(s, f"fence (debited={applied}) preserves I1'")


def test_fence_that_credits_would_oversell_SANITY():
    # If the sweeper WRONGLY credited back on fence when the debit had NOT
    # happened, Z3 finds reserved' < others (oversell of others' live holds).
    s, reserved, amount, limit, others, debited = _base_syms()
    s.add(z3.Not(debited))                       # NOT debited
    reserved_after = reserved - amount            # the WRONG credit-back
    # oversell witness: reserved' below the true outstanding (others)
    s.add(reserved_after < others)
    assert_has_bug(s, "crediting an undebited fence oversells")


# ---------------------------------------------------------------------------
# Transition: settle/release of an ACTIVE (or client-held PENDING) hold. By A1
# the debit applied (debited == True). reserved' = reserved - amount, status ->
# credited. I1'(after): the hold no longer contributes.
# ---------------------------------------------------------------------------
def test_settle_preserves_I1():
    s, reserved, amount, limit, others, debited = _base_syms()
    s.add(debited)                        # A1: client holds id => debited
    reserved_after = reserved - amount
    # hold now credited back -> contributes 0
    s.add(z3.Not(reserved_after == others + 0))
    assert_proved(s, "settle preserves I1'")


def test_settle_keeps_reserved_nonnegative():
    s, reserved, amount, limit, others, debited = _base_syms()
    s.add(debited)
    reserved_after = reserved - amount
    # reserved' >= 0 because reserved == others + amount >= amount
    s.add(reserved_after < 0)
    assert_proved(s, "settle keeps reserved >= 0")


# ---------------------------------------------------------------------------
# Reconcile read-order safety lemma (Fable (a)): counter read FIRST, hold set
# SECOND. Model a reserve of `new` (>0) that commits BETWEEN the two reads.
#   counter_seen  = reserved_before_recon (counter read first, before `new`)
#   entitled_seen = active_after + new     (holds read second, AFTER `new`)
# drift = counter_seen - entitled_seen. Prove drift can NEVER exceed the true
# leak, i.e. crediting `drift` never drives reserved below the true live floor
# (no oversell). We work in the "no PENDING in flight" regime the model uses:
# reserved == active_outstanding + leak, leak >= 0, active_outstanding >= 0.
# ---------------------------------------------------------------------------
def test_reconcile_counter_first_never_oversells():
    s = _solver()
    active_outstanding = z3.Int("active_outstanding")  # Σ ACTIVE debited-outstanding
    leak = z3.Int("leak")                               # debited EXPIRED_UNCREDITED
    new = z3.Int("new")                                 # a reserve racing the scan
    s.add(active_outstanding >= 0, leak >= 0, new >= 0)
    reserved_before = active_outstanding + leak
    counter_seen = reserved_before                      # counter read FIRST
    # holds read SECOND, after `new` committed & activated -> entitled inflated
    entitled_seen = active_outstanding + new
    drift = counter_seen - entitled_seen
    # reconcile only credits positive drift:
    credited = z3.If(drift > 0, drift, z3.IntVal(0))
    # true reserved now (the `new` reserve added `new` to the counter too):
    reserved_now = reserved_before + new
    reserved_after = reserved_now - credited
    true_live_floor = active_outstanding + new          # all genuinely-live holds
    # OVERSELL witness: reserved_after < true_live_floor
    s.add(reserved_after < true_live_floor)
    assert_proved(s, "counter-first reconcile never oversells under an in-flight reserve")


def test_reconcile_holds_first_CAN_oversell_SANITY():
    # Reverse the read order (holds FIRST, counter SECOND). Now `new` appears in
    # the counter but the holds snapshot predates it -> drift overestimated ->
    # over-credit -> Z3 finds reserved_after < true live floor.
    s = _solver()
    active_outstanding = z3.Int("active_outstanding")
    leak = z3.Int("leak")
    new = z3.Int("new")
    s.add(active_outstanding >= 0, leak >= 0, new > 0)
    # holds read FIRST (before `new`): entitled = active_outstanding
    entitled_seen = active_outstanding
    # counter read SECOND (after `new` commits): counter = active+leak+new
    counter_seen = active_outstanding + leak + new
    drift = counter_seen - entitled_seen                # = leak + new (too big)
    credited = z3.If(drift > 0, drift, z3.IntVal(0))
    reserved_now = active_outstanding + leak + new
    reserved_after = reserved_now - credited
    true_live_floor = active_outstanding + new
    s.add(reserved_after < true_live_floor)
    assert_has_bug(s, "holds-first reconcile can oversell")


# ---------------------------------------------------------------------------
# PR-1 (docs/design/pending-protocol.md): the SEPARATE-item marker's credit-back
# is exactly-once because it is gated on the marker PHASE, not mere presence. The
# marker is NOT deleted at credit-back (it must survive to dedupe a late reserve
# retry) — it transitions RESERVED -> SETTLED + TTL. So the credit-back MUST be
# conditioned on phase==RESERVED, else a second credit-back of a marker that is
# still present (but already SETTLED) would double-return headroom.
#
# Model two credit-back attempts of the SAME hold over the reserved counter, with
# a boolean `phase_reserved` that flips to False after the first credit. Symbols:
#   reserved0     : reserved before any credit (== amount + others, hold debited)
#   amount (> 0)
#   others (>= 0) : other holds' outstanding
# The phase CAS: a credit applies iff phase_reserved is True; it then subtracts
# amount and sets phase_reserved False.
# ---------------------------------------------------------------------------
def _credit_back(reserved, amount, phase_reserved):
    """Return (reserved', phase') for one phase-gated credit-back attempt."""
    applied = phase_reserved
    reserved_after = z3.If(applied, reserved - amount, reserved)
    phase_after = z3.If(applied, z3.BoolVal(False), phase_reserved)
    return reserved_after, phase_after


def test_phase_gated_credit_back_is_exactly_once():
    # Two credit-back attempts of the same hold. After BOTH, reserved must equal
    # `others` (the hold's amount returned EXACTLY once), never others-amount.
    s = _solver()
    amount = z3.Int("amount")
    others = z3.Int("others")
    s.add(amount > 0, others >= 0)
    reserved0 = others + amount                 # hold debited-outstanding
    r1, p1 = _credit_back(reserved0, amount, z3.BoolVal(True))   # first: applies
    r2, p2 = _credit_back(r1, amount, p1)                         # second: CAS fails
    s.add(z3.Not(r2 == others))                 # negate "returned exactly once"
    assert_proved(s, "phase-gated credit-back returns the hold exactly once")


def test_presence_only_credit_back_double_returns_SANITY():
    # If credit-back were gated on marker PRESENCE (which stays True after settle,
    # since the marker is kept for TTL dedupe) instead of PHASE, a second attempt
    # would subtract amount AGAIN -> reserved = others - amount < others (oversell
    # of the other holds). Z3 must find it, proving the phase gate is necessary.
    s = _solver()
    amount = z3.Int("amount")
    others = z3.Int("others")
    s.add(amount > 0, others >= 0)
    reserved0 = others + amount
    # presence stays True across both attempts (marker not deleted) -> both apply.
    r1, _ = _credit_back(reserved0, amount, z3.BoolVal(True))
    r2, _ = _credit_back(r1, amount, z3.BoolVal(True))
    s.add(r2 < others)                          # oversell witness
    assert_has_bug(s, "presence-gated credit-back double-returns headroom")
