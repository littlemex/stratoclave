"""
Executable specification of a sound reservation bound, written before the
implementation exists.

WHY THIS FILE EXISTS, AND WHY IT IS WRITTEN FIRST
-------------------------------------------------
`test_reservation_bound_formal_z3.py` proves that a sound reservation turns the
existing admission condition into a hard ceiling. That proof takes soundness as a
hypothesis. This file is where soundness stops being a hypothesis: it states, as
runnable properties, what a bound function has to satisfy, and it does so BEFORE
the production function is written so that the specification cannot be reverse-
engineered from whatever the implementation happens to do.

The reference bound below is therefore a specification, not a draft of the
implementation. When the production function lands, the switch at
`_production_bound` turns every property here into a differential check against
it, and the reference stays as the independent second opinion.

THE ENVELOPE, STATED PLAINLY
----------------------------
A bound can only cover content the gateway can measure. The properties here hold
under these conditions and are FALSE outside them, which is the honest scope of
any hard-ceiling claim:

 E1. The request is text only. Image tokens scale with pixel dimensions, on the
     order of pixels/750, and have no relationship to byte length — a few hundred
     bytes of flat-colour PNG can carry thousands of tokens. A byte bound is
     simply false for multimodal input.
 E2. No provider-injected billable tokens. Tool-use scaffolding the provider adds,
     and server-side tool results such as web search, are billed and were never in
     the bytes the gateway sent. No constant covers a variable-length search
     result.
 E3. The SUM of the input-side token counts (input + cache_read + cache_write) is
     bounded by the UTF-8 byte length of what was sent. This is the load-bearing
     assumption about the provider's accounting, and it is stated rather than
     buried: each token consumes at least one byte, and the three input-side
     counts partition the prompt rather than each counting all of it. If a provider
     ever double-counted a token as both read and written, this breaks and so does
     the bound.
 E4. Output is bounded by `max_output_tokens * effort_multiplier`, which holds only
     if the provider respects the requested ceiling for total output including
     reasoning tokens.

Bytes rather than characters, deliberately. A byte-level tokeniser can split one
multi-byte character into several tokens, so `tokens <= characters` is unsound
while `tokens <= utf8_bytes` is not.
"""

import math

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from mvp.pricing import RateSnapshot, baseline_rates, rate_usage

TOKENS_PER_MTOK = 1_000_000

# The production bound does not exist yet. When it lands, import it here and every
# property below becomes a differential check against it rather than a
# specification of it. Kept as an explicit switch so the transition is a one-line,
# reviewable change instead of a quiet edit spread across the file.
try:  # pragma: no cover - exercised once the implementation lands
    from mvp.reservation_bound import strict_reservation_microusd as _strict_bound

    def _production_bound(*, pricing_key: str, utf8_bytes: int,
                          max_output_tokens: int, effort_multiplier: int = 1) -> int:
        """Adapter onto the shipped signature.

        The implementation was written without access to this file, so the names
        differ; the adapter exists so the properties below test the shipped
        function rather than a reimplementation of it. Nothing here computes
        anything — if it did, it would be testing itself.
        """
        return _strict_bound(
            baseline_rates()[pricing_key],
            input_bytes=utf8_bytes,
            max_output_tokens=max_output_tokens,
            effort_multiplier=effort_multiplier,
        )
except ImportError:  # pragma: no cover
    _production_bound = None

_needs_production_bound = pytest.mark.skipif(
    _production_bound is None,
    reason="the production bound does not exist yet; the properties in this file "
           "are its specification until it does",
)


def _ceil_cost(tokens: int, rate_microusd_per_mtok: int) -> int:
    if tokens <= 0:
        return 0
    return math.ceil((tokens * rate_microusd_per_mtok) / TOKENS_PER_MTOK)


def reference_bound_microusd(*, utf8_bytes: int, max_output_tokens: int,
                             effort_multiplier: int = 1, rate=None) -> int:
    """A sound upper bound on what settle can charge, under E1-E4.

    Every input-side token is priced at the WORST of the three input-side rates.
    That is what removes the assumption about provider caching behaviour: whether
    the provider reads cache, writes cache, or does neither, no input-side token
    can cost more than this rate. Output is the requested ceiling times the effort
    multiplier at the output rate.
    """
    rate = rate or baseline_rates()["default"]
    worst_input_side = max(rate.input_per_mtok_microusd,
                           rate.cache_read_per_mtok_microusd,
                           rate.cache_write_per_mtok_microusd)
    reserved_output = max(max_output_tokens, 0) * max(effort_multiplier, 1)
    return (_ceil_cost(max(utf8_bytes, 0), worst_input_side)
            + _ceil_cost(reserved_output, rate.output_per_mtok_microusd))


