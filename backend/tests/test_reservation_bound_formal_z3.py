"""
Formal (SMT) verification that a SOUND reservation makes the ceiling hard.

WHY THIS FILE EXISTS
--------------------
`test_rating_formal_z3.py` proves the ceiling is sound given per-component
estimate dominance, and `test_rating_differential.py` shows that premise is false
today. The conclusion drawn from that by two independent reviewers was that a hard
ceiling had never been achievable, because prompt-cache writes are decided by the
provider during the call. That conclusion was wrong, and the owner refused it.

Two facts settle it. First, the blocking mechanism already exists and is already
correct: the pool reserve is a conditional write on
`pool_headroom_microusd >= :amt`, so an insufficient pool already refuses
admission and no upstream call is made. Second, the provider cannot bill for
content it was never sent, so an upper bound on the charge IS computable at
reserve time — pricing every input-side token at the worst of the input-side rates
covers cache reads and cache writes with no assumption about provider behaviour.

So the ceiling is not hard for exactly one reason: the number handed to that
condition is an estimate rather than a bound. This file proves that replacing it
with a bound is sufficient — that `settled + reserved <= limit` becomes an
invariant of the existing transaction, with nothing else changed.

WHAT THIS DOES NOT ESTABLISH
----------------------------
That any particular function computes a sound bound. That is the production
obligation, and it has two known holes that no proof can close, both found in
review:

  - **Images.** Image tokens scale with pixels (roughly pixels/750), not with
    bytes. A large flat-colour PNG is a few hundred bytes and thousands of tokens,
    so `tokens <= utf8_bytes` is FALSE for multimodal requests.
  - **Provider-injected tokens.** Tool-use scaffolding the provider adds, and
    server-side tool results such as web search, are billable and were never in
    the bytes the gateway sent. A constant does not cover them.

A bound is therefore only sound within a stated content envelope. What is proved
here is the implication; what the envelope must be is a contract term, not a
theorem.

METHOD
------
Encode, assert the NEGATION is UNSAT, and pair every proof with a `sat` sanity
test that deletes one guard inside an otherwise fully constrained system, so the
sanity has something to search rather than being satisfiability of free variables.
`unknown` is a failure.

ASSUMPTIONS
-----------
 D1. Single-item serialisation of the conditional write, as in A2 of
     `test_billing_formal_z3.py` — AWS's documented semantics, taken as an axiom.
 D2. Settle is unconditional. Making settle conditional would let a broken bound
     silently drop a charge instead of recording it, which the money model
     forbids: the actual is recorded even when it breaks the bound, and that is
     what the overrun alarm is for.
 D3. Every admission goes through the reserve path. Any bypass — a hold reused on
     retry, an additional mid-stream charge, an external fixed-amount capture —
     is outside this model and would break the ceiling independently.
"""

import pytest
import z3

Z3_TIMEOUT_MS = 60_000

z3.set_param("smt.random_seed", 0)
z3.set_param("sat.random_seed", 0)

# How many concurrent requests the model reasons about. Three is enough to express
# "one is admitted while two others are in flight", which is where a per-request
# argument would miss the interaction.
FLIGHT = 3


def _solver() -> z3.Solver:
    s = z3.Solver()
    s.set("timeout", Z3_TIMEOUT_MS)
    return s


def _assert_unsat(s: z3.Solver, what: str) -> None:
    result = s.check()
    assert result == z3.unsat, f"{what}: expected unsat, got {result}"


def _assert_sat(s: z3.Solver, what: str) -> None:
    result = s.check()
    assert result == z3.sat, f"{what}: expected sat (bug reachable), got {result}"


def _prove_and_show_guard_is_needed(constraints: dict, guard: str, negation, what: str):
    """Prove a property, then re-run the SAME constraint set with exactly one guard
    deleted and require the violation to become reachable.

    Written as one helper rather than two tests because the danger this file kept
    walking into is a sanity check built from a DIFFERENT, weaker model than its
    proof — which demonstrates that arithmetic permits something, not that the
    guard is load-bearing. Deriving both from one dict makes that structurally
    impossible: the only difference between the two runs is the named key.

    Four tautologies were written and shipped into review before this existed, so
    the helper is the fix for a repeated mistake rather than decoration.
    """
    assert guard in constraints, f"{what}: no guard named {guard!r} to delete"

    proof = _solver()
    for c in constraints.values():
        proof.add(c)
    proof.add(negation)
    result = proof.check()
    assert result == z3.unsat, f"{what}: expected unsat with {guard!r}, got {result}"

    without = _solver()
    for key, c in constraints.items():
        if key != guard:
            without.add(c)
    without.add(negation)
    result = without.check()
    assert result == z3.sat, (
        f"{what}: deleting {guard!r} should make the violation reachable, got "
        f"{result} — the guard is not doing the work the proof credits it with"
    )


def _pool_state(name: str):
    """A pool at one instant: the ceiling, the two counters, and the derived
    headroom the condition expression reads.
    """
    limit = z3.Int(f"{name}_limit")
    reserved = z3.Int(f"{name}_reserved")
    settled = z3.Int(f"{name}_settled")
    headroom = limit - reserved - settled
    return limit, reserved, settled, headroom


