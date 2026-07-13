"""OpenAI Chat Completions compatibility endpoint.

POST /v1/chat/completions
    Accepts an OpenAI-shaped request, calls Bedrock `converse` / `converse_stream`
    via the shared budget-flow layer, and returns an OpenAI-shaped response.

This is NOT a new backend — it is the SAME Bedrock Converse backend as
/v1/messages, with a different request/response wire shape. The shared
converse core + budget-flow layer eliminate all duplication.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncGenerator, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .anthropic import _bedrock_client
from ._pipeline import (
    release_pool as _release_pool,
    reserve_credit_for_model,
    settle_reservation_and_log as _settle_reservation_and_log,
)
from .authz import require_permission
from .deps import AuthenticatedUser
from .models import resolve_bedrock_model

router = APIRouter(tags=["mvp-chat-completions"])


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: str
    content: Any = None
    name: Optional[str] = None
    tool_calls: Optional[list[dict[str, Any]]] = None
    tool_call_id: Optional[str] = None


class ChatCompletionsRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str = Field(min_length=1, max_length=256)
    messages: list[ChatMessage]
    max_tokens: Optional[int] = Field(default=4096, ge=1, le=65536)
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    top_p: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    stop: Optional[list[str]] = None
    stream: bool = False
    tools: Optional[list[dict[str, Any]]] = None
    tool_choice: Optional[Any] = None
    n: Optional[int] = None
    logprobs: Optional[bool] = None
    top_logprobs: Optional[int] = None
    response_format: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Conversion: OpenAI Chat → Bedrock Converse kwargs
# ---------------------------------------------------------------------------

def _convert_chat_messages(
    messages: list[ChatMessage],
) -> tuple[list[dict[str, Any]], Optional[list[dict[str, str]]]]:
    """Convert OpenAI chat messages to Bedrock Converse messages + system."""
    system_texts: list[str] = []
    converse_msgs: list[dict[str, Any]] = []

    for msg in messages:
        if msg.role == "system":
            if isinstance(msg.content, str):
                system_texts.append(msg.content)
            elif isinstance(msg.content, list):
                for part in msg.content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        system_texts.append(part.get("text", ""))
            continue

        if msg.role == "tool":
            tool_result = {
                "toolResult": {
                    "toolUseId": msg.tool_call_id or "",
                    "content": [{"text": msg.content if isinstance(msg.content, str) else json.dumps(msg.content)}],
                }
            }
            if converse_msgs and converse_msgs[-1]["role"] == "user":
                converse_msgs[-1]["content"].append(tool_result)
            else:
                converse_msgs.append({"role": "user", "content": [tool_result]})
            continue

        role = "assistant" if msg.role == "assistant" else "user"
        content_blocks: list[dict[str, Any]] = []

        if msg.content is not None:
            if isinstance(msg.content, str):
                if msg.content:
                    content_blocks.append({"text": msg.content})
            elif isinstance(msg.content, list):
                for part in msg.content:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            content_blocks.append({"text": part.get("text", "")})

        if msg.tool_calls:
            for tc in msg.tool_calls:
                fn = tc.get("function", {})
                args = fn.get("arguments", "{}")
                try:
                    parsed_args = json.loads(args) if isinstance(args, str) else args
                except (json.JSONDecodeError, TypeError):
                    parsed_args = {}
                content_blocks.append({
                    "toolUse": {
                        "toolUseId": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": parsed_args,
                    }
                })

        if content_blocks:
            converse_msgs.append({"role": role, "content": content_blocks})
        elif role == "assistant":
            converse_msgs.append({"role": "assistant", "content": [{"text": ""}]})

    system = [{"text": "\n".join(system_texts)}] if system_texts else None
    return converse_msgs, system


def _convert_chat_tools(tools: Optional[list[dict[str, Any]]]) -> Optional[dict[str, Any]]:
    """Convert OpenAI tools array to Bedrock toolConfig."""
    if not tools:
        return None
    converse_tools = []
    for t in tools:
        if t.get("type") != "function":
            continue
        fn = t.get("function", {})
        converse_tools.append({
            "toolSpec": {
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "inputSchema": {"json": fn.get("parameters", {})},
            }
        })
    if not converse_tools:
        return None
    return {"tools": converse_tools}


def _build_chat_bedrock_kwargs(
    body: ChatCompletionsRequest, model_id: str
) -> dict[str, Any]:
    """Build Bedrock Converse kwargs from an OpenAI Chat Completions request."""
    messages, system = _convert_chat_messages(body.messages)

    inference_config: dict[str, Any] = {"maxTokens": body.max_tokens or 4096}
    if body.temperature is not None:
        inference_config["temperature"] = min(body.temperature, 1.0)
    if body.top_p is not None:
        inference_config["topP"] = body.top_p
    if body.stop:
        inference_config["stopSequences"] = body.stop[:4]

    kwargs: dict[str, Any] = {
        "modelId": model_id,
        "messages": messages,
        "inferenceConfig": inference_config,
    }
    if system:
        kwargs["system"] = system

    tool_config = _convert_chat_tools(body.tools)
    if tool_config:
        kwargs["toolConfig"] = tool_config

    return kwargs


# ---------------------------------------------------------------------------
# Response rendering
# ---------------------------------------------------------------------------

_STOP_MAP = {
    "end_turn": "stop",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "content_filtered": "content_filter",
}


def _map_finish_reason(bedrock_reason: Optional[str]) -> str:
    return _STOP_MAP.get(bedrock_reason or "end_turn", "stop")


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@router.post("/v1/chat/completions")
def chat_completions(
    body: ChatCompletionsRequest,
    user: AuthenticatedUser = Depends(require_permission("messages:send")),
):
    # Reject unsupported parameters explicitly (no silent drops)
    if body.n is not None and body.n > 1:
        raise HTTPException(status_code=400, detail={"error": {"message": "n > 1 is not supported", "type": "invalid_request_error", "code": "unsupported_parameter"}})
    if body.logprobs:
        raise HTTPException(status_code=400, detail={"error": {"message": "logprobs is not supported", "type": "invalid_request_error", "code": "unsupported_parameter"}})
    if body.top_logprobs is not None:
        raise HTTPException(status_code=400, detail={"error": {"message": "top_logprobs is not supported", "type": "invalid_request_error", "code": "unsupported_parameter"}})
    if body.response_format is not None:
        raise HTTPException(status_code=400, detail={"error": {"message": "response_format is not supported", "type": "invalid_request_error", "code": "unsupported_parameter"}})
    if body.stream and body.tools:
        raise HTTPException(status_code=400, detail={"error": {"message": "streaming with tools is not yet supported; use stream=false for tool-calling requests", "type": "invalid_request_error", "code": "unsupported_parameter"}})

    try:
        model_id = resolve_bedrock_model(body.model)
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"error": {"message": str(e), "type": "invalid_request_error", "code": "invalid_model"}})

    char_count = sum(
        len(m.content) if isinstance(m.content, str) else 0 for m in body.messages
    )
    input_est = max(char_count // 3, 0)
    max_out = body.max_tokens or 4096
    reservation = max(max_out + input_est, 1024)

    tenants_repo = reserve_credit_for_model(
        user, reservation,
        model_name=body.model,
        input_tokens_est=input_est,
        max_output_tokens=max_out,
    )

    if body.stream:
        return StreamingResponse(
            _stream_chat(body, model_id, user, tenants_repo, reservation),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # Non-streaming path
    kwargs = _build_chat_bedrock_kwargs(body, model_id)
    try:
        resp = _bedrock_client().converse(**kwargs)
    except Exception as e:
        tenants_repo.refund(user_id=user.user_id, tenant_id=user.org_id, tokens=reservation)
        _release_pool(tenants_repo)
        from core.error_handler import sanitize_exception_message
        raise HTTPException(status_code=502, detail={"error": {"message": sanitize_exception_message(str(e)), "type": "api_error"}})

    usage = resp.get("usage", {})
    input_tokens = int(usage.get("inputTokens", 0))
    output_tokens = int(usage.get("outputTokens", 0))
    from ._converse_core import cache_tokens_from_usage
    cache_read, cache_write = cache_tokens_from_usage(usage)

    _settle_reservation_and_log(
        user=user, tenants_repo=tenants_repo, reservation=reservation,
        actual_input_tokens=input_tokens, actual_output_tokens=output_tokens,
        model_id=model_id, context=tenants_repo,
        actual_cache_read_tokens=cache_read, actual_cache_write_tokens=cache_write,
    )

    content_blocks = resp.get("output", {}).get("message", {}).get("content", [])
    text_parts = []
    tool_calls_out = []
    tc_idx = 0
    for block in content_blocks:
        if "text" in block:
            text_parts.append(block["text"])
        elif "toolUse" in block:
            tu = block["toolUse"]
            tool_calls_out.append({
                "id": tu.get("toolUseId", f"call_{uuid.uuid4().hex[:24]}"),
                "type": "function",
                "function": {
                    "name": tu.get("name", ""),
                    "arguments": json.dumps(tu.get("input", {})),
                },
                "index": tc_idx,
            })
            tc_idx += 1

    stop_reason = resp.get("stopReason", "end_turn")
    finish_reason = _map_finish_reason(stop_reason)

    message_out: dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts) if text_parts else None}
    if tool_calls_out:
        message_out["tool_calls"] = tool_calls_out

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.model,
        "choices": [
            {
                "index": 0,
                "message": message_out,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

async def _stream_chat(
    body: ChatCompletionsRequest,
    model_id: str,
    user: AuthenticatedUser,
    tenants_repo: Any,
    reservation: int,
) -> AsyncGenerator[bytes, None]:
    """SSE stream in OpenAI chat.completion.chunk format."""
    import asyncio
    from botocore.exceptions import ClientError
    from core.error_handler import sanitize_exception_message
    from ._converse_core import _aiter_blocking_stream, cache_tokens_from_usage

    chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    settled = False

    def _sse(data: dict) -> bytes:
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode()

    try:
        # First chunk: role
        yield _sse({
            "id": chat_id, "object": "chat.completion.chunk",
            "created": int(time.time()), "model": body.model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
        })

        kwargs = _build_chat_bedrock_kwargs(body, model_id)
        try:
            client = _bedrock_client()
            resp = await asyncio.to_thread(client.converse_stream, **kwargs)
        except ClientError as e:
            tenants_repo.refund(user_id=user.user_id, tenant_id=user.org_id, tokens=reservation)
            _release_pool(tenants_repo)
            settled = True
            yield _sse({"error": {"message": sanitize_exception_message(str(e)), "type": "api_error"}})
            return

        stop_reason_bedrock = None
        try:
            async for event in _aiter_blocking_stream(resp.get("stream", [])):
                if "contentBlockDelta" in event:
                    delta_obj = event["contentBlockDelta"].get("delta", {})
                    text = delta_obj.get("text", "")
                    if text:
                        yield _sse({
                            "id": chat_id, "object": "chat.completion.chunk",
                            "created": int(time.time()), "model": body.model,
                            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
                        })
                elif "messageStop" in event:
                    stop_reason_bedrock = event["messageStop"].get("stopReason")
                elif "metadata" in event:
                    usage = event["metadata"].get("usage", {})
                    input_tokens = int(usage.get("inputTokens", input_tokens))
                    output_tokens = int(usage.get("outputTokens", output_tokens))
                    cr, cw = cache_tokens_from_usage(usage)
                    cache_read_tokens = cr or cache_read_tokens
                    cache_write_tokens = cw or cache_write_tokens
        except Exception as e:
            yield _sse({"error": {"message": sanitize_exception_message(str(e)), "type": "api_error"}})
            _settle_reservation_and_log(
                user=user, tenants_repo=tenants_repo, reservation=reservation,
                actual_input_tokens=input_tokens, actual_output_tokens=output_tokens,
                model_id=model_id, context=tenants_repo,
                actual_cache_read_tokens=cache_read_tokens, actual_cache_write_tokens=cache_write_tokens,
            )
            settled = True
            return

        # Final chunk with finish_reason
        finish_reason = _map_finish_reason(stop_reason_bedrock)
        yield _sse({
            "id": chat_id, "object": "chat.completion.chunk",
            "created": int(time.time()), "model": body.model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": input_tokens, "completion_tokens": output_tokens, "total_tokens": input_tokens + output_tokens},
        })

        yield b"data: [DONE]\n\n"

        _settle_reservation_and_log(
            user=user, tenants_repo=tenants_repo, reservation=reservation,
            actual_input_tokens=input_tokens, actual_output_tokens=output_tokens,
            model_id=model_id, context=tenants_repo,
            actual_cache_read_tokens=cache_read_tokens, actual_cache_write_tokens=cache_write_tokens,
        )
        settled = True
    finally:
        if not settled:
            _settle_reservation_and_log(
                user=user, tenants_repo=tenants_repo, reservation=reservation,
                actual_input_tokens=input_tokens, actual_output_tokens=output_tokens,
                model_id=model_id, context=tenants_repo,
                actual_cache_read_tokens=cache_read_tokens, actual_cache_write_tokens=cache_write_tokens,
            )
