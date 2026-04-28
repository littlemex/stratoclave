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

Credit (pessimistic reservation):
  - リクエスト受付時に max_tokens (+ 想定 input) を atomic に先取り (reserve)
    → 残高不足なら即 402。並列 N 本でも TOCTOU なしで確実にブロック。
  - Bedrock 呼び出し完了後、実消費 (input+output) との差額を refund で戻す。
  - エラー時も finally で全額 refund (課金が発生していないため)。
  - UsageLog は実消費が確定した時に 1 回だけ記録する。
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
from pydantic import BaseModel, Field

from core.logging import get_logger
from dynamo import UsageLogsRepository, UserTenantsRepository
from dynamo.user_tenants import CreditExhaustedError

from .deps import AuthenticatedUser, get_current_user
from .models import _MAPPING as _ANTHROPIC_TO_BEDROCK, resolve_bedrock_model


logger = get_logger(__name__)
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
    # Claude Opus/Sonnet 4.x accept up to 64K output tokens on Bedrock.
    # Claude Desktop Cowork defaults to `max_tokens=64000`, so anything
    # below that rejects legitimate clients at the proxy layer. The cap
    # still guards `_estimate_reservation_tokens` against unbounded input.
    max_tokens: int = Field(default=4096, ge=1, le=65536)
    temperature: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    top_p: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    top_k: Optional[int] = Field(default=None, ge=1, le=500)
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


# ===== クレジット予約 =====


# Reservation の最小値。1 リクエストあたり少なくともこの額を先取りする。
# max_tokens だけだと output 側しかカバーできないため、input 分の余裕も含める。
_MIN_RESERVATION_TOKENS = 1024


def _estimate_reservation_tokens(body: AnthropicMessagesRequest) -> int:
    """Bedrock 呼び出し前に先取りする token 数を見積もる.

    Anthropic の max_tokens は output の上限だが、input_tokens も含めて課金される。
    input の厳密な tokenization は行わず、シンプルに文字数ベースで粗見積もり
    (BPE ではないため概算; refund で差額は戻るので過大見積もりでも問題ない)。
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

    # ざっくり 3 文字 = 1 token (日英混在の保守見積もり)。
    input_estimate = max(char_count // 3, 0)
    reservation = body.max_tokens + input_estimate
    return max(reservation, _MIN_RESERVATION_TOKENS)


def _reserve_credit(
    user: AuthenticatedUser, reservation_tokens: int
) -> UserTenantsRepository:
    """リクエスト処理の入口で reservation_tokens 分を atomic に確保.

    残高不足なら 402 Payment Required を返す (Anthropic API の credit_exhausted 相当)。
    """
    repo = UserTenantsRepository()
    repo.ensure(user_id=user.user_id, tenant_id=user.org_id)
    try:
        repo.reserve(
            user_id=user.user_id,
            tenant_id=user.org_id,
            tokens=reservation_tokens,
        )
    except CreditExhaustedError:
        remaining = repo.remaining_credit(user.user_id, user.org_id)
        raise HTTPException(
            status_code=402,
            detail={
                "type": "credit_exhausted",
                "message": (
                    "Insufficient credit balance for this request. "
                    "Contact your admin."
                ),
                "remaining_credit": remaining,
                "reservation_required": reservation_tokens,
            },
        )
    return repo


# ===== 非ストリーミング =====


@router.post("/v1/messages")
def messages(
    body: AnthropicMessagesRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    # model allowlist を先にチェック (credit 予約の前に 400 で弾く)
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
        # Bedrock エラー時は課金が発生していないため全額 refund
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


# ===== ストリーミング =====


async def _stream_messages(
    body: AnthropicMessagesRequest,
    model_id: str,
    user: AuthenticatedUser,
    tenants_repo: UserTenantsRepository,
    reservation: int,
) -> AsyncGenerator[bytes, None]:
    """ストリーミング経路.

    入口で reservation 済みのため途中の credit 枯渇チェックは不要。
    完了時 (正常 / 異常どちらも) に実消費で reservation を settle する。
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
            resp = _bedrock_client().converse_stream(**kwargs)
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
            # Bedrock 側は呼ばれていないため全額 refund
            tenants_repo.refund(
                user_id=user.user_id, tenant_id=user.org_id, tokens=reservation
            )
            settled = True
            return

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
            # 途中までトークンが確定している場合はその分だけ settle、残りは refund
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

        # 6. 実消費で reservation を settle
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
        # クライアント切断等で途中で generator が閉じられた場合の保険。
        # 未 settle のままだと reservation がリークするので、実消費 (0 でも可) で必ず決算する。
        if not settled:
            _settle_reservation_and_log(
                user=user,
                tenants_repo=tenants_repo,
                reservation=reservation,
                actual_input_tokens=input_tokens,
                actual_output_tokens=output_tokens,
                model_id=model_id,
            )


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


