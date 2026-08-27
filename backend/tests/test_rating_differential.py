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
The premise the whole ceiling rests on — that the reserve-time estimate dominates
the settled actual, per component — is FALSE in the shipped implementation:
`rate_usage` charges four components while `estimate_cost_microusd` prices three,
so a request that writes prompt cache settles above what was reserved for it.
`test_estimate_omits_the_cache_write_leg` is an `xfail(strict=True)` marker for
exactly that, which means the day the estimator grows a cache-write leg this file
fails with "unexpectedly passing" and forces the marker out. A known defect that
cannot be forgotten is worth more than a comment.
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
                       effort_multiplier: int = 1, warm_prefix_tokens: int = 0) -> int:
    """Independent `estimate_cost_microusd`, written from its docstring.

    "input_estimate + max_output * effort_multiplier, priced per token type: input
    at the input rate, the (multiplied) max output at the output rate", with
    `warm_prefix_tokens` of the input re-priced at the cache-read rate and clamped
    so warm never exceeds the input estimate.
    """
    reserved_output = max(max_output_tokens, 0) * max(effort_multiplier, 1)
    total_input = max(input_tokens_est, 0)
    warm = min(max(warm_prefix_tokens, 0), total_input)
    fresh = total_input - warm
    return (reference_component_cost(fresh, rate.input_per_mtok_microusd)
            + reference_component_cost(warm, rate.cache_read_per_mtok_microusd)
            + reference_component_cost(reserved_output, rate.output_per_mtok_microusd))


@settings(max_examples=80, deadline=None)
@given(
    input_tokens_est=st.integers(min_value=0, max_value=200_000),
    max_output_tokens=st.integers(min_value=0, max_value=100_000),
    effort_multiplier=st.sampled_from([1, 2, 4, 8]),
    warm_prefix_tokens=st.integers(min_value=0, max_value=200_000),
)
def test_estimate_matches_an_independent_recomputation(
    input_tokens_est, max_output_tokens, effort_multiplier, warm_prefix_tokens,
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
        warm_prefix_tokens=warm_prefix_tokens,
    )
    actual = estimate_cost_microusd(
        pricing_key="default",
        input_tokens_est=input_tokens_est,
        max_output_tokens=max_output_tokens,
        effort_multiplier=effort_multiplier,
        warm_prefix_tokens=warm_prefix_tokens,
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

def test_the_clamp_is_on_tokens_and_a_negative_rate_credits():
    """The Z3 files encode `_mtok_cost` as clamped on TOKENS. That is an
    assumption about the code, so it is checked here rather than trusted.

    It also records something the clamp does not cover: a negative rate with
    positive tokens produces a NEGATIVE cost — a credit that would inflate
    headroom. Nothing in the rating path rejects a negative rate; the only defence
    is that the rate document has never held one. That is a configuration
    assumption, and this test is where it is visible instead of implied.
    """
    from mvp.pricing import _mtok_cost

    assert _mtok_cost(-100, 5_000_000) == 0        # clamped on tokens
    assert _mtok_cost(0, 5_000_000) == 0
    assert _mtok_cost(1_000, 0) == 0               # a zero rate costs nothing
    assert _mtok_cost(1_000, -5_000_000) == -5_000, (
        "a negative rate no longer credits — if the rating path started rejecting "
        "negative rates, say so here and drop the configuration assumption from "
        "docs/EVIDENCE.md"
    )


@settings(max_examples=120, deadline=None)
@given(tokens=st.integers(min_value=-10_000, max_value=10_000), rate=rate_st)
def test_no_usage_report_can_mint_a_credit_at_a_nonnegative_rate(tokens, rate):
    """With a non-negative rate, no token count — including a negative one from a
    malformed usage report — produces a negative charge.
    """
    from mvp.pricing import _mtok_cost

    assert _mtok_cost(tokens, rate) >= 0


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN DEFECT, and the marker is the tracking device: rate_usage bills "
           "four components (input, output, cache_read, cache_write) while "
           "estimate_cost_microusd prices three (fresh input, warm input at the "
           "cache-read rate, output). It has no cache-write leg, and in the "
           "shipped rate document cache_write is priced ABOVE input, so any "
           "request that writes prompt cache settles above what was reserved for "
           "it. That breaks premise (P) of the ceiling theorem in "
           "test_rating_formal_z3.py, and the dollar pool has no overrun path of "
           "its own — the token dimension has credit_overrun, the pool just books "
           "the actual. When the estimator grows a cache-write leg this test will "
           "pass, the strict marker will fail the suite, and the marker must then "
           "be deleted along with this reason.",
)
def test_estimate_omits_the_cache_write_leg():
    """The premise fails on a request that writes prompt cache.

    Concrete rather than generated, so the number in the failure is the number a
    reader can reproduce: the shipped `default` rates, 1,000 input tokens, 100
    output tokens, 5,000 cache-write tokens.
    """
    rate = baseline_rates()["default"]
    # The precondition, asserted rather than assumed: if the rate document ever
    # prices cache writes at zero this test would flip to passing for a reason
    # that has nothing to do with the estimator, and the strict xfail would fail
    # the suite misleadingly. Fail loudly here instead.
    assert rate.cache_write_per_mtok_microusd > 0, (
        "cache writes are priced at zero in the rate document; this test can no "
        "longer demonstrate the missing leg"
    )
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
    assert actual <= reserved, (
        f"settled {actual} against a reservation of {reserved} — the cache-write "
        f"leg is unreserved"
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


def test_the_estimator_prices_fewer_components_than_the_charger():
    """The asymmetry itself, stated so the cause is visible without reading both
    functions.

    Kept separate from the xfail above: this one passes today and documents WHY
    that one fails. It fails the day a fifth component is charged without a
    matching estimate leg, which is the same defect arriving again.
    """
    snap = _snapshot(input_rate=1_000_000, output_rate=1_000_000,
                     cache_read_rate=1_000_000, cache_write_rate=1_000_000)
    charged_components = set(
        rate_usage(snap, input_tokens=1, output_tokens=1,
                   cache_read_tokens=1, cache_write_tokens=1).components
    )
    assert charged_components == {"input", "output", "cache_read", "cache_write"}

    # What the estimator can express, read off its signature rather than guessed.
    import inspect
    estimate_params = set(inspect.signature(estimate_cost_microusd).parameters)
    assert "cache_write_tokens" not in estimate_params, (
        "the estimator grew a cache-write parameter. This test and the strict "
        "xfail on test_estimate_omits_the_cache_write_leg will both fail, on "
        "purpose and together: one says the shape changed, the other says the "
        "behaviour did. Delete both, and add the dominance test for the new leg."
    )
    assert {"input_tokens_est", "max_output_tokens", "warm_prefix_tokens"} <= estimate_params