# ---------------------------------------------------------------------------
# The theorem: a sound bound turns the existing condition into a hard ceiling
# ---------------------------------------------------------------------------

def test_settle_preserves_the_ceiling_when_the_reservation_is_a_bound():
    """The inductive step that is currently false and becomes true under a bound.

    Settle moves `bound` out of `reserved` and `actual` into `settled`. If
    `actual <= bound` then `settled + reserved` cannot rise, so a state that
    satisfied the ceiling still satisfies it. This is the whole content of the fix
    to the money path: nothing about the transaction changes, only the number.
    """
    s = _solver()
    limit, reserved, settled, _ = _pool_state("pre")
    bound, actual = z3.Int("bound"), z3.Int("actual")

    s.add(limit >= 0, reserved >= 0, settled >= 0)
    s.add(settled + reserved <= limit)            # the invariant, before
    s.add(bound >= 0, actual >= 0)
    s.add(bound <= reserved)                      # this hold's share of reserved
    s.add(actual <= bound)                        # SOUNDNESS of the reservation

    reserved_after = reserved - bound
    settled_after = settled + actual
    s.add(z3.Not(settled_after + reserved_after <= limit))
    _assert_unsat(s, "settle preserves the ceiling when the reservation is a bound")


def test_sanity_settle_breaks_the_ceiling_when_the_reservation_is_an_estimate():
    """SANITY: delete ONLY soundness — keep the prior invariant, keep the hold
    bounded by `reserved`, keep every quantity non-negative — and the ceiling
    breaks.

    This is today's system: `actual` can exceed the amount admission checked, so
    the negative headroom is not a policy choice, it is the absence of this one
    guard.
    """
    s = _solver()
    limit, reserved, settled, _ = _pool_state("pre")
    bound, actual = z3.Int("bound"), z3.Int("actual")

    s.add(limit >= 0, reserved >= 0, settled >= 0)
    s.add(settled + reserved <= limit)
    s.add(bound >= 0, actual >= 0)
    s.add(bound <= reserved)
    # soundness deleted: nothing relates `actual` to `bound`
    reserved_after = reserved - bound
    settled_after = settled + actual
    s.add(settled_after + reserved_after > limit)
    _assert_sat(s, "sanity: an estimate lets settle breach the ceiling")


def test_reserve_preserves_the_ceiling_under_its_condition_expression():
    """Admission is already safe: the conditional write refuses unless the
    headroom covers the whole amount, so reserving cannot breach the ceiling.

    Modelled on the real condition, `pool_headroom_microusd >= :amt`, with
    headroom defined as `limit - reserved - settled` exactly as the table
    maintains it.
    """
    s = _solver()
    limit, reserved, settled, headroom = _pool_state("pre")
    amount = z3.Int("amount")

    s.add(limit >= 0, reserved >= 0, settled >= 0)
    s.add(settled + reserved <= limit)
    s.add(amount >= 0)
    s.add(headroom >= amount)                     # the condition expression

    s.add(z3.Not(settled + (reserved + amount) <= limit))
    _assert_unsat(s, "reserve preserves the ceiling under its condition")


def test_sanity_reserve_without_its_condition_breaches_the_ceiling():
    """SANITY: delete the condition and admission alone breaks the ceiling, so the
    conditional write is load-bearing and was never the defect.
    """
    s = _solver()
    limit, reserved, settled, headroom = _pool_state("pre")
    amount = z3.Int("amount")

    s.add(limit >= 0, reserved >= 0, settled >= 0)
    s.add(settled + reserved <= limit)
    s.add(amount >= 0)
    # condition deleted
    s.add(settled + (reserved + amount) > limit)
    _assert_sat(s, "sanity: reserve without its condition breaches the ceiling")


# ---------------------------------------------------------------------------
# Concurrency: the bound has to hold for every in-flight request, not on average
# ---------------------------------------------------------------------------

def test_concurrent_settles_cannot_breach_the_ceiling_under_sound_bounds():
    """Several requests admitted, then all settled, in any order.

    The per-request step above is not enough on its own: the question an operator
    asks is whether a burst can breach the ceiling collectively. With every
    reservation sound, the sum of the actuals is bounded by the sum of the bounds,
    which admission already fitted under the limit.
    """
    s = _solver()
    limit = z3.Int("limit")
    bounds = [z3.Int(f"bound_{i}") for i in range(FLIGHT)]
    actuals = [z3.Int(f"actual_{i}") for i in range(FLIGHT)]

    s.add(limit >= 0)
    for i in range(FLIGHT):
        s.add(bounds[i] >= 0, actuals[i] >= 0)
        s.add(actuals[i] <= bounds[i])            # soundness, per request
    # Every one of them passed admission, so their bounds fitted together: this is
    # what the conditional writes collectively enforce.
    s.add(z3.Sum(bounds) <= limit)

    s.add(z3.Not(z3.Sum(actuals) <= limit))
    _assert_unsat(s, "concurrent settles cannot breach the ceiling under sound bounds")


