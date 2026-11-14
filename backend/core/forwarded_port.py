"""Classify which ALB listener a request arrived through.

Only meaningful on the shared internal ALB `STRATOCLAVE_ENTRY=both` builds
(`iac/lib/alb-stack.ts`'s `entryMode` doc): port 80 is CloudFront's VPC
origin, port 443 is the internal NLB (PrivateLink). On every other entry
mode there is only one listener, so `is_cloudfront_leg` degrades to "true
for every request" and is a no-op.

Round-1 implementation review (both reviews' matching blocker): an earlier
version asserted that `X-Forwarded-Port` is "never a value a client can
supply," citing only an AWS documentation page that states WHAT the header
means, not whether the ALB OVERWRITES a client-supplied one or merely
appends to it the way `X-Forwarded-For` can. That assertion is removed.
This module instead:

  - reads the CloudFront listener port from
    `STRATOCLAVE_BOTH_CLOUDFRONT_PORT` (set by `bin/iac.ts` only under
    `entryMode: 'both'`, from the single constant
    `BOTH_MODE_CLOUDFRONT_LISTENER_PORT` in `iac/lib/_common.ts` — no
    literal port number is hardcoded here at all), rather than hardcoding
    "80" independently of the CDK side;
  - reads every value of the header with `Request.headers.getlist`, not
    `.get` (which silently returns only the FIRST of several same-named
    header lines — the exact gap the Fable review's B3 finding named: if the
    ALB ever appends rather than overwrites, a client sending two
    `X-Forwarded-Port` lines, one matching and one not, would be classified
    by whichever line `.get` happens to return first);
  - treats anything other than EXACTLY ONE matching value as "not this
    leg" — absent, duplicate (even of the same value), or any other value —
    which is the fail-closed shape: a confirmed overwrite-not-append
    behaviour on a real deployment would make this check strictly more
    permissive than it is today, never less, so shipping the strict version
    first and loosening it later (if the live spoof test below confirms it
    is safe to) cannot regress anything that depends on it being strict.

This module makes NO claim about what the ALB does with a client-supplied or
duplicated `X-Forwarded-Port` — that is exactly the premise a live spoof
test settles, not a code comment. See docs/DEPLOYMENT.md's "Both" section
for the verification command and its result once run.
"""
from __future__ import annotations

import os

from fastapi import Request

_CLOUDFRONT_PORT_ENV = "STRATOCLAVE_BOTH_CLOUDFRONT_PORT"


def _configured_cloudfront_port() -> str | None:
    """The CloudFront listener port `bin/iac.ts` configured, or `None` when
    unset — every entry mode except `both` (including `both` deployed from
    an image older than this module's env-driven redesign, which never set
    `STRATOCLAVE_BOTH_CLOUDFRONT_PORT` at all)."""
    value = os.environ.get(_CLOUDFRONT_PORT_ENV, "").strip()
    return value or None


def is_cloudfront_leg(request: Request) -> bool:
    """True when `X-Forwarded-Port` carries EXACTLY ONE value and it
    positively matches the configured CloudFront VPC-origin listener, OR
    when `STRATOCLAVE_BOTH_CLOUDFRONT_PORT` is unset entirely (no ALB in
    this deployment's path ever sends more than one listener — every entry
    mode except `both`, and `both` deployed from an image old enough that
    `bin/iac.ts` had not yet started setting this variable, must not have
    their existing behaviour changed by this function's mere existence).

    False for anything else on a deployment where the variable IS set — a
    header present with a non-matching value, a header repeated (matching
    or not), or no header at all — which is the fail-closed case: callers
    that gate XFF trust or a PrivateLink-only response on this function
    fall back to their own safe default rather than extending trust on an
    ambiguous signal.
    """
    configured_port = _configured_cloudfront_port()
    if configured_port is None:
        return True
    return request.headers.getlist("x-forwarded-port") == [configured_port]


def is_privatelink_leg(request: Request) -> bool:
    """`not is_cloudfront_leg(request)` — not an independent positive match
    on the PrivateLink listener's own port.

    An earlier version matched a second literal ("443") the same way
    `is_cloudfront_leg` matches "80", which is exactly the deny-list shape
    round-1 implementation review flagged on the ORIGINAL version of this
    module (before the fail-closed CloudFront-side rewrite): classifying
    "is this definitely private?" by checking "is this NOT definitely
    CloudFront?" means a value this module does not recognise — a future
    port, a malformed header, every case `is_cloudfront_leg` already fails
    closed on — is treated as PrivateLink rather than as neither. For every
    current caller (`backend/mvp/well_known.py`'s private-API-endpoint
    selection) that is the SAFE direction to be wrong in: handing an
    unidentified requester the private URL fails harmlessly (a CloudFront
    client cannot resolve or reach the internal ALB's DNS name from outside
    the VPC, so its bootstrap fails loudly rather than silently talking to
    the wrong backend) in a way handing an unidentified requester the public
    CloudFront URL does not symmetrically fail for a PrivateLink client.
    Nothing security-sensitive (XFF trust, rate-limit bucketing) is gated on
    this function; only `is_cloudfront_leg` is.
    """
    return not is_cloudfront_leg(request)
