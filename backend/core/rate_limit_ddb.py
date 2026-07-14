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

Failure policy (see `_check`): the limiter distinguishes *throttling* of
its own counter item from a genuine backend outage.
  - Throttle-class errors (ProvisionedThroughputExceeded / Throttling /
    RequestLimitExceeded) are **evidence the IP is hammering one hot item**,
    which is exactly the attack this control exists to stop — fail CLOSED (429).
  - Connectivity / other errors are a real outage of a control that must not
    lock users out of auth — fail OPEN (allow), but always LOG + emit a metric
    so fail-open is alarmable and never silent.
Programming errors are NOT swallowed: they propagate so a broken limiter is a
loud 500, not silent unlimited auth.
"""
from __future__ import annotations

import functools
import inspect
import os
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

# Botocore error codes that mean "this item is being hammered", i.e. the breach
# the limiter exists to stop. Fail CLOSED on these.
_THROTTLE_CODES = frozenset(
    {
        "ProvisionedThroughputExceededException",
        "ThrottlingException",
        "RequestLimitExceeded",
        "TransactionInProgressException",
    }
)


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


_rl_resource = None


def _table():
    """Return the rate-limits table on a client with the tight RL timeouts.

    A dedicated resource (separate from the shared process-wide one) so the
    0.5s connect/read timeout applies here without touching the budget/repo
    path. Lazily created and cached; created inside the current region + any
    moto mock active at first call.
    """
    global _rl_resource
    if _rl_resource is None:
        import boto3
        region = os.getenv("AWS_REGION", "us-east-1")
        _rl_resource = boto3.resource(
            "dynamodb", region_name=region, config=_RL_CLIENT_CONFIG
        )
    return _rl_resource.Table(_TABLE)


def _check(scope: str, client_key: str, limit: int, window_seconds: int) -> None:
    """Atomically increment the window counter; raise 429 if over limit.

    Failure policy (see module docstring): throttle → fail closed; outage →
    fail open with a logged, alarmable metric; programming error → propagate.
    """
    now = int(time.time())
    window_start = (now // window_seconds) * window_seconds
    pk = f"RL#{scope}#{client_key}#{window_start}"
    ttl = window_start + window_seconds + _TTL_GRACE_SECONDS
    try:
        resp = _table().update_item(
            Key={"pk": pk},
            UpdateExpression="ADD hits :one SET expires_at = if_not_exists(expires_at, :ttl)",
            ExpressionAttributeValues={":one": 1, ":ttl": ttl},
            ReturnValues="UPDATED_NEW",
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in _THROTTLE_CODES:
            # The counter item itself is being throttled → the client is
            # hammering one hot key. Fail CLOSED: that IS the breach.
            _log.warning(
                "rate_limit_fail_closed_on_throttle",
                scope=scope, code=code,
            )
            raise RateLimitExceeded(window_seconds) from e
        # Any other AWS-side error is a genuine outage of the control; do not
        # lock users out of auth, but make the fail-open loud and alarmable.
        _log.warning("rate_limit_fail_open", scope=scope, code=code, error=str(e))
        return
    except (EndpointConnectionError, BotoConnectionError, BotoCoreError) as e:
        # Connectivity/timeout to DynamoDB → outage, fail open with a signal.
        _log.warning("rate_limit_fail_open", scope=scope, error=str(e))
        return
    # NOTE: no bare `except Exception`. A KeyError/TypeError/etc. here is a bug
    # in the limiter and must surface as a 500, not silent unlimited auth.

    count = int(resp.get("Attributes", {}).get("hits", 0))
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
            # missing `request` param would otherwise silently disable the cap.
            sig = inspect.signature(func)
            has_request = any(
                p.annotation is Request or p.name == "request"
                for p in sig.parameters.values()
            )
            if not has_request:
                raise RuntimeError(
                    f"@limiter.limit on {func.__qualname__} requires a "
                    f"'request: Request' parameter to derive the client key"
                )

            @functools.wraps(func)
            def sync_wrapper(*args, **kwargs):
                request = _find_request(args, kwargs)
                if request is not None:
                    _check(bucket_scope, self._client_key(request), limit, window)
                return func(*args, **kwargs)

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                import asyncio

                request = _find_request(args, kwargs)
                if request is not None:
                    # _check does a blocking boto3 call; never run it on the
                    # event loop or a slow table freezes every co-located
                    # SSE stream and trips ALB health checks.
                    await asyncio.to_thread(
                        _check, bucket_scope, self._client_key(request), limit, window
                    )
                return await func(*args, **kwargs)

            return async_wrapper if inspect.iscoroutinefunction(func) else sync_wrapper

        return decorator


def _find_request(args, kwargs) -> Optional[Request]:
    req = kwargs.get("request")
    if isinstance(req, Request):
        return req
    for a in args:
        if isinstance(a, Request):
            return a
    return None
