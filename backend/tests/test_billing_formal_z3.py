"""
Formal (SMT) verification of Stratoclave's pooled-billing invariants with Z3.

METHOD
------
Each invariant is proved by encoding the billing logic as constraints and
asserting that the NEGATION of the invariant is UNSAT: Z3 has searched every
model expressible in the encoding (i.e. every interleaving the encoding can
represent) and found no violation.  Paired `sat` "sanity" tests delete the
guard under proof (CAS condition / claim flag / attribute_exists) and confirm
Z3 immediately finds the bug — proving the harness is not vacuous.

GLOBAL SIMPLIFYING ASSUMPTIONS (per-test ones are called out inline)
--------------------------------------------------------------------
 A1. Money is unbounded mathematical integers (micro-USD).  DynamoDB Number
     overflow (38 digits) is out of scope.
 A2. DynamoDB TransactWriteItems touching a single item SERIALISES: committed
     transactions take effect in some total order, and a ConditionExpression
     is evaluated against the item state at the transaction's commit point.
     This is AWS's documented semantics; we take it as an AXIOM.  If boto3
     usage breaks it (e.g. non-transactional writes), these proofs say nothing.
 A3. `status == active` is folded away (a suspended pool only REMOVES
     behaviours, it cannot add admissions).
 A4. Failed CAS attempts write nothing, so only COMMITTING attempts are
     modelled.  Retry counts / jittered backoff are liveness, not safety,
     and are not proved here.
 A5. The exactly-once dedup token for lost-ack settle retries is assumed to
     work (a settle's money effect is modelled as applied at most once).
     That token's implementation needs its own test.
"""

import pytest
import z3

Z3_TIMEOUT_MS = 60_000

# Deterministic solving in CI: pin the z3 version (requirements-dev.txt) and
# fix the seeds.  These models solve in milliseconds; the timeout is a
# tripwire, and `unknown` is treated as a FAILURE, never a pass.
z3.set_param("smt.random_seed", 0)
z3.set_param("sat.random_seed", 0)


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


# ===========================================================================
# 1. NO OVER-ADMISSION UNDER CONCURRENT CAS
# ===========================================================================
#
# Model: N concurrent reservers.  Each reserver i has a snapshot (snapR_i,
# snapS_i), a cost c_i >= 1, a boolean commit_i, and — if it commits — a
# commit slot pos_i in [0, N).  By axiom A2 committed transactions are
# totally ordered, so committed reservers occupy DISTINCT slots.
#
# The CAS ConditionExpression (pool_reserved == r0 AND pool_settled == s0)
# is encoded as: a commit lands ONLY IF the row state at its commit slot
# equals its snapshot.  Equivalently: snapshot == state-at-commit-time.
# The Python-side ceiling check is on the SNAPSHOT: snapR + snapS + c <= L.
#
# Per-test simplification: this bounded trace contains only reserves (S is
# constant).  test_pool_invariant_inductive_step_all_ops below closes the
# gap: it proves ONE step of ANY op type (reserve/settle/release/reap)
# preserves the invariant, which extends the result to mixed traces of any
# length by induction.

_N = 5  # bounded reserver count; the inductive-step test covers unbounded N


def _cas_model(n: int, enforce_cas: bool):
    L, R0, S0 = z3.Ints("L R0 S0")
    cons = [L >= 0, R0 >= 0, S0 >= 0, R0 + S0 <= L]

    cost = [z3.Int(f"c{i}") for i in range(n)]
    commit = [z3.Bool(f"commit{i}") for i in range(n)]
    pos = [z3.Int(f"pos{i}") for i in range(n)]
    snapR = [z3.Int(f"snapR{i}") for i in range(n)]
    snapS = [z3.Int(f"snapS{i}") for i in range(n)]

    for i in range(n):
        cons.append(cost[i] >= 1)
        cons.append(z3.Implies(commit[i], z3.And(pos[i] >= 0, pos[i] < n)))

    # A2: committed single-item txns serialise -> distinct commit slots.
    for i in range(n):
        for j in range(i + 1, n):
            cons.append(z3.Implies(z3.And(commit[i], commit[j]), pos[i] != pos[j]))

    def R_at(t):
        # Row's pool_reserved just before slot t: R0 plus every commit that
        # landed strictly earlier.
        return R0 + z3.Sum(
            [z3.If(z3.And(commit[i], pos[i] < t), cost[i], z3.IntVal(0)) for i in range(n)]
        )

    for i in range(n):
        if enforce_cas:
            # THE KEY LEMMA, encoded: the ConditionExpression means a commit
            # lands only if the row still equals the snapshot, i.e. the
            # snapshot IS the state at the commit slot.
            cons.append(z3.Implies(commit[i], snapR[i] == R_at(pos[i])))
            cons.append(z3.Implies(commit[i], snapS[i] == S0))
        else:
            # Broken variant: unconditional write, snapshot may be stale.
            # Everyone read the initial state (a real, but stale, read).
            cons.append(z3.Implies(commit[i], snapR[i] == R0))
            cons.append(z3.Implies(commit[i], snapS[i] == S0))
        # Python-side ceiling check, evaluated on the SNAPSHOT:
        cons.append(z3.Implies(commit[i], snapR[i] + snapS[i] + cost[i] <= L))

    R_final = R_at(n)  # all committed slots are < n
    S_final = S0       # reserves-only trace; see inductive-step test for mixes
    return cons, commit, snapR, snapS, R_final, S_final, L


