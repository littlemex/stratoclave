"""
Formal (SMT) verification of Stratoclave's per-model QUOTA invariants with Z3.

Companion to tests/test_billing_formal_z3.py (pooled budget); same method:
each invariant is proved by encoding the quota logic as constraints and
asserting the NEGATION is UNSAT — Z3 has searched every interleaving the
encoding can represent and found no violation.  EVERY proof has a paired
`sanity` test that deletes the guard under proof and asserts Z3 FINDS the
bug (SAT), so the harness is provably non-vacuous.  Three of those sanity
tests reproduce real bugs found by hand (BUG-1/2/3 below).

GLOBAL SIMPLIFYING ASSUMPTIONS
------------------------------
Reused verbatim from the pooled suite:
 A1. Money/tokens are unbounded mathematical integers (micro-USD).
 A2. DynamoDB writes touching a single item SERIALISE: committed transactions
     take effect in some total order and a ConditionExpression is evaluated
     against the item state at the commit point.  AXIOM (AWS documented).
 A3. `status == active` folded away (suspension only removes behaviours).
 A4. Failed CAS attempts write nothing; only COMMITTING attempts modelled.
 A5. Exactly-once dedup for lost-ack retries assumed (money effect applied
     at most once); its implementation needs its own tests.

Quota-specific axioms:
 A6. TransactWriteItems is ALL-OR-NOTHING: if ANY ConditionExpression in the
     transaction fails, NO item in the transaction is written.  This is what
     makes the QuotaExhausted cascade-advance safe (Section 4).  AXIOM.
 A7. DynamoDB semantics on a MISSING attribute:  `used <= :headroom` is
     FALSE when `used` is absent;  `attribute_not_exists(used)` is TRUE;
     `ADD used :d` seeds from 0.  Verified against REAL DynamoDB (moto
     diverges on related expressions!) — this is an AXIOM here and MUST be
     covered by the differential layer, not by SMT.
 A8. settle/release key off the period string CAPTURED AT RESERVE TIME
     (`context.quota_period`), never a recomputed current_period().  The
     proof in Section 5 shows WHY this matters; the plumbing itself is a
     code fact checked by example tests.
 A9. Finalizer calls on one request context are serialised in-process
     (same claim-flag/lock structure the pooled suite proves in its
     Section 2).  Section 3 models them as an ordered trace.
A10. Ledger side condition: at settle/release time the reservation being
     removed is LIVE, i.e. `res <= used` on the row.  Established
     compositionally by Section 3 (idempotency) + the stateful machine —
     not circular; see test_sanity_inductive_settle_requires_ledger_side_condition.

NOT PROVED HERE (other layers): A7's real-DynamoDB fidelity (differential);
pk/sk/period string construction and TTL expiry arithmetic (example tests);
which finalizer runs exactly once (pooled suite Section 2 + Hypothesis);
soft_check_exhausted staleness (optimization only — it can only BLOCK, never
admit, so safety never rests on it); ClientError code filtering in
_adjust_used; retry/backoff liveness.
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
# 1. NO OVER-ADMISSION UNDER CONCURRENT QUOTA CAS  (includes BUG-1)
# ===========================================================================
#
# Model: N concurrent quota reservers on ONE (scope, model, period) item.
# Unlike the pool, the item's `used` attribute may be ABSENT, so row state is
# a pair (exists, used):  exists0/U0 is the initial state; a committed
# reserve makes the attribute exist and ADDs its amount (A7: ADD seeds 0).
#
# The ConditionExpression is branch-selected client-side on the sign of
# headroom = limit - amount (THE guard under proof):
#   headroom >= 0 : attribute_not_exists(used) OR used <= :headroom
#   headroom <  0 : used <= :headroom          (false on missing attr AND on
#                                               any value — always cancels)
# Encoded per A2/A7 against the row state at the commit slot:
#   fixed : (headroom >= 0 AND NOT exists)  OR  (exists AND used <= headroom)
#   bug1  : (NOT exists)                    OR  (exists AND used <= headroom)
#           -- the shipped bug: attribute_not_exists short-circuits TRUE on a
#              fresh row even for an oversized request.
#   stale : the FIXED condition, but evaluated against the INITIAL state
#           instead of the commit-slot state — models losing A2 / the
#           ConditionExpression entirely (classic lost update).
#
# Per-test simplification: reserves-only trace; Section 6 closes the gap to
# mixed traces of any length by induction (same structure as the pool suite).

_N = 5  # bounded reserver count; the inductive-step test covers unbounded N


def _quota_cas_model(n: int, mode: str):
    assert mode in ("fixed", "bug1", "stale")
    L, U0 = z3.Ints("L U0")
    exists0 = z3.Bool("exists0")
    cons = [L >= 0, z3.Implies(exists0, z3.And(U0 >= 0, U0 <= L))]
    base = z3.If(exists0, U0, z3.IntVal(0))

    amt = [z3.Int(f"amt{i}") for i in range(n)]
    commit = [z3.Bool(f"commit{i}") for i in range(n)]
    pos = [z3.Int(f"pos{i}") for i in range(n)]

    for i in range(n):
        cons.append(amt[i] >= 1)
        cons.append(z3.Implies(commit[i], z3.And(pos[i] >= 0, pos[i] < n)))

    # A2: committed single-item txns serialise -> distinct commit slots.
    for i in range(n):
        for j in range(i + 1, n):
            cons.append(z3.Implies(z3.And(commit[i], commit[j]), pos[i] != pos[j]))

    def exists_at(t):
        # Attribute exists at slot t iff it existed initially or any commit
        # landed strictly earlier (ADD creates it; A7).
        return z3.Or(exists0, *[z3.And(commit[j], pos[j] < t) for j in range(n)])

    def used_at(t):
        return base + z3.Sum(
            [z3.If(z3.And(commit[j], pos[j] < t), amt[j], z3.IntVal(0)) for j in range(n)]
        )

    for i in range(n):
        headroom = L - amt[i]
        t = z3.IntVal(0) if mode == "stale" else pos[i]
        ex, u = exists_at(t), used_at(t)
        if mode == "bug1":
            cond = z3.Or(z3.Not(ex), z3.And(ex, u <= headroom))
        else:
            cond = z3.Or(z3.And(headroom >= 0, z3.Not(ex)), z3.And(ex, u <= headroom))
        cons.append(z3.Implies(commit[i], cond))

    used_final = used_at(z3.IntVal(n))  # all committed slots are < n
    return cons, commit, amt, exists0, used_final, L


def test_quota_no_over_admission_under_concurrent_cas():
    """For EVERY interleaving of N quota reservers — any initial row state,
    any amounts INCLUDING oversized (amount > limit) — used <= limit holds
    after all commits.  This subsumes BUG-1 (an oversized commit on a fresh
    row would itself set used = amount > limit)."""
    cons, _, _, _, used_final, L = _quota_cas_model(_N, "fixed")
    s = _solver()
    s.add(*cons)
    s.add(used_final > L)  # negation of the invariant
    assert_proved(s, f"used <= limit after any interleaving of {_N} quota CAS reservers")


def test_sanity_stale_snapshot_over_admission_is_found():
    """Delete the at-commit ConditionExpression (evaluate against the initial
    state instead) and Z3 must find over-admission (SAT) — e.g. two fresh
    reservers of L each both pass, used = 2L > L."""
    cons, _, _, _, used_final, L = _quota_cas_model(2, "stale")
    s = _solver()
    s.add(*cons)
    s.add(used_final > L)
    m = assert_counterexample_exists(s, "over-admission with a stale condition")
    del m


def test_bug1_oversized_first_request_never_commits():
    """BUG-1 theorem, stated constructively: with the FIXED branch selection,
    a single request with amount > limit against a FRESH row (no `used`
    attribute) can NEVER commit — headroom < 0 drops the
    attribute_not_exists branch, and `used <= :headroom` is false both on
    the missing attribute (A7) and on any value."""
    cons, commit, amt, exists0, _, L = _quota_cas_model(1, "fixed")
    s = _solver()
    s.add(*cons)
    s.add(z3.Not(exists0), amt[0] > L, commit[0])  # negation: it DID commit
    assert_proved(s, "an oversized request on a fresh row can never commit")


def test_sanity_bug1_not_exists_branch_admits_oversized_request():
    """Reproduce BUG-1: keep the attribute_not_exists branch when headroom<0
    and Z3 immediately admits a single oversized request past the limit."""
    cons, commit, amt, exists0, used_final, L = _quota_cas_model(1, "bug1")
    s = _solver()
    s.add(*cons)
    s.add(z3.Not(exists0), amt[0] > L)
    s.add(used_final > L)
    m = assert_counterexample_exists(
        s, "BUG-1: oversized first request admitted via attribute_not_exists"
    )
    # e.g. L=1, fresh row, amt=2: NOT exists short-circuits TRUE, used=2>1.
    del m


# ===========================================================================
# 2. PHANTOM / NEGATIVE-DRIFT PREVENTION  (BUG-2)
# ===========================================================================
#
# Model: ONE settle/release adjustment (`ADD used :d`, d <= -1) against a row
# in any state.  The guard under proof is `attribute_exists(used)`: applied
# iff the attribute exists.  Without it, ADD on a missing attribute seeds
# from 0 (A7) and CREATES a row with negative `used` — which, if a limit is
# configured later, over-admits by |d| (second sanity shows this
# constructively).

def _adjust_model(gated: bool):
    exists0 = z3.Bool("exists0")
    used0, delta = z3.Ints("used0 delta")
    cons = [z3.Implies(exists0, used0 >= 0)]
    applied = exists0 if gated else z3.BoolVal(True)
    exists1 = z3.Or(exists0, applied)
    base = z3.If(exists0, used0, z3.IntVal(0))
    used1 = z3.If(applied, base + delta, base)
    return cons, exists0, exists1, used1, delta


def test_bug2_missing_row_stays_missing_on_settle_release():
    """A scope that was never reserved (row absent) MUST stay absent through
    any settle/release: the attribute_exists gate makes the update a no-op."""
    cons, exists0, exists1, _, delta = _adjust_model(gated=True)
    s = _solver()
    s.add(*cons)
    s.add(z3.Not(exists0), delta <= -1)
    s.add(exists1)  # negation: the row was created
    assert_proved(s, "gated ADD never creates a row on an absent scope")


def test_sanity_bug2_ungated_add_creates_negative_phantom_row():
    """Delete the attribute_exists gate: Z3 must create the phantom row with
    negative `used` (SAT) — the exact BUG-2 state."""
    cons, exists0, exists1, used1, delta = _adjust_model(gated=False)
    s = _solver()
    s.add(*cons)
    s.add(z3.Not(exists0), delta <= -1)
    s.add(exists1, used1 < 0)
    m = assert_counterexample_exists(s, "BUG-2: phantom negative row via ungated ADD")
    del m


def test_sanity_bug2_phantom_row_later_over_admits():
    """Consequence proof-by-construction: the phantom negative row lets a
    LATER reserve (fixed condition!) admit amount a > limit — real spend on
    that scope was 0, yet a > L is admitted.  This is why 'absent stays
    absent' is a safety property, not hygiene."""
    cons, exists0, exists1, used1, delta = _adjust_model(gated=False)
    L, a = z3.Ints("L a")
    s = _solver()
    s.add(*cons)
    s.add(z3.Not(exists0), delta <= -1, exists1, used1 < 0)
    s.add(L >= 0, a >= 1)
    s.add(used1 <= L - a)  # the FIXED reserve condition passes on the phantom row
    s.add(a > L)           # ... yet admits more than the whole limit
    m = assert_counterexample_exists(s, "phantom row over-admission (a > L)")
    del m


