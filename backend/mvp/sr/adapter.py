"""vLLM Semantic Router adapter — the decide-layer RoutePort implementation.

"The SR chooses, Stratoclave accounts — and Stratoclave also executes."
(architecture A', see mvp/sr/CONTRACT.md). This adapter is the ONLY place that
talks to the real vLLM Semantic Router, and it talks to the DECIDE surface only:
`POST /api/v1/eval` on the management API, which returns a routing decision
WITHOUT running inference. SR hands back a `RouteDecision` (a single model name +
hints); that flows through the SAME `vsr_hard_model`/`prefer_model` reserve path,
and Stratoclave then reserves that one model at its exact price and executes it on
its OWN transport (bedrock / self-hosted vLLM). **SR never executes our traffic
and never touches money** — the fail-closed reserve is entirely first-party.

A live run of vllm-sr established that `/api/v1/eval` is decision-only, which is
why A' (decide-only consult) is the shipping path rather than the earlier
option B (front an executing SR and reserve at pool-max). The option-B money
apparatus (reservation.py / settle.py two-phase / hardening.py / forward_to_sr) is
FROZEN and dark (`sr_is_servable()` is False); it is not on this path.

Mode is a per-tenant three-state (`sr_mode`: off | canary | active), resolved
here with the exact pattern the shadow judge's tri-state uses:

  * STRATOCLAVE_SR_FORCE_OFF=true  → OFF for everyone (operator kill-switch,
    outranks per-tenant config; the same pattern as shadow's force-off).
  * tenant `sr_mode` in the routing config wins next.
  * None → global default STRATOCLAVE_SR_MODE_DEFAULT ("off" unless set).

Fail-open is the law of this module: the request path must degrade to the normal
resolver on ANY SR problem (down / slow / REFUSED / garbage). `decide()` returns
`NO_DECISION` rather than raising, because a router outage is a routing problem,
never a money problem — money is gated by the fail-closed reserve elsewhere.
"""
from __future__ import annotations

import os
from typing import Optional

from core.logging import get_logger

from .port import NO_DECISION, RouteDecision

logger = get_logger(__name__)

_MODES = ("off", "canary", "active")


def _env_true(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("true", "1", "yes", "on")


def sr_globally_forced_off() -> bool:
    """Operator kill-switch, checked first and cheap (env only, no I/O). When set,
    SR is OFF for every tenant regardless of stored config — the fleet-wide brake
    if SR starts misbehaving. Mirrors shadow's `shadow_globally_forced_off()`."""
    return _env_true("STRATOCLAVE_SR_FORCE_OFF")


def _global_sr_mode_default() -> str:
    """Global fallback mode when a tenant expresses none. Dark by default: SR
    ships off and a deployment must opt a tenant (or the fleet) in."""
    val = os.getenv("STRATOCLAVE_SR_MODE_DEFAULT", "off").strip().lower()
    return val if val in _MODES else "off"


def sr_mode_for(tenant_id: str) -> str:
    """Resolve the effective SR mode for a tenant: one of _MODES.

    Order: force-off kill-switch → tenant explicit sr_mode → global default.
    Fail-open: any config-read failure resolves to the global default (never
    raises), so an SR-config outage degrades routing to the default, never breaks
    the request. Reads the 60s-TTL-cached routing config (no extra hot-path I/O)."""
    if sr_globally_forced_off():
        return "off"
    try:
        from ..routing.config import get_tenant_routing_config
        mode = get_tenant_routing_config(tenant_id).sr_mode
    except Exception as e:  # noqa: BLE001 — routing decision only; never break a request.
        logger.warning("sr_mode_lookup_failed", tenant_id=tenant_id, error=str(e))
        return _global_sr_mode_default()
    if mode in _MODES:
        return mode
    return _global_sr_mode_default()


def sr_active_for(tenant_id: str) -> bool:
    """True iff SR should be consulted at all for this tenant (mode != off).
    A cheap pre-check the handlers use to skip the SR call entirely when dark."""
    return sr_mode_for(tenant_id) != "off"


def decide(
    *,
    tenant_id: str,
    session_key: Optional[str],
    requested_model: str,
    has_tool_result: bool,
) -> RouteDecision:
    """Consult the vLLM Semantic Router DECIDE surface (`POST /api/v1/eval`) and
    return a source-agnostic RouteDecision.

    Groundwork: the eval HTTP client + auth + latency deadline + decision→registry
    whitelist land in the live-E2E sub-step (see mvp/sr/CONTRACT.md "Preconditions
    before turning A' on"). Until then this is a fully-fail-open no-op returning
    NO_DECISION, so wiring it into the handlers is byte-neutral (the request flows
    through the normal resolver). The mode plumbing above (sr_mode_for /
    kill-switch) is already live and testable.

    When wired: consult eval → map `routing_decision`/`decision_name` to a registry
    model_id (unmapped ⇒ NO_DECISION + alert) → return it as `prefer_model` (or
    `hard_model` under an explicit tenant policy). Fail-open on ANY error, timeout
    past the deadline, or unmapped decision — a router problem is never a money
    problem; money is gated by the fail-closed reserve downstream."""
    if not sr_active_for(tenant_id):
        return NO_DECISION
    # TODO(live-E2E eval client): auth'd /api/v1/eval consult with a latency
    # deadline + decision→registry whitelist. Fail-open on any error/timeout.
    return NO_DECISION
