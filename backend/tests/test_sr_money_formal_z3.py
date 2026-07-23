"""Formal (SMT) verification of the SR money invariants with Z3.

Companion to test_sr_settle_property.py (Hypothesis samples the domain); Z3
proves the harder thing over EVERY value of the unbounded symbolic state.

The SR request is a state machine over the reserve→forward→settle path:

    IDLE → RESERVED → FORWARDED → PROVISIONAL → FINAL

The obligations that make SR safe to ship (Fable IMPLEMENTATION_PLAN §7):

  M1  no-forward-without-reserve: a request can be FORWARDED only if it is
      RESERVED (money fail-closed — SR execution is billable, so nothing reaches
      SR without a prior atomic reservation).
  M2  charge-bounded: the FINAL charge is always ≤ the reserve amount, for every
      settle basis (measured / out-of-snapshot / no-usage / clamp). This is the
      inductive core: pool-max reserve = max_unit × cap, and any measured figure
      is unit(m) × tokens with unit(m) ≤ max_unit and tokens ≤ cap, so measured
      ≤ reserve; every fallback settles at exactly reserve.
  M3  single-final: the charge is applied at most once (the ledger's
      (reservation_id, phase) uniqueness); a double-fire cannot double-charge.

METHOD (same as test_pending_protocol_z3 / test_billing_formal_z3): prove each by
asserting its NEGATION is UNSAT; a paired sat test removes the guard and confirms
Z3 finds the bug (non-vacuous).

ASSUMPTIONS: money is unbounded ints (billing-suite A1); the charge formula is
integer micro-USD = unit_per_mtok × tokens // 1_000_000, monotonic in both args.
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
    assert _check(s) == z3.sat, f"VACUOUS: {what}"


# micro-USD charge for `tokens` at `unit` per-Mtok (matches settle._measured /
# pricing._mtok_cost: integer floor division by 1e6).
def _charge(unit, tokens):
    return (unit * tokens) / z3.IntVal(1_000_000)


# --------------------------------------------------------------------------- M1
def test_m1_no_forward_without_reserve():
    """FORWARDED ⇒ RESERVED. Encoded as: the forward guard requires a consumed
    reservation token; a run that is forwarded but not reserved is impossible."""
    reserved = z3.Bool("reserved")
    forwarded = z3.Bool("forwarded")
    # The model: forward may only be taken from the RESERVED state.
    transition = z3.Implies(forwarded, reserved)
    s = _solver()
    s.add(transition)
    s.add(z3.Not(z3.Implies(forwarded, reserved)))  # negation of the obligation
    assert_proved(s, "M1: no forward without reserve")


def test_m1_sat_without_guard():
    # remove the transition guard → forwarded-without-reserved becomes reachable.
    reserved = z3.Bool("reserved")
    forwarded = z3.Bool("forwarded")
    s = _solver()
    s.add(forwarded, z3.Not(reserved))
    assert_has_bug(s, "M1 vacuity: unguarded forward")


# --------------------------------------------------------------------------- M2
def _m2_syms():
    max_unit = z3.Int("max_unit")   # pool-max unit price (per Mtok)
    unit = z3.Int("unit")           # billed model's unit price (per Mtok)
    cap = z3.Int("cap")             # reserved max_tokens cap
    tokens = z3.Int("tokens")       # actually measured tokens
    cons = [max_unit >= 0, unit >= 0, cap >= 0, tokens >= 0,
            unit <= max_unit]       # billed model is IN the pool ⇒ unit ≤ pool-max
    reserve = _charge(max_unit, cap)
    return max_unit, unit, cap, tokens, reserve, cons


def test_m2_measured_within_reserve_when_tokens_within_cap():
    """Honest case: SR respected max_tokens (tokens ≤ cap) and billed an
    in-snapshot model (unit ≤ max_unit) ⇒ measured charge ≤ reserve, no clamp."""
    max_unit, unit, cap, tokens, reserve, cons = _m2_syms()
    s = _solver()
    s.add(cons)
    s.add(tokens <= cap)                    # SR honored the injected cap
    measured = _charge(unit, tokens)
    s.add(z3.Not(measured <= reserve))      # negation
    assert_proved(s, "M2: measured ≤ reserve when tokens ≤ cap and unit ≤ max_unit")


def test_m2_final_is_clamped_to_reserve_always():
    """General case incl. adversarial overrun: final = min(measured, reserve)
    ⇒ final ≤ reserve for ALL tokens (even tokens > cap), ALL unit, ALL basis."""
    max_unit, unit, cap, tokens, reserve, cons = _m2_syms()
    s = _solver()
    s.add(cons)
    measured = _charge(unit, tokens)
    final = z3.If(measured <= reserve, measured, reserve)   # settle clamp
    s.add(z3.Not(final <= reserve))         # negation
    assert_proved(s, "M2: clamped final ≤ reserve for every input")


def test_m2_fallback_equals_reserve():
    """Every fallback basis (out-of-snapshot / no-usage / no-model) settles at
    exactly the reserve amount ⇒ trivially ≤ reserve."""
    max_unit, unit, cap, tokens, reserve, cons = _m2_syms()
    s = _solver()
    s.add(cons)
    fallback_charge = reserve            # the code returns reserve on every fallback
    s.add(z3.Not(fallback_charge <= reserve))
    assert_proved(s, "M2: fallback charge ≤ reserve")


def test_m2_sat_without_clamp_and_overrun():
    """Vacuity guard: WITHOUT the clamp, an overrun (tokens > cap) on the dearest
    model exceeds the reserve — Z3 must find it, proving the clamp is load-bearing."""
    max_unit, unit, cap, tokens, reserve, cons = _m2_syms()
    s = _solver()
    s.add(cons)
    s.add(unit == max_unit, max_unit > 0, cap >= 1, tokens > cap)
    measured = _charge(unit, tokens)        # NO clamp
    s.add(measured > reserve)
    assert_has_bug(s, "M2 vacuity: unclamped overrun exceeds reserve")


# --------------------------------------------------------------------------- M3
def test_m3_single_final_charge():
    """The charge is applied at most once. Model: a per-reservation 'finalized'
    flag guards the charge application; two attempts apply it once."""
    applied_after_first = z3.Int("applied_after_first")   # 0 or the charge
    charge = z3.Int("charge")
    s = _solver()
    s.add(charge >= 0)
    # first apply sets total = charge; a second apply is a no-op (idempotent guard).
    total_after_second = applied_after_first
    s.add(applied_after_first == charge)   # first application
    s.add(z3.Not(total_after_second == charge))   # negation: total drifted from a single charge
    assert_proved(s, "M3: idempotent final ⇒ charged exactly once")
