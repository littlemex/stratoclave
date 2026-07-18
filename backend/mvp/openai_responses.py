"""OpenAI Responses API compatible endpoint.

POST /openai/v1/responses
    Accepts an OpenAI Responses-API request and forwards it to Bedrock's
    OpenAI-compatible endpoint at `bedrock-mantle.{region}.api.aws/openai/v1`.
    Supports both non-streaming and `stream: true` (SSE pass-through).

GET /openai/v1/models
    Returns the OpenAI-family entries from the model registry. The shape
    mirrors OpenAI's `/v1/models` response (`{"data": [...], "object":
    "list"}`) so codex / openai SDK clients can probe model availability.

Credit semantics are identical to the Anthropic Messages route — the
shared `mvp._pipeline` module owns the reserve / settle / log flow. The
only OpenAI-specific bits in this module are:

  - request shape (Responses API: `input`, `reasoning`, `max_output_tokens`)
  - reservation multiplier driven by `reasoning.effort` (xhigh runs can
    emit 8x the output tokens of a no-effort run)
  - bedrock-mantle wire transport (httpx + short-lived bearer token from
    `aws-bedrock-token-generator.provide_token`)
  - SSE event-name parsing (`response.completed` → final `usage`)

All three caller authentication paths (Cognito password, Vouch-by-STS,
`sk-stratoclave-*`) reach this route through `mvp.deps.get_current_user`
and are gated by the new `responses:send` scope.
"""
from __future__ import annotations

import json
import os
import time
from datetime import timedelta
from typing import Any, AsyncGenerator, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.error_handler import sanitize_exception_message
from core.logging import get_logger
from dynamo import UserTenantsRepository

from ._pipeline import (
    release_pool as _release_pool,
    reserve_credit,
    reserve_credit_for_model,
    settle_reservation_and_log,
)
from .authz import require_permission
from .deps import AuthenticatedUser, extract_model_pin, get_request_context
from .models import ModelEntry, _REGISTRY, resolve_model
from .observability.context import RequestContext, response_headers as _corr_headers


logger = get_logger(__name__)
router = APIRouter(tags=["mvp-openai-responses"])


# ---------------------------------------------------------------------------
# Feature flag — true rollback gate
# ---------------------------------------------------------------------------
# `CODEX_ENABLED` is checked at request time (not module load) so an
# operator can flip the flag via ECS task-definition env without re-importing
# the module. Matches the `ENABLE_WAF` / `ENABLE_ECS_EXEC` pattern in
# `iac/bin/iac.ts`.

def _codex_enabled() -> bool:
    return os.getenv("CODEX_ENABLED", "false").lower() == "true"


# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------
# We intentionally do NOT validate the full Responses-API surface. Bedrock
# is the source of truth for what fields the upstream model accepts; we
# pass extra fields through (`extra="allow"`) and only enforce the bits
# that affect *our* invariants: model allowlist, body size, image/file
# rejection, and the reservation math inputs.

_MAX_INPUT_CHARS = 200_000          # mirrors Anthropic route's body cap
_MAX_INPUT_ITEMS = 500              # absurd-upper-bound guard
_MAX_TOTAL_INPUT_TEXT_CHARS = 200_000  # aggregate cap across list items
_MIN_RESERVATION_TOKENS_OPENAI = 8192


class OpenAIResponsesRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = Field(min_length=1, max_length=256)
    input: Any
    reasoning: Optional[dict[str, Any]] = None
    max_output_tokens: int = Field(default=4096, ge=1, le=65536)
    stream: bool = False

    @field_validator("input")
    @classmethod
    def _validate_input(cls, v: Any) -> Any:
        # Reject unsupported block types up-front. Image/file inputs are
        # not blocked at the IAM layer (bedrock-mantle accepts them) but
        # token accounting for image tokens is not modelled by stratoclave
        # yet — letting them through would silently undercount credit.
        #
        # The per-element + aggregate length checks are belt-and-suspenders
        # against the ASGI body cap in `main.MaxBodySizeASGIMiddleware`:
        # the middleware bounds total request bytes, but a future relaxation
        # of that cap should not silently widen the per-route surface here.
        if isinstance(v, str):
            if len(v) > _MAX_INPUT_CHARS:
                raise ValueError(
                    f"input exceeds maximum length of {_MAX_INPUT_CHARS} characters"
                )
            return v
        if isinstance(v, list):
            if len(v) > _MAX_INPUT_ITEMS:
                raise ValueError(
                    f"input exceeds maximum item count of {_MAX_INPUT_ITEMS}"
                )
            total_text_chars = 0
            for item in v:
                if not isinstance(item, dict):
                    continue
                # Top-level item.type can be the unsupported types directly.
                if item.get("type") in ("input_image", "input_file"):
                    raise ValueError(
                        "image/file inputs are not supported by this proxy (MVP)"
                    )
                # Or nested under content[].
                content = item.get("content")
                blocks = content if isinstance(content, list) else None
                # Walk the text-bearing blocks, enforce per-block + aggregate caps.
                for block in blocks or []:
                    if isinstance(block, dict):
                        if block.get("type") in ("input_image", "input_file"):
                            raise ValueError(
                                "image/file inputs are not supported by this proxy (MVP)"
                            )
                        text = block.get("text", "")
                        if isinstance(text, str):
                            if len(text) > _MAX_INPUT_CHARS:
                                raise ValueError(
                                    f"input element exceeds {_MAX_INPUT_CHARS} characters"
                                )
                            total_text_chars += len(text)
                    elif isinstance(block, str):
                        if len(block) > _MAX_INPUT_CHARS:
                            raise ValueError(
                                f"input element exceeds {_MAX_INPUT_CHARS} characters"
                            )
                        total_text_chars += len(block)
                    if total_text_chars > _MAX_TOTAL_INPUT_TEXT_CHARS:
                        raise ValueError(
                            f"aggregate input text exceeds "
                            f"{_MAX_TOTAL_INPUT_TEXT_CHARS} characters"
                        )
            return v
        # Anything else (None, ints, etc.) is rejected by Bedrock; let it
        # through so the upstream error message is the canonical one.
        return v


# ---------------------------------------------------------------------------
# Reservation math
# ---------------------------------------------------------------------------
# Reasoning effort can amplify output tokens by ~8x at xhigh. Reserving
# only `max_output_tokens` would routinely under-debit and force the
# `_settle_reservation_and_log` clamp path — the multiplier is the
# pessimistic upfront debit so refunds dominate over overruns.

_REASONING_MULTIPLIERS: dict[str, int] = {
    "low": 1,
    "medium": 2,
    "high": 4,
    "xhigh": 8,
}


