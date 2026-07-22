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
from typing import Callable, Optional

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


def sr_should_consult(tenant_id: str, conversation_id: Optional[str]) -> bool:
    """The single gate the request handlers call before consulting SR. Combines
    the per-tenant mode with canary sampling so every surface shares one rule:

      * mode "off"    → never (dark).
      * mode "active" → always.
      * mode "canary" → only for the deterministic, session-sticky canary slice
        (a whole conversation is either in or out, so a session never straddles),
        AND only while the circuit breaker is not tripped.

    Cheap and fail-safe: any error resolves to False (do not consult), so a
    config/canary hiccup degrades to the normal resolver, never an exception."""
    try:
        mode = sr_mode_for(tenant_id)
        if mode == "off":
            return False
        if mode == "active":
            return True
        # canary
        from . import canary as _canary

        if _canary.circuit_open():
            return False
        return _canary.in_canary(tenant_id, conversation_id or "")
    except Exception:  # noqa: BLE001 — a gate error must not break a request.
        return False


def _default_is_known_model(model_id: str) -> bool:
    """True iff `model_id` resolves to a registry entry (the identity-mapping
    membership test decision_map uses). Never raises."""
    try:
        from ..models import resolve_model

        resolve_model(model_id)
        return True
    except Exception:  # noqa: BLE001 — unknown model ⇒ not identity-mappable.
        return False


def decide(
    *,
    tenant_id: str,
    session_key: Optional[str],
    requested_model: str,
    has_tool_result: bool,
    messages: Optional[list] = None,
    is_known_model: Optional[Callable[[str], bool]] = None,
) -> RouteDecision:
    """Consult the vLLM Semantic Router DECIDE surface (`POST /api/v1/eval`) and
    return a source-agnostic RouteDecision.

    Flow (architecture A'): if SR is active for this tenant AND `messages` were
    supplied, POST them to the router's decision-only endpoint, map the returned
    `decision_name` to a registry model_id (via `mvp.sr.decision_map`), and return
    it as a SOFT `prefer_model` — it only reorders the servable candidate chain and
    can never turn a servable request into a 402/403, so it passes the SAME
    allowlist/servability gate as a client pin. Money is never touched here.

    Fail-open on EVERYTHING: SR off for the tenant, no messages, no base URL,
    transport error, timeout past the deadline, a bad body, or an unmapped
    decision → NO_DECISION (the normal resolver runs). This never raises on the hot
    path — a router outage is a routing problem, not a money problem.

    `messages` defaults to None so a caller that has not wired it (or a unit test)
    is byte-neutral. `is_known_model` is injectable for tests; production uses the
    registry membership test."""
    if not sr_active_for(tenant_id):
        return NO_DECISION
    if not messages:
        return NO_DECISION
    try:
        from . import eval_client
        from .decision_map import make_normalizer

        normalizer = make_normalizer(is_known_model or _default_is_known_model)
        return eval_client.consult_eval(messages=messages, normalize=normalizer)
    except Exception as e:  # noqa: BLE001 — fail-open; a decide error never breaks a request.
        logger.warning("sr_decide_failed", tenant_id=tenant_id, error=str(e))
        return NO_DECISION
