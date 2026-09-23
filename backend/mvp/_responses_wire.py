"""The `/responses` wire: its frames, its event names, and its usage block.

Owned by neither caller. The serving route (`mvp.openai_responses`) and the discovery
probe (`mvp.discovery.probe`) both read this wire, and the probe's whole purpose is to
certify that what serving does with a response is what this gateway will bill. That
only holds if both read the SAME bytes into the SAME frames and the SAME legs, so the
reading lives here rather than in either of them.

It was in `mvp.openai_responses` first, and the probe imported its underscore names.
That put an ops component downstream of a serving route: a routine rename in the route
would have surfaced as an `ImportError` raised inside the probe's provider `try`, and
been recorded as "the provider may have billed us" for a call that never left the
process. The parser was in `mvp._converse_core`, a module named for the protocol this
is not.

Everything here is measured against the live Bedrock OpenAI-compatible endpoint
(`us.openai.gpt-5.6-sol`, through this gateway, 2026-09-24) rather than read off a
specification. Where a fact is assumed rather than measured, the comment says so.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from core.logging import get_logger

from . import _converse_types as t

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# The usage block
# ---------------------------------------------------------------------------
class ResponsesUsageShapeError(ValueError):
    """A `/responses` usage block does not match the shape this gateway measured.

    Raised rather than coerced. `_openai_transport.extract_usage`, which the Chat
    spelling still shares, answers `(0, 0)` for a missing block and 0 for a missing
    leg; that turns a counter nobody read into a measured zero on a money path, which
    is the defect `_converse_core.usage_from_bedrock` already refuses for Converse.
    This parser fails closed for the same reason, and its caller decides whether an
    unreadable block is an unobserved outcome (it is) or a zero charge (it is not).
    """


#: The complete key set of a `/responses` usage block. An unknown key REFUSES rather
#: than being ignored: a counter this gateway does not know about is a dimension the
#: provider may be billing and the rate card is not pricing, and silently dropping it
#: is how that becomes invisible.
_TOP_KEYS: frozenset[str] = frozenset({
    "input_tokens", "output_tokens", "total_tokens",
    "input_tokens_details", "output_tokens_details",
})
_INPUT_DETAIL_KEYS: frozenset[str] = frozenset({"cached_tokens", "cache_write_tokens"})
_OUTPUT_DETAIL_KEYS: frozenset[str] = frozenset({"reasoning_tokens"})


def _is_nonneg_int(value: Any) -> bool:
    """True iff `value` is an `int` (never `bool`) that is >= 0.

    `bool` is a subclass of `int`, and `"output_tokens": true` is a malformed response,
    not a token count of one. The same rule `_converse_core` applies to Converse.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _count(block: dict[str, Any], key: str, where: str) -> int:
    if key not in block:
        raise ResponsesUsageShapeError(f"{where}: missing {key!r}")
    value = block[key]
    if not _is_nonneg_int(value):
        raise ResponsesUsageShapeError(
            f"{where}.{key}: expected a non-negative int, got {value!r}")
    return value


def _details(
    usage: dict[str, Any], key: str, allowed: frozenset[str]
) -> Optional[dict[str, Any]]:
    """The named details object, validated, or `None` when it is absent.

    Absent is not malformed. Every measured response carried both details objects, but
    a model reporting no cache or reasoning breakdown at all reports less, not
    nonsense, and refusing it would make traffic this gateway meters correctly
    unbillable. A details object that IS present is validated in full, because that is
    where a counter nobody prices would hide.
    """
    if key not in usage:
        return None
    block = usage[key]
    if not isinstance(block, dict):
        raise ResponsesUsageShapeError(f"usage.{key}: expected an object, got {block!r}")
    unknown = set(block) - allowed
    if unknown:
        raise ResponsesUsageShapeError(f"usage.{key}: unknown counters {sorted(unknown)}")
    return block


def _optional_count(
    block: Optional[dict[str, Any]], key: str, where: str
) -> Optional[int]:
    """A count from an optional details object: `None` when the block or the key is
    absent, validated when present. `None` reaches `rate_usage` as "the provider did
    not report this leg", which costs the same as zero and records a different fact --
    the distinction `_converse_core.cache_tokens_from_usage` already draws.
    """
    if block is None or key not in block:
        return None
    return _count(block, key, where)


