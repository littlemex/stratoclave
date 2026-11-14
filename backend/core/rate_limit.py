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

import hashlib
import logging
import os

from fastapi import HTTPException, Request

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


from core.forwarded_port import is_cloudfront_leg  # noqa: E402

_BOTH_CLOUDFRONT_TRUSTED_HOPS_ENV = "STRATOCLAVE_BOTH_CLOUDFRONT_TRUSTED_HOPS"


def _both_mode_cloudfront_trusted_hops() -> int | None:
    """The CloudFront leg's trusted-hop count under ``STRATOCLAVE_ENTRY=both``
    (``iac/lib/_common.ts``'s ``BOTH_MODE_CLOUDFRONT_TRUSTED_HOPS``), or
    ``None`` on every other entry mode and on a ``both`` deployment running
    an image old enough that ``bin/iac.ts`` had not yet started setting this
    variable.

    Deliberately NOT ``_trusted_hops()`` (``RATE_LIMIT_TRUSTED_HOPS``) in
    this mode: ``bin/iac.ts`` sets ``RATE_LIMIT_TRUSTED_HOPS=0`` on the SAME
    container, on purpose, precisely so an OLDER backend image — one that
    predates this module's leg classification and reads only
    ``RATE_LIMIT_TRUSTED_HOPS`` — trusts no XFF at all on the shared ALB,
    rather than inheriting whatever value an operator had set for a plain
    single-leg deployment. Reading ``_trusted_hops()`` here would read back
    that forced 0 and silently defeat the whole mechanism for every request,
    on every deployment new enough to look for this variable at all.
    """
    raw = os.getenv(_BOTH_CLOUDFRONT_TRUSTED_HOPS_ENV, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _effective_trusted_hops(request: Request) -> int:
    """``_trusted_hops()`` (or, under ``both``, the dedicated CloudFront-leg
    hop count above), but ONLY on the request's CloudFront leg. Everything
    else — including a confirmed PrivateLink request, a missing or
    malformed ``X-Forwarded-Port``, or any value this function does not
    positively recognise — gets 0.

    ``STRATOCLAVE_ENTRY=both`` (iac/bin/iac.ts) puts CloudFront (via a VPC
    origin, port 80) and the internal NLB/PrivateLink (port 443) on the SAME
    ALB. Classification is by `core.forwarded_port.is_cloudfront_leg` — a
    POSITIVE match on the known-trusted port (and, round-1 implementation
    review: on EXACTLY ONE value of the header, via `Request.headers.
    getlist`, not `.get`'s first-of-duplicates), not a negative match on the
    known-untrusted one. The two are not equivalent: a positive match fails
    CLOSED (0 trusted hops: use the ALB's own observed peer) on anything
    unrecognised, including a future change this function does not know
    about (e.g. an HTTPS CloudFront VPC origin landing on a different port,
    or a duplicate header this deployment's own ALB turns out to append
    rather than overwrite — see `core/forwarded_port.py`'s own doc on why no
    claim about ALB overwrite semantics is made here at all, only verified
    by a live spoof test). A negative match (checking only "is this port
    443") would instead have failed OPEN — trusting ``X-Forwarded-For`` on
    anything that is not exactly ``"443"`` — which is the direction that
    matters here: trusting a forged client-controlled header on the
    rate-limited auth routes is a bypass, where distrusting a legitimate
    CloudFront request only produces a coarser bucket (`get_remote_address`'s
    fallback).

    0 here is the SAME value ``RATE_LIMIT_TRUSTED_HOPS=0`` already produces
    (operator override: "do not trust any XFF"), so this reuses that
    existing branch in `_client_key` rather than adding a new one.

    A pure ``STRATOCLAVE_ENTRY=private`` deployment needs no special case
    here at all: its ALB-wide ``xff_header_processing.mode=remove``
    (lib/alb-stack.ts) already strips ``X-Forwarded-For`` before this
    function's caller ever sees it, so the ``if not xff`` branch in
    `_client_key` always returns first. A plain ``STRATOCLAVE_ENTRY=cloudfront``
    deployment (no PrivateLink leg at all) always arrives on port 80, so
    `is_cloudfront_leg` is true for every request and this is a no-op there.
    """
    both_hops = _both_mode_cloudfront_trusted_hops()
    if both_hops is not None:
        return both_hops if is_cloudfront_leg(request) else 0
    if is_cloudfront_leg(request):
        return _trusted_hops()
    return 0


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

    hops = _effective_trusted_hops(request)
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

# Sign-in routes keep their OWN per-route address bucket (the `@limiter
# .limit(LOGIN_RATE_LIMIT)`-style decorator, at the SAME numbers this module
# always had: 10/minute login, 10/minute respond, 20/minute sso-exchange,
# 20/minute ui-ticket/consume) rather than one shared, looser bucket.
#
# Pooling every sign-in route into ONE shared address-keyed scope at a much
# higher ceiling (say, 300/minute, reasoning that normal shared egress -- a
# NAT, or the private entry's single load-balancer address -- needs a
# generous floor) would add nothing a per-route bucket at these numbers does
# not already give for free: the sum of the four routes' own limits
# (10 + 10 + 20 + 20 = 60/minute) is already well under such a ceiling, so a
# shared bucket would never bind before a route's own did -- a strictly
# looser outer guard with no observable effect, at the cost of a second
# scope to reason about. Each route's own per-address decorator IS the
# flood guard for that route.
#
# What is NOT per-route is identity-guessing protection: the tight,
# credential-guessing limits (`LOGIN_RATE_LIMIT` etc.) are enforced by a
# SEPARATE check keyed on BOTH the identity the request names AND the
# client's own address (`identity_key`/`check_identity_rate_limit` below),
# run explicitly inside each handler -- the identity lives in the request
# BODY, which a `Request`-only decorator cannot read, and before body
# validation the identity is not even attacker-controlled yet. Binding the
# address into that key is the fix for a real attack: without it, an
# attacker names a victim's email from as many addresses as they control, and
# each address gets its OWN 10/minute budget against the SAME victim bucket
# -- ten guesses, discard the address, repeat, and the victim is locked out
# of their own account by requests that never came from anywhere the victim
# would ever log in from. With the address in the key, every attacking
# address only ever spends ITS OWN (victim, that address) bucket; the
# victim's bucket for their OWN address is untouched. See docs/DEPLOYMENT.md's
# rate-limit section for the full threat model, including what this does and
# does not do on the private entry, where every client shares one address.


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
    Unauthenticated routes (sign-in) key on the address here too -- this IS
    that route's own flood guard, at the numbers this module always used
    (`LOGIN_RATE_LIMIT` etc., applied directly as the decorator's own scope).
    Credential-guessing protection is the SEPARATE identity-plus-address check
    each sign-in handler runs itself (`check_identity_rate_limit`), which this
    address-only bucket does not replace: binding only the address here would
    let an attacker trying many different identities from one address spend
    only this one flood-guard budget while writing an unbounded number of
    identity counters. A request that fails authentication never reaches the
    limiter."""
    principal = getattr(getattr(request, "state", None), "rate_principal", None)
    if principal:
        return f"user:{principal}"
    return _client_key(request)


limiter = DynamoRateLimiter(client_key_func=_request_key)


#: A generous cap on the identity string a sign-in request may name, well
#: above any real email (RFC 5321's own limit is 254 bytes) or access key id
#: (20 characters), chosen so the cap itself never rejects a real identity --
#: its only job is to stop an unbounded string from reaching DynamoDB at all.
#: Without it, a caller could name an arbitrarily long "email", and whatever
#: DynamoDB itself does with an oversize key item (a `ValidationException`,
#: which this module's own documented failure policy reads as "misconfigured
#: table" and fails CLOSED on -- see `rate_limit_ddb._check`) is a response an
#: attacker chose, not one this module decided.
MAX_IDENTITY_LENGTH = 320


def sign_in_address(request: Request) -> str:
    """The address an identity bucket is bound to, alongside the identity
    itself (`identity_key`) -- the same address `_client_key` already
    derives for the decorator's own flood-guard bucket, exposed here so a
    sign-in handler can read it once and pass it to both."""
    return _client_key(request)


def identity_key(kind: str, raw: str, address: str) -> str:
    """The rate-limit bucket for an identity a sign-in request NAMES ITSELF AS,
    not one it merely presents in a free-form header -- the username/email a
    `/login` or `/respond` body carries, or (post-verification only; see
    `check_identity_rate_limit`) the account a `/sso-exchange` caller's
    signature proved -- bound together with the CLIENT'S OWN ADDRESS.

    The address half is load-bearing, not incidental: an identity-only bucket
    is a lever an attacker who does not hold the credential can still pull --
    name a victim's email from address after address, ten guesses each, and
    the victim's own sign-in attempts share that same exhausted bucket no
    matter which address they come from. Binding the address means every
    attacking address only ever spends ITS OWN (identity, address) bucket;
    the victim's bucket, keyed on the victim's OWN address, is never touched
    by a request that never came from there.

    `kind` separates login/respond's email namespace from sso-exchange's
    account namespace so one could not collide with the other. The identity
    half is lowercased and stripped (so `Alice@Example.com` and
    `alice@example.com` share one bucket) then hashed with SHA-256 before it
    becomes part of a DynamoDB key: the raw value would otherwise be
    unbounded-length PII sitting in a rate-limit table whose only job is
    counting. Empty or over `MAX_IDENTITY_LENGTH` raises 400 rather than
    either silently skipping the check or handing DynamoDB a value an
    attacker chose the shape of.
    """
    normalized = raw.strip().lower()
    if not normalized:
        raise HTTPException(status_code=400, detail=f"{kind} identity must not be empty")
    if len(normalized) > MAX_IDENTITY_LENGTH:
        raise HTTPException(status_code=400, detail=f"{kind} identity is too long")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"identity:{kind}:{digest}:{address}"


def check_identity_rate_limit(scope: str, kind: str, raw_identity: str, address: str, spec: str) -> None:
    """Run one DynamoDB-backed rate check immediately, keyed on the identity a
    sign-in request names TOGETHER WITH the caller's own address
    (`identity_key`) rather than on the `Request` object the
    `@limiter.limit(...)` decorator reads -- the identity lives in a body
    field FastAPI has not finished parsing at decoration time, so this has to
    run inside the handler, after `body` exists.

    For `/login` and `/respond` this runs before any expensive work (the
    Cognito call) because the identity is already fully determined from the
    request body at that point and binding the address closes the
    targeted-lockout gap regardless of ordering. For `/sso-exchange` the
    identity is NOT available pre-verification at all -- an access key id
    parsed out of an unverified `Authorization` header is not evidence its
    holder is making the request -- so that route calls this only AFTER
    `verify_and_call_sts` has proven control of the credential; see
    `mvp/sso_exchange.py`.

    Raises `RateLimitExceeded` (a 429, an `HTTPException` subclass -- see
    `rate_limit_ddb.RateLimitExceeded`) exactly as the decorator does,
    through the same `_check`/`_parse_spec` this module's own
    `DynamoRateLimiter.limit` uses, so the two mechanisms share one failure
    policy, one TTL-reaped table and one 429 shape; FastAPI converts it to a
    429 response the same way it would from the decorator, because the
    conversion happens at the ASGI/exception-handler layer around the whole
    request, not inside the decorator's own wrapper.
    """
    from core.rate_limit_ddb import _check, _parse_spec

    limit, window = _parse_spec(spec)
    _check(scope, identity_key(kind, raw_identity, address), limit, window)


__all__ = [
    "BILLING_WRITE_RATE_LIMIT",
    "LOGIN_RATE_LIMIT",
    "MAX_IDENTITY_LENGTH",
    "RESPOND_RATE_LIMIT",
    "RateLimitExceeded",
    "SSO_EXCHANGE_RATE_LIMIT",
    "check_identity_rate_limit",
    "identity_key",
    "limiter",
    "sign_in_address",
]
