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

# ===========================================================================
# 4. LEDGER PHASE 2 — RECLAIM + LATE_SETTLE: NO DOUBLE-RETURN, SPEND EXACTLY-ONCE
# ===========================================================================
#
# Phase 2 closes the revenue leak: when the reaper reclaims a hold FIRST
# (writing a RECLAIM terminal that returns `reserved`), a late settle must
# still record the spend — via a LATE_SETTLE on a DISTINCT sk with
# reserved_delta == 0 — instead of blind-returning (Phase 1's leak).
#
# The terminal money moves SETTLE / RELEASE / RECLAIM share ONE sk under
# attribute_not_exists, so AT MOST ONE terminal commits per hold (the reserved
# return happens exactly once).  LATE_SETTLE is a separate sk, also under
# attribute_not_exists (at most one), and its own txn carries a ConditionCheck
# that the terminal IS a RECLAIM.
#
# We model, for one hold, the boolean commit of each of the four writers, with
# the storage guards encoded exactly:
#   * terminal cell: at most one of {settle, release, reclaim} commits
#   * late cell:     at most one late_settle commits
#   * late ⇒ reclaim committed  (the ConditionCheck: terminal.event_type=RECLAIM)
# and prove:  reserved returned ∈ {0, R} (never 2R), settled ∈ {0, actual}
# (never 2·actual), and — the liveness-flavoured safety the leak was about —
# IF the reaper reclaimed AND a late settle committed, THEN settled == actual
# (the spend is NOT lost).


def _phase2_ledger_model(*, late_requires_reclaim: bool):
    R, actual = z3.Ints("p2_R p2_actual")
    settle = z3.Bool("p2_settle")
    release = z3.Bool("p2_release")
    reclaim = z3.Bool("p2_reclaim")
    late = z3.Bool("p2_late")
    cons = [R >= 1, actual >= 0, actual <= R]

    # Terminal cell exclusion (attribute_not_exists on the shared TERMINAL sk):
    # at most ONE of the three terminal money moves commits.
    cons.append(
        z3.AtMost(settle, release, reclaim, 1)
    )
    # LATE_SETTLE lives on its own sk; its guard is the terminal-is-RECLAIM
    # ConditionCheck. With the guard, late ⇒ reclaim. The broken variant drops
    # that link (models a mis-route that writes LATE without a RECLAIM).
    if late_requires_reclaim:
        cons.append(z3.Implies(late, reclaim))

    # reserved returned: each terminal returns exactly R; LATE returns 0.
    reserved_returned = z3.Sum([
        z3.If(settle, R, z3.IntVal(0)),
        z3.If(release, R, z3.IntVal(0)),
        z3.If(reclaim, R, z3.IntVal(0)),
        # late: reserved_delta == 0 by construction (no term here)
    ])
    # settled recorded: SETTLE records actual; RECLAIM records 0; LATE records
    # actual; RELEASE records 0.
    settled_recorded = z3.Sum([
        z3.If(settle, actual, z3.IntVal(0)),
        z3.If(late, actual, z3.IntVal(0)),
    ])
    return cons, R, actual, settle, release, reclaim, late, reserved_returned, settled_recorded


def test_phase2_no_double_return_and_spend_exactly_once():
    """With the terminal-cell exclusion + late⇒reclaim guard: reserved is
    returned at most once (∈{0,R}), settled is recorded at most once
    (∈{0,actual}), and a reaped-then-late-settled hold records the spend
    (reclaim ∧ late ⇒ settled == actual) — the revenue leak cannot recur."""
    (cons, R, actual, settle, release, reclaim, late,
     reserved_returned, settled_recorded) = _phase2_ledger_model(late_requires_reclaim=True)
    s = _solver()
    s.add(*cons)
    prop = z3.And(
        z3.Or(reserved_returned == 0, reserved_returned == R),      # never 2R
        z3.Or(settled_recorded == 0, settled_recorded == actual),   # never 2·actual
        # the leak-closure property: reaped AND late-settled ⇒ spend recorded.
        z3.Implies(z3.And(reclaim, late), settled_recorded == actual),
        # RECLAIM alone (no late settle) records no spend but DID return reserved.
        z3.Implies(z3.And(reclaim, z3.Not(late)),
                   z3.And(settled_recorded == 0, reserved_returned == R)),
    )
    s.add(z3.Not(prop))
    assert_proved(s, "Phase 2: no double-return, spend exactly-once, leak closed")


