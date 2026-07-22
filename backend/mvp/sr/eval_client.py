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

Gating (be precise — Fable 2e): this module itself only checks
`SEMANTIC_ROUTER_BASE_URL` (empty ⇒ `consult_eval` returns NO_DECISION without
building a client). The per-tenant on/off decision is `adapter.sr_should_consult`
(sr_mode + canary + kill-switch, all default off), which the handlers call BEFORE
`decide()`. So with a default deploy (no base URL, sr_mode off) nothing here runs
and routing is byte-behaviour-identical to today; `SEMANTIC_ROUTER_ENABLED` is the
operator's master intent flag read at the adapter/deploy layer, not here.

Fail-open is the law: any error, timeout past the deadline, a 4xx/5xx, an
unparseable body, or a decision that does not map to a priced registry model all
yield NO_DECISION (the normal resolver runs). Money is gated by the fail-closed
reserve elsewhere, so a router problem is never a money problem.
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from typing import Callable, Optional

from core.logging import get_logger

from .port import NO_DECISION, RouteDecision, SwitchCostHint

logger = get_logger(__name__)


def semantic_router_base_url() -> str:
    """The router management-API base URL (e.g. http://vllm-sr:8080). Empty ⇒ no
    peer to consult ⇒ decide() is a no-op. Trailing slash trimmed."""
    return (os.getenv("SEMANTIC_ROUTER_BASE_URL", "") or "").rstrip("/")


def eval_deadline_s() -> float:
    """Hot-path consult deadline in seconds. BERT-on-CPU classification is tens of
    ms; we cap at 300ms by default and fail open past it so a slow router can never
    add material latency. Operator-tunable but clamped to a sane band. Public so
    the async handler's fail_after can mirror it (Fable round3 d)."""
    try:
        v = float(os.getenv("SEMANTIC_ROUTER_EVAL_TIMEOUT_S", "0.3"))
    except ValueError:
        v = 0.3
    return max(0.05, min(5.0, v))


# Max bytes we will read from an eval response body — a hostile/misconfigured SR
# returning a huge body cannot exhaust memory/CPU (Fable 2c).
_MAX_BODY_BYTES = 256 * 1024

# Fault-injection seam for tests: a callable (messages, base_url, timeout_s) ->
# dict (the parsed JSON body) or raises. When None, the real httpx transport is
# used. Mirrors serving/semantic_router.py's _transport_hook so the money-path
# and decide-path share one testing discipline. Test-only: assign only from tests
# (never touched on the production hot path), so no lock is needed here.
_TransportHook = Callable[[list, str, float], dict]
_transport_hook: Optional[_TransportHook] = None
_client_lock = threading.Lock()
_client = None      # lazily-built httpx.Client
_client_key = None  # (base_url, deadline) the current _client was built for


def set_transport_hook(fn: Optional[_TransportHook]) -> None:
    global _transport_hook
    _transport_hook = fn


def reset_for_test() -> None:
    global _client, _client_key
    set_transport_hook(None)
    with _client_lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:  # noqa: BLE001
                pass
        _client = None
        _client_key = None


def _drop_client() -> None:
    """Discard the current client so the next fetch rebuilds it (after a forced
    close on deadline overrun). The socket is already being closed by the caller;
    we only clear the handle."""
    global _client, _client_key
    with _client_lock:
        _client = None
        _client_key = None


def _get_client(base_url: str, deadline: float):
    """Return a client bound to (base_url, deadline), rebuilding if either changed
    (Fable 2d: the old code pinned the first base_url/timeout for the process
    lifetime and ignored later env changes). The per-phase timeouts are kept SHORT
    so that connect/write/read individually cannot each burn the full deadline; the
    TOTAL wall bound is enforced separately in `_fetch_eval` (Fable 2a)."""
    global _client, _client_key
    key = (base_url, round(deadline, 3))
    with _client_lock:
        if _client is None or _client_key != key:
            if _client is not None:
                try:
                    _client.close()
                except Exception:  # noqa: BLE001
                    pass
            import httpx

            # split the deadline across phases so no single phase can exceed it;
            # the wall-clock guard in _fetch_eval is the real ceiling.
            phase = max(0.02, deadline / 3.0)
            _client = httpx.Client(
                base_url=base_url,
                timeout=httpx.Timeout(connect=phase, read=phase, write=phase, pool=phase),
            )
            _client_key = key
        return _client


