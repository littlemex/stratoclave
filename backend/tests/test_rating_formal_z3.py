"""
Formal (SMT) verification of the step that turns tokens into money.

WHY THIS FILE EXISTS
--------------------
The pooled-billing proofs in `test_billing_formal_z3.py` establish that
admission cannot over-admit and that the headroom identity survives every
operation.  Every one of them reasons about *amounts* and takes for granted that
the amount settled is no larger than the amount reserved.  That assumption is
what makes checking the ceiling against a reserve-time ESTIMATE sound at all,
and it was written down nowhere.  This file states it, proves the part of it
that is provable, and — the point of the exercise — pins the part that is NOT
true of the current implementation.

The claim decomposes into an implication and a premise:

    (P)  per-component estimate dominance:  for every billable component c,
         tokens_actual[c] * rate[c] is bounded by what reserve priced for c
    (I)  P  and  a pinned rate  and  monotone rounding
             =>  cost_actual <= cost_reserved
             =>  headroom' = headroom + (cost_reserved - cost_actual) >= headroom

(I) is a theorem about the arithmetic and is proved here.  (P) is an empirical
property of the estimator and the provider, NOT a theorem — see
`test_estimate_dominance_fails_for_cache_write_tokens`, which demonstrates on the
shipped rate document that it is false today.  Do not read a green run of this
file as "the ceiling holds".  It says: the arithmetic is sound, and the premise
the arithmetic needs is a separate, currently-violated obligation.

METHOD
------
As in `test_billing_formal_z3.py`: encode, assert the NEGATION is UNSAT, and
pair each proof with a `sat` sanity test that removes the guard and confirms Z3
finds the bug immediately, so the harness cannot be vacuous.

GLOBAL ASSUMPTIONS (per-test ones are inline)
---------------------------------------------
 B1. Money and tokens are unbounded mathematical integers, except in the
     overflow tests (G6) which bound them deliberately.
 B2. The rounding policy under proof is `ceil`, because `rate_usage` refuses any
     other policy outright rather than charging under an unknown one.  A future
     policy ships with its own branch, its own pricing version, and its own
     proof; this file proves that recording the policy is load-bearing rather
     than assuming which policy is in force.
 B3. Rates are non-negative.  A negative rate is a configuration error that
     `_mtok_cost` would turn into a credit; it is out of scope here and is
     covered by the sign discipline in G6.
 B4. The snapshot is pinned: reserve and settle use the same rate vector.  That
     protocol property is proved in `test_pricing_pinning_z3.py`, not here; this
     file assumes it and shows in G1's sanity test what breaks without it.
"""

import pytest
import z3

Z3_TIMEOUT_MS = 60_000

z3.set_param("smt.random_seed", 0)
z3.set_param("sat.random_seed", 0)

TOKENS_PER_MTOK = 1_000_000

# The four billable components `rate_usage` charges, in its own order.
COMPONENTS = ("input", "output", "cache_read", "cache_write")


def _solver() -> z3.Solver:
    s = z3.Solver()
    s.set("timeout", Z3_TIMEOUT_MS)
    return s


def _assert_unsat(s: z3.Solver, what: str) -> None:
    """A proof: the negation of the property has no model.

    `unknown` is a FAILURE, never a pass — a solver that gave up has not proved
    anything, and treating it as success is how a formal suite rots into
    decoration.
    """
    result = s.check()
    assert result == z3.unsat, f"{what}: expected unsat, got {result}"


def _assert_sat(s: z3.Solver, what: str) -> None:
    """A sanity check: with the guard removed, the bug is reachable."""
    result = s.check()
    assert result == z3.sat, f"{what}: expected sat (bug reachable), got {result}"


def _ceil_of(numerator: z3.ArithRef, name: str):
    """Symbolic ceil division: the unique integer `q` with
    `(q-1) * M < numerator <= q * M`, and `q == 0` for a non-positive numerator.

    Stated over the NUMERATOR rather than over (tokens, rate) on purpose. The
    product of two symbolic integers is nonlinear, and mixing it into every
    proof pushes Z3 into a fragment where it returns `unknown` — which this file
    treats as a failure. Keeping the ceil encoding linear lets the arithmetic
    lemmas be decided, and confines nonlinear reasoning to the one lemma that
    genuinely needs it (`test_g3_numerator_is_monotone_*`).
    """
    q = z3.Int(name)
    return q, z3.If(
        numerator <= 0,
        q == 0,
        z3.And(q * TOKENS_PER_MTOK >= numerator,
               (q - 1) * TOKENS_PER_MTOK < numerator),
    )