def test_phase2_late_settle_cannot_double_count_with_settle():
    """A LATE_SETTLE and a SETTLE terminal cannot both record the same spend:
    late⇒reclaim and terminal exclusion make settle∧late unsatisfiable, so
    settled is never 2·actual."""
    (cons, R, actual, settle, release, reclaim, late,
     _rr, settled_recorded) = _phase2_ledger_model(late_requires_reclaim=True)
    s = _solver()
    s.add(*cons)
    s.add(actual >= 1)
    s.add(settle, late)  # try to force the double-count
    assert_proved(s, "settle ∧ late is impossible (no double-count of spend)")


def _external_capture_model(*, external_forbids_late: bool):
    """Phase-2 terminal model + a `source=external` flag. The external
    authorize/capture contract (Fable authcap D-2) adds ONE rule to the base
    model: an external hold never takes LATE_SETTLE (its capture window is
    unbounded, so late-billing a reclaimed hold could break the budget
    invariant). We model that as `external ⇒ ¬late`. The broken variant drops
    the rule to prove the harness bites."""
    (cons, R, actual, settle, release, reclaim, late,
     reserved_returned, settled_recorded) = _phase2_ledger_model(late_requires_reclaim=True)
    external = z3.Bool("ext_source")
    if external_forbids_late:
        cons.append(z3.Implies(external, z3.Not(late)))
    return (cons, R, actual, settle, release, reclaim, late, external,
            reserved_returned, settled_recorded)


def test_external_reclaimed_hold_records_no_spend():
    """authcap D-2 (safety): an EXTERNAL hold the reaper reclaimed records NO
    spend — no LATE_SETTLE recovery — so its reserved is returned exactly once
    and settled stays 0. The terminal set for an external hold is closed to
    {SETTLE, RELEASE, RECLAIM}; late is unreachable when source=external."""
    (cons, R, actual, settle, release, reclaim, late, external,
     reserved_returned, settled_recorded) = _external_capture_model(external_forbids_late=True)
    s = _solver()
    s.add(*cons)
    s.add(external)  # the hold is external
    prop = z3.And(
        z3.Not(late),  # external ⇒ never late-settled
        # a reclaimed external hold: reserved returned once, NO spend recorded.
        z3.Implies(reclaim, z3.And(settled_recorded == 0, reserved_returned == R)),
        # settled is still at-most-once (never 2·actual) and reserved never 2R.
        z3.Or(settled_recorded == 0, settled_recorded == actual),
        z3.Or(reserved_returned == 0, reserved_returned == R),
    )
    s.add(z3.Not(prop))
    assert_proved(s, "external reclaimed hold records no spend (D-2)")


def test_sanity_external_without_late_ban_allows_late_bill():
    """Model validation: drop the external⇒¬late rule and Z3 finds a run where an
    external hold IS late-settled after reclaim (settled == actual) — the exact
    unbounded-window billing D-2 forbids."""
    (cons, R, actual, settle, release, reclaim, late, external,
     _rr, settled_recorded) = _external_capture_model(external_forbids_late=False)
    s = _solver()
    s.add(*cons)
    s.add(actual >= 1)
    s.add(external, reclaim, late)          # external hold, reaped, then late-billed
    s.add(settled_recorded == actual)       # spend recorded on a reclaimed external hold
    assert_counterexample_exists(
        s, "external hold late-billed after reclaim when the ban is removed"
    )


def test_sanity_phase2_without_late_guard_double_count_is_found():
    """Model validation: drop the late⇒reclaim ConditionCheck and Z3 finds a
    run where a SETTLE terminal AND a LATE_SETTLE both record the spend —
    settled == 2·actual (the bug the ConditionCheck prevents)."""
    (cons, R, actual, settle, release, reclaim, late,
     _rr, settled_recorded) = _phase2_ledger_model(late_requires_reclaim=False)
    s = _solver()
    s.add(*cons)
    s.add(actual >= 1)
    s.add(settle, late)                      # both writers land
    s.add(settled_recorded > actual)         # double-counted spend
    assert_counterexample_exists(
        s, "double-counted spend when LATE_SETTLE is not gated on RECLAIM"
    )


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


# ===========================================================================
# 5. LAYER 5 RATING — rate_usage ARITHMETIC (ceil rounding, monotone, subadditive)
# ===========================================================================
#
# rate_usage rates real usage against a FROZEN snapshot with per-component
# ceil division: cost_c = ceildiv(tokens_c * rate_c, 10^6). These prove the
# money-integrity properties of that pure function, independent of any race
# (the freeze design removed rating's concurrency — see test_rating_properties
# for the flip-race replay). Model ONE component; the total is their sum, so
# each property lifts componentwise.

