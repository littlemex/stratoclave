"""
Differential checks between the money arithmetic and an independent reference.

WHY A SEPARATE FILE FROM THE Z3 ONE
-----------------------------------
`test_rating_formal_z3.py` proves properties of an ENCODING. A green run there
says the design is sound; it says nothing about whether this repository's Python
matches the design. That gap is the honest limit of a formal layer, and the only
way to narrow it is to drive the real functions and an independently written
reference with the same inputs and compare.

So the reference below is written from the SPEC, not from `mvp/pricing.py`:
ceil per component at the recorded rate, summed. If someone changes the
implementation's fold, this file fails. If someone changes both to the same wrong
thing, it does not — that is why the rounding policy is asserted against a
literal constant rather than read back from the code under test.

WHAT THIS FILE ALSO PINS
------------------------
The premise the whole ceiling rests on — that what is reserved dominates what is
settled — used to be FALSE on the rate axis in the shipped implementation:
`rate_usage` charged four legs while `estimate_cost_microusd` priced three, with no
cache-write leg, and cache writes cost more than fresh input. A request that wrote
prompt cache settled above its reservation: 7,500 microUSD reserved, 38,750 settled.
This file carried that as an `xfail(strict=True)` until it was fixed.

It is fixed on the RATE axis and only there. Both sides now read one registry
(`mvp.pricing.BILLABLE_LEGS`), and the estimator prices every input-side token at
the worst rate any input-side leg can bill it at, so no classification the provider
chooses can push the settle above the reservation.

The TOKEN axis is a different claim and this function does not make it. An estimated
token count is not a bound on the token count, so a prompt that tokenises to more
than `input_tokens_est` still settles above its reservation, by design, on the
accounting path. Only `mvp.reservation_bound` — which prices a byte-count ceiling
rather than an estimate — carries the ceiling claim, and
`tests/test_billable_legs_registry.py` is where that dominance is proved against
every partition. `test_the_estimate_path_is_not_a_bound_on_the_token_count` below
pins the boundary so the two claims cannot be confused for one.
"""

import math

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from mvp.pricing import (
    RateSnapshot,
    baseline_rates,
    estimate_cost_microusd,
    rate_usage,
)

TOKENS_PER_MTOK = 1_000_000

# The policy is a literal here, on purpose. Reading it from the module under test
# would let an implementation and its check drift together into the same mistake.
EXPECTED_ROUNDING_POLICY = "ceil"

# Bounds wide enough to cover any request the routes accept.
tokens_st = st.integers(min_value=0, max_value=5_000_000)
rate_st = st.integers(min_value=0, max_value=500_000_000)


def reference_component_cost(tokens: int, rate_microusd_per_mtok: int) -> int:
    """Independent `_mtok_cost`: ceil at a per-MTok rate, zero for no tokens.

    Written from the spec sentence "ceil rounding per component (never
    under-charge by truncation)", using `math.ceil` on a fraction rather than the
    implementation's negated floor-division trick, so an error in that trick is
    visible instead of mirrored.
    """
    if tokens <= 0:
        return 0
    return math.ceil((tokens * rate_microusd_per_mtok) / TOKENS_PER_MTOK)


def reference_total_from_inputs(usage: dict, rates: dict) -> int:
    """The fold, computed from the INPUTS the caller supplied.

    Deliberately not from the returned `components`. Folding the implementation's
    own output back over itself cannot detect a component that was dropped or
    priced against the wrong rate — it only checks the addition. Codex caught that
    in review; this version is driven by the tokens and rates the test chose.
    """
    return sum(reference_component_cost(usage[name], rates[name])
               for name in usage)


def _snapshot(*, input_rate: int, output_rate: int,
              cache_read_rate: int, cache_write_rate: int,
              version: str = "vdiff") -> RateSnapshot:
    return RateSnapshot(
        version=version,
        pricing_key="diff-key",
        input_per_mtok_microusd=input_rate,
        output_per_mtok_microusd=output_rate,
        cache_read_per_mtok_microusd=cache_read_rate,
        cache_write_per_mtok_microusd=cache_write_rate,
    )


# ---------------------------------------------------------------------------
# G3 — the implementation agrees with an independent recomputation
# ---------------------------------------------------------------------------