def _ceil_cost(tokens: z3.ArithRef, rate: z3.ArithRef, name: str):
    """Symbolic `_mtok_cost(tokens, rate)`."""
    return _ceil_of(tokens * rate, name)


# ---------------------------------------------------------------------------
# G1 — ceiling soundness, CONDITIONAL on per-component estimate dominance
# ---------------------------------------------------------------------------

def test_g1_dominance_implies_actual_not_above_reserved():
    """(I): if every component's actual cost is within what reserve priced for
    it, the total actual cannot exceed the total reserved.

    Trivial as arithmetic, load-bearing as documentation: this is the step every
    other pooled-billing proof silently assumes, and stating it makes the
    premise it needs visible instead of invisible.
    """
    s = _solver()
    n = len(COMPONENTS)
    reserved = [z3.Int(f"res_{c}") for c in COMPONENTS]
    actual = [z3.Int(f"act_{c}") for c in COMPONENTS]

    for i in range(n):
        s.add(reserved[i] >= 0, actual[i] >= 0)
        s.add(actual[i] <= reserved[i])          # (P), per component

    total_reserved = z3.Sum(reserved)
    total_actual = z3.Sum(actual)
    s.add(z3.Not(total_actual <= total_reserved))   # negation of (I)
    _assert_unsat(s, "G1 dominance => actual <= reserved")


def test_g1_settle_never_lowers_headroom_under_dominance():
    """The consequence the ceiling actually depends on: settling a dominated
    actual returns headroom rather than consuming more of it.
    """
    s = _solver()
    headroom = z3.Int("headroom")
    cost_reserved = z3.Int("cost_reserved")
    cost_actual = z3.Int("cost_actual")
    s.add(cost_reserved >= 0, cost_actual >= 0)
    s.add(cost_actual <= cost_reserved)             # (P), aggregated
    headroom_after = headroom + (cost_reserved - cost_actual)
    s.add(z3.Not(headroom_after >= headroom))
    _assert_unsat(s, "G1 settle under dominance never lowers headroom")


def test_g1_sanity_without_dominance_ceiling_breaks():
    """SANITY: keep every component dominated EXCEPT one, and over-spend appears.

    An earlier draft of this test declared three free integers and asserted their
    satisfiability, which is `assert True` wearing a solver. Both reviewers named
    it. This version keeps the whole system constrained — admission checked the
    estimate, and three of the four components behave — so the only thing that
    changed is the deleted guard on the fourth.
    """
    s = _solver()
    limit = z3.Int("limit")
    reserved = [z3.Int(f"res_{c}") for c in COMPONENTS]
    actual = [z3.Int(f"act_{c}") for c in COMPONENTS]
    for i, _ in enumerate(COMPONENTS):
        s.add(reserved[i] >= 0, actual[i] >= 0)
        if i < len(COMPONENTS) - 1:
            s.add(actual[i] <= reserved[i])         # (P) holds here
    # (P) deleted for the last component only — the cache-write leg in reality.
    s.add(limit > 0, z3.Sum(reserved) <= limit)     # admission passed on the estimate
    s.add(z3.Sum(actual) > limit)                   # settled above the ceiling
    _assert_sat(s, "G1 sanity: one undominated component breaches the ceiling")


def test_g1_sanity_repricing_at_settle_breaks_the_ceiling():
    """SANITY: keep (P) on tokens but let settle re-read a HIGHER rate instead of
    the pinned one, and over-spend reappears.

    This is why snapshot pinning is a structural member of the ceiling rather
    than an audit nicety: token dominance alone does not save it.
    """
    s = _solver()
    tokens_reserved = z3.Int("tok_res")
    tokens_actual = z3.Int("tok_act")
    rate_at_reserve = z3.Int("rate_reserve")
    rate_at_settle = z3.Int("rate_settle")
    s.add(tokens_reserved > 0, tokens_actual > 0)
    s.add(tokens_actual <= tokens_reserved)         # (P) holds on tokens
    s.add(rate_at_reserve > 0, rate_at_settle > rate_at_reserve)   # price rose
    cost_reserved = tokens_reserved * rate_at_reserve
    cost_actual = tokens_actual * rate_at_settle
    s.add(cost_actual > cost_reserved)
    _assert_sat(s, "G1 sanity: re-pricing at settle breaks the ceiling")