def _estimate_reservation_tokens(body: OpenAIResponsesRequest) -> int:
    """Estimate the upfront credit debit for a Responses request.

    Uses the same `chars // 3` heuristic as the Anthropic route for input
    tokens (BPE-free, deliberately conservative for mixed JP/EN text).
    Output budget is `max_output_tokens × reasoning_multiplier`, floored
    at `_MIN_RESERVATION_TOKENS_OPENAI = 8192` to keep tiny requests
    from racing through the floor and leaving credit unreserved on a
    runaway reasoning trace.
    """
    chars = 0
    if isinstance(body.input, str):
        chars = len(body.input)
    elif isinstance(body.input, list):
        for item in body.input:
            if isinstance(item, dict):
                content = item.get("content", item)
                blocks = content if isinstance(content, list) else [content]
                for block in blocks:
                    if isinstance(block, dict) and block.get("type") == "input_text":
                        text = block.get("text", "")
                        if isinstance(text, str):
                            chars += len(text)
                    elif isinstance(block, str):
                        chars += len(block)

    multiplier = _reasoning_multiplier(body)

    return max(
        (chars // 3) + body.max_output_tokens * multiplier,
        _MIN_RESERVATION_TOKENS_OPENAI,
    )


def _reasoning_multiplier(body: "OpenAIResponsesRequest") -> int:
    """Reasoning-effort output multiplier (1/2/4/8) for a request.

    Extracted so both the token reservation estimate and the dollar pool cost
    estimate use the same multiplier for the output leg.
    """
    effort = "none"
    if body.reasoning:
        raw = body.reasoning.get("effort")
        if isinstance(raw, str):
            effort = raw.lower()
    return _REASONING_MULTIPLIERS.get(effort, 1)


# ---------------------------------------------------------------------------
# bedrock-mantle client
# ---------------------------------------------------------------------------
# `provide_token(region=..., expiry=timedelta(seconds=900))` mints a
# short-lived bearer (15 min cap; library default is 1h with a 12h max).
# The 15-min cap is intentional: the bearer lives in the ECS task heap as
# a plain string; a smaller window bounds the blast radius after a task
# compromise. A SigV4-from-task-role migration is tracked as a P1
# follow-up — see plan's Out of scope.

_DEFAULT_TOKEN_TTL = timedelta(seconds=900)


def _mint_bearer_token(region: str) -> str:
    """Mint a short-lived bearer token for `bedrock-mantle.{region}.api.aws`."""
    # Imported lazily so that the module loads even when the dependency is
    # not yet installed (e.g. dev environments running just the Anthropic
    # tests). The route-time check below ensures the import error surfaces
    # as an HTTP 503 rather than crashing the whole worker.
    try:
        from aws_bedrock_token_generator import provide_token  # type: ignore
    except ImportError as exc:  # pragma: no cover — covered at deploy time
        raise HTTPException(
            status_code=503,
            detail=(
                "OpenAI Responses route is enabled but "
                "aws-bedrock-token-generator is not installed. "
                "Add it to backend/requirements.txt."
            ),
        ) from exc
    try:
        return provide_token(region=region, expiry=_DEFAULT_TOKEN_TTL)
    except TypeError:
        # Older versions of the library may not support `expiry` kwarg.
        # Falling back keeps us functional, but the bearer then defaults
        # to 1 h — the SigV4 migration follow-up addresses this case too.
        logger.warning(
            "bedrock_token_generator_no_expiry",
            region=region,
            note="library version does not support expiry kwarg; using default TTL",
        )
        return provide_token(region=region)


def _mantle_client(region: str) -> httpx.AsyncClient:
    """Build an httpx async client targeting bedrock-mantle in `region`."""
    token = _mint_bearer_token(region)
    return httpx.AsyncClient(
        base_url=f"https://bedrock-mantle.{region}.api.aws/openai/v1",
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(600.0, connect=10.0),
    )


# ---------------------------------------------------------------------------
# Usage extraction
# ---------------------------------------------------------------------------
# bedrock-mantle returns Responses-API-shaped JSON. We guard against the
# Chat-Completions field names because some intermediate proxies and the
# OpenAI SDK normalise inconsistently — defaulting to 0 on missing keys
# is safer than relying on either name being present.

def _extract_usage(usage: dict[str, Any]) -> tuple[int, int]:
    """Return `(input_tokens, output_tokens)` from a Responses `usage` block.

    `output_tokens_details.reasoning_tokens` is a SUBSET of `output_tokens`
    in the Responses API contract; do not add it separately.
    """
    if not isinstance(usage, dict):
        return 0, 0
    input_tokens = int(
        usage.get("input_tokens") or usage.get("prompt_tokens", 0) or 0
    )
    output_tokens = int(
        usage.get("output_tokens") or usage.get("completion_tokens", 0) or 0
    )
    return input_tokens, output_tokens


def _previous_response_id(body: OpenAIResponsesRequest) -> Optional[str]:
    """The `previous_response_id` this request references, if any. The Responses
    request model allows extra fields, so the continuation id (non-portable
    provider state) arrives as an extra attr / model_extra entry. Returns a
    non-empty string or None — the trigger for SAAR's provider-state lock."""
    v = getattr(body, "previous_response_id", None)
    if v is None:
        extra = getattr(body, "model_extra", None) or {}
        v = extra.get("previous_response_id")
    return v if isinstance(v, str) and v.strip() else None


def _response_id(data: dict[str, Any]) -> Optional[str]:
    """The `id` a Responses result minted (the referenceable continuation id).
    Its presence means the NEXT turn that references it must hard-lock to the
    backend that produced it (provider-state)."""
    rid = data.get("id") if isinstance(data, dict) else None
    return rid if isinstance(rid, str) and rid.strip() else None


# ---------------------------------------------------------------------------
# Error sanitization
# ---------------------------------------------------------------------------
# Both the non-streaming response body and SSE `event: error` lines are
# fed through `core.error_handler.sanitize_exception_message` before they
# reach the client. The sanitizer scrubs ARNs, account IDs, and internal
# request IDs that bedrock-mantle's error envelopes can include.

def _format_mantle_error(resp: httpx.Response) -> str:
    """Extract and sanitize an error message from a non-2xx mantle response."""
    try:
        body = resp.json()
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                msg = err.get("message")
                if isinstance(msg, str) and msg:
                    return sanitize_exception_message(msg)
    except Exception:
        pass
    return sanitize_exception_message(resp.text[:500])


def _sanitize_sse_error_line(line: str) -> str:
    """Sanitize an SSE `data:` line that follows an `event: error` line.

    The line is parsed as JSON, the `error.message` field is sanitized in
    place, and the line is re-encoded. On any parse failure the original
    line is returned — `sanitize_exception_message` is a regex sweep, so
    a malformed payload that bypasses parsing is still passed through it
    as a string of last resort.
    """
    if not line.startswith("data:"):
        return line
    payload_str = line[5:].lstrip()
    try:
        payload = json.loads(payload_str)
    except (json.JSONDecodeError, TypeError):
        return f"data: {sanitize_exception_message(payload_str)}"
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            msg = err.get("message")
            if isinstance(msg, str):
                err["message"] = sanitize_exception_message(msg)
                payload["error"] = err
                return f"data: {json.dumps(payload, ensure_ascii=False)}"
    return line


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/openai/v1/models")
def list_openai_models(
    _user: AuthenticatedUser = Depends(require_permission("responses:send")),
) -> dict[str, Any]:
    """Return the OpenAI-family entries from the model registry."""
    if not _codex_enabled():
        raise HTTPException(
            status_code=503,
            detail="OpenAI Responses API is not enabled on this deployment",
        )
    now = int(time.time())
    data = []
    for entry in _REGISTRY:
        if entry.provider != "openai":
            continue
        # Surface every alias as its own row so SDK clients that probe
        # for either short or fully-qualified IDs both succeed.
        for alias in entry.aliases:
            data.append(
                {
                    "id": alias,
                    "object": "model",
                    "created": now,
                    "owned_by": "stratoclave",
                }
            )
    return {"object": "list", "data": data}


def _saar_finalize_responses(
    sctx,
    response: Response,
    *,
    committed_model: str,
    minted_response_id: Optional[str],
) -> None:
    """Persist SAAR routing state + emit replay headers for a Responses turn.
    ``minted_response_id`` is the id THIS response actually produced (or None);
    it is stored so the next turn can only lock by echoing it back exactly.
    No-op when SAAR did not act (sctx is None). Entirely best-effort: a failure
    here never affects the response (money-neutrality + fail-open)."""
    if sctx is None:
        return
    try:
        from .routing import saar as _saar

        _saar.saar_post_settle(
            sctx=sctx,
            committed_model=committed_model,
            response_had_tool_use=False,          # Responses tool calls are a P1 concern
            request_had_tool_result=False,
            minted_response_id=minted_response_id,
        )
        for k, v in _saar.replay_headers(
            replay_id=sctx.replay_id, decision=sctx.decision,
            chosen_model=committed_model,
        ).items():
            response.headers[k] = v
    except Exception:  # noqa: BLE001 — observability/persist must never break a request.
        pass


@router.post("/openai/v1/responses")
async def create_response(
    body: OpenAIResponsesRequest,
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(require_permission("responses:send")),
    ctx: RequestContext = Depends(get_request_context),
):
    # P0-12: echo the correlation ids so a client can stitch calls into a run.
    corr = _corr_headers(ctx)
    response.headers.update(corr)

    # P0-15: optional VSR hard pin. wire_protocol="responses" so a pin that
    # resolves to a non-responses model is rejected (400), never misrouted.
    model_pin = extract_model_pin(request)

    if not _codex_enabled():
        raise HTTPException(
            status_code=503,
            detail="OpenAI Responses API is not enabled on this deployment",
        )

    # Allowlist check before credit reservation, mirroring the Anthropic route.
    try:
        entry = resolve_model(body.model)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail={"type": "invalid_model", "message": str(e)},
        )
    if entry.wire_protocol != "responses":
        # The model exists but speaks a different wire protocol (e.g. a
        # Claude entry typed in by mistake). Reject rather than silently
        # routing to the wrong path.
        raise HTTPException(
            status_code=400,
            detail={
                "type": "invalid_model",
                "message": (
                    f"model '{body.model}' uses the {entry.wire_protocol} "
                    "protocol; use the matching route instead"
                ),
            },
        )

    # SAAR pre-pass (session-aware routing). Fail-open: sctx None => pre-SAAR
    # cascade, nothing touched. This is the Responses route's provider-state
    # lock: a request carrying `previous_response_id` references non-portable
    # continuation state that lives only on the backend that minted it, so a
    # stored provider-state phase hard-locks it back to the origin model.
    sctx = None
    saar_hard = None
    saar_prefer = None
    saar_warm = 0
    prev_response_id = _previous_response_id(body)
    try:
        from .routing import saar as _saar

        sctx = _saar.saar_pre_reserve(
            ctx=ctx,
            org_id=user.org_id,
            user_id=user.user_id,
            request_messages=[],  # Responses input is not the Converse message list
            previous_response_id=prev_response_id,
        )
        if sctx is not None:
            saar_hard = sctx.decision.hard_model
            saar_prefer = sctx.decision.prefer_model
            saar_warm = int(sctx.decision.warm_prefix_tokens)
    except Exception:  # noqa: BLE001 — SAAR must never break the request.
        sctx = None

    reservation = _estimate_reservation_tokens(body)
    _multiplier = _reasoning_multiplier(body)
    tenants_repo = reserve_credit_for_model(
        user,
        reservation,
        model_name=body.model,
        input_tokens_est=max(reservation - body.max_output_tokens * _multiplier, 0),
        max_output_tokens=body.max_output_tokens,
        effort_multiplier=_multiplier,
        wire_protocol="responses",
        # Hard-pin precedence: explicit client pin > SAAR provider-state lock.
        vsr_hard_model=model_pin or saar_hard,
        saar_prefer_model=saar_prefer,
        saar_warm_prefix_tokens=saar_warm,
        # L5-d: per-run billing attribution.
        workflow_run_id=ctx.workflow_run_id if ctx else None,
        group_id=ctx.group_id if ctx else None,
        request_id=ctx.request_id if ctx else None,
    )

    # The reservation may have cascaded to a fallback model (P0-11). Invoke the
    # model actually priced/quota-charged. The cascade's servability filter only
    # ever selects a registry-resolvable `responses`-protocol model, so the
    # selection is always servable on this route (a cross-protocol / typo'd chain
    # entry is dropped before it can win). Re-resolve defensively all the same.
    _selected = getattr(tenants_repo, "selected_model", None)
    if _selected and _selected != body.model:
        try:
            _sel_entry = resolve_model(_selected)
            if _sel_entry.wire_protocol == "responses":
                entry = _sel_entry
            else:  # pragma: no cover — filtered out upstream
                logger.warning("cascade_model_wrong_protocol",
                               selected_model=_selected,
                               wire_protocol=_sel_entry.wire_protocol)
        except ValueError:  # pragma: no cover — filtered out upstream
            logger.warning("cascade_model_unresolvable", selected_model=_selected)

    if body.stream:
        return StreamingResponse(
            _stream_response(body, entry, user, tenants_repo, reservation,
                             request_id=ctx.request_id if ctx else None,
                             sctx=sctx),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                **corr,
            },
        )

    # Non-streaming path.
    payload = body.model_dump(exclude_none=True)
    payload["stream"] = False
    try:
        async with _mantle_client(entry.bedrock_region) as client:
            resp = await client.post("/responses", json=payload)
    except httpx.HTTPError as e:
        tenants_repo.refund(
            user_id=user.user_id, tenant_id=user.org_id, tokens=reservation
        )
        _release_pool(tenants_repo)
        raise HTTPException(
            status_code=502,
            detail=f"Bedrock OpenAI error: {sanitize_exception_message(str(e))}",
        )
    except Exception:
        tenants_repo.refund(
            user_id=user.user_id, tenant_id=user.org_id, tokens=reservation
        )
        _release_pool(tenants_repo)
        raise

    if resp.status_code >= 400:
        tenants_repo.refund(
            user_id=user.user_id, tenant_id=user.org_id, tokens=reservation
        )
        _release_pool(tenants_repo)
        # Map upstream 4xx to our 502 (the client did not directly cause
        # this — it's a downstream/IAM/region issue from the proxy's
        # perspective). 4xx-vs-5xx upstream classification is left to the
        # error message after sanitisation.
        raise HTTPException(
            status_code=502,
            detail=f"Bedrock OpenAI error: {_format_mantle_error(resp)}",
        )

    data = resp.json()
    input_tokens, output_tokens = _extract_usage(data.get("usage", {}))
    settle_reservation_and_log(
        user=user,
        tenants_repo=tenants_repo,
        reservation=reservation,
        actual_input_tokens=input_tokens,
        actual_output_tokens=output_tokens,
        model_id=entry.bedrock_model_id,
        context=tenants_repo,
        # Key the UsageLogs row on the request id for the offline VSR reconcile join.
        request_id=ctx.request_id if ctx else None,
    )
    # SAAR post-settle: persist the session's routing state. A minted response id
    # marks non-portable continuation state, so the NEXT turn that references it
    # hard-locks to this backend (provider-state). Emit replay headers. Best-
    # effort throughout — never affects the response.
    _saar_finalize_responses(
        sctx, response, committed_model=entry.bedrock_model_id,
        minted_response_id=_response_id(data),
    )
    return data


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