_MTOK = 1_000_000
# Bounds used across the rating proofs (also the documented overflow envelope):
# tokens <= 10^10 (10 GT), rate <= 10^9 microUSD/MTok ($1000/MTok).
_MAX_TOKENS = 10**10
_MAX_RATE = 10**9


def _ceildiv(num, den):
    # z3 integer ceil division for non-negative num, positive den.
    return (num + den - 1) / den


def test_rating_ceil_never_undercharges_and_is_tightly_bounded():
    """For one component: exact <= cost < exact + 1 microUSD-worth of rounding,
    i.e. 0 <= cost*10^6 - tokens*rate < 10^6. Never under-charges (cost*10^6 >=
    tokens*rate), never over by a full microUSD."""
    tokens, rate = z3.Ints("tokens rate")
    s = _solver()
    s.add(tokens >= 0, tokens <= _MAX_TOKENS, rate >= 0, rate <= _MAX_RATE)
    cost = _ceildiv(tokens * rate, _MTOK)
    # negation: either under-charge, or rounded up by >= a full microUSD.
    s.add(z3.Or(cost * _MTOK < tokens * rate,
                cost * _MTOK >= tokens * rate + _MTOK))
    assert_proved(s, "ceil rating: 0 <= cost*10^6 - tokens*rate < 10^6")


def test_rating_total_rounding_bound_four_components():
    """The 4-component total rounds up by strictly less than 4 microUSD vs the
    exact real-valued sum (each component < 1 microUSD of rounding)."""
    ti, to, tr, tw = z3.Ints("ti to tr tw")
    ri, ro, rr, rw = z3.Ints("ri ro rr rw")
    s = _solver()
    for t in (ti, to, tr, tw):
        s.add(t >= 0, t <= _MAX_TOKENS)
    for r in (ri, ro, rr, rw):
        s.add(r >= 0, r <= _MAX_RATE)
    total = (_ceildiv(ti * ri, _MTOK) + _ceildiv(to * ro, _MTOK)
             + _ceildiv(tr * rr, _MTOK) + _ceildiv(tw * rw, _MTOK))
    exact_x = ti * ri + to * ro + tr * rr + tw * rw  # = exact_total * 10^6
    s.add(z3.Or(total * _MTOK < exact_x,              # under-charge
                total * _MTOK >= exact_x + 4 * _MTOK))  # over by >= 4 microUSD
    assert_proved(s, "4-component total rounds up by < 4 microUSD, never under")


def test_rating_monotone_in_tokens():
    """More tokens never charges less (a component's cost is non-decreasing)."""
    t1, t2, rate = z3.Ints("t1 t2 rate")
    s = _solver()
    s.add(t1 >= 0, t2 >= 0, rate >= 0, rate <= _MAX_RATE, t1 <= t2)
    s.add(_ceildiv(t1 * rate, _MTOK) > _ceildiv(t2 * rate, _MTOK))  # negation
    assert_proved(s, "rating monotone non-decreasing in tokens")


def test_rating_subadditive_over_split_settle():
    """ceil(a) + ceil(b) >= ceil(a+b): splitting usage into two settles can only
    over-charge, never under — so a future partial/split settle is safe against
    the budget (no under-billing by fragmentation)."""
    ta, tb, rate = z3.Ints("ta tb rate")
    s = _solver()
    s.add(ta >= 0, tb >= 0, rate >= 0, rate <= _MAX_RATE,
          ta <= _MAX_TOKENS, tb <= _MAX_TOKENS)
    split = _ceildiv(ta * rate, _MTOK) + _ceildiv(tb * rate, _MTOK)
    whole = _ceildiv((ta + tb) * rate, _MTOK)
    s.add(split < whole)  # negation of subadditivity
    assert_proved(s, "ceil rating subadditive: split settle never under-charges")


def test_sanity_floor_rounding_would_undercharge():
    """Model validation: FLOOR division (the wrong rounding) admits a strict
    under-charge — Z3 finds tokens*rate not divisible by 10^6 charged as less
    than the exact cost. This is why rating pins ceil."""
    tokens, rate = z3.Ints("f_tokens f_rate")
    s = _solver()
    s.add(tokens >= 1, tokens <= _MAX_TOKENS, rate >= 1, rate <= _MAX_RATE)
    floor_cost = (tokens * rate) / _MTOK  # z3 int division = floor for >=0
    s.add(floor_cost * _MTOK < tokens * rate)  # a real under-charge exists
    assert_counterexample_exists(s, "floor rounding under-charges the budget")
