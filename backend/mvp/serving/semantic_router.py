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

import os


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
