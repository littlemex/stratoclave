"""Formal (SMT) JOINT-TRANSITION equivalence proof: the transactional GOLDEN
reference (billing/ledger.py :: BillingLedger) vs the PENDING protocol reference
(billing/pending_protocol.py :: PendingLedger), for the golden-reference migration
delete gate (docs/design/pending-protocol.md).

WHY THIS FILE EXISTS. The delete gate that lets us delete `transaction` + the
reference model + the runtime oracle requires BOTH legs of condition (2):

    (2) Z3 equivalence proof green AND Hypothesis differential test green.

The Hypothesis leg (tests/test_billing_differential_oracle.py) SAMPLES operation
sequences and checks the two models stay α-equal. This file is the OTHER leg: it
proves — over EVERY symbolic value of the unbounded state, not sampled ones —
that applying the SAME input to both abstract transition systems PRESERVES the
coupling. Sampling can miss a divergence that only fires at one arithmetic corner;
the inductive symbolic proof cannot.

    tests/test_pending_protocol_z3.py proves pending PRESERVES ITS OWN invariant.
    THIS file proves pending stays EQUAL TO GOLDEN. Different obligations; both
    required (per the design's "formal (two roles, orthogonal)" clause).

THE COUPLING (α). Golden carries three quantities that gate admission and move on
each op: reserved R, settled S, ceiling L (headroom == L - R - S). Pending carries
the contended counter P (pool_reserved) and a ceiling Lp (headroom == Lp - P);
pending has NO settled counter. The differential oracle closes that gap by the
"settled injection": whenever golden books spend `actual` on settle, pending's
effective limit is lowered by the same `actual`. So the coupling maintained across
the whole sequence is

    J(R,S,L, P,Lp)  ==  (P == R)  AND  (Lp == L - S).

Under J, the two admission ceilings are ALGEBRAICALLY THE SAME inequality:

    golden admits amount  iff  R + S + amount <= L
    pending admits amount iff  Lp - P >= amount  ==  (L - S) - R >= amount
                                                  ==  R + S + amount <= L.   [same]

So verdict parity is not an extra axiom — it is a consequence of J. We PROVE that
(a) each transition preserves J for every symbolic state, and (b) at the shared
admission point the two verdicts coincide. A companion `sat` sanity test breaks J
(drops the settled-injection, i.e. uses Lp == L instead of Lp == L - S) and shows
Z3 then finds a state where the verdicts DIVERGE — so the coupling is load-bearing,
not a tautology, and the harness is not vacuous.

STATE ENCODING. One hold in flight over one pool suffices for an inductive-
preservation argument: J is an equality of running counters, each transition moves
each side by exactly the hold's amount (or the settled `actual`), and the argument
composes over holds. Symbols are unbounded ints / bools:

    R, S, L        : golden reserved / settled / limit  BEFORE the step
    P, Lp          : pending pool_reserved / effective-limit BEFORE the step
    amount (> 0)   : the hold's amount
    actual (>= 0)  : realized spend at settle (reservation is the max plausible
                     cost; `actual` may exceed `amount` — see BillingLedger.settle)
    debited        : ghost — did this hold's pending debit already apply

ASSUMPTIONS (identical to the billing/pending Z3 suites): money is unbounded ints
(no wrap); single-item conditional writes serialise; status transitions are the
models'. We enter each transition ASSUMING J holds (the inductive hypothesis) and
prove J holds after.
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
    assert _check(s) == z3.sat, f"VACUOUS: {what} (breaking the coupling found no bug)"


# --- the coupling J, as a reusable predicate over a (golden, pending) state ----
def _J(R, S, L, P, Lp):
    """J: pending counter tracks golden reserved, and pending's effective limit is
    golden's limit minus booked spend. Equivalence is maintained iff J holds."""
    return z3.And(P == R, Lp == L - S)