@settings(max_examples=120, deadline=None)
@given(
    input_tokens=tokens_st, output_tokens=tokens_st,
    cache_read_tokens=tokens_st, cache_write_tokens=tokens_st,
    input_rate=rate_st, output_rate=rate_st,
    cache_read_rate=rate_st, cache_write_rate=rate_st,
)
def test_rating_total_matches_an_independent_recomputation(
    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
    input_rate, output_rate, cache_read_rate, cache_write_rate,
):
    """`rate_usage`'s total equals the reference fold over its own components."""
    snap = _snapshot(input_rate=input_rate, output_rate=output_rate,
                     cache_read_rate=cache_read_rate,
                     cache_write_rate=cache_write_rate)
    rating = rate_usage(
        snap,
        input_tokens=input_tokens, output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )
    usage = {"input": input_tokens, "output": output_tokens,
             "cache_read": cache_read_tokens, "cache_write": cache_write_tokens}
    rates = {"input": input_rate, "output": output_rate,
             "cache_read": cache_read_rate, "cache_write": cache_write_rate}
    assert rating.total_cost_microusd == reference_total_from_inputs(usage, rates)

    # And the components are the ones asked for, at the rates asked for — so a
    # dropped or mis-rated component fails here rather than hiding inside a total
    # that happens to add up.
    assert set(rating.components) == set(usage)
    for name, comp in rating.components.items():
        assert comp["tokens"] == usage[name]
        assert comp["rate_microusd_per_mtok"] == rates[name]


@settings(max_examples=120, deadline=None)
@given(
    input_tokens=tokens_st, output_tokens=tokens_st,
    input_rate=rate_st, output_rate=rate_st,
)
def test_t1_the_event_recomputes_from_itself_alone(
    input_tokens, output_tokens, input_rate, output_rate,
):
    """T1, as a property: the ledger dictionary carries enough to recompute its
    own amount with no table read.

    This is the claim a reader is invited to check by hand on any single event,
    so it is checked here the same way — from `to_ledger_dict()` only.
    """
    snap = _snapshot(input_rate=input_rate, output_rate=output_rate,
                     cache_read_rate=0, cache_write_rate=0)
    ledger = rate_usage(snap, input_tokens=input_tokens,
                        output_tokens=output_tokens).to_ledger_dict()

    assert ledger["rounding"] == EXPECTED_ROUNDING_POLICY
    recomputed = sum(
        reference_component_cost(c["tokens"], c["rate_microusd_per_mtok"])
        for c in ledger["components"].values()
    )
    assert recomputed == ledger["total_cost_microusd"]
    # T1 is a claim about what a THIRD PARTY can do with the event, so here the
    # event's own numbers are the right input — unlike the fold test above, where
    # using them would have made the check circular.


def test_an_unknown_rounding_policy_is_refused_not_guessed():
    """A snapshot carrying a policy the code cannot implement must raise rather
    than charge under an assumption.

    This is the behaviour that makes the `rounding` field meaningful: recording a
    policy is only worth something if an unrecognised one stops the charge.
    """
    snap = _snapshot(input_rate=1_000, output_rate=1_000,
                     cache_read_rate=0, cache_write_rate=0)
    object.__setattr__(snap, "rounding", "bankers")
    with pytest.raises(ValueError, match="rounding"):
        rate_usage(snap, input_tokens=10, output_tokens=10)


@settings(max_examples=80, deadline=None)
@given(tokens=tokens_st, rate=rate_st)
def test_ceil_never_undercharges_the_exact_rational_cost(tokens, rate):
    """The charged amount is never below the exact cost, so integer arithmetic
    cannot be used to nibble past a limit.
    """
    snap = _snapshot(input_rate=rate, output_rate=0,
                     cache_read_rate=0, cache_write_rate=0)
    charged = rate_usage(snap, input_tokens=tokens, output_tokens=0).total_cost_microusd
    assert charged * TOKENS_PER_MTOK >= tokens * rate


# ---------------------------------------------------------------------------
# G1's premise — measured against the real estimator
# ---------------------------------------------------------------------------

