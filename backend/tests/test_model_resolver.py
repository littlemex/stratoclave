"""Tests for ModelResolver — per-model quota selection + cascading fallback."""
from __future__ import annotations

import pytest

from mvp.routing.model_resolver import (
    ModelSelection,
    RoutingConfig,
    UserRoutingConfig,
    resolve_model,
)


def _tenant_config(**kwargs):
    defaults = {
        "allowlist": ("claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"),
        "chain": ("claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"),
    }
    defaults.update(kwargs)
    return RoutingConfig(**defaults)


class TestBasicResolution:
    def test_returns_requested_model_when_in_chain(self):
        r = resolve_model(
            requested_model="claude-sonnet-4-6",
            tenant_config=_tenant_config(),
        )
        assert r.selected_model == "claude-sonnet-4-6"
        assert not r.fallback_occurred

    def test_returns_first_in_chain_when_requested_not_found(self):
        r = resolve_model(
            requested_model="nonexistent",
            tenant_config=_tenant_config(),
        )
        assert r.selected_model == "claude-opus-4-6"

    def test_no_config_returns_requested(self):
        r = resolve_model(
            requested_model="claude-sonnet-4-6",
            tenant_config=RoutingConfig(),
        )
        assert r.selected_model == "claude-sonnet-4-6"


class TestFallbackChain:
    def test_fallback_disabled_returns_only_requested(self):
        r = resolve_model(
            requested_model="claude-opus-4-6",
            tenant_config=_tenant_config(),
            fallback_allowed=False,
        )
        assert r.selected_model == "claude-opus-4-6"

    def test_fallback_enabled_starts_from_requested(self):
        r = resolve_model(
            requested_model="claude-sonnet-4-6",
            tenant_config=_tenant_config(),
            fallback_allowed=True,
        )
        # Starts from sonnet, includes haiku as fallback
        assert r.selected_model == "claude-sonnet-4-6"

    def test_free_tier_appended_when_fallback_enabled(self):
        cfg = _tenant_config(free_tier_model="claude-haiku-4-5")
        r = resolve_model(
            requested_model="claude-opus-4-6",
            tenant_config=cfg,
            fallback_allowed=True,
        )
        assert r.selected_model == "claude-opus-4-6"


class TestUserPreferences:
    def test_user_chain_takes_priority(self):
        user = UserRoutingConfig(chain=("claude-opus-4-6", "claude-haiku-4-5"))
        r = resolve_model(
            requested_model="claude-opus-4-6",
            tenant_config=_tenant_config(),
            user_config=user,
            fallback_allowed=True,
        )
        assert r.selected_model == "claude-opus-4-6"

    def test_user_preferred_model_starts_chain_there(self):
        user = UserRoutingConfig(preferred_model="claude-haiku-4-5")
        r = resolve_model(
            requested_model="claude-opus-4-6",
            tenant_config=_tenant_config(),
            user_config=user,
        )
        assert r.selected_model == "claude-haiku-4-5"


class TestBreakerDowngrade:
    def test_breaker_filters_high_tier(self):
        r = resolve_model(
            requested_model="claude-opus-4-6",
            tenant_config=_tenant_config(),
            breaker_max_tier=1,
            fallback_allowed=True,
        )
        # Opus is tier 3, sonnet tier 2, haiku tier 1 → only haiku survives
        assert "haiku" in r.selected_model
        assert r.fallback_occurred

    def test_breaker_keeps_all_when_tier_high(self):
        r = resolve_model(
            requested_model="claude-opus-4-6",
            tenant_config=_tenant_config(),
            breaker_max_tier=5,
        )
        assert r.selected_model == "claude-opus-4-6"


class TestVSRHard:
    def test_vsr_hard_pins_model(self):
        r = resolve_model(
            requested_model="claude-sonnet-4-6",
            tenant_config=_tenant_config(),
            vsr_hard_model="claude-opus-4-6",
            fallback_allowed=True,
        )
        assert r.selected_model == "claude-opus-4-6"
        assert not r.fallback_occurred

    def test_vsr_hard_outside_allowlist_raises(self):
        with pytest.raises(ValueError, match="not in tenant allowlist"):
            resolve_model(
                requested_model="claude-sonnet-4-6",
                tenant_config=_tenant_config(),
                vsr_hard_model="nonexistent-model",
            )