def test_sanity_one_unsound_reservation_in_a_burst_breaches_the_ceiling():
    """SANITY: make every reservation sound EXCEPT one and the burst breaches.

    One unsound component is enough, which is why the bound has to cover every
    billable component rather than the ones that are easy to predict.
    """
    s = _solver()
    limit = z3.Int("limit")
    bounds = [z3.Int(f"bound_{i}") for i in range(FLIGHT)]
    actuals = [z3.Int(f"actual_{i}") for i in range(FLIGHT)]

    s.add(limit > 0)
    for i in range(FLIGHT):
        s.add(bounds[i] >= 0, actuals[i] >= 0)
        if i < FLIGHT - 1:
            s.add(actuals[i] <= bounds[i])        # sound for all but the last
    s.add(z3.Sum(bounds) <= limit)
    s.add(z3.Sum(actuals) > limit)
    _assert_sat(s, "sanity: one unsound reservation in a burst breaches")


# ---------------------------------------------------------------------------
# The reaper: released early, a live hold becomes a second admission
# ---------------------------------------------------------------------------

def test_a_hold_must_stay_reserved_until_its_own_settle_replaces_it():
    """The reaper guard, stated as a property of the counters rather than as a
    warning in prose.

    Both reviewers promoted the expired-hold reaper from housekeeping to the
    correctness boundary once reservations get larger: a hold released while its
    call is still running returns headroom, a second request consumes it, and both
    charges eventually land. The guard is that A's amount stays in `reserved` until
    A's own settle removes it, so B's admission is checked against headroom that
    still accounts for A.

    Deleting that guard is exactly what a reaper timeout shorter than the longest
    real call does, and the helper requires the breach to become reachable without
    it.
    """
    limit = z3.Int("limit")
    bound_a, actual_a = z3.Int("bound_a"), z3.Int("actual_a")
    bound_b, actual_b = z3.Int("bound_b"), z3.Int("actual_b")

    constraints = {
        "domain": z3.And(limit >= 0, bound_a >= 0, bound_b >= 0,
                         actual_a >= 0, actual_b >= 0),
        "soundness": z3.And(actual_a <= bound_a, actual_b <= bound_b),
        "a_admitted": bound_a <= limit,
        # The guard: B is admitted against headroom that STILL holds A's amount.
        # Without it, B is admitted against `limit` alone, as if A had been
        # released — which is the early-reap bug.
        "a_still_held_while_b_is_admitted": bound_a + bound_b <= limit,
    }
    _prove_and_show_guard_is_needed(
        constraints, "a_still_held_while_b_is_admitted",
        actual_a + actual_b > limit,
        "a hold must stay reserved until its own settle replaces it",
    )


# ---------------------------------------------------------------------------
# What the overrun record means once the reservation is a bound
# ---------------------------------------------------------------------------

def test_the_recorded_overrun_is_zero_exactly_when_the_bound_holds():
    """What the overrun field means once the reservation is a bound.

    `overrun = max(0, actual - bound)` is zero for every request the bound covers,
    and strictly positive only when the bound was wrong. So an overrun observed in
    production is not an operating mode, it is evidence of a defect in the bound.

    An earlier version of this test asserted `actual <= bound` and `actual > bound`
    in the same solver and called the resulting unsat a proof. That is a tautology
    and it is the fourth of its kind in this work; the helper above now makes the
    shape hard to write by accident. What is proved here instead is the equivalence
    the alarm depends on, with the soundness guard shown to be load-bearing.
    """
    bound, actual, overrun = z3.Int("bound"), z3.Int("actual"), z3.Int("overrun")
    constraints = {
        "domain": z3.And(bound >= 0, actual >= 0),
        "overrun_definition": overrun == z3.If(actual > bound, actual - bound, 0),
        "soundness": actual <= bound,
    }
    _prove_and_show_guard_is_needed(
        constraints, "soundness", overrun != 0,
        "the recorded overrun is zero exactly when the bound holds",
    )


@pytest.mark.parametrize("uncovered", ["image_tokens", "provider_injected_tokens"])
def test_sanity_a_component_outside_the_bound_reopens_the_overrun(uncovered):
    """SANITY, and it is the honest limit of the whole design: add a billable
    component the bound does not cover and the overrun is reachable again.

    Both are real. Image tokens scale with pixels rather than bytes, so a small
    flat PNG can carry thousands of tokens. Tool scaffolding and server-side tool
    results are billed and were never in the bytes the gateway sent. Neither is
    covered by a byte count or by a constant, so a bound is sound only inside a
    stated content envelope — and that envelope is a contract term the
    implementation must enforce at the door, not something this proof provides.
    """
    s = _solver()
    bound = z3.Int("bound")
    covered = z3.Int("covered_actual")
    outside = z3.Int(f"{uncovered}_cost")
    s.add(bound >= 0, covered >= 0, outside > 0)
    s.add(covered <= bound)                       # the covered part is sound
    s.add(covered + outside > bound)              # the total is not
    _assert_sat(s, f"sanity: {uncovered} outside the bound reopens the overrun")