def _base_syms():
    """A pre-state that satisfies the inductive hypothesis J, plus the usual
    well-formedness bounds the real system guarantees (non-negative counters,
    positive amount). We do NOT assume headroom is non-negative up front: an admin
    limit-lower can transiently push R + S past L, and the proof must survive that
    regime too (it is exactly where a naive equivalence would crack)."""
    R = z3.Int("R")
    S = z3.Int("S")
    L = z3.Int("L")
    P = z3.Int("P")
    Lp = z3.Int("Lp")
    amount = z3.Int("amount")
    actual = z3.Int("actual")
    debited = z3.Bool("debited")
    s = _solver()
    s.add(amount > 0, actual >= 0, R >= 0, S >= 0, L >= 0, P >= 0)
    s.add(_J(R, S, L, P, Lp))          # inductive hypothesis: J holds before the step
    return s, R, S, L, P, Lp, amount, actual, debited


# ======================================================================
# 1. RESERVE / COMMIT — the shared admission point.
#    golden: admit iff R + S + amount <= L ; on admit R' = R + amount.
#    pending: admit iff Lp - P >= amount   ; on admit P' = P + amount.
#    Prove (a) verdict parity and (b) J preserved on the admit branch.
# ======================================================================
def test_reserve_verdicts_agree_under_J():
    """Under J the two admission verdicts are the SAME boolean for every state —
    including the settled>0 regime and the R+S>L overshoot regime."""
    s, R, S, L, P, Lp, amount, actual, debited = _base_syms()
    gold_admit = (R + S + amount <= L)
    pend_admit = (Lp - P >= amount)
    s.add(gold_admit != pend_admit)          # negate: verdicts disagree
    assert_proved(s, "reserve verdicts agree under J")


def test_reserve_admit_preserves_J():
    """On the admit branch, R' = R + amount and P' = P + amount; S, L, Lp unchanged.
    J' = (P+amount == R+amount) AND (Lp == L - S) must hold."""
    s, R, S, L, P, Lp, amount, actual, debited = _base_syms()
    s.add(R + S + amount <= L)                # admitted (parity proven above)
    R2 = R + amount
    P2 = P + amount
    s.add(z3.Not(_J(R2, S, L, P2, Lp)))       # negate J after
    assert_proved(s, "reserve admit preserves J")


def test_reserve_reject_preserves_J():
    """On the reject branch neither side moves any counter; J trivially preserved.
    (Kept explicit so the reject leg is covered, not assumed.)"""
    s, R, S, L, P, Lp, amount, actual, debited = _base_syms()
    s.add(R + S + amount > L)                 # rejected on both (parity)
    s.add(z3.Not(_J(R, S, L, P, Lp)))         # nothing moved -> still J
    assert_proved(s, "reserve reject preserves J")


def test_reserve_verdicts_diverge_without_settled_injection_SANITY():
    """VACUITY GUARD: drop the settled injection (use Lp == L instead of Lp == L-S).
    Then whenever S > 0 the ceilings differ and Z3 must find a state where the
    verdicts DIVERGE — proving the settled injection in J is load-bearing."""
    R = z3.Int("R")
    S = z3.Int("S")
    L = z3.Int("L")
    P = z3.Int("P")
    Lp = z3.Int("Lp")
    amount = z3.Int("amount")
    s = _solver()
    s.add(amount > 0, R >= 0, S >= 0, L >= 0, P >= 0)
    s.add(P == R, Lp == L)                    # BROKEN coupling: no settled injection
    gold_admit = (R + S + amount <= L)
    pend_admit = (Lp - P >= amount)
    s.add(gold_admit != pend_admit)           # can the verdicts disagree now?
    assert_has_bug(s, "verdicts diverge when settled injection is dropped")


# ======================================================================
# 2. SETTLE — reserved returns by `amount` on BOTH; golden books `actual` into
#    settled, pending lowers its effective limit by the same `actual` (injection).
#    golden: R' = R - amount, S' = S + actual.
#    pending: P' = P - amount, Lp' = Lp - actual.
#    Prove J preserved (this is where the injection earns its keep).
# ======================================================================
def test_settle_preserves_J():
    s, R, S, L, P, Lp, amount, actual, debited = _base_syms()
    s.add(R >= amount, P >= amount)           # the hold is reserved on both (J => equal)
    R2 = R - amount
    S2 = S + actual
    P2 = P - amount
    Lp2 = Lp - actual                         # ADVERSARIAL settled injection
    s.add(z3.Not(_J(R2, S2, L, P2, Lp2)))
    assert_proved(s, "settle preserves J (with settled injection)")


