"""Classify which ALB listener a request arrived through.

Only meaningful on the shared internal ALB `STRATOCLAVE_ENTRY=both` builds
(`iac/lib/alb-stack.ts`'s `entryMode` doc): port 80 is always CloudFront's
VPC origin, port 443 is always the internal NLB (PrivateLink). On every
other entry mode there is only one listener, so these checks degrade to
"is it the one listener this deployment has" and are harmless no-ops.

`X-Forwarded-Port` is set by the ALB itself from which of its own listeners
accepted the connection — never a value a client can supply. Unlike
`X-Forwarded-For`, there is no `append`/`preserve`/`remove` mode for it; AWS
documents it only as "the destination port that the client used to connect
to the load balancer," with no configurable behaviour at all. See
docs/DEPLOYMENT.md's "Both" section for the citation.

Reviewed design note (Fable 5.2, both-mode design review): an earlier
version of this classification checked `== "443"` to detect PrivateLink and
trusted everything else by default — a deny-list, which fails OPEN: any
value other than a clean "443" (a future HTTPS CloudFront VPC-origin on a
different port, a malformed header) fell through to the trusting branch.
`is_cloudfront_leg` below is a POSITIVE match on the known-trusted value
instead — `== "80"` — so anything that is not recognised as the trusted
leg is rejected, not admitted by default, EXCEPT for one case: the header
being entirely ABSENT. A deployment with only one ALB listener (every mode
except `both`) behaves identically before and after this module existed,
whether or not its request happens to carry this header — only a PRESENT,
wrong-valued header (a confirmed non-CloudFront leg) changes the outcome.
`is_privatelink_leg` has no such fallback: it is consulted only from code
paths that already know they are on a `both`-mode shared ALB, where the
header is always present.
"""
from __future__ import annotations

from fastapi import Request

_CLOUDFRONT_LISTENER_PORT = "80"
_PRIVATELINK_LISTENER_PORT = "443"


def is_cloudfront_leg(request: Request) -> bool:
    """True when `X-Forwarded-Port` positively matches the CloudFront
    VPC-origin listener (80), OR when the header is absent entirely (no
    ALB in the path told us otherwise — every entry mode except `both`
    never sends this header in a test fixture, and must not have its
    existing trusted-hops behaviour changed by this function's mere
    existence). False for anything ELSE — a header present with any other
    value, including the PrivateLink listener's "443" — which is the
    fail-closed case: `_trusted_hops()` is not applied, and the caller
    falls back to the ALB's own observed peer.
    """
    forwarded_port = request.headers.get("x-forwarded-port")
    if forwarded_port is None:
        return True
    return forwarded_port == _CLOUDFRONT_LISTENER_PORT


def is_privatelink_leg(request: Request) -> bool:
    """True only when `X-Forwarded-Port` positively matches the internal
    NLB (PrivateLink) listener (443). No absent-header fallback: every
    caller of this function already knows it is looking at a `both`-mode
    shared ALB request, where the header is always present."""
    return request.headers.get("x-forwarded-port") == _PRIVATELINK_LISTENER_PORT
