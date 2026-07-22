"""S6: canary sampling + circuit breaker (Fable IMPLEMENTATION_PLAN §6).

`sr_mode="canary"` routes a *fraction* of a tenant's traffic through the full
reserve→forward→settle SR path; the rest uses the direct path. Two properties
matter:

  * DETERMINISTIC, session-sticky sampling — a conversation is either entirely on
    SR or entirely off it, so a session's model does not flip mid-conversation
    (UX + clean evidence). We hash (tenant_id, conversation_id), never random.
  * a CIRCUIT BREAKER that trips the SR path off (fail-open to direct) when SR
    misbehaves. Under architecture A' (the shipping path — SR is consulted via
    the decision-only /api/v1/eval, see CONTRACT.md) the trip conditions are the
    eval timeout/error rate and the unmapped-decision rate. (The forward error /
    replay-miss / out-of-snapshot vocabulary belonged to the frozen option-B
    execute-forward path and is not what trips this under A'.) Tripping is the
    safe default; recovery is time-based.

    SCOPE (P2-2, be honest): the breaker state is PER-PROCESS in-memory. In a
    multi-replica deployment a trip stops SR on the tripping pod only; sibling
    pods keep routing until they each independently observe the fault and trip.
    That is acceptable because money stays fail-closed regardless of the breaker
    (a still-routing pod still reserves before forwarding). The nearest thing to a
    fleet-wide hard stop is the STRATOCLAVE_SR_FORCE_OFF env kill-switch, but note
    it too is only as fleet-wide as the deploy mechanism that propagates it: env
    vars are fixed at process start, so flipping it takes effect as each process
    restarts / re-reads config, NOT instantly across a running fleet. A
    shared-store (config push / Redis) breaker for true instant fleet-wide auto
    trip is deferred; until then do NOT claim instant fleet-wide from either the
    breaker or the env switch.

Pure/deterministic where it can be: `in_canary` is a pure function of its inputs,
so a decision is reproducible for incident forensics.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass


def _canary_bps_default() -> int:
    """Global canary fraction in basis points (1 bps = 0.01%). Default 100 = 1%.
    Clamped to [0, 10000]."""
    try:
        v = int(os.getenv("STRATOCLAVE_SR_CANARY_BPS", "100"))
    except ValueError:
        v = 100
    return max(0, min(10000, v))


def in_canary(tenant_id: str, conversation_id: str, *, canary_bps: int | None = None) -> bool:
    """Deterministic, session-sticky canary membership. Same (tenant, conversation)
    always yields the same answer, so a conversation never straddles SR and direct.
    bps=0 ⇒ never; bps=10000 ⇒ always."""
    bps = _canary_bps_default() if canary_bps is None else max(0, min(10000, canary_bps))
    if bps <= 0:
        return False
    if bps >= 10000:
        return True
    h = hashlib.sha256(f"{tenant_id}\x00{conversation_id}".encode("utf-8")).digest()
    bucket = int.from_bytes(h[:4], "big") % 10000
    return bucket < bps


# --------------------------------------------------------------------------- breaker
@dataclass
class _BreakerState:
    open_until: float = 0.0
    trips: int = 0


_state = _BreakerState()
_lock = threading.Lock()

# How long a trip keeps SR off (seconds). Manual STRATOCLAVE_SR_FORCE_OFF is the
# hard kill; this is the automatic, self-healing one.
_OPEN_SECONDS = 600.0


def _now() -> float:
    return time.monotonic()


def circuit_open() -> bool:
    """True while the breaker is tripped (this process) — SR path is skipped
    (fail-open to direct)."""
    with _lock:
        return _now() < _state.open_until


def trip(reason: str) -> None:
    """Trip the breaker: SR off in THIS process for _OPEN_SECONDS (see module
    docstring on per-process scope). Idempotent. Under A' the trip conditions are
    eval timeout/error rate and unmapped-decision rate (the out-of-snapshot /
    replay-miss vocabulary belonged to the frozen option-B forward path). Nearest
    fleet-wide stop = STRATOCLAVE_SR_FORCE_OFF, itself only as fast as config
    propagation (see module docstring)."""
    from core.logging import get_logger
    with _lock:
        _state.open_until = _now() + _OPEN_SECONDS
        _state.trips += 1
        trips = _state.trips
    get_logger(__name__).warning("sr_circuit_tripped", reason=reason, trips=trips)


def reset_for_test() -> None:
    global _state
    with _lock:
        _state = _BreakerState()
