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
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.error_handler import sanitize_exception_message
from core.logging import get_logger
from dynamo import UserTenantsRepository

from ._pipeline import reserve_credit, settle_reservation_and_log
from .authz import require_permission
from .deps import AuthenticatedUser
from .models import ModelEntry, _REGISTRY, resolve_model


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

    effort = "none"
    if body.reasoning:
        raw = body.reasoning.get("effort")
        if isinstance(raw, str):
            effort = raw.lower()
    multiplier = _REASONING_MULTIPLIERS.get(effort, 1)

    return max(
        (chars // 3) + body.max_output_tokens * multiplier,
        _MIN_RESERVATION_TOKENS_OPENAI,
    )


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


@router.post("/openai/v1/responses")
async def create_response(
    body: OpenAIResponsesRequest,
    user: AuthenticatedUser = Depends(require_permission("responses:send")),
):
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

    reservation = _estimate_reservation_tokens(body)
    tenants_repo = reserve_credit(user, reservation)

    if body.stream:
        return StreamingResponse(
            _stream_response(body, entry, user, tenants_repo, reservation),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
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
        raise HTTPException(
            status_code=502,
            detail=f"Bedrock OpenAI error: {sanitize_exception_message(str(e))}",
        )
    except Exception:
        tenants_repo.refund(
            user_id=user.user_id, tenant_id=user.org_id, tokens=reservation
        )
        raise

    if resp.status_code >= 400:
        tenants_repo.refund(
            user_id=user.user_id, tenant_id=user.org_id, tokens=reservation
        )
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
) -> AsyncGenerator[bytes, None]:
    """Stream the Responses-API SSE feed back to the caller.

    The Bedrock SSE bytes are forwarded byte-for-byte with one exception:
    `event: error` payloads are intercepted and re-emitted with the
    `error.message` field passed through the sanitizer. Final usage is
    extracted from the `response.completed` event payload's
    `response.usage` block.
    """
    input_tokens = 0
    output_tokens = 0
    settled = False
    last_event_was_error = False

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
                        settled = True
                        return

                    async for raw_line in resp.aiter_lines():
                        # httpx strips the trailing newline; we add it back
                        # because SSE clients depend on the empty-line
                        # boundary between events.
                        line = raw_line

                        if line.startswith("event:"):
                            last_event_was_error = (
                                line[len("event:") :].strip() == "error"
                            )
                            yield (line + "\n").encode("utf-8")
                            continue

                        if last_event_was_error and line.startswith("data:"):
                            sanitized_line = _sanitize_sse_error_line(line)
                            yield (sanitized_line + "\n").encode("utf-8")
                            last_event_was_error = False
                            continue

                        # Empty line terminates the event; reset the flag.
                        if line == "":
                            last_event_was_error = False

                        yield (line + "\n").encode("utf-8")

                        if line.startswith("data:"):
                            try:
                                payload_obj = json.loads(line[5:].lstrip())
                            except (json.JSONDecodeError, TypeError):
                                continue
                            if (
                                isinstance(payload_obj, dict)
                                and payload_obj.get("type") == "response.completed"
                            ):
                                resp_obj = payload_obj.get("response")
                                if isinstance(resp_obj, dict):
                                    input_tokens, output_tokens = _extract_usage(
                                        resp_obj.get("usage", {})
                                    )
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
                settled = True
                return

        settle_reservation_and_log(
            user=user,
            tenants_repo=tenants_repo,
            reservation=reservation,
            actual_input_tokens=input_tokens,
            actual_output_tokens=output_tokens,
            model_id=entry.bedrock_model_id,
        )
        settled = True
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
            )


def _sse_event(event: str, data: dict[str, Any]) -> bytes:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")
