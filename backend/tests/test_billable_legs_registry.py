"""One definition of what is billed, and a bound that is actually a bound.

Two defects of the same kind live behind these tests, and both were in the code that
existed to prevent them.

**The legs were enumerated twice.** `rate_usage` charged four token counts (input,
output, cache_read, cache_write) while `estimate_cost_microusd` priced three. There
is no cache-write leg in the estimate, and cache writes are priced above input, so a
request that wrote prompt cache settled above what was reserved for it — measured on
the shipped rates: reserved 7,500 microUSD, settled 38,750. That is premise (P) of
the ceiling theorem, false in the shipped code. `worst_input_side_rate_microusd`,
whose whole job was to close that gap, hard-coded the same three fields while its
docstring claimed a fifth leg could not slip past it.

**The rounding did not compose.** `rate_usage` rounds each leg up; the reservation
bound rounded the input-side total up once. Ceiling is not subadditive, so the "sound
bound" was not an upper bound on the settle: three input-side legs at 1 microUSD/MTok
with one token each settle at 3 while the total rounds to 1.

So: the legs are defined once, in `BILLABLE_LEGS`, and both sides read it; the bound
carries the whole-microUSD slack that per-leg rounding can add, derived from the
registry rather than written as a number.
"""
from __future__ import annotations

import dataclasses
import itertools

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mvp.pricing import (
    BILLABLE_LEGS,
    INPUT_SIDE,
    OUTPUT_SIDE,
    RateSnapshot,
    legs_in_group,
    rate_usage,
    rounding_slack_microusd,
    worst_rate_in_group,
)
from mvp.reservation_bound import strict_reservation_microusd

#: A rate column that charges money. `cost_*` columns are provider-cost record-only
#: fields and are deliberately not billable legs.
def _billable_rate_fields() -> set[str]:
    return {
        f.name for f in dataclasses.fields(RateSnapshot)
        if f.name.endswith("_per_mtok_microusd") and not f.name.startswith("cost_")
    }


# ---------------------------------------------------------------------------
# the registry is the only enumeration
# ---------------------------------------------------------------------------


def test_every_billable_rate_column_has_a_leg():
    """A rate that charges money with no leg is a leg the reservation cannot see —
    which is precisely how cache_write came to be charged but not reserved."""
    assert {leg.rate_field for leg in BILLABLE_LEGS} == _billable_rate_fields()


def test_every_leg_belongs_to_a_group_that_bounds_it():
    """A leg with no token pool has no bound at reserve time, so a reservation
    computed from the groups would silently omit it."""
    assert all(leg.group in {INPUT_SIDE, OUTPUT_SIDE} for leg in BILLABLE_LEGS)
    assert legs_in_group(INPUT_SIDE) and legs_in_group(OUTPUT_SIDE)


def test_leg_names_are_the_names_the_usage_block_uses():
    """The rater indexes observed usage by these names; a rename here without a
    rename there would charge zero for a leg that was used."""
    snap = _snapshot(1, 1, 1, 1)
    record = rate_usage(
        snap, input_tokens=1, output_tokens=1,
        cache_read_tokens=1, cache_write_tokens=1,
    )
    assert set(record.components) == {leg.name for leg in BILLABLE_LEGS}


def test_the_worst_input_side_rate_is_the_worst_of_every_input_leg():
    """Not "of the three the author remembered"."""
    snap = _snapshot(input_rate=10, output_rate=999, cache_read_rate=20, cache_write_rate=30)
    assert worst_rate_in_group(snap, INPUT_SIDE) == 30
    snap2 = _snapshot(input_rate=40, output_rate=999, cache_read_rate=20, cache_write_rate=30)
    assert worst_rate_in_group(snap2, INPUT_SIDE) == 40


def test_the_slack_is_one_less_than_the_legs_that_can_be_nonzero():
    """Capped by the leg count and by the token count, whichever is smaller: n tokens
    cannot make more than n legs round up."""
    input_legs = len(legs_in_group(INPUT_SIDE))
    assert rounding_slack_microusd(INPUT_SIDE, 1_000_000) == input_legs - 1
    assert rounding_slack_microusd(OUTPUT_SIDE, 1_000_000) == len(legs_in_group(OUTPUT_SIDE)) - 1
    assert rounding_slack_microusd(INPUT_SIDE, 0) == 0, "a side with no tokens needs no slack"
    assert rounding_slack_microusd(INPUT_SIDE, 1) == 0, "one token lands on one leg"
    assert rounding_slack_microusd(INPUT_SIDE, 2) == min(input_legs, 2) - 1


# ---------------------------------------------------------------------------
# the bound dominates the settle, for every way the provider can classify tokens
# ---------------------------------------------------------------------------


