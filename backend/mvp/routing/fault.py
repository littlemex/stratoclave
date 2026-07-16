"""Fault injection for live failover testing.

Gated on SC_FAULT_INJECTION=1 (set on the ECS task, never in prod config).
When enabled, a request-scoped fault spec triggers synthetic Bedrock errors
constructed from REAL botocore exception shapes — so the InfraRouter
classification path is exercised identically to a genuine failure.

Fault specs:
  429-pre            : ThrottlingException before stream opens (every attempt)
  429-attempt-1-only : ThrottlingException on attempt 1 only, succeed after
  503-pre            : ServiceUnavailableException before stream
  fail-region-<R>    : ServiceUnavailableException ONLY when the attempt targets
                       region <R> (e.g. fail-region-us-east-1). Other regions
                       succeed — so a request demonstrates a SUCCESSFUL
                       cross-region failover live (the region-agnostic specs
                       above can only show fail-closed exhaustion, since they
                       fail identically in every region).
  timeout-first-event: hang past first-event timeout (every attempt)
  empty-stream       : return an empty stream (every attempt)
  empty-stream-1     : empty stream on attempt 1 only
  500-mid-stream     : succeed, then raise mid-stream (after first event)
"""
from __future__ import annotations

import os
from typing import Optional

from botocore.exceptions import ClientError

# Per-attempt counter keyed by request_id, so "attempt-1-only" specs can
# distinguish the first attempt from subsequent ones within one request.
_attempt_counters: dict[str, int] = {}


def fault_enabled() -> bool:
    return os.getenv("SC_FAULT_INJECTION") == "1"


def _throttle_error() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "injected throttle"}},
        "ConverseStream",
    )


def _unavailable_error() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ServiceUnavailableException", "Message": "injected unavailable"}},
        "ConverseStream",
    )


def next_attempt(request_id: str) -> int:
    n = _attempt_counters.get(request_id, 0) + 1
    _attempt_counters[request_id] = n
    return n


def clear(request_id: str) -> None:
    _attempt_counters.pop(request_id, None)


def maybe_raise_pre_stream(
    spec: Optional[str],
    request_id: str,
    attempt: int,
    region: Optional[str] = None,
) -> None:
    """Raise a synthetic error before the stream opens, per fault spec.

    `region` is the region of the attempt currently being made; it enables the
    region-targeted `fail-region-<R>` spec (fail only that region so a real
    cross-region failover to a healthy region can be observed end-to-end).
    """
    if not spec or not fault_enabled():
        return
    if spec == "429-pre":
        raise _throttle_error()
    if spec == "503-pre":
        raise _unavailable_error()
    if spec == "429-attempt-1-only" and attempt == 1:
        raise _throttle_error()
    if spec.startswith("fail-region-"):
        target_region = spec[len("fail-region-"):]
        if region is not None and region == target_region:
            # Throttle (not unavailable): ThrottlingException classifies as
            # FAILOVER, so the router advances to the NEXT region immediately
            # rather than burning per-target retries on RETRY_SAME. That makes
            # the cross-region commit the single, clear observable.
            raise _throttle_error()


def maybe_empty_stream(spec: Optional[str], attempt: int) -> bool:
    """Return True if this attempt should yield an empty stream."""
    if not spec or not fault_enabled():
        return False
    if spec == "empty-stream":
        return True
    if spec == "empty-stream-1" and attempt == 1:
        return True
    return False


def maybe_hang(spec: Optional[str]) -> bool:
    if not spec or not fault_enabled():
        return False
    return spec == "timeout-first-event"


def should_fail_mid_stream(spec: Optional[str]) -> bool:
    if not spec or not fault_enabled():
        return False
    return spec == "500-mid-stream"
