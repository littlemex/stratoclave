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
import base64
from typing import Optional, Any, AsyncGenerator, Iterator

from core.logging import get_logger

from . import _converse_types as t

logger = get_logger(__name__)

# The one declaration of Bedrock's three reasoningContent legs (streaming
# delta shape: `contentBlockDelta.delta.reasoningContent` carries these three
# keys, flat, any subset truthy at once). `normalized_events`' unknown-leg
# warning is a set difference against this frozenset, and the non-streaming
# leg extractor below reads the SAME three names rather than re-spelling them
# — see C13.4: a leg spelled twice is trustworthy on one path and silently
# stale on the other the day the provider adds a fourth leg.
REASONING_LEGS: frozenset[str] = frozenset({"text", "signature", "redactedContent"})


def _is_nonneg_int(v: Any) -> bool:
    """True iff `v` is an `int` (never `bool`) that is >= 0.

    `bool` is a subclass of `int` in Python, and a caller sending
    `"outputTokens": true` is a malformed request, not a token count of one —
    accepting it would substitute a coincidental type match for a real
    measurement, which is exactly the class of defect `usage_from_bedrock`
    exists to refuse.
    """
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def usage_from_bedrock(usage: Optional[dict[str, Any]]) -> Optional[t.Usage]:
    """Build a `Usage` event from a Bedrock `usage` block, or `None` if it
    cannot be trusted as a measurement.

    Returns `None` when `usage` is `None`, empty, or missing either
    `inputTokens` or `outputTokens`, or when either is present but is not a
    non-negative integer. This function never substitutes a default for an
    unread field — the caller (`normalized_events`'s `metadata` branch, and
    the non-streaming Converse path) must treat `None` as "nothing to yield",
    not as "yield zero".

    This is the extension, to the two legs that carry the money, of the "zero
    is a measurement, absence is not" doctrine `cache_tokens_from_usage`
    above already states for the cache legs. Before this function existed,
    `int(usage.get("inputTokens", 0))` turned an absent or malformed usage
    block into a measured zero, and `UsageAccumulator.absorb` then marked that
    zero as `saw_final_usage=True` — a billed call settled at nothing and was
    recorded as fully observed, not as unobserved.
    """
    if not usage:
        return None
    input_tokens = usage.get("inputTokens")
    output_tokens = usage.get("outputTokens")
    if not _is_nonneg_int(input_tokens) or not _is_nonneg_int(output_tokens):
        return None
    cache_read, cache_write = cache_tokens_from_usage(usage)
    return t.Usage(
        input=input_tokens,
        output=output_tokens,
        cache_read=cache_read,
        cache_write=cache_write,
    )


def unknown_reasoning_legs_in_output_block(reasoning_content: dict[str, Any]) -> frozenset[str]:
    """The NON-STREAMING equivalent of the streaming path's `set(reasoning.
    items()) - REASONING_LEGS` check, called BEFORE `reasoning_legs_from_
    output_block` flattens this same block.

    This must run before the flatten, not after: `reasoning_legs_from_
    output_block`'s `candidate` dict is built from exactly the three names
    `REASONING_LEGS` declares, so a fourth leg the provider adds is never
    even constructed into `candidate`, and the `if leg in REASONING_LEGS`
    filter downstream has nothing left to reject — the flattened dict looks
    identical whether the provider sent three legs or four. Checking the RAW
    block here, once, before that construction, is what makes the leg
    visible at all on this path (the defect a review round predicted in
    these words: "the non-streaming path silently drops the next leg Bedrock
    adds while the streaming path warns" — CONTRACTS.md C13.4, which this
    function exists to keep true for the non-streaming transport too).

    Mind the nesting Bedrock's OUTPUT shape uses (see the docstring below):
    `text`/`signature` sit one level under `reasoningText`, while
    `redactedContent` — and any unknown leg the provider chooses to add — can
    arrive as a SIBLING of `reasoningText` instead. An unknown key can appear
    at either level, so both are checked; checking only one would miss
    exactly the shape the provider picks for its next leg.

    Returns the block's unknown-leg names, or an empty frozenset. This
    function sees only ONE block; a response can carry more than one
    `reasoningContent` block, so "warn once per response" is the caller's
    job (mirroring `normalized_events`, which owns "once per stream" for the
    same reason: this function has no visibility beyond its own block).
    """
    reasoning_text = reasoning_content.get("reasoningText") or {}
    nested_unknown = {k for k, v in reasoning_text.items() if v} - REASONING_LEGS
    sibling_unknown = {
        k for k, v in reasoning_content.items() if v and k != "reasoningText"
    } - REASONING_LEGS
    return frozenset(nested_unknown | sibling_unknown)


