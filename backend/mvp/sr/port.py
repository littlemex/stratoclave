"""The routing-decision seam (port) — source-agnostic types + Protocol.

Both the legacy self-hosted SAAR router and the future vLLM Semantic Router
adapter satisfy this same port, so the request path consumes a single neutral
shape and never depends on WHO decided. Stage 1 defines these types only; there
are no callers yet (the handlers keep calling the legacy modules directly until
the adapter lands in a later stage).

Design rules baked into the shape:

  * A decision is ADVISORY unless `hard` is True. A hard pin disables the
    fallback cascade for that request (the tool-loop / provider-state cases);
    an advisory (`prefer_model`) only reorders candidates and can never remove a
    servable model. This mirrors the existing SaarDecision hard/soft partition.
  * `SwitchCostHint` carries only `(warm_model, warm_prefix_tokens)` — the two
    numbers the LEDGER needs to price a stay-vs-switch reserve. It is deliberately
    source-agnostic: `pricing.switch_cost_delta_microusd` consumes the tokens and
    does not care whether a SAAR memory read or an SR response produced them.
  * Nothing here touches money. The port yields a *suggestion*; the reserve/settle
    machinery remains the sole gate on spend (money fail-closed), while a null or
    failed decision degrades to the normal resolver (routing fail-open).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class SwitchCostHint:
    """The ledger-facing hint for pricing a model switch. `warm_model` is the
    model whose prefix cache is warm for this session; `warm_prefix_tokens` is how
    many input tokens are expected to hit that warm cache (0 ⇒ no discount ⇒ the
    reserve is byte-identical to the pre-routing estimate). Fed to
    `pricing.switch_cost_delta_microusd` and the reserve estimate's warm split."""

    warm_model: Optional[str] = None
    warm_prefix_tokens: int = 0


@dataclass(frozen=True)
class RouteDecision:
    """A source-agnostic routing decision to feed the resolver + reserve.

    `hard_model` (when set) is a session-derived pin that DISABLES the cascade —
    used only for correctness locks (tool-loop, provider-state). `prefer_model`
    is a soft preference that only reorders the candidate chain (pure permutation:
    never removes a servable model). At most one of the two is ever set — the same
    hard/soft partition the legacy SaarDecision guarantees.

    `switch_cost` is the ledger hint (above). `reason`/`switched` ride the replay
    trace and observability only; they never affect money. `origin` records which
    router produced this ("saar" legacy | "semantic-router" | "none") so the
    decision log and Savings Certificate can attribute the choice."""

    hard_model: Optional[str] = None
    prefer_model: Optional[str] = None
    switch_cost: SwitchCostHint = SwitchCostHint()
    reason: str = "none"
    switched: bool = False
    origin: str = "none"

    @property
    def acts(self) -> bool:
        """True iff this decision changes routing at all (a hard pin or a soft
        preference). A non-acting decision is a no-op the resolver ignores."""
        return bool(self.hard_model or self.prefer_model)

    def __post_init__(self) -> None:
        # Enforce the hard/soft partition at construction (defence in depth): a
        # decision must never be BOTH a hard pin and a soft preference.
        if self.hard_model and self.prefer_model:
            raise ValueError("RouteDecision cannot set both hard_model and prefer_model")


# A non-acting decision any adapter can return to mean "no routing opinion" —
# the request then flows through the normal resolver unchanged (fail-open).
NO_DECISION = RouteDecision()


@runtime_checkable
class RoutePort(Protocol):
    """The seam every router adapter implements. `decide` is a pure-ish function
    of the request context returning a `RouteDecision`; it MUST be fail-open — any
    internal error should surface as `NO_DECISION`, never an exception on the hot
    path, because a router outage must degrade to the normal resolver, not fail
    the request (money is gated elsewhere and stays fail-closed)."""

    def decide(
        self,
        *,
        tenant_id: str,
        session_key: Optional[str],
        requested_model: str,
        has_tool_result: bool,
    ) -> RouteDecision:
        ...