# ===========================================================================
# 3. IDEMPOTENT SETTLE/RELEASE  (BUG-3)
# ===========================================================================
#
# Model: after a reserve of `res` the row holds used0 = B + res (B = prior
# settled baseline).  K finalizer calls run in order (A9), each an arbitrary
# choice of settle(actual)/release.  Each call reads the in-memory
# amt = context.quota_reserved_amount, no-ops if amt <= 0, otherwise ADDs
# (actual - amt) or (-amt), and — THE GUARD UNDER PROOF — clears the context
# amount in a `finally`.  Property: B <= used_final <= B + res, i.e. the
# reservation is subtracted AT MOST ONCE and never more than it added.  This
# is exactly what BUG-3 violated (double-release / release-after-settle).

_K = 3


def _finalizer_model(k: int, clears: bool):
    B, res = z3.Ints("B res")
    cons = [B >= 0, res >= 1]
    used = B + res       # row state right after the reserve
    ctx = res            # in-memory context.quota_reserved_amount
    is_settle = [z3.Bool(f"is_settle{j}") for j in range(k)]
    act = [z3.Int(f"act{j}") for j in range(k)]
    for j in range(k):
        cons.append(z3.And(act[j] >= 0, act[j] <= res))  # actual <= reserved
        amt = ctx
        applied = amt >= 1  # the `if amt <= 0: return` guard
        delta = z3.If(is_settle[j], act[j] - amt, -amt)
        used = used + z3.If(applied, delta, z3.IntVal(0))
        if clears:
            ctx = z3.IntVal(0)  # finally: context.quota_reserved_amount = 0
        # else: ctx unchanged — the BUG-3 code shape
    return cons, used, B, res


