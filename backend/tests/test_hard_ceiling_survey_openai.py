"""Unit tests for the OpenAI-shape canonical-payload surveyors in
`mvp.reservation_bound` — `survey_and_hash_openai_chat_payload` (the
`/v1/chat/completions` mantle leg) and `survey_and_hash_openai_responses_payload`
(`/openai/v1/responses`). Pure-function tests, no AWS.
"""
from __future__ import annotations

import base64
import struct
import zlib

from mvp.reservation_bound import (
    survey_and_hash_openai_chat_payload,
    survey_and_hash_openai_responses_payload,
)


def _png_data_uri(width: int, height: int) -> str:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(
            ">I", zlib.crc32(tag + data)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00" + b"\x00" * (width * 3))
    raw = sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(raw).decode()


# ---------------------------------------------------------------------------
# Chat Completions (mantle) payload shape
# ---------------------------------------------------------------------------


def test_chat_payload_counts_text_across_messages():
    payload = {
        "messages": [
            {"role": "system", "content": "be nice"},
            {"role": "user", "content": "hello world"},
        ],
    }
    survey, nbytes, digest = survey_and_hash_openai_chat_payload(payload)
    assert survey.text_bytes == len(b"be nice") + 1 + len(b"hello world")
    # The returned byte count is the SERIALISED payload, not `survey.text_bytes`.
    # A measured request settled above its own bound because the text-only count
    # omitted the chat template the provider bills for, so the two numbers are
    # deliberately different now: the survey still reports the content it saw, and
    # the bound is priced from the envelope that contains it.
    assert nbytes > survey.text_bytes
    assert len(digest) == 64


def test_chat_payload_handles_content_part_lists():
    payload = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hi there"}]},
        ],
    }
    survey, _, _ = survey_and_hash_openai_chat_payload(payload)
    assert survey.text_bytes == len(b"hi there")


def test_chat_payload_bounds_inline_data_uri_image_and_excludes_its_bytes():
    uri = _png_data_uri(750, 750)
    payload = {
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": uri}},
            ]},
        ],
    }
    survey, nbytes, _ = survey_and_hash_openai_chat_payload(payload)
    assert survey.text_bytes == len(b"look")
    assert survey.image_dims == ((750, 750),)
    assert survey.unmeasurable_images == 0


def test_chat_payload_refuses_remote_image_url():
    payload = {
        "messages": [
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
            ]},
        ],
    }
    survey, _, _ = survey_and_hash_openai_chat_payload(payload)
    assert survey.unmeasurable_images == 1
    assert survey.image_dims == ()


def test_chat_payload_counts_tool_calls_and_top_level_tools():
    base_payload = {"messages": [{"role": "user", "content": "weather?"}]}
    with_history = {
        "messages": [
            {"role": "user", "content": "weather?"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "get_weather", "arguments": '{"location": "Tokyo, a long string"}'}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "22C sunny"},
        ],
    }
    with_tools = {**base_payload, "tools": [
        {"type": "function", "function": {"name": "get_weather",
         "description": "get weather", "parameters": {"type": "object"}}},
    ]}
    base_survey, _, _ = survey_and_hash_openai_chat_payload(base_payload)
    hist_survey, _, _ = survey_and_hash_openai_chat_payload(with_history)
    tools_survey, _, _ = survey_and_hash_openai_chat_payload(with_tools)
    assert hist_survey.text_bytes > base_survey.text_bytes
    assert tools_survey.text_bytes > base_survey.text_bytes


# ---------------------------------------------------------------------------
# Responses API payload shape
# ---------------------------------------------------------------------------


def test_responses_payload_counts_instructions_and_string_input():
    payload = {"instructions": "be nice", "input": "hello world"}
    survey, nbytes, digest = survey_and_hash_openai_responses_payload(payload)
    assert survey.text_bytes == len(b"be nice") + 1 + len(b"hello world")
    # The returned byte count is the SERIALISED payload, not `survey.text_bytes`.
    # A measured request settled above its own bound because the text-only count
    # omitted the chat template the provider bills for, so the two numbers are
    # deliberately different now: the survey still reports the content it saw, and
    # the bound is priced from the envelope that contains it.
    assert nbytes > survey.text_bytes
    assert len(digest) == 64


def test_responses_payload_counts_input_text_blocks_in_list_items():
    payload = {"input": [
        {"role": "user", "content": [{"type": "input_text", "text": "hi there"}]},
    ]}
    survey, _, _ = survey_and_hash_openai_responses_payload(payload)
    assert survey.text_bytes == len(b"hi there")


def test_responses_payload_counts_function_call_history_as_input():
    base_payload = {"input": "weather?"}
    with_history = {"input": [
        {"role": "user", "content": [{"type": "input_text", "text": "weather?"}]},
        {"type": "function_call", "name": "get_weather",
         "arguments": '{"location": "Tokyo, a fairly long location string"}',
         "call_id": "c1"},
        {"type": "function_call_output", "call_id": "c1", "output": "22C sunny"},
    ]}
    base_survey, _, _ = survey_and_hash_openai_responses_payload(base_payload)
    hist_survey, _, _ = survey_and_hash_openai_responses_payload(with_history)
    assert hist_survey.text_bytes > base_survey.text_bytes


def test_responses_payload_refuses_input_file_and_bare_input_image():
    payload = {"input": [
        {"role": "user", "content": [{"type": "input_file", "file_id": "f1"}]},
    ]}
    survey, _, _ = survey_and_hash_openai_responses_payload(payload)
    assert survey.unmeasurable_images == 1


def test_responses_payload_bounds_inline_input_image():
    uri = _png_data_uri(100, 200)
    payload = {"input": [
        {"role": "user", "content": [{"type": "input_image", "image_url": uri}]},
    ]}
    survey, _, _ = survey_and_hash_openai_responses_payload(payload)
    assert survey.image_dims == ((100, 200),)
    assert survey.unmeasurable_images == 0


def test_responses_payload_counts_top_level_tools():
    base_payload = {"input": "hi"}
    with_tools = {"input": "hi", "tools": [
        {"type": "function", "name": "get_weather", "parameters": {"type": "object"}},
    ]}
    base_survey, _, _ = survey_and_hash_openai_responses_payload(base_payload)
    tools_survey, _, _ = survey_and_hash_openai_responses_payload(with_tools)
    assert tools_survey.text_bytes > base_survey.text_bytes
