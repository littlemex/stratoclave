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
  - item carries `ttl` = window_start + window_seconds + grace, so
    DynamoDB reaps expired windows automatically

Fixed window (not sliding) is deliberate: it needs exactly one atomic
write per request and no read, so it is cheap and race-free under
concurrency. The worst-case burst at a window boundary is 2x the limit,
which is acceptable for auth brute-force mitigation.
"""
from __future__ import annotations

import functools
import inspect
import os
import time
from typing import Callable, Optional

from fastapi import HTTPException, Request

_TABLE = os.getenv("DYNAMODB_RATE_LIMITS_TABLE", "stratoclave-rate-limits")
_TTL_GRACE_SECONDS = 60


def _parse_spec(spec: str) -> tuple[int, int]:
    """Parse a slowapi-style spec like '10/minute' → (limit, window_seconds)."""
    limit_str, _, period = spec.partition("/")
    limit = int(limit_str.strip())
    period = period.strip().lower()
    window = {
        "second": 1, "minute": 60, "hour": 3600, "day": 86400,
    }.get(period.rstrip("s"), 60)
    return limit, window


def _table():
    from dynamo.client import get_dynamodb_resource
    return get_dynamodb_resource().Table(_TABLE)


def _check(scope: str, client_key: str, limit: int, window_seconds: int) -> None:
    """Atomically increment the window counter; raise 429 if over limit.

    A DynamoDB error must never turn into a 500 on the auth path — fail
    open (allow) and let the request proceed rather than lock users out
    if the table is briefly unavailable.
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
    except Exception:
        return  # fail open — availability over strictness on transient DDB errors

    count = int(resp.get("Attributes", {}).get("hits", 0))
    if count > limit:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Try again later.",
            headers={"Retry-After": str(window_seconds)},
        )


class DynamoRateLimiter:
    """slowapi-compatible facade: `@limiter.limit("10/minute")` on a route.

    The decorated handler must accept a `request: Request` parameter
    (same requirement slowapi imposes) so the client key can be derived.
    """

    def __init__(self, client_key_func: Callable[[Request], str]):
        self._client_key = client_key_func

    def limit(self, spec: str, scope: Optional[str] = None):
        limit, window = _parse_spec(spec)

        def decorator(func):
            bucket_scope = scope or func.__name__

            @functools.wraps(func)
            def sync_wrapper(*args, **kwargs):
                request = _find_request(args, kwargs)
                if request is not None:
                    _check(bucket_scope, self._client_key(request), limit, window)
                return func(*args, **kwargs)

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                request = _find_request(args, kwargs)
                if request is not None:
                    _check(bucket_scope, self._client_key(request), limit, window)
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