def test_bug3_finalizers_idempotent_used_never_below_baseline():
    """For EVERY sequence of K settle/release finalizer calls in any order,
    B <= used_final <= B + res: the second and later calls are no-ops."""
    cons, used_final, B, res = _finalizer_model(_K, clears=True)
    s = _solver()
    s.add(*cons)
    s.add(z3.Not(z3.And(used_final >= B, used_final <= B + res)))
    assert_proved(s, f"any {_K}-call finalizer sequence keeps B <= used <= B+res")


def test_sanity_bug3_uncleared_context_drives_used_negative():
    """Delete the context-clearing: Z3 must find double-release (or
    release-after-settle) pushing `used` below the baseline — and below zero
    outright when B < res.  This is BUG-3."""
    cons, used_final, B, res = _finalizer_model(_K, clears=False)
    s = _solver()
    s.add(*cons)
    s.add(used_final < B)
    m = assert_counterexample_exists(s, "BUG-3: repeated finalizers drift below baseline")
    del m

    # Stronger exhibit: used goes strictly NEGATIVE.
    cons2, used_final2, _, _ = _finalizer_model(_K, clears=False)
    s2 = _solver()
    s2.add(*cons2)
    s2.add(used_final2 < 0)
    m2 = assert_counterexample_exists(s2, "BUG-3: used < 0 (e.g. B=0, double release)")
    del m2


