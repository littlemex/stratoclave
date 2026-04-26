"""Anthropic Messages API 互換エンドポイント.

POST /v1/messages
    Anthropic 形式のリクエストを受け取り、Bedrock Converse / ConverseStream を呼んで
    Anthropic 形式のレスポンスを返す.

ストリーミング (`stream: true`) の場合は Anthropic 形式の SSE を emit する:
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

Credit:
  - リクエスト受付時に残量 > 0 をチェック (0 以下は 403 CreditExhausted)
  - レスポンス完了時に input_tokens + output_tokens を原子的に減算
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, AsyncGenerator, Optional

import boto3
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from dynamo import UsageLogsRepository, UserTenantsRepository
from dynamo.user_tenants import CreditExhaustedError

from .deps import AuthenticatedUser, get_current_user
from .models import _MAPPING as _ANTHROPIC_TO_BEDROCK, resolve_bedrock_model


router = APIRouter(tags=["mvp-anthropic"])


# ---------------------------------------------------------------------------
# /v1/models — Claude Desktop (cowork) / Claude Code の provider discovery 用
# ---------------------------------------------------------------------------
# Anthropic の /v1/models は以下の shape を返す:
#   {"data": [{"id":"claude-opus-4-7","display_name":"Claude Opus 4.7","type":"model",
#              "created_at":"2026-..."}], "has_more": false, "first_id":..., "last_id":...}
# 本実装は MVP として minimum viable shape (id と type のみ) を返す.
# Claude Desktop cowork は Gateway auth scheme=Bearer で probe してくるため、
# 認証必須 (他ユーザーが Model list を覗ける問題を防ぐ).
@router.get("/v1/models")
def list_models(_user: AuthenticatedUser = Depends(get_current_user)) -> dict:
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


# ===== Anthropic 互換 API のリクエスト/レスポンス =====


class AnthropicMessage(BaseModel):
    role: str
    content: Any  # str or list[dict]


class AnthropicMessagesRequest(BaseModel):
    model: str
    messages: list[AnthropicMessage]
    max_tokens: int = 4096
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    stop_sequences: Optional[list[str]] = None
    system: Optional[Any] = None  # str or list[dict]
    stream: bool = False


def _bedrock_client():
    region = os.getenv("BEDROCK_REGION") or os.getenv("AWS_REGION", "us-east-1")
    return boto3.client("bedrock-runtime", region_name=region)


def _convert_content_blocks(content: Any) -> list[dict[str, Any]]:
    """Anthropic 形式の content (str or list[dict]) を Bedrock Converse の content に変換."""
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
                # MVP は画像非対応 (Claude Code は主にテキスト)。スキップする
                continue
            else:
                # その他の未知ブロックはテキスト化してスキップしない
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


# ===== クレジットチェック =====


def _check_credit_available(user: AuthenticatedUser) -> UserTenantsRepository:
    repo = UserTenantsRepository()
    repo.ensure(user_id=user.user_id, tenant_id=user.org_id)
    remaining = repo.remaining_credit(user.user_id, user.org_id)
    if remaining <= 0:
        raise HTTPException(
            status_code=403,
            detail={
                "type": "credit_exhausted",
                "message": "Credit balance is zero. Contact your admin.",
                "remaining_credit": 0,
            },
        )
    return repo


# ===== 非ストリーミング =====


@router.post("/v1/messages")
def messages(
    body: AnthropicMessagesRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    tenants_repo = _check_credit_available(user)
    model_id = resolve_bedrock_model(body.model)

    if body.stream:
        return StreamingResponse(
            _stream_messages(body, model_id, user, tenants_repo),
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
        raise HTTPException(status_code=502, detail=f"Bedrock error: {e}")

    usage = resp.get("usage", {})
    input_tokens = int(usage.get("inputTokens", 0))
    output_tokens = int(usage.get("outputTokens", 0))
    _record_and_deduct(user, tenants_repo, model_id, input_tokens, output_tokens)

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


# ===== ストリーミング =====


async def _stream_messages(
    body: AnthropicMessagesRequest,
    model_id: str,
    user: AuthenticatedUser,
    tenants_repo: UserTenantsRepository,
) -> AsyncGenerator[bytes, None]:
    message_id = f"msg_{uuid.uuid4().hex[:24]}"

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
        resp = _bedrock_client().converse_stream(**kwargs)
    except ClientError as e:
        yield _sse_event("error", {"type": "error", "error": {"type": "api_error", "message": str(e)}})
        return

    input_tokens = 0
    output_tokens = 0
    stop_reason_bedrock: Optional[str] = None

    try:
        for event in resp.get("stream", []):
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
            # contentBlockStart / contentBlockStop は Anthropic 側には 1 つのブロックに
            # 集約するのでスキップ (MVP)
    except Exception as e:  # pragma: no cover - ランタイムエラーの包み込み
        yield _sse_event("error", {"type": "error", "error": {"type": "api_error", "message": str(e)}})
        return

    # 3. content_block_stop
    yield _sse_event(
        "content_block_stop",
        {"type": "content_block_stop", "index": 0},
    )

    stop_reason = _map_stop_reason(stop_reason_bedrock or "end_turn")

    # 4. message_delta (usage と stop_reason を含める)
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

    # 6. 非同期イベント終了後に credit 減算 + UsageLog 記録
    try:
        _record_and_deduct(user, tenants_repo, model_id, input_tokens, output_tokens)
    except CreditExhaustedError:
        # 既に結果は返し終わっているため、次回リクエスト時に 403 となる
        pass


def _sse_event(event: str, data: dict[str, Any]) -> bytes:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


def _map_stop_reason(bedrock_reason: str) -> str:
    """Bedrock → Anthropic の stop_reason マッピング."""
    mapping = {
        "end_turn": "end_turn",
        "max_tokens": "max_tokens",
        "stop_sequence": "stop_sequence",
        "tool_use": "tool_use",
        "content_filtered": "refusal",
    }
    return mapping.get(bedrock_reason, "end_turn")


def _record_and_deduct(
    user: AuthenticatedUser,
    tenants_repo: UserTenantsRepository,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    total = max(input_tokens + output_tokens, 0)
    try:
        tenants_repo.deduct(
            user_id=user.user_id,
            tenant_id=user.org_id,
            tokens=total,
        )
    except CreditExhaustedError:
        # 減算失敗時も UsageLog は記録する (監査目的)
        pass
    UsageLogsRepository().record(
        tenant_id=user.org_id,
        user_id=user.user_id,
        user_email=user.email,
        model_id=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
