"""The `/responses` usage block: what it is, and what it must be billed as.

Every fixture below is a VERBATIM capture from the Bedrock OpenAI-compatible
endpoint (`us.openai.gpt-5.6-sol`, through this gateway, 2026-09-24), not a shape
derived from a specification. That matters twice over. The route's own streaming
tests passed for months against a hand-written shape with `event:` lines the
endpoint does not send, which is exactly why nobody saw that no streamed call was
ever metered; and the cache counts turn out to be SUBSETS of `input_tokens`, which
is not what the transport's docstring claimed.
"""
from __future__ import annotations

import pytest

from mvp._responses_wire import ResponsesUsageShapeError, usage_from_responses


# --- the six measured responses --------------------------------------------
# Rows in the order they were produced: a cold ~3.5k prompt, the same prompt twice
# more, the prompt extended by ~3k, that longer prompt again, and that extended
# again. `max_output_tokens=16` throughout.
MEASURED_COLD_WRITE = {
    "input_tokens": 3527,
    "input_tokens_details": {"cache_write_tokens": 3525, "cached_tokens": 0},
    "output_tokens": 16,
    "output_tokens_details": {"reasoning_tokens": 16},
    "total_tokens": 3543,
}
MEASURED_CACHE_HIT = {
    "input_tokens": 3527,
    "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 3525},
    "output_tokens": 16,
    "output_tokens_details": {"reasoning_tokens": 16},
    "total_tokens": 3543,
}
MEASURED_TINY_PROMPT = {
    "input_tokens": 7,
    "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 0},
    "output_tokens": 5,
    "output_tokens_details": {"reasoning_tokens": 0},
    "total_tokens": 12,
}
MEASURED_EXTENDED_WRITE = {
    "input_tokens": 9927,
    "input_tokens_details": {"cache_write_tokens": 9925, "cached_tokens": 0},
    "output_tokens": 16,
    "output_tokens_details": {"reasoning_tokens": 16},
    "total_tokens": 9943,
}


class TestTheMeasuredShapeDecomposesToLegsThatSumToWhatWasCounted:
    """`mvp.pricing.rate_usage` is additive over its four legs, so the legs have to
    partition what the provider counted. If they do not, some tokens are billed
    twice and others not at all."""

    @pytest.mark.parametrize(
        "usage,expected",
        [
            (MEASURED_COLD_WRITE, (2, 0, 3525, 16)),
            (MEASURED_CACHE_HIT, (2, 3525, 0, 16)),
            (MEASURED_TINY_PROMPT, (7, 0, 0, 5)),
            (MEASURED_EXTENDED_WRITE, (2, 0, 9925, 16)),
        ],
        ids=["cold-write", "cache-hit", "tiny-prompt", "extended-write"],
    )
    def test_legs_and_their_sum(self, usage, expected):
        parsed = usage_from_responses(usage)
        legs = (parsed.input, parsed.cache_read or 0, parsed.cache_write or 0, parsed.output)
        assert legs == expected, (
            f"expected (input, cache_read, cache_write, output) == {expected}, got {legs}"
        )
        assert sum(legs) == usage["total_tokens"], (
            "the four legs must sum to the provider's own total; any other sum means "
            "the settle either bills a token twice or bills one at nothing"
        )

    def test_reasoning_is_not_added_on_top_of_output(self):
        """`reasoning_tokens == output_tokens == 16` on three of the four captures.
        Adding it to the output leg would bill the same sixteen tokens twice."""
        parsed = usage_from_responses(MEASURED_CACHE_HIT)
        assert parsed.output == 16


