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
    additional_model_request_fields: Optional[dict[str, Any]] = None


@dataclass
class NormalizedResult:
    """A non-streaming Converse result the adapter renders back to its wire JSON."""

    content_blocks: list[dict[str, Any]]  # Converse output content (text + toolUse + reasoningContent)
    stop_reason: str  # Bedrock stop reason (adapter maps to its wire vocabulary)
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


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
    cache_read: int = 0
    cache_write: int = 0


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
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
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
            self.cache_read_tokens = event.cache_read or self.cache_read_tokens
            self.cache_write_tokens = event.cache_write or self.cache_write_tokens
            self.saw_final_usage = True
        elif isinstance(event, MessageStop):
            self.stop_reason = event.stop_reason
