"""The Bedrock Converse invoker (layer b).

This is the ONLY place that iterates a Bedrock `converse_stream` event stream and
maps it to the wire-agnostic `StreamEvent` sequence the adapters render. It also
owns the thread-offload wrapper (`_aiter_blocking_stream`) that keeps the event
loop responsive while boto3 blocks on socket reads.

It touches no DynamoDB and no budget state — money orchestration lives in
`_budget_flow.py`, wire shapes live in `_wire/*.py`.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator, Iterator

from . import _converse_types as t


def cache_tokens_from_usage(usage: dict[str, Any]) -> tuple[int, int]:
    """Extract (cache_read, cache_write) token counts from a Bedrock usage block.

    Bedrock's Converse usage reports prompt-cache activity as
    `cacheReadInputTokens` / `cacheWriteInputTokens` (0 or absent when caching is
    not used). Returning them lets settle price cached traffic at its own rate
    instead of billing it at zero. Bad/missing values collapse to 0.
    """

    def _int(v) -> int:
        try:
            n = int(v)
        except (TypeError, ValueError):
            return 0
        return n if n > 0 else 0

    return (
        _int(usage.get("cacheReadInputTokens")),
        _int(usage.get("cacheWriteInputTokens")),
    )


async def _aiter_blocking_stream(
    stream: Iterator[dict[str, Any]],
) -> AsyncGenerator[dict[str, Any], None]:
    """Wrap a blocking iterator (boto3 EventStream) for use under asyncio.

    Each `next(it)` is dispatched to the default thread executor, so the uvicorn
    event loop is free to service other coroutines while the underlying socket
    waits for the next Bedrock SSE chunk. The function yields one event per loop
    iteration; when the upstream iterator raises `StopIteration` (i.e. Bedrock
    closed the stream cleanly) we return normally.

    NOTE: moved VERBATIM from mvp.anthropic — the StopIteration sentinel is
    load-bearing. Rewriting to `to_thread(next, it)` turns a clean StopIteration
    into `RuntimeError: generator raised StopIteration` (PEP 479) that would
    masquerade as a mid-stream failure. Do not "simplify" it.
    """
    sentinel = object()
    it = iter(stream)

    def _next_or_sentinel() -> Any:
        # `StopIteration` cannot cross thread boundaries cleanly; convert
        # to a sentinel so the caller terminates without re-raising
        # `RuntimeError: generator raised StopIteration`.
        try:
            return next(it)
        except StopIteration:
            return sentinel

    while True:
        item = await asyncio.to_thread(_next_or_sentinel)
        if item is sentinel:
            return
        yield item


async def normalized_events(
    event_source: Any,
) -> AsyncGenerator[t.StreamEvent, None]:
    """Map a Bedrock `converse_stream` response's event stream to StreamEvents.

    Behaviour matches today's `_stream_messages` loop exactly (step 1a is a
    faithful move, not a rewrite):

      - a `contentBlockDelta` carrying non-empty text -> ContentTextDelta(0, ...);
        empty text yields nothing (as today's `if text:` guard).
      - `messageStop` -> MessageStop carrying the RAW Bedrock stop reason (the
        adapter maps it to its wire vocabulary, as today's `_map_stop_reason`
        call did at render time).
      - `metadata.usage` -> Usage(input, output, cache_read, cache_write).

    The single-block, index-0 shape mirrors today; multi-block and reasoning
    events are added in step 2 with their own red->green tests.
    """
    async for event in _aiter_blocking_stream(event_source):
        if "contentBlockDelta" in event:
            delta_obj = event["contentBlockDelta"].get("delta", {})
            text = delta_obj.get("text", "")
            if text:
                yield t.ContentTextDelta(index=0, text=text)
        elif "messageStop" in event:
            yield t.MessageStop(stop_reason=event["messageStop"].get("stopReason"))
        elif "metadata" in event:
            usage = event["metadata"].get("usage", {})
            cr, cw = cache_tokens_from_usage(usage)
            yield t.Usage(
                input=int(usage.get("inputTokens", 0)),
                output=int(usage.get("outputTokens", 0)),
                cache_read=cr,
                cache_write=cw,
            )
