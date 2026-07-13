"""Unit tests for the Converse invoker's event normalization (_converse_core).

These pin, in isolation (no DynamoDB, no budget), that:
  - `_aiter_blocking_stream` still yields in order, terminates cleanly on
    StopIteration, and offloads each `next()` to a worker thread (moved VERBATIM
    from mvp.anthropic — same guarantees the pre-move regression tests asserted);
  - `normalized_events` maps a Bedrock converse_stream event sequence to the
    wire-agnostic StreamEvent sequence with the SAME observable behaviour as
    today's `_stream_messages` loop: non-empty text deltas only, raw stop reason,
    usage totals with cache tokens.
"""
from __future__ import annotations

import asyncio
import threading

from mvp import _converse_core as core
from mvp import _converse_types as t


def _run(agen_factory):
    async def collect():
        out = []
        async for ev in agen_factory():
            out.append(ev)
        return out

    return asyncio.run(collect())


# --- _aiter_blocking_stream: order / termination / thread-offload -----------
def test_aiter_yields_in_order():
    events = [
        {"contentBlockDelta": {"delta": {"text": "a"}}},
        {"messageStop": {"stopReason": "end_turn"}},
    ]
    got = _run(lambda: core._aiter_blocking_stream(iter(events)))
    assert got == events


def test_aiter_terminates_on_stopiteration_without_runtimeerror():
    assert _run(lambda: core._aiter_blocking_stream(iter([]))) == []


def test_aiter_offloads_next_to_worker_thread():
    main_tid = threading.get_ident()
    seen: list[int] = []

    class TracingIter:
        def __init__(self):
            self._n = 0

        def __iter__(self):
            return self

        def __next__(self):
            seen.append(threading.get_ident())
            self._n += 1
            if self._n > 2:
                raise StopIteration
            return {"contentBlockDelta": {"delta": {"text": "x"}}}

    _run(lambda: core._aiter_blocking_stream(TracingIter()))
    assert seen, "iterator must have been advanced"
    assert all(tid != main_tid for tid in seen), "every next() must run off the loop"


# --- normalized_events: faithful mapping of a Bedrock stream ----------------
def test_normalized_events_maps_text_stop_and_usage():
    source = iter(
        [
            {"contentBlockDelta": {"delta": {"text": "he"}}},
            {"contentBlockDelta": {"delta": {"text": ""}}},  # empty -> dropped
            {"contentBlockDelta": {"delta": {"text": "llo"}}},
            {"messageStop": {"stopReason": "tool_use"}},
            {
                "metadata": {
                    "usage": {
                        "inputTokens": 12,
                        "outputTokens": 3,
                        "cacheReadInputTokens": 4,
                        "cacheWriteInputTokens": 0,
                    }
                }
            },
        ]
    )
    got = _run(lambda: core.normalized_events(source))

    # Two text deltas (the empty one is dropped, as today's `if text:` guard).
    text_deltas = [e for e in got if isinstance(e, t.ContentTextDelta)]
    assert [d.text for d in text_deltas] == ["he", "llo"]
    assert all(d.index == 0 for d in text_deltas)

    stops = [e for e in got if isinstance(e, t.MessageStop)]
    assert len(stops) == 1
    # RAW Bedrock reason preserved; adapter maps it (as _map_stop_reason did).
    assert stops[0].stop_reason == "tool_use"

    usages = [e for e in got if isinstance(e, t.Usage)]
    assert len(usages) == 1
    assert (usages[0].input, usages[0].output) == (12, 3)
    assert (usages[0].cache_read, usages[0].cache_write) == (4, 0)


def test_normalized_events_accumulator_reproduces_todays_usage():
    """Feeding the normalized events through UsageAccumulator must land the same
    totals today's inline loop accumulated (single metadata event, index 0).
    """
    source = iter(
        [
            {"contentBlockDelta": {"delta": {"text": "hi"}}},
            {"messageStop": {"stopReason": "end_turn"}},
            {"metadata": {"usage": {"inputTokens": 7, "outputTokens": 2}}},
        ]
    )
    acc = t.UsageAccumulator()
    for ev in _run(lambda: core.normalized_events(source)):
        acc.absorb(ev)
    assert (acc.input_tokens, acc.output_tokens) == (7, 2)
    assert acc.stop_reason == "end_turn"
