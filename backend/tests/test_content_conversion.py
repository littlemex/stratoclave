"""Red tests pinning the root-cause bugs in _convert_content_blocks and
_build_bedrock_kwargs (mvp/anthropic.py).

Each test documents a specific translation contract that the current code
violates. All tests should FAIL until the bugs are fixed.

Bugs tested:
  1. Image blocks are silently dropped (BUG-IMG)
  2. tool_use blocks are JSON-stringified instead of translated to toolUse (BUG-TOOL-USE)
  3. tool_result blocks are JSON-stringified instead of translated to toolResult (BUG-TOOL-RESULT)
  4. _build_bedrock_kwargs never forwards tools/tool_choice as toolConfig (BUG-TOOL-CONFIG)
  5. thinking blocks are JSON-stringified instead of translated to reasoningContent (BUG-THINKING)
  6. cache_control on a content block does not append a cachePoint entry (BUG-CACHE)
"""
from __future__ import annotations

import base64

import pytest

from mvp.anthropic import (
    AnthropicMessagesRequest,
    _build_bedrock_kwargs,
    _convert_content_blocks,
)


# ---------------------------------------------------------------------------
# BUG-IMG: image blocks are silently dropped
# ---------------------------------------------------------------------------

def test_image_block_is_translated_not_dropped():
    """An Anthropic base64 image block must produce a Bedrock image dict.

    Current code hits `continue` at the image branch and silently omits
    the block, so the output is shorter than expected.
    """
    raw_png = b"\x89PNG\r\n"
    b64_data = base64.b64encode(raw_png).decode("ascii")

    content = [
        {"type": "text", "text": "describe this image"},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": b64_data,
            },
        },
    ]

    result = _convert_content_blocks(content)

    # Must have exactly two entries: one text, one image
    assert len(result) == 2, (
        f"Expected 2 blocks (text + image), got {len(result)}: {result}"
    )

    text_blocks = [b for b in result if "text" in b]
    image_blocks = [b for b in result if "image" in b]

    assert len(text_blocks) == 1, f"Expected 1 text block, got: {text_blocks}"
    assert len(image_blocks) == 1, f"Expected 1 image block, got: {image_blocks}"

    img = image_blocks[0]["image"]
    assert img["format"] == "png", f"Expected format 'png', got: {img.get('format')}"
    assert img["source"]["bytes"] == raw_png, (
        "Decoded bytes must match original PNG bytes"
    )


# ---------------------------------------------------------------------------
# BUG-TOOL-USE: tool_use blocks are JSON-stringified instead of using toolUse
# ---------------------------------------------------------------------------

def test_tool_use_block_produces_tool_use_shape():
    """An Anthropic tool_use block must translate to Bedrock toolUse shape.

    Current code falls into the `else` branch and emits
    {"text": json.dumps(block)}, losing all structure.
    """
    content = [
        {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "get_weather",
            "input": {"city": "Tokyo"},
        }
    ]

    result = _convert_content_blocks(content)

    assert len(result) == 1, f"Expected 1 block, got {len(result)}: {result}"

    block = result[0]
    assert "toolUse" in block, (
        f"Expected 'toolUse' key in block, got keys: {list(block.keys())}"
    )
    assert "text" not in block, (
        "Block must NOT be text-stringified — got raw text instead of toolUse"
    )

    tool_use = block["toolUse"]
    assert tool_use["toolUseId"] == "toolu_1"
    assert tool_use["name"] == "get_weather"
    assert tool_use["input"] == {"city": "Tokyo"}


# ---------------------------------------------------------------------------
# BUG-TOOL-RESULT: tool_result blocks are JSON-stringified instead of toolResult
# ---------------------------------------------------------------------------

def test_tool_result_block_produces_tool_result_shape():
    """An Anthropic tool_result block must translate to Bedrock toolResult shape.

    Current code falls into the `else` branch and emits
    {"text": json.dumps(block)}.
    """
    content = [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_1",
            "content": [{"type": "text", "text": "sunny"}],
        }
    ]

    result = _convert_content_blocks(content)

    assert len(result) == 1, f"Expected 1 block, got {len(result)}: {result}"

    block = result[0]
    assert "toolResult" in block, (
        f"Expected 'toolResult' key in block, got keys: {list(block.keys())}"
    )
    assert "text" not in block, (
        "Block must NOT be text-stringified — got raw text instead of toolResult"
    )

    tool_result = block["toolResult"]
    assert tool_result["toolUseId"] == "toolu_1"
    assert tool_result["content"] == [{"text": "sunny"}]


