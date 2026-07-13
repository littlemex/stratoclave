"""Backend-agnostic money orchestration (layer a).

Owns the ONE canonical reserve → invoke → settle skeleton, the two error paths
(invoke-time full-refund+release vs mid-stream partial-settle), and the streaming
finally-settle guard. Knows nothing about Converse or mantle — drives injected
callables for invoke/settle/release so any backend can plug in.

The streaming generator's control flow is the exact skeleton moved VERBATIM from
`mvp.anthropic._stream_messages`; the only structural difference is that the
side-effects (bedrock call, settle, release) arrive as arguments rather than as
module-global lookups. This is the dependency-injection seam that lets step 1a's
delegation shim pass closures resolving at call time — so existing monkeypatches
on `anth._bedrock_client` / `anth._settle_reservation_and_log` /
`anth._release_pool` pass straight through without touching the test files.
"""
from __future__ import annotations

from typing import Any, AsyncGenerator, Callable

from . import _converse_core as core
from . import _converse_types as t
from ._wire import anthropic_wire as wire


async def run_stream(
    *,
    body: Any,
    model_id: str,
    model_alias: str,
    user: Any,
    tenants_repo: Any,
    reservation: int,
    invoke_stream: Callable[..., Any],
    settle: Callable[..., Any],
    release: Callable[..., Any],
) -> AsyncGenerator[bytes, None]:
    """Streaming budget flow — the settle-once invariant is explicit.

    Parameters
    ----------
    invoke_stream : callable(**bedrock_kwargs) -> resp with resp["stream"]
        The Bedrock converse_stream call (offloaded to a thread by the caller
        or inside this function via asyncio.to_thread).
    settle : callable(**kwargs) -> None
        Wraps `settle_reservation_and_log`.
    release : callable(ctx) -> None
        Wraps `release_pool`.

    The two error paths are DISTINCT:
      - invoke-time failure: refund + release, NO settle.
      - mid-stream failure: partial settle, NO release.
    """
    import asyncio
    import uuid

    from botocore.exceptions import ClientError

    from core.error_handler import sanitize_exception_message

    message_id = f"msg_{uuid.uuid4().hex[:24]}"
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    settled = False

    state = wire.AnthropicStreamState(model=model_alias, message_id=message_id)

    try:
        # prologue: message_start + content_block_start
        for frame in wire.stream_prologue(state):
            yield frame

        # invoke Bedrock
        try:
            resp = await asyncio.to_thread(invoke_stream, body=body, model_id=model_id)
        except ClientError as e:
            # ---- invoke-time failure ----
            # Refund BEFORE yielding so a disconnect at the error frame
            # cannot convert this into a settle path.
            tenants_repo.refund(
                user_id=user.user_id, tenant_id=user.org_id, tokens=reservation
            )
            release(tenants_repo)
            settled = True
            for frame in wire.error_event(sanitize_exception_message(str(e))):
                yield frame
            return

        # iterate the normalized event stream
        stop_reason_bedrock = None
        try:
            async for event in core._aiter_blocking_stream(resp.get("stream", [])):
                if "contentBlockDelta" in event:
                    delta_obj = event["contentBlockDelta"].get("delta", {})
                    text = delta_obj.get("text", "")
                    if text:
                        for frame in wire.render_stream_event(
                            t.ContentTextDelta(index=0, text=text), state
                        ):
                            yield frame
                elif "messageStop" in event:
                    stop_reason_bedrock = event["messageStop"].get("stopReason")
                    state.stop_reason = stop_reason_bedrock
                elif "metadata" in event:
                    usage = event["metadata"].get("usage", {})
                    input_tokens = int(usage.get("inputTokens", input_tokens))
                    output_tokens = int(usage.get("outputTokens", output_tokens))
                    cr, cw = core.cache_tokens_from_usage(usage)
                    cache_read_tokens = cr or cache_read_tokens
                    cache_write_tokens = cw or cache_write_tokens
                    state.input_tokens = input_tokens
                    state.output_tokens = output_tokens
        except Exception as e:
            # ---- mid-stream failure ----
            for frame in wire.error_event(sanitize_exception_message(str(e))):
                yield frame
            settle(
                user=user,
                tenants_repo=tenants_repo,
                reservation=reservation,
                actual_input_tokens=input_tokens,
                actual_output_tokens=output_tokens,
                model_id=model_id,
                context=tenants_repo,
                actual_cache_read_tokens=cache_read_tokens,
                actual_cache_write_tokens=cache_write_tokens,
            )
            settled = True
            return

        # epilogue: content_block_stop + message_delta + message_stop
        for frame in wire.stream_epilogue(state):
            yield frame

        # success settle
        settle(
            user=user,
            tenants_repo=tenants_repo,
            reservation=reservation,
            actual_input_tokens=input_tokens,
            actual_output_tokens=output_tokens,
            model_id=model_id,
            context=tenants_repo,
            actual_cache_read_tokens=cache_read_tokens,
            actual_cache_write_tokens=cache_write_tokens,
        )
        settled = True
    finally:
        if not settled:
            settle(
                user=user,
                tenants_repo=tenants_repo,
                reservation=reservation,
                actual_input_tokens=input_tokens,
                actual_output_tokens=output_tokens,
                model_id=model_id,
                context=tenants_repo,
                actual_cache_read_tokens=cache_read_tokens,
                actual_cache_write_tokens=cache_write_tokens,
            )