def test_no_over_admission_under_concurrent_cas():
    """For EVERY interleaving of N CAS reservers, R + S <= L after all commits."""
    cons, *_rest = _cas_model(_N, enforce_cas=True)
    _, _, _, R_final, S_final, L = _rest
    s = _solver()
    s.add(*cons)
    s.add(R_final + S_final > L)  # negation of the invariant
    assert_proved(s, f"R + S <= L after any interleaving of {_N} CAS reservers")


def test_cas_snapshot_equality_serialises_commits():
    """
    Lemma: two reservers that read the SAME snapshot cannot both commit
    (on a reserves-only trace with cost >= 1: R strictly increases between
    distinct commit slots, so the later CAS condition must fail).

    NOTE: with releases interleaved, ABA (R returning to r0) is possible in
    principle — but the main theorem above does NOT rely on this lemma; it
    relies only on snapshot == state-at-commit, which the ConditionExpression
    gives you regardless of ABA.  ABA re-admission still passes the ceiling
    against the CURRENT state, so it is harmless.  This lemma is belt and
    braces for the reserves-only case.
    """
    cons, commit, snapR, snapS, *_ = _cas_model(_N, enforce_cas=True)
    s = _solver()
    s.add(*cons)
    s.add(commit[0], commit[1])
    s.add(snapR[0] == snapR[1], snapS[0] == snapS[1])
    assert_proved(s, "two committers cannot share a snapshot (reserves-only)")


def test_sanity_without_cas_condition_over_admission_is_found():
    """Delete the ConditionExpression and Z3 must find over-admission (SAT)."""
    cons, *_rest = _cas_model(2, enforce_cas=False)
    _, _, _, R_final, S_final, L = _rest
    s = _solver()
    s.add(*cons)
    s.add(R_final + S_final > L)
    m = assert_counterexample_exists(s, "over-admission without the CAS condition")
    # e.g. L=1, R0=S0=0, both read (0,0), both pass the ceiling, both commit.
    del m


def test_pool_invariant_inductive_step_all_ops():
    """
    Inductive step for MIXED traces of ANY length: assume the invariant
    (R>=0, S>=0, R+S<=L) plus the per-op guard; prove each op preserves it.
    Combined with the bounded interleaving proof, this covers unbounded N.

    Per-op assumptions (each justified elsewhere):
      * settle/release/reap operate on a live hold of amount `res`, and the
        ledger invariant R == sum(live holds) gives res <= R.  That ledger
        invariant is exactly what test_hold_reclaimed_exactly_once and the
        Hypothesis machine establish (each hold subtracted from R exactly
        once), so this is not circular — it is compositional.
      * The FALLBACK settle (S += actual with NO -reserved) is deliberately
        EXCLUDED here: it does NOT preserve R+S<=L.  See
        test_strict_ceiling_is_false_under_reaper_fallback_race.
    """
    L, R, S, res, act = z3.Ints("L R S res act")
    inv = z3.And(R >= 0, S >= 0, R + S <= L)

    def preserved(name, guard, dR, dS):
        s = _solver()
        s.add(inv, guard)
        s.add(z3.Not(z3.And(R + dR >= 0, S + dS >= 0, (R + dR) + (S + dS) <= L)))
        assert_proved(s, f"{name} preserves (R>=0, S>=0, R+S<=L)")

    # reserve: CAS => ceiling checked against the CURRENT state at commit
    preserved("reserve", z3.And(res >= 1, R + S + res <= L), res, 0)
    # settle (main path): -reserved, +actual, actual <= reserved, hold live
    preserved("settle", z3.And(res >= 1, res <= R, act >= 0, act <= res), -res, act)
    # release: -reserved, hold live
    preserved("release", z3.And(res >= 1, res <= R), -res, 0)
    # reaper reclaim: -hold.amount, hold live (guarded by attribute_exists)
    preserved("reap", z3.And(res >= 1, res <= R), -res, 0)


