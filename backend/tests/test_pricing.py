"""Tests for dollar-denominated pricing (mvp.pricing).

Covers the integer micro-USD math, the per-token-type settlement pricing, the
reasoning-effort multiplier on the reservation estimate, and the hot-reload
cache that overlays PricingConfig rows on the built-in defaults.
"""
from __future__ import annotations

import pytest

from mvp import pricing


@pytest.fixture(autouse=True)
def _reset_pricing_cache():
    """Every test starts from built-in defaults, not another test's overrides."""
    pricing.reset_cache()
    yield
    pricing.reset_cache()


def test_estimate_uses_input_and_output_rates(dynamodb_mock):
    # opus default: input 5_000_000 /MTok, output 25_000_000 /MTok
    # 1 MTok input + 1 MTok output = 5_000_000 + 25_000_000 micro-USD
    cost = pricing.estimate_cost_microusd(
        pricing_key="opus",
        input_tokens_est=1_000_000,
        max_output_tokens=1_000_000,
    )
    assert cost == 30_000_000  # $30.00


def test_estimate_applies_effort_multiplier_to_output_only(dynamodb_mock):
    # 1 MTok output at 4x effort = 4 MTok priced at output rate; input 0.
    cost = pricing.estimate_cost_microusd(
        pricing_key="sonnet",  # output 15_000_000 /MTok
        input_tokens_est=0,
        max_output_tokens=1_000_000,
        effort_multiplier=4,
    )
    assert cost == 60_000_000  # 4 * 15_000_000


def test_estimate_rounds_up_sub_mtok(dynamodb_mock):
    # 1 token of Haiku input (1_000_000 /MTok) must round UP to 1 micro-USD,
    # never truncate to 0 — otherwise sub-MTok requests would be free.
    cost = pricing.estimate_cost_microusd(
        pricing_key="haiku",
        input_tokens_est=1,
        max_output_tokens=0,
    )
    assert cost == 1


def test_actual_cost_prices_each_token_type(dynamodb_mock):
    # haiku: in 1_000_000, out 5_000_000, cache_read 100_000, cache_write 1_250_000
    cost = pricing.actual_cost_microusd(
        pricing_key="haiku",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )
    assert cost == 1_000_000 + 5_000_000 + 100_000 + 1_250_000


def test_unknown_pricing_key_falls_back_to_default(dynamodb_mock):
    # An unknown key uses the "default" tier (Opus-priced), so it never
    # under-charges a budget.
    unknown = pricing.actual_cost_microusd(
        pricing_key="does-not-exist",
        input_tokens=1_000_000,
        output_tokens=0,
    )
    default = pricing.actual_cost_microusd(
        pricing_key="default",
        input_tokens=1_000_000,
        output_tokens=0,
    )
    assert unknown == default == 5_000_000


def test_pricing_config_override_is_hot_reloaded(dynamodb_mock):
    """A PricingConfig version overlays the defaults after the cache refreshes."""
    from dynamo.pricing_config import PricingConfigRepository

    repo = PricingConfigRepository()
    # Halve the opus input rate under a new version and activate it.
    override = pricing.Rate(
        input_per_mtok_microusd=2_500_000,
        output_per_mtok_microusd=25_000_000,
        cache_read_per_mtok_microusd=500_000,
        cache_write_per_mtok_microusd=6_250_000,
    )
    repo.set_rates(version="2026-07", rates={"opus": override})

    # Force a reload (bypass the 60s TTL) and confirm the override applies.
    pricing.reset_cache()
    cost = pricing.estimate_cost_microusd(
        pricing_key="opus",
        input_tokens_est=1_000_000,
        max_output_tokens=0,
    )
    assert cost == 2_500_000  # halved input rate took effect


def test_missing_pricing_table_keeps_defaults(dynamodb_mock):
    """If the pricing table read fails/returns nothing, defaults stand and no
    exception escapes (a request must never 500 because pricing is unset).
    """
    pricing.reset_cache()
    cost = pricing.estimate_cost_microusd(
        pricing_key="opus",
        input_tokens_est=1_000_000,
        max_output_tokens=0,
    )
    assert cost == 5_000_000