async def _stream_response(
    body: OpenAIResponsesRequest,
    entry: ModelEntry,
    user: AuthenticatedUser,
    tenants_repo: UserTenantsRepository,
    reservation: int,
    request_id: Optional[str] = None,
    sctx=None,
) -> AsyncGenerator[bytes, None]:
    """Stream the Responses-API SSE feed back to the caller.

    Implementation note — why bytes, not lines
    -----------------------------------------
    The previous version of this generator iterated the upstream stream
    via ``resp.aiter_lines()`` and re-emitted each line with a single
    ``"\\n"`` appended. That was fragile in two ways the codex CLI hit
    in the wild ("stream closed before response.completed"):

      1. ``aiter_lines`` discards the original line terminator (``\\n``,
         ``\\r\\n``, or ``\\r``) and there is no guarantee that the
         upstream's blank-line event boundaries survive a re-emit that
         always adds exactly one ``\\n`` per line. Clients expect a
         literal blank line (``\\n\\n``) between events; if even one
         boundary collapses, the SSE parser stalls until the connection
         drops, surfacing as the "stream closed" error.
      2. The Responses API splits long JSON payloads onto multiple
         ``data:`` lines per the SSE spec — the parser is supposed to
         join them with ``\\n`` and then JSON-decode the whole thing.
         The line-by-line parse used here for usage extraction missed
         every multi-line ``response.completed`` event, which silently
         settled with usage=(0, 0).

    The fix is to switch to byte-level pass-through with an
    event-boundary buffer:

      - chunks are appended to a buffer and the buffer is scanned for
        ``\\n\\n`` / ``\\r\\n\\r\\n`` event terminators;
      - each completed event is emitted *as the bytes the upstream
        sent* (preserving terminator, line endings, and any comment
        lines), with the only exception being ``event: error`` events
        which are still rebuilt through the sanitizer;
      - the same completed event is parsed in-memory to recover the
        ``response.completed`` usage block (concatenating multi-line
        data per the SSE spec).
    """
    input_tokens = 0
    output_tokens = 0
    settled = False
    # The provider continuation id the stream actually minted (from
    # response.completed). None unless a real completed event carried one — so a
    # cut / errored / id-less stream arms NO provider-state lock (Fable review §2).
    minted_id: Optional[str] = None

    payload = body.model_dump(exclude_none=True)
    payload["stream"] = True

    try:
        async with _mantle_client(entry.bedrock_region) as client:
            try:
                async with client.stream(
                    "POST", "/responses", json=payload
                ) as resp:
                    if resp.status_code >= 400:
                        # Read the body so the sanitizer has something to chew on.
                        body_text = (await resp.aread()).decode("utf-8", "replace")
                        sanitized = sanitize_exception_message(body_text[:500])
                        # Server-side audit: also log the upstream status +
                        # sanitized message so we can diagnose stream
                        # failures from the backend logs (the SSE error
                        # event reaches the client but their TUI usually
                        # only surfaces "stream disconnected").
                        logger.warning(
                            "bedrock_mantle_stream_4xx_5xx",
                            status_code=resp.status_code,
                            region=entry.bedrock_region,
                            model_id=entry.bedrock_model_id,
                            message=sanitized,
                        )
                        yield _sse_event(
                            "error",
                            {
                                "type": "error",
                                "error": {
                                    "type": "api_error",
                                    "message": f"Bedrock OpenAI error: {sanitized}",
                                },
                            },
                        )
                        tenants_repo.refund(
                            user_id=user.user_id,
                            tenant_id=user.org_id,
                            tokens=reservation,
                        )
                        _release_pool(tenants_repo)
                        settled = True
                        return

                    buffer = bytearray()
                    async for chunk in resp.aiter_bytes():
                        if not chunk:
                            continue
                        buffer.extend(chunk)
                        for raw_event in _drain_events(buffer):
                            out_bytes, usage, ev_id = _handle_sse_event(raw_event)
                            yield out_bytes
                            if usage is not None:
                                input_tokens, output_tokens = usage
                            if ev_id is not None:
                                minted_id = ev_id

                    # Flush any trailing bytes that were not terminated
                    # by a blank line. bedrock-mantle (or any hop in
                    # between) sometimes closes the chunked-transfer
                    # body before the final `\\n\\n` is flushed —
                    # that's the exact symptom of the codex
                    # "stream closed before response.completed" report.
                    # If the buffer still contains an SSE-shaped event
                    # (an `event:` and/or `data:` line), append a
                    # blank-line terminator so the client's SSE parser
                    # actually fires the terminal event before tearing
                    # down the connection. We only synthesize the
                    # terminator when one is missing — well-formed
                    # streams flow through unchanged.
                    if buffer:
                        trailing = bytes(buffer)
                        out_bytes, usage, ev_id = _handle_sse_event(trailing)
                        if ev_id is not None:
                            minted_id = ev_id
                        if out_bytes:
                            if not (
                                out_bytes.endswith(b"\n\n")
                                or out_bytes.endswith(b"\r\n\r\n")
                            ) and (
                                b"event:" in out_bytes or b"data:" in out_bytes
                            ):
                                # Choose the line ending the upstream
                                # already used; default to `\n` if we
                                # cannot tell. This keeps the byte
                                # mix consistent with what the client
                                # has been parsing all along.
                                if b"\r\n" in out_bytes:
                                    if out_bytes.endswith(b"\r\n"):
                                        out_bytes = out_bytes + b"\r\n"
                                    else:
                                        out_bytes = out_bytes + b"\r\n\r\n"
                                else:
                                    if out_bytes.endswith(b"\n"):
                                        out_bytes = out_bytes + b"\n"
                                    else:
                                        out_bytes = out_bytes + b"\n\n"
                                logger.warning(
                                    "bedrock_mantle_stream_unterminated_final_event",
                                    region=entry.bedrock_region,
                                    model_id=entry.bedrock_model_id,
                                    note=(
                                        "upstream closed before final "
                                        "blank-line terminator; "
                                        "synthesizing one to unblock "
                                        "the SSE client"
                                    ),
                                )
                            yield out_bytes
                        if usage is not None:
                            input_tokens, output_tokens = usage
                        buffer.clear()
            except httpx.HTTPError as e:
                yield _sse_event(
                    "error",
                    {
                        "type": "error",
                        "error": {
                            "type": "api_error",
                            "message": (
                                f"Bedrock OpenAI error: "
                                f"{sanitize_exception_message(str(e))}"
                            ),
                        },
                    },
                )
                tenants_repo.refund(
                    user_id=user.user_id,
                    tenant_id=user.org_id,
                    tokens=reservation,
                )
                _release_pool(tenants_repo)
                settled = True
                return

        settle_reservation_and_log(
            user=user,
            tenants_repo=tenants_repo,
            reservation=reservation,
            actual_input_tokens=input_tokens,
            actual_output_tokens=output_tokens,
            model_id=entry.bedrock_model_id,
            context=tenants_repo,
            request_id=request_id,
        )
        settled = True
        # SAAR post-settle (stream): persist routing state, storing the ACTUAL
        # response id the stream minted (from response.completed) — NOT a fixed
        # True. A stream that errored / was cut / minted no id stores minted_id=
        # None, so the next turn arms NO provider-state lock (Fable review §2: no
        # false lock). Replay headers can't be added post-hoc to an already-
        # flushed SSE response, so only the phase persist runs here.
        if sctx is not None:
            try:
                from .routing import saar as _saar

                _saar.saar_post_settle(
                    sctx=sctx,
                    committed_model=entry.bedrock_model_id,
                    response_had_tool_use=False,
                    request_had_tool_result=False,
                    minted_response_id=minted_id,
                )
            except Exception:  # noqa: BLE001 — best-effort, never breaks the stream.
                pass
    finally:
        # Defensive: if the generator was closed mid-stream (client drop,
        # cancellation), still settle so the reservation does not leak.
        if not settled:
            settle_reservation_and_log(
                user=user,
                tenants_repo=tenants_repo,
                reservation=reservation,
                actual_input_tokens=input_tokens,
                actual_output_tokens=output_tokens,
                model_id=entry.bedrock_model_id,
                context=tenants_repo,
                request_id=request_id,
            )