def _snapshot(input_rate=5_000_000, output_rate=25_000_000,
              cache_read_rate=500_000, cache_write_rate=6_250_000) -> RateSnapshot:
    return RateSnapshot(
        version="v1", pricing_key="default",
        input_per_mtok_microusd=input_rate,
        output_per_mtok_microusd=output_rate,
        cache_read_per_mtok_microusd=cache_read_rate,
        cache_write_per_mtok_microusd=cache_write_rate,
    )


class _Rate:
    """The live-table shape the bound reads, with the same fields as a snapshot."""

    def __init__(self, snap: RateSnapshot) -> None:
        for leg in BILLABLE_LEGS:
            setattr(self, leg.rate_field, getattr(snap, leg.rate_field))


@pytest.mark.parametrize("rates", [
    (1, 1, 1, 1),                       # the minimal counter-example to a single ceil
    (500_000, 1, 500_000, 500_000),     # every rate a half microUSD per token
    (5_000_000, 25_000_000, 500_000, 6_250_000),   # the shipped `default` rates
    (999_999, 3, 999_999, 1_000_001),   # nothing a multiple of 1e6
])
def test_no_partition_of_the_input_tokens_can_settle_above_the_bound(rates):
    """The property the bound is FOR. The provider decides how many of the tokens it
    was sent count as fresh input, cache reads and cache writes; the gateway learns
    that only at settle. So the bound has to dominate every partition, not the one
    the estimator guessed."""
    input_rate, output_rate, cache_read_rate, cache_write_rate = rates
    snap = _snapshot(input_rate, output_rate, cache_read_rate, cache_write_rate)
    for input_bytes in range(0, 7):
        for max_output in (0, 1, 5):
            bound = strict_reservation_microusd(
                rate=_Rate(snap), input_bytes=input_bytes,
                max_output_tokens=max_output, effort_multiplier=1,
            )
            for split in itertools.product(range(input_bytes + 1), repeat=3):
                if sum(split) > input_bytes:
                    continue
                settled = rate_usage(
                    snap,
                    input_tokens=split[0],
                    cache_read_tokens=split[1],
                    cache_write_tokens=split[2],
                    output_tokens=max_output,
                ).total_cost_microusd
                assert settled <= bound, (
                    f"settle {settled} > bound {bound} for split {split} at {rates}"
                )


@given(
    input_bytes=st.integers(min_value=0, max_value=4_000),
    max_output=st.integers(min_value=0, max_value=4_000),
    input_rate=st.integers(min_value=0, max_value=50_000_000),
    output_rate=st.integers(min_value=0, max_value=50_000_000),
    cache_read_rate=st.integers(min_value=0, max_value=50_000_000),
    cache_write_rate=st.integers(min_value=0, max_value=50_000_000),
    fresh=st.integers(min_value=0, max_value=4_000),
    reads=st.integers(min_value=0, max_value=4_000),
    writes=st.integers(min_value=0, max_value=4_000),
)
@settings(max_examples=400, deadline=None)
def test_the_bound_dominates_for_any_rates_and_any_partition(
    input_bytes, max_output, input_rate, output_rate,
    cache_read_rate, cache_write_rate, fresh, reads, writes,
):
    """Generated: any rate table an admin could write, any partition inside the token
    bound. A rate that is not a multiple of 1,000,000 is the common case, and it is
    what made the single rounding unsound."""
    total = fresh + reads + writes
    if total > input_bytes:
        return  # outside the bound's premise: the tokens must fit the byte count
    snap = _snapshot(input_rate, output_rate, cache_read_rate, cache_write_rate)
    bound = strict_reservation_microusd(
        rate=_Rate(snap), input_bytes=input_bytes,
        max_output_tokens=max_output, effort_multiplier=1,
    )
    settled = rate_usage(
        snap, input_tokens=fresh, cache_read_tokens=reads,
        cache_write_tokens=writes, output_tokens=max_output,
    ).total_cost_microusd
    assert settled <= bound


def test_the_cache_write_case_that_broke_the_premise_now_holds():
    """The concrete failure the strict `xfail` in test_rating_differential.py pinned,
    re-expressed against the BOUND rather than the heuristic estimate: 1,000 input-side
    tokens on the shipped rates, all of them classified as cache writes."""
    from mvp.pricing import baseline_rates

    rate = baseline_rates()["default"]
    assert rate.cache_write_per_mtok_microusd > rate.input_per_mtok_microusd, (
        "the premise of the test: cache writes cost more than fresh input"
    )
    snap = _snapshot(
        rate.input_per_mtok_microusd, rate.output_per_mtok_microusd,
        rate.cache_read_per_mtok_microusd, rate.cache_write_per_mtok_microusd,
    )
    bound = strict_reservation_microusd(
        rate=rate, input_bytes=1_000, max_output_tokens=100, effort_multiplier=1,
    )
    settled = rate_usage(
        snap, input_tokens=0, cache_write_tokens=1_000, output_tokens=100,
    ).total_cost_microusd
    assert settled <= bound
