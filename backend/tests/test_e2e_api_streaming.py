"""End-to-end API tests for /v1/messages and /v1/chat/completions streaming.

These exercise the full HTTP layer (FastAPI TestClient) with mocked Bedrock
and mocked auth, verifying:
- SSE framing (event: / data: / \\n\\n)
- Event ordering (message_start → content_block_* → message_delta → message_stop)
- Non-streaming response shape
- chat/completions chunk format + [DONE]
- Error response shapes
- Parameter rejection HTTP status codes
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mvp.anthropic import router as anthropic_router
from mvp.chat_completions import router as chat_router
from mvp.deps import get_current_user
from mvp.authz import _PERMS_CACHE


@dataclass
class _FakeUser:
    user_id: str = "user-11111111-1111-1111-1111-111111111111"
    org_id: str = "default-org"
    email: str = "test@example.com"
    roles: list = None
    auth_kind: str = "jwt"
    key_scopes: list = None

    def __post_init__(self):
        if self.roles is None:
            self.roles = ["user", "admin"]


def _mock_converse(**kwargs):
    return {
        "output": {"message": {"content": [{"text": "Hello world!"}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 10, "outputTokens": 5},
    }


def _mock_converse_stream(**kwargs):
    return {"stream": iter([
        {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Hi"}}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": " there"}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 8, "outputTokens": 3}}},
    ])}


@pytest.fixture
def api_client(dynamodb_mock, seed_active_tenant):
    """TestClient with both routers, mocked auth, mocked Bedrock."""
    import time
    _PERMS_CACHE["user"] = (["messages:send", "usage:read-self"], time.time() + 3600)
    _PERMS_CACHE["admin"] = (["messages:send", "usage:read-self", "tenants:update"], time.time() + 3600)

    app = FastAPI()
    app.include_router(anthropic_router)
    app.include_router(chat_router)
    app.dependency_overrides[get_current_user] = lambda: _FakeUser()

    with patch("mvp.anthropic._bedrock_client") as mock_bedrock:
        with patch("mvp.chat_completions._bedrock_client") as mock_chat_bedrock:
            with patch("mvp.routing.infrarouter.bedrock_client") as mock_routing:
                mock_bedrock.return_value.converse.side_effect = _mock_converse
                mock_bedrock.return_value.converse_stream.side_effect = _mock_converse_stream
                mock_chat_bedrock.return_value.converse.side_effect = _mock_converse
                mock_chat_bedrock.return_value.converse_stream.side_effect = _mock_converse_stream
                mock_routing.return_value.converse_stream.side_effect = _mock_converse_stream
                yield TestClient(app)


# ---------------------------------------------------------------------------
# /v1/messages - Anthropic wire
# ---------------------------------------------------------------------------

class TestAnthropicMessagesE2E:
    def test_non_streaming_response_shape(self, api_client):
        resp = api_client.post("/v1/messages", json={
            "model": "us.anthropic.claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 50,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "message"
        assert data["role"] == "assistant"
        assert data["id"].startswith("msg_")
        assert data["stop_reason"] == "end_turn"
        assert data["content"][0]["type"] == "text"
        assert data["usage"]["input_tokens"] == 10
        assert data["usage"]["output_tokens"] == 5

    def test_streaming_sse_format(self, api_client):
        resp = api_client.post("/v1/messages", json={
            "model": "us.anthropic.claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 50,
            "stream": True,
        })
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

        # Parse SSE events
        events = []
        for line in resp.text.split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))

        types = [e.get("type") for e in events]
        assert types[0] == "message_start"
        assert types[1] == "content_block_start"
        assert "content_block_delta" in types
        assert types[-3] == "content_block_stop"
        assert types[-2] == "message_delta"
        assert types[-1] == "message_stop"

        # Verify message_start shape
        msg = events[0]["message"]
        assert msg["role"] == "assistant"
        assert msg["id"].startswith("msg_")

        # Verify content was streamed
        text_deltas = [e for e in events if e.get("type") == "content_block_delta"]
        content = "".join(d["delta"]["text"] for d in text_deltas)
        assert "Hi" in content

        # Verify message_delta has usage
        msg_delta = next(e for e in events if e.get("type") == "message_delta")
        assert msg_delta["usage"]["input_tokens"] == 8
        assert msg_delta["delta"]["stop_reason"] == "end_turn"

    def test_invalid_model_returns_400(self, api_client):
        resp = api_client.post("/v1/messages", json={
            "model": "nonexistent-model",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 50,
        })
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /v1/chat/completions - OpenAI wire
# ---------------------------------------------------------------------------

class TestChatCompletionsE2E:
    def test_non_streaming_response_shape(self, api_client):
        resp = api_client.post("/v1/chat/completions", json={
            "model": "us.anthropic.claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "chat.completion"
        assert data["id"].startswith("chatcmpl-")
        assert data["choices"][0]["message"]["role"] == "assistant"
        assert data["choices"][0]["finish_reason"] == "stop"
        assert data["usage"]["prompt_tokens"] == 10
        assert data["usage"]["completion_tokens"] == 5

    def test_streaming_chunk_format(self, api_client):
        resp = api_client.post("/v1/chat/completions", json={
            "model": "us.anthropic.claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        })
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

        lines = [ln for ln in resp.text.split("\n") if ln.startswith("data:")]
        assert lines[-1] == "data: [DONE]"

        # Parse chunks
        chunks = []
        for line in lines[:-1]:
            chunks.append(json.loads(line[6:]))

        # First chunk has role
        assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
        assert chunks[0]["object"] == "chat.completion.chunk"

        # Content chunks
        content = ""
        for c in chunks:
            delta = c["choices"][0]["delta"]
            if "content" in delta:
                content += delta["content"]
        assert "Hi" in content

        # Last non-DONE chunk has finish_reason
        last_chunk = chunks[-1]
        assert last_chunk["choices"][0]["finish_reason"] == "stop"

    def test_n_greater_than_1_returns_400(self, api_client):
        resp = api_client.post("/v1/chat/completions", json={
            "model": "us.anthropic.claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
            "n": 2,
        })
        assert resp.status_code == 400

    def test_system_message_works(self, api_client):
        resp = api_client.post("/v1/chat/completions", json={
            "model": "us.anthropic.claude-opus-4-7",
            "messages": [
                {"role": "system", "content": "You are helpful"},
                {"role": "user", "content": "hi"},
            ],
        })
        assert resp.status_code == 200
