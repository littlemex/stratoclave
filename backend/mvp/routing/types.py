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
