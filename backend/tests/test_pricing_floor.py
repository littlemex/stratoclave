"""The bundled floor holds MEASURED list prices, and this is where they are pinned.

Everywhere else in the suite the expected charge is derived from
`pricing.baseline_rates()`, so that a price change does not fail thirty unrelated
tests. That leaves exactly one thing unguarded — whether the floor itself is right —
and this file is it. A price move should fail here, once, with the provenance of the
new number in the diff.

Every figure below was read from the provider's own APIs on 2026-08-31:

- `bedrock:ListFoundationModelAgreementOffers` for the Marketplace-metered families
  (all Anthropic generations, OpenAI GPT-5.x). This is the only source that carries
  the current Claude prices; the Price List API's `AmazonBedrock` offer stops at
  Claude 3 Sonnet.
- The Price List API (`AmazonBedrock`, and `AmazonBedrockFoundationModels` for the
  legacy Anthropic rows) for the families AWS bills directly.

They are the IN-REGION (geo / "Regional CRIS") standard-tier rates, because every
registry entry addresses its model through a `us.` inference profile and that is
what such a request is billed at. The `global` rate is ~10% lower and is not the
floor: the floor's job is to be safe when a feed is unavailable.
"""
from __future__ import annotations

import pytest

from mvp.models import registry_entries
from mvp.pricing import baseline_rates

# pricing key -> (input, output, cache_read, cache_write) in micro-USD per MTok.
MEASURED = {
    "opus": (5_500_000, 27_500_000, 550_000, 6_875_000),
    "opus-legacy": (15_000_000, 75_000_000, 1_500_000, 18_750_000),
    "fable": (11_000_000, 55_000_000, 1_100_000, 13_750_000),
    "sonnet": (3_300_000, 16_500_000, 330_000, 4_125_000),
    "sonnet-5": (2_200_000, 11_000_000, 220_000, 2_750_000),
    "sonnet-3": (3_000_000, 15_000_000, 300_000, 3_750_000),
    "haiku": (1_100_000, 5_500_000, 110_000, 1_375_000),
    "haiku-3-5": (800_000, 4_000_000, 80_000, 1_000_000),
    "haiku-3": (250_000, 1_250_000, 250_000, 312_500),
    "gpt-5.6-sol": (5_500_000, 33_000_000, 550_000, 6_875_000),
    "gpt-5.6-terra": (2_200_000, 13_200_000, 275_000, 3_437_500),
    "grok": (2_200_000, 6_600_000, 550_000, 2_750_000),
    "gemma": (140_000, 400_000, 140_000, 175_000),
    "nemotron": (150_000, 650_000, 150_000, 187_500),
    "qwen": (140_000, 1_200_000, 140_000, 175_000),
}

# Keys whose cache legs are an UPPER BOUND rather than a published price, because
# the provider publishes none. Listed here so the next reader can tell an assumption
# from a measurement without opening the JSON.
CACHE_LEGS_ASSUMED = {"haiku-3", "gemma", "nemotron", "qwen", "grok"}

# `default` is what a pricing key that exists nowhere is charged, which in practice means
# a typo. The document says an unpriced model over-charges rather than under-charges, so
# `default` has to dominate every provider row leg by leg — a fallback cheaper than a real
# tier under-charges exactly the models that tier covers. There is no exception list: an
# exception here is the rule not holding.


@pytest.mark.parametrize("key", sorted(MEASURED))
def test_floor_matches_the_measured_list_price(key):
    rate = baseline_rates()[key]
    assert (rate.input_per_mtok_microusd, rate.output_per_mtok_microusd,
            rate.cache_read_per_mtok_microusd, rate.cache_write_per_mtok_microusd) \
        == MEASURED[key], (
        f"{key}: the floor no longer matches the measured list price. If AWS moved "
        f"the price, update MEASURED here and defaults/pricing.json together, and "
        f"say in the row's `notes` when it was measured."
    )


def test_every_registry_key_has_a_floor_row():
    """A key that exists only in a live feed is not enough: the floor is the layer
    guaranteed to load, so such a model would be charged at `default` the moment the
    feed is unavailable."""
    floor = baseline_rates()
    missing = sorted({e.pricing_key for e in registry_entries()} - set(floor))
    assert not missing, f"registry pricing keys with no floor row: {missing}"


def test_no_leg_is_free_except_self_hosted_cache():
    """A zero leg is a discount, not a price. The one legitimate zero is vLLM's cache
    pair: vLLM reports no Bedrock-style cache split, so those tokens are already
    counted as input."""
    for key, rate in baseline_rates().items():
        assert rate.input_per_mtok_microusd > 0, key
        assert rate.output_per_mtok_microusd > 0, key
        if key == "vllm":
            assert rate.cache_read_per_mtok_microusd == 0
            assert rate.cache_write_per_mtok_microusd == 0
            continue
        assert rate.cache_read_per_mtok_microusd > 0, key
        assert rate.cache_write_per_mtok_microusd > 0, key


def test_default_dominates_every_bundled_provider_rate_leg():
    """A pricing key that exists nowhere is charged at `default`, and the document promises
    that such a model over-charges. That promise is only true if `default` is at least as
    dear as every provider row, on every leg — otherwise a typo in the registry
    under-charges by the difference, silently, for as long as it goes unnoticed."""
    from mvp.rates import RATE_FIELDS

    floor = baseline_rates()
    default = floor["default"]
    for key, rate in floor.items():
        if key in {"default", "vllm"}:      # vllm is operator cost recovery, not a list price
            continue
        for leg in RATE_FIELDS:
            assert getattr(rate, leg) <= getattr(default, leg), (
                f"{key}.{leg} out-prices `default`, so a pricing key that exists nowhere "
                f"would UNDER-charge. Raise `default` to at least the dearest row."
            )


def test_assumed_cache_legs_are_declared_in_the_document():
    """A number nobody published has to say so where it lives, not only in a test."""
    from mvp.price_sources import pricing_path
    import json

    doc = json.load(open(pricing_path(), encoding="utf-8"))
    for key in CACHE_LEGS_ASSUMED:
        notes = (doc["rates"][key].get("notes") or "").lower()
        assert "upper bound" in notes, (
            f"{key}'s cache legs are an assumption; say so in its `notes`"
        )


def test_a_pricing_key_holds_one_price_point():
    """Two models on one key are charged at the dearest of them, so a key spanning
    two price points over-charges the cheaper model on every request.

    This is not hypothetical: `opus` covered Claude Opus 4.1 ($15/$75) and Opus 5
    ($5.50/$27.50) until the live feed measured them apart, which would have charged
    every Opus 5 request at nearly three times its rate. The registry now splits
    them, and this test is what keeps a future model from being filed under a key it
    does not belong to.
    """
    by_key: dict[str, set[str]] = {}
    for entry in registry_entries():
        by_key.setdefault(entry.pricing_key, set()).add(entry.bedrock_model_id)
    # The check is on the DECLARED grouping: models sharing a key must be documented
    # as sharing a price. `notes` on the key's row (or the entry) is where that is
    # said, and MEASURED above is the number they share.
    for key, models in sorted(by_key.items()):
        assert key in MEASURED or key in {"vllm", "default"}, (
            f"pricing key {key!r} is used by {sorted(models)} but has no measured "
            f"price pinned here"
        )