# ---------------------------------------------------------------------------
# SSE event-buffer helpers
# ---------------------------------------------------------------------------
# These exist so the proxy can preserve the upstream's exact SSE framing
# while still inspecting the (possibly multi-line) `data:` payload of
# selected events. They are intentionally lenient: any bytes that fail to
# decode as UTF-8 still flow back to the client unchanged — the parse path
# only triggers a rewrite for `event: error` events whose `data:` is valid
# JSON, and only triggers a usage read for `response.completed` events.


def _drain_events(buffer: bytearray) -> list[bytes]:
    """Pop every fully-terminated SSE event from `buffer` (in place).

    SSE event boundaries are blank lines: either `\\n\\n` or
    `\\r\\n\\r\\n` per the spec. We search for whichever appears first
    and slice up to and including it. Bytes that do not yet contain a
    terminator stay in the buffer for the next chunk.
    """
    events: list[bytes] = []
    while True:
        # Find the earliest event terminator. `find` returns -1 if absent.
        crlf = buffer.find(b"\r\n\r\n")
        lf = buffer.find(b"\n\n")
        if crlf == -1 and lf == -1:
            break
        if crlf == -1:
            cut = lf + 2
        elif lf == -1:
            cut = crlf + 4
        else:
            # Take the boundary that ends earliest in the buffer.
            cut = min(lf + 2, crlf + 4)
        events.append(bytes(buffer[:cut]))
        del buffer[:cut]
    return events