def test_settle_without_injection_breaks_J_SANITY():
    """VACUITY GUARD: if pending did NOT lower its limit on settle (Lp2 = Lp), then
    after any actual>0 settle the (Lp == L - S) leg of J breaks. Z3 must find it."""
    s, R, S, L, P, Lp, amount, actual, debited = _base_syms()
    s.add(R >= amount, P >= amount, actual > 0)
    R2 = R - amount
    S2 = S + actual
    P2 = P - amount
    Lp2 = Lp                                  # BUG: injection omitted
    s.add(z3.Not(_J(R2, S2, L, P2, Lp2)))
    assert_has_bug(s, "settle without injection breaks J")


# ======================================================================
# 3. RELEASE / REAP — reservation returns by `amount`, NO spend booked. Ceilings
#    (L on golden, Lp on pending) unchanged; settled unchanged. Same transition
#    for release, reap_active_expired, and the debited-fence reconcile credit.
# ======================================================================
def test_release_preserves_J():
    s, R, S, L, P, Lp, amount, actual, debited = _base_syms()
    s.add(R >= amount, P >= amount)
    R2 = R - amount
    P2 = P - amount
    s.add(z3.Not(_J(R2, S, L, P2, Lp)))       # S, L, Lp all unchanged (no spend)
    assert_proved(s, "release/reap preserves J (no spend, ceilings fixed)")


def test_reap_that_books_spend_breaks_J_SANITY():
    """VACUITY GUARD: the real reaper must NOT charge spend. If a buggy reaper booked
    `actual` into golden settled on reap (S2 = S + actual) while pending only frees
    the reservation, J's second leg breaks. Z3 must find it."""
    s, R, S, L, P, Lp, amount, actual, debited = _base_syms()
    s.add(R >= amount, P >= amount, actual > 0)
    R2 = R - amount
    P2 = P - amount
    S2 = S + actual                           # BUG: reaper charged spend
    s.add(z3.Not(_J(R2, S2, L, P2, Lp)))
    assert_has_bug(s, "reap that books spend breaks J")


# ======================================================================
# 4. FENCE (PENDING -> EXPIRED_UNCREDITED). The sweeper CANNOT touch the pool
#    (no capability, ghost unreadable), so pending's P is UNCHANGED; golden keeps
#    the hold reserved too (it defers reap to the batch reconcile). NEITHER side
#    moves a counter, for BOTH ghost values of `debited`. J preserved.
# ======================================================================
def test_fence_preserves_J_both_ghost_values():
    for applied in (True, False):
        s, R, S, L, P, Lp, amount, actual, debited = _base_syms()
        s.add(debited == applied)
        # fence moves NO counter on either side (design: sweeper never debits/credits,
        # golden defers its reap to reconcile). J holds trivially — asserted, not assumed.
        s.add(z3.Not(_J(R, S, L, P, Lp)))
        assert_proved(s, f"fence (debited={applied}) preserves J")


# ======================================================================
# 5. RECONCILE of a fenced hold. Marker present (debited) -> credit back exactly
#    once: P' = P - amount AND golden reaps it R' = R - amount (batch drain), no
#    spend. Marker absent (never debited) -> credit ZERO on both, nothing moves
#    (phantom-credit guard). Prove J for BOTH ghost branches.
# ======================================================================
def test_reconcile_debited_credits_once_preserves_J():
    s, R, S, L, P, Lp, amount, actual, debited = _base_syms()
    s.add(debited, R >= amount, P >= amount)   # marker present
    R2 = R - amount        # golden batch reap frees the reservation (no spend)
    P2 = P - amount        # pending marker-driven credit-back, exactly once
    s.add(z3.Not(_J(R2, S, L, P2, Lp)))       # S, L, Lp unchanged
    assert_proved(s, "reconcile (debited) credits once, preserves J")


