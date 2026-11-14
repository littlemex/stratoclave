"""Per-IP rate limiting for authentication endpoints.

Background
----------
P0-3 (2026-04 security review) gave Stratoclave its first rate limits
on ``POST /api/mvp/auth/login``, ``/respond``, ``/sso-exchange`` and
``/ui-ticket/consume``. Before that, credential stuffing and
temporary-password brute force were unbounded.

Z-2 (2026-04 third blind review) — final XFF correction
--------------------------------------------------------

Two earlier passes got the X-Forwarded-For algebra wrong in
different directions:

* **Sweep-1** trusted the *leftmost* entry → attacker rotates
  ``X-Forwarded-For: <junk>`` per request, bypasses every per-IP cap.
* **Sweep-2** tried to fix it by peeling ``TRUSTED_HOPS`` entries
  from the right and defaulting to 2, assuming the ECS-side XFF
  always carried ``<viewer>, <cf-edge>, <alb>``. AWS ALB **does not
  append its own IP** to XFF — it appends the immediate upstream's
  IP. So in the CloudFront → ALB → ECS topology, a normal request
  arrives with **2** entries (``<viewer>, <cf-edge>``), not 3.
  Peeling 2 emptied the list and the fallback returned the CF edge
  IP as the bucket key for every viewer. Worst case everyone on the
  same CF edge shared a single 10 req/min bucket; best case the
  attacker re-added a forged leftmost to make ``parts`` size 3, the
  rightmost-peel branch returned the forged value, and the bypass was
  right back.

The correct model (matching the AWS docs):

    actual XFF at ECS = <viewer>[, <cf-edge>]
    number of TRUSTED right-side entries appended by OUR chain = 1
        (only ALB appends in our current topology; CF's entry IS the
        viewer itself, not an extra proxy hop)

So ``RATE_LIMIT_TRUSTED_HOPS=1`` in this deployment. The safer
algorithm when the hops count is ever misconfigured is to **index
from the right** (not slice-and-fallback), clamp, and bias towards
the rightmost-known-good entry when the list is shorter than
expected: a proxy-written value, while possibly coarse, is still not
attacker-controlled.

Operators with a different topology override ``RATE_LIMIT_TRUSTED_HOPS``:
* CloudFront only (no ALB, e.g. Lambda@Edge)  → 0
* CloudFront → ALB → ECS                       → 1 (default)
* custom WAF → CloudFront → ALB → ECS          → 2

The limiter is backed by DynamoDB (fixed-window counters with TTL), so
per-IP caps hold across a horizontally-scaled multi-task / multi-AZ
deployment without Redis or any new infrastructure class — see
``core/rate_limit_ddb.py``.
"""
from __future__ import annotations

import logging
import os

from fastapi import Request

_log = logging.getLogger(__name__)


def get_remote_address(request: Request) -> str:
    """Return the direct peer IP, or a stable placeholder when unknown.

    Replaces slowapi's helper of the same name so the rate-limit path has
    no slowapi dependency. Used as the fallback bucket key when no trusted
    X-Forwarded-For entry is available.
    """
    if request.client and request.client.host:
        return request.client.host
    return "unknown"

# `RateLimitExceeded` is a real HTTPException *subclass* (see rate_limit_ddb),
# re-exported here for import-site compatibility. It is NOT an alias for
# HTTPException — aliasing would make any `isinstance`/exception-handler on it
# match every HTTPException in the app (401/402/404...). Import the concrete
# type so handlers can target 429s specifically.
from core.rate_limit_ddb import RateLimitExceeded  # noqa: E402,F401


def _trusted_hops() -> int:
    """Return the number of right-side XFF entries written by OUR OWN
    proxy chain (ALB / custom WAF / ...) — NOT counting the viewer
    IP that CloudFront appended on the way in (that entry IS the
    viewer, not a proxy hop).

    Default 1 matches the production topology (CloudFront → ALB →
    ECS). A mis-parse or negative value falls back to 1.
    """
    raw = os.getenv("RATE_LIMIT_TRUSTED_HOPS", "1")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1
    if value < 0:
        return 1
    return value


