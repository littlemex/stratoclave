"""Wire-agnostic normalized form for the Bedrock Converse control core.

The route adapters (`_wire/*.py`) translate their client-facing request/response
shapes to and from these types; the control core (`_converse_core.py`) and the
budget flow (`_budget_flow.py`) speak ONLY these. Keeping the normalized form a
faithful projection of the Bedrock Converse API means the core needs zero
per-wire branching.

Nothing here touches DynamoDB, boto3, or budget math — these are plain data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class NormalizedRequest:
    """A request reduced to Converse-shaped inputs plus what reservation needs.

    `messages`/`system`/`tool_config` are already in Converse content-block shape
    so the core can hand them to `converse`/`converse_stream` unchanged. The
    reservation math reads `max_output_tokens` and `input_text_chars` (the
    adapter counts the chars — including serialized tool schemas, tool results,
    and image byte-length — so tool traffic is not systematically under-reserved).
    """

    model_alias: str  # client-facing model name (for pricing + echo)
    bedrock_model_id: str  # resolved inference-profile id
    messages: list[dict[str, Any]]  # Converse content blocks (text/image/toolUse/toolResult)
    system: Optional[list[dict[str, Any]]]  # Converse system blocks
    inference_config: dict[str, Any]  # maxTokens/temperature/topP/stopSequences
    max_output_tokens: int  # for reservation math
    input_text_chars: int  # for reservation estimate (adapter-counted)
    stream: bool
    tool_config: Optional[dict[str, Any]] = None  # Converse toolConfig {tools, toolChoice}
    # Converse additionalModelRequestFields: thinking {type, budget_tokens},
    # top_k, anthropic_beta, etc. Without this the core would silently drop
    # thinking/top_k — the same silent-drop class as the tools bug being fixed.
    # NOTE: this dataclass is not on the live request path today (the two
    # Converse-shaped routes build `kwargs` dicts directly — `mvp.anthropic.
    # _build_bedrock_kwargs` / `mvp.chat_completions._build_chat_bedrock_kwargs`
    # — rather than constructing a `NormalizedRequest`); this field is a
    # forward-looking receptacle, not itself the fix. The actual wiring for
    # `additionalModelRequestFields` is `additional_model_request_fields()`
    # below, called directly from both `_build_*_kwargs` functions.
    additional_model_request_fields: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# additionalModelRequestFields passthrough — shared by both Converse-shaped
# routes (`mvp.anthropic._build_bedrock_kwargs` and `mvp.chat_completions.
# _build_chat_bedrock_kwargs`), declared once so `/v1/messages` and
# `/v1/chat/completions` cannot drift on what this channel carries.
# ---------------------------------------------------------------------------
# Bedrock Converse validates `additionalModelRequestFields` against the
# TARGET MODEL's own request schema, not this gateway's — a key the model
# does not recognise comes back as an upstream `ValidationException` the
# caller cannot act on. That is why this is an explicit ALLOWLIST rather than
# "every unrecognised top-level field forwarded verbatim": forwarding every
# extra would turn this gateway's own forward-compatible `extra="allow"` (a
# new Anthropic/OpenAI field must not 422 the request) into a 502 the moment
# a caller sent a field the model does not also recognise under this name —
# the gateway would be converting its own forward-compatibility into an
# upstream error the caller cannot act on, exactly what `extra="allow"`
# exists to avoid.
#
# Starting set: `thinking` is the one verified missing on real Bedrock
# traffic — a `thinking`-enabled request came back with a single `text`
# block, `stop_reason: end_turn`, and no error, because nothing in either
# route ever built `additionalModelRequestFields` at all. `top_k` and
# `anthropic_beta` are the two other native Anthropic-on-Converse controls
# with no field in `inferenceConfig`/`toolConfig` to travel on instead.
ADDITIONAL_MODEL_REQUEST_FIELD_KEYS: tuple[str, ...] = (
    "thinking", "top_k", "anthropic_beta",
)

#: Keys whose EFFECT is output the caller must be able to read back. Forwarding one
#: to a wire that cannot render the result bills the caller for tokens it never
#: receives, which is worse than not honouring the parameter at all -- the same
#: reasoning as C13.1, one step further on: a parameter this gateway cannot honour
#: END TO END is not honoured half way.
#:
#: `thinking` is here because reasoning is rendered on the OpenAI-shaped route
#: (`reasoning_content`) and NOT on the Anthropic Messages route, whose response
#: builder emits `text` and `tool_use` blocks only. Honouring it there would mean a
#: caller pays for thinking tokens and, when the output budget goes entirely to
#: thinking, receives an empty reply with a full bill. `top_k` and `anthropic_beta`
#: are not listed: neither produces output blocks, so neither can go unrendered.
#:
#: Removing a key from this set is how the Anthropic wire earns `thinking`, once it
#: renders reasoning back as that API's own `thinking` block type.
RENDERED_ONLY_FIELD_KEYS: frozenset[str] = frozenset({"thinking"})


def additional_model_request_fields(
    body: Any, *, renders_reasoning: bool = True
) -> Optional[dict[str, Any]]:
    """The `additionalModelRequestFields` dict for a Converse `kwargs` build, or
    `None`.

    `body` is a pydantic request model with `extra="allow"`
    (`AnthropicMessagesRequest` / `ChatCompletionsRequest`), so an allowlisted
    key the caller sent is reachable by plain `getattr` regardless of whether
    the request model DECLARES the field (`AnthropicMessagesRequest.top_k`,
    validated `ge=1, le=500`) or it only arrives as an extra (`thinking`,
    `anthropic_beta`, and `top_k` on the OpenAI-shaped route, which declares
    none of the three): pydantic v2's `extra="allow"` sets extra fields as
    real attributes on the instance, not only in `model_extra`.

    Returns `None`, never `{}`, when no allowlisted key is present, so a
    request that sends none of them produces `kwargs` byte-identical to one
    built before this function existed.

    `renders_reasoning=False` drops the keys in `RENDERED_ONLY_FIELD_KEYS`, for a
    transport whose response builder cannot return what they produce.
    """
    out: dict[str, Any] = {}
    for key in ADDITIONAL_MODEL_REQUEST_FIELD_KEYS:
        if key in RENDERED_ONLY_FIELD_KEYS and not renders_reasoning:
            continue
        value = getattr(body, key, None)
        if value is not None:
            out[key] = value
    return out or None


@dataclass
class NormalizedResult:
    """A non-streaming Converse result the adapter renders back to its wire JSON."""

    content_blocks: list[dict[str, Any]]  # Converse output content (text + toolUse + reasoningContent)
    stop_reason: str  # Bedrock stop reason (adapter maps to its wire vocabulary)
    input_tokens: int
    output_tokens: int
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None


# ---------------------------------------------------------------------------
# StreamEvent tagged union.
# ---------------------------------------------------------------------------
# The core yields these; each adapter renders each event to its own wire SSE.
# The union MUST cover every Bedrock converse_stream event shape or a silent
# drop is recreated. Anthropic SSE is block-oriented (message_start /
# content_block_start / content_block_delta / content_block_stop /
# message_delta / message_stop); OpenAI Chat SSE is chunk-oriented
# (chat.completion.chunk with choices[].delta, tool_calls streamed as
# function.arguments fragments, terminated by data: [DONE]). Both derive from
# the same StreamEvent sequence.


@dataclass(frozen=True)
class StreamEvent:
    """Base for the tagged union. Instances are one of the subclasses below."""


@dataclass(frozen=True)
class MessageStart(StreamEvent):
    """The response has begun; no content yet."""


@dataclass(frozen=True)
class ContentBlockStart(StreamEvent):
    """A content block at `index` began. Emitted for EVERY block including text.

    Bedrock sends `contentBlockStart` only for toolUse; for text it sends a bare
    `contentBlockDelta` at a new index. The core synthesizes this on the first
    delta of a not-yet-started index so adapters never infer block boundaries.
    """

    index: int
    block_type: str  # "text" | "tool_use" | "reasoning"


@dataclass(frozen=True)
class ContentTextDelta(StreamEvent):
    index: int
    text: str


@dataclass(frozen=True)
class ContentToolUseStart(StreamEvent):
    """A toolUse block began (from Bedrock contentBlockStart.start.toolUse)."""

    index: int
    tool_use_id: str
    name: str


@dataclass(frozen=True)
class ContentToolUseDelta(StreamEvent):
    """A fragment of a toolUse block's JSON input (Bedrock delta.toolUse.input)."""

    index: int
    partial_json: str