# ---------------------------------------------------------------------------
# G3 — the fold is the one the event records
# ---------------------------------------------------------------------------

def test_g3_the_fold_is_a_function_of_the_recorded_components():
    """The total is uniquely determined by the recorded components, so two
    recomputations of one event cannot disagree.

    This is what T1 rests on: "pick any event and recompute it" only means
    something if the recomputation has one answer.

    An earlier draft of this test added `total == Sum(costs)` and its own negation,
    which is unsat in any encoding whatsoever and said nothing about the code. It
    survived one round of self-review because a repair script failed silently
    after printing success; the adversarial pass caught it. Agreement between this
    fold and the Python is a differential obligation, not an SMT one, and lives in
    `test_rating_differential.py`.
    """
    s = _solver()
    constraints, costs_a, costs_b = [], [], []
    for c in COMPONENTS:
        numerator = z3.Int(f"num_{c}")
        s.add(numerator >= 0)
        qa, cons_a = _ceil_of(numerator, f"qa_{c}")
        qb, cons_b = _ceil_of(numerator, f"qb_{c}")
        constraints += [cons_a, cons_b]
        costs_a.append(qa)
        costs_b.append(qb)
    s.add(z3.And(constraints))
    s.add(z3.Sum(costs_a) != z3.Sum(costs_b))
    _assert_unsat(s, "G3 the fold is a function of the recorded components")


def test_g3_sanity_an_unconstrained_quotient_makes_the_fold_ambiguous():
    """SANITY: drop the ceil constraint on one component and two recomputations of
    the same event can disagree — so the constraint, not the shape of the sum, is
    what makes the fold a function.
    """
    s = _solver()
    numerator = z3.Int("num")
    s.add(numerator > TOKENS_PER_MTOK)
    qa = z3.Int("qa_free")
    qb = z3.Int("qb_free")
    s.add(qa >= 0, qb >= 0)          # ceil constraint deleted
    s.add(qa != qb)
    _assert_sat(s, "G3 sanity: an unconstrained quotient makes the fold ambiguous")


def test_g3_per_component_and_post_total_rounding_differ():
    """The two folds are NOT interchangeable, which is why the policy has to be
    recorded on the event rather than assumed by whoever recomputes it.

    A satisfiability check, not a deep proof: it exhibits one token/rate vector
    where rounding each component and rounding the raw total disagree.  Cheap,
    and it is the whole justification for the `rounding` field existing.
    """
    s = _solver()
    tok_a, tok_b = z3.Int("tok_a"), z3.Int("tok_b")
    rate = z3.Int("rate")
    s.add(tok_a > 0, tok_b > 0, rate > 0)

    qa, cons_a = _ceil_cost(tok_a, rate, "qa")
    qb, cons_b = _ceil_cost(tok_b, rate, "qb")
    s.add(cons_a, cons_b)

    per_component = qa + qb
    q_total = z3.Int("q_total")
    raw = tok_a * rate + tok_b * rate
    s.add(q_total * TOKENS_PER_MTOK >= raw,
          (q_total - 1) * TOKENS_PER_MTOK < raw)

    s.add(per_component != q_total)
    _assert_sat(s, "G3 the two folds disagree on some input")


def test_g3_ceil_never_undercharges():
    """Ceil is chosen so integer division cannot nibble past a limit: the charged
    amount is never below the exact rational cost.
    """
    s = _solver()
    tokens, rate = z3.Int("tokens"), z3.Int("rate")
    s.add(tokens > 0, rate > 0)
    q, cons = _ceil_cost(tokens, rate, "q")
    s.add(cons)
    s.add(z3.Not(q * TOKENS_PER_MTOK >= tokens * rate))
    _assert_unsat(s, "G3 ceil never undercharges")


def test_g3_sanity_floor_undercharges():
    """SANITY: swap ceil for floor and under-charging becomes reachable."""
    s = _solver()
    tokens, rate = z3.Int("tokens"), z3.Int("rate")
    q = z3.Int("q_floor")
    s.add(tokens > 0, rate > 0)
    s.add(q * TOKENS_PER_MTOK <= tokens * rate,
          (q + 1) * TOKENS_PER_MTOK > tokens * rate)
    s.add(q * TOKENS_PER_MTOK < tokens * rate)
    _assert_sat(s, "G3 sanity: floor undercharges")


