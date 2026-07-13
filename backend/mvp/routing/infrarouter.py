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


async def _peek_first_event(
    stream: Any,
) -> tuple[dict, AsyncIterator[dict]]:
    """Await the first event from a Bedrock stream. Returns (first_event, rest).

    If the stream raises before yielding, the exception propagates for classification.
    Empty streams raise RuntimeError (treated as FAILOVER by classify).
    """
    it = iter(stream)
    sentinel = object()

    def _next():
        result = next(it, sentinel)
        return result

    first = await asyncio.to_thread(_next)
    if first is sentinel:
        raise RuntimeError("Bedrock returned empty stream")

    async def _rest():
        sentinel = object()
        while True:
            item = await asyncio.to_thread(lambda: next(it, sentinel))
            if item is sentinel:
                return
            yield item

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

                async def _prepend(first, rest_iter):
                    yield first
                    async for ev in rest_iter:
                        yield ev

                return RoutedStream(
                    target=target,
                    events=_prepend(first_event, rest),
                    attempt_facts=attempts,
                )

    if last_exc:
        raise last_exc
    raise RuntimeError("Chain exhausted with no attempts")
