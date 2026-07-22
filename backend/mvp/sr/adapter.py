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


# Dedicated concurrency cap for offloading the blocking SR consult from async
# handlers, so a slow SR cannot starve Starlette's shared sync-handler threadpool
# (Fable round2 b). Small: consults are short; saturation ⇒ the caller skips the
# consult (fail-open), never queues. Lazily built on first use (anyio limiters must
# be created inside a running loop context to be safe to reuse).
_sr_limiter = None
_sr_limiter_lock = None


def sr_thread_limiter():
    """The anyio CapacityLimiter for SR consult offload. Created once, reused."""
    global _sr_limiter, _sr_limiter_lock
    import threading as _t

    import anyio

    if _sr_limiter_lock is None:
        _sr_limiter_lock = _t.Lock()
    with _sr_limiter_lock:
        if _sr_limiter is None:
            _sr_limiter = anyio.CapacityLimiter(8)
        return _sr_limiter


def sr_eval_deadline_s() -> float:
    """The eval consult deadline (seconds), mirrored from the eval client so the
    async handler's fail_after can bound the offloaded call."""
    from . import eval_client

    return eval_client._deadline_s()


def _default_is_known_model(model_id: str) -> bool:
    """True iff `model_id` resolves to a registry entry AND is priced (the
    identity-mapping membership test). Fable 5: this MUST match the CI gate's
    priced+enabled predicate, so an SR decision can only identity-map to a model
    the ledger can actually price — never one the reserve could not cost. Never
    raises (unknown/unpriced ⇒ False ⇒ NO_DECISION, fail-open)."""
    if not model_id:
        return False  # empty would resolve to DEFAULT_MODEL — never identity-map it
    try:
        from ..models import resolve_model
        from ..pricing import estimate_cost_microusd

        entry = resolve_model(model_id)
        if entry is None:
            return False
        # a model the rater cannot price would break the reserve upper bound; treat
        # it as unmappable. A trivial (1,1) estimate just proves pricing resolves
        # for this model's pricing_key (raises otherwise).
        estimate_cost_microusd(
            pricing_key=entry.pricing_key, input_tokens_est=1, max_output_tokens=1)
        return True
    except Exception:  # noqa: BLE001 — unknown/unpriced ⇒ not identity-mappable.
        return False


def decide(
    *,
    tenant_id: str,
    session_key: Optional[str],
    requested_model: str,
    messages: Optional[list] = None,
    has_tool_result: bool = False,   # accepted for the RoutePort shape; unused here
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
        # ALWAYS soft (hard=False): an SR decision may only REORDER the servable
        # candidate chain, never disable the cascade — so it can never turn a
        # servable request into a 402/403 (Fable 1). A future hard-pin mode must be
        # gated behind an explicit per-tenant policy checked HERE, not by flipping
        # consult_eval's arg; until that policy exists, SR is soft-only.
        return eval_client.consult_eval(messages=messages, normalize=normalizer, hard=False)
    except Exception as e:  # noqa: BLE001 — fail-open; a decide error never breaks a request.
        logger.warning("sr_decide_failed", tenant_id=tenant_id, error=str(e))
        return NO_DECISION


# How much of a conversation to send to /api/v1/eval. The classifier only needs
# recent context; sending the whole history bloats the POST (⇒ write-timeout ⇒
# permanent fail-open) and widens the prompt egress. Cap to the last N messages
# and a total char budget, and drop non-text / non-dict items (Fable 4).
_EVAL_MAX_MESSAGES = 12
_EVAL_MAX_CHARS = 24_000


# Only conversational roles are sent to SR. A `tool`/`function` message's content
# is a STRING (tool output) and would otherwise pass the str-content check, leaking
# tool results into the prompt egress — so role is allowlisted (Fable 4 blocker).
_EVAL_ROLES = frozenset({"user", "assistant", "system"})
# text-part type tags across Anthropic (`text`) and OpenAI Responses (`input_text`
# / `output_text`). Only these part types' text is extracted; everything else
# (images, tool_use/tool_result blocks, audio) is dropped.
_EVAL_TEXT_PART_TYPES = frozenset({"text", "input_text", "output_text"})


def _extract_text(content) -> Optional[str]:
    """Pull plain text from a message's content, dropping non-text parts. Accepts a
    str (⇒ itself) or an Anthropic/OpenAI parts list (⇒ concatenated text-part
    text). Returns None if no text (⇒ the message is skipped). Never str()-coerces
    an unknown object (Fable 4: no arbitrary object into the prompt egress)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in _EVAL_TEXT_PART_TYPES:
                t = part.get("text")
                if isinstance(t, str) and t:
                    texts.append(t)
        return "\n".join(texts) if texts else None
    return None


def prepare_eval_messages(raw) -> list:
    """Build a compact, text-only OpenAI-shaped message list for the eval consult
    from a handler's message/input container. PURE, defensive, never raises:

      * accepts a str (⇒ one user message), a list of dicts / pydantic models, or
        anything else (⇒ []);
      * keeps only conversational roles (user/assistant/system) — a `tool`/
        `function` message is dropped even though its content is a string, so tool
        outputs never reach SR (Fable 4);
      * extracts plain text from str OR Anthropic/OpenAI text-part lists, dropping
        images / tool blocks / arbitrary objects;
      * keeps the LAST _EVAL_MAX_MESSAGES and trims to _EVAL_MAX_CHARS total.
    Returns [] when nothing usable remains (⇒ decide() no-ops, fail-open)."""
    try:
        if isinstance(raw, str):
            raw = [{"role": "user", "content": raw}]
        if not isinstance(raw, list):
            return []
        norm = []
        for m in raw:
            if hasattr(m, "model_dump"):
                m = m.model_dump()
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            if role not in _EVAL_ROLES:
                continue  # drop tool/function/unknown roles (no tool-output egress)
            text = _extract_text(m.get("content"))
            if not text:
                continue  # skip images / tool blocks / empty
            norm.append({"role": role, "content": text})
        norm = norm[-_EVAL_MAX_MESSAGES:]
        # drop from the FRONT until under the char budget (keep the most recent);
        # but if a SINGLE (last) message alone exceeds the budget, TRUNCATE its tail
        # rather than dropping it — a lone huge user prompt is the most common case
        # and must still reach SR (Fable round3: drop-all made SR silent there).
        total = sum(len(m["content"]) for m in norm)
        while len(norm) > 1 and total > _EVAL_MAX_CHARS:
            total -= len(norm[0]["content"])
            norm = norm[1:]
        if norm and len(norm[0]["content"]) > _EVAL_MAX_CHARS:
            norm[0] = {**norm[0], "content": norm[0]["content"][:_EVAL_MAX_CHARS]}
        return norm
    except Exception:  # noqa: BLE001 — prep must never break a request.
        return []