def usage_from_responses(usage: Any) -> t.Usage:
    """A `/responses` usage block as billable legs, or raise.

    This endpoint counts the cache legs INSIDE `input_tokens`, and `reasoning_tokens`
    INSIDE `output_tokens`. `mvp.pricing.rate_usage` is additive over its four legs, so
    passing the raw `input_tokens` alongside a cache count bills the cached portion
    twice -- once at the base rate and once at the cache rate. This function therefore
    returns the base leg with the cache counts SUBTRACTED, and the four legs sum to
    what the provider counted.

    Measured 2026-09-24, `us.openai.gpt-5.6-sol` through this gateway, six calls at
    `max_output_tokens=16`:

        cold ~3.5k prompt : input 3527  cached 0     written 3525  out 16  total 3543
        same prompt again : input 3527  cached 3525  written 0     out 16  total 3543
        same prompt again : input 3527  cached 3525  written 0     out 16  total 3543
        prompt + ~3k more : input 6527  cached 0     written 6525  out 16  total 6543
        that prompt again : input 6527  cached 6525  written 0     out 16  total 6543
        that + ~3.4k more : input 9927  cached 0     written 9925  out 16  total 9943

    What those six establish, and what they do not:

    * `total_tokens == input_tokens + output_tokens` in every row. This is what rules
      OUT the reading where the cache counts are additions: were they, row 1's total
      would be 3527 + 3525 + 16 = 7068, not 3543.
    * `input_tokens` does not move when the same prompt goes from being written to
      being read (rows 1 -> 2). The cache counts relabel part of the input.
    * `cached + written == input_tokens - 2` in every cache-touching row.
    * The two cache legs were NEVER both nonzero. Extending a cached prompt reported a
      WHOLE-prompt write (row 6 wrote 9925 with 6525 already cached by row 5) rather
      than a partial read plus a write, so no partial-hit shape was observed at all.

    Both legs nonzero is therefore UNOBSERVED, not impossible. It is settled under the
    subset reading above and logged, rather than refused: the subtraction stays correct
    if the two counts partition the input, and refusing a shape nobody has seen would
    stop settlement on traffic this gateway can meter correctly the day the provider
    starts reporting partial hits. The one genuinely dangerous case -- counts that
    OVERLAP, which would drive the base leg negative -- is refused below. Re-measure if
    the log line ever fires.
    """
    if not isinstance(usage, dict):
        raise ResponsesUsageShapeError(f"usage: expected an object, got {usage!r}")
    unknown = set(usage) - _TOP_KEYS
    if unknown:
        raise ResponsesUsageShapeError(f"usage: unknown keys {sorted(unknown)}")

    input_tokens = _count(usage, "input_tokens", "usage")
    output_tokens = _count(usage, "output_tokens", "usage")
    # Optional, and then required conditionally below: see the cache-count rule.
    total_tokens = (
        _count(usage, "total_tokens", "usage") if "total_tokens" in usage else None)
    input_details = _details(usage, "input_tokens_details", _INPUT_DETAIL_KEYS)
    output_details = _details(usage, "output_tokens_details", _OUTPUT_DETAIL_KEYS)
    cached = _optional_count(input_details, "cached_tokens", "usage.input_tokens_details")
    written = _optional_count(
        input_details, "cache_write_tokens", "usage.input_tokens_details")
    reasoning = _optional_count(
        output_details, "reasoning_tokens", "usage.output_tokens_details")

    # `total_tokens` is the only evidence for the subset reading, and the subtraction
    # below is sound only under it. So it is optional exactly while there is nothing to
    # subtract: a block reporting a cache count without a total would otherwise have
    # thousands of base tokens silently removed on the strength of an assumption this
    # response did not support. The overlap check does not catch that -- it only
    # catches counts that exceed the input.
    if (cached or written) and total_tokens is None:
        raise ResponsesUsageShapeError(
            f"cache counts are reported (cached={cached}, written={written}) but "
            f"total_tokens is absent, so the subset reading this parser would subtract "
            f"under is unverifiable for this response"
        )
    if total_tokens is not None and total_tokens != input_tokens + output_tokens:
        raise ResponsesUsageShapeError(
            f"total_tokens {total_tokens} != input {input_tokens} + output "
            f"{output_tokens}; the subset reading this parser bills under does not hold "
            f"for this response, so a counter lives somewhere it was not read"
        )
    if reasoning is not None and reasoning > output_tokens:
        raise ResponsesUsageShapeError(
            f"usage.output_tokens_details.reasoning_tokens {reasoning} > output_tokens "
            f"{output_tokens}; reasoning is a subset of output, never an addition"
        )
    if (cached or 0) + (written or 0) > input_tokens:
        raise ResponsesUsageShapeError(
            f"cache counts {cached} + {written} exceed input_tokens {input_tokens}; the "
            f"cache legs overlap or are not subsets, and subtracting them would bill a "
            f"negative base leg"
        )
    if cached and written:
        logger.warning(
            "responses_usage_cache_read_and_write_both_reported",
            extra={"cached_tokens": cached, "cache_write_tokens": written,
                   "input_tokens": input_tokens},
        )

    return t.Usage(
        input=input_tokens - (cached or 0) - (written or 0),
        # Reasoning tokens are already inside this count; adding them would bill the
        # same tokens twice at the output rate.
        output=output_tokens,
        cache_read=cached,
        cache_write=written,
    )


