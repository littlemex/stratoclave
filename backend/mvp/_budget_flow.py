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
    import threading

    from core.error_handler import sanitize_exception_message

    from . import _converse_core as core

    acc = t.UsageAccumulator()

    # ONE-SHOT finalizer guard. There are four finalizer sites (invoke-error,
    # mid-stream-error, clean-completion, and the disconnect `finally`). Exactly
    # ONE must ever run its money writes. The flag is flipped INSIDE the lock
    # BEFORE any write, so even if a client disconnect throws CancelledError at
    # an `await asyncio.to_thread(...)` point (the offloaded thread still runs to
    # completion and commits), the `finally` re-entry sees `finalized` already
    # set and does nothing — no double-settle / double-refund. Without this the
    # to_thread offload double-commits with fresh idempotency tokens (no DDB
    # dedupe) → pool over-admission + double-billing.
    _final_lock = threading.Lock()
    finalized = False

    def _claim_finalize() -> bool:
        nonlocal finalized
        with _final_lock:
            if finalized:
                return False
            finalized = True
            return True

    def _do_settle():
        # settle does blocking boto3 (transact_write_items + jittered sleeps).
        settle(
            user=user, tenants_repo=tenants_repo, reservation=reservation,
            actual_input_tokens=acc.input_tokens,
            actual_output_tokens=acc.output_tokens,
            model_id=model_id, context=tenants_repo,
            actual_cache_read_tokens=acc.cache_read_tokens,
            actual_cache_write_tokens=acc.cache_write_tokens,
        )

    def _do_refund_release():
        tenants_repo.refund(
            user_id=user.user_id, tenant_id=user.org_id, tokens=reservation
        )
        release(tenants_repo)

    try:
        for frame in adapter.prologue():
            yield frame

        try:
            import inspect
            if inspect.iscoroutinefunction(invoke_stream):
                resp = await invoke_stream(body=body, model_id=model_id)
            else:
                resp = await asyncio.to_thread(invoke_stream, body=body, model_id=model_id)
        except Exception as e:
            # Invoke-time failure → refund + release, NO settle. Claim first,
            # then shield the offloaded write so a disconnect mid-refund can't
            # cancel it before it starts (the finally must not then settle a
            # request we already refunded).
            if _claim_finalize():
                await asyncio.shield(asyncio.to_thread(_do_refund_release))
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
            # Mid-stream failure → partial settle, NO release. Offload the
            # blocking settle; shield + once-guard make it exactly-once even on
            # a disconnect at the await.
            if _claim_finalize():
                await asyncio.shield(asyncio.to_thread(_do_settle))
            return

        for frame in adapter.epilogue():
            yield frame

        if _claim_finalize():
            await asyncio.shield(asyncio.to_thread(_do_settle))
    finally:
        # Disconnect/GeneratorExit before any finalizer claimed: settle once for
        # partial usage. Awaiting in a closing async generator is unsafe, so
        # fire-and-forget onto the loop's executor (the thread outlives request
        # teardown; process death is covered by the hold reaper) — never block
        # the SHARED event loop with settle's boto3 + sleeps here.
        if _claim_finalize():
            try:
                loop = asyncio.get_running_loop()
                loop.run_in_executor(None, _do_settle)
            except RuntimeError:
                _do_settle()