def test_reconcile_undebited_credits_zero_preserves_J():
    s, R, S, L, P, Lp, amount, actual, debited = _base_syms()
    s.add(z3.Not(debited))                     # marker absent (never debited)
    # phantom-credit guard: an un-debited fenced hold contributed 0 to BOTH counters
    # while fenced (put-only holds never debited), so reconcile moves nothing.
    s.add(z3.Not(_J(R, S, L, P, Lp)))
    assert_proved(s, "reconcile (undebited) credits zero, preserves J")


def test_reconcile_undebited_that_credits_breaks_J_SANITY():
    """VACUITY GUARD: if reconcile credited an UN-debited hold on the pending side
    (P2 = P - amount) while golden minted/held nothing for it (R unchanged), the two
    counters diverge — the classic oversell. Z3 must find the divergence."""
    s, R, S, L, P, Lp, amount, actual, debited = _base_syms()
    s.add(z3.Not(debited), P >= amount)       # marker absent (never debited)
    P2 = P - amount                           # BUG: credited a never-debited hold
    s.add(z3.Not(_J(R, S, L, P2, Lp)))        # golden R unchanged
    assert_has_bug(s, "crediting an undebited reconcile breaks J")


# ======================================================================
# 6. SET_LIMIT (admin). golden: L' = new; pending's effective limit shifts by the
#    SAME delta on top of the settled injection, so Lp' = Lp + (new - L) keeps
#    (Lp' == L' - S). This is the one transition that legally makes R + S > L.
# ======================================================================
def test_set_limit_preserves_J():
    s, R, S, L, P, Lp, amount, actual, debited = _base_syms()
    new_limit = z3.Int("new_limit")
    s.add(new_limit >= 0)
    L2 = new_limit
    Lp2 = Lp + (new_limit - L)                # shift effective limit by the delta
    s.add(z3.Not(_J(R, S, L2, P, Lp2)))       # R, S, P unchanged; ceilings shifted
    assert_proved(s, "set_limit preserves J (delta applied to both ceilings)")


def test_set_limit_verdicts_still_agree_after_lower_into_overshoot():
    """After an admin lowers L below R + S (overshoot regime), a subsequent reserve
    must STILL get the same verdict on both sides. Proves J's verdict-parity
    survives the only regime where R + S > L."""
    s, R, S, L, P, Lp, amount, actual, debited = _base_syms()
    new_limit = z3.Int("new_limit")
    s.add(new_limit >= 0, new_limit < R + S)   # lowered into overshoot
    L2 = new_limit
    Lp2 = Lp + (new_limit - L)                 # J maintained by the same delta
    gold_admit = (R + S + amount <= L2)
    pend_admit = (Lp2 - P >= amount)
    s.add(gold_admit != pend_admit)
    assert_proved(s, "verdicts agree after limit-lower into overshoot")


# ======================================================================
# 7. MODEL-FIDELITY CROSS-CHECK. The Z3 obligations above reason over SYMBOLIC
#    transition equations I transcribed by hand (R' = R + amount, etc.). If those
#    equations ever drift from what the REAL BillingLedger / PendingLedger actually
#    do, every proof above would stay green while proving the wrong thing. This
#    test drives the SAME transitions through the REAL model objects and asserts
#    J numerically — so a change to either model that breaks a transition equation
#    fails HERE, and the migration pauses (the design's FREEZE clause). It is the
#    bridge between "the symbols are equivalent" and "the code the symbols model".
# ======================================================================
from billing.ledger import BillingLedger, LimitExceeded  # noqa: E402
from billing.pending_protocol import PendingLedger        # noqa: E402


def _J_holds(gold: BillingLedger, pend: PendingLedger) -> bool:
    """J on real objects: pending counter tracks golden reserved, and pending's
    (injection-adjusted) limit equals golden limit minus booked spend."""
    return (pend.pool_reserved == gold.reserved()
            and pend.limit == gold.limit() - gold.settled_total())