def reasoning_legs_from_output_block(reasoning_content: dict[str, Any]) -> dict[str, Any]:
    """Flatten a NON-STREAMING Converse `reasoningContent` OUTPUT block to the
    same flat `{leg_name: value}` shape the streaming delta already uses.

    Bedrock's `output.message.content[].reasoningContent` nests the text and
    signature legs one level under `reasoningText` and leaves `redactedContent`
    at the top level (the same union type used to echo reasoning back as
    input — see `anthropic._convert_content`'s `thinking` branch and
    `reservation_bound`'s reasoningContent walk for the identical nesting).
    The streaming delta (`contentBlockDelta.delta.reasoningContent`) reports
    all three flat. Reconciling the shape here, once, is what lets the
    non-streaming path read the same three leg names `REASONING_LEGS`
    declares instead of re-deriving its own set.

    Only truthy legs are returned — an absent leg is omitted, not present
    with an empty/`None` value, matching the "absence is not zero" rule this
    module applies everywhere else.

    This function ONLY flattens the three known legs; it cannot, by
    construction, see a fourth one (that is exactly the bug — see
    `unknown_reasoning_legs_in_output_block` above). A caller that wants the
    unknown-leg warning must call that function on the SAME raw
    `reasoning_content` BEFORE calling this one, not derive it from this
    function's return value.
    """
    reasoning_text = reasoning_content.get("reasoningText") or {}
    candidate = {
        "text": reasoning_text.get("text"),
        "signature": reasoning_text.get("signature"),
        "redactedContent": reasoning_content.get("redactedContent"),
    }
    return {leg: v for leg, v in candidate.items() if leg in REASONING_LEGS and v}


def answerless_billed(
    *,
    output_tokens: Optional[int],
    saw_nonempty_text: bool,
    saw_tool_use: bool,
    block_types: frozenset[str],
) -> bool:
    """`True` iff a call was billed for output but produced no answer at all.

    `output_tokens` a positive integer, `saw_nonempty_text` false, AND
    `saw_tool_use` false — all three, or a tool-use-only reply (a normal,
    successful agentic turn) would warn on every routine call. `block_types`
    is carried through for the caller's warning payload; it plays no part in
    the decision, so a new block type this function does not know about can
    never silently change what counts as "no answer".

    This is the only place the predicate exists, so the two
    transports that call it — and the two ways a "reply" can be represented
    (an appended empty text block that is truthy vs. a dropped empty delta) —
    cannot independently drift on what "no answer" means. This function does
    not itself emit the warning; the caller that gathers the token/block
    facts across its own transport does that.
    """
    if not isinstance(output_tokens, int) or isinstance(output_tokens, bool):
        return False
    if output_tokens <= 0:
        return False
    return not saw_nonempty_text and not saw_tool_use


