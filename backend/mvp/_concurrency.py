"""How many requests one process may hold at once.

Two framework defaults decide this and neither was chosen:

* FastAPI runs a `def` route in `anyio`'s worker threadpool, whose default limit is
  40. `POST /v1/chat/completions` is such a route and it holds its worker for the
  whole upstream call, so a process could hold at most 40 chat requests.
* `asyncio.to_thread`, which the streaming and accounting paths use to keep blocking
  DynamoDB and boto3 calls off the event loop, runs on the loop's default executor.
  That executor is sized `min(32, cpu_count + 4)`, and `cpu_count` inside a Fargate
  task reports the HOST's cores rather than the task's share, so the number was
  neither intentional nor predictable.

Both are set explicitly here. Note the direction: the sync-route ceiling is now
BELOW anyio's default, not above it. Raising it to 128 was measured and it was a
mistake — p50 tracked requests in flight per process (390 ms at 4, 549 ms at 8,
1331 ms at 32, 2791 ms at 64, 7706 ms at 128) and throughput collapsed from 91.9
req/s at 512 concurrent to 31.7 at 1024 rather than plateauing. Collapse means the
cost of a request grows with concurrency, which is what an oversubscribed connection
pool does: past the pool size every call pays a fresh TLS handshake. The pools are
now sized (see `core.aws_pool`), and this ceiling sits near the knee of the latency
curve rather than at the edge of what a process will accept.

Fleet concurrency is this per-process ceiling times processes per task times tasks.
Scaling it means adding processes — `GATEWAY_UVICORN_WORKERS`, sized with the task's
CPU — not raising the ceiling, because a process that admits more than it can turn
around quickly is simply queueing inside itself where nothing can see it.
"""
from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from core.logging import get_logger

logger = get_logger(__name__)

# Per task, not per fleet. See the module docstring for how these relate to the
# autoscaling target.
OFFLOAD_THREADS_ENV = "GATEWAY_OFFLOAD_THREADS"
SYNC_ROUTE_THREADS_ENV = "GATEWAY_SYNC_ROUTE_THREADS"

# Per PROCESS, not per task, and deliberately far below what a process will
# accept. Measured on 2026-08-25, p50 tracked requests in flight per process — 390
# ms at 4, 549 ms at 8, 1331 ms at 32, 2791 ms at 64, 7706 ms at 128 — while task
# CPU stayed under 70%, and throughput COLLAPSED from 91.9 req/s at 512 concurrent
# to 31.7 at 1024 rather than plateauing. Collapse means per-request cost grows
# with concurrency, which is what a client whose connection pool is oversubscribed
# does: every call past the pool size pays a fresh TLS handshake. Admitting 128
# into one process was self-inflicted, so the ceiling now sits near the knee of
# that curve and the fleet grows by processes instead.
DEFAULT_OFFLOAD_THREADS = 32
DEFAULT_SYNC_ROUTE_THREADS = 32


@dataclass(frozen=True)
class Capacity:
    """The ceilings actually applied, for the startup log and for tests."""

    offload_threads: int
    sync_route_threads: int


class CapacityConfigError(RuntimeError):
    """A capacity variable is set to something unusable."""


def capacity_env_int(name: str, default: int) -> int:
    """A positive integer from the environment, or `default` when unset.

    Raises rather than falling back when the value is set but unusable. A typo in
    a deploy variable would otherwise silently shrink the fleet's capacity, and a
    quietly throttled gateway is harder to notice than one that refuses to start —
    the same reason a misconfigured price source fails startup rather than
    degrading. Every capacity knob reads through here so the Python side and the
    IaC side reject bad input the same way.
    """
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise CapacityConfigError(
            f"{name} must be a positive integer when set (got {raw!r})"
        ) from exc
    if value < 1:
        raise CapacityConfigError(
            f"{name} must be a positive integer when set (got {raw!r})"
        )
    return value


def configure_capacity() -> Capacity:
    """Apply both ceilings to the running loop and return what was applied.

    Called once from the application lifespan, which is also where every capacity
    variable is validated: reading them all here means a bad value fails the
    deployment instead of the first request that happens to touch that code path.

    Calling it a second time does not interrupt work in flight, but it does leave
    the previous executor's idle threads alive until the process exits, so it is
    startup-only in practice.
    """
    offload = capacity_env_int(OFFLOAD_THREADS_ENV, DEFAULT_OFFLOAD_THREADS)
    sync_routes = capacity_env_int(SYNC_ROUTE_THREADS_ENV, DEFAULT_SYNC_ROUTE_THREADS)

    loop = asyncio.get_running_loop()
    loop.set_default_executor(
        ThreadPoolExecutor(max_workers=offload, thread_name_prefix="gateway-offload")
    )

    # anyio's limiter is a capacity limiter shared by every sync route in the
    # process. It is imported here rather than at module scope so this module
    # stays importable in a context that has no anyio (a unit test of the env
    # parsing, for instance).
    from anyio import to_thread

    to_thread.current_default_thread_limiter().total_tokens = sync_routes

    # Validate the ceilings owned by the transports too, so one startup rejects
    # every unusable value rather than failing later, one request at a time.
    from core.aws_pool import max_pool_connections

    from ._bedrock_clients import bedrock_pool_size
    from ._openai_transport import openai_transport_connection_ceiling

    bedrock_connections = bedrock_pool_size()
    openai_connections = openai_transport_connection_ceiling()
    # Every request reserves and settles, so this is the busiest pool in the
    # process and the one whose default of 10 cost the most.
    dynamodb_connections = max_pool_connections("DYNAMODB_MAX_POOL_CONNECTIONS")
    # Each pool is compared with the ceiling that actually feeds it. Converse calls
    # reach Bedrock through `asyncio.to_thread`, so the offload width is what can
    # be in flight there; the OpenAI-compatible endpoint requests come from the sync routes and from async
    # streams, so the sync ceiling is the closer bound of the two.
    for name, connections, feeding_ceiling in (
        ("the OpenAI-compatible endpoint", openai_connections, sync_routes),
        ("bedrock", bedrock_connections, offload),
        ("dynamodb", dynamodb_connections, sync_routes),
    ):
        if connections < feeding_ceiling:
            # Not fatal: a task may legitimately admit more requests than any one
            # upstream can carry connections for, because the traffic is split
            # across upstreams. It is worth saying out loud, though — the surplus
            # waits on a connection pool, which looks like upstream latency.
            logger.warning(
                "connection_ceiling_below_request_ceiling",
                upstream=name,
                connections=connections,
                feeding_ceiling=feeding_ceiling,
                note="requests beyond the connection ceiling queue on the pool",
            )

    applied = Capacity(offload_threads=offload, sync_route_threads=sync_routes)
    logger.info(
        "concurrency_capacity_configured",
        offload_threads=applied.offload_threads,
        sync_route_threads=applied.sync_route_threads,
        openai_connections=openai_connections,
        bedrock_connections=bedrock_connections,
        dynamodb_connections=dynamodb_connections,
        note="per task; fleet concurrency is this times the running task count",
    )
    return applied
