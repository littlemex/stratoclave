"""Backend-agnostic money orchestration (layer a).

Owns the ONE canonical reserve → invoke → settle skeleton, the two error paths
(invoke-time full-refund+release vs mid-stream partial-settle), and the streaming
finally-settle guard.

The event loop drives normalized StreamEvents (from _converse_core.normalized_events)
through an injected adapter. Both the Anthropic Messages wire and the OpenAI Chat
Completions wire share this single settle-once machine.
"""
from __future__ import annotations

from typing import Any, AsyncGenerator, Callable, Iterable, Protocol

from . import _converse_types as t


class StreamAdapter(Protocol):
    """Protocol for wire-format adapters driven by run_stream."""

    def prologue(self) -> Iterable[bytes]: ...
    def render_event(self, event: t.StreamEvent) -> Iterable[bytes]: ...
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

    from core.error_handler import sanitize_exception_message

    from . import _converse_core as core

    acc = t.UsageAccumulator()
    settled = False

    try:
        for frame in adapter.prologue():
            yield frame

        try:
            resp = await asyncio.to_thread(invoke_stream, body=body, model_id=model_id)
        except Exception as e:
            tenants_repo.refund(
                user_id=user.user_id, tenant_id=user.org_id, tokens=reservation
            )
            release(tenants_repo)
            settled = True
            for frame in adapter.error_event(sanitize_exception_message(str(e))):
                yield frame
            return

        try:
            async for event in core.normalized_events(resp.get("stream", [])):
                acc.absorb(event)
                for frame in adapter.render_event(event):
                    yield frame
        except Exception as e:
            for frame in adapter.error_event(sanitize_exception_message(str(e))):
                yield frame
            settle(
                user=user, tenants_repo=tenants_repo, reservation=reservation,
                actual_input_tokens=acc.input_tokens,
                actual_output_tokens=acc.output_tokens,
                model_id=model_id, context=tenants_repo,
                actual_cache_read_tokens=acc.cache_read_tokens,
                actual_cache_write_tokens=acc.cache_write_tokens,
            )
            settled = True
            return

        for frame in adapter.epilogue():
            yield frame

        settle(
            user=user, tenants_repo=tenants_repo, reservation=reservation,
            actual_input_tokens=acc.input_tokens,
            actual_output_tokens=acc.output_tokens,
            model_id=model_id, context=tenants_repo,
            actual_cache_read_tokens=acc.cache_read_tokens,
            actual_cache_write_tokens=acc.cache_write_tokens,
        )
        settled = True
    finally:
        if not settled:
            settle(
                user=user, tenants_repo=tenants_repo, reservation=reservation,
                actual_input_tokens=acc.input_tokens,
                actual_output_tokens=acc.output_tokens,
                model_id=model_id, context=tenants_repo,
                actual_cache_read_tokens=acc.cache_read_tokens,
                actual_cache_write_tokens=acc.cache_write_tokens,
            )
