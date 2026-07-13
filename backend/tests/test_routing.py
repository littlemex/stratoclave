"""Tests for the routing/resilience layer."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from mvp.routing.breaker import compute_breaker
from mvp.routing.chains import resolve_chain, _tier_for
from mvp.routing.classify import classify
from mvp.routing.types import (
    BreakerDecision,
    BreakerStage,
    Disposition,
    RouteRequest,
    Target,
)


# ---------------------------------------------------------------------------
# Breaker
# ---------------------------------------------------------------------------

class TestBreaker:
    def test_normal_when_healthy(self):
        d = compute_breaker(800_000, 1_000_000)
        assert d.stage == BreakerStage.NORMAL
        assert d.remaining_ratio == 0.8

    def test_downgrade_at_20_percent(self):
        d = compute_breaker(200_000, 1_000_000)
        assert d.stage == BreakerStage.DOWNGRADE
        assert d.max_cost_tier == 1

    def test_reject_at_3_percent(self):
        d = compute_breaker(30_000, 1_000_000)
        assert d.stage == BreakerStage.REJECT

    def test_no_limit_is_normal(self):
        d = compute_breaker(0, 0)
        assert d.stage == BreakerStage.NORMAL


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

class TestClassify:
    def _client_error(self, code):
        return ClientError(
            {"Error": {"Code": code, "Message": "test"}},
            "ConverseStream",
        )

    def test_throttling_is_failover(self):
        t = Target(model_id="m", region="us-east-1")
        assert classify(self._client_error("ThrottlingException"), t) == Disposition.FAILOVER

    def test_service_unavailable_is_retry(self):
        t = Target(model_id="m", region="us-east-1")
        assert classify(self._client_error("ServiceUnavailableException"), t) == Disposition.RETRY_SAME

    def test_validation_is_fatal(self):
        t = Target(model_id="m", region="us-east-1")
        assert classify(self._client_error("ValidationException"), t) == Disposition.FATAL

    def test_timeout_is_failover(self):
        t = Target(model_id="m", region="us-east-1")
        assert classify(TimeoutError(), t) == Disposition.FAILOVER


# ---------------------------------------------------------------------------
# Chain resolution
# ---------------------------------------------------------------------------

class TestChainResolution:
    def test_pin_returns_single_target(self):
        t = Target(model_id="m", region="r")
        chain = resolve_chain("anything", pin=t)
        assert chain.targets == (t,)

    def test_breaker_downgrade_filters_high_tier(self):
        breaker = BreakerDecision(stage=BreakerStage.DOWNGRADE, remaining_ratio=0.15, max_cost_tier=1)
        # Use sonnet (tier 2) which has haiku (tier 1) as potential fallback in catalog
        chain = resolve_chain("us.anthropic.claude-sonnet-4-6", breaker=breaker)
        # If tier-1 alternatives exist, all targets should be tier <= 1
        # If no tier-1 exists for this alias, original targets are kept
        has_tier_1 = any(t.cost_tier <= 1 for t in chain.targets)
        if has_tier_1:
            assert all(t.cost_tier <= 1 for t in chain.targets)

    def test_breaker_downgrade_keeps_targets_when_no_cheaper(self):
        """When only high-tier targets exist, downgrade keeps them (no empty chain)."""
        breaker = BreakerDecision(stage=BreakerStage.DOWNGRADE, remaining_ratio=0.15, max_cost_tier=1)
        chain = resolve_chain("us.anthropic.claude-opus-4-7", breaker=breaker)
        assert len(chain.targets) >= 1

    def test_exclude_removes_targets(self):
        chain_full = resolve_chain("us.anthropic.claude-sonnet-4-6")
        if len(chain_full.targets) > 1:
            excluded = (chain_full.targets[0],)
            chain_filtered = resolve_chain("us.anthropic.claude-sonnet-4-6", exclude=excluded)
            assert chain_full.targets[0] not in chain_filtered.targets


# ---------------------------------------------------------------------------
# InfraRouter execution
# ---------------------------------------------------------------------------

class TestInfraRouterExecution:
    def test_success_on_first_attempt(self):
        from mvp.routing.infrarouter import route_stream

        fake_stream = iter([
            {"contentBlockDelta": {"delta": {"text": "hi"}}},
            {"messageStop": {"stopReason": "end_turn"}},
        ])

        with patch("mvp.routing.infrarouter.bedrock_client") as mock_pool:
            mock_client = MagicMock()
            mock_client.converse_stream.return_value = {"stream": fake_stream}
            mock_pool.return_value = mock_client

            req = RouteRequest(
                alias="us.anthropic.claude-sonnet-4-6",
                payload={"messages": [], "inferenceConfig": {"maxTokens": 50}},
                tenant_id="test",
                request_id="r1",
            )

            async def run():
                result = await route_stream(req)
                events = []
                async for ev in result.events:
                    events.append(ev)
                return result, events

            result, events = asyncio.run(run())
            assert result.target.model_id == "us.anthropic.claude-sonnet-4-6"
            assert len(events) == 2
            assert result.attempt_facts[0].outcome == "success"

    def test_failover_on_throttle(self):
        from mvp.routing.infrarouter import route_stream, _cooldowns

        _cooldowns.clear()
        call_count = {"n": 0}

        def mock_converse(**kwargs):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                raise ClientError(
                    {"Error": {"Code": "ThrottlingException", "Message": "rate"}},
                    "ConverseStream",
                )
            return {"stream": iter([
                {"contentBlockDelta": {"delta": {"text": "ok"}}},
                {"messageStop": {"stopReason": "end_turn"}},
            ])}

        with patch("mvp.routing.infrarouter.bedrock_client") as mock_pool:
            mock_client = MagicMock()
            mock_client.converse_stream.side_effect = mock_converse
            mock_pool.return_value = mock_client

            req = RouteRequest(
                alias="us.anthropic.claude-sonnet-4-6",
                payload={"messages": [], "inferenceConfig": {"maxTokens": 50}},
                tenant_id="test",
                request_id="r2",
            )

            async def run():
                result = await route_stream(req)
                events = []
                async for ev in result.events:
                    events.append(ev)
                return result, events

            result, events = asyncio.run(run())
            assert len(result.attempt_facts) >= 2
            assert any(a.outcome == "failover" for a in result.attempt_facts)
            assert result.attempt_facts[-1].outcome == "success"

    def test_fatal_error_raises(self):
        from mvp.routing.infrarouter import route_stream

        with patch("mvp.routing.infrarouter.bedrock_client") as mock_pool:
            mock_client = MagicMock()
            mock_client.converse_stream.side_effect = ClientError(
                {"Error": {"Code": "ValidationException", "Message": "bad"}},
                "ConverseStream",
            )
            mock_pool.return_value = mock_client

            req = RouteRequest(
                alias="us.anthropic.claude-sonnet-4-6",
                payload={"messages": [], "inferenceConfig": {"maxTokens": 50}},
                tenant_id="test",
                request_id="r3",
            )

            with pytest.raises(ClientError):
                asyncio.run(route_stream(req))
