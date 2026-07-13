"""Anthropic Messages wire adapter.

Renders the normalized StreamEvent sequence to Anthropic-style SSE frames,
byte-identical to what `mvp.anthropic._stream_messages` emitted before the move.
For step 1a this keeps the exact single-block, index-0 shape; multi-block,
tool_use, and reasoning rendering land in step 2 with their own tests.

State machine per stream (mirrors today's fixed frame order):
    message_start
    content_block_start (index 0, empty text)     <- emitted in the prologue
    content_block_delta*                           <- one per non-empty text delta
    content_block_stop (index 0)                   <- emitted in the epilogue
    message_delta (stop_reason + usage)            <- epilogue
    message_stop                                   <- epilogue
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .. import _converse_types as t


def _sse_event(event: str, data: dict[str, Any]) -> bytes:
    """Frame one SSE event. Identical to mvp.anthropic._sse_event."""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


_STOP_REASON_MAP = {
    "end_turn": "end_turn",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
    "tool_use": "tool_use",
    "content_filtered": "refusal",
}


def map_stop_reason(bedrock_reason: Optional[str]) -> str:
    """Map a Bedrock stop reason to the Anthropic vocabulary (identity-ish).

    Matches mvp.anthropic._map_stop_reason, including its default of `end_turn`
    for None/unknown (today's `_map_stop_reason(stop_reason_bedrock or "end_turn")`).
    """
    return _STOP_REASON_MAP.get(bedrock_reason or "end_turn", "end_turn")


@dataclass
class AnthropicStreamState:
    """Per-stream render state. `model` echoes the client's requested alias."""

    model: str
    message_id: str = field(default_factory=lambda: f"msg_{uuid.uuid4().hex[:24]}")
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: Optional[str] = None


def stream_prologue(state: AnthropicStreamState) -> Iterable[bytes]:
    """Leading SSE: message_start only.

    content_block_start is emitted lazily when the first ContentBlockStart or
    ContentToolUseStart event arrives — this prevents a phantom text block
    at index 0 when the model's first block is a toolUse.
    """
    yield _sse_event(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": state.message_id,
                "type": "message",
                "role": "assistant",
                "model": state.model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )


def render_stream_event(
    event: t.StreamEvent, state: AnthropicStreamState
) -> Iterable[bytes]:
    """Render one normalized event to zero or more Anthropic SSE frames.

    Step-1a scope: text deltas render to content_block_delta; Usage/MessageStop
    update state (rendered on the epilogue frames, matching today's ordering
    where usage+stop_reason ride the trailing message_delta). Other event types
    are inert here until step 2 wires tool_use/reasoning rendering.
    """
    if isinstance(event, t.ContentTextDelta):
        if event.text:
            yield _sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": event.index,
                    "delta": {"type": "text_delta", "text": event.text},
                },
            )
    elif isinstance(event, t.ContentBlockStart):
        if event.block_type == "tool_use":
            pass  # tool_use block_start rendered via ContentToolUseStart
        else:
            yield _sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": event.index,
                    "content_block": {"type": event.block_type, "text": ""}
                    if event.block_type == "text"
                    else {"type": event.block_type},
                },
            )
    elif isinstance(event, t.ContentToolUseStart):
        yield _sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": event.index,
                "content_block": {
                    "type": "tool_use",
                    "id": event.tool_use_id,
                    "name": event.name,
                    "input": {},
                },
            },
        )
    elif isinstance(event, t.ContentToolUseDelta):
        yield _sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": event.index,
                "delta": {"type": "input_json_delta", "partial_json": event.partial_json},
            },
        )
    elif isinstance(event, t.ContentBlockStop):
        yield _sse_event(
            "content_block_stop",
            {"type": "content_block_stop", "index": event.index},
        )
    elif isinstance(event, t.Usage):
        state.input_tokens = event.input or state.input_tokens
        state.output_tokens = event.output or state.output_tokens
    elif isinstance(event, t.MessageStop):
        state.stop_reason = event.stop_reason


def stream_epilogue(state: AnthropicStreamState) -> Iterable[bytes]:
    """Trailing SSE: message_delta (usage + stop), message_stop."""
    yield _sse_event(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {
                "stop_reason": map_stop_reason(state.stop_reason),
                "stop_sequence": None,
            },
            "usage": {
                "input_tokens": state.input_tokens,
                "output_tokens": state.output_tokens,
            },
        },
    )
    yield _sse_event("message_stop", {"type": "message_stop"})


def error_event(message: str) -> Iterable[bytes]:
    """Wire-shaped SSE error frame. Identical shape to today's inline error yield."""
    yield _sse_event(
        "error",
        {
            "type": "error",
            "error": {"type": "api_error", "message": message},
        },
    )