def _settle_reservation_and_log(
    *,
    user: AuthenticatedUser,
    tenants_repo: UserTenantsRepository,
    reservation: int,
    actual_input_tokens: int,
    actual_output_tokens: int,
    model_id: str,
) -> None:
    """Reservation と実消費の差額を決算し、UsageLog を記録する.

    - actual <= reservation なら diff 分を refund。
    - actual > reservation なら reservation 超過分を追加で reserve する
      (通常は max_tokens + input 見積もりで余裕を持たせているため起きにくい)。
    - UsageLog は 1 リクエストあたり必ず 1 件記録する (append-only 監査証跡)。

    ここに silent pass は置かない。例外は呼び出し側に伝播させる。
    """
    actual = max(actual_input_tokens + actual_output_tokens, 0)
    diff = reservation - actual
    if diff > 0:
        tenants_repo.refund(
            user_id=user.user_id, tenant_id=user.org_id, tokens=diff
        )
    elif diff < 0:
        # 超過分をベストエフォートで追加確保。残高不足でも既に Bedrock 呼び出しは完了しており
        # UsageLog は記録する必要があるため、ここでは HTTPException にせず監査ログに残す。
        # (確保失敗は次回リクエストで 402 になる。監査は UsageLogs + credit_overrun event。)
        overrun = -diff
        try:
            tenants_repo.reserve(
                user_id=user.user_id,
                tenant_id=user.org_id,
                tokens=overrun,
            )
        except CreditExhaustedError:
            # 残高不足で追加確保できなかった場合は credit_used をクランプで total まで埋める。
            # これで UsageLogs 合計と credit_used の乖離を最小化する。
            item = tenants_repo.get(user.user_id, user.org_id)
            clamped_gap = 0
            uncovered = overrun
            if item is not None:
                total_credit = int(item.get("total_credit", 0))
                used = int(item.get("credit_used", 0))
                clamped_gap = max(total_credit - used, 0)
                if clamped_gap > 0:
                    try:
                        tenants_repo.reserve(
                            user_id=user.user_id,
                            tenant_id=user.org_id,
                            tokens=clamped_gap,
                        )
                        uncovered = overrun - clamped_gap
                    except CreditExhaustedError:
                        # 他の並列リクエストが同時に埋めて再度失敗、clamp 額はそのまま uncovered 扱い
                        clamped_gap = 0
            # 乖離監査イベント。reconciliation ジョブ / alert 対象。
            logger.warning(
                "credit_overrun",
                user_id=user.user_id,
                tenant_id=user.org_id,
                model_id=model_id,
                reservation=reservation,
                actual=actual,
                overrun=overrun,
                clamped=clamped_gap,
                uncovered=uncovered,
            )

    UsageLogsRepository().record(
        tenant_id=user.org_id,
        user_id=user.user_id,
        user_email=user.email,
        model_id=model_id,
        input_tokens=actual_input_tokens,
        output_tokens=actual_output_tokens,
    )