# ===========================================================================
# 2. SETTLE-ONCE CONSERVATION (_claim_finalize one-shot flag)
# ===========================================================================
#
# Model: four finalizer sites.  Site 0 = invoke-error (refund+release: money
# effect on the POOL is -reserved, +0).  Sites 1..3 = mid-stream-error /
# clean-completion / disconnect-finally (settle: -reserved, +actual).
#
# The claim flag is flipped under a lock BEFORE any money write, so the
# FIRST site to attempt the claim — in real time — is the only one whose
# writes run.  We model attempt times as distinct integers (the lock
# serialises claim attempts; ties are impossible).  This faithfully covers
# the CancelledError race: the settle running in asyncio.to_thread has
# ALREADY claimed before its await point, so even though its thread commits
# after the CancelledError propagates, the disconnect-finally site attempts
# the claim LATER and is refused.
#
# The disconnect `finally` block always executes => attempt[3] is True, so
# at least one finalizer always attempts (no leaked reservation).

_FIN = ["invoke_error", "mid_stream_error", "clean_completion", "disconnect_finally"]


def _finalizer_model(claim_guard: bool):
    R0, S0, reserved, actual = z3.Ints("R0 S0 reserved actual")
    cons = [R0 >= 0, S0 >= 0, reserved >= 1, actual >= 0, actual <= reserved]

    attempt = [z3.Bool(f"attempt_{n}") for n in _FIN]
    t = [z3.Int(f"t_{n}") for n in _FIN]
    cons.append(z3.Distinct(*t))
    cons.append(attempt[3])  # the `finally` always runs

    runs = []
    for k in range(4):
        earlier = z3.Or(
            [z3.And(attempt[j], t[j] < t[k]) for j in range(4) if j != k]
        )
        if claim_guard:
            runs.append(z3.And(attempt[k], z3.Not(earlier)))  # first claimant wins
        else:
            runs.append(attempt[k])  # BROKEN: no flag, everyone who attempts writes

    R1 = R0 + reserved  # state after the reserve committed
    dS = [z3.IntVal(0), actual, actual, actual]
    R_final = R1 + z3.Sum([z3.If(runs[k], -reserved, z3.IntVal(0)) for k in range(4)])
    S_final = S0 + z3.Sum([z3.If(runs[k], dS[k], z3.IntVal(0)) for k in range(4)])
    n_runs = z3.Sum([z3.If(runs[k], z3.IntVal(1), z3.IntVal(0)) for k in range(4)])
    return cons, R0, S0, actual, R_final, S_final, n_runs


def test_settle_once_exactly_one_finalizer_runs():
    cons, _R0, _S0, _act, _Rf, _Sf, n_runs = _finalizer_model(claim_guard=True)
    s = _solver()
    s.add(*cons)
    s.add(n_runs != 1)
    assert_proved(s, "exactly one of the four finalizers runs its money writes")


def test_settle_once_conservation():
    """R returns to pre-reserve exactly once; S grows by 0 or actual, never 2x."""
    cons, R0, S0, actual, R_final, S_final, _ = _finalizer_model(claim_guard=True)
    s = _solver()
    s.add(*cons)
    prop = z3.And(
        R_final == R0,                                    # reservation fully removed, once
        z3.Or(S_final == S0, S_final == S0 + actual),     # spend once (settle) or zero (release)
        S_final <= S0 + actual,                           # NEVER twice
        R_final >= R0,                                    # NEVER double-subtracted
    )
    s.add(z3.Not(prop))
    assert_proved(s, "settle-once conservation (R_final == R0; ΔS in {0, actual})")


def test_sanity_without_claim_flag_double_settle_is_found():
    cons, R0, S0, actual, R_final, S_final, _ = _finalizer_model(claim_guard=False)
    s = _solver()
    s.add(*cons)
    s.add(actual >= 1)
    s.add(z3.Or(S_final > S0 + actual, R_final < R0))  # double-billed or double-refunded
    assert_counterexample_exists(s, "double settle without _claim_finalize")


# ===========================================================================
# 3. REAPER IDEMPOTENCY / HOLD RECLAIMED EXACTLY ONCE
# ===========================================================================
#
# Model: one hold of amount c.  Four possible actors race on it: the owning
# request's settle OR release (mutually exclusive — proved by the finalizer
# theorem above, assumed here compositionally), plus TWO reaper firings
# (models the reaper racing itself / re-running after a partial crash).
#
# Every actor's transaction includes Delete(HOLD) with attribute ConditionExpression attribute_exists(pk).  DynamoDB rejects the whole
# transaction if the hold row is already gone, so only the actor whose
# delete lands FIRST commits its money writes.  With delete_condition=False
# we model the buggy variant: an unconditioned Delete on a missing item is
# a silent no-op, so every attempter's money writes commit.
#
#   k=0  owner settle       (dS = actual)
#   k=1  owner release      (dS = 0)
#   k=2  reaper firing A    (dS = metered fallback charge)
#   k=3  reaper firing B    (same reaper re-run after a partial crash)


