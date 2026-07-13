"""Routing and resilience layer.

Sits between budget enforcement (L3) and Bedrock invocation. Provides:
- Model chain resolution (recipe ∩ breaker ∩ health)
- Retry + cross-region fallback with first-event commit
- Per-region Bedrock client pool
- Staged breaker (advisory budget check)
"""
from .types import Target, Chain, RouteRequest, RoutedStream, BreakerStage, BreakerDecision
from .infrarouter import route_stream

__all__ = [
    "Target", "Chain", "RouteRequest", "RoutedStream",
    "BreakerStage", "BreakerDecision",
    "route_stream",
]
