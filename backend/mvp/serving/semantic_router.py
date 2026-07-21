"""vLLM Semantic Router transport branch (SR integration, option B).

Mirrors the structure of ``serving/vllm.py``: this module is the ENTIRE SR
transport surface, reachable only under an explicit flag and a committed
``served_by == "semantic-router"`` target. SR is an EXECUTING gateway, so — unlike
the decision-only shape first assumed — Stratoclave reserves BEFORE forwarding and
settles from router-replay evidence (see mvp/sr/CONTRACT.md, IMPLEMENTATION_PLAN.md).

STAGE S1 (this commit): stub only. ``sr_is_servable`` returns False for every
input, so an SR virtual entry can exist in the type system but can NEVER be
selected — the request path is byte-identical to today. The reserve→forward→settle
machinery (PoolReservation, forward_to_sr, two-phase settle) lands in later
substeps (S2–S6). Nothing here constructs an HTTP client yet.

INVARIANT (holds from S1 onward): money is fail-closed — no code path forwards to
SR without a consumed reservation token; routing is fail-open — SR unservable ⇒
the candidate chain falls back to the direct Bedrock default.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

from core.logging import get_logger

from ..sr.reservation import ConsumedProof

logger = get_logger(__name__)

# Timeouts (Fable IMPLEMENTATION_PLAN §4). Explicit, not defaulted: an SR that
# accepts a connection then never chunks must be reaped promptly. total_wall
# scales with the reserved cap (more tokens ⇒ legitimately longer generation).
_CONNECT_TIMEOUT_S = 2.0
_FIRST_BYTE_TIMEOUT_S = 15.0
_INTER_CHUNK_IDLE_S = 30.0


def _total_wall_s(max_tokens_cap: int) -> float:
    return min(120.0, 8.0 + max(max_tokens_cap, 0) * 0.06)


def semantic_router_enabled() -> bool:
    """Master switch, checked at request time (not import) so an operator can flip
    it via env without a code change — same pattern as HYBRID_SERVING_ENABLED /
    SAAR_ENABLED. Defaults to False: SR ships dark."""
    return os.getenv("SEMANTIC_ROUTER_ENABLED", "false").lower() == "true"


def sr_is_servable(entry, tenant_id: str, now: float) -> bool:
    """Whether an SR virtual pool entry is servable for this tenant right now.

    STAGE S1: always False — SR is never selected yet, so a "semantic-router"
    entry in the registry (there are none) or a candidate chain can carry the
    type without ever routing through SR. This keeps the hot path byte-identical
    while the money-path substeps are built and verified against a fake SR.

    Later substeps replace this with the real gate (Fable IMPLEMENTATION_PLAN §1):
    SR /healthz freshness, candidate-pool non-empty, every pool model enabled +
    priced + registry-known, SR /v1/models sync freshness, and a servable
    Bedrock fail-open default. Until then, returning False is the safe stub."""
    return False


# ---------------------------------------------------------------------------
# S3: forward to SR (reserve-before-forward, enforced by the ConsumedProof arg)
# ---------------------------------------------------------------------------


class SrForwardError(Exception):
    """Any failure of the SR forward (transport, timeout, protocol). The caller
    treats it as fail-open: fall back to the direct default path, which the
    pool-max reserve already covers. NEVER surfaces as a money error."""


@dataclass(frozen=True)
class SrForwardRequest:
    """The request Stratoclave forwards to SR. `messages` is the OpenAI-compatible
    body; `logical_model` is the SR auto-name (e.g. "auto"). `max_tokens_cap` is
    force-injected (overriding any client value) so reserve >= real cost holds."""
    tenant_id: str
    span_id: str
    logical_model: str
    messages: tuple
    max_tokens_cap: int
    pool_hash: str


@dataclass(frozen=True)
class SrForwardResult:
    """The parsed SR response. `chosen_model_raw` is SR's model string (normalized
    to the registry at settle). `usage` is the provisional token count (None ⇒
    settle at reserve). `replay_id` is SR's x-vsr-replay-id (async final settle
    key). `raw_cost_microusd` is SR's own cost figure — EVIDENCE only, never the
    charge."""
    chosen_model_raw: str
    usage_input_tokens: Optional[int]
    usage_output_tokens: Optional[int]
    replay_id: Optional[str]
    raw_cost_microusd: Optional[int] = None


# Fault-injection seam for the fake SR harness: tests set this to a callable
# (request) -> SrForwardResult | raises, so every failure mode (timeout,
# first-byte drop, replay-id missing, out-of-snapshot model, double-fire) is
# exercised without real hardware. None ⇒ the real httpx transport.
_transport_hook = None


def set_transport_hook(fn) -> None:
    global _transport_hook
    _transport_hook = fn


def reset_for_test() -> None:
    set_transport_hook(None)


def forward_to_sr(proof: ConsumedProof, request: SrForwardRequest) -> SrForwardResult:
    """Forward a request to SR AFTER a reservation has been consumed.

    The `proof: ConsumedProof` first argument is the type-level enforcement of
    'no forward without a reserve' (§3): a ConsumedProof can only come from
    PoolReservation.consume(), which only the reserve path mints. This function
    is unreachable on the hot path in S3 (sr_is_servable is still False); it is
    exercised by the fake-SR harness to close the money invariants before real
    hardware.

    Guarantees:
      * max_tokens force-injected = proof.max_tokens_cap (upper-bound the spend).
      * span_id propagated so settle can join SR's replay by (run_id, span_id).
      * fail-open: any transport/protocol error raises SrForwardError, which the
        caller turns into a direct-path fallback (covered by the pool-max reserve).
    """
    if proof.max_tokens_cap != request.max_tokens_cap:
        # defence in depth: the forwarded cap MUST equal the reserved cap, else
        # the upper-bound (reserve >= cost) no longer holds. Refuse rather than
        # forward a request that could exceed its reservation.
        raise SrForwardError("max_tokens_cap mismatch between reservation and request")
    if _transport_hook is not None:
        return _transport_hook(request)
    # Real transport lands in a later substep together with mTLS/service-token
    # (§4-§5). Until then the only caller is the fake harness via the hook.
    raise SrForwardError("SR real transport not wired yet (use fake harness)")