# Cap on any single decision/model string we will hand to the map + registry, so
# a hostile/oversized SR field cannot bloat a lookup or a log line.
_MAX_NAME_LEN = 256


def _clean_name(v) -> Optional[str]:
    """A trimmed, length-capped string, or None if not a usable non-empty str."""
    if not isinstance(v, str):
        return None
    v = v.strip()
    if not v:
        return None
    return v[:_MAX_NAME_LEN]


@dataclass(frozen=True)
class EvalOutcome:
    """A parsed eval response reduced to what decide() needs. `candidates` is the
    ORDERED list of names the router offered (concrete model recommendations AND
    decision rule names), each of which the caller tries through the decision map
    in turn — so whether the operator maps a rule name OR a model name, the map is
    consulted. `raw` keeps the whole body for the observability evidence block.
    Empty `candidates` means 'no usable decision' (⇒ NO_DECISION, fail-open)."""

    candidates: tuple[str, ...] = ()
    raw: Optional[dict] = None

    @property
    def decision_name(self) -> Optional[str]:
        """The first candidate (back-compat convenience for callers/tests that
        only want the top choice)."""
        return self.candidates[0] if self.candidates else None


def parse_eval_response(body: dict) -> EvalOutcome:
    """PURE: extract the ORDERED candidate names from an eval response, tolerating
    the shapes the upstream router / CLI emit (verified against a LIVE v0.3
    response: it carries `recommended_models`, `decision_result.decision_name`, and
    `routing_decision` TOGETHER).

    The candidates are collected in preference order and DEDUPED:
      1. `recommended_models[0]` — the router's concrete model recommendation;
      2. `decision_result.decision_name` — the decision RULE name;
      3. a top-level `routing_decision` string, or a dict's model/decision_name/name;
      4. legacy `chat.completion` pass-through — top-level `model`.

    Crucially the caller tries EACH candidate through the decision→registry map, so
    an operator map keyed on the RULE name (e.g. {"default-route": "..."}) is still
    honoured even though `recommended_models` is present — the earlier "return only
    the first" behaviour silently killed such maps. An empty list ⇒ NO_DECISION."""
    if not isinstance(body, dict):
        return EvalOutcome((), None)

    out: list[str] = []

    def _add(v):
        n = _clean_name(v)
        if n and n not in out:
            out.append(n)

    rec = body.get("recommended_models")
    if isinstance(rec, list):
        for r in rec[:3]:   # SR ranks; try the top few so a mapped lower rank wins
            _add(r)

    dr = body.get("decision_result")
    if isinstance(dr, dict):
        _add(dr.get("decision_name"))

    routing = body.get("routing_decision")
    if isinstance(routing, str):
        _add(routing)
    elif isinstance(routing, dict):
        for k in ("model", "decision_name", "name"):
            _add(routing.get(k))

    _add(body.get("model"))

    return EvalOutcome(tuple(out), body)


# Dedicated executor for the eval fetch so a hard TOTAL deadline can be enforced
# via future.result(timeout=...) — the only way to bound header slow-drip, which
# httpx's per-phase read timeout resets on every byte (Fable 2a/2c). Small pool:
# consults are short; a backlog past the pool just means the caller times out and
# fails open. Threads are daemon so a hung socket read never blocks shutdown.
_FETCH_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sr-eval")