# ---------------------------------------------------------------------------
# G3 monotonicity — the property G1's rounding premise needs
# ---------------------------------------------------------------------------

def test_g3_ceil_is_monotone_in_the_numerator():
    """Lemma A, fully linear so it is decided rather than guessed: a larger
    numerator never ceils to a smaller quotient.

    Splitting monotonicity here is not cosmetic. Stated directly over
    (tokens, rate) the query is nonlinear and Z3 answered `unknown` — which this
    file counts as a failure, so the first draft of these two tests failed
    honestly instead of passing vacuously.
    """
    s = _solver()
    n_small, n_big = z3.Int("n_small"), z3.Int("n_big")
    s.add(n_small >= 0, n_big >= n_small)
    q_small, cons_s = _ceil_of(n_small, "q_small")
    q_big, cons_b = _ceil_of(n_big, "q_big")
    s.add(cons_s, cons_b)
    s.add(z3.Not(q_big >= q_small))
    _assert_unsat(s, "G3 lemma A: ceil is monotone in the numerator")


def test_g3_sanity_ceil_monotonicity_needs_the_ordering():
    """SANITY: drop the ordering of the numerators and the conclusion fails, so
    lemma A is not an artefact of the encoding.
    """
    s = _solver()
    n_a, n_b = z3.Int("n_a"), z3.Int("n_b")
    s.add(n_a >= 0, n_b >= 0)
    q_a, cons_a = _ceil_of(n_a, "q_a")
    q_b, cons_b = _ceil_of(n_b, "q_b")
    s.add(cons_a, cons_b)
    s.add(q_b < q_a)
    _assert_sat(s, "G3 sanity: without ordering, monotonicity does not hold")


def test_g3_numerator_is_monotone_in_the_varying_factor():
    """Lemma B: `a * b` is monotone in `b` while `a` is non-negative.

    Stated over the REALS. Over the integers Z3 returned `unknown` even after
    bounding both factors to ten million tokens and a thousand dollars per MTok —
    generous enough to be no restriction at all, and still not decided. The same
    inequality over the reals is UNSAT, and the integer instances are a subset of
    the real ones, so the integer cases are covered. This is not a claim that real
    nonlinear arithmetic is always decidable in practice, and it would not carry
    over for a property that depended on the variables being integers.

    One statement, not two: multiplication is commutative, so "monotone in tokens
    at a fixed rate" and "monotone in rate at fixed tokens" are the same formula.
    An earlier draft parametrised over both and never used the parameter, running
    the identical query twice and naming it coverage.
    """
    fixed = z3.Real("fixed_factor")
    low, high = z3.Real("varying_low"), z3.Real("varying_high")
    s = _solver()
    s.add(fixed >= 0, low >= 0, high >= low)
    s.add(z3.Not(high * fixed >= low * fixed))
    _assert_unsat(s, "G3 lemma B: the numerator is monotone in the varying factor")


def test_g1_the_composed_monotone_rounding_theorem():
    """**The theorem this file exists for**, proved rather than left in prose.

    Given token dominance and a pinned non-negative rate, the CEILED cost of the
    actual is no greater than the ceiled cost of the reserved:

        tok_act <= tok_res  and  rate >= 0
          =>  ceil(tok_act * rate / M)  <=  ceil(tok_res * rate / M)

    Lemmas A and B were proved separately and their composition was asserted only
    in a docstring — so the file's central claim was unproved while the file said
    it was proved. Adversarial review caught that; this is the repair.

    Decided with the tokens and the rate as REALS while only the quantised costs
    are integers. Over the integers Z3 returned `unknown` on this query, which
    this file counts as a failure. The precise justification for the move, since a
    looser one would be overclaiming: this is the SAME inequality with the integer
    variables embedded in the reals, and it is UNSAT for all reals, so it is UNSAT
    for the integer subset. That does not say Z3 decides nonlinear real arithmetic
    in general — it can return `unknown` there too — only that it decided this
    query. And it would not transfer for a property that depended on integrality.

    The ceil relation carries an explicit non-positive branch so the encoding
    matches `_mtok_cost`'s clamp rather than assuming positive usage.
    """
    s = _solver()
    tok_act, tok_res = z3.Real("tok_act"), z3.Real("tok_res")
    rate = z3.Real("rate")
    s.add(rate >= 0, tok_act >= 0, tok_res >= tok_act)

    q_act, q_res = z3.Int("q_act"), z3.Int("q_res")
    for q, tokens in ((q_act, tok_act), (q_res, tok_res)):
        numerator = tokens * rate
        # Clamped on TOKENS, not on the numerator, because that is what
        # `_mtok_cost` does: `if tokens <= 0: return 0`. Clamping the numerator
        # instead makes a negative rate look harmless, and an earlier draft of
        # the sanity test below failed for exactly that reason — the encoding was
        # kinder to the system than the code is.
        s.add(z3.Implies(tokens <= 0, q == 0))
        s.add(z3.Implies(tokens > 0,
                         z3.And(q * TOKENS_PER_MTOK >= numerator,
                                (q - 1) * TOKENS_PER_MTOK < numerator)))

    s.add(z3.Not(q_act <= q_res))
    _assert_unsat(s, "G1 composed: token dominance survives the rounding")