# ===========================================================================
# 4. CASCADE SAFETY: QuotaExhausted ADVANCE NEVER COMMITS THE EXHAUSTED LINE
# ===========================================================================
#
# HONESTY NOTE: this proof rests almost entirely on axiom A6 (transaction is
# all-or-nothing) — the fixed model is close to definitional and its UNSAT is
# cheap.  Its value is (a) pinning the cascade semantics precisely (selected =
# FIRST candidate whose quota condition passes; pool debited at most once),
# and (b) the paired sanity: if the quota line ever moves OUT of the
# TransactWriteItems (a plausible refactor), Z3 shows the exhausted
# candidate's counter committing anyway.

_M = 3


def _cascade_model(m: int, atomic: bool):
    sel = z3.Int("sel")  # -1 == every candidate exhausted (402)
    cons = [z3.Or(sel == -1, z3.And(sel >= 0, sel < m))]
    L = [z3.Int(f"L{i}") for i in range(m)]
    U = [z3.Int(f"U{i}") for i in range(m)]
    ex = [z3.Bool(f"ex{i}") for i in range(m)]
    c = [z3.Int(f"c{i}") for i in range(m)]
    conds = []
    for i in range(m):
        cons += [L[i] >= 0, c[i] >= 1, z3.Implies(ex[i], z3.And(U[i] >= 0, U[i] <= L[i]))]
        h = L[i] - c[i]
        conds.append(z3.Or(z3.And(h >= 0, z3.Not(ex[i])), z3.And(ex[i], U[i] <= h)))
    for i in range(m):
        cons.append((sel == i) == z3.And(conds[i], *[z3.Not(conds[j]) for j in range(i)]))
    cons.append((sel == -1) == z3.And(*[z3.Not(conds[i]) for i in range(m)]))

    base = [z3.If(ex[i], U[i], z3.IntVal(0)) for i in range(m)]
    post = []
    for i in range(m):
        if atomic:
            # A6: only the SELECTED candidate's transaction commits its ADD.
            post.append(base[i] + z3.If(sel == i, c[i], z3.IntVal(0)))
        else:
            # BROKEN: the quota update is a separate non-transactional write —
            # every ATTEMPTED candidate (0..sel, or all when sel == -1)
            # leaves its ADD behind even though its condition failed.
            attempted = z3.Or(sel == -1, sel >= i)
            post.append(base[i] + z3.If(attempted, c[i], z3.IntVal(0)))
    pool_debits = z3.Sum([z3.If(sel == i, z3.IntVal(1), z3.IntVal(0)) for i in range(m)])
    return cons, sel, base, post, L, pool_debits


