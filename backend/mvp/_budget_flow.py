"""Backend-agnostic money orchestration (layer a).

Owns the ONE canonical reserve → invoke → settle skeleton, the two error paths
(invoke-time full-refund+release vs mid-stream partial-settle), and the streaming
finally-settle guard. Knows nothing about Converse, mantle, or any specific wire
format — drives injected callables for invoke/settle/release AND an adapter
protocol for rendering frames.

The adapter protocol (`StreamAdapter`) is a simple set of callbacks the caller
provides. This lets both the Anthropic Messages wire and the OpenAI Chat
Completions wire share the single settle-once machine without either knowing
about the other.
"""
from __future__ import annotations

from typing import Any, AsyncGenerator, Callable, Iterable, Protocol


class StreamAdapter(Protocol):
    """Protocol for wire-format adapters driven by run_stream."""

    def prologue(self) -> Iterable[bytes]: ...
    def render_raw_event(self, event: dict[str, Any]) -> Iterable[bytes]: ...
    def epilogue(self) -> Iterable[bytes]: ...
    def error_event(self, message: str) -> Iterable[bytes]: ...


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
    adapter: StreamAdapter,
) -> AsyncGenerator[bytes, None]:
    """Streaming budget flow — the settle-once invariant is explicit.

    The two error paths are DISTINCT:
      - invoke-time failure: refund + release, NO settle.
      - mid-stream failure: partial settle, NO release.
    """
    import asyncio

    from botocore.exceptions import ClientError

    from core.error_handler import sanitize_exception_message

    from . import _converse_core as core

    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    settled = False

    try:
        for frame in adapter.prologue():
            yield frame

        try:
            resp = await asyncio.to_thread(invoke_stream, body=body, model_id=model_id)
        except (ClientError, Exception) as e:
            # ---- invoke-time failure ----
            # Refund BEFORE yielding so a disconnect at the error frame
            # cannot convert this into a settle path.
            tenants_repo.refund(
                user_id=user.user_id, tenant_id=user.org_id, tokens=reservation
            )
            release(tenants_repo)
            settled = True
            for frame in adapter.error_event(sanitize_exception_message(str(e))):
                yield frame
            return

        try:
            async for event in core._aiter_blocking_stream(resp.get("stream", [])):
                if "metadata" in event:
                    usage = event["metadata"].get("usage", {})
                    input_tokens = int(usage.get("inputTokens", input_tokens))
                    output_tokens = int(usage.get("outputTokens", output_tokens))
                    cr, cw = core.cache_tokens_from_usage(usage)
                    cache_read_tokens = cr or cache_read_tokens
                    cache_write_tokens = cw or cache_write_tokens
                for frame in adapter.render_raw_event(event):
                    yield frame
        except Exception as e:
            # ---- mid-stream failure ----
            for frame in adapter.error_event(sanitize_exception_message(str(e))):
                yield frame
            settle(
                user=user, tenants_repo=tenants_repo, reservation=reservation,
                actual_input_tokens=input_tokens, actual_output_tokens=output_tokens,
                model_id=model_id, context=tenants_repo,
                actual_cache_read_tokens=cache_read_tokens,
                actual_cache_write_tokens=cache_write_tokens,
            )
            settled = True
            return

        for frame in adapter.epilogue():
            yield frame

        settle(
            user=user, tenants_repo=tenants_repo, reservation=reservation,
            actual_input_tokens=input_tokens, actual_output_tokens=output_tokens,
            model_id=model_id, context=tenants_repo,
            actual_cache_read_tokens=cache_read_tokens,
            actual_cache_write_tokens=cache_write_tokens,
        )
        settled = True
    finally:
        if not settled:
            settle(
                user=user, tenants_repo=tenants_repo, reservation=reservation,
                actual_input_tokens=input_tokens, actual_output_tokens=output_tokens,
                model_id=model_id, context=tenants_repo,
                actual_cache_read_tokens=cache_read_tokens,
                actual_cache_write_tokens=cache_write_tokens,
            )