def _client_key(request: Request) -> str:
    """Identify the requester for rate-limit bucketing.

    Algorithm (right-indexed):

    1. If ``X-Forwarded-For`` is absent → direct peer (`request.client.host`).
    2. Split the header, strip empties. This gives ``parts[0..n-1]``
       where ``parts[-1]`` is the value the outermost trusted proxy
       added, and ``parts[0]`` is whatever the viewer (or a fibber
       upstream of the viewer) supplied.
    3. Pick ``parts[-hops-1]`` — the entry immediately to the left of
       our trusted chain. That is the first entry we no longer wrote
       ourselves.
    4. If the list is shorter than ``hops+1`` (operator mis-configured
       the hop count, or the request arrived via a shorter chain than
       declared), fall back to ``parts[-1]``. That is never
       attacker-controlled (a real proxy wrote it), so it degrades to
       a coarser-but-honest bucket rather than an attacker-pickable
       one.

    Notes vs. the previous slice-and-peel implementation:

    * ``parts[:-hops]`` in a 2-entry XFF with ``hops=2`` produced the
      empty list and fell through to ``parts[-1]`` (the CF edge IP),
      which silently put every viewer behind one CF edge into the
      same bucket. Index-from-right avoids that branch.
    * In a forged 3-entry XFF the slice version returned the forged
      leftmost. Index-from-right returns the viewer IP CloudFront
      wrote, independent of how many extra fibs the caller prepended.
    """
    xff = request.headers.get("x-forwarded-for")
    if not xff:
        return get_remote_address(request)

    parts = [p.strip() for p in xff.split(",") if p.strip()]
    if not parts:
        return get_remote_address(request)

    hops = _trusted_hops()
    if hops <= 0:
        # Operator declared zero trusted hops → do not trust any XFF.
        return get_remote_address(request)

    # Index the entry immediately to the left of our proxy chain.
    idx = -hops - 1  # e.g. hops=1 → parts[-2]
    if -idx <= len(parts):
        return parts[idx]

    # Shorter chain than expected: the rightmost entry is the safest
    # single value because a real proxy wrote it.
    return parts[-1]


# Applied globally via `limiter.limit(...)` decorators on the auth
# routers. The numbers below are per-IP, per-minute.
LOGIN_RATE_LIMIT = os.getenv("AUTH_LOGIN_RATE_LIMIT", "10/minute")
RESPOND_RATE_LIMIT = os.getenv("AUTH_RESPOND_RATE_LIMIT", "10/minute")
SSO_EXCHANGE_RATE_LIMIT = os.getenv("SSO_EXCHANGE_RATE_LIMIT", "20/minute")
# External authorize/capture/void (Layer-5 authcap). These pool-only endpoints
# do NOT call Bedrock, so a client can issue them far faster than an inline LLM
# request — and they contend on the SAME single TenantBudgets row + CreditLedger
# partition that every inline request's reserve/settle uses. Without a per-IP
# cap, a fast authorize→void loop (even a buggy client re-minting Idempotency-Key
# each time) can saturate that hot item's optimistic-CAS retries and starve
# unrelated users' inference in the same tenant (503/402). 60/minute is generous
# for legitimate programmatic use while bounding the contention burst well under
# the ~20-way concurrency the reserve CAS was tuned for.
BILLING_WRITE_RATE_LIMIT = os.getenv("BILLING_WRITE_RATE_LIMIT", "60/minute")

# A sign-in request has no verified caller yet, so the `@limiter.limit(...)`
# decorator on each sign-in route cannot bucket it by user -- the address is
# all there is for the DECORATOR to key on. But a single shared address
# (behind a NAT, or on the private entry where every request arrives from the
# load balancer) must not become one global lockout: the decorator's address
# bucket is therefore a coarse FLOOD GUARD, sized well above what normal
# shared egress produces, and every sign-in route writes to the SAME scope
# (`AUTH_SIGN_IN_ADDRESS_SCOPE` below) so an attacker cannot multiply their
# address budget by spreading requests across `/login`, `/respond`,
# `/sso-exchange` and `/ui-ticket/consume`. The tight, credential-guessing
# limits above (`LOGIN_RATE_LIMIT` etc.) move to a SEPARATE check keyed on the
# identity the request names -- the username/email for `/login` and
# `/respond`, the access key id for `/sso-exchange` -- run explicitly inside
# each handler via `check_identity_rate_limit` below, because that identity
# lives in the request BODY, which a `Request`-only decorator cannot read.
# See docs/DEPLOYMENT.md's rate-limit section for the sizing rationale.
AUTH_ADDRESS_RATE_LIMIT = os.getenv("AUTH_ADDRESS_RATE_LIMIT", "300/minute")
AUTH_SIGN_IN_ADDRESS_SCOPE = "auth_sign_in_address"


