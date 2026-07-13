"""Tests for fault injection + first-event commit + reader thread cleanup.

Validates the InfraRouter failover path deterministically using the
fault injection hook (the same mechanism used for live testing).
"""
from __future__ import annotations

import asyncio
import os
import threading
import time

import pytest

from mvp.routing import fault
from mvp.routing.infrarouter import route_stream, _cooldowns
from mvp.routing.types import RouteRequest


@pytest.fixture(autouse=True)
def _enable_faults_and_clean(monkeypatch):
    monkeypatch.setenv("SC_FAULT_INJECTION", "1")
    _cooldowns.clear()
    fault._attempt_counters.clear()
    yield
    _cooldowns.clear()
    fault._attempt_counters.clear()


def _req(fault_spec=None, rid="r1"):
    return RouteRequest(
        alias="us.anthropic.claude-sonnet-4-6",
        payload={"messages": [], "inferenceConfig": {"maxTokens": 20}},
        tenant_id="test",
        request_id=rid,
        fault_spec=fault_spec,
    )


def _mock_stream():
    return iter([
        {"contentBlockDelta": {"delta": {"text": "hi"}}},
        {"messageStop": {"stopReason": "end_turn"}},
    ])


class TestFaultInjectionDisabled:
    def test_fault_noop_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("SC_FAULT_INJECTION", raising=False)
        # 429-pre should be a no-op when injection is disabled
        fault.maybe_raise_pre_stream("429-pre", "r1", 1)  # must not raise


class TestFailover:
    def test_429_attempt_1_only_fails_over(self, monkeypatch):
        from unittest.mock import MagicMock, patch

        call_regions = []

        def mock_client(region):
            call_regions.append(region)
            c = MagicMock()
            c.converse_stream.return_value = {"stream": _mock_stream()}
            return c

        with patch("mvp.routing.infrarouter.bedrock_client", side_effect=mock_client):
            async def run():
                routed = await route_stream(_req(fault_spec="429-attempt-1-only"))
                events = [e async for e in routed.events]
                return routed, events

            routed, events = asyncio.run(run())
            # Attempt 1 raises 429 (fault), attempt 2 succeeds
            assert any(a.outcome == "failover" for a in routed.attempt_facts)
            assert routed.attempt_facts[-1].outcome == "success"
            assert len(events) == 2

    def test_empty_stream_1_fails_over(self):
        from unittest.mock import MagicMock, patch

        with patch("mvp.routing.infrarouter.bedrock_client") as mock_pool:
            c = MagicMock()
            c.converse_stream.return_value = {"stream": _mock_stream()}
            mock_pool.return_value = c

            async def run():
                routed = await route_stream(_req(fault_spec="empty-stream-1"))
                events = [e async for e in routed.events]
                return routed, events

            routed, events = asyncio.run(run())
            # Attempt 1 empty → failover, attempt 2 real stream
            assert any(a.outcome == "failover" for a in routed.attempt_facts)
            assert len(events) == 2


class TestFirstEventCommit:
    def test_mid_stream_failure_no_retry(self):
        """500-mid-stream: after first event committed, error propagates,
        NO failover to another target."""
        from unittest.mock import MagicMock, patch

        with patch("mvp.routing.infrarouter.bedrock_client") as mock_pool:
            c = MagicMock()
            c.converse_stream.return_value = {"stream": _mock_stream()}
            mock_pool.return_value = c

            async def run():
                routed = await route_stream(_req(fault_spec="500-mid-stream"))
                events = []
                with pytest.raises(RuntimeError, match="mid-stream"):
                    async for e in routed.events:
                        events.append(e)
                return routed, events

            routed, events = asyncio.run(run())
            # Committed on first event, only ONE success attempt (no retry)
            assert routed.attempt_facts[-1].outcome == "success"
            assert sum(1 for a in routed.attempt_facts if a.outcome == "success") == 1
            # First event was delivered before the mid-stream failure
            assert len(events) == 1


class TestReaderThreadCleanup:
    def test_reader_thread_stops_on_consumer_abandon(self):
        """When the consumer abandons the stream, the reader thread exits."""
        from unittest.mock import MagicMock, patch

        baseline = sum(1 for t in threading.enumerate() if t.name == "sc-reader")

        def slow_stream():
            for i in range(1000):
                time.sleep(0.01)
                yield {"contentBlockDelta": {"delta": {"text": str(i)}}}

        with patch("mvp.routing.infrarouter.bedrock_client") as mock_pool:
            c = MagicMock()
            c.converse_stream.return_value = {"stream": slow_stream()}
            mock_pool.return_value = c

            async def run():
                routed = await route_stream(_req(rid="cleanup"))
                agen = routed.events.__aiter__()
                # Consume only 2 events then abandon
                await agen.__anext__()
                await agen.__anext__()
                await agen.aclose()

            asyncio.run(run())
            # Give the reader a moment to observe the stop flag
            time.sleep(0.2)
            after = sum(1 for t in threading.enumerate() if t.name == "sc-reader")
            # Reader threads should not accumulate (daemon + stop flag)
            assert after <= baseline + 1