def test_cascade_advance_never_commits_exhausted_candidate():
    """For any chain of M candidates: every non-selected candidate's row is
    UNCHANGED, the selected one stays within its limit, and the pool is
    debited at most once."""
    cons, sel, base, post, L, pool_debits = _cascade_model(_M, atomic=True)
    s = _solver()
    s.add(*cons)
    violation = z3.Or(
        pool_debits > 1,
        *[z3.Or(post[i] > L[i], z3.And(sel != i, post[i] != base[i])) for i in range(_M)],
    )
    s.add(violation)
    assert_proved(s, "cascade advance leaves exhausted candidates untouched, <= limit")


def test_sanity_non_transactional_quota_write_breaks_cascade():
    """Break A6 (quota ADD as a separate write): Z3 must find an exhausted
    candidate whose counter committed past its limit (SAT)."""
    cons, _, _, post, L, _ = _cascade_model(_M, atomic=False)
    s = _solver()
    s.add(*cons)
    s.add(z3.Or(*[post[i] > L[i] for i in range(_M)]))
    m = assert_counterexample_exists(s, "exhausted candidate committed without atomicity")
    del m


# ===========================================================================
# 5. PERIOD ISOLATION  (settle/release key off the RESERVED period)
# ===========================================================================
#
# Two rows: r = the period the quota was RESERVED against (holds Br + res),
# s = current_period() at settle time, r != s (request crossed the month
# boundary).  Row s may or may not exist.  Guard under proof: A8, the release
# targets row r.  HONESTY NOTE: the fixed transition is deterministic, so the
# UNSAT is trivial — the substantive content is A8 plus the sanity, which
# retargets row s and exhibits BOTH failure modes at once: row r leaks the
# reservation forever, and an existing row s is negative-seeded.

def _period_model(keyed_on_reserved: bool):
    Br, res, Us = z3.Ints("Br res Us")
    exs = z3.Bool("exists_s")
    cons = [Br >= 0, res >= 1, z3.Implies(exs, Us >= 0)]
    used_r0 = Br + res  # reserved-period row carries the live reservation
    if keyed_on_reserved:
        used_r1 = used_r0 - res       # release lands on the reserved row
        used_s1 = Us                  # other period untouched
        exs1 = exs
    else:
        used_r1 = used_r0             # LEAK: reservation never released
        used_s1 = z3.If(exs, Us - res, Us)  # existing row negative-seeded;
        exs1 = exs                    # absent row saved only by the Sec.2 gate
    return cons, used_r1, Br, exs, exs1, used_s1, Us


def test_period_isolation_release_hits_reserved_row_only():
    """Releasing against the RESERVED period fully removes the reservation
    from row r and leaves row s (existence AND value) bit-identical."""
    cons, used_r1, Br, exs, exs1, used_s1, Us = _period_model(keyed_on_reserved=True)
    s = _solver()
    s.add(*cons)
    s.add(z3.Not(z3.And(used_r1 == Br, exs1 == exs, used_s1 == Us)))
    assert_proved(s, "reserved-period release: r back to baseline, s untouched")


def test_sanity_recomputed_period_leaks_and_negative_seeds():
    """Key the release off current_period() instead (the Fable F-1 bug):
    Z3 must find (a) the reserved period leaking, and (b) an existing
    current-period row driven negative."""
    cons, used_r1, Br, exs, _, used_s1, _ = _period_model(keyed_on_reserved=False)
    s = _solver()
    s.add(*cons)
    s.add(used_r1 > Br)  # (a) leak: reservation never released
    m = assert_counterexample_exists(s, "period leak under recomputed period")
    del m

    cons2, _, _, exs2, _, used_s2, _ = _period_model(keyed_on_reserved=False)
    s2 = _solver()
    s2.add(*cons2)
    s2.add(exs2, used_s2 < 0)  # (b) negative seed of the wrong month's row
    m2 = assert_counterexample_exists(s2, "negative seed of current-period row")
    del m2