# ---------------------------------------------------------------------------
# The frames
# ---------------------------------------------------------------------------
#: Per the SSE spec a frame with no `event:` line has the event type `"message"`, so
#: that name carries no information about WHICH response event arrived.
SSE_DEFAULT_EVENT = "message"

#: The terminal events that carry a usage block. `response.incomplete` is here because
#: it is what a stream that hits `max_output_tokens` actually ends with -- measured: a
#: ~3.5k-token prompt at `max_output_tokens=16` returned `status: "incomplete"`,
#: `incomplete_details: {"reason": "max_output_tokens"}` and a full usage block with
#: `reasoning_tokens == output_tokens == 16`. Reading usage only from
#: `response.completed` therefore leaves every truncated stream unmetered.
METERED_TERMINAL_TYPES: frozenset[str] = frozenset({
    "response.completed", "response.incomplete",
})


class SSEFrameConflict(ValueError):
    """An SSE frame's `event:` line and its payload `"type"` disagree."""


def drain_events(buffer: bytearray) -> list[bytes]:
    """Pop every fully-terminated SSE event from `buffer` (in place).

    SSE event boundaries are blank lines: either `\\n\\n` or `\\r\\n\\r\\n` per the
    spec. We search for whichever appears first and slice up to and including it. Bytes
    that do not yet contain a terminator stay in the buffer for the next chunk.

    Shared rather than reimplemented per caller: two framers reading the same bytes into
    different frames make a probe verdict certify a cut the serving route does not make,
    and a splitter that only looks for `\\n\\n` never finds a boundary in a CRLF stream
    at all -- the whole body accumulates and then parses as one frame with every
    `data:` line joined.
    """
    events: list[bytes] = []
    while True:
        # Find the earliest event terminator. `find` returns -1 if absent.
        crlf = buffer.find(b"\r\n\r\n")
        lf = buffer.find(b"\n\n")
        if crlf == -1 and lf == -1:
            break
        if crlf == -1:
            cut = lf + 2
        elif lf == -1:
            cut = crlf + 4
        else:
            # Take the boundary that ends earliest in the buffer.
            cut = min(lf + 2, crlf + 4)
        events.append(bytes(buffer[:cut]))
        del buffer[:cut]
    return events


def parse_sse_frame(text: str) -> tuple[Optional[str], Optional[str]]:
    """Return `(event_name, joined_data)` from an SSE event text.

    Per the SSE spec, multiple `data:` lines in the same event are joined with `"\\n"`
    before delivery to the client's parser. We follow that rule so a multi-line
    `response.completed` payload still JSON-decodes correctly. Lines starting with `:`
    are SSE comments and are ignored.
    """
    event_name: Optional[str] = None
    data_lines: list[str] = []
    # SSE accepts \n, \r\n, and \r as line endings. splitlines handles all.
    for raw_line in text.splitlines():
        if not raw_line or raw_line.startswith(":"):
            continue
        if raw_line.startswith("event:"):
            event_name = raw_line[len("event:"):].strip()
            continue
        if raw_line.startswith("data:"):
            # Strip exactly one leading space if present (per SSE spec "field: value" --
            # value is the bytes after the optional single space).
            value = raw_line[len("data:"):]
            if value.startswith(" "):
                value = value[1:]
            data_lines.append(value)
    if not data_lines:
        return event_name, None
    return event_name, "\n".join(data_lines)


