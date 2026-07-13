"""Routing signal emission — fire-and-forget DynamoDB write.

A signal write failure never fails the request. These accumulate for
future offline evaluation that feeds back into chain resolution policy.
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional

from core.logging import get_logger

logger = get_logger(__name__)

_TABLE_NAME = os.getenv("DYNAMODB_ROUTING_SIGNALS_TABLE", "stratoclave-routing-signals")


def emit_signal_sync(
    *,
    tenant_id: str,
    group_id: str,
    workflow_run_id: str,
    span_id: str,
    category: str,
    committed_model_id: str,
    committed_region: str,
    cost_tier: int,
    chain_position_served: int,
    status: str,
    usage_is_partial: bool,
    output_tokens: int,
    latency_first_event_ms: Optional[int],
    attempts_total: int,
    targets_distinct: int,
    breaker_stage: str,
) -> None:
    """Fire-and-forget signal write. Never raises."""
    try:
        from dynamo.client import get_dynamodb_resource

        table = get_dynamodb_resource().Table(_TABLE_NAME)
        now_ms = int(time.time() * 1000)

        table.put_item(Item={
            "pk": f"TENANT#{tenant_id}#CAT#{category}",
            "sk": f"TS#{now_ms}#{span_id}",
            "group_id": group_id,
            "workflow_run_id": workflow_run_id,
            "span_id": span_id,
            "category": category,
            "committed_model_id": committed_model_id,
            "committed_region": committed_region,
            "cost_tier": cost_tier,
            "chain_position_served": chain_position_served,
            "status": status,
            "usage_is_partial": usage_is_partial,
            "output_tokens": output_tokens,
            "latency_first_event_ms": latency_first_event_ms or 0,
            "attempts_total": attempts_total,
            "targets_distinct": targets_distinct,
            "breaker_stage": breaker_stage,
            "created_at_ms": now_ms,
        })
    except Exception as e:
        logger.warning("routing_signal_write_failed", error=str(e))


def emit_signal(**kwargs) -> None:
    """Fire-and-forget signal write, off the event loop. Never raises."""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, lambda: emit_signal_sync(**kwargs))
    except RuntimeError:
        emit_signal_sync(**kwargs)
