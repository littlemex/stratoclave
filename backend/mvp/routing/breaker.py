"""Staged breaker — advisory budget check that shapes routing.

This is NOT the authoritative budget gate (that's _pipeline.py's
TransactWriteItems conditional reserve). This is a cheap pre-check
that determines whether to downgrade model tier or reject early.
"""
from __future__ import annotations

from .types import BreakerDecision, BreakerStage

_DOWNGRADE_THRESHOLD = 0.25
_REJECT_THRESHOLD = 0.05


def compute_breaker(
    remaining_microusd: int,
    limit_microusd: int,
    *,
    downgrade_threshold: float = _DOWNGRADE_THRESHOLD,
    reject_threshold: float = _REJECT_THRESHOLD,
) -> BreakerDecision:
    """Compute breaker stage from budget remaining ratio."""
    if limit_microusd <= 0:
        return BreakerDecision(
            stage=BreakerStage.NORMAL,
            remaining_ratio=1.0,
            reason="no limit configured",
        )

    ratio = remaining_microusd / limit_microusd

    if ratio <= reject_threshold:
        return BreakerDecision(
            stage=BreakerStage.REJECT,
            remaining_ratio=ratio,
            reason=f"remaining {ratio:.1%} <= reject threshold {reject_threshold:.0%}",
        )

    if ratio <= downgrade_threshold:
        return BreakerDecision(
            stage=BreakerStage.DOWNGRADE,
            remaining_ratio=ratio,
            max_cost_tier=1,
            reason=f"remaining {ratio:.1%} <= downgrade threshold {downgrade_threshold:.0%}",
        )

    return BreakerDecision(
        stage=BreakerStage.NORMAL,
        remaining_ratio=ratio,
        reason="budget healthy",
    )