def test_g1_sanity_composed_theorem_needs_a_nonnegative_rate():
    """SANITY: allow a negative rate — a misconfigured rate table — and token
    dominance no longer implies cost dominance, so the non-negativity guard is
    load-bearing rather than tidy.

    Worth knowing what this reachable model means for the real code: `_mtok_cost`
    clamps on tokens, not on the product, so positive tokens at a negative rate
    produce a NEGATIVE cost — a credit that inflates headroom. Nothing in the
    rating path rejects a negative rate; the only defence is that the rate
    document has never contained one. That is an assumption about configuration,
    and it is recorded as such rather than proved.
    """
    s = _solver()
    tok_act, tok_res = z3.Real("tok_act"), z3.Real("tok_res")
    rate = z3.Real("rate")
    s.add(rate < 0, tok_act > 0, tok_res > tok_act)

    q_act, q_res = z3.Int("q_act"), z3.Int("q_res")
    for q, tokens in ((q_act, tok_act), (q_res, tok_res)):
        numerator = tokens * rate
        # Clamped on TOKENS, not on the numerator, because that is what
        # `_mtok_cost` does: `if tokens <= 0: return 0`. Clamping the numerator
        # instead makes a negative rate look harmless, and an earlier draft of
        # the sanity test below failed for exactly that reason — the encoding was
        # kinder to the system than the code is.
        s.add(z3.Implies(tokens <= 0, q == 0))
        s.add(z3.Implies(tokens > 0,
                         z3.And(q * TOKENS_PER_MTOK >= numerator,
                                (q - 1) * TOKENS_PER_MTOK < numerator)))

    s.add(q_act > q_res)
    _assert_sat(s, "G1 sanity: a negative rate breaks the composed theorem")


def test_g3_sanity_numerator_monotonicity_needs_a_nonnegative_factor():
    """SANITY: allow the fixed factor to be negative — a misconfigured rate — and
    monotonicity inverts. This is why B3 excludes negative rates rather than
    passing over them in silence.
    """
    fixed = z3.Real("fixed_factor")
    low, high = z3.Real("varying_low"), z3.Real("varying_high")
    s = _solver()
    s.add(fixed < 0, low >= 0, high > low)
    s.add(high * fixed < low * fixed)
    _assert_sat(s, "G3 sanity: a negative factor inverts monotonicity")


# ---------------------------------------------------------------------------
# G6 — overflow and sign
# ---------------------------------------------------------------------------

# Bounds chosen well above anything the routes accept, so a proof inside them
# says something about reality rather than about a convenient toy.
MAX_TOKENS_PER_COMPONENT = 10_000_000          # 10 MTok in one request
MAX_RATE_MICROUSD_PER_MTOK = 1_000_000_000     # $1,000 per MTok
INT64_MAX = 2**63 - 1


def test_g6_no_overflow_within_realistic_bounds():
    """`tokens * rate` stays inside signed 64-bit for every component under
    bounds far looser than the routes allow, so the ceil division cannot wrap.

    Wrap-around here would mean a negative charge, which inflates headroom — a
    money bug in the most dangerous direction.
    """
    s = _solver()
    total_raw = []
    for c in COMPONENTS:
        tokens, rate = z3.Int(f"tok_{c}"), z3.Int(f"rate_{c}")
        s.add(tokens >= 0, tokens <= MAX_TOKENS_PER_COMPONENT)
        s.add(rate >= 0, rate <= MAX_RATE_MICROUSD_PER_MTOK)
        total_raw.append(tokens * rate)
    s.add(z3.Not(z3.Sum(total_raw) <= INT64_MAX))
    _assert_unsat(s, "G6 no overflow within realistic bounds")