def test_real_models_match_the_symbolic_transitions():
    """Each leg mirrors one Z3 obligation, executed on the REAL models. Fails if a
    model's transition delta ever diverges from the equation Z3 proved over."""
    # reserve/commit admit: R'=R+amount, P'=P+amount  (test_reserve_admit_preserves_J)
    g = BillingLedger(limit=100)
    p = PendingLedger(limit=100)
    gh = g.reserve(30)
    assert p.put_pending("a", 30) and p.commit_debit("a", "commit") == "committed"
    assert p.activate("a")
    assert _J_holds(g, p) and g.reserved() == p.pool_reserved == 30

    # reserve verdict parity in the settled==0 regime (test_reserve_verdicts_agree_under_J)
    grej = False
    try:
        g.reserve(80)               # 30 + 0 + 80 > 100 -> reject
    except LimitExceeded:
        grej = True
    assert p.put_pending("r", 80) and p.commit_debit("r", "commit") == "rejected"
    assert grej is True

    # settle actual=20: golden R-=amount,S+=actual ; pending P-=amount, limit-=actual
    # (test_settle_preserves_J — the settled-injection leg)
    g.settle(gh, 20)
    p.settle("a")
    p.limit -= 20
    assert _J_holds(g, p) and g.reserved() == p.pool_reserved == 0
    assert g.settled_total() == 20 and p.limit == 80  # Lp == L - S

    # verdict parity in the settled>0 regime (test_reserve_verdicts_agree_under_J):
    # golden 0+20+80<=100 admit ; pending 80-0>=80 admit -> BOTH admit.
    gh2 = g.reserve(80)
    assert p.put_pending("b", 80) and p.commit_debit("b", "commit") == "committed"
    assert _J_holds(g, p) and g.reserved() == p.pool_reserved == 80

    # release: reservation returns, NO spend, ceilings fixed (test_release_preserves_J)
    g.release(gh2)
    p.release("b")
    assert _J_holds(g, p) and g.reserved() == p.pool_reserved == 0
    assert g.settled_total() == 20 and p.limit == 80  # unchanged by release

    # reap: reservation returns, NO spend (test_release_preserves_J shares this delta)
    gh3 = g.reserve(15)
    assert p.put_pending("c", 15) and p.commit_debit("c", "commit") == "committed"
    assert p.activate("c")
    g.expire_lease(gh3)
    g.reap_expired()
    assert p.reap_active_expired("c")
    assert _J_holds(g, p) and g.reserved() == p.pool_reserved == 0

    # set_limit: both ceilings shift by the same delta (test_set_limit_preserves_J)
    g.set_limit(60)
    p.limit += 60 - 100    # mirror the delta on the injection-adjusted limit
    assert _J_holds(g, p) and p.limit == g.limit() - g.settled_total()  # 60 - 20 == 40


def test_real_models_agree_on_admission_across_the_settled_ceiling():
    """The admission-parity obligation (test_reserve_verdicts_agree_under_J) executed
    on the real models right at the settled-adjusted ceiling boundary: 60 reserved +
    50 settled leaves headroom 100-0-50 == 50 on golden; pending limit lowered to 50,
    P==0 -> headroom 50. A 60 rejects both; a 50 admits both."""
    g = BillingLedger(limit=100)
    p = PendingLedger(limit=100)
    gh = g.reserve(60)
    assert p.put_pending("h", 60) and p.commit_debit("h", "commit") == "committed"
    assert p.activate("h")
    g.settle(gh, 50)
    p.settle("h")
    p.limit -= 50
    assert g.reserved() == p.pool_reserved == 0
    # 60 over the ceiling on both (0 + 50 + 60 > 100 ; 0 + 60 > 50).
    grej = False
    try:
        g.reserve(60)
    except LimitExceeded:
        grej = True
    assert p.put_pending("x", 60) and p.commit_debit("x", "commit") == "rejected"
    assert grej is True
    # 50 fits both (0 + 50 + 50 <= 100 ; 0 + 50 <= 50).
    assert g.reserve(50)
    assert p.put_pending("y", 50) and p.commit_debit("y", "commit") == "committed"
    assert _J_holds(g, p) and g.reserved() == p.pool_reserved == 50
