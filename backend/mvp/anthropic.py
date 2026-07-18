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
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.logging import get_logger
from dynamo import UserTenantsRepository
from dynamo.user_tenants import CreditExhaustedError

from ._bedrock_clients import bedrock_runtime_client
from ._pipeline import (
    release_pool as _release_pool,
    reserve_credit,
    reserve_credit_for_model,
    settle_reservation_and_log,
)
from .authz import require_permission
from .deps import AuthenticatedUser, extract_model_pin, get_current_user, get_request_context
from .observability.context import RequestContext, response_headers as _corr_headers
from .models import _MAPPING as _ANTHROPIC_TO_BEDROCK, resolve_bedrock_model

# Backward-compatible aliases for tests that import the underscore-prefixed
# functions from this module. New code should import directly from
# `mvp._pipeline`.
_reserve_credit = reserve_credit
_reserve_credit_for_model = reserve_credit_for_model
_settle_reservation_and_log = settle_reservation_and_log


logger = get_logger(__name__)


def _selected_bedrock_model(context, default_model_id: str) -> str:
    """Bedrock model id the reservation actually chose (P0-11 cascade).

    The reservation carries `selected_model` (a client-facing name); when the
    cascade fell back to a different model we must invoke THAT one so the
    Bedrock call matches the pool debit and quota charge. Falls back to the
    already-resolved default id when there's no selection or it can't be
    resolved (e.g. an out-of-allowlist chain entry) — safer to invoke the model
    we validated up front than to fail the request.
    """
    selected = getattr(context, "selected_model", None)
    if not selected:
        return default_model_id
    try:
        return resolve_bedrock_model(selected)
    except ValueError:
        logger.warning("cascade_model_unresolvable", selected_model=selected)
        return default_model_id


def _saar_req_tool_result(body) -> bool:
    """Did THIS request carry a tool_result block? (tool-loop-lock trigger.)
    Fenced so a shape surprise never breaks the handler."""
    try:
        from .routing.saar import request_has_tool_result

        return request_has_tool_result(getattr(body, "messages", None))
    except Exception:  # noqa: BLE001
        return False


def _saar_finalize(sctx, response, context, committed_model_id, content_blocks, *,
                   request_had_tool_result: bool, cache_read_tokens: int) -> None:
    """Persist SAAR routing state + fire the provable claim + set the x-sc-saar-*
    replay headers. No-op when SAAR did not act (sctx is None). Entirely best-
    effort and money-neutral — the charge already settled. NEVER raises."""
    if sctx is None:
        return
    try:
        from .routing import saar as _saar
        from . import pricing as _pricing

        # The committed pricing_key + frozen rating version (so the claim's
        # micro-USD delta is recomputable — the "provable" property).
        pk = getattr(context, "pricing_key", None) or "default"
        snap = getattr(context, "rate_snapshot", None)
        rating_version = getattr(snap, "version", None) if snap else None
        had_tool_use = _saar.response_has_tool_use(content_blocks)
        # P0: warm_prefix_tokens is 0 (cache evidence lands in P1); the checkout
        # delta is therefore 0 and is honestly recorded as such in the claim.
        warm = int(sctx.decision.warm_prefix_tokens)
        try:
            delta = _pricing.saar_checkout_delta_microusd(
                pricing_key=pk, warm_prefix_tokens=warm
            )
        except Exception:  # noqa: BLE001 — pricing miss ⇒ claim delta 0.
            delta = 0
        _saar.saar_post_settle(
            sctx=sctx,
            committed_model=committed_model_id,
            response_had_tool_use=had_tool_use,
            request_had_tool_result=request_had_tool_result,
            warm_prefix_tokens=warm,
            rating_version=rating_version,
            checkout_delta_microusd=delta,
            pricing_key=pk,
        )
        # Replay headers on the response (money-neutral, observational).
        for k, v in _saar.replay_headers(
            replay_id=sctx.replay_id, decision=sctx.decision,
            chosen_model=committed_model_id, checkout_delta_microusd=delta,
        ).items():
            response.headers[k] = v
    except Exception as e:  # noqa: BLE001 — SAAR finalize must never break a settled request.
        try:
            logger.warning("saar_finalize_failed", error=str(e))
        except Exception:
            pass


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


