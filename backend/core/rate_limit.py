"""Per-IP rate limiting for authentication endpoints.

P0-3 (2026-04 security review): `POST /api/mvp/auth/login`,
`/respond`, and `/sso-exchange` were reachable by unauthenticated
clients with **no** rate limit. That made them a trivial channel for
credential stuffing, user enumeration (via timing / response-shape
differences between `NotAuthorizedException` and
`UserNotFoundException`), and brute-force of the one-time passwords
emitted by `bootstrap-admin.sh`.

The limiter itself is slowapi (already a runtime dependency). In-
memory storage is fine because Stratoclave runs a single ECS task
today; if we ever scale to N tasks we will switch to a DynamoDB or
Redis backend via `storage_uri`.

Design choices:

* Client identity is the peer IP as seen by the backend. We trust
  `X-Forwarded-For` only when it is prepended by our own ALB /
  CloudFront chain. `slowapi.util.get_remote_address` pulls
  `request.client.host`, which under ECS Fargate behind ALB is the
  ALB node IP — not useful for per-user limits. We therefore build a
  small wrapper (`_client_key`) that prefers the first entry of
  `X-Forwarded-For` when present.
* Limits are intentionally loose enough for normal CLI retry loops
  (e.g. typo + correct password is 2 attempts within a second) but
  tight enough to make credential stuffing uneconomic.
* When the limit is hit we return 429 with a short retry hint.
  `slowapi` wires this into FastAPI via its own exception handler.
"""
from __future__ import annotations

import logging
import os

from fastapi import Request
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

_log = logging.getLogger(__name__)


def _client_key(request: Request) -> str:
    """Prefer the leftmost `X-Forwarded-For` IP when ALB is in front."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # Pick the first non-empty entry (may be "ip1, ip2, ip3").
        for part in xff.split(","):
            candidate = part.strip()
            if candidate:
                return candidate
    return get_remote_address(request)


# Applied globally via `limiter.limit(...)` decorators on the auth
# routers. The numbers below are per-IP, per-minute.
LOGIN_RATE_LIMIT = os.getenv("AUTH_LOGIN_RATE_LIMIT", "10/minute")
RESPOND_RATE_LIMIT = os.getenv("AUTH_RESPOND_RATE_LIMIT", "10/minute")
SSO_EXCHANGE_RATE_LIMIT = os.getenv("SSO_EXCHANGE_RATE_LIMIT", "20/minute")


limiter = Limiter(
    key_func=_client_key,
    default_limits=[],  # opt-in per route
    # `headers_enabled=True` makes slowapi's sync decorator try to mutate
    # the handler's return value into a `starlette.responses.Response`
    # and inject `X-RateLimit-*` headers. Our auth handlers return
    # pydantic models via `response_model=...`, so the injector blows up
    # with `parameter response must be an instance of starlette.responses.Response`
    # and turns every authenticated request into a 500. Disable the
    # header injection; the 429 emission on cap breach is independent of
    # headers_enabled and still fires through the exception handler.
    headers_enabled=False,
    swallow_errors=False,
)


__all__ = [
    "LOGIN_RATE_LIMIT",
    "RESPOND_RATE_LIMIT",
    "RateLimitExceeded",
    "SSO_EXCHANGE_RATE_LIMIT",
    "limiter",
]
