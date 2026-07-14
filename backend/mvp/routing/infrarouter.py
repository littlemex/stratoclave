"""InfraRouter execution engine.

Retry + cross-region fallback with first-event commit semantics.
Sits below _budget_flow.run_stream as the invoke_stream callable.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator

from core.logging import get_logger

from .chains import resolve_chain
from .classify import classify
from .clients import bedrock_client
from .types import (
    AttemptRecord,
    Chain,
    Disposition,
    RouteRequest,
    RoutedStream,
    Target,
)

logger = get_logger(__name__)

_MAX_RETRIES_PER_TARGET = 2
_BASE_DELAY_S = 0.2
_MAX_DELAY_S = 2.0
_CHAIN_DEADLINE_S = 12.0

_cooldowns: dict[tuple[str, str], float] = {}
_COOLDOWN_TTL_S = 15.0


def _is_cooled_down(target: Target) -> bool:
    key = (target.model_id, target.region)
    expiry = _cooldowns.get(key, 0)
    return time.monotonic() < expiry


def _mark_cooldown(target: Target) -> None:
    key = (target.model_id, target.region)
    _cooldowns[key] = time.monotonic() + _COOLDOWN_TTL_S


async def _attempt_invoke(target: Target, payload: dict) -> dict:
    """Invoke converse_stream on a target, offloaded to thread."""
    client = bedrock_client(target.region)
    kwargs = dict(payload)
    kwargs["modelId"] = target.model_id
    return await asyncio.to_thread(client.converse_stream, **kwargs)


_FIRST_EVENT_TIMEOUT_S = 10.0


def _never_yields():
    """A synchronous stream that never produces a first event.

    Used only by the `timeout-first-event` fault: the reader thread spins in
    short sleeps (bounded so the reader isn't a hard-busy loop) and never
    reaches the `yield`, so no event is ever put on the queue. This forces the
    real first-event `wait_for` guard to fire exactly as a genuinely hung
    Bedrock connection would. The generator never yields in practice.
    """
    import time as _t

    # Cap the spin so a leaked reader thread can't run forever; well past the
    # 10s first-event timeout and any chain deadline, the reader exits and the
    # (already-failed-over or released) request is unaffected.
    deadline = _FIRST_EVENT_TIMEOUT_S * 6
    waited = 0.0
    while waited < deadline:
        _t.sleep(0.2)
        waited += 0.2
    yield  # pragma: no cover - unreachable within any live request's lifetime


async def _peek_first_event(
    stream: Any,
) -> tuple[dict, AsyncIterator[dict]]:
    """Await the first event from a Bedrock stream. Returns (first_event, rest).

    A dedicated daemon reader thread pumps events into an asyncio.Queue via
    call_soon_threadsafe (thread-safe). First event is awaited with a timeout
    so hung connections don't block the chain deadline. A stop flag lets the
    consumer signal the reader to exit on disconnect (checked between events),
    preventing thread leaks.
    """
    import threading

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    sentinel = object()
    stop = threading.Event()

    def _put(item):
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:
            # Event loop closed (consumer gone) — nothing to deliver to.
            pass

    def _reader():
        try:
            for item in stream:
                if stop.is_set():
                    break
                _put(item)
        except Exception as e:
            _put(e)
        finally:
            _put(sentinel)

    reader_thread = threading.Thread(target=_reader, name="sc-reader", daemon=True)
    reader_thread.start()

    try:
        first = await asyncio.wait_for(queue.get(), timeout=_FIRST_EVENT_TIMEOUT_S)
    except (asyncio.TimeoutError, Exception):
        stop.set()
        raise
    if first is sentinel:
        raise RuntimeError("Bedrock returned empty stream")
    if isinstance(first, Exception):
        raise first

    async def _rest():
        try:
            while True:
                item = await queue.get()
                if item is sentinel:
                    return
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            # Consumer done/abandoned → signal reader to stop on next event.
            stop.set()

    return first, _rest()


async def route_stream(req: RouteRequest) -> RoutedStream:
    """Execute a request through the chain with retry + fallback.

    Returns a RoutedStream with the committed target and event iterator.
    The first event has already been consumed (first-event commit).
    """
    chain = resolve_chain(
        req.alias,
        exclude=req.exclude,
        pin=req.pin,
    )

    attempts: list[AttemptRecord] = []
    deadline = time.monotonic() + _CHAIN_DEADLINE_S
    last_exc: Exception | None = None

    for target in chain.targets:
        if time.monotonic() > deadline:
            break
        if _is_cooled_down(target):
            continue

        for attempt_n in range(1, _MAX_RETRIES_PER_TARGET + 1):
            if time.monotonic() > deadline:
                break

            t0 = time.monotonic()
            try:
                if req.fault_spec:
                    from . import fault
                    fault_attempt = fault.next_attempt(req.request_id)
                    fault.maybe_raise_pre_stream(req.fault_spec, req.request_id, fault_attempt)
                    if fault.maybe_hang(req.fault_spec):
                        # Feed _peek_first_event a stream that never yields a
                        # first event, so the real first-event wait_for guard
                        # (not an ad-hoc sleep) is what fires. This exercises
                        # the production timeout path: TimeoutError -> classify
                        # -> failover, and chain-exhaustion -> clean release.
                        first_event, rest = await _peek_first_event(_never_yields())
                    elif fault.maybe_empty_stream(req.fault_spec, fault_attempt):
                        first_event, rest = await _peek_first_event(iter([]))
                    else:
                        resp = await _attempt_invoke(target, req.payload)
                        raw_stream = resp.get("stream", iter([]))
                        first_event, rest = await _peek_first_event(raw_stream)
                else:
                    resp = await _attempt_invoke(target, req.payload)
                    raw_stream = resp.get("stream", iter([]))
                    first_event, rest = await _peek_first_event(raw_stream)
            except Exception as e:
                latency = int((time.monotonic() - t0) * 1000)
                disposition = classify(e, target)
                attempts.append(AttemptRecord(
                    target=target,
                    outcome=disposition.value,
                    error_class=type(e).__name__,
                    latency_ms=latency,
                ))

                if disposition == Disposition.FATAL:
                    raise

                if disposition == Disposition.FAILOVER:
                    _mark_cooldown(target)
                    last_exc = e
                    break

                if disposition == Disposition.RETRY_SAME:
                    if attempt_n < _MAX_RETRIES_PER_TARGET:
                        delay = min(_BASE_DELAY_S * (2 ** (attempt_n - 1)), _MAX_DELAY_S)
                        await asyncio.sleep(delay)
                        last_exc = e
                        continue
                    else:
                        _mark_cooldown(target)
                        last_exc = e
                        break
                break
            else:
                latency = int((time.monotonic() - t0) * 1000)
                attempts.append(AttemptRecord(
                    target=target,
                    outcome="success",
                    latency_ms=latency,
                ))

                fault_spec = req.fault_spec

                async def _prepend(first, rest_iter, spec):
                    yield first
                    if spec:
                        from . import fault
                        if fault.should_fail_mid_stream(spec):
                            raise RuntimeError("injected mid-stream failure")
                    async for ev in rest_iter:
                        yield ev

                return RoutedStream(
                    target=target,
                    events=_prepend(first_event, rest, fault_spec),
                    attempt_facts=attempts,
                )

    if last_exc:
        raise last_exc
    raise RuntimeError("Chain exhausted with no attempts")