def reference_estimate(*, rate, input_tokens_est: int, max_output_tokens: int,
                       effort_multiplier: int = 1) -> int:
    """Independent `estimate_cost_microusd`, written from its docstring.

    "input_estimate + max_output * effort_multiplier, priced per token type", where
    every input-side token is priced at the worst rate an input-side leg can bill it
    at, because the provider — not the gateway — classifies it at settle.

    The three input-side rate fields are named LITERALLY here rather than read from
    `BILLABLE_LEGS`. That is the point of a differential reference: if a leg were
    dropped from the registry, the implementation would quietly price one fewer and
    this reference would still price all three, so the disagreement surfaces. The
    registry's own completeness is checked against the dataclass fields in
    `tests/test_billable_legs_registry.py`.

    The slack terms are the whole microUSD that per-leg rounding at settle can add
    over a rounded group total: each input-side leg can round up, the group total
    rounds up once, and ceil is not subadditive. Capped by the token count too — n
    tokens cannot make more than n legs round up — so a side with no tokens adds
    nothing. Two output-side legs would add a unit; there is one, so it adds none.
    """
    reserved_output = max(max_output_tokens, 0) * max(effort_multiplier, 1)
    total_input = max(input_tokens_est, 0)
    worst_input_side = max(
        rate.input_per_mtok_microusd,
        rate.cache_read_per_mtok_microusd,
        rate.cache_write_per_mtok_microusd,
    )
    input_side_legs = 3
    output_side_legs = 1
    input_slack = max(min(input_side_legs, total_input) - 1, 0)
    output_slack = max(min(output_side_legs, reserved_output) - 1, 0)
    return (reference_component_cost(total_input, worst_input_side)
            + reference_component_cost(reserved_output, rate.output_per_mtok_microusd)
            + input_slack + output_slack)


@settings(max_examples=80, deadline=None)
@given(
    input_tokens_est=st.integers(min_value=0, max_value=200_000),
    max_output_tokens=st.integers(min_value=0, max_value=100_000),
    effort_multiplier=st.sampled_from([1, 2, 4, 8]),
)
def test_estimate_matches_an_independent_recomputation(
    input_tokens_est, max_output_tokens, effort_multiplier,
):
    """The estimator agrees with a reference written from its own specification.

    An earlier draft of this file tried to check estimate dominance and smuggled
    the conclusion into an `assume` that equated the reserved amount to a
    `rate_usage` output — so the assertion followed from output-token monotonicity
    and a broken estimator would merely have discarded examples. Adversarial review
    caught it. Dominance is now checked separately, below, against the components
    the estimator actually prices; this test checks the estimator itself.
    """
    rate = baseline_rates()["default"]
    expected = reference_estimate(
        rate=rate,
        input_tokens_est=input_tokens_est,
        max_output_tokens=max_output_tokens,
        effort_multiplier=effort_multiplier,
    )
    actual = estimate_cost_microusd(
        pricing_key="default",
        input_tokens_est=input_tokens_est,
        max_output_tokens=max_output_tokens,
        effort_multiplier=effort_multiplier,
    )
    assert actual == expected


@settings(max_examples=80, deadline=None)
@given(
    input_tokens=st.integers(min_value=0, max_value=200_000),
    output_tokens=st.integers(min_value=0, max_value=50_000),
    extra_output_allowance=st.integers(min_value=0, max_value=50_000),
)
def test_dominance_holds_on_the_components_the_estimator_prices(
    input_tokens, output_tokens, extra_output_allowance,
):
    """(P) holds when only input and output are billed and neither was
    under-estimated — the restricted shape in which the ceiling theorem's premise
    is true.

    Both sides are built from the same rate document, so a rate edit moves them
    together instead of turning this into a false failure. Nothing here is assumed
    equal to anything: the reserved side comes from the estimator and the actual
    side from the charger.
    """
    rate = baseline_rates()["default"]
    snap = _snapshot(input_rate=rate.input_per_mtok_microusd,
                     output_rate=rate.output_per_mtok_microusd,
                     cache_read_rate=rate.cache_read_per_mtok_microusd,
                     cache_write_rate=rate.cache_write_per_mtok_microusd)
    max_output = output_tokens + extra_output_allowance

    reserved = estimate_cost_microusd(
        pricing_key="default",
        input_tokens_est=input_tokens,
        max_output_tokens=max_output,
    )
    actual = rate_usage(snap, input_tokens=input_tokens,
                        output_tokens=output_tokens).total_cost_microusd
    assert actual <= reserved


