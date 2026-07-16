"""Tests for the OpenAI Chat Completions endpoint (/v1/chat/completions).

Unit tests for conversion logic + integration tests via TestClient with moto.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from mvp.chat_completions import (
    ChatCompletionsRequest,
    ChatMessage,
    _build_chat_bedrock_kwargs,
    _convert_chat_messages,
    _convert_chat_tools,
    _map_finish_reason,
)


def _dummy_request(headers: dict | None = None):
    """A bare FastAPI Request for direct handler calls (P0-15 pin extraction)."""
    from fastapi import Request
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "headers": hdrs})


def _dummy_response():
    """A bare FastAPI Response for direct handler calls (headers land here)."""
    from fastapi import Response
    return Response()


def _dummy_ctx():
    """A minimal RequestContext for direct handler calls (correlation echo)."""
    from mvp.observability.context import build_request_context
    return build_request_context(
        tenant_id="test-org", group_id_header=None, workflow_run_id_header=None,
    )


# ---------------------------------------------------------------------------
# Unit: message conversion
# ---------------------------------------------------------------------------

class TestMessageConversion:
    def test_system_message_extracted(self):
        msgs = [
            ChatMessage(role="system", content="You are helpful"),
            ChatMessage(role="user", content="hi"),
        ]
        converse_msgs, system = _convert_chat_messages(msgs)
        assert system == [{"text": "You are helpful"}]
        assert len(converse_msgs) == 1
        assert converse_msgs[0]["role"] == "user"

    def test_tool_messages_merge_into_user(self):
        msgs = [
            ChatMessage(role="user", content="call tools"),
            ChatMessage(role="assistant", content=None, tool_calls=[
                {"id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
            ]),
            ChatMessage(role="tool", content="result1", tool_call_id="call_1"),
        ]
        converse_msgs, _ = _convert_chat_messages(msgs)
        last_user = [m for m in converse_msgs if m["role"] == "user"][-1]
        assert any("toolResult" in b for b in last_user["content"])

    def test_assistant_tool_calls_become_tool_use(self):
        msgs = [
            ChatMessage(role="assistant", content="", tool_calls=[
                {"id": "tc_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"Tokyo"}'}}
            ]),
        ]
        converse_msgs, _ = _convert_chat_messages(msgs)
        assert len(converse_msgs) == 1
        block = converse_msgs[0]["content"][0]
        assert "toolUse" in block
        assert block["toolUse"]["name"] == "get_weather"
        assert block["toolUse"]["input"] == {"city": "Tokyo"}


# ---------------------------------------------------------------------------
# Unit: tool config conversion
# ---------------------------------------------------------------------------

class TestToolConversion:
    def test_openai_tools_to_bedrock_tool_config(self):
        tools = [
            {"type": "function", "function": {"name": "search", "description": "Search", "parameters": {"type": "object"}}}
        ]
        result = _convert_chat_tools(tools)
        assert result is not None
        assert len(result["tools"]) == 1
        assert result["tools"][0]["toolSpec"]["name"] == "search"

    def test_none_tools_returns_none(self):
        assert _convert_chat_tools(None) is None
        assert _convert_chat_tools([]) is None


# ---------------------------------------------------------------------------
# Unit: finish reason mapping
# ---------------------------------------------------------------------------

class TestFinishReason:
    def test_end_turn_maps_to_stop(self):
        assert _map_finish_reason("end_turn") == "stop"

    def test_tool_use_maps_to_tool_calls(self):
        assert _map_finish_reason("tool_use") == "tool_calls"

    def test_max_tokens_maps_to_length(self):
        assert _map_finish_reason("max_tokens") == "length"

    def test_none_defaults_to_stop(self):
        assert _map_finish_reason(None) == "stop"


# ---------------------------------------------------------------------------
# Unit: parameter rejection (tested at the route function level)
# ---------------------------------------------------------------------------

class TestParameterRejection:
    def test_n_greater_than_1_rejected(self):
        from fastapi import HTTPException
        from mvp.chat_completions import chat_completions

        body = ChatCompletionsRequest.model_validate({
            "model": "us.anthropic.claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
            "n": 2,
        })
        with pytest.raises(HTTPException) as exc:
            chat_completions(body, _dummy_request(), _dummy_response(), user=None, ctx=_dummy_ctx())
        assert exc.value.status_code == 400
        assert "n > 1" in exc.value.detail["error"]["message"]

    def test_logprobs_rejected(self):
        from fastapi import HTTPException
        from mvp.chat_completions import chat_completions

        body = ChatCompletionsRequest.model_validate({
            "model": "us.anthropic.claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
            "logprobs": True,
        })
        with pytest.raises(HTTPException) as exc:
            chat_completions(body, _dummy_request(), _dummy_response(), user=None, ctx=_dummy_ctx())
        assert exc.value.status_code == 400

    def test_response_format_rejected(self):
        from fastapi import HTTPException
        from mvp.chat_completions import chat_completions

        body = ChatCompletionsRequest.model_validate({
            "model": "us.anthropic.claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {"type": "json_object"},
        })
        with pytest.raises(HTTPException) as exc:
            chat_completions(body, _dummy_request(), _dummy_response(), user=None, ctx=_dummy_ctx())
        assert exc.value.status_code == 400

    def test_image_url_content_part_rejected_pre_reservation(self):
        # Fable F4/F5: image_url parts must be rejected with a 400 request error
        # BEFORE the reserve, not surface as a post-reserve 502 from the
        # converter. Reaching this raise with user=None proves the check runs
        # ahead of reserve_credit_for_model (which would need a real user).
        from fastapi import HTTPException
        from mvp.chat_completions import chat_completions

        body = ChatCompletionsRequest.model_validate({
            "model": "us.anthropic.claude-opus-4-7",
            "messages": [{
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": "https://example.com/x.png"}}],
            }],
        })
        with pytest.raises(HTTPException) as exc:
            chat_completions(body, _dummy_request(), _dummy_response(), user=None, ctx=_dummy_ctx())
        assert exc.value.status_code == 400
        assert exc.value.detail["error"]["code"] == "unsupported_content"


# ---------------------------------------------------------------------------
# Unit: bedrock kwargs building
# ---------------------------------------------------------------------------

class TestBuildKwargs:
    def test_basic_request_produces_valid_kwargs(self):
        body = ChatCompletionsRequest.model_validate({
            "model": "us.anthropic.claude-opus-4-7",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 100,
            "temperature": 0.5,
        })
        kwargs = _build_chat_bedrock_kwargs(body, "us.anthropic.claude-opus-4-7")
        assert kwargs["modelId"] == "us.anthropic.claude-opus-4-7"
        assert kwargs["inferenceConfig"]["maxTokens"] == 100
        assert kwargs["inferenceConfig"]["temperature"] == 0.5
        assert len(kwargs["messages"]) == 1

    def test_tools_produce_tool_config(self):
        body = ChatCompletionsRequest.model_validate({
            "model": "us.anthropic.claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "f", "description": "d", "parameters": {}}}],
        })
        kwargs = _build_chat_bedrock_kwargs(body, "us.anthropic.claude-opus-4-7")
        assert "toolConfig" in kwargs
        assert kwargs["toolConfig"]["tools"][0]["toolSpec"]["name"] == "f"

    def test_stop_sequences_forwarded(self):
        body = ChatCompletionsRequest.model_validate({
            "model": "us.anthropic.claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
            "stop": ["END", "STOP"],
        })
        kwargs = _build_chat_bedrock_kwargs(body, "us.anthropic.claude-opus-4-7")
        assert kwargs["inferenceConfig"]["stopSequences"] == ["END", "STOP"]
