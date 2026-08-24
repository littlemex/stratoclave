"""How many connections an AWS client may hold.

`botocore` defaults `max_pool_connections` to 10. That is not a queue: with
`urllib3` in non-blocking mode, a call that finds the pool full opens a connection,
uses it, and discards it on release — so past the tenth concurrent call every call
pays a fresh TCP and TLS handshake. It is invisible until concurrency exceeds ten,
and then it costs both latency and the CPU that TLS needs.

Measured on 2026-08-25 at 128 requests in flight per task: the reservation phase
took p50 1201 ms while DynamoDB's own UpdateItem latency was 3-4 ms, and the log
carried 1,010 instances of `Connection pool is full, discarding connection:
dynamodb.us-east-1.amazonaws.com. Connection pool size: 10`. Throughput also
collapsed rather than plateaued as concurrency rose — 91.9 req/s at 512 concurrent
against 31.7 at 1024 — which is the signature of per-request cost growing with
concurrency rather than of a saturated task.

So every AWS client's pool is sized from the same number as the process's request
ceiling: a client that can be reached by N concurrent requests needs N connections.
One helper, so a new client cannot quietly inherit the default of 10.
"""
from __future__ import annotations

import os
from typing import Optional

from botocore.config import Config

# The per-process request ceiling. A pool smaller than this is oversubscribed by
# construction, so this is the default every pool falls back to.
SYNC_ROUTE_THREADS_ENV = "GATEWAY_SYNC_ROUTE_THREADS"
DEFAULT_POOL_CONNECTIONS = 128


def _positive_int(raw: Optional[str]) -> Optional[int]:
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 1 else None


def max_pool_connections(service_env_var: Optional[str] = None) -> int:
    """Connections a client for this service may hold.

    Resolution order: the service's own variable, then the process's request
    ceiling, then a built-in default. Per-service override exists because the
    services are not reached equally often — every request touches DynamoDB twice,
    while only some touch a given Bedrock region — but the fallback is the request
    ceiling rather than 10, so an unconfigured client is still sized for the
    concurrency the process admits.
    """
    if service_env_var:
        explicit = _positive_int(os.getenv(service_env_var))
        if explicit is not None:
            return explicit
    from_ceiling = _positive_int(os.getenv(SYNC_ROUTE_THREADS_ENV))
    if from_ceiling is not None:
        return from_ceiling
    return DEFAULT_POOL_CONNECTIONS


def boto_config(
    service_env_var: Optional[str] = None,
    *,
    connect_timeout: Optional[float] = None,
    read_timeout: Optional[float] = None,
    retries: Optional[dict] = None,
) -> Config:
    """A `botocore.Config` with the pool sized, and nothing else assumed.

    Timeouts and retries stay the caller's decision: a Bedrock invocation and a
    DynamoDB conditional write want different ones, and guessing here would hide
    that.
    """
    kwargs: dict = {"max_pool_connections": max_pool_connections(service_env_var)}
    if connect_timeout is not None:
        kwargs["connect_timeout"] = connect_timeout
    if read_timeout is not None:
        kwargs["read_timeout"] = read_timeout
    if retries is not None:
        kwargs["retries"] = retries
    return Config(**kwargs)