def _handle_sse_event(raw: bytes) -> tuple[bytes, Optional[tuple[int, int]], Optional[str]]:
    """Process one fully-buffered SSE event.

    Returns the bytes to forward to the client (usually `raw` itself —
    we are byte-transparent by default), an optional `(input_tokens,
    output_tokens)` extracted from `response.completed`, and the minted
    `response_id` from that same completed event (or None). The id is
    captured ONLY from `response.completed` — never from an error/failed/
    partial event — so the provider-state lock is armed only when a real,
    referenceable continuation was actually produced (Fable review §2: no
    false lock on a stream that errored / was cut / minted no id).

    The only event whose bytes we rewrite is `event: error`: its
    `data:` payload is JSON-decoded, `error.message` is sanitized, and
    the rebuilt frame replaces the original. This preserves the
    sanitizer guarantee for ARNs / account IDs in upstream errors
    (see test_openai_responses_credit.py::test_sse_error_event_sanitized_before_yield).
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Bedrock-mantle's stream is documented as UTF-8; if a chunk
        # somehow violated that we still want to forward it verbatim
        # rather than drop the frame. The codex parser will handle the
        # decode error itself.
        return raw, None, None

    event_name, data_payload = _parse_sse_frame(text)

    usage: Optional[tuple[int, int]] = None
    minted_id: Optional[str] = None
    if event_name == "response.completed" and data_payload is not None:
        try:
            obj = json.loads(data_payload)
        except (json.JSONDecodeError, TypeError):
            obj = None
        if isinstance(obj, dict):
            response_block = obj.get("response")
            if isinstance(response_block, dict):
                usage = _extract_usage(response_block.get("usage", {}))
                minted_id = _response_id(response_block)

    if event_name == "error":
        # A-03-sse: when the upstream error payload does not match the
        # expected JSON shape (`{"error": {"message": "..."}}`) the
        # sanitizer returns None, and previously we forwarded the raw
        # bytes as-is — defeating the redaction guarantee for ARNs /
        # account IDs. Fall back to a regex sweep over the entire data
        # payload (or the raw frame, if data was missing) so callers
        # never see an unsanitized error event.
        sanitized = (
            _sanitize_error_payload(data_payload)
            if data_payload is not None
            else None
        )
        if sanitized is not None:
            return f"event: error\ndata: {sanitized}\n\n".encode("utf-8"), usage, minted_id
        fallback = data_payload if data_payload is not None else text
        scrubbed = sanitize_exception_message(fallback)
        return (
            f"event: error\ndata: {json.dumps({'error': {'message': scrubbed}}, ensure_ascii=False)}\n\n"
        ).encode("utf-8"), usage, minted_id

    return raw, usage, minted_id


def _parse_sse_frame(text: str) -> tuple[Optional[str], Optional[str]]:
    """Return `(event_name, joined_data)` from an SSE event text.

    Per the SSE spec, multiple `data:` lines in the same event are
    joined with `"\\n"` before delivery to the client's parser. We
    follow that rule so a multi-line `response.completed` payload still
    JSON-decodes correctly. Lines starting with `:` are SSE comments
    and are ignored.
    """
    event_name: Optional[str] = None
    data_lines: list[str] = []
    # SSE accepts \n, \r\n, and \r as line endings. splitlines handles all.
    for raw_line in text.splitlines():
        if not raw_line or raw_line.startswith(":"):
            continue
        if raw_line.startswith("event:"):
            event_name = raw_line[len("event:") :].strip()
            continue
        if raw_line.startswith("data:"):
            # Strip exactly one leading space if present (per SSE spec
            # "field: value" — value is the bytes after the optional
            # single space).
            v = raw_line[len("data:") :]
            if v.startswith(" "):
                v = v[1:]
            data_lines.append(v)
    if not data_lines:
        return event_name, None
    return event_name, "\n".join(data_lines)


def _sanitize_error_payload(data_payload: str) -> Optional[str]:
    """Sanitize `error.message` inside an SSE error data payload.

    Returns the rebuilt JSON string on success, or `None` if there was
    nothing to sanitize and the caller should pass the original bytes
    through unchanged. A parse failure also returns `None` — the raw
    event is then forwarded verbatim and the regex sweep that
    `core.error_handler.sanitize_exception_message` performs at log
    time still applies on the server side.
    """
    try:
        obj = json.loads(data_payload)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    err = obj.get("error")
    if not isinstance(err, dict):
        return None
    msg = err.get("message")
    if not isinstance(msg, str):
        return None
    err["message"] = sanitize_exception_message(msg)
    obj["error"] = err
    return json.dumps(obj, ensure_ascii=False)


def _sse_event(event: str, data: dict[str, Any]) -> bytes:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")