# ---------------------------------------------------------------------------
# Boundary behaviour of the real `_mtok_cost`, which the Z3 encoding assumes
# ---------------------------------------------------------------------------

def test_the_clamp_is_on_tokens_and_a_negative_rate_is_refused():
    """The Z3 files encode `_mtok_cost` as clamped on TOKENS. That is an
    assumption about the code, so it is checked here rather than trusted.

    The negative-rate case used to be recorded here as a configuration assumption:
    a negative rate with positive tokens returned a NEGATIVE cost — a credit that
    inflates headroom — and the only defence was that no rate document had ever
    held one. A discipline is not a mechanism, so the rating path refuses it now,
    at both the write boundary (`PricingConfigRepository.set_rates`) and here on
    every charging path, whatever wrote the document.
    """
    import pytest as _pytest

    from mvp.pricing import _mtok_cost

    assert _mtok_cost(-100, 5_000_000) == 0        # clamped on tokens
    assert _mtok_cost(0, 5_000_000) == 0
    assert _mtok_cost(1_000, 0) == 0               # a zero rate costs nothing
    with _pytest.raises(ValueError):
        _mtok_cost(1_000, -5_000_000)
    # The refusal does not depend on there being tokens to charge: a document
    # holding a negative leg is rejected even where the cost would round to zero.
    with _pytest.raises(ValueError):
        _mtok_cost(0, -5_000_000)


@settings(max_examples=120, deadline=None)
@given(tokens=st.integers(min_value=-10_000, max_value=10_000), rate=rate_st)
def test_no_usage_report_can_mint_a_credit_at_a_nonnegative_rate(tokens, rate):
    """With a non-negative rate, no token count — including a negative one from a
    malformed usage report — produces a negative charge.
    """
    from mvp.pricing import _mtok_cost

    assert _mtok_cost(tokens, rate) >= 0


def test_the_cache_write_leg_no_longer_settles_above_the_estimate():
    """The case that used to break premise (P), now holding.

    Concrete rather than generated, so the number a reader can reproduce is the
    number in the failure: the shipped `default` rates, 1,000 input-side tokens,
    100 output tokens, and the provider classifying every one of those input-side
    tokens as the most expensive thing it could be — a cache write.
    """
    rate = baseline_rates()["default"]
    # The precondition, asserted rather than assumed: at a zero cache-write rate
    # this test would pass for a reason that has nothing to do with the estimator.
    assert rate.cache_write_per_mtok_microusd > 0, (
        "cache writes are priced at zero in the rate document; this test can no "
        "longer exercise the leg it exists for"
    )
    snap = _snapshot(input_rate=rate.input_per_mtok_microusd,
                     output_rate=rate.output_per_mtok_microusd,
                     cache_read_rate=rate.cache_read_per_mtok_microusd,
                     cache_write_rate=rate.cache_write_per_mtok_microusd)
    reserved = estimate_cost_microusd(
        pricing_key="default", input_tokens_est=1_000, max_output_tokens=100,
    )
    actual = rate_usage(
        snap, input_tokens=0, output_tokens=100, cache_write_tokens=1_000,
    ).total_cost_microusd
    assert actual <= reserved, (
        f"settled {actual} against a reservation of {reserved} — an input-side leg "
        f"is priced below what it settles at"
    )


@pytest.mark.parametrize("partition", [
    (1_000, 0, 0),      # all fresh input
    (0, 1_000, 0),      # all cache reads
    (0, 0, 1_000),      # all cache writes, the leg that used to be missing
    (400, 300, 300),    # split three ways, where per-leg rounding compounds
    (333, 333, 334),
])
def test_no_classification_of_the_estimated_tokens_settles_above_the_estimate(partition):
    """The estimate has to dominate every partition, not the one it guessed.

    The gateway sends tokens; the PROVIDER decides at settle how many of them were
    fresh input, cache reads or cache writes, and the gateway learns that only from
    the usage report. So pricing the estimated count at the input rate reserved
    against one classification out of many.
    """
    fresh, reads, writes = partition
    rate = baseline_rates()["default"]
    snap = _snapshot(input_rate=rate.input_per_mtok_microusd,
                     output_rate=rate.output_per_mtok_microusd,
                     cache_read_rate=rate.cache_read_per_mtok_microusd,
                     cache_write_rate=rate.cache_write_per_mtok_microusd)
    reserved = estimate_cost_microusd(
        pricing_key="default", input_tokens_est=fresh + reads + writes,
        max_output_tokens=100,
    )
    actual = rate_usage(
        snap, input_tokens=fresh, cache_read_tokens=reads,
        cache_write_tokens=writes, output_tokens=100,
    ).total_cost_microusd
    assert actual <= reserved, f"settled {actual} > reserved {reserved} at {partition}"