# ---------------------------------------------------------------------------
# BUG-TOOL-CONFIG: _build_bedrock_kwargs does not forward tools/tool_choice
# ---------------------------------------------------------------------------

def test_build_bedrock_kwargs_forwards_tools_as_tool_config():
    """When the request body includes tools, kwargs must contain toolConfig.

    Current _build_bedrock_kwargs reads only the known fields and ignores
    `tools` / `tool_choice` entirely, so toolConfig is never set.
    """
    body = AnthropicMessagesRequest.model_validate(
        {
            "model": "claude-opus-4-5",
            "messages": [{"role": "user", "content": "What is the weather in Tokyo?"}],
            "max_tokens": 1024,
            "tools": [
                {
                    "name": "get_weather",
                    "description": "Get the current weather for a city",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "city": {"type": "string", "description": "City name"}
                        },
                        "required": ["city"],
                    },
                }
            ],
            "tool_choice": {"type": "auto"},
        }
    )

    # Use a dummy model_id that won't trigger any real Bedrock calls
    kwargs = _build_bedrock_kwargs(body, "us.anthropic.claude-opus-4-7-20250514-v1:0")

    assert "toolConfig" in kwargs, (
        f"Expected 'toolConfig' in kwargs but got keys: {list(kwargs.keys())}"
    )

    tool_config = kwargs["toolConfig"]
    assert "tools" in tool_config, (
        f"Expected 'tools' inside toolConfig, got: {tool_config}"
    )

    tools_list = tool_config["tools"]
    assert len(tools_list) == 1, f"Expected 1 tool, got {len(tools_list)}"

    tool_spec = tools_list[0].get("toolSpec", {})
    assert tool_spec.get("name") == "get_weather"


# ---------------------------------------------------------------------------
# BUG-THINKING: thinking blocks are JSON-stringified instead of reasoningContent
# ---------------------------------------------------------------------------

def test_thinking_block_produces_reasoning_content_shape():
    """An Anthropic thinking block must translate to Bedrock reasoningContent.

    Current code falls into the `else` branch and emits
    {"text": json.dumps(block)}, losing the structured reasoning.
    """
    content = [
        {
            "type": "thinking",
            "thinking": "Let me reason through this step by step...",
            "signature": "sig_abc123",
        }
    ]

    result = _convert_content_blocks(content)

    assert len(result) == 1, f"Expected 1 block, got {len(result)}: {result}"

    block = result[0]
    assert "reasoningContent" in block, (
        f"Expected 'reasoningContent' key in block, got keys: {list(block.keys())}"
    )
    assert "text" not in block, (
        "Block must NOT be text-stringified — got raw text instead of reasoningContent"
    )

    reasoning = block["reasoningContent"]
    assert "reasoningText" in reasoning, (
        f"Expected 'reasoningText' inside reasoningContent, got: {reasoning}"
    )
    reasoning_text = reasoning["reasoningText"]
    assert reasoning_text["text"] == "Let me reason through this step by step..."
    assert reasoning_text["signature"] == "sig_abc123"


# ---------------------------------------------------------------------------
# BUG-CACHE: cache_control on a content block does not append a cachePoint
# ---------------------------------------------------------------------------

def test_cache_control_appends_cache_point_block():
    """A text block with cache_control must be followed by a cachePoint entry.

    Current code completely ignores cache_control, so no cachePoint is
    appended and prompt caching is silently disabled.
    """
    content = [
        {
            "type": "text",
            "text": "You are a helpful assistant.",
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": "Now answer my question.",
        },
    ]

    result = _convert_content_blocks(content)

    # We expect at least 3 entries: text, cachePoint, text
    assert len(result) >= 3, (
        f"Expected at least 3 blocks (text + cachePoint + text), got {len(result)}: {result}"
    )

    # The cachePoint must appear right after the first text block (index 1)
    cache_point_blocks = [b for b in result if "cachePoint" in b]
    assert len(cache_point_blocks) >= 1, (
        f"Expected at least one cachePoint block, got none. Result: {result}"
    )

    # Verify position: cachePoint immediately follows the first text block
    first_text_idx = next(i for i, b in enumerate(result) if "text" in b)
    cache_point_idx = next(i for i, b in enumerate(result) if "cachePoint" in b)
    assert cache_point_idx == first_text_idx + 1, (
        f"cachePoint (idx {cache_point_idx}) must immediately follow the "
        f"first text block (idx {first_text_idx})"
    )

    # Verify the cachePoint shape
    cache_point = cache_point_blocks[0]["cachePoint"]
    assert cache_point.get("type") == "default", (
        f"Expected cachePoint type 'default', got: {cache_point}"
    )