def cache_tokens_from_usage(
    usage: dict[str, Any],
) -> tuple[Optional[int], Optional[int]]:
    """Extract (cache_read, cache_write) token counts from a Bedrock usage block.

    Bedrock's Converse usage reports prompt-cache activity as
    `cacheReadInputTokens` / `cacheWriteInputTokens`. Returning them lets settle
    price cached traffic at its own rate instead of billing it at zero.

    An ABSENT or unparseable field returns `None`, not 0. Some models report these
    counts and some never do, and whether a model caches is the single largest term
    in its economics — so a caller comparing models has to tell "the provider said
    nothing was cached" from "the provider does not report caching". Collapsing
    both into 0 made the second look like the first. Zero is a measurement; absence
    is not. The charge for an absent leg is still nothing (`rate_usage` costs it at
    zero) — what changes is that the record no longer claims the provider said so.
    """

    def _count(v) -> Optional[int]:
        if v is None:
            return None
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        return n if n > 0 else 0

    return (
        _count(usage.get("cacheReadInputTokens")),
        _count(usage.get("cacheWriteInputTokens")),
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
      - a `contentBlockDelta` carrying `reasoningContent` -> one
        `ContentReasoningDelta` per truthy leg (`text`/`signature`/
        `redactedContent`), each an INDEPENDENT `if` — never an `elif` chain.
        A single delta can carry an empty `text` and a non-empty `signature`
        at once, and an `elif` chain drops exactly that shape. An
        unrecognised leg (a key outside `REASONING_LEGS` with a truthy value)
        logs a warning once per stream rather than being silently dropped.
      - `messageStop` -> MessageStop carrying the RAW Bedrock stop reason (the
        adapter maps it to its wire vocabulary, as today's `_map_stop_reason`
        call did at render time).
      - `metadata.usage` -> a `Usage` event, but ONLY when `usage_from_bedrock`
        can read one; an absent/empty/malformed usage block yields nothing at
        all, so the stream ends unobserved rather than settling at a measured
        zero.

    The single-block, index-0 shape mirrors today for text; multi-block
    indexing already works via `contentBlockIndex`, and reasoning events are
    now on the same footing as text and tool-use.
    """
    block_index = 0
    started_indices: set[int] = set()
    warned_unknown_reasoning_legs = False
    if hasattr(event_source, "__aiter__"):
        event_iter = event_source
    else:
        event_iter = _aiter_blocking_stream(event_source)
    async for event in event_iter:
        if "contentBlockStart" in event:
            start = event["contentBlockStart"]
            idx = start.get("contentBlockIndex", block_index)
            block_index = idx
            started_indices.add(idx)
            start_obj = start.get("start", {})
            if "toolUse" in start_obj:
                tu = start_obj["toolUse"]
                yield t.ContentToolUseStart(
                    index=idx,
                    tool_use_id=tu.get("toolUseId", ""),
                    name=tu.get("name", ""),
                )
            else:
                yield t.ContentBlockStart(index=idx, block_type="text")
        elif "contentBlockDelta" in event:
            delta_block = event["contentBlockDelta"]
            idx = delta_block.get("contentBlockIndex", block_index)
            delta_obj = delta_block.get("delta", {})
            text = delta_obj.get("text", "")
            reasoning = delta_obj.get("reasoningContent")
            if text:
                if idx not in started_indices:
                    started_indices.add(idx)
                    yield t.ContentBlockStart(index=idx, block_type="text")
                yield t.ContentTextDelta(index=idx, text=text)
            elif "toolUse" in delta_obj:
                partial = delta_obj["toolUse"].get("input", "")
                if partial:
                    yield t.ContentToolUseDelta(index=idx, partial_json=partial)
            elif reasoning:
                # The block-start auto-emit must cover a delta whose FIRST leg
                # is not text: Bedrock never sends an explicit contentBlockStart
                # for a reasoning block (only for toolUse), so the first
                # reasoning delta at a not-yet-started index is what starts it
                # — exactly as the first text delta does above.
                if idx not in started_indices:
                    started_indices.add(idx)
                    yield t.ContentBlockStart(index=idx, block_type="reasoning")
                # Independent ifs, not an elif chain: a single delta can carry
                # an empty `text` and a non-empty `signature` at once, and this
                # repo already shipped the elif-chain bug that drops exactly
                # that shape.
                leg_text = reasoning.get("text")
                if leg_text:
                    yield t.ContentReasoningDelta(index=idx, kind="text", value=leg_text)
                leg_signature = reasoning.get("signature")
                if leg_signature:
                    yield t.ContentReasoningDelta(index=idx, kind="signature", value=leg_signature)
                leg_redacted = reasoning.get("redactedContent")
                if leg_redacted:
                    value = (
                        base64.b64encode(leg_redacted).decode("ascii")
                        if isinstance(leg_redacted, bytes) else leg_redacted
                    )
                    yield t.ContentReasoningDelta(index=idx, kind="redacted", value=value)
                unknown = {k for k, v in reasoning.items() if v} - REASONING_LEGS
                if unknown and not warned_unknown_reasoning_legs:
                    warned_unknown_reasoning_legs = True
                    # Event name shared with the NON-STREAMING path's
                    # equivalent set-difference check (see
                    # `unknown_reasoning_legs_in_output_block` below) — one
                    # provider-vocabulary defect, one event name, so an
                    # operator's alarm/dashboard does not have to know which
                    # transport served a given request.
                    logger.warning(
                        "unknown_reasoning_leg",
                        legs=sorted(unknown),
                    )
        elif "contentBlockStop" in event:
            idx = event["contentBlockStop"].get("contentBlockIndex", block_index)
            yield t.ContentBlockStop(index=idx)
        elif "messageStop" in event:
            yield t.MessageStop(stop_reason=event["messageStop"].get("stopReason"))
        elif "metadata" in event:
            # `usage_from_bedrock` returns `None` for an absent/empty/malformed
            # usage block; yielding nothing (rather than a Usage(0, 0)) is what
            # lets the stream end unobserved so STRATOCLAVE_UNOBSERVED_HOLDS
            # applies, instead of a billed call settling at a measured zero and
            # `UsageAccumulator.absorb` marking it `saw_final_usage=True`.
            usage_event = usage_from_bedrock(event["metadata"].get("usage"))
            if usage_event is not None:
                yield usage_event