def test_the_estimate_path_is_not_a_bound_on_the_token_count():
    """The boundary of the claim above, pinned so it cannot be overstated.

    Fixing the rate axis did not turn the accounting estimate into a ceiling. The
    token count is still a guess, and a prompt that tokenises to more than the guess
    settles above its reservation. That is the difference between `accounting` and
    the bound modes in `dollar_pool_bound_state`, and it is why
    `docs/design/hard-ceiling.md` states the ceiling for the bound path only.
    """
    rate = baseline_rates()["default"]
    snap = _snapshot(input_rate=rate.input_per_mtok_microusd,
                     output_rate=rate.output_per_mtok_microusd,
                     cache_read_rate=rate.cache_read_per_mtok_microusd,
                     cache_write_rate=rate.cache_write_per_mtok_microusd)
    reserved = estimate_cost_microusd(
        pricing_key="default", input_tokens_est=1_000, max_output_tokens=100,
    )
    actual = rate_usage(
        snap, input_tokens=1_000, output_tokens=100, cache_write_tokens=5_000,
    ).total_cost_microusd
    assert actual > reserved, (
        "the accounting estimate now dominates a token count six times its guess. "
        "If the estimator grew a token bound, say so in docs/design/hard-ceiling.md "
        "and move this case to the dominance tests — do not just delete it."
    )


def test_the_shipped_rate_document_prices_cache_writes_above_input():
    """The property that makes the missing leg expensive rather than academic.

    If a future rate document priced cache writes at or below input, the overrun
    would shrink and the defect could be mistaken for fixed. Pinning it here means
    the reason the defect matters cannot drift out from under the xfail above.
    """
    rate = baseline_rates()["default"]
    assert rate.cache_write_per_mtok_microusd > rate.input_per_mtok_microusd, (
        "cache writes are no longer priced above input in the shipped document — "
        "re-measure the overrun in docs/EVIDENCE.md before trusting its numbers"
    )


def test_the_estimator_and_the_charger_enumerate_the_same_legs():
    """The asymmetry that caused the defect, closed at the source rather than
    patched at one call site.

    The estimator does not take a cache-write parameter and should not: it prices a
    token count it cannot classify. What it must not do is enumerate the legs a
    SECOND time — that is how three came to be priced while four were charged. It
    reads `BILLABLE_LEGS`, so a fifth billable leg appears on both sides at once.
    """
    from mvp.pricing import BILLABLE_LEGS, INPUT_SIDE, legs_in_group

    snap = _snapshot(input_rate=1_000_000, output_rate=1_000_000,
                     cache_read_rate=1_000_000, cache_write_rate=1_000_000)
    charged_components = set(
        rate_usage(snap, input_tokens=1, output_tokens=1,
                   cache_read_tokens=1, cache_write_tokens=1).components
    )
    assert charged_components == {leg.name for leg in BILLABLE_LEGS}

    # A leg added to the registry with a rate above the others must move the
    # estimate up. Priced at 10x the shipped input rate, on the same token count.
    baseline = estimate_cost_microusd(
        pricing_key="default", input_tokens_est=1_000, max_output_tokens=0,
    )
    rate = baseline_rates()["default"]
    dearest = max(
        getattr(rate, leg.rate_field) for leg in legs_in_group(INPUT_SIDE)
    )
    assert baseline >= reference_component_cost(1_000, dearest), (
        "the estimate is below the worst input-side leg on the shipped rates — the "
        "estimator is enumerating legs of its own again"
    )
