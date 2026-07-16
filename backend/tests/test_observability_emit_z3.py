"""
Formal (SMT) verification of the P0-13 exactly-once observability-emit
invariants with Z3.

METHOD (matches tests/test_billing_formal_z3.py)
-------------------------------------------------
Each invariant is proved by encoding the finalizer-claim logic as constraints
and asserting that the NEGATION of the invariant is UNSAT.  Paired `sat`
sanity tests delete the guard under proof and confirm Z3 immediately finds
the bug — proving the harness is not vacuous.

WHAT IS MODELLED
----------------
run_stream has FOUR finalizer sites: invoke_error, midstream_error,
completed, client_disconnect (the `finally`).  All four gate their money
writes AND the new on_finalized emit behind the SAME one-shot claim:

    if _claim_finalize():
        _notify(status)          # <- P0-13 emit, no await before this
        <money write(s)>

The claim is a check-and-set under a threading.Lock, so committed claim
attempts take effect in a TOTAL ORDER (encoded as distinct integer
serialization points).  The flag is flipped INSIDE the lock BEFORE any
write ("flip-before"), so a claim is visible to later sites even if the
claimant is subsequently cancelled at its `await`.

ASSUMPTIONS (per-test ones inline)
----------------------------------
 O1. threading.Lock serialises claim attempts (Python runtime axiom).
 O2. CODE-SHAPE AXIOM: there is NO await/suspension point between a winning
     _claim_finalize() and the _notify() call, at every site.  Cancellation
     therefore cannot fire between claim and emit.  This is enforced by the
     diff shape (_notify is called BEFORE the shielded money await) and must
     be preserved by future edits.
 O3. Whether a won finalizer's fire-and-forget write eventually COMMITS is
     liveness (process death -> hold reaper), not modelled.  We prove who may
     RUN, not that they finish.
 O4. on_finalized itself is try/except-wrapped and side-effect-free w.r.t.
     money state; emit "happening" is modelled as a pure observation.
"""

import pytest
import z3

Z3_TIMEOUT_MS = 60_000

z3.set_param("smt.random_seed", 0)
z3.set_param("sat.random_seed", 0)

SITES = ["invoke_error", "midstream_error", "completed", "client_disconnect"]
N = len(SITES)
DISCONNECT = SITES.index("client_disconnect")


def _solver() -> z3.Solver:
    s = z3.Solver()
    s.set("timeout", Z3_TIMEOUT_MS)
    return s


def _check(s: z3.Solver):
    res = s.check()
    if res == z3.unknown:
        pytest.fail(
            f"Z3 returned 'unknown' (reason: {s.reason_unknown()}). "
            f"Raise Z3_TIMEOUT_MS or shrink the model; do NOT ignore this."
        )
    return res


def assert_proved(s: z3.Solver, what: str) -> None:
    """The solver holds the NEGATION of `what`; UNSAT == proved for all models."""
    if _check(s) == z3.sat:
        pytest.fail(f"INVARIANT VIOLATED: {what}\nCounterexample:\n{s.model()}")


def assert_counterexample_exists(s: z3.Solver, what: str) -> z3.ModelRef:
    res = _check(s)
    assert res == z3.sat, f"expected a counterexample to exist for: {what}"
    return s.model()


# ---------------------------------------------------------------------------
# Shared model: flip-before-write claim under a lock.
# ---------------------------------------------------------------------------

def _claim_model(s: z3.Solver):
    """attempted[i]: site i reached its _claim_finalize() call.
    order[i]:     the lock's serialization point of that attempt (distinct).
    won[i]:       site i is the EARLIEST attempted site — flip-before means
                  the flag is committed at order[i] regardless of any later
                  cancellation of site i's awaited write.
    """
    attempted = [z3.Bool(f"attempted_{SITES[i]}") for i in range(N)]
    order = [z3.Int(f"ord_{SITES[i]}") for i in range(N)]
    s.add(z3.Distinct(*order))
    won = []
    for i in range(N):
        earlier = z3.Or([
            z3.And(attempted[j], order[j] < order[i])
            for j in range(N) if j != i
        ])
        won.append(z3.And(attempted[i], z3.Not(earlier)))
    return attempted, order, won


# ---------------------------------------------------------------------------
# PROOF 1: at most one site ever wins the claim (base for money AND emit).
# ---------------------------------------------------------------------------

def test_claim_won_at_most_once():
    s = _solver()
    _attempted, _order, won = _claim_model(s)
    s.add(z3.Or([
        z3.And(won[i], won[j]) for i in range(N) for j in range(i + 1, N)
    ]))
    assert_proved(s, "at most one finalizer site wins the one-shot claim")


def test_sanity_single_winner_is_reachable():
    """Hygiene: the model is not trivially UNSAT — a run where all four sites
    race and exactly one wins is representable."""
    s = _solver()
    attempted, _order, won = _claim_model(s)
    s.add(z3.And(*attempted))
    s.add(z3.Or(*won))
    assert_counterexample_exists(s, "a reachable single-winner execution")


