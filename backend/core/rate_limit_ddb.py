"""DynamoDB-backed fixed-window rate limiter.

Replaces slowapi's in-process counter so per-IP limits hold across a
horizontally-scaled (multi-task / multi-AZ) ECS deployment. No Redis,
no new infrastructure class — a single DynamoDB table with a TTL
attribute, consistent with the DynamoDB-single-store constraint.

Algorithm (fixed window):
  - bucket key = RL#{scope}#{client_key}#{window_start_epoch}
  - window_start = floor(now / window_seconds) * window_seconds
  - one atomic `ADD hits :1` UpdateItem returns the post-increment count
  - if count > limit → 429 (RateLimitExceeded)
  - item carries `expires_at` = window_start + window_seconds + grace, so
    DynamoDB reaps expired windows automatically

Fixed window (not sliding) is deliberate: it needs exactly one atomic
write per request and no read, so it is cheap and race-free under
concurrency. The worst-case burst at a window boundary is 2x the limit,
which is acceptable for auth brute-force mitigation.

Failure policy (see `_check`) — three buckets, chosen so no single failure
mode both (a) admits unlimited auth traffic and (b) does so silently:
  - **Per-partition throttle** of THIS counter item (ProvisionedThroughput /
    Throttling) is evidence the key is being hammered — the attack this control
    exists to stop — so fail CLOSED (429). NOTE account/table-scoped throttles
    (RequestLimitExceeded) are deliberately NOT here: a noisy neighbour trips
    them, so they fail OPEN to avoid locking out every user.
  - **Misconfiguration** (ResourceNotFound / AccessDenied / Validation /
    UnrecognizedClient) is a broken control, not a transient outage → fail
    CLOSED + log at error, so a fat-fingered env var can't disable auth limiting
    forever.
  - **Transient outage** (connectivity/timeout, other AWS errors incl.
    account-scoped RequestLimitExceeded) → DEGRADE to an in-process per-task
    fixed-window limiter rather than failing fully open. That bounds a bypass to
    `limit × task_count` instead of ∞ while never locking users out on a real
    outage. Always `_log.warning("rate_limit_degrade_to_local")` — that log line
    is the alarm hook: wire a CloudWatch metric filter + alarm on it in ops.
Programming errors (KeyError/TypeError/...) are NOT caught: they propagate so a
broken limiter is a loud 500, not silent unlimited auth.
"""
from __future__ import annotations

import functools
import inspect
import asyncio
import os
import threading
import time
from typing import Callable, Optional

from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionError as BotoConnectionError,
    EndpointConnectionError,
)
from fastapi import HTTPException, Request

from core.logging import get_logger

_log = get_logger(__name__)

_TABLE = os.getenv("DYNAMODB_RATE_LIMITS_TABLE", "stratoclave-rate-limits")
_TTL_GRACE_SECONDS = 60

# A rate-limit check must answer fast or get out of the way: it sits in front
# of every auth request, so the default 60s connect/read timeouts would park
# the event loop (and every co-located SSE stream) on a slow table. 0.5s with a
# single attempt bounds the worst case; a check that can't answer in time is
# treated per the failure policy above, quickly.
_RL_CLIENT_CONFIG = Config(
    connect_timeout=0.5,
    read_timeout=0.5,
    retries={"max_attempts": 1, "mode": "standard"},
)

# Per-*partition* throttle of the counter item: on an on-demand table this is
# evidence THIS key (RL#scope#ip#window) is being hammered — the breach the
# limiter exists to stop — so fail CLOSED. We deliberately do NOT put
# account/table-scoped throttles here: `RequestLimitExceeded` is an on-demand
# account throughput quota that any traffic (even another table) can trip, so
# failing closed on it would let one noisy neighbour 429 every auth user. Those
# go to the fail-open bucket instead. (`ProvisionedThroughputExceeded` is
# table-level under provisioned mode; we pin this table to PAY_PER_REQUEST in
# IaC precisely so this stays a per-partition signal.)
_THROTTLE_CODES = frozenset(
    {
        "ProvisionedThroughputExceededException",
        "ThrottlingException",
    }
)

# Misconfiguration / programming errors: a wrong table name, missing IAM grant,
# or a key-schema mismatch must NOT silently disable auth rate limiting. Fail
# CLOSED and log loudly so it's caught, rather than running unlimited forever.
_MISCONFIG_CODES = frozenset(
    {
        "ResourceNotFoundException",
        "AccessDeniedException",
        "ValidationException",
        "UnrecognizedClientException",
    }
)


