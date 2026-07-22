"""Unit tests for the SR routing seam types (mvp.sr.port) and the source-agnostic
switch-cost rename (SR migration stage 1, S1-2/S1-3).

Stage 1 introduces the seam TYPES only; no wiring yet. These tests pin the
contract both the legacy SAAR adapter and the future vLLM Semantic Router
adapter must satisfy, and prove the switch-cost primitive is unchanged behind
its new source-agnostic name.
"""
from __future__ import annotations

import pytest

from mvp import pricing
from mvp.sr.port import NO_DECISION, RouteDecision, SwitchCostHint


def test_no_decision_is_inert():
    assert NO_DECISION.acts is False
    assert NO_DECISION.hard_model is None and NO_DECISION.prefer_model is None
    assert NO_DECISION.origin == "none"


def test_hard_pin_acts():
    d = RouteDecision(hard_model="claude-opus-4-7", reason="tool-loop-lock",
                      origin="saar")
    assert d.acts is True
    assert d.hard_model == "claude-opus-4-7"


def test_soft_preference_acts():
    d = RouteDecision(prefer_model="claude-haiku-4-5", reason="sticky", origin="saar")
    assert d.acts is True
    assert d.prefer_model == "claude-haiku-4-5"


def test_hard_and_soft_are_mutually_exclusive():
    # the hard/soft partition is enforced at construction (defence in depth).
    with pytest.raises(ValueError):
        RouteDecision(hard_model="a", prefer_model="b")


def test_switch_cost_hint_defaults_to_no_discount():
    h = SwitchCostHint()
    assert h.warm_model is None and h.warm_prefix_tokens == 0


def _seed_rate(repo, key, *, input_mtok, cache_read_mtok, output_mtok):
    repo.set_rates(version=f"sr-test-{key}", rates={key: pricing.Rate(
        input_per_mtok_microusd=input_mtok,
        output_per_mtok_microusd=output_mtok,
        cache_read_per_mtok_microusd=cache_read_mtok,
        cache_write_per_mtok_microusd=0,
    )})


def test_switch_cost_alias_is_the_new_function():
    # the deprecated SAAR-specific name is now a pure alias of the source-agnostic
    # name — same object, so every legacy caller/spec keeps identical behaviour.
    assert pricing.saar_checkout_delta_microusd is pricing.switch_cost_delta_microusd


def test_switch_cost_arithmetic_unchanged(dynamodb_mock):
    from mvp.pricing import PricingConfigRepository
    repo = PricingConfigRepository()
    _seed_rate(repo, "opus", input_mtok=3_000_000, cache_read_mtok=300_000,
               output_mtok=15_000_000)
    # 1000 warm prefix tokens × (3.00 − 0.30)/Mtok = 2700 microusd — same value the
    # pre-rename saar_checkout_delta_microusd test asserts.
    assert pricing.switch_cost_delta_microusd(
        pricing_key="opus", warm_prefix_tokens=1000, repo=repo) == 2700
    assert pricing.switch_cost_delta_microusd(
        pricing_key="opus", warm_prefix_tokens=0, repo=repo) == 0
