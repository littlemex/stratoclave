"""Core types for the routing/resilience layer."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Optional


class BreakerStage(Enum):
    NORMAL = "normal"
    DOWNGRADE = "downgrade"
    REJECT = "reject"


@dataclass(frozen=True)
class BreakerDecision:
    stage: BreakerStage
    remaining_ratio: float
    max_cost_tier: Optional[int] = None
    reason: str = ""


@dataclass(frozen=True)
class Target:
    model_id: str
    region: str
    cost_tier: int = 1
    price_key: str = ""
    # Hybrid serving (P0). "bedrock" (default) == today's behaviour. "vllm"
    # routes the invoke through mvp.serving.vllm to a self-hosted, internal
    # OpenAI-compatible endpoint identified by `endpoint_key` (an opaque token
    # resolved against an operator allowlist — never a URL). A vLLM target
    # carries region="self-hosted" and is a single target with no cross-region
    # failover fan-out.
    served_by: str = "bedrock"
    endpoint_key: Optional[str] = None


@dataclass(frozen=True)
class Chain:
    targets: tuple[Target, ...]
    reserve_estimate_microusd: int = 0
    resolution_facts: dict = field(default_factory=dict)


class Disposition(Enum):
    FAILOVER = "failover"
    RETRY_SAME = "retry_same"
    FATAL = "fatal"


@dataclass(frozen=True)
class AttemptRecord:
    target: Target
    outcome: str
    error_class: str = ""
    latency_ms: int = 0


@dataclass(frozen=True)
class RouteRequest:
    alias: str
    payload: dict
    tenant_id: str
    request_id: str
    stream: bool = True
    span_id: Optional[str] = None
    group_id: Optional[str] = None
    workflow_run_id: Optional[str] = None
    exclude: tuple[Target, ...] = ()
    pin: Optional[Target] = None
    fault_spec: Optional[str] = None  # test-only fault injection (gated on SC_FAULT_INJECTION)


@dataclass
class RoutedStream:
    target: Target
    events: AsyncIterator[dict[str, Any]]
    attempt_facts: list[AttemptRecord] = field(default_factory=list)
    # P0-14: breaker state of the COMMITTED target at commit time
    # ('closed' | 'half_open' | ...). Observational only — routing behaviour
    # must not read this back.
    breaker_stage: str = "closed"