def _decode_image_source(source: dict[str, Any]) -> dict[str, Any]:
    """Decode an Anthropic image source to Bedrock image block. Raises ValueError for unsupported."""
    import base64 as _b64
    import binascii

    src_type = source.get("type", "")
    if src_type == "base64":
        media = source.get("media_type", "image/png")
        fmt = media.split("/", 1)[-1] if "/" in media else media
        try:
            raw = _b64.b64decode(source.get("data", ""))
        except (binascii.Error, ValueError) as e:
            raise ValueError(f"invalid base64 image data: {e}") from e
        return {"image": {"format": fmt, "source": {"bytes": raw}}}
    raise ValueError(
        f"unsupported image source type '{src_type}'; only base64 data: URIs are accepted"
    )


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
                source = block.get("source", {})
                out.append(_decode_image_source(source))
            elif btype == "tool_use":
                out.append({
                    "toolUse": {
                        "toolUseId": block.get("id", ""),
                        "name": block.get("name", ""),
                        "input": block.get("input", {}),
                    }
                })
            elif btype == "tool_result":
                raw_content = block.get("content", [])
                tr_content = []
                if isinstance(raw_content, str):
                    tr_content.append({"text": raw_content})
                elif isinstance(raw_content, list):
                    for sub in raw_content:
                        if isinstance(sub, str):
                            tr_content.append({"text": sub})
                        elif isinstance(sub, dict):
                            if sub.get("type") == "text":
                                tr_content.append({"text": sub.get("text", "")})
                            elif sub.get("type") == "image":
                                tr_content.append(_decode_image_source(sub.get("source", {})))
                tr_entry: dict[str, Any] = {
                    "toolUseId": block.get("tool_use_id", ""),
                    "content": tr_content or [{"text": ""}],
                }
                if block.get("is_error"):
                    tr_entry["status"] = "error"
                out.append({"toolResult": tr_entry})
            elif btype == "thinking":
                entry: dict[str, Any] = {"text": block.get("thinking", "")}
                sig = block.get("signature")
                if sig:
                    entry["signature"] = sig
                out.append({"reasoningContent": {"reasoningText": entry}})
            else:
                out.append({"text": json.dumps(block)})
            if block.get("cache_control"):
                out.append({"cachePoint": {"type": "default"}})
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

    tools = getattr(body, "tools", None)
    if tools:
        tool_config: dict[str, Any] = {
            "tools": [
                {
                    "toolSpec": {
                        "name": t.get("name", "") if isinstance(t, dict) else "",
                        "description": t.get("description", "") if isinstance(t, dict) else "",
                        "inputSchema": {
                            "json": t.get("input_schema", {}) if isinstance(t, dict) else {}
                        },
                    }
                }
                for t in tools
            ]
        }
        tool_choice = getattr(body, "tool_choice", None)
        if isinstance(tool_choice, dict):
            tc_type = tool_choice.get("type", "auto")
            if tc_type == "any":
                tool_config["toolChoice"] = {"any": {}}
            elif tc_type == "tool":
                tool_config["toolChoice"] = {"tool": {"name": tool_choice.get("name", "")}}
            else:
                tool_config["toolChoice"] = {"auto": {}}
        kwargs["toolConfig"] = tool_config

    return kwargs


# ===== Credit reservation =====


# Minimum reservation per request. We always pre-debit at least this much.
# max_tokens alone only covers the output side, so we also reserve a
# margin for input tokens.
_MIN_RESERVATION_TOKENS = 1024


