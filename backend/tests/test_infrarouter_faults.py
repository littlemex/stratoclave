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
from botocore.exceptions import ClientError

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

    def test_failover_commits_to_a_DIFFERENT_region(self, monkeypatch):
        """The core failover contract: when the PRIMARY region keeps failing,
        the committed target must be a DIFFERENT (failover) region — not just
        "a failover happened". This is the assertion the older tests omitted
        (Fable review): they retried the same target. Uses a region-aware mock
        (no fault injection) so the real classify → advance-region path runs on
        a genuine ClientError shape.
        """
        from unittest.mock import MagicMock, patch

        from mvp.routing import chains

        # Explicit chain: primary us-east-1 + us-west-2 + us-east-2 (all us-*, so
        # the jurisdiction filter is irrelevant and the chain is deterministic).
        monkeypatch.setenv("BEDROCK_REGION", "us-east-1")
        monkeypatch.setenv("STRATOCLAVE_FAILOVER_REGIONS", "us-west-2,us-east-2")
        chains.reset_catalog()

        seen_regions: list[str] = []

        def mock_client(region):
            seen_regions.append(region)
            c = MagicMock()
            if region == "us-east-1":
                # Primary throttles on every attempt → FAILOVER to next region.
                c.converse_stream.side_effect = ClientError(
                    {"Error": {"Code": "ThrottlingException", "Message": "rate"}},
                    "ConverseStream",
                )
            else:
                c.converse_stream.return_value = {"stream": _mock_stream()}
            return c

        with patch("mvp.routing.infrarouter.bedrock_client", side_effect=mock_client):
            async def run():
                routed = await route_stream(_req(rid="rdiff"))
                events = [e async for e in routed.events]
                return routed, events

            routed, events = asyncio.run(run())

        chains.reset_catalog()

        # Committed target is a real failover region, NOT the primary.
        assert routed.target.region != "us-east-1"
        assert routed.target.region in {"us-west-2", "us-east-2"}
        # The primary was actually attempted (and failed over) before commit.
        assert "us-east-1" in seen_regions
        assert any(
            a.outcome == "failover" and a.target.region == "us-east-1"
            for a in routed.attempt_facts
        )
        assert routed.attempt_facts[-1].outcome == "success"
        assert len(events) == 2

    def test_fail_region_spec_targets_only_that_region(self, monkeypatch):
        """The `fail-region-<R>` fault fails ONLY region R, so a real failover to
        a healthy region succeeds — the mechanism the live E2E uses to prove a
        successful cross-region commit (region-agnostic specs can only show
        exhaustion). Here fault injection IS enabled (autouse fixture)."""
        from unittest.mock import MagicMock, patch

        from mvp.routing import chains

        monkeypatch.setenv("BEDROCK_REGION", "us-east-1")
        monkeypatch.setenv("STRATOCLAVE_FAILOVER_REGIONS", "us-west-2")
        chains.reset_catalog()

        seen: list[str] = []

        def mock_client(region):
            seen.append(region)
            c = MagicMock()
            c.converse_stream.return_value = {"stream": _mock_stream()}
            return c

        with patch("mvp.routing.infrarouter.bedrock_client", side_effect=mock_client):
            async def run():
                routed = await route_stream(_req(fault_spec="fail-region-us-east-1", rid="rfr"))
                events = [e async for e in routed.events]
                return routed, events

            routed, events = asyncio.run(run())

        chains.reset_catalog()

        # us-east-1 is synthetically unavailable → committed on us-west-2.
        # NOTE: the fault raises BEFORE _attempt_invoke builds the client, so
        # `seen` (populated inside the client factory) only records the region
        # that actually got a client (us-west-2). The us-east-1 attempt is
        # visible in attempt_facts, which is the authoritative record.
        assert routed.target.region == "us-west-2"
        assert seen == ["us-west-2"]
        assert any(
            a.outcome == "failover" and a.target.region == "us-east-1"
            for a in routed.attempt_facts
        )
        assert len(events) == 2

    def test_all_regions_throttle_raises_after_exhausting_chain(self, monkeypatch):
        """If every region fails over, the chain exhausts and the last error is
        raised (fail-closed) rather than committing to nothing."""
        from unittest.mock import MagicMock, patch

        from mvp.routing import chains

        monkeypatch.setenv("BEDROCK_REGION", "us-east-1")
        monkeypatch.setenv("STRATOCLAVE_FAILOVER_REGIONS", "us-west-2")
        chains.reset_catalog()

        def mock_client(region):
            c = MagicMock()
            c.converse_stream.side_effect = ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "rate"}},
                "ConverseStream",
            )
            return c

        with patch("mvp.routing.infrarouter.bedrock_client", side_effect=mock_client):
            async def run():
                return await route_stream(_req(rid="rexhaust"))

            with pytest.raises(ClientError):
                asyncio.run(run())

        chains.reset_catalog()


class TestFirstEventTimeout:
    def test_timeout_first_event_never_settles_as_success(self, monkeypatch):
        """timeout-first-event: every attempt hangs past the first-event
        guard, so no attempt commits. The chain exhausts and route_stream
        raises (release path), never returning a success stream.

        Regression: the fault used to `asyncio.sleep` and then invoke the real
        Bedrock client, so it produced a *successful* response instead of
        exercising the first-event timeout — the guard was never tested.
        """
        from unittest.mock import MagicMock, patch

        # Shrink the guard so the test is fast; the fault feeds a stream that
        # never yields, so wait_for must time out on every attempt.
        monkeypatch.setattr("mvp.routing.infrarouter._FIRST_EVENT_TIMEOUT_S", 0.5)

        invoked = {"n": 0}

        def mock_client(region):
            invoked["n"] += 1
            c = MagicMock()
            # If the router ever reached a real invoke under this fault, it
            # would get a valid stream — and the test would wrongly pass. It
            # must NOT: the hang path bypasses _attempt_invoke entirely.
            c.converse_stream.return_value = {"stream": _mock_stream()}
            return c

        with patch("mvp.routing.infrarouter.bedrock_client", side_effect=mock_client):
            async def run():
                routed = await route_stream(_req(fault_spec="timeout-first-event", rid="hang"))
                # Should not get here; if we do, drain to surface the bug.
                return [e async for e in routed.events]

            with pytest.raises(Exception) as exc:
                asyncio.run(run())
            # The surfaced error is the timeout/last-exc, not a success.
            assert not isinstance(exc.value, StopAsyncIteration)
            # Every attempt classified as failover (timeout), none succeeded.
            # (invoked may be 0 because the hang path never calls the client.)


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
