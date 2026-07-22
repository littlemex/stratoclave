"""vLLM Semantic Router DECIDE-surface client — `POST /api/v1/eval`.

This is the wiring behind `adapter.decide()` for architecture A': it consults the
real vLLM Semantic Router's DECISION-ONLY endpoint (which returns a routing
decision WITHOUT running inference), maps the returned decision name to a
Stratoclave registry model_id, and hands back a source-agnostic `RouteDecision`.
The chosen model then flows through the SAME allowlist/servability/reserve path as
a client `x-sc-model-pin` — SR never executes our traffic and never touches money.

Contract (verified from the upstream CLI `cli/commands/eval.py`, vllm-sr v0.3.0 —
see mvp/sr/CONTRACT.md and the eval-schema reference):

  request  POST {base}/api/v1/eval
           {"messages": [...], "evaluate_all_signals": true}
  response { "decision_result": {"decision_name": "...", "used_signals": {...},
                                 "matched_signals": {...}, "unmatched_signals": {...}},
             "signal_confidences": {...}, "routing_decision": ... }
           (plus two legacy shapes the parser tolerates: an OpenAI
            `object=="chat.completion"` with a `model`, and a bare `signals` list.)

Auth: the endpoint on the management port (:8080) takes NO Authorization header
(the CLI sends none); reachability is a network-boundary concern (a netpol that
exposes :8080 only to the money-path service), NOT a bearer token. A 403 with an
HTML body means the request hit Envoy (:8899) instead of the router API port.

Everything here is INERT unless SEMANTIC_ROUTER_ENABLED=true AND a base URL is
set. With the flag off, no HTTP client is built and `decide()` returns
NO_DECISION immediately — routing is byte-behaviour-identical to today.

Fail-open is the law: any error, timeout past the deadline, a 4xx/5xx, an
unparseable body, or a decision that does not map to a priced registry model all
yield NO_DECISION (the normal resolver runs). Money is gated by the fail-closed
reserve elsewhere, so a router problem is never a money problem.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from core.logging import get_logger

from .port import NO_DECISION, RouteDecision, SwitchCostHint

logger = get_logger(__name__)


def semantic_router_base_url() -> str:
    """The router management-API base URL (e.g. http://vllm-sr:8080). Empty ⇒ no
    peer to consult ⇒ decide() is a no-op. Trailing slash trimmed."""
    return (os.getenv("SEMANTIC_ROUTER_BASE_URL", "") or "").rstrip("/")


def _deadline_s() -> float:
    """Hot-path consult deadline in seconds. BERT-on-CPU classification is tens of
    ms; we cap at 300ms by default and fail open past it so a slow router can never
    add material latency. Operator-tunable but clamped to a sane band."""
    try:
        v = float(os.getenv("SEMANTIC_ROUTER_EVAL_TIMEOUT_S", "0.3"))
    except ValueError:
        v = 0.3
    return max(0.05, min(5.0, v))


# Fault-injection seam for tests: a callable (messages, base_url, timeout_s) ->
# dict (the parsed JSON body) or raises. When None, the real httpx transport is
# used. Mirrors serving/semantic_router.py's _transport_hook so the money-path
# and decide-path share one testing discipline.
_TransportHook = Callable[[list, str, float], dict]
_transport_hook: Optional[_TransportHook] = None
_client_lock = threading.Lock()
_client = None  # lazily-built httpx.Client


def set_transport_hook(fn: Optional[_TransportHook]) -> None:
    global _transport_hook
    _transport_hook = fn


def reset_for_test() -> None:
    global _client
    set_transport_hook(None)
    with _client_lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:  # noqa: BLE001
                pass
        _client = None


def _get_client(base_url: str):
    global _client
    with _client_lock:
        if _client is None:
            import httpx

            d = _deadline_s()
            _client = httpx.Client(
                base_url=base_url,
                timeout=httpx.Timeout(connect=d, read=d, write=d, pool=d),
            )
        return _client


@dataclass(frozen=True)
class EvalOutcome:
    """A parsed eval response reduced to what decide() needs. `decision_name` is
    the router's chosen decision (mapped to a registry model downstream);
    `raw` keeps the whole body for the observability evidence block. All fields
    optional — a None decision_name means 'no usable decision' (fail-open)."""

    decision_name: Optional[str] = None
    raw: Optional[dict] = None


def parse_eval_response(body: dict) -> EvalOutcome:
    """PURE: extract the chosen decision name from an eval response, tolerating the
    shapes the upstream router / CLI emit (verified against a LIVE v0.3 response:
    it carries `recommended_models`, `decision_result.decision_name`, and
    `routing_decision` together).

    Precedence — prefer the most CONCRETE model signal first:
      1. `recommended_models[0]` — the router's actual model recommendation (a
         concrete model name, live-verified as the most useful mapping target);
      2. `decision_result.decision_name` — the decision RULE name;
      3. a top-level `routing_decision` string, or a dict carrying model/name;
      4. legacy `chat.completion` pass-through — top-level `model`.
    Returns decision_name=None when none is present (⇒ NO_DECISION).

    NB the returned value is still passed through the decision→registry map, so
    whether SR hands back a model name or a rule name, an unmapped value fails
    open — the parser only decides WHICH string the map is asked to resolve."""
    if not isinstance(body, dict):
        return EvalOutcome(None, None)

    rec = body.get("recommended_models")
    if isinstance(rec, list) and rec and isinstance(rec[0], str) and rec[0].strip():
        return EvalOutcome(rec[0].strip(), body)

    dr = body.get("decision_result")
    if isinstance(dr, dict):
        name = dr.get("decision_name")
        if isinstance(name, str) and name.strip():
            return EvalOutcome(name.strip(), body)

    routing = body.get("routing_decision")
    if isinstance(routing, str) and routing.strip():
        return EvalOutcome(routing.strip(), body)
    if isinstance(routing, dict):
        for k in ("model", "decision_name", "name"):
            v = routing.get(k)
            if isinstance(v, str) and v.strip():
                return EvalOutcome(v.strip(), body)

    model = body.get("model")
    if isinstance(model, str) and model.strip():
        return EvalOutcome(model.strip(), body)

    return EvalOutcome(None, body)


def _fetch_eval(messages: list, base_url: str, timeout_s: float) -> dict:
    """Do the actual POST /api/v1/eval (or the injected hook). Returns the parsed
    JSON body; raises on any transport/HTTP/JSON failure (the caller fails open)."""
    if _transport_hook is not None:
        return _transport_hook(messages, base_url, timeout_s)
    client = _get_client(base_url)
    resp = client.post(
        "/api/v1/eval",
        json={"messages": messages, "evaluate_all_signals": True},
        timeout=timeout_s,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"eval non-200: {resp.status_code}")
    return resp.json()


def consult_eval(
    *,
    messages: list,
    normalize,                       # (decision_name:str) -> registry model_id | None
    hard: bool = False,
) -> RouteDecision:
    """Consult the SR decide surface and return a source-agnostic RouteDecision.

    `normalize` maps the router's decision namespace to a Stratoclave registry
    model_id (an unmapped decision ⇒ NO_DECISION + alert, never a raw pass-through
    of an unknown model into the reserve). `hard=False` (default) yields a SOFT
    `prefer_model` — it only reorders the servable candidate chain and can never
    turn a servable request into a 402/403; `hard=True` (an explicit tenant policy)
    yields a `hard_model` pin that disables the cascade, exactly like a client pin.

    Fail-open on EVERYTHING: no base url, transport error, timeout, bad body, or
    unmapped decision → NO_DECISION. Never raises on the hot path."""
    base = semantic_router_base_url()
    if not base:
        return NO_DECISION
    if not messages:
        return NO_DECISION
    try:
        body = _fetch_eval(messages, base, _deadline_s())
    except Exception as e:  # noqa: BLE001 — advisory + fail-open; never break a request.
        logger.info("sr_eval_failed", error=str(e))
        return NO_DECISION

    outcome = parse_eval_response(body if isinstance(body, dict) else {})
    if not outcome.decision_name:
        return NO_DECISION
    model_id = normalize(outcome.decision_name)
    if not model_id:
        # An unmapped decision is a config gap, not a routing opinion we can act on.
        # Alert so the decision→registry map can be extended; do NOT pass an unknown
        # model into the reserve.
        logger.warning("sr_eval_unmapped_decision", decision=outcome.decision_name)
        return NO_DECISION

    if hard:
        return RouteDecision(
            hard_model=model_id,
            switch_cost=SwitchCostHint(),
            reason="sr-eval",
            origin="semantic-router",
        )
    return RouteDecision(
        prefer_model=model_id,
        switch_cost=SwitchCostHint(),
        reason="sr-eval",
        origin="semantic-router",
    )