def _do_fetch(client, messages: list, deadline_s: float) -> dict:
    """The blocking POST + incremental read, run inside _FETCH_POOL. Bounds the
    BODY by wall time + size; the caller bounds the TOTAL (incl. headers) via the
    future timeout and force-closes this stream on overrun."""
    import json as _json
    import time as _time

    start = _time.monotonic()
    with client.stream(
        "POST", "/api/v1/eval",
        json={"messages": messages, "evaluate_all_signals": True},
    ) as resp:
        # headers are in now; check elapsed before reading the body.
        if _time.monotonic() - start > deadline_s:
            raise TimeoutError("eval deadline exceeded before body")
        if resp.status_code != 200:
            raise RuntimeError(f"eval non-200: {resp.status_code}")
        buf = bytearray()
        for chunk in resp.iter_bytes():
            buf += chunk
            if len(buf) > _MAX_BODY_BYTES:
                raise RuntimeError("eval body exceeds size cap")
            if _time.monotonic() - start > deadline_s:
                raise TimeoutError("eval total deadline exceeded")
    return _json.loads(bytes(buf))


def _fetch_eval(messages: list, base_url: str, deadline_s: float) -> dict:
    """Do the actual POST /api/v1/eval (or the injected hook). Returns the parsed
    JSON body; raises on any transport/HTTP/JSON failure OR overrun (caller fails
    open).

    HARD TOTAL deadline (Fable 2a): the fetch runs on a worker thread and we
    `future.result(timeout=deadline+margin)`. This bounds EVERYTHING — connect,
    header slow-drip (which per-phase read timeouts cannot catch), and body — not
    just the chunk loop. On overrun we close the client so the orphaned socket read
    unwinds, and raise TimeoutError."""
    if _transport_hook is not None:
        return _transport_hook(messages, base_url, deadline_s)

    client = _get_client(base_url, deadline_s)
    fut = _FETCH_POOL.submit(_do_fetch, client, messages, deadline_s)
    try:
        # small margin over the internal wall check so the thread normally wins the
        # race and raises its own precise error; this is the backstop for headers.
        return fut.result(timeout=deadline_s + 0.1)
    except FuturesTimeout:
        # header slow-drip / stuck connect: force the socket to unwind and rebuild
        # the client next call, then fail open.
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
        _drop_client()
        raise TimeoutError("eval hard deadline exceeded")


def consult_eval(
    *,
    messages: list,
    normalize,                       # (decision_name:str) -> registry model_id | None
    hard: bool = False,
) -> RouteDecision:
    """Consult the SR decide surface and return a source-agnostic RouteDecision.

    `normalize` maps the router's decision namespace to a Stratoclave registry
    model_id. Each parsed candidate (concrete model recommendation AND rule name)
    is tried in order; the FIRST that maps wins (Fable 6 — so an operator map keyed
    on the rule name is honoured even when `recommended_models` is present). An
    all-unmapped response ⇒ NO_DECISION + alert, never a raw pass-through of an
    unknown model into the reserve.

    `hard=False` (default) yields a SOFT `prefer_model` — it only reorders the
    servable candidate chain and can never turn a servable request into a 402/403;
    `hard=True` (an explicit tenant policy) yields a `hard_model` pin that disables
    the cascade, exactly like a client pin.

    Fail-open on EVERYTHING: no base url, transport error, timeout, bad body, or
    all-unmapped candidates → NO_DECISION. Never raises on the hot path."""
    base = semantic_router_base_url()
    if not base:
        return NO_DECISION
    if not messages:
        return NO_DECISION
    try:
        body = _fetch_eval(messages, base, eval_deadline_s())
    except Exception as e:  # noqa: BLE001 — advisory + fail-open; never break a request.
        logger.info("sr_eval_failed", error=str(e))
        return NO_DECISION

    outcome = parse_eval_response(body if isinstance(body, dict) else {})
    if not outcome.candidates:
        return NO_DECISION
    model_id = None
    for cand in outcome.candidates:
        model_id = normalize(cand)
        if model_id:
            break
    if not model_id:
        # No candidate maps to a registry model — a config gap, not a routing
        # opinion we can act on. Alert so the decision→registry map can be extended;
        # do NOT pass an unknown model into the reserve.
        logger.warning("sr_eval_unmapped_decision", candidates=list(outcome.candidates))
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
