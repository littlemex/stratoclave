"""Unit tests for `mvp.anthropic._survey_and_hash_converse_kwargs` —
CONTRACT-hard-ceiling.md section 3a's canonical-payload byte survey and hash.

Pure-function tests, no AWS: `_build_bedrock_kwargs` and
`_survey_and_hash_converse_kwargs` take/produce plain dicts.
"""
from __future__ import annotations

import base64
import struct
import zlib

from mvp.anthropic import AnthropicMessagesRequest, _build_bedrock_kwargs, _survey_and_hash_converse_kwargs


def _png_b64(width: int, height: int) -> str:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(
            ">I", zlib.crc32(tag + data)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00" + b"\x00" * (width * 3))
    raw = sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
    return base64.b64encode(raw).decode()


def _kwargs_for(body_kwargs: dict, model_id: str = "anthropic.claude-3-5-sonnet-20241022-v2:0") -> dict:
    body = AnthropicMessagesRequest(**body_kwargs)
    return _build_bedrock_kwargs(body, model_id)


def test_survey_counts_text_bytes_across_messages_and_system():
    kwargs = _kwargs_for({
        "model": "claude-3-5-sonnet",
        "max_tokens": 100,
        "system": "be nice",
        "messages": [{"role": "user", "content": "hello world"}],
    })
    survey, nbytes, digest = _survey_and_hash_converse_kwargs(kwargs)
    assert survey.text_bytes == len(b"be nice") + 1 + len(b"hello world")
    assert nbytes == survey.text_bytes
    assert len(digest) == 64  # sha256 hex digest


def test_survey_excludes_image_bytes_from_length_but_not_from_hash():
    png = _png_b64(750, 750)  # 750*750/750 = 750 tokens exactly
    kwargs = _kwargs_for({
        "model": "claude-3-5-sonnet",
        "max_tokens": 100,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "look at this"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png}},
            ],
        }],
    })
    survey, nbytes, digest_with_image = _survey_and_hash_converse_kwargs(kwargs)
    assert survey.text_bytes == len(b"look at this")  # image bytes NOT counted
    assert survey.image_dims == ((750, 750),)
    assert survey.unmeasurable_images == 0

    # Swap the image for a DIFFERENT one of the SAME text length contribution
    # (i.e. still zero bytes added to text_bytes) but different pixel bytes:
    # the HASH must differ even though the byte LENGTH is identical, because
    # the hash covers the whole payload including image bytes (contract
    # section 3a: "a retry that swapped image bytes while keeping the length
    # must not pass the pin").
    other_png = _png_b64(750, 751)
    kwargs2 = _kwargs_for({
        "model": "claude-3-5-sonnet",
        "max_tokens": 100,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "look at this"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": other_png}},
            ],
        }],
    })
    survey2, nbytes2, digest_other_image = _survey_and_hash_converse_kwargs(kwargs2)
    assert survey2.text_bytes == survey.text_bytes
    assert nbytes2 == nbytes
    assert digest_other_image != digest_with_image


def test_survey_refuses_unmeasurable_image_and_flags_it():
    kwargs = {
        "system": None,
        "messages": [{
            "role": "user",
            "content": [{"image": {"source": {"bytes": b"not a real image"}}}],
        }],
    }
    survey, _, _ = _survey_and_hash_converse_kwargs(kwargs)
    assert survey.unmeasurable_images == 1
    assert survey.image_dims == ()


def test_survey_counts_tool_config_schema_bytes():
    """Section 4 correction: client tool schemas travel in the payload the
    gateway sends, so the byte term must cover them — a request with tools
    must survey to MORE bytes than the identical request without tools."""
    base = {
        "model": "claude-3-5-sonnet",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "what's the weather"}],
    }
    kwargs_no_tools = _kwargs_for(base)
    kwargs_with_tools = _kwargs_for({
        **base,
        "tools": [{
            "name": "get_weather",
            "description": "Get the current weather for a location",
            "input_schema": {
                "type": "object",
                "properties": {"location": {"type": "string"}},
                "required": ["location"],
            },
        }],
    })
    survey_no_tools, _, _ = _survey_and_hash_converse_kwargs(kwargs_no_tools)
    survey_with_tools, _, _ = _survey_and_hash_converse_kwargs(kwargs_with_tools)
    assert survey_with_tools.text_bytes > survey_no_tools.text_bytes


def test_survey_counts_multiturn_tool_use_and_reasoning_history_as_input():
    """Regression for a real bug this change introduced and then fixed: a
    `toolUse`/`reasoningContent` block echoed back in message history is
    genuine INPUT for the request that re-sends it (each HTTP request
    reserves/settles independently), not output already bounded by a prior
    turn. Skipping it would under-count this request's own bound."""
    base = {
        "model": "claude-3-5-sonnet",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "what's the weather in Tokyo"}],
    }
    with_history = {
        "model": "claude-3-5-sonnet",
        "max_tokens": 100,
        "messages": [
            {"role": "user", "content": "what's the weather in Tokyo"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "get_weather",
                 "input": {"location": "Tokyo, Japan, a reasonably long location string"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "22C, sunny"},
            ]},
        ],
    }
    survey_base, _, _ = _survey_and_hash_converse_kwargs(_kwargs_for(base))
    survey_hist, _, _ = _survey_and_hash_converse_kwargs(_kwargs_for(with_history))
    assert survey_hist.text_bytes > survey_base.text_bytes


def test_survey_cache_point_marker_contributes_no_bytes():
    kwargs_plain = _kwargs_for({
        "model": "claude-3-5-sonnet",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
    })
    kwargs_cached = _kwargs_for({
        "model": "claude-3-5-sonnet",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}},
        ]}],
    })
    survey_plain, _, _ = _survey_and_hash_converse_kwargs(kwargs_plain)
    survey_cached, _, _ = _survey_and_hash_converse_kwargs(kwargs_cached)
    assert survey_plain.text_bytes == survey_cached.text_bytes