# ===========================================================================
# 6. INDUCTIVE STEP FOR MIXED TRACES OF ANY LENGTH
# ===========================================================================
#
# Assume the invariant  exists => 0 <= used <= L  (absent reads as 0, A7) and
# each op's guard; prove every op preserves it.  Combined with the bounded
# interleaving proof of Section 1, this covers unbounded N and mixed traces
# (same compositional structure as the pool suite's inductive step).
#
# Per-op assumptions:
#   * settle/release require res <= used — the ledger invariant (each
#     reservation subtracted at most once), established by Section 3 plus the
#     stateful machine (A10).  NOT circular: Section 3 does not assume it.
#   * reserve's guard is the FIXED branch-selected condition of Section 1.
#   * TTL reap deletes the whole item (DynamoDB TTL) — trivially safe.

def _inductive_pieces():
    L, used, res, act, amt = z3.Ints("L used res act amt")
    ex = z3.Bool("ex")

    def inv(e, u):
        return z3.Implies(e, z3.And(u >= 0, u <= L))

    return L, used, res, act, amt, ex, inv


def test_quota_invariant_inductive_step_all_ops():
    L, used, res, act, amt, ex, inv = _inductive_pieces()
    base = z3.If(ex, used, z3.IntVal(0))

    def preserved(name, guard, ex1, used1):
        s = _solver()
        s.add(L >= 0, inv(ex, used), guard)
        s.add(z3.Not(inv(ex1, used1)))
        assert_proved(s, f"{name} preserves (exists => 0 <= used <= L)")

    headroom = L - amt
    reserve_cond = z3.Or(
        z3.And(headroom >= 0, z3.Not(ex)),
        z3.And(ex, used <= headroom),
    )
    # reserve: fixed condition at commit (A2); attribute now exists
    preserved("reserve", z3.And(amt >= 1, reserve_cond), z3.BoolVal(True), base + amt)
    # settle: ADD (act - res); reservation live (A10), actual <= reserved
    preserved(
        "settle",
        z3.And(ex, res >= 1, res <= used, act >= 0, act <= res),
        ex,
        used + act - res,
    )
    # release: ADD (-res); reservation live (A10)
    preserved("release", z3.And(ex, res >= 1, res <= used), ex, used - res)
    # settle/release on an absent row: attribute_exists gate => no-op (Sec. 2)
    preserved("adjust-missing-noop", z3.Not(ex), ex, used)
    # TTL reap: item deleted
    preserved("ttl-reap", ex, z3.BoolVal(False), z3.IntVal(0))


def test_sanity_inductive_reserve_without_headroom_branch_breaks():
    """Inductive-step sanity: use the BUG-1 condition (attribute_not_exists
    kept when headroom < 0) as reserve's guard and Z3 must break the
    invariant in one step (fresh row, amt > L)."""
    L, used, res, act, amt, ex, inv = _inductive_pieces()
    base = z3.If(ex, used, z3.IntVal(0))
    bug1_cond = z3.Or(z3.Not(ex), z3.And(ex, used <= L - amt))
    s = _solver()
    s.add(L >= 0, inv(ex, used))
    s.add(amt >= 1, bug1_cond)
    s.add(z3.Not(inv(z3.BoolVal(True), base + amt)))
    m = assert_counterexample_exists(s, "one bug1-reserve step breaks the invariant")
    del m


def test_sanity_inductive_settle_requires_ledger_side_condition():
    """Drop A10 (res <= used) from settle's guard: Z3 must drive used < 0 in
    one step.  Documents that the side condition is load-bearing and is
    discharged by Section 3 + the stateful layer, not assumed for free."""
    L, used, res, act, amt, ex, inv = _inductive_pieces()
    s = _solver()
    s.add(L >= 0, inv(ex, used))
    s.add(ex, res >= 1, act >= 0, act <= res)  # NOTE: no `res <= used`
    s.add(z3.Not(inv(ex, used + act - res)))
    m = assert_counterexample_exists(s, "settle without the ledger side condition")
    del m