# ---------------------------------------------------------------------------
# PROOF 2: emit happens at most once per request, across all 4 sites.
# Guard under proof: emitted[i] -> won[i]  (on_finalized is called only
# inside the `if _claim_finalize():` branch; O2 says nothing can intervene).
# ---------------------------------------------------------------------------

def test_emit_at_most_once():
    s = _solver()
    _attempted, _order, won = _claim_model(s)
    emitted = [z3.Bool(f"emitted_{SITES[i]}") for i in range(N)]
    for i in range(N):
        s.add(z3.Implies(emitted[i], won[i]))          # the guard
    s.add(z3.Or([
        z3.And(emitted[i], emitted[j])
        for i in range(N) for j in range(i + 1, N)
    ]))
    assert_proved(s, "on_finalized fires at most once per request")


def test_sanity_guard_deleted_double_emit_is_sat():
    """NON-VACUITY: replace the claim guard with the buggy 'emit whenever the
    site is reached' — Z3 must immediately find a double emit."""
    s = _solver()
    attempted, _order, _won = _claim_model(s)
    emitted = [z3.Bool(f"emitted_{SITES[i]}") for i in range(N)]
    for i in range(N):
        s.add(z3.Implies(emitted[i], attempted[i]))    # BUG: attempt, not claim
    s.add(z3.And(emitted[SITES.index("completed")],
                 emitted[DISCONNECT]))
    m = assert_counterexample_exists(
        s, "double emit (completed + client_disconnect) when the claim guard is deleted"
    )
    assert m is not None


# ---------------------------------------------------------------------------
# PROOF 3: status=client_disconnect emit implies NO other finalizer ran its
# money writes.  Money writes are gated by the same claim: money[i] -> won[i].
# (Whether the disconnect settle itself COMMITS is liveness — see O3.)
# ---------------------------------------------------------------------------

def test_disconnect_emit_excludes_other_finalizers():
    s = _solver()
    _attempted, _order, won = _claim_model(s)
    emitted = [z3.Bool(f"emitted_{SITES[i]}") for i in range(N)]
    money = [z3.Bool(f"money_{SITES[i]}") for i in range(N)]
    for i in range(N):
        s.add(z3.Implies(emitted[i], won[i]))
        s.add(z3.Implies(money[i], won[i]))
    s.add(emitted[DISCONNECT])
    s.add(z3.Or([money[i] for i in range(N) if i != DISCONNECT]))
    assert_proved(
        s,
        "a client_disconnect emit implies no other site ran refund/settle/release",
    )


# ---------------------------------------------------------------------------
# PROOF 4 + SANITY: flip-before-write is what makes cancellation harmless.
# ---------------------------------------------------------------------------

def test_flip_before_write_is_cancellation_immune():
    """cancelled[i] is a FREE variable: the adversary may cancel any site's
    awaited write.  Because won[] does not depend on cancelled[] (the flag was
    committed inside the lock before the write), at-most-one-winner still
    holds under arbitrary cancellation."""
    s = _solver()
    _attempted, _order, won = _claim_model(s)
    _cancelled = [z3.Bool(f"cancelled_{SITES[i]}") for i in range(N)]  # unconstrained
    s.add(z3.Or([
        z3.And(won[i], won[j]) for i in range(N) for j in range(i + 1, N)
    ]))
    assert_proved(s, "at-most-one-winner under arbitrary cancellation (flip-before)")


def test_sanity_flip_after_write_double_finalize_is_sat():
    """NON-VACUITY for proof 4: model the BUGGY flip-AFTER-write variant —
    a claim only becomes visible to later sites if the claimant COMPLETES
    (is not cancelled between check and flag-set).  Z3 must find the classic
    counterexample: site A checks, starts its write, is cancelled before the
    flip; the disconnect `finally` then also 'wins' -> double finalize AND
    double emit."""
    s = _solver()
    attempted = [z3.Bool(f"attempted_{SITES[i]}") for i in range(N)]
    order = [z3.Int(f"ord_{SITES[i]}") for i in range(N)]
    cancelled = [z3.Bool(f"cancelled_{SITES[i]}") for i in range(N)]
    s.add(z3.Distinct(*order))
    won_broken = []
    for i in range(N):
        visible_earlier = z3.Or([
            z3.And(attempted[j], z3.Not(cancelled[j]), order[j] < order[i])
            for j in range(N) if j != i
        ])
        won_broken.append(z3.And(attempted[i], z3.Not(visible_earlier)))
    s.add(z3.Or([
        z3.And(won_broken[i], won_broken[j])
        for i in range(N) for j in range(i + 1, N)
    ]))
    m = assert_counterexample_exists(
        s, "double finalize when the flag is flipped AFTER the write"
    )
    # The counterexample must actually use a cancellation (that IS the bug).
    assert any(
        z3.is_true(m.eval(cancelled[i], model_completion=True)) for i in range(N)
    ), "expected the flip-after counterexample to involve a cancelled claimant"
