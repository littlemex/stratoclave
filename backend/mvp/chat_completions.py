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

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .anthropic import _bedrock_client, _selected_bedrock_model
from ._pipeline import (
    release_pool as _release_pool,
    reserve_credit_for_model,
    settle_reservation_and_log as _settle_reservation_and_log,
)
from .authz import require_permission
from .deps import AuthenticatedUser, extract_model_pin, get_request_context
from .observability.context import RequestContext, response_headers as _corr_headers
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


_MAX_CHAT_MESSAGES = 500
_MAX_CHAT_CONTENT_CHARS = 200_000


class ChatCompletionsRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str = Field(min_length=1, max_length=256)
    messages: list[ChatMessage] = Field(max_length=_MAX_CHAT_MESSAGES)
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

def _shadow_tenant_pref(org_id: str):
    """The tenant's per-tenant shadow_vsr preference (True/False/None) via the
    single shared, cached, rate-limited-fail-open helper (routing.config.
    tenant_shadow_pref). Thin wrapper kept so the call site reads locally."""
    from .routing.config import tenant_shadow_pref

    return tenant_shadow_pref(org_id)


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
                        elif part.get("type") == "image_url":
                            raise ValueError("image_url content parts are not supported; use the Anthropic /v1/messages endpoint with base64 images")

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
        tc = body.tool_choice
        if tc is not None:
            if tc == "required" or (isinstance(tc, dict) and tc.get("type") == "required"):
                tool_config["toolChoice"] = {"any": {}}
            elif isinstance(tc, dict) and tc.get("type") == "function":
                tool_config["toolChoice"] = {"tool": {"name": tc.get("function", {}).get("name", "")}}
            else:
                tool_config["toolChoice"] = {"auto": {}}
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
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(require_permission("messages:send")),
    ctx: RequestContext = Depends(get_request_context),
):
    # P0-12: echo the correlation ids so a client can stitch calls into a run.
    corr = _corr_headers(ctx)
    response.headers.update(corr)

    # P0-15: optional VSR hard pin (see anthropic.messages). Absent -> unchanged.
    model_pin = extract_model_pin(request)

    # Reject unsupported parameters explicitly (no silent drops)
    if body.n is not None and body.n > 1:
        raise HTTPException(status_code=400, detail={"error": {"message": "n > 1 is not supported", "type": "invalid_request_error", "code": "unsupported_parameter"}})
    if body.logprobs:
        raise HTTPException(status_code=400, detail={"error": {"message": "logprobs is not supported", "type": "invalid_request_error", "code": "unsupported_parameter"}})
    if body.top_logprobs is not None:
        raise HTTPException(status_code=400, detail={"error": {"message": "top_logprobs is not supported", "type": "invalid_request_error", "code": "unsupported_parameter"}})
    if body.response_format is not None:
        raise HTTPException(status_code=400, detail={"error": {"message": "response_format is not supported", "type": "invalid_request_error", "code": "unsupported_parameter"}})
    try:
        model_id = resolve_bedrock_model(body.model)
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"error": {"message": str(e), "type": "invalid_request_error", "code": "invalid_model"}})

    char_count = 0
    for m in body.messages:
        if isinstance(m.content, str):
            char_count += len(m.content)
        elif isinstance(m.content, list):
            for part in m.content:
                if isinstance(part, dict):
                    text = part.get("text", "")
                    if isinstance(text, str):
                        char_count += len(text)
        if m.tool_calls:
            for tc in m.tool_calls:
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                args = fn.get("arguments", "")
                char_count += len(args) if isinstance(args, str) else 0
    if body.tools:
        char_count += sum(len(json.dumps(t)) for t in body.tools)
    if char_count > _MAX_CHAT_CONTENT_CHARS:
        raise HTTPException(status_code=400, detail={"error": {"message": f"content exceeds {_MAX_CHAT_CONTENT_CHARS} char cap", "type": "invalid_request_error", "code": "content_too_large"}})
    input_est = max(char_count // 3, 0)
    max_out = body.max_tokens or 4096
    reservation = max(max_out + input_est, 1024)

    # Build the Bedrock kwargs BEFORE the reserve. `_build_chat_bedrock_kwargs`
    # is pure, and it is the single place that rejects unsupported content
    # (image_url, etc.). Doing it pre-reserve turns every conversion error into
    # a clean 400 request error instead of a post-reserve 502, avoids a needless
    # hold, and removes the twin-validation drift hazard (no separate pre-check
    # that can diverge from the converter). The same kwargs are reused by both
    # the streaming and non-streaming paths below.
    try:
        kwargs = _build_chat_bedrock_kwargs(body, model_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"error": {"message": str(e), "type": "invalid_request_error", "code": "unsupported_content"}})

    # Shadow VSR (litellm wedge): this endpoint has no external-VSR consult, so the
    # local rule judge is the only advisory. Dark by default + fail-open +
    # advisory-only: it never sets a pin (no vsr_hard_model) and emits no response
    # header; it only attaches a shadow-advised block to the decision record so the
    # offline savings certificate can show the POTENTIAL saving. Never on money path.
    # Suppressed when a pin decides routing (a deliberate pin is not a downgrade
    # candidate); shadow_enabled() checked FIRST so a dark deploy extracts no
    # features on the hot path (Fable review-2 (d)/(e)).
    _shadow_vsr = None
    if model_pin is None:
        try:
            from .vsr import shadow as _shadow
            # cheap env-only force-off before the per-tenant config read so a
            # fleet-wide dark deploy pays no lookup (Fable per-tenant review Low).
            _tenant_shadow = (None if _shadow.shadow_globally_forced_off()
                              else _shadow_tenant_pref(user.org_id))
            if _shadow.shadow_enabled(_tenant_shadow):
                _shadow_vsr = _shadow.shadow_vsr_decision(
                    requested_model=body.model,
                    tenant_shadow=_tenant_shadow,
                    features=_shadow.extract_features_openai(
                        approx_input_tokens=input_est,
                        tools=getattr(body, "tools", None), messages=body.messages),
                )
        except Exception:  # noqa: BLE001 — advisory + fail-open; never break a request.
            _shadow_vsr = None

    tenants_repo = reserve_credit_for_model(
        user, reservation,
        model_name=body.model,
        input_tokens_est=input_est,
        max_output_tokens=max_out,
        wire_protocol="messages",
        vsr_hard_model=model_pin,
        # L5-d: per-run billing attribution.
        workflow_run_id=ctx.workflow_run_id if ctx else None,
        group_id=ctx.group_id if ctx else None,
        request_id=ctx.request_id if ctx else None,
        vsr_decision=_shadow_vsr,
    )

    # The reservation may have cascaded to a fallback model (P0-11). Re-point
    # both the invoke target and the pre-built kwargs at the model actually
    # priced/quota-charged so the Bedrock call agrees with the pool + quota.
    # The cascade only selects registry-resolvable `messages`-protocol models,
    # so a cross-protocol / typo'd chain entry can never win here.
    selected_id = _selected_bedrock_model(tenants_repo, model_id)
    if selected_id != model_id:
        model_id = selected_id
        kwargs["modelId"] = model_id

    if body.stream:
        return StreamingResponse(
            _stream_chat(body, model_id, user, tenants_repo, reservation, kwargs,
                         request_id=ctx.request_id if ctx else None),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                **corr,
            },
        )

    # Non-streaming path
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
        # Key the UsageLogs row on the request id for the offline VSR reconcile join.
        request_id=ctx.request_id if ctx else None,
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
    kwargs: dict,
    request_id: Optional[str] = None,
) -> AsyncGenerator[bytes, None]:
    """SSE stream via the shared _budget_flow.run_stream + ChatAdapter.

    `kwargs` is the pre-built Bedrock converse payload (built once, pre-reserve,
    by the caller) so conversion errors surface as a 400 before any hold and
    the build isn't duplicated on the streaming path.
    """
    from . import _budget_flow

    chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    def _sse(data: dict) -> bytes:
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode()

    class _ChatAdapter:
        def __init__(self):
            from . import _converse_types as t
            self._t = t
            self.stop_reason = None
            self._tool_calls: list[dict] = []
            self._tc_idx = 0

        def prologue(self):
            yield _sse({
                "id": chat_id, "object": "chat.completion.chunk",
                "created": created, "model": body.model,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
            })

        def render_event(self, event):
            t = self._t
            if isinstance(event, t.ContentTextDelta):
                yield _sse({
                    "id": chat_id, "object": "chat.completion.chunk",
                    "created": created, "model": body.model,
                    "choices": [{"index": 0, "delta": {"content": event.text}, "finish_reason": None}],
                })
            elif isinstance(event, t.ContentToolUseStart):
                tc = {"index": self._tc_idx, "id": event.tool_use_id, "type": "function",
                      "function": {"name": event.name, "arguments": ""}}
                self._tc_idx += 1
                yield _sse({
                    "id": chat_id, "object": "chat.completion.chunk",
                    "created": created, "model": body.model,
                    "choices": [{"index": 0, "delta": {"tool_calls": [tc]}, "finish_reason": None}],
                })
            elif isinstance(event, t.ContentToolUseDelta):
                yield _sse({
                    "id": chat_id, "object": "chat.completion.chunk",
                    "created": created, "model": body.model,
                    "choices": [{"index": 0, "delta": {"tool_calls": [
                        {"index": self._tc_idx - 1, "function": {"arguments": event.partial_json}}
                    ]}, "finish_reason": None}],
                })
            elif isinstance(event, t.MessageStop):
                self.stop_reason = event.stop_reason

        def epilogue(self):
            finish_reason = _map_finish_reason(self.stop_reason)
            yield _sse({
                "id": chat_id, "object": "chat.completion.chunk",
                "created": created, "model": body.model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            })
            yield b"data: [DONE]\n\n"

        def error_event(self, message):
            yield _sse({"error": {"message": message, "type": "api_error"}})

    def _invoke(*, body, model_id):
        # kwargs was built (and validated) pre-reserve by the caller. Honour the
        # model_id run_stream passes so the payload can't drift from the caller
        # (today they always match — this path does not go through InfraRouter,
        # so there is no model failover — but keeping model_id load-bearing
        # avoids a silent same-model re-invoke if that ever changes).
        client = _bedrock_client()
        return client.converse_stream(**{**kwargs, "modelId": model_id})

    async for frame in _budget_flow.run_stream(
        body=body,
        model_id=model_id,
        model_alias=body.model,
        user=user,
        tenants_repo=tenants_repo,
        reservation=reservation,
        invoke_stream=_invoke,
        settle=lambda **kw: _settle_reservation_and_log(**kw),
        release=lambda ctx: _release_pool(ctx),
        adapter=_ChatAdapter(),
        request_id=request_id,
    ):
        yield frame