def sse_event_type(event_name: Optional[str], payload: Any) -> Optional[str]:
    """The effective response-event type of one SSE frame.

    Measured 2026-09-24: this endpoint sends NO `event:` lines at all -- a 4,431-byte
    streamed response carried ten `data:` lines and zero `event:` lines, with the event
    name only inside the JSON as `"type"`. Keying on the `event:` line alone is why the
    serving route read usage off no streamed response ever: the ledger for three
    consecutive calls showed `input 0 / output 0` for the two streamed ones beside
    `7 / 5` for the non-streamed one.

    Either source alone is accepted, so a future upstream that DOES send `event:` lines
    keeps working; when both are present they must agree, because a frame that says two
    different things about what it is cannot be metered on a guess.
    """
    line_type = (
        event_name if event_name and event_name != SSE_DEFAULT_EVENT else None)
    body_type = None
    if isinstance(payload, dict) and "type" in payload:
        candidate = payload["type"]
        if isinstance(candidate, str) and candidate:
            body_type = candidate
    if line_type and body_type and line_type != body_type:
        raise SSEFrameConflict(
            f"SSE frame event line {line_type!r} disagrees with payload type "
            f"{body_type!r}")
    return body_type or line_type


@dataclass(frozen=True)
class TerminalFrame:
    """What one SSE frame turned out to be.

    `usage` is set only for a metered terminal whose usage block parsed. Everything
    else -- a non-terminal frame, a frame that contradicts itself, a terminal whose
    counters will not parse -- leaves it `None`, and `None` must never become a zero
    charge. `response_id` is captured ONLY from a metered terminal, so a provider-state
    lock is armed only when a real, referenceable continuation was produced.
    """

    event_type: Optional[str]
    payload: Optional[dict[str, Any]]
    usage: Optional[t.Usage] = None
    response_id: Optional[str] = None


def terminal_usage_from_frame(frame: bytes) -> TerminalFrame:
    """The one terminal-event detector. Both readers of this wire call it.

    Resolving an event type is not detecting a terminal: detection is the whole
    composition -- decode, JSON-parse, resolve the type from either source, handle a
    frame that contradicts itself, test membership of the metered set, find the
    `response` object, parse its usage. Each caller having its own copy of that
    composition is how the two came to differ on which failures were logged and which
    were swallowed, which is the drift a shared detector exists to prevent.
    """
    import json

    try:
        text = frame.decode("utf-8")
    except UnicodeDecodeError:
        # This endpoint's stream is documented UTF-8. A frame that violates it is
        # forwarded verbatim by the route and simply teaches a probe nothing.
        return TerminalFrame(event_type=None, payload=None)

    event_name, data_payload = parse_sse_frame(text)
    payload: Optional[dict[str, Any]] = None
    if data_payload is not None:
        try:
            decoded = json.loads(data_payload)
        except (ValueError, TypeError):
            decoded = None
        if isinstance(decoded, dict):
            payload = decoded

    try:
        event_type = sse_event_type(event_name, payload)
    except SSEFrameConflict as conflict:
        logger.error("sse_frame_type_conflict", extra={"error": str(conflict)})
        return TerminalFrame(event_type=None, payload=payload)

    if event_type not in METERED_TERMINAL_TYPES or payload is None:
        return TerminalFrame(event_type=event_type, payload=payload)

    response_block = payload.get("response")
    if not isinstance(response_block, dict):
        logger.error(
            "responses_terminal_frame_missing_response_object",
            extra={"event_type": event_type})
        return TerminalFrame(event_type=event_type, payload=payload)

    response_id = response_block.get("id")
    try:
        usage = usage_from_responses(response_block.get("usage"))
    except ResponsesUsageShapeError as shape_error:
        logger.error(
            "responses_terminal_usage_unreadable",
            extra={"event_type": event_type, "error": str(shape_error)})
        return TerminalFrame(
            event_type=event_type, payload=payload,
            response_id=response_id if isinstance(response_id, str) else None,
        )
    return TerminalFrame(
        event_type=event_type, payload=payload, usage=usage,
        response_id=response_id if isinstance(response_id, str) else None,
    )