class TestTheSettleArithmeticThisReplaces:
    """The defect, stated as arithmetic. Before this parser the route passed the RAW
    `input_tokens` beside the cache count, and `rate_usage` summed them."""

    def test_the_cached_portion_is_no_longer_billed_at_the_input_rate_as_well(self):
        from mvp.pricing import rate_usage, snapshot_rates

        snapshot = snapshot_rates("gpt-5.6-sol")
        parsed = usage_from_responses(MEASURED_CACHE_HIT)

        fixed = rate_usage(
            snapshot, input_tokens=parsed.input, output_tokens=parsed.output,
            cache_read_tokens=parsed.cache_read, cache_write_tokens=parsed.cache_write,
        )
        # What the route did before: the raw count, unreduced, beside the cache leg.
        previous = rate_usage(
            snapshot, input_tokens=MEASURED_CACHE_HIT["input_tokens"],
            output_tokens=MEASURED_CACHE_HIT["output_tokens"],
            cache_read_tokens=3525, cache_write_tokens=0,
        )
        assert fixed.total_cost_microusd < previous.total_cost_microusd, (
            "billing the reduced base leg must cost less than billing the full "
            "input_tokens beside the same cache leg; if it does not, the "
            "subtraction is not reaching the rating"
        )
        input_rate = snapshot.input_per_mtok_microusd
        over = previous.total_cost_microusd - fixed.total_cost_microusd
        assert over > 0 and input_rate > 0, (over, input_rate)


class TestAnUnreadableBlockRefusesRatherThanReportingZero:
    """`extract_usage`, which the Chat spelling still shares, answers `(0, 0)` for a
    missing block and 0 for a missing leg. On a money path that turns a counter
    nobody read into a measured zero -- a settle that charges nothing and records
    itself as fully observed. Each case below is a FALSE PASS that shape allows."""

    @pytest.mark.parametrize(
        "usage",
        [
            None,
            "not an object",
            {"output_tokens": 3},
            {"input_tokens": 5},
            {"input_tokens": 5, "output_tokens": True},
            {"input_tokens": "5", "output_tokens": 3},
            {"input_tokens": 5.9, "output_tokens": 3},
            {"input_tokens": -1, "output_tokens": 3},
            {"input_tokens": 5, "output_tokens": 3, "total_tokens": 20},
            {"input_tokens": 5, "output_tokens": 3, "widget_tokens": 2},
            {"input_tokens": 5, "output_tokens": 3,
             "input_tokens_details": {"cached_tokens": 0, "mystery_tokens": 4}},
            {"input_tokens": 5, "output_tokens": 3,
             "output_tokens_details": {"reasoning_tokens": 9}},
            {"input_tokens": 5, "output_tokens": 3,
             "input_tokens_details": {"cached_tokens": 4, "cache_write_tokens": 4}},
        ],
        ids=["absent", "not-an-object", "no-input", "no-output", "bool-output",
             "string-input", "float-input", "negative-input", "total-disagrees",
             "unknown-top-key", "unknown-detail-key", "reasoning-exceeds-output",
             "cache-exceeds-input"],
    )
    def test_refused(self, usage):
        with pytest.raises(ResponsesUsageShapeError):
            usage_from_responses(usage)

    def test_a_sparser_block_is_read_rather_than_refused(self):
        """The non-vacuous companion: absence of an OPTIONAL counter is not
        malformation. A model reporting no cache or reasoning breakdown reports
        less, not nonsense, and refusing it would make traffic this gateway meters
        correctly unbillable. The cache legs come back `None` -- "not reported" --
        never a measured zero."""
        parsed = usage_from_responses({"input_tokens": 7, "output_tokens": 13})
        assert (parsed.input, parsed.output) == (7, 13)
        assert parsed.cache_read is None and parsed.cache_write is None


class TestTheSubsetReadingIsNotAppliedWithoutEvidenceForIt:
    """`total_tokens` is the only thing that rules out the reading where the cache
    counts are ADDITIONS to `input_tokens` rather than parts of it, and the
    subtraction is only sound under the subset reading. So it may be absent exactly
    while there is nothing to subtract."""

    def test_a_cache_count_without_a_total_is_refused(self):
        with pytest.raises(ResponsesUsageShapeError):
            usage_from_responses({
                "input_tokens": 3527, "output_tokens": 16,
                "input_tokens_details": {"cached_tokens": 3525, "cache_write_tokens": 0},
            })

    def test_a_zero_cache_count_without_a_total_is_read(self):
        parsed = usage_from_responses({
            "input_tokens": 7, "output_tokens": 5,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
        })
        assert (parsed.input, parsed.output) == (7, 5)