# The limiter is DynamoDB-backed (fixed-window, TTL-reaped). Per-IP
# counters are shared across every ECS task, so caps hold under
# horizontal scale-out and survive task restarts — no Redis, no new
# infra class. The client key derivation (XFF algebra above) is reused
# verbatim; only the counter storage moved from in-process to DynamoDB.
from core.rate_limit_ddb import DynamoRateLimiter  # noqa: E402

def _request_key(request: Request) -> str:
    """The rate-limit bucket: the verified user when the route authenticated one,
    else the client address (`_client_key`).

    Never a header value the caller presents: an unverified header would let a
    caller pick a fresh bucket per request by sending a made-up key. The user id
    is set on `request.state` by the authentication dependency
    (`mvp.deps.get_current_user`), which FastAPI resolves before the limited
    route runs, so it exists only after authentication has succeeded. Keyed on the user because an address is a poor
    identity for an authenticated caller: users behind one NAT share it, and on
    the private (PrivateLink) entry every request arrives from the load balancer.
    Unauthenticated routes (sign-in) key on the address here too -- but that is
    now only this decorator's coarse flood guard (`AUTH_ADDRESS_RATE_LIMIT`
    above); the credential-guessing-relevant limit is the identity-keyed check
    each sign-in handler runs itself. A request that fails authentication
    never reaches the limiter."""
    principal = getattr(getattr(request, "state", None), "rate_principal", None)
    if principal:
        return f"user:{principal}"
    return _client_key(request)


limiter = DynamoRateLimiter(client_key_func=_request_key)


def identity_key(kind: str, raw: str) -> str:
    """The rate-limit bucket for an identity a sign-in request NAMES ITSELF AS,
    not one it merely presents in a free-form header: the username/email a
    `/login` or `/respond` body carries, or the access key id a `/sso-exchange`
    body's `Credential=` names. `kind` separates the two namespaces so a
    username that happened to collide with an access key id could not share a
    bucket with it. Lowercased and stripped so two spellings of the same
    identity (`Alice@Example.com` / `alice@example.com`) cannot each buy their
    own quota -- the whole point is that this bucket is exactly as hard for an
    attacker to multiply as the account it names.
    """
    return f"identity:{kind}:{raw.strip().lower()}"


def check_identity_rate_limit(scope: str, kind: str, raw_identity: str, spec: str) -> None:
    """Run one DynamoDB-backed rate check immediately, keyed on the identity a
    sign-in request names (`identity_key`) rather than on the `Request` object
    the `@limiter.limit(...)` decorator reads -- the identity lives in a body
    field FastAPI has not finished parsing at decoration time, so this has to
    run inside the handler, after `body` exists, and before any expensive work
    (a Cognito call, forwarding to STS) happens for a credential that might be
    one of thousands an attacker is trying. Raises `RateLimitExceeded` (a 429)
    exactly as the decorator does, through the same `_check`/`_parse_spec`
    this module's own `DynamoRateLimiter.limit` uses, so the two mechanisms
    share one failure policy, one TTL-reaped table and one 429 shape.
    """
    from core.rate_limit_ddb import _check, _parse_spec

    limit, window = _parse_spec(spec)
    _check(scope, identity_key(kind, raw_identity), limit, window)


__all__ = [
    "AUTH_ADDRESS_RATE_LIMIT",
    "AUTH_SIGN_IN_ADDRESS_SCOPE",
    "BILLING_WRITE_RATE_LIMIT",
    "LOGIN_RATE_LIMIT",
    "RESPOND_RATE_LIMIT",
    "RateLimitExceeded",
    "SSO_EXCHANGE_RATE_LIMIT",
    "check_identity_rate_limit",
    "identity_key",
    "limiter",
]