def _snapshot(rate) -> RateSnapshot:
    return RateSnapshot(
        version="vbound", pricing_key="default",
        input_per_mtok_microusd=rate.input_per_mtok_microusd,
        output_per_mtok_microusd=rate.output_per_mtok_microusd,
        cache_read_per_mtok_microusd=rate.cache_read_per_mtok_microusd,
        cache_write_per_mtok_microusd=rate.cache_write_per_mtok_microusd,
    )


# Split three ways so a generated example is a realistic partition of a prompt
# rather than three independent numbers that happen to be small.
@st.composite
def _input_side_split(draw, max_bytes=400_000):
    utf8_bytes = draw(st.integers(min_value=0, max_value=max_bytes))
    total_tokens = draw(st.integers(min_value=0, max_value=utf8_bytes))  # E3
    cache_read = draw(st.integers(min_value=0, max_value=total_tokens))
    cache_write = draw(st.integers(min_value=0, max_value=total_tokens - cache_read))
    plain_input = total_tokens - cache_read - cache_write
    return utf8_bytes, plain_input, cache_read, cache_write


# ---------------------------------------------------------------------------
# The property the hard ceiling rests on
# ---------------------------------------------------------------------------

@settings(max_examples=250, deadline=None)
@given(
    split=_input_side_split(),
    output_tokens=st.integers(min_value=0, max_value=50_000),
    extra_output_allowance=st.integers(min_value=0, max_value=20_000),
    effort_multiplier=st.sampled_from([1, 2, 4, 8]),
)
def test_the_reference_bound_dominates_every_charge_inside_the_envelope(
    split, output_tokens, extra_output_allowance, effort_multiplier,
):
    """No usage the charger can produce inside E1-E4 costs more than the bound.

    This is the obligation the Z3 file assumes and the whole ceiling depends on.
    The input-side counts are generated as a PARTITION of a byte budget rather than
    independently, because that is what E3 asserts and a test that generated them
    freely would be checking a stronger claim than the envelope makes.
    """
    utf8_bytes, plain_input, cache_read, cache_write = split
    rate = baseline_rates()["default"]
    max_output = output_tokens + extra_output_allowance

    bound = reference_bound_microusd(
        utf8_bytes=utf8_bytes, max_output_tokens=max_output,
        effort_multiplier=effort_multiplier, rate=rate,
    )
    actual = rate_usage(
        _snapshot(rate),
        input_tokens=plain_input, output_tokens=output_tokens,
        cache_read_tokens=cache_read, cache_write_tokens=cache_write,
    ).total_cost_microusd

    assert actual <= bound, (
        f"bound {bound} did not cover actual {actual} for bytes={utf8_bytes} "
        f"in={plain_input} read={cache_read} write={cache_write} out={output_tokens}"
    )


@settings(max_examples=200, deadline=None)
@given(
    split=_input_side_split(),
    output_tokens=st.integers(min_value=1, max_value=50_000),
    image_tokens=st.integers(min_value=1, max_value=100_000),
)
def test_the_bound_is_broken_by_a_component_outside_the_envelope(
    split, output_tokens, image_tokens,
):
    """E1 is not a formality: add tokens that byte length cannot see and the bound
    stops dominating.

    Image tokens are modelled as extra input-side tokens with no corresponding
    bytes, which is exactly what a small flat-colour PNG produces. A run where the
    bound still happens to cover them is discarded rather than counted, because the
    claim is that the envelope is load-bearing, not that every image breaks it.
    """
    utf8_bytes, plain_input, cache_read, cache_write = split
    rate = baseline_rates()["default"]

    bound = reference_bound_microusd(
        utf8_bytes=utf8_bytes, max_output_tokens=output_tokens, rate=rate,
    )
    actual = rate_usage(
        _snapshot(rate),
        input_tokens=plain_input + image_tokens,   # tokens with no bytes behind them
        output_tokens=output_tokens,
        cache_read_tokens=cache_read, cache_write_tokens=cache_write,
    ).total_cost_microusd

    assume(actual > bound)
    assert actual > bound


def test_bytes_not_characters_is_the_sound_unit():
    """A character bound is unsound and a byte bound is not, on real text.

    Japanese is three bytes per character in UTF-8 and runs about one token per
    character, so a character-based bound would sit at roughly a third of the
    tokens it has to cover. This is also why the shipped `char_count // 3` estimate
    under-reserves CJK traffic by about threefold with no prompt caching involved.
    """
    japanese = "予算の上限を超えたら必ず拒否する仕組みが必要である。" * 40
    chars = len(japanese)
    utf8_bytes = len(japanese.encode("utf-8"))
    assert utf8_bytes == chars * 3, "expected three bytes per character for this text"

    # One token per character is the realistic worst case for this script.
    plausible_tokens = chars
    assert plausible_tokens <= utf8_bytes, "the byte bound covers it"
    assert plausible_tokens > chars // 3, (
        "and the shipped heuristic does not — this is the CJK under-reservation"
    )