@dataclass(frozen=True)
class ContentReasoningDelta(StreamEvent):
    """A fragment of a reasoning block (Bedrock delta.reasoningContent).

    `kind` selects which field: "text" (visible thinking), "signature" (the
    opaque signature that MUST round-trip for multi-turn thinking+tools), or
    "redacted" (redactedContent bytes, base64). Dropping any of these breaks
    thinking — the same silent-drop class as the tools bug.
    """

    index: int
    kind: str  # "text" | "signature" | "redacted"
    value: str


@dataclass(frozen=True)
class ContentBlockStop(StreamEvent):
    index: int


@dataclass(frozen=True)
class MessageStop(StreamEvent):
    """The turn ended. `stop_sequence` is the matched string when applicable;
    Converse does not return it, so it is always None here (noted, not dropped).
    """

    stop_reason: str
    stop_sequence: Optional[str] = None


@dataclass(frozen=True)
class Usage(StreamEvent):
    """Token accounting (Bedrock `metadata.usage`). Emitted before MessageStop
    in what the adapter sees, so adapters can render usage on the stop frame.
    """

    input: int
    output: int
    # `None` = the provider did not report this leg (distinct from a reported zero;
    # see `_converse_core.cache_tokens_from_usage`).
    cache_read: Optional[int] = None
    cache_write: Optional[int] = None


@dataclass(frozen=True)
class Error(StreamEvent):
    """A sanitized error the adapter renders as its wire-shaped error frame."""

    message: str


@dataclass
class UsageAccumulator:
    """Mutable running total the core fills while streaming; settle reads it.

    Tracks the last-seen stop reason and the token counts so a disconnect at any
    point settles against whatever usage was observed (zero is fine).
    """

    input_tokens: int = 0
    output_tokens: int = 0
    # Start as "not reported": a stream that never carries a prompt-cache count must
    # not settle as though the provider had reported zero.
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    stop_reason: Optional[str] = None
    # P0-14: True once a terminal Usage event has landed. Bedrock emits usage
    # exactly once (metadata, ahead of MessageStop), so any Usage marks the
    # totals final; usage_is_partial = not saw_final_usage on disconnect.
    saw_final_usage: bool = False

    def absorb(self, event: StreamEvent) -> None:
        if isinstance(event, Usage):
            # Bedrock reports the running totals, not increments; take the latest.
            self.input_tokens = event.input or self.input_tokens
            self.output_tokens = event.output or self.output_tokens
            # `or` would treat a reported 0 as "keep the previous value"; an
            # explicit report — including zero — is what the provider said.
            if event.cache_read is not None:
                self.cache_read_tokens = event.cache_read
            if event.cache_write is not None:
                self.cache_write_tokens = event.cache_write
            self.saw_final_usage = True
        elif isinstance(event, MessageStop):
            self.stop_reason = event.stop_reason