def _cache_tokens_from_usage(usage: dict[str, Any]) -> tuple[int, int]:
    """Extract (cache_read, cache_write) token counts from a Bedrock usage block.

    Bedrock's Converse usage reports prompt-cache activity as
    `cacheReadInputTokens` / `cacheWriteInputTokens` (0 or absent when caching
    is not used). Returning them lets settle price cached traffic at its own
    rate instead of billing it at zero. Bad/missing values collapse to 0.
    """
    def _int(v) -> int:
        try:
            n = int(v)
        except (TypeError, ValueError):
            return 0
        return n if n > 0 else 0

    return (
        _int(usage.get("cacheReadInputTokens")),
        _int(usage.get("cacheWriteInputTokens")),
    )


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
    request: Request,
    response: Response,
    # X-2 (2026-04 critical-sweep follow-up): enforce the scope layer
    # on the Bedrock invocation path. See list_models() for the full
    # rationale.
    user: AuthenticatedUser = Depends(require_permission("messages:send")),
    ctx: RequestContext = Depends(get_request_context),
):
    # Echo the correlation ids (server-assigned span, workflow run) so a client
    # can stitch its calls into one run. Set on `response`; StreamingResponse
    # below copies them into its own header block (P0-12).
    corr = _corr_headers(ctx)
    response.headers.update(corr)

    # Allowlist check first; reject with 400 before reserving credit.
    try:
        model_id = resolve_bedrock_model(body.model)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail={"type": "invalid_model", "message": str(e)},
        )

    fault_spec = request.headers.get("x-sc-fault")

    # P0-15: optional VSR hard pin. Absent -> today's behavior; present -> the
    # request is pinned to exactly that model (no cascade/fallback/downgrade),
    # validated against the allowlist + servability downstream (403 / 400).
    model_pin = extract_model_pin(request)

    # SAAR (session-aware routing). Runs ONLY when the client did not send an
    # explicit x-sc-model-pin (an explicit pin always wins — Fable design
    # precedence #1). saar_pre_reserve checks SAAR_ENABLED FIRST and fetches
    # routing config internally, so a flag-off deployment touches nothing here
    # (C1). It yields a HARD pin (tool-loop lock only → vsr_hard_model, disables
    # cascade) and/or a SOFT preference (sticky → heads the cascade but keeps
    # fallback, so SAAR can never turn a servable request into a 402/403). Fail-
    # open: sctx None ⇒ pre-SAAR cascade, nothing touched.
    sctx = None
    saar_hard = None
    saar_prefer = None
    saar_warm = 0
    if model_pin is None:
        from .routing import saar as _saar

        sctx = _saar.saar_pre_reserve(
            ctx=ctx,
            org_id=user.org_id,
            user_id=user.user_id,
            request_messages=body.messages,
        )
        if sctx is not None:
            saar_hard = sctx.decision.hard_model
            saar_prefer = sctx.decision.prefer_model
            saar_warm = int(sctx.decision.warm_prefix_tokens)

    # External VSR consult (task #13). Runs ONLY when the client sent no explicit
    # pin AND SAAR produced no hard lock (both are stronger, local signals). It
    # is off by default (EXTERNAL_VSR_ENABLED) and version-pinned + fail-open:
    # a missing/slow/version-skewed VSR yields no advice, routing = today. The
    # suggestion is fed into the SAME resolver inputs as an x-sc-model-pin, so it
    # passes the SAME allowlist/servability enforcement — the VSR is an untrusted
    # advisor, never a bypass.
    vsr_hard = None
    if model_pin is None and saar_hard is None:
        try:
            from .vsr import client as _vsr

            # Only DO anything (and only LOG anything) when the feature is on:
            # flag off => zero new work AND zero new log lines (dark ship). The
            # consult itself is fail-open and never on the money path.
            if _vsr.external_vsr_enabled():
                sk = ctx.session_key() if ctx else None
                result = _vsr.consult_ex(
                    tenant_id=user.org_id, session_key=sk, requested_model=body.model,
                )
                suggestion = result.suggestion
                # The decision the gateway can prove AT CONSULT TIME (routing
                # quality itself belongs to the VSR's own metrics stack). The
                # post-reserve enforcement split (allowlist reject) is a later
                # increment. classify_consult_decision is pure + unit-tested.
                decision = _vsr.classify_consult_decision(
                    result, saar_prefer_present=saar_prefer is not None)
                if suggestion is not None and suggestion.mode == "hard":
                    vsr_hard = suggestion.model
                elif (suggestion is not None and suggestion.mode == "prefer"
                      and saar_prefer is None):
                    saar_prefer = suggestion.model
                # tenant_id is auth-derived; we log the advised model id (bound
                # for the same allowlist enforcement as a client pin) but NEVER
                # the raw session key, the VSR url, or the tenant config blob.
                logger.info(
                    "vsr_consult_decision",
                    tenant_id=user.org_id,
                    decision=decision,
                    suggested_model=(suggestion.model if suggestion else None),
                    mode=(suggestion.mode if suggestion else None),
                    requested_model=body.model,
                )
        except Exception:  # noqa: BLE001 — advisory + fail-open; never break a request.
            vsr_hard = None

    reservation = _estimate_reservation_tokens(body)
    tenants_repo = _reserve_credit_for_model(
        user,
        reservation,
        model_name=body.model,
        input_tokens_est=max(reservation - body.max_tokens, 0),
        max_output_tokens=body.max_tokens,
        wire_protocol="messages",
        # Hard-pin precedence: explicit client pin > SAAR tool-loop lock >
        # external VSR hard suggestion. All three land on the same enforced pin.
        vsr_hard_model=model_pin or saar_hard or vsr_hard,
        saar_prefer_model=saar_prefer,
        saar_warm_prefix_tokens=saar_warm,
        # L5-d: carry request attribution so settle keys the ledger run-index on
        # the client's workflow_run_id (per-run billing).
        workflow_run_id=ctx.workflow_run_id if ctx else None,
        group_id=ctx.group_id if ctx else None,
        request_id=ctx.request_id if ctx else None,
    )

    # The reservation may have cascaded to a fallback model (P0-11). Invoke the
    # model the reservation actually priced/quota-charged, not the requested one,
    # so the Bedrock call, the pool debit, and the per-model quota all agree. The
    # cascade only ever selects a registry-resolvable `messages`-protocol model
    # (servability filter), so this re-resolve cannot land on the wrong model.
    model_id = _selected_bedrock_model(tenants_repo, model_id)

    if body.stream:
        # SAAR replay headers are known BEFORE the stream (replay id, chosen
        # model, phase, decision) since the reserve already committed the model;
        # the checkout delta is 0 in P0. Persist + claim happen in _on_finalized.
        saar_hdrs = {}
        if sctx is not None:
            try:
                from .routing import saar as _saar
                saar_hdrs = _saar.replay_headers(
                    replay_id=sctx.replay_id, decision=sctx.decision,
                    chosen_model=model_id, checkout_delta_microusd=0,
                )
            except Exception:  # noqa: BLE001 — headers are best-effort.
                saar_hdrs = {}
        return StreamingResponse(
            _stream_messages(body, model_id, user, tenants_repo, reservation, fault_spec=fault_spec, ctx=ctx, sctx=sctx),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                **saar_hdrs,
                **corr,
            },
        )

    kwargs = _build_bedrock_kwargs(body, model_id)
    try:
        resp = _bedrock_client().converse(**kwargs)
    except ClientError as e:
        # On a Bedrock error nothing was billed; refund the full reservation
        # AND release the pool hold (release_pool is a no-op when unpooled).
        tenants_repo.refund(
            user_id=user.user_id, tenant_id=user.org_id, tokens=reservation
        )
        _release_pool(tenants_repo)
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
        _release_pool(tenants_repo)
        raise

    usage = resp.get("usage", {})
    input_tokens = int(usage.get("inputTokens", 0))
    output_tokens = int(usage.get("outputTokens", 0))
    cache_read, cache_write = _cache_tokens_from_usage(usage)
    _settle_reservation_and_log(
        user=user,
        tenants_repo=tenants_repo,
        reservation=reservation,
        actual_input_tokens=input_tokens,
        actual_output_tokens=output_tokens,
        model_id=model_id,
        context=tenants_repo,
        actual_cache_read_tokens=cache_read,
        actual_cache_write_tokens=cache_write,
    )

    content_blocks: list[dict[str, Any]] = []
    for block in resp.get("output", {}).get("message", {}).get("content", []):
        if "text" in block:
            content_blocks.append({"type": "text", "text": block["text"]})
        elif "toolUse" in block:
            tu = block["toolUse"]
            content_blocks.append({
                "type": "tool_use",
                "id": tu.get("toolUseId", ""),
                "name": tu.get("name", ""),
                "input": tu.get("input", {}),
            })

    stop_reason_bedrock = resp.get("stopReason", "end_turn")
    stop_reason = _map_stop_reason(stop_reason_bedrock)

    # SAAR: persist the session's new routing state + fire the provable claim, and
    # echo the x-sc-saar-* replay headers. All best-effort / money-neutral (the
    # settle above already committed the charge). Only runs when SAAR chose to act.
    _saar_finalize(
        sctx, response, tenants_repo, model_id, content_blocks,
        request_had_tool_result=_saar_req_tool_result(body),
        cache_read_tokens=cache_read,
    )

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
    fault_spec: Optional[str] = None,
    ctx: Optional[RequestContext] = None,
    sctx=None,
) -> AsyncGenerator[bytes, None]:
    """Delegation shim — forwards to `_budget_flow.run_stream`.

    Closures resolve module globals (`_bedrock_client`, `_settle_reservation_and_log`,
    `_release_pool`) AT CALL TIME so existing monkeypatches pass straight through.
    """
    import time as _time

    from . import _budget_flow
    from ._wire import anthropic_wire as wire

    # Resolve the request_id ONCE so RouteRequest and the span record agree.
    request_id = ctx.request_id if ctx else f"msg_{uuid.uuid4().hex[:12]}"
    _routed_box: dict = {}          # filled by _invoke once routing commits
    _started_at_ms = int(_time.time() * 1000)

    async def _invoke(*, body, model_id):
        from .routing import route_stream as _route
        from .routing.types import RouteRequest

        kwargs = _build_bedrock_kwargs(body, model_id)
        kwargs.pop("modelId", None)

        # P0-12: propagate the edge-minted correlation ids as opaque pass-through
        # (the routing layer must NOT read them).
        req = RouteRequest(
            alias=model_id,
            payload=kwargs,
            tenant_id=user.org_id,
            request_id=request_id,
            span_id=(ctx.span_id if ctx else None),
            group_id=(ctx.group_id if ctx else None),
            workflow_run_id=(ctx.workflow_run_id if ctx else None),
            fault_spec=fault_spec,
        )

        routed = await _route(req)
        _routed_box["routed"] = routed  # commit-time routing facts for the span
        return {"stream": routed.events}

    def _on_finalized(status: str, acc) -> None:
        # P0-13/14: runs on the event loop right after the finalizer claim is
        # won (money-neutral; see _budget_flow._notify + the Z3 emit proofs).
        # Builds the frozen span draft from commit-time routing facts and hands
        # off to the fire-and-forget writer. When routing never committed
        # (invoke_error), committed_* stay empty and breaker_stage is "unknown".
        # Import here (not at the request-path top) so nothing observability-
        # related can raise into the request before the hook's own swallow (F1).
        # The SIGNALS import is deliberately deferred into the P0-16 try block
        # below (N1): it pulls a private cross-package symbol, and an ImportError
        # here would take the AUTHORITATIVE span emit down with the best-effort
        # signal — inverting the record hierarchy.
        from .observability.store import SpanDraft, emit_span_and_rollup

        routed = _routed_box.get("routed")
        target = routed.target if routed else None
        attempts = routed.attempt_facts if routed else []
        draft = SpanDraft(
            tenant_id=user.org_id,          # auth-derived; NEVER a client header
            request_id=request_id,
            span_id=(ctx.span_id if ctx else None) or request_id,
            group_id=(ctx.group_id if ctx else None),
            workflow_run_id=(ctx.workflow_run_id if ctx else None),
            model_alias=body.model,
            committed_model_id=(target.model_id if target else ""),
            committed_region=(getattr(target, "region", "") if target else ""),
            breaker_stage=(routed.breaker_stage if routed else "unknown"),
            attempts_total=len(attempts),
            targets_distinct=len({a.target for a in attempts}),
            stream=True,
            started_at_ms=_started_at_ms,
        )
        emit_span_and_rollup(draft, status, acc)

        # P0-16 (learning-signals seam): a write-only routing signal for the
        # FUTURE offline evaluator. Rides the SAME at-most-once finalizer claim
        # as the span (this closure runs once), takes no second claim, and is
        # fire-and-forget (never raises/blocks; money path untouched). The
        # partial/cancel classification is derived from the SAME acc snapshot
        # semantics the store uses (saw_final_usage), NOT from acc attributes
        # that don't exist. First-event latency comes from the committed
        # attempt's record. `route_exhausted` refinement of `status` is deferred
        # with the span-status rename (needs a run_stream money-path increment).
        try:
            from .learning.signals import category_for_model, emit_signal

            saw_final = bool(getattr(acc, "saw_final_usage", False))
            # N2: dedupe targets by (model_id, region) value, not object identity
            # or hashability — always hashable, and value equality is what a
            # "distinct chain hop" means (robust even if Target ever changes).
            targets_distinct = len({
                (getattr(a.target, "model_id", ""), getattr(a.target, "region", ""))
                for a in attempts
            })
            # F3a: first-event latency belongs to the COMMITTED attempt only —
            # attempts[-1] is the success record (route_stream measures t0->first
            # peeked event = TTFB). Zero when routing never committed, so we never
            # report a failed attempt's latency under this name.
            committed_latency = (
                int(getattr(attempts[-1], "latency_ms", 0) or 0)
                if (target and attempts) else 0
            )
            # F2: chain position = number of DISTINCT chain hops ATTEMPTED before
            # the committed one. Same-target retries append extra AttemptRecords,
            # so len(attempts)-1 overcounts; targets_distinct-1 is the true hop
            # index. N5 (semantics, documented for the consumer): this counts
            # hops that produced an AttemptRecord — a breaker-open hop skipped
            # WITHOUT an attempt is not counted, so this is "attempted-hop index",
            # not "resolved-chain index". -1 == routing never committed.
            chain_position = (targets_distinct - 1) if (target and routed) else -1
            emit_signal(
                tenant_id=user.org_id,      # auth-derived; NEVER a client header
                group_id=(ctx.group_id if ctx else None) or "",
                workflow_run_id=(ctx.workflow_run_id if ctx else None) or "",
                span_id=(ctx.span_id if ctx else None) or request_id,
                category=category_for_model(body.model, target.model_id if target else ""),
                committed_model_id=(target.model_id if target else ""),
                committed_region=(getattr(target, "region", "") if target else ""),
                cost_tier=(getattr(target, "cost_tier", 0) if target else 0),
                chain_position_served=chain_position,
                status=status,
                usage_is_partial=not saw_final,
                canceled_by_client=(status == "client_disconnect" and not saw_final),
                output_tokens=int(getattr(acc, "output_tokens", 0) or 0),
                latency_first_event_ms=committed_latency,
                attempts_total=len(attempts),
                targets_distinct=targets_distinct,
                breaker_stage=(routed.breaker_stage if routed else "unknown"),
            )
        except Exception as _sig_exc:  # noqa: BLE001 — F6: never-raises stays
            # LOCAL to this block. N2a: log (guarded) so a SYSTEMATIC failure
            # (kwargs drift, import error) isn't invisible — it fails 100% of the
            # time otherwise, silently.
            try:
                logger.warning("routing_signal_block_failed", error=str(_sig_exc))
            except Exception:
                pass

        # SAAR: persist the session's routing state + fire the provable claim on
        # the SAME at-most-once finalizer claim (money-neutral, fire-and-forget).
        # Anthropic signals a tool call via stop_reason == "tool_use", so the next
        # turn's phase is derived from that. No-op when SAAR did not act.
        if sctx is not None:
            try:
                from .routing import saar as _saar
                from . import pricing as _pricing

                committed = (target.model_id if target else model_id)
                pk = getattr(tenants_repo, "pricing_key", None) or "default"
                snap = getattr(tenants_repo, "rate_snapshot", None)
                rating_version = getattr(snap, "version", None) if snap else None
                had_tool_use = str(getattr(acc, "stop_reason", "")) == "tool_use"
                warm = int(sctx.decision.warm_prefix_tokens)
                try:
                    delta = _pricing.saar_checkout_delta_microusd(
                        pricing_key=pk, warm_prefix_tokens=warm
                    )
                except Exception:  # noqa: BLE001
                    delta = 0
                _saar.saar_post_settle(
                    sctx=sctx,
                    committed_model=committed,
                    response_had_tool_use=had_tool_use,
                    request_had_tool_result=_saar_req_tool_result(body),
                    warm_prefix_tokens=warm,
                    rating_version=rating_version,
                    checkout_delta_microusd=delta,
                    pricing_key=pk,
                )
            except Exception as _saar_exc:  # noqa: BLE001 — never-raises stays local.
                try:
                    logger.warning("saar_stream_finalize_failed", error=str(_saar_exc))
                except Exception:
                    pass

    class _AnthropicAdapter:
        def __init__(self):
            self.state = wire.AnthropicStreamState(model=body.model)

        def prologue(self):
            return wire.stream_prologue(self.state)

        def render_event(self, event):
            from . import _converse_types as t
            if isinstance(event, (t.Usage, t.MessageStop)):
                list(wire.render_stream_event(event, self.state))
                return ()
            return wire.render_stream_event(event, self.state)

        def epilogue(self):
            return wire.stream_epilogue(self.state)

        def error_event(self, message):
            return wire.error_event(message)

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
        adapter=_AnthropicAdapter(),
        on_finalized=_on_finalized,
    ):
        yield frame


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
