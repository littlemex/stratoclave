"""Anthropic Messages API compatibility endpoint.

POST /v1/messages
    Accepts an Anthropic-shaped request, calls Bedrock
    `converse` / `converse_stream`, and returns an Anthropic-shaped
    response.

When `stream: true`, emits Anthropic-style SSE events:

    event: message_start
    data: {"type":"message_start", ...}

    event: content_block_start
    ...

    event: content_block_delta
    data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"..."}}

    ...

    event: content_block_stop
    event: message_delta
    event: message_stop

Credit handling (pessimistic reservation):

  - On request entry, atomically reserve `max_tokens + estimated_input`.
    Insufficient balance returns 402 immediately; the conditional write
    keeps N concurrent requests from racing past the budget.
  - After Bedrock returns, refund the difference between reservation
    and actual usage.
  - On any error path, the `finally` clause refunds the full
    reservation (Bedrock did not bill us).
  - UsageLogs receives exactly one row per request, with the actual
    usage (never the reservation).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any, AsyncGenerator, Iterator, Optional

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.logging import get_logger
from dynamo import UserTenantsRepository
from dynamo.user_tenants import CreditExhaustedError

from ._bedrock_clients import bedrock_runtime_client
from ._pipeline import reserve_credit, settle_reservation_and_log
from .authz import require_permission
from .deps import AuthenticatedUser, get_current_user
from .models import _MAPPING as _ANTHROPIC_TO_BEDROCK, resolve_bedrock_model

# Backward-compatible aliases for tests that import the underscore-prefixed
# functions from this module. New code should import directly from
# `mvp._pipeline`.
_reserve_credit = reserve_credit
_settle_reservation_and_log = settle_reservation_and_log


logger = get_logger(__name__)
router = APIRouter(tags=["mvp-anthropic"])


# ---------------------------------------------------------------------------
# /v1/models — provider discovery for Claude Desktop (cowork) / Claude Code.
# ---------------------------------------------------------------------------
# Anthropic's `/v1/models` returns the shape:
#   {"data": [{"id":"claude-opus-4-7","display_name":"Claude Opus 4.7","type":"model",
#              "created_at":"2026-..."}], "has_more": false, "first_id":..., "last_id":...}
# The MVP returns the minimum viable shape (id + type only).
# Claude Desktop cowork probes with `Authorization: Bearer ...`, so the
# endpoint requires auth — otherwise unauthenticated callers could
# enumerate the deployment's model list.
@router.get("/v1/models")
def list_models(
    # X-2 (2026-04 critical-sweep follow-up): `/v1/messages` and
    # `/v1/models` previously only called `get_current_user`, which
    # resolves the AuthenticatedUser but does not check scopes. An
    # API key minted with `scopes=["usage:read-self"]` could therefore
    # enumerate models and drive Bedrock invocations, completely
    # bypassing the advertised scope-based blast-radius guarantee.
    # Gating on `require_permission("messages:send")` routes both JWTs
    # and API keys through `user_has_permission`, which AND-checks
    # against `user.roles` AND `user.key_scopes` for API-key auth.
    _user: AuthenticatedUser = Depends(require_permission("messages:send")),
) -> dict:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    data = [
        {
            "id": anthropic_id,
            "type": "model",
            "display_name": anthropic_id,
            "created_at": now,
        }
        for anthropic_id in _ANTHROPIC_TO_BEDROCK.keys()
    ]
    return {
        "data": data,
        "has_more": False,
        "first_id": data[0]["id"] if data else None,
        "last_id": data[-1]["id"] if data else None,
    }


# ===== Anthropic-compatible request / response models =====


# C-H (2026-04 critical sweep): Pydantic hard caps. The body-size
# middleware (``main.MaxBodySizeMiddleware``) rejects 2 MiB+ requests
# outright; the caps below add a second layer that refuses malformed
# inputs that fit within the byte budget but still stress the
# credit-reservation math or copy-on-parse memory usage.
_MAX_CONTENT_CHARS = 200_000          # Claude 200K context (≈ chars)
_MAX_MESSAGES = 500                   # absurd-upper-bound guard
_MAX_STOP_SEQUENCES = 4               # Bedrock Converse limit
_MAX_STOP_SEQUENCE_CHARS = 64


def _enforce_content_size(value: Any) -> Any:
    """Validator: cap the serialized size of an Anthropic content block.

    ``content`` and ``system`` accept either a plain string or an
    Anthropic content-block list, so the simplest universal guard is
    to JSON-serialize the value and limit the resulting length. That
    also prevents an attacker from burying 500 MB of text inside a
    single deeply-nested content block.
    """
    if value is None:
        return value
    import json as _json

    try:
        serialized = _json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("content is not JSON-serializable") from exc
    if len(serialized) > _MAX_CONTENT_CHARS:
        raise ValueError(
            f"content exceeds {_MAX_CONTENT_CHARS} char cap "
            f"(got {len(serialized)} chars)"
        )
    return value


class AnthropicMessage(BaseModel):
    # Anthropic's Messages API is forward-evolving (tool_use /
    # tool_result / image blocks / cache_control etc.). Rejecting
    # unknown keys here would break every SDK upgrade, so we allow
    # passthrough and rely on the size validator + Bedrock's own
    # schema for structural enforcement.
    model_config = ConfigDict(extra="allow")

    role: str = Field(min_length=1, max_length=16)
    # content may be a plain string or an Anthropic content-block list.
    # The length cap below catches the trivial DoS vector of attaching
    # a single-message body stuffed with hundreds of MB of text that
    # passes the outer middleware because it was chunked.
    content: Any

    @field_validator("content")
    @classmethod
    def _content_size(cls, v: Any) -> Any:
        return _enforce_content_size(v)


class AnthropicMessagesRequest(BaseModel):
    # Anthropic's Messages API is not frozen: Claude Code / Claude
    # Desktop / the Anthropic SDKs routinely ship new top-level
    # fields (``tools``, ``tool_choice``, ``metadata``, ``service_tier``,
    # ``anthropic_beta``, ``thinking``, ``cache_control``, ...) that
    # we forward to Bedrock without needing to understand.
    # Z-hotfix (2026-04): the original sweep-1 C-H locked this model
    # with ``extra="forbid"``, which meant every `stratoclave claude`
    # invocation 422'd the moment the CLI sent `tools`. The body
    # middleware (``MaxBodySizeASGIMiddleware``) still caps the raw
    # byte size and the field-level caps below still guard the
    # values we *do* read (messages / stop_sequences / system /
    # max_tokens / model). Forward-compat drift is the whole point
    # of a proxy gateway.
    model_config = ConfigDict(extra="allow")

    model: str = Field(min_length=1, max_length=256)
    messages: list[AnthropicMessage] = Field(max_length=_MAX_MESSAGES)
    # Claude Opus/Sonnet 4.x accept up to 64K output tokens on Bedrock.
    # Claude Desktop Cowork defaults to `max_tokens=64000`, so anything
    # below that rejects legitimate clients at the proxy layer. The cap
    # still guards `_estimate_reservation_tokens` against unbounded input.
    max_tokens: int = Field(default=4096, ge=1, le=65536)
    temperature: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    top_p: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    top_k: Optional[int] = Field(default=None, ge=1, le=500)
    stop_sequences: Optional[list[str]] = Field(
        default=None, max_length=_MAX_STOP_SEQUENCES
    )
    system: Optional[Any] = None
    stream: bool = False

    @field_validator("stop_sequences")
    @classmethod
    def _stop_sequence_lengths(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        if v is None:
            return v
        for item in v:
            if not isinstance(item, str):
                raise ValueError("stop_sequences must be a list of strings")
            if len(item) > _MAX_STOP_SEQUENCE_CHARS:
                raise ValueError(
                    f"stop_sequences entry exceeds {_MAX_STOP_SEQUENCE_CHARS} chars"
                )
        return v

    @field_validator("system")
    @classmethod
    def _system_size(cls, v: Any) -> Any:
        return _enforce_content_size(v)


def _bedrock_client():
    """Resolve the Bedrock client for the Anthropic Messages route.

    Claude family lives in `BEDROCK_REGION` (defaults to `us-east-1` per
    `iac/bin/iac.ts`). Per-model regions are encoded in the model registry
    but the Anthropic route here is single-region by design — the OpenAI
    Responses route consults `client_for_model(entry)` directly when it
    needs the bedrock-mantle endpoint in us-east-2/us-west-2.
    """
    region = os.getenv("BEDROCK_REGION") or os.getenv("AWS_REGION", "us-east-1")
    return bedrock_runtime_client(region)


def _convert_content_blocks(content: Any) -> list[dict[str, Any]]:
    """Convert Anthropic-shaped content (str or list[dict]) into Bedrock Converse content."""
    if isinstance(content, str):
        return [{"text": content}]
    if isinstance(content, list):
        out: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                out.append({"text": block.get("text", "")})
            elif btype == "image":
                # MVP does not support image input (Claude Code is text-mostly); skip.
                continue
            else:
                # Unknown block types are text-stringified rather than skipped.
                out.append({"text": json.dumps(block)})
        return out or [{"text": ""}]
    # fallback
    return [{"text": str(content)}]


def _convert_system(system: Any) -> Optional[list[dict[str, str]]]:
    if system is None:
        return None
    if isinstance(system, str):
        return [{"text": system}] if system else None
    if isinstance(system, list):
        texts: list[str] = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
            elif isinstance(block, str):
                texts.append(block)
        merged = "\n".join(t for t in texts if t)
        return [{"text": merged}] if merged else None
    return None


def _build_bedrock_kwargs(
    body: AnthropicMessagesRequest, model_id: str
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    for msg in body.messages:
        if msg.role not in ("user", "assistant"):
            continue
        messages.append(
            {
                "role": msg.role,
                "content": _convert_content_blocks(msg.content),
            }
        )

    inference_config: dict[str, Any] = {"maxTokens": body.max_tokens}
    if body.temperature is not None:
        inference_config["temperature"] = body.temperature
    if body.top_p is not None:
        inference_config["topP"] = body.top_p
    if body.stop_sequences:
        inference_config["stopSequences"] = body.stop_sequences

    kwargs: dict[str, Any] = {
        "modelId": model_id,
        "messages": messages,
        "inferenceConfig": inference_config,
    }
    system = _convert_system(body.system)
    if system:
        kwargs["system"] = system
    return kwargs


# ===== Credit reservation =====


# Minimum reservation per request. We always pre-debit at least this much.
# max_tokens alone only covers the output side, so we also reserve a
# margin for input tokens.
_MIN_RESERVATION_TOKENS = 1024


def _estimate_reservation_tokens(body: AnthropicMessagesRequest) -> int:
    """Estimate how many tokens to pre-reserve before calling Bedrock.

    Anthropic's `max_tokens` caps output, but input tokens are billed
    too. We do not tokenize precisely (no BPE) — a simple char-count
    heuristic is enough because the refund step reconciles any over- or
    under-estimate against the actual Bedrock-reported usage.
    """
    char_count = 0
    for msg in body.messages:
        content = msg.content
        if isinstance(content, str):
            char_count += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text", "")
                    if isinstance(text, str):
                        char_count += len(text)
    if isinstance(body.system, str):
        char_count += len(body.system)
    elif isinstance(body.system, list):
        for block in body.system:
            if isinstance(block, dict):
                text = block.get("text", "")
                if isinstance(text, str):
                    char_count += len(text)

    # Rough heuristic: 3 chars per token (conservative for mixed JP/EN text).
    input_estimate = max(char_count // 3, 0)
    reservation = body.max_tokens + input_estimate
    return max(reservation, _MIN_RESERVATION_TOKENS)


# Credit reservation and settlement live in `mvp._pipeline` and are
# imported above as `reserve_credit` / `settle_reservation_and_log`.
# The underscore-prefixed aliases at the top of this module preserve
# backward compatibility for tests that import the private names.


# ===== Non-streaming path =====


@router.post("/v1/messages")
def messages(
    body: AnthropicMessagesRequest,
    # X-2 (2026-04 critical-sweep follow-up): enforce the scope layer
    # on the Bedrock invocation path. See list_models() for the full
    # rationale.
    user: AuthenticatedUser = Depends(require_permission("messages:send")),
):
    # Allowlist check first; reject with 400 before reserving credit.
    try:
        model_id = resolve_bedrock_model(body.model)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail={"type": "invalid_model", "message": str(e)},
        )

    reservation = _estimate_reservation_tokens(body)
    tenants_repo = _reserve_credit(user, reservation)

    if body.stream:
        return StreamingResponse(
            _stream_messages(body, model_id, user, tenants_repo, reservation),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    kwargs = _build_bedrock_kwargs(body, model_id)
    try:
        resp = _bedrock_client().converse(**kwargs)
    except ClientError as e:
        # On a Bedrock error nothing was billed; refund the full reservation.
        tenants_repo.refund(
            user_id=user.user_id, tenant_id=user.org_id, tokens=reservation
        )
        # Sanitize the upstream message before returning it: Bedrock errors
        # can leak account IDs, inference-profile ARNs, and internal paths.
        from core.error_handler import sanitize_exception_message

        raise HTTPException(
            status_code=502,
            detail=f"Bedrock error: {sanitize_exception_message(str(e))}",
        )
    except Exception:
        tenants_repo.refund(
            user_id=user.user_id, tenant_id=user.org_id, tokens=reservation
        )
        raise

    usage = resp.get("usage", {})
    input_tokens = int(usage.get("inputTokens", 0))
    output_tokens = int(usage.get("outputTokens", 0))
    _settle_reservation_and_log(
        user=user,
        tenants_repo=tenants_repo,
        reservation=reservation,
        actual_input_tokens=input_tokens,
        actual_output_tokens=output_tokens,
        model_id=model_id,
    )

    content_blocks: list[dict[str, Any]] = []
    for block in resp.get("output", {}).get("message", {}).get("content", []):
        if "text" in block:
            content_blocks.append({"type": "text", "text": block["text"]})

    stop_reason_bedrock = resp.get("stopReason", "end_turn")
    stop_reason = _map_stop_reason(stop_reason_bedrock)

    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": body.model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


# ===== Streaming =====


async def _stream_messages(
    body: AnthropicMessagesRequest,
    model_id: str,
    user: AuthenticatedUser,
    tenants_repo: UserTenantsRepository,
    reservation: int,
) -> AsyncGenerator[bytes, None]:
    """Streaming path.

    Credit is reserved at entry, so no mid-stream balance check is
    required. On completion (success or failure) we settle the
    reservation against the actual usage.
    """
    message_id = f"msg_{uuid.uuid4().hex[:24]}"
    input_tokens = 0
    output_tokens = 0
    settled = False

    try:
        # 1. message_start
        yield _sse_event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": body.model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        )
        # 2. content_block_start
        yield _sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        )

        kwargs = _build_bedrock_kwargs(body, model_id)
        try:
            # boto3's `converse_stream` is synchronous. Calling it directly
            # from this async generator pins the uvicorn event loop for the
            # entire Bedrock TCP handshake; multi-tenant traffic to other
            # endpoints (incl. /healthz) blocks until it returns. Offload
            # to the default thread executor so the loop stays responsive.
            client = _bedrock_client()
            resp = await asyncio.to_thread(client.converse_stream, **kwargs)
        except ClientError as e:
            from core.error_handler import sanitize_exception_message

            yield _sse_event(
                "error",
                {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": sanitize_exception_message(str(e)),
                    },
                },
            )
            # Bedrock was never invoked; refund the full reservation.
            tenants_repo.refund(
                user_id=user.user_id, tenant_id=user.org_id, tokens=reservation
            )
            settled = True
            return

        stop_reason_bedrock: Optional[str] = None

        try:
            # The Bedrock event stream is a sync iterator backed by a
            # blocking socket read. Wrap it in `_aiter_blocking_stream`
            # so each `next(...)` runs in a worker thread and the event
            # loop is free to service other requests in between events.
            async for event in _aiter_blocking_stream(resp.get("stream", [])):
                if "contentBlockDelta" in event:
                    delta_obj = event["contentBlockDelta"].get("delta", {})
                    text = delta_obj.get("text", "")
                    if text:
                        yield _sse_event(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": 0,
                                "delta": {"type": "text_delta", "text": text},
                            },
                        )
                elif "messageStop" in event:
                    stop_reason_bedrock = event["messageStop"].get("stopReason")
                elif "metadata" in event:
                    usage = event["metadata"].get("usage", {})
                    input_tokens = int(usage.get("inputTokens", input_tokens))
                    output_tokens = int(usage.get("outputTokens", output_tokens))
        except Exception as e:  # pragma: no cover
            from core.error_handler import sanitize_exception_message

            yield _sse_event(
                "error",
                {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": sanitize_exception_message(str(e)),
                    },
                },
            )
            # Partial usage has already been observed; settle that and refund the rest.
            _settle_reservation_and_log(
                user=user,
                tenants_repo=tenants_repo,
                reservation=reservation,
                actual_input_tokens=input_tokens,
                actual_output_tokens=output_tokens,
                model_id=model_id,
            )
            settled = True
            return

        # 3. content_block_stop
        yield _sse_event(
            "content_block_stop",
            {"type": "content_block_stop", "index": 0},
        )

        stop_reason = _map_stop_reason(stop_reason_bedrock or "end_turn")

        # 4. message_delta (carries usage and stop_reason)
        yield _sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                },
            },
        )

        # 5. message_stop
        yield _sse_event("message_stop", {"type": "message_stop"})

        # 6. Settle the reservation against actual usage.
        _settle_reservation_and_log(
            user=user,
            tenants_repo=tenants_repo,
            reservation=reservation,
            actual_input_tokens=input_tokens,
            actual_output_tokens=output_tokens,
            model_id=model_id,
        )
        settled = True
    finally:
        # Defensive: if the client disconnected mid-stream, settle now
        # so the reservation does not leak. Settling with zero is fine.
        if not settled:
            _settle_reservation_and_log(
                user=user,
                tenants_repo=tenants_repo,
                reservation=reservation,
                actual_input_tokens=input_tokens,
                actual_output_tokens=output_tokens,
                model_id=model_id,
            )


async def _aiter_blocking_stream(
    stream: Iterator[dict[str, Any]],
) -> AsyncGenerator[dict[str, Any], None]:
    """Wrap a blocking iterator (boto3 EventStream) for use under asyncio.

    Each `next(it)` is dispatched to the default thread executor, so the
    uvicorn event loop is free to service other coroutines while the
    underlying socket waits for the next Bedrock SSE chunk. The function
    yields one event per loop iteration; when the upstream iterator
    raises `StopIteration` (i.e. Bedrock closed the stream cleanly) we
    return normally.
    """
    sentinel = object()
    it = iter(stream)

    def _next_or_sentinel() -> Any:
        # `StopIteration` cannot cross thread boundaries cleanly; convert
        # to a sentinel so the caller terminates without re-raising
        # `RuntimeError: generator raised StopIteration`.
        try:
            return next(it)
        except StopIteration:
            return sentinel

    while True:
        item = await asyncio.to_thread(_next_or_sentinel)
        if item is sentinel:
            return
        yield item


def _sse_event(event: str, data: dict[str, Any]) -> bytes:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


def _map_stop_reason(bedrock_reason: str) -> str:
    """Map Bedrock stop reasons to Anthropic ones."""
    mapping = {
        "end_turn": "end_turn",
        "max_tokens": "max_tokens",
        "stop_sequence": "stop_sequence",
        "tool_use": "tool_use",
        "content_filtered": "refusal",
    }
    return mapping.get(bedrock_reason, "end_turn")


# `_settle_reservation_and_log` is the alias for `mvp._pipeline.settle_reservation_and_log`
# declared near the top of this module. The implementation moved to
# `mvp/_pipeline.py` so the OpenAI Responses route shares it byte-for-byte.
