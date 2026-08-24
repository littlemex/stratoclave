"""OpenAI Chat Completions compatibility endpoint.

POST /v1/chat/completions
    Accepts an OpenAI-shaped request and returns an OpenAI-shaped response for
    ANY model in the registry, not just the Claude family.

Two upstream transports sit behind the one wire shape, selected by the
resolved entry's `wire_protocol` — the route adds no third transport:

  - `messages`  → Bedrock `converse` / `converse_stream` via the shared
                  budget-flow layer, exactly as /v1/messages does. The client is
                  bound to `entry.bedrock_region`, because this route now serves
                  entries outside the Claude family's us-east-1 (Nemotron and
                  Qwen3 are us-west-2).
  - `responses` → bedrock-mantle's NATIVE OpenAI Chat Completions surface at
                  `/openai/v1/chat/completions`. Mantle speaks Chat Completions
                  directly, so this is a pass-through: no Responses-API
                  round-trip and no shape translation to drift.

Widening this route is what lets an OpenAI-compatible client that cannot speak
the Responses API — vLLM Semantic Router, for one, whose only upstream formats
are `openai` and `anthropic` — reach the mantle-served models through the
gateway, with the same reserve/settle accounting as every other route.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncGenerator, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from . import _mantle_transport
from .anthropic import _selected_bedrock_model
from ._bedrock_clients import client_for_model
from ._pipeline import (
    release_pool as _release_pool,
    reserve_credit_for_model,
    settle_reservation_and_log as _settle_reservation_and_log,
)
from .authz import require_permission
from .deps import AuthenticatedUser, extract_model_pin, get_request_context
from .observability.context import RequestContext, response_headers as _corr_headers
from .models import ModelEntry, resolve_model

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
# One source for the output cap default: the request-model field default, the
# reservation estimate and the Converse inferenceConfig all read it, and a second
# copy would let them disagree about what the caller asked for.
_DEFAULT_MAX_OUTPUT_TOKENS = 4096


class ChatCompletionsRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str = Field(min_length=1, max_length=256)
    messages: list[ChatMessage] = Field(max_length=_MAX_CHAT_MESSAGES)
    max_tokens: Optional[int] = Field(default=_DEFAULT_MAX_OUTPUT_TOKENS, ge=1, le=65536)
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


def _requested_max_output(body: ChatCompletionsRequest) -> int:
    """The caller's intended output cap, preferring the modern spelling.

    `max_tokens` has a non-None default on the request model, so its presence
    says nothing about intent. `max_completion_tokens` only appears when the
    caller actually sent it, so an explicit value there wins.
    """
    explicit = getattr(body, "max_completion_tokens", None)
    if isinstance(explicit, int) and explicit > 0:
        return explicit
    return body.max_tokens or _DEFAULT_MAX_OUTPUT_TOKENS


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

    inference_config: dict[str, Any] = {"maxTokens": _requested_max_output(body)}
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
    # Resolve against the WHOLE registry (not the Claude-only legacy resolver) and
    # keep the entry: its `wire_protocol` picks the upstream transport below and
    # its `bedrock_region` binds the Converse client, so a us-west-2 model is never
    # invoked against the Claude family's us-east-1.
    try:
        entry = resolve_model(body.model)
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"error": {"message": str(e), "type": "invalid_request_error", "code": "invalid_model"}})
    # `resolve_model` resolves the WHOLE registry, including entries this route
    # cannot serve: self-hosted vLLM entries (`served_by="vllm"`) have no Bedrock
    # region or mantle endpoint, and virtual semantic-router pool entries are
    # candidate-chain placeholders that must never be a charge-of-record model.
    # The old Claude-only resolver excluded both by accident; now it is explicit.
    if getattr(entry, "served_by", "bedrock") != "bedrock" or getattr(entry, "virtual", False):
        raise HTTPException(status_code=400, detail={"error": {"message": f"model '{body.model}' is not servable by this route", "type": "invalid_request_error", "code": "invalid_model"}})
    if entry.wire_protocol not in ("messages", "responses"):
        # Fail fast rather than defaulting to a transport: a new wire_protocol that
        # silently fell through to one of these two would be misrouted.
        raise HTTPException(status_code=500, detail={"error": {"message": f"unsupported transport for model '{body.model}'", "type": "api_error"}})
    model_id = entry.bedrock_model_id

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
    # `max_tokens` defaults to 4096, so it is ALWAYS set; a caller using the modern
    # OpenAI spelling sends `max_completion_tokens`, which arrives as an extra
    # (the request model allows extras). Taking the default over an explicit
    # caller value would both truncate the response and under-reserve the hold.
    max_out = _requested_max_output(body)
    reservation = max(max_out + input_est, 1024)

    # Build the Bedrock kwargs BEFORE the reserve. `_build_chat_bedrock_kwargs`
    # is pure, and it is the single place that rejects unsupported content
    # (image_url, etc.). Doing it pre-reserve turns every conversion error into
    # a clean 400 request error instead of a post-reserve 502, avoids a needless
    # hold, and removes the twin-validation drift hazard (no separate pre-check
    # that can diverge from the converter). The same kwargs are reused by both
    # the streaming and non-streaming paths below.
    # Only the Converse transport needs Bedrock kwargs. The mantle transport
    # forwards the client's OpenAI body verbatim, so building (and its content
    # restrictions) would be dead work that also wrongly rejects payloads mantle
    # accepts natively.
    kwargs: dict[str, Any] = {}
    if entry.wire_protocol == "messages":
        # Converse has no equivalent for these two, so they are rejected here
        # rather than route-wide: mantle serves both natively and rejecting them
        # for every transport would deny structured output to the models that
        # support it.
        if body.top_logprobs is not None:
            raise HTTPException(status_code=400, detail={"error": {"message": "top_logprobs is not supported", "type": "invalid_request_error", "code": "unsupported_parameter"}})
        if body.response_format is not None:
            raise HTTPException(status_code=400, detail={"error": {"message": "response_format is not supported", "type": "invalid_request_error", "code": "unsupported_parameter"}})
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
        wire_protocol=entry.wire_protocol,
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
    # The mantle transport forks here, AFTER the reservation, so it inherits the
    # identical reserve/settle/refund accounting as the Converse path — and, like
    # the Converse path, it must invoke whatever the reservation actually selected.
    # `reserve_credit_for_model` cascades and honours a hard pin WITHIN a protocol,
    # so a `responses` request whose quota cascaded to another mantle model would
    # otherwise charge one model and invoke another.
    if entry.wire_protocol == "responses":
        selected_id = _selected_bedrock_model(tenants_repo, entry.bedrock_model_id)
        if selected_id != entry.bedrock_model_id:
            try:
                selected_entry = resolve_model(selected_id)
            except ValueError:
                selected_entry = None
            # Only follow a selection that is still mantle-served: a cross-protocol
            # selection cannot be invoked here, and invoking the originally
            # validated model is safer than guessing a transport.
            if selected_entry is not None and selected_entry.wire_protocol == "responses":
                entry = selected_entry
        return _mantle_chat_completion(
            body=body, entry=entry, user=user, tenants_repo=tenants_repo,
            reservation=reservation, corr=corr,
            request_id=ctx.request_id if ctx else None,
        )

    selected_id = _selected_bedrock_model(tenants_repo, model_id)
    if selected_id != model_id:
        model_id = selected_id
        kwargs["modelId"] = model_id
        # Re-resolve so the Bedrock client follows the model the cascade actually
        # chose. Before this route served non-Claude Converse models every entry
        # shared us-east-1 and `entry` could safely go stale; now a cascade across
        # entries would otherwise invoke a us-west-2 model on a us-east-1 client.
        try:
            entry = resolve_model(selected_id)
        except ValueError:
            # An unresolvable selection means the cascade produced something the
            # registry does not know. Keep the entry we validated up front rather
            # than invoking against a guessed region.
            pass

    if body.stream:
        return StreamingResponse(
            _stream_chat(body, model_id, user, tenants_repo, reservation, kwargs,
                         entry=entry,
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
        resp = client_for_model(entry).converse(**kwargs)
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
# bedrock-mantle pass-through (entries whose wire_protocol is not "messages")
# ---------------------------------------------------------------------------

# Upstream statuses that describe the CALLER's request (or ask it to back off) and
# are therefore worth preserving. Anything else collapses to 502: it describes the
# gateway's own upstream problem, not something the caller can act on.
# 401/403 are deliberately absent: from mantle they mean the GATEWAY's credential
# is wrong, which the caller can do nothing about, so they surface as 502.
_MANTLE_PASSTHROUGH_STATUSES = frozenset({400, 404, 408, 409, 413, 422, 429})

# mantle's Chat Completions path, relative to the transport's `/openai/v1` base.
_MANTLE_CHAT_PATH = "/chat/completions"


def _mantle_status(upstream: int) -> int:
    """Map an upstream status onto the one the caller should see."""
    return upstream if upstream in _MANTLE_PASSTHROUGH_STATUSES else 502


def _mantle_chat_completion(
    *,
    body: ChatCompletionsRequest,
    entry: "ModelEntry",
    user: AuthenticatedUser,
    tenants_repo: Any,
    reservation: int,
    corr: dict[str, str],
    request_id: Optional[str] = None,
):
    """Forward an OpenAI Chat Completions request to bedrock-mantle unchanged.

    mantle serves `/openai/v1/chat/completions` natively, so there is no shape
    translation. Three rewrites are applied and all are mechanical: the `model`
    field carries the resolved Bedrock model ID (mantle 404s on our client-facing
    aliases); `max_tokens` becomes `max_completion_tokens`, the spelling mantle's
    reasoning-capable tiers accept; and on a streamed request
    `stream_options.include_usage` is forced on.

    That last one is an accounting requirement, not a nicety. An OpenAI-compatible
    stream carries no usage block unless the caller asks for it, so without the
    injection every streamed request would settle at zero — a full refund of the
    hold, i.e. free tokens. When we inject it ourselves the terminal usage-only
    chunk is swallowed instead of forwarded, so a caller that did not ask for
    usage sees the stream it expected.

    Accounting is exclusive by construction: `_account_once` runs either the
    refund-and-release path or the settle path, never both, and only once. An
    invoke-time failure refunds with NO settle; a response that arrived settles
    against reported usage.
    """
    payload = body.model_dump(exclude_none=True)
    payload["model"] = entry.bedrock_model_id
    # Collapse to the modern spelling without letting `max_tokens`'s default
    # overwrite a value the caller actually asked for.
    payload.pop("max_tokens", None)
    payload["max_completion_tokens"] = _requested_max_output(body)

    # Inject usage reporting only when the caller did not already ask for it, so
    # we know whether the terminal usage chunk is ours to swallow or theirs to see.
    injected_usage = False
    if body.stream:
        opts = payload.get("stream_options")
        opts = dict(opts) if isinstance(opts, dict) else {}
        if not opts.get("include_usage"):
            opts["include_usage"] = True
            injected_usage = True
        payload["stream_options"] = opts

    # One latch for both terminal paths. Whichever fires first wins; the other is a
    # no-op. This is what keeps a 4xx on a streamed request from being refunded by
    # the error branch and then settled again by the generator's cleanup.
    accounted = {"done": False}

    def _refund_and_release() -> None:
        if accounted["done"]:
            return
        accounted["done"] = True
        tenants_repo.refund(user_id=user.user_id, tenant_id=user.org_id, tokens=reservation)
        _release_pool(tenants_repo)

    def _settle(input_tokens: int, output_tokens: int) -> None:
        if accounted["done"]:
            return
        accounted["done"] = True
        _settle_reservation_and_log(
            user=user, tenants_repo=tenants_repo, reservation=reservation,
            actual_input_tokens=input_tokens, actual_output_tokens=output_tokens,
            model_id=entry.bedrock_model_id, context=tenants_repo,
            requested_model=body.model, request_id=request_id,
        )

    def _fail(status: int, message: str) -> HTTPException:
        _refund_and_release()
        return HTTPException(status_code=status, detail={"error": {"message": message, "type": "api_error"}})

    if not body.stream:
        from core.error_handler import sanitize_exception_message

        client = _mantle_transport.sync_client(entry.bedrock_region)
        try:
            try:
                resp = client.post(_MANTLE_CHAT_PATH, json=payload)
            except Exception as e:  # noqa: BLE001 — transport failure is invoke-time
                raise _fail(502, sanitize_exception_message(str(e)))
            if resp.status_code >= 400:
                raise _fail(_mantle_status(resp.status_code), _mantle_transport.format_error(resp))
            # A 200 with an unparseable body is still an invoke-time failure: without
            # a usage block there is nothing to settle against, and letting the
            # exception escape would strand the hold and the pool slot.
            try:
                data = resp.json()
                input_tokens, output_tokens = _mantle_transport.extract_usage(data.get("usage") or {})
            except Exception as e:  # noqa: BLE001
                raise _fail(502, f"malformed upstream response: {sanitize_exception_message(str(e))}")
        finally:
            client.close()

        _settle(input_tokens, output_tokens)
        # Echo the client-facing alias, not the Bedrock ID: the caller asked for
        # `body.model` and an OpenAI client may compare the two.
        data["model"] = body.model
        return Response(content=json.dumps(data, ensure_ascii=False),
                        media_type="application/json", headers=corr)

    async def _proxy() -> AsyncGenerator[bytes, None]:
        # Async client on purpose. A sync generator would hold an anyio worker for
        # the whole stream (up to the 600 s timeout), and the workload this route
        # exists for is many concurrent streams — that starves every other sync
        # route, Converse included. The blocking DynamoDB accounting calls are the
        # part that must not run on the event loop, so they go to a thread.
        import asyncio

        input_tokens = output_tokens = 0
        client = _mantle_transport.async_client(entry.bedrock_region)
        try:
            try:
                async with client.stream("POST", _MANTLE_CHAT_PATH, json=payload) as resp:
                    if resp.status_code >= 400:
                        await resp.aread()
                        msg = _mantle_transport.format_error(resp)
                        await asyncio.to_thread(_refund_and_release)
                        yield f"data: {json.dumps({'error': {'message': msg, 'type': 'api_error'}})}\n\n".encode()
                        return
                    # Buffer whole SSE events (lines up to a blank separator) rather
                    # than forwarding line by line. An event can carry several
                    # `data:` lines plus `event:`/`id:`/`retry:` fields, and a `:`
                    # comment is a valid keepalive; re-framing per line would split
                    # or drop those. Buffering also gives the granularity needed to
                    # drop a usage event we asked for on the caller's behalf.
                    event: list[str] = []

                    def _render(lines: list[str]) -> bytes:
                        return ("\n".join(lines) + "\n\n").encode()

                    def _is_ours(lines: list[str]) -> bool:
                        """True when this event is the terminal usage-only chunk that
                        exists solely because we injected `include_usage`."""
                        nonlocal input_tokens, output_tokens
                        ours = False
                        for line in lines:
                            if not line.startswith("data:"):
                                continue
                            chunk = line[5:].strip()
                            if not chunk or chunk == "[DONE]":
                                continue
                            try:
                                parsed = json.loads(chunk) or {}
                            except json.JSONDecodeError:
                                continue
                            usage = parsed.get("usage")
                            if usage:
                                input_tokens, output_tokens = _mantle_transport.extract_usage(usage)
                                if injected_usage and not parsed.get("choices"):
                                    ours = True
                        return ours

                    async for raw in resp.aiter_lines():
                        if raw:
                            event.append(raw)
                            continue
                        if event and not _is_ours(event):
                            yield _render(event)
                        event = []
                    if event and not _is_ours(event):
                        yield _render(event)
            except Exception:  # noqa: BLE001 — transport/read failure mid-stream
                # Settle what the model already produced rather than refunding it
                # all; a partial stream is not a free request.
                await asyncio.to_thread(_settle, input_tokens, output_tokens)
                raise
            await asyncio.to_thread(_settle, input_tokens, output_tokens)
        finally:
            await client.aclose()
            # Backstop for a client disconnect (GeneratorExit) or any path above
            # that neither refunded nor settled. `_settle` is latched, so this
            # cannot double-account.
            await asyncio.to_thread(_settle, input_tokens, output_tokens)

    return StreamingResponse(
        _proxy(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no", **corr},
    )


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
    *,
    entry: Any,
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
        client = client_for_model(entry)
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