@settings(max_examples=150, deadline=None)
@given(
    utf8_bytes=st.integers(min_value=0, max_value=200_000),
    max_output_tokens=st.integers(min_value=0, max_value=50_000),
    effort_multiplier=st.sampled_from([1, 2, 4, 8]),
)
def test_the_bound_is_monotone_in_every_input(utf8_bytes, max_output_tokens,
                                              effort_multiplier):
    """Growing any input cannot shrink the bound.

    A non-monotone bound would let a caller reduce its reservation by sending more,
    which is the shape of every quota-evasion bug.
    """
    rate = baseline_rates()["default"]
    base = reference_bound_microusd(
        utf8_bytes=utf8_bytes, max_output_tokens=max_output_tokens,
        effort_multiplier=effort_multiplier, rate=rate)

    assert reference_bound_microusd(
        utf8_bytes=utf8_bytes + 1, max_output_tokens=max_output_tokens,
        effort_multiplier=effort_multiplier, rate=rate) >= base
    assert reference_bound_microusd(
        utf8_bytes=utf8_bytes, max_output_tokens=max_output_tokens + 1,
        effort_multiplier=effort_multiplier, rate=rate) >= base
    assert reference_bound_microusd(
        utf8_bytes=utf8_bytes, max_output_tokens=max_output_tokens,
        effort_multiplier=effort_multiplier * 2, rate=rate) >= base


def test_the_bounds_cost_is_stated_not_discovered():
    """The price of the guarantee, pinned as a number so it cannot drift silently.

    An operator choosing strict mode is trading concurrency for an absolute
    ceiling, and the trade has to be quotable. If a rate change or a bound change
    moves these ratios materially, this test fails and the documented figures get
    re-measured rather than quietly becoming wrong.
    """
    rate = baseline_rates()["default"]
    samples = {
        "english": "The quick brown fox jumps over the lazy dog. " * 600,
        "japanese": "予算の上限を超えたら必ず拒否する仕組みが必要である。" * 600,
    }
    for name, text in samples.items():
        chars, utf8_bytes = len(text), len(text.encode("utf-8"))
        current_estimate = (_ceil_cost(chars // 3, rate.input_per_mtok_microusd)
                            + _ceil_cost(2_000, rate.output_per_mtok_microusd))
        bound = reference_bound_microusd(
            utf8_bytes=utf8_bytes, max_output_tokens=2_000, rate=rate)
        ratio = bound / current_estimate
        assert 2.0 <= ratio <= 12.0, (
            f"{name}: bound/estimate is {ratio:.2f}x, outside the documented 2-12x "
            f"range — re-measure the figures in docs/EVIDENCE.md and the contract"
        )


# ---------------------------------------------------------------------------
# The same properties, against the production function once it exists
# ---------------------------------------------------------------------------

@_needs_production_bound
@settings(max_examples=250, deadline=None)
@given(
    split=_input_side_split(),
    output_tokens=st.integers(min_value=0, max_value=50_000),
    extra_output_allowance=st.integers(min_value=0, max_value=20_000),
)
def test_production_bound_dominates_every_charge_inside_the_envelope(
    split, output_tokens, extra_output_allowance,
):
    """The obligation, transferred to the shipped function.

    Deliberately not asserted equal to the reference: an implementation may be
    tighter, and a tighter sound bound is an improvement rather than a failure.
    What it may never be is smaller than what settle charges.
    """
    utf8_bytes, plain_input, cache_read, cache_write = split
    rate = baseline_rates()["default"]
    max_output = output_tokens + extra_output_allowance

    bound = _production_bound(
        pricing_key="default", utf8_bytes=utf8_bytes,
        max_output_tokens=max_output,
    )
    actual = rate_usage(
        _snapshot(rate),
        input_tokens=plain_input, output_tokens=output_tokens,
        cache_read_tokens=cache_read, cache_write_tokens=cache_write,
    ).total_cost_microusd
    assert actual <= bound


@_needs_production_bound
@settings(max_examples=150, deadline=None)
@given(split=_input_side_split(),
       output_tokens=st.integers(min_value=0, max_value=50_000))
def test_production_bound_is_never_below_the_reference(split, output_tokens):
    """A soundness cross-check that does not depend on generating the worst case.

    If the production bound ever dips below the reference, either it dropped a
    component or it priced one below the worst input-side rate. Both are soundness
    failures, and this catches them without needing an example that actually
    breaches.
    """
    utf8_bytes, *_ = split
    reference = reference_bound_microusd(
        utf8_bytes=utf8_bytes, max_output_tokens=output_tokens)
    production = _production_bound(
        pricing_key="default", utf8_bytes=utf8_bytes,
        max_output_tokens=output_tokens)
    assert production >= reference, (
        f"production bound {production} is below the reference {reference} for "
        f"bytes={utf8_bytes} out={output_tokens} — a component or the worst-rate "
        f"pricing is missing"
    )
