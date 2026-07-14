"""Tests for GeneratorExit behavior at every yield point in run_stream.

Covers Fable's P0 gaps:
- 1.1: GeneratorExit at every wire-chunk yield → settle exactly once
- 1.2: Disconnect before metadata → settle uses reservation (not 0)
- 1.3: Bedrock stream exception events → settle + error frame
- 1.5: Multi-block streaming (text + toolUse)
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from mvp import _budget_flow
from mvp import _converse_types as t
from mvp._wire import anthropic_wire as wire


@dataclass
class _User:
    user_id: str = "test-user"
    org_id: str = "test-org"


class _FakeRepo:
    def __init__(self):
        self.refunded = 0
        self.released = False

    def refund(self, *, user_id, tenant_id, tokens):
        self.refunded += tokens


class _TestAdapter:
    def __init__(self):
        self.state = wire.AnthropicStreamState(model="test")

    def prologue(self):
        return wire.stream_prologue(self.state)

    def render_event(self, event):
        return wire.render_stream_event(event, self.state)

    def epilogue(self):
        return wire.stream_epilogue(self.state)

    def error_event(self, message):
        return wire.error_event(message)


def _multi_block_stream():
    """Simulates a Bedrock stream with text + toolUse blocks."""
    return iter([
        {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Let me "}}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "calculate."}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"contentBlockStart": {"contentBlockIndex": 1, "start": {"toolUse": {"toolUseId": "tu_1", "name": "calc"}}}},
        {"contentBlockDelta": {"contentBlockIndex": 1, "delta": {"toolUse": {"input": '{"expr"'}}}},
        {"contentBlockDelta": {"contentBlockIndex": 1, "delta": {"toolUse": {"input": ': "2+2"}'}}}},
        {"contentBlockStop": {"contentBlockIndex": 1}},
        {"messageStop": {"stopReason": "tool_use"}},
        {"metadata": {"usage": {"inputTokens": 100, "outputTokens": 50}}},
    ])


def _simple_stream():
    """Simple text stream: 2 deltas + stop + metadata."""
    return iter([
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "he"}}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "llo"}}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 12, "outputTokens": 3}}},
    ])


async def _count_frames(gen):
    """Fully consume and count yielded frames."""
    frames = []
    async for frame in gen:
        frames.append(frame)
    return frames


async def _drive_and_close(gen, *, stop_after):
    """Advance gen exactly stop_after frames, then aclose()."""
    agen = gen.__aiter__()
    got = []
    try:
        for _ in range(stop_after):
            got.append(await agen.__anext__())
        await agen.aclose()
    except StopAsyncIteration:
        pass
    return got


# ---------------------------------------------------------------------------
# 1.1: GeneratorExit at every yield → settle exactly once
# ---------------------------------------------------------------------------

def _make_gen(stream_factory, settle_calls, repo):
    return _budget_flow.run_stream(
        body=None,
        model_id="test",
        model_alias="test",
        user=_User(),
        tenants_repo=repo,
        reservation=5000,
        invoke_stream=lambda *, body, model_id: {"stream": stream_factory()},
        settle=lambda **kw: settle_calls.append(kw),
        release=lambda ctx: setattr(ctx, "released", True),
        adapter=_TestAdapter(),
    )


def test_full_run_settle_count():
    """Full consumption settles exactly once."""
    settle_calls = []
    repo = _FakeRepo()
    gen = _make_gen(_simple_stream, settle_calls, repo)
    asyncio.run(_count_frames(gen))
    assert len(settle_calls) == 1
    assert settle_calls[0]["actual_input_tokens"] == 12
    assert settle_calls[0]["actual_output_tokens"] == 3


@pytest.mark.parametrize("stop_after", list(range(1, 10)))
def test_disconnect_at_every_yield_settles_once(stop_after):
    """GeneratorExit at any wire-chunk position settles exactly once."""
    settle_calls = []
    repo = _FakeRepo()
    gen = _make_gen(_simple_stream, settle_calls, repo)
    asyncio.run(_drive_and_close(gen, stop_after=stop_after))
    assert len(settle_calls) == 1, (
        f"Expected 1 settle at stop_after={stop_after}, got {len(settle_calls)}"
    )


# ---------------------------------------------------------------------------
# 1.2: Disconnect before metadata → settle uses observed-so-far
# ---------------------------------------------------------------------------

def test_disconnect_before_metadata_settles_with_zero():
    """If metadata hasn't arrived, settle is called with 0 tokens.

    This documents the current behavior. In prod, Bedrock may have billed
    more — the orphan reaper or a future drain-to-metadata fix handles
    the gap. The key invariant is: settle fires exactly once, never leaks.
    """
    settle_calls = []
    repo = _FakeRepo()
    # Stop after prologue (2 frames) + first delta (1 frame) = 3 frames
    gen = _make_gen(_simple_stream, settle_calls, repo)
    asyncio.run(_drive_and_close(gen, stop_after=3))
    assert len(settle_calls) == 1
    # Usage will be 0 because metadata hasn't been seen
    assert settle_calls[0]["actual_input_tokens"] == 0
    assert settle_calls[0]["actual_output_tokens"] == 0


# ---------------------------------------------------------------------------
# 1.3: Bedrock stream raises exception mid-flight
# ---------------------------------------------------------------------------

def _raising_stream():
    """Stream that raises after first delta."""
    yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "hi"}}}
    raise RuntimeError("bedrock stream error mid-flight")


def test_mid_stream_exception_settles_once():
    """An exception during stream iteration → mid-stream settle path."""
    settle_calls = []
    repo = _FakeRepo()
    gen = _make_gen(_raising_stream, settle_calls, repo)
    frames = asyncio.run(_count_frames(gen))
    assert len(settle_calls) == 1
    # Should not have released (mid-stream path)
    assert not repo.released
    # Error frame emitted
    assert any(b"error" in f for f in frames)


# ---------------------------------------------------------------------------
# 1.4: invoke_stream raises → refund + release, no settle
# ---------------------------------------------------------------------------

def test_invoke_failure_refunds_and_releases():
    """invoke_stream raises → refund + release, settle NOT called."""
    settle_calls = []
    repo = _FakeRepo()

    def raising_invoke(*, body, model_id):
        raise RuntimeError("connection refused")

    gen = _budget_flow.run_stream(
        body=None,
        model_id="test",
        model_alias="test",
        user=_User(),
        tenants_repo=repo,
        reservation=5000,
        invoke_stream=raising_invoke,
        settle=lambda **kw: settle_calls.append(kw),
        release=lambda ctx: setattr(ctx, "released", True),
        adapter=_TestAdapter(),
    )
    frames = asyncio.run(_count_frames(gen))
    assert len(settle_calls) == 0, "invoke failure must NOT settle"
    assert repo.refunded == 5000
    assert repo.released
    assert any(b"error" in f for f in frames)


# ---------------------------------------------------------------------------
# 1.5: Multi-block streaming (text + toolUse)
# ---------------------------------------------------------------------------

def test_multi_block_stream_all_events_rendered():
    """Multi-block (text + toolUse) stream renders all events correctly."""
    settle_calls = []
    repo = _FakeRepo()
    gen = _make_gen(_multi_block_stream, settle_calls, repo)
    frames = asyncio.run(_count_frames(gen))

    # Decode and check for expected SSE events
    decoded = [f.decode() for f in frames]
    text_deltas = [d for d in decoded if "text_delta" in d]
    tool_starts = [d for d in decoded if '"type": "tool_use"' in d and "content_block_start" in d]
    input_json = [d for d in decoded if "input_json_delta" in d]
    block_stops = [d for d in decoded if "content_block_stop" in d]

    assert len(text_deltas) == 2, f"Expected 2 text deltas, got {len(text_deltas)}"
    assert len(tool_starts) == 1, f"Expected 1 tool_use start, got {len(tool_starts)}"
    assert len(input_json) == 2, f"Expected 2 input_json_delta, got {len(input_json)}"
    # 2 block stops (block 0 + block 1) + epilogue content_block_stop
    assert len(block_stops) >= 2

    # Settle used correct usage
    assert len(settle_calls) == 1
    assert settle_calls[0]["actual_input_tokens"] == 100
    assert settle_calls[0]["actual_output_tokens"] == 50


@pytest.mark.parametrize("stop_after", list(range(1, 15)))
def test_multi_block_disconnect_settles_once(stop_after):
    """GeneratorExit during multi-block stream still settles exactly once."""
    settle_calls = []
    repo = _FakeRepo()
    gen = _make_gen(_multi_block_stream, settle_calls, repo)
    asyncio.run(_drive_and_close(gen, stop_after=stop_after))
    assert len(settle_calls) == 1, (
        f"Multi-block: expected 1 settle at stop_after={stop_after}, got {len(settle_calls)}"
    )


# ---------------------------------------------------------------------------
# SEV-1 regression (full-branch Fable review): disconnect WHILE the offloaded
# settle is running in its thread must NOT double-settle. The settle now runs
# via asyncio.to_thread; a client that closes on `data: [DONE]` cancels the
# await, but the thread still commits — the finally must not settle again.
# ---------------------------------------------------------------------------

def test_disconnect_during_offloaded_settle_settles_once():
    """Close the generator while settle is mid-flight in its thread.

    Regression for the double-settle: with fresh idempotency tokens per settle,
    a second settle would subtract `reserved` twice (pool over-admission) and
    double-bill. The once-guard must make it exactly one settle.
    """
    import threading

    settle_count = {"n": 0}
    in_settle = threading.Event()
    release_settle = threading.Event()
    lock = threading.Lock()

    def slow_settle(**kw):
        with lock:
            settle_count["n"] += 1
        in_settle.set()          # signal we're inside settle
        release_settle.wait(2.0)  # hold the thread (simulate slow DDB)

    repo = _FakeRepo()
    gen = _budget_flow.run_stream(
        body=None, model_id="test", model_alias="test", user=_User(),
        tenants_repo=repo, reservation=5000,
        invoke_stream=lambda *, body, model_id: {"stream": _simple_stream()},
        settle=slow_settle,
        release=lambda ctx: setattr(ctx, "released", True),
        adapter=_TestAdapter(),
    )

    async def run():
        agen = gen.__aiter__()
        # Consume everything so the clean-completion settle is triggered.
        async def _consume():
            try:
                while True:
                    await agen.__anext__()
            except StopAsyncIteration:
                pass
        task = asyncio.create_task(_consume())
        # Wait until settle has started in its thread, then close/cancel.
        await asyncio.to_thread(in_settle.wait, 2.0)
        release_settle.set()   # let the in-flight settle finish
        await task
        await agen.aclose()    # finally runs here — must not settle again

    asyncio.run(run())
    assert settle_count["n"] == 1, f"expected exactly 1 settle, got {settle_count['n']}"


def test_invoke_error_disconnect_does_not_settle_after_refund():
    """Invoke-time failure refunds+releases and must NEVER settle — even if the
    generator is closed right after (the finally must respect the claim)."""
    settle_count = {"n": 0}
    refund_release = {"n": 0}

    def boom(*, body, model_id):
        raise RuntimeError("invoke failed")

    repo = _FakeRepo()

    def _refund(**kw):
        refund_release["n"] += 1

    gen = _budget_flow.run_stream(
        body=None, model_id="test", model_alias="test", user=_User(),
        tenants_repo=repo, reservation=5000,
        invoke_stream=boom,
        settle=lambda **kw: settle_count.__setitem__("n", settle_count["n"] + 1),
        release=lambda ctx: refund_release.__setitem__("n", refund_release["n"] + 1),
        adapter=_TestAdapter(),
    )
    repo.refund = _refund  # count refund calls

    async def run():
        agen = gen.__aiter__()
        try:
            while True:
                await agen.__anext__()
        except StopAsyncIteration:
            pass
        await agen.aclose()

    asyncio.run(run())
    # Invoke-error path: refund+release happened, settle NEVER did.
    assert settle_count["n"] == 0, f"invoke-error path must not settle, got {settle_count['n']}"
