"""Byte-equivalence tests for the Anthropic wire adapter.

The adapter (`_wire/anthropic_wire.py`) must render the SAME SSE bytes the
current inline `mvp.anthropic._stream_messages` emits for the same Bedrock event
sequence. We assert equivalence by reconstructing the expected frame list from
the same primitives the live path uses (`_sse_event`, the message envelope, the
frame order) and comparing to the adapter's prologue + per-event render +
epilogue output.

If a future edit to the adapter drifts from the wire shape, these fail — which
is exactly the tripwire the step-1a move needs.
"""
from __future__ import annotations

import json

from mvp import _converse_core as core
from mvp import _converse_types as t
from mvp._wire import anthropic_wire as wire


def _run_normalize(events):
    import asyncio

    async def collect():
        out = []
        async for ev in core.normalized_events(iter(events)):
            out.append(ev)
        return out

    return asyncio.run(collect())


def _render_all(model, bedrock_events):
    """Full adapter render: prologue + normalized(bedrock_events) + epilogue."""
    state = wire.AnthropicStreamState(model=model, message_id="msg_FIXED")
    frames = list(wire.stream_prologue(state))
    for ev in _run_normalize(bedrock_events):
        frames.extend(wire.render_stream_event(ev, state))
    frames.extend(wire.stream_epilogue(state))
    return frames


def _expected_frames(model, *, texts, in_tok, out_tok, stop_reason_wire):
    """The exact frame bytes today's _stream_messages emits, rebuilt from the
    same _sse_event primitive and envelope (message_id pinned to msg_FIXED).
    """

    def sse(event, data):
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()

    frames = [
        sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_FIXED",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        ),
        sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
    ]
    for txt in texts:
        frames.append(
            sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": txt},
                },
            )
        )
    frames.append(sse("content_block_stop", {"type": "content_block_stop", "index": 0}))
    frames.append(
        sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason_wire, "stop_sequence": None},
                "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
            },
        )
    )
    frames.append(sse("message_stop", {"type": "message_stop"}))
    return frames


def test_full_stream_is_byte_identical_to_todays_frames():
    model = "us.anthropic.claude-opus-4-7"
    bedrock_events = [
        {"contentBlockDelta": {"delta": {"text": "he"}}},
        {"contentBlockDelta": {"delta": {"text": ""}}},  # dropped, as today
        {"contentBlockDelta": {"delta": {"text": "llo"}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 12, "outputTokens": 3}}},
    ]
    got = _render_all(model, bedrock_events)
    expected = _expected_frames(
        model, texts=["he", "llo"], in_tok=12, out_tok=3, stop_reason_wire="end_turn"
    )
    assert got == expected


def test_tool_use_stop_reason_maps_like_today():
    """A tool_use Bedrock stop reason must render as `tool_use` on message_delta,
    exactly as _map_stop_reason produced (it was reachable via the stop event)."""
    model = "us.anthropic.claude-sonnet-4-7"
    bedrock_events = [
        {"contentBlockDelta": {"delta": {"text": "x"}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "tool_use"}},
        {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1}}},
    ]
    got = _render_all(model, bedrock_events)
    expected = _expected_frames(
        model, texts=["x"], in_tok=1, out_tok=1, stop_reason_wire="tool_use"
    )
    assert got == expected


def test_unknown_stop_reason_defaults_to_end_turn():
    model = "us.anthropic.claude-opus-4-7"
    bedrock_events = [
        {"contentBlockDelta": {"delta": {"text": "x"}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "some_future_reason"}},
        {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1}}},
    ]
    got = _render_all(model, bedrock_events)
    expected = _expected_frames(
        model, texts=["x"], in_tok=1, out_tok=1, stop_reason_wire="end_turn"
    )
    assert got == expected


def test_error_event_shape_matches_today():
    frames = list(wire.error_event("boom"))
    assert len(frames) == 1
    payload = json.loads(frames[0].decode().split("data: ", 1)[1])
    assert payload == {
        "type": "error",
        "error": {"type": "api_error", "message": "boom"},
    }