class _LocalWindows:
    """Tiny thread-safe in-process fixed-window counter, used ONLY as a degraded
    fallback when DynamoDB is unavailable (transient outage / account-scoped
    throttle). It bounds a fail-open bypass to `limit × task_count` instead of
    ∞ — far better than admitting everything — while never locking out users on
    a real DDB outage. Per-task state (not shared), pruned lazily so it can't
    grow unbounded.
    """

    def __init__(self):
        self._counts: dict[tuple[str, str, int], int] = {}
        self._lock = threading.Lock()

    def over_limit(self, scope: str, client_key: str, limit: int, window_seconds: int) -> bool:
        now = int(time.time())
        window_start = (now // window_seconds) * window_seconds
        key = (scope, client_key, window_start)
        with self._lock:
            # Prune expired windows opportunistically (bounded work).
            if len(self._counts) > 4096:
                self._counts = {
                    k: v for k, v in self._counts.items() if k[2] >= window_start
                }
            n = self._counts.get(key, 0) + 1
            self._counts[key] = n
            return n > limit


_local_fallback = _LocalWindows()


class RateLimitExceeded(HTTPException):
    """Raised (as a 429) when a client exceeds its window budget.

    A dedicated subclass — not a bare ``HTTPException`` alias — so callers can
    ``isinstance``/``add_exception_handler`` on it without matching every other
    HTTPException in the app.
    """

    def __init__(self, window_seconds: int):
        super().__init__(
            status_code=429,
            detail="Rate limit exceeded. Try again later.",
            headers={"Retry-After": str(window_seconds)},
        )


def _parse_spec(spec: str) -> tuple[int, int]:
    """Parse a slowapi-style spec like '10/minute' → (limit, window_seconds).

    Raises ValueError on a malformed spec. These specs come from env vars read
    at import time, so a typo (``100/hr``, ``10`` with no period) fails the
    deploy loudly rather than silently defaulting to a 60x-looser window.
    """
    limit_str, sep, period = spec.partition("/")
    if not sep:
        raise ValueError(f"rate-limit spec {spec!r} missing '/period' (e.g. '10/minute')")
    try:
        limit = int(limit_str.strip())
    except ValueError as e:
        raise ValueError(f"rate-limit spec {spec!r} has a non-integer limit") from e
    period = period.strip().lower().rstrip("s")
    windows = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
    if period not in windows:
        raise ValueError(
            f"rate-limit spec {spec!r} has an unknown period {period!r}; "
            f"expected one of {sorted(windows)}"
        )
    return limit, windows[period]


_rl_client = None
_rl_client_lock = threading.Lock()


def _client():
    """Return a low-level DynamoDB client with the tight RL timeouts.

    A low-level client (not a resource) because boto3 documents only clients as
    thread-safe — and this runs from `asyncio.to_thread`, i.e. on the shared
    threadpool. Creation is guarded by a lock so concurrent first-requests don't
    race the (not-thread-safe) endpoint-resolver/credential-provider init. The
    dedicated client keeps the 0.5s timeout off the budget/repo path.

    Cached in a module global; tests reset it (see conftest) so a fresh client
    is built inside the active moto mock.
    """
    global _rl_client
    if _rl_client is None:
        with _rl_client_lock:
            if _rl_client is None:  # re-check inside the lock
                import boto3
                region = os.getenv("AWS_REGION", "us-east-1")
                _rl_client = boto3.client(
                    "dynamodb", region_name=region, config=_RL_CLIENT_CONFIG
                )
    return _rl_client


def _degraded_check(scope: str, client_key: str, limit: int, window_seconds: int) -> None:
    """Fallback limit when DynamoDB is unavailable: an in-process per-task
    fixed window. Raises 429 if over. Never itself raises on backend errors
    (there is no backend) — this is the last line of defence, not the primary.
    """
    if _local_fallback.over_limit(scope, client_key, limit, window_seconds):
        raise RateLimitExceeded(window_seconds)


def _check(scope: str, client_key: str, limit: int, window_seconds: int) -> None:
    """Atomically increment the window counter; raise 429 if over limit.

    Failure policy (see module docstring): per-partition throttle → fail closed;
    misconfig → fail closed; transient outage → degrade to an in-process
    fallback limiter (not fully open); programming error → propagate.
    """
    now = int(time.time())
    window_start = (now // window_seconds) * window_seconds
    pk = f"RL#{scope}#{client_key}#{window_start}"
    ttl = window_start + window_seconds + _TTL_GRACE_SECONDS
    try:
        resp = _client().update_item(
            TableName=_TABLE,
            Key={"pk": {"S": pk}},
            UpdateExpression="ADD hits :one SET expires_at = if_not_exists(expires_at, :ttl)",
            ExpressionAttributeValues={":one": {"N": "1"}, ":ttl": {"N": str(ttl)}},
            ReturnValues="UPDATED_NEW",
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in _THROTTLE_CODES:
            # Per-partition throttle of THIS counter item → the client is
            # hammering one hot key. Fail CLOSED: that IS the breach.
            _log.warning("rate_limit_fail_closed_on_throttle", scope=scope, code=code)
            raise RateLimitExceeded(window_seconds) from e
        if code in _MISCONFIG_CODES:
            # Wrong table / missing IAM / key-schema mismatch. This is a broken
            # control, not a transient outage; do NOT run auth unlimited.
            _log.error("rate_limit_fail_closed_on_misconfig", scope=scope, code=code, error=str(e))
            raise RateLimitExceeded(window_seconds) from e
        # Any other AWS-side error (incl. account-scoped RequestLimitExceeded,
        # which noisy neighbours can trip) is a transient outage of the shared
        # control: don't lock users out, but degrade to the in-process limiter
        # (bounds the bypass to limit×tasks) and log loudly so it's alarmable.
        _log.warning("rate_limit_degrade_to_local", scope=scope, code=code, error=str(e))
        _degraded_check(scope, client_key, limit, window_seconds)
        return
    except (EndpointConnectionError, BotoConnectionError, BotoCoreError) as e:
        # Connectivity/timeout to DynamoDB → outage; degrade to local, don't
        # fail fully open.
        _log.warning("rate_limit_degrade_to_local", scope=scope, error=str(e))
        _degraded_check(scope, client_key, limit, window_seconds)
        return
    # NOTE: no bare `except Exception`. A KeyError/TypeError/etc. here is a bug
    # in the limiter and must surface as a 500, not silent unlimited auth.

    count = int(resp.get("Attributes", {}).get("hits", {}).get("N", "0"))
    if count > limit:
        raise RateLimitExceeded(window_seconds)


class DynamoRateLimiter:
    """slowapi-compatible facade: `@limiter.limit("10/minute")` on a route.

    The decorated handler MUST accept a `request: Request` parameter (same
    requirement slowapi imposes) so the client key can be derived. This is
    enforced at decoration time — a handler without one raises RuntimeError at
    import, never silently skips the limit at runtime.
    """

    def __init__(self, client_key_func: Callable[[Request], str]):
        self._client_key = client_key_func

    def limit(self, spec: str, scope: Optional[str] = None):
        limit, window = _parse_spec(spec)

        def decorator(func):
            bucket_scope = scope or func.__name__

            # Fail loudly at import if the handler can't be rate-limited: a
            # missing Request param would otherwise silently disable the cap.
            # Match the annotation whether it is the real class or a string
            # (PEP 563 / `from __future__ import annotations` makes annotations
            # strings), OR a param literally named "request". Runtime lookup
            # (_find_request) is by isinstance, so any of these is sufficient.
            sig = inspect.signature(func)
            def _is_request_param(p) -> bool:
                ann = p.annotation
                if ann is Request:
                    return True
                if isinstance(ann, str) and ann.split("[")[0].split(".")[-1] == "Request":
                    return True
                return p.name == "request"
            if not any(_is_request_param(p) for p in sig.parameters.values()):
                raise RuntimeError(
                    f"@limiter.limit on {func.__qualname__} requires a "
                    f"parameter annotated `Request` to derive the client key"
                )

            def _require_request(args, kwargs) -> Request:
                # Decoration guaranteed a Request param exists, so a miss here
                # is a wiring bug — raise rather than silently skip the limit.
                request = _find_request(args, kwargs)
                if request is None:
                    raise RuntimeError(
                        f"@limiter.limit on {func.__qualname__}: no Request "
                        f"found in call args; the limit was not applied"
                    )
                return request

            @functools.wraps(func)
            def sync_wrapper(*args, **kwargs):
                request = _require_request(args, kwargs)
                _check(bucket_scope, self._client_key(request), limit, window)
                return func(*args, **kwargs)

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                request = _require_request(args, kwargs)
                # _check does a blocking boto3 call; never run it on the event
                # loop or a slow table freezes every co-located SSE stream and
                # trips ALB health checks.
                await asyncio.to_thread(
                    _check, bucket_scope, self._client_key(request), limit, window
                )
                return await func(*args, **kwargs)

            return async_wrapper if inspect.iscoroutinefunction(func) else sync_wrapper

        return decorator


def _find_request(args, kwargs) -> Optional[Request]:
    # Scan by type in both kwargs and positionals — FastAPI passes the Request
    # under the handler's own parameter name, which may not be "request".
    for v in kwargs.values():
        if isinstance(v, Request):
            return v
    for a in args:
        if isinstance(a, Request):
            return a
    return None