def _reaper_model(delete_condition: bool):
    R0, S0 = z3.Ints("rp_R0 rp_S0")
    c, actual, metered = z3.Ints("rp_c rp_actual rp_metered")
    attempt = [z3.Bool(f"rp_attempt_{k}") for k in range(4)]
    t = [z3.Int(f"rp_t_{k}") for k in range(4)]

    cons = [
        R0 >= c, S0 >= 0, c >= 1,
        actual >= 0,
        metered >= 0, metered <= actual,   # meter lags true usage
        z3.Distinct(*t),
        z3.Or(*attempt),                                # someone acts on the hold
        z3.Not(z3.And(attempt[0], attempt[1])),         # settle-once (proved above,
    ]                                                   # assumed compositionally)

    runs = []
    for k in range(4):
        earlier = z3.Or(
            [z3.And(attempt[j], t[j] < t[k]) for j in range(4) if j != k]
        )
        if delete_condition:
            runs.append(z3.And(attempt[k], z3.Not(earlier)))  # first delete wins
        else:
            runs.append(attempt[k])  # BROKEN: unconditioned delete, all commit

    dS = [actual, z3.IntVal(0), metered, metered]
    R_final = R0 + z3.Sum([z3.If(runs[k], -c, z3.IntVal(0)) for k in range(4)])
    S_final = S0 + z3.Sum([z3.If(runs[k], dS[k], z3.IntVal(0)) for k in range(4)])
    n_reclaims = z3.Sum([z3.If(runs[k], z3.IntVal(1), z3.IntVal(0)) for k in range(4)])
    return cons, R0, S0, c, actual, metered, R_final, S_final, n_reclaims


def test_reaper_hold_reclaimed_exactly_once():
    """Whoever wins the delete race, the hold is reclaimed exactly once:
    R drops by exactly c, never goes negative, and S never decreases."""
    cons, R0, S0, c, _a, _m, R_final, S_final, n = _reaper_model(delete_condition=True)
    s = _solver()
    s.add(*cons)
    prop = z3.And(
        n == 1,                # exactly one actor commits
        R_final == R0 - c,     # hold reclaimed exactly once
        R_final >= 0,          # (follows from R0 >= c, stated for clarity)
        S_final >= S0,         # money never un-spent
    )
    s.add(z3.Not(prop))
    assert_proved(s, "reaper idempotency: hold reclaimed exactly once")


def test_sanity_without_delete_condition_double_reclaim_is_found():
    """Model validation: drop attribute_exists(pk) from the Delete and Z3
    must find a run where the hold is reclaimed twice (double refund and/or
    double fallback charge)."""
    cons, R0, S0, c, _a, metered, R_final, S_final, _n = _reaper_model(
        delete_condition=False
    )
    s = _solver()
    s.add(*cons)
    s.add(metered >= 1)
    s.add(z3.Or(
        R_final < R0 - c,             # double refund of the reservation
        S_final > S0 + metered + 0,   # ... or, with two reapers racing,
    ))                                #     the fallback charge lands twice
    assert_counterexample_exists(s, "double reclaim without conditioned Delete")


# ===========================================================================
# COUNTEREXAMPLE: THE STRICT CEILING IS *NOT* AN INVARIANT
# ===========================================================================

def test_strict_ceiling_is_false_under_reaper_fallback_race():
    """CE: R + S <= L is NOT invariant, and we prove it with a witness.

    Admission checks R + S + c <= L against the *estimate* c.  A stream can
    meter past its estimate before cutoff enforcement lands (the meter is
    updated by heartbeats; the kill is asynchronous).  If the owner then
    crashes, the reaper's fallback charge bills the metered amount — which
    exceeds c — pushing committed spend above the ceiling.

    This is not a bug in the finalizer or the reaper (both were proved
    correct above); it is a fundamental property of estimate-based
    admission.  It is exactly why the stateful suite below asserts

        R + S <= L + overshoot_debt

    rather than the strict ceiling, and why the ops runbook says overshoot
    is bounded by (in-flight streams x per-heartbeat token delta), not zero.
    """
    s = _solver()
    R0, S0, L = z3.Ints("ce_R0 ce_S0 ce_L")
    c, metered = z3.Ints("ce_c ce_metered")
    s.add(R0 >= 0, S0 >= 0, L >= 1, c >= 1)
    s.add(R0 + S0 + c <= L)          # admission check passed at reserve time
    s.add(metered > c)               # stream metered past its estimate
    R1 = R0 + c                      # hold placed
    R_final = R1 - c                 # reaper reclaims the hold ...
    S_final = S0 + metered           # ... and fallback-charges the meter
    s.add(R_final + S_final > L)     # strict ceiling violated
    assert_counterexample_exists(
        s, "R + S <= L violated by metered overshoot + reaper fallback charge"
    )