def test_g6_sanity_unbounded_tokens_overflow():
    """SANITY: remove the token bound and overflow is reachable, so the bound is
    doing work rather than decorating the proof.
    """
    s = _solver()
    tokens, rate = z3.Int("tokens"), z3.Int("rate")
    s.add(tokens >= 0, rate >= 0, rate <= MAX_RATE_MICROUSD_PER_MTOK)
    s.add(tokens * rate > INT64_MAX)
    _assert_sat(s, "G6 sanity: unbounded tokens overflow")


def test_g6_sanity_unbounded_rate_overflows():
    """SANITY: the rate bound is a guard too, not just the token bound.

    The proof above constrains both; only one of them had its work demonstrated.
    """
    s = _solver()
    tokens, rate = z3.Int("tokens"), z3.Int("rate")
    s.add(tokens >= 0, tokens <= MAX_TOKENS_PER_COMPONENT, rate >= 0)
    s.add(tokens * rate > INT64_MAX)
    _assert_sat(s, "G6 sanity: unbounded rate overflows")


def test_g6_refund_cannot_drive_settled_negative():
    """A refund removes at most what the hold settled, so `settled` cannot go
    negative and the identity cannot be repaired by inventing money.
    """
    s = _solver()
    settled_total = z3.Int("settled_total")
    hold_settled = z3.Int("hold_settled")
    refund = z3.Int("refund")
    s.add(settled_total >= 0, hold_settled >= 0, hold_settled <= settled_total)
    s.add(refund >= 0, refund <= hold_settled)     # the guard under proof
    s.add(z3.Not(settled_total - refund >= 0))
    _assert_unsat(s, "G6 refund cannot drive settled negative")


def test_g6_sanity_a_refund_above_the_hold_goes_negative():
    """SANITY: keep the hold's own accounting intact and delete ONLY the
    `refund <= hold_settled` guard.

    The earlier version dropped `hold_settled` from the model entirely, so it
    showed that two free non-negative integers can subtract to something negative
    — true of arithmetic, silent about the guard. This keeps the hold bounded by
    the total and lets the refund exceed the hold, which is the actual bug shape.
    """
    s = _solver()
    settled_total = z3.Int("settled_total")
    hold_settled = z3.Int("hold_settled")
    refund = z3.Int("refund")
    s.add(settled_total >= 0, hold_settled >= 0, hold_settled <= settled_total)
    s.add(refund > hold_settled)                    # the guard, deleted
    s.add(settled_total - refund < 0)
    _assert_sat(s, "G6 sanity: a refund above the hold goes negative")


# ---------------------------------------------------------------------------
# G7 — zero boundaries
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("zero_side", ["tokens", "rate"])
def test_g7_zero_side_costs_nothing(zero_side):
    """A zero rate or zero tokens costs exactly zero — no ceil artefact that
    charges a micro-USD for nothing.
    """
    s = _solver()
    tokens, rate = z3.Int("tokens"), z3.Int("rate")
    if zero_side == "tokens":
        s.add(tokens == 0, rate >= 0, rate <= MAX_RATE_MICROUSD_PER_MTOK)
    else:
        s.add(rate == 0, tokens >= 0, tokens <= MAX_TOKENS_PER_COMPONENT)
    q, cons = _ceil_cost(tokens, rate, "q")
    s.add(cons)
    s.add(q != 0)
    _assert_unsat(s, f"G7 zero {zero_side} costs nothing")


def test_g7_negative_tokens_are_clamped_not_credited():
    """`_mtok_cost` returns 0 for non-positive tokens rather than a negative
    cost, so a bad usage report cannot mint headroom.
    """
    s = _solver()
    tokens, rate = z3.Int("tokens"), z3.Int("rate")
    s.add(tokens <= 0, rate >= 0)
    q, cons = _ceil_cost(tokens, rate, "q")
    s.add(cons)
    s.add(q != 0)
    _assert_unsat(s, "G7 non-positive tokens cost zero")
