"""vLLM Semantic Router transport branch — FROZEN option-B execute-forward path.

Mirrors the structure of ``serving/vllm.py``: this module is the ENTIRE SR
execute-forward surface for the option-B design (Stratoclave reserves pool-max,
forwards to an executing SR, settles from router-replay evidence).

STATUS: FROZEN and dark under architecture A' (see mvp/sr/CONTRACT.md). A live run
established that SR exposes a decision-only endpoint (`POST /api/v1/eval`), so the
shipping path is A' — consult eval, then reserve+execute on Stratoclave's own
transport — which does NOT forward money-bearing traffic to SR. This module is
therefore never reached: ``sr_is_servable`` returns False for every input, so a
``served_by == "semantic-router"`` virtual entry can carry the type but can NEVER
be selected, and the request path is byte-identical to today. Its P1/P2/P3 review
findings were fixed BEFORE freezing, so a future unfreeze (only on measured demand
for a model that exists solely behind SR's pool — see CONTRACT.md "Unfreeze
condition") inherits corrected code, not a stale "verified" label.

INVARIANT (still holds while frozen): money is fail-closed — no code path forwards
to SR without a consumed reservation token; routing is fail-open — SR unservable ⇒
the candidate chain falls back to the direct Bedrock default.
"""
from __future__ import annotations

import os
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
    """Whether an SR virtual pool entry is servable (i.e. whether the option-B
    execute-forward path may run) for this tenant right now.

    Under architecture A' this is ALWAYS False and stays that way: A' never
    forwards money-bearing traffic to SR (it consults eval and executes on its own
    transport), so the execute-forward path is frozen. A "semantic-router" virtual
    entry can carry the type but can NEVER be selected, keeping the hot path
    byte-identical. This returns True only if the option-B forward is ever
    unfrozen (see CONTRACT.md "Unfreeze condition"), at which point the real gate
    lands here (SR /healthz freshness, non-empty priced candidate pool, /v1/models
    sync, servable Bedrock fail-open default)."""
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

    NOT guaranteed here — an OPEN gap, documented so it is not mistaken for closed:
    a ConsumedProof is single-MINT (one reservation → one proof) but this function
    does NOT burn the proof, so the same proof could be forwarded twice with two
    distinct-but-consistent requests — a 1-reserve → 2-execute double-spend path.
    The pieces that would fence it DO NOT YET EXIST: `SrForwardRequest` carries no
    reservation_id/idempotency-key field, and the ledger's (reservation_id, phase)
    unique constraint stops double-CHARGE but NOT double-EXECUTION (the 2nd run's
    provider cost falls outside the reserve; only SR-side dedupe would stop it, and
    that upstream capability is unverified). This module is frozen under A' (no
    money-bearing forward), so the gap is inert — but it is listed in CONTRACT.md's
    unfreeze checklist as a MUST-implement-and-verify item, not a solved problem.
    """
    # P2-1: bind the proof to THIS request on all three money-relevant axes, not
    # only the cap. A ConsumedProof carries no tenant_id of its own, but its pool
    # snapshot does — so a tenant-A proof paired with a tenant-B request (a mix-up
    # that the type system alone cannot catch) is refused here. Likewise a
    # pool_hash mismatch means the request was priced against a different snapshot
    # than the one reserved, breaking the reserve>=cost upper bound.
    if proof.max_tokens_cap != request.max_tokens_cap:
        # the forwarded cap MUST equal the reserved cap, else the upper-bound
        # (reserve >= cost) no longer holds.
        raise SrForwardError("max_tokens_cap mismatch between reservation and request")
    if proof.pool.tenant_id != request.tenant_id:
        raise SrForwardError("tenant mismatch between reservation and request")
    if proof.pool.pool_hash != request.pool_hash:
        raise SrForwardError("pool_hash mismatch between reservation and request")
    if _transport_hook is not None:
        return _transport_hook(request)
    # Real transport lands in a later substep together with mTLS/service-token
    # (§4-§5). Until then the only caller is the fake harness via the hook.
    raise SrForwardError("SR real transport not wired yet (use fake harness)")
