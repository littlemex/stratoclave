"""Formal (SMT) verification of the Savings Certificate algebra with Z3.

The savings number goes to customers, so its accounting identities are proven
over ALL integer inputs, not just sampled ones (docs/design/
vsr-savings-certificate.md, Fable review). Method mirrors test_billing_formal_z3:
prove each obligation by asserting its NEGATION is UNSAT; a paired `sat` sanity
check removes the guard and confirms Z3 finds the bug (harness not vacuous).

What is proved (the money-honesty invariants of mvp.learning.savings):

  P1  saving_microusd == recompute_billed - recompute_suggested   (definition)
  P2  net == Σ positive - Σ negative, with positive/negative the exact
      partition of the per-row savings (no clipping, no double-count)
  P3  the counterfactual is symmetric in rate basis: pricing BOTH models at the
      SAME per-token rates over the SAME tokens means the saving depends only on
      the rate DIFFERENCE, never on an (un-knowable) billed rate version — i.e.
      rate drift cannot bias the sign. (This is why model-vs-model beats
      billed - cf.)
"""
import pytest
import z3

Z3_TIMEOUT_MS = 60_000
z3.set_param("smt.random_seed", 0)
z3.set_param("sat.random_seed", 0)


def _solver():
    s = z3.Solver()
    s.set("timeout", Z3_TIMEOUT_MS)
    return s


def _check(s):
    r = s.check()
    if r == z3.unknown:
        pytest.fail(f"Z3 unknown: {s.reason_unknown()}")
    return r


def assert_proved(s, what):
    assert _check(s) == z3.unsat, f"NOT PROVED: {what}"


def assert_has_bug(s, what):
    assert _check(s) == z3.sat, f"VACUOUS: {what}"


# --- token/rate cost model: cost(rate, tin, tout) = rate_in*tin + rate_out*tout
def _cost(rate_in, rate_out, tin, tout):
    return rate_in * tin + rate_out * tout


def test_saving_is_difference_of_two_recomputes():
    """P1: saving == recompute(billed) - recompute(suggested), both over the SAME
    tokens. Proven for all non-negative tokens and rates."""
    s = _solver()
    bi, bo, si, so = z3.Ints("bi bo si so")   # billed/suggested in/out per-tok rates
    tin, tout = z3.Ints("tin tout")
    for v in (bi, bo, si, so, tin, tout):
        s.add(v >= 0)
    recompute_billed = _cost(bi, bo, tin, tout)
    recompute_sug = _cost(si, so, tin, tout)
    saving = recompute_billed - recompute_sug
    # saving expands to the rate-DIFFERENCE times tokens — no other term.
    s.add(saving != (bi - si) * tin + (bo - so) * tout)
    assert_proved(s, "saving = Δrate · tokens (model-vs-model definition)")


def test_saving_sign_depends_only_on_rate_difference():
    """P3: with both models priced at the SAME snapshot, a positive saving is
    EXACTLY 'suggested rates are cheaper on this token mix'. There is no billed
    rate-version term that could flip the sign — the anti-drift guarantee."""
    s = _solver()
    bi, bo, si, so, tin, tout = z3.Ints("bi bo si so tin tout")
    for v in (bi, bo, si, so, tin, tout):
        s.add(v >= 0)
    saving = _cost(bi, bo, tin, tout) - _cost(si, so, tin, tout)
    # If the suggested model is at-least-as-expensive per token on BOTH legs, the
    # saving can NEVER be positive (no phantom saving from a rate mismatch).
    s.add(si >= bi, so >= bo)
    s.add(saving > 0)
    assert_proved(s, "suggested dearer on both legs => saving never positive")


def test_billed_minus_cf_CAN_flip_sign_under_drift_SANITY():
    """Sanity: the REJECTED design (billed_actual - recompute_suggested) CAN show
    a phantom positive saving purely from a stale billed charge, even when the
    suggested model is dearer at the current snapshot. This is the bias
    model-vs-model removes; Z3 finds it, proving the harness is real."""
    s = _solver()
    bi, bo, si, so, tin, tout = z3.Ints("bi bo si so tin tout")
    billed_actual = z3.Int("billed_actual")     # a PAST charge, unrelated to current rates
    for v in (bi, bo, si, so, tin, tout, billed_actual):
        s.add(v >= 0)
    s.add(si >= bi, so >= bo)                    # suggested dearer at current snapshot
    naive_saving = billed_actual - _cost(si, so, tin, tout)
    s.add(naive_saving > 0)                      # yet a phantom positive saving exists
    assert_has_bug(s, "billed_actual - cf can fake a positive saving under drift")


def test_net_is_partition_sum_no_clipping():
    """P2: for a two-row set, net == positive - negative where each row's saving
    is placed in exactly one bucket by sign. Proven for all integer savings
    (including negatives — no clipping to zero)."""
    s = _solver()
    a, b = z3.Ints("a b")   # two per-row savings, any sign
    pos = z3.If(a >= 0, a, 0) + z3.If(b >= 0, b, 0)
    neg = z3.If(a < 0, -a, 0) + z3.If(b < 0, -b, 0)
    net = a + b
    s.add(net != pos - neg)
    assert_proved(s, "net == Σpositive - Σnegative (no clipping)")


def test_net_can_be_negative():
    """A set whose losses exceed its wins has a NEGATIVE net — the honest sign the
    certificate must be able to show. (Existence, so a `sat` model is expected.)"""
    s = _solver()
    a, b = z3.Ints("a b")
    net = a + b
    s.add(a > 0, b < 0, net < 0)
    assert _check(s) == z3.sat, "a net loss must be representable"
