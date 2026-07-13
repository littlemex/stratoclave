"""Routing configuration loader with TTL cache.

Reads tenant and user routing config from DynamoDB with 60s in-memory
TTL cache. Config propagation latency is acceptable for routing policy
changes (not latency-critical).
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional

from dynamo.client import get_dynamodb_resource

from .model_resolver import ModelQuotaConfig, RoutingConfig, UserRoutingConfig

_TABLE = os.getenv("DYNAMODB_USER_TENANTS_TABLE", "stratoclave-user-tenants")
_CACHE_TTL_S = 60.0
_cache: dict[str, tuple[Any, float]] = {}


def _get_cached(key: str):
    entry = _cache.get(key)
    if entry and entry[1] > time.monotonic():
        return entry[0]
    return None


def _set_cached(key: str, value: Any):
    _cache[key] = (value, time.monotonic() + _CACHE_TTL_S)


def get_tenant_routing_config(tenant_id: str) -> RoutingConfig:
    """Load tenant routing config, with 60s TTL cache."""
    cache_key = f"tenant:{tenant_id}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    table = get_dynamodb_resource().Table(_TABLE)
    try:
        resp = table.get_item(
            Key={"user_id": "CONFIG#ROUTING", "tenant_id": tenant_id},
            ConsistentRead=False,
        )
    except Exception:
        config = RoutingConfig()
        _set_cached(cache_key, config)
        return config

    item = resp.get("Item")
    if not item:
        config = RoutingConfig()
        _set_cached(cache_key, config)
        return config

    config = _parse_tenant_config(item)
    _set_cached(cache_key, config)
    return config


def get_user_routing_config(tenant_id: str, user_id: str) -> Optional[UserRoutingConfig]:
    """Load user routing overrides, with 60s TTL cache."""
    cache_key = f"user:{tenant_id}:{user_id}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    table = get_dynamodb_resource().Table(_TABLE)
    try:
        resp = table.get_item(
            Key={"user_id": f"CONFIG#ROUTING#USER#{user_id}", "tenant_id": tenant_id},
            ConsistentRead=False,
        )
    except Exception:
        _set_cached(cache_key, None)
        return None

    item = resp.get("Item")
    if not item:
        _set_cached(cache_key, None)
        return None

    config = _parse_user_config(item)
    _set_cached(cache_key, config)
    return config


def _parse_tenant_config(item: dict) -> RoutingConfig:
    allowlist = tuple(item.get("allowlist", []))
    chain = tuple(item.get("chain", []))
    quotas = {}
    for model, cfg in item.get("quotas", {}).items():
        if isinstance(cfg, dict):
            quotas[model] = ModelQuotaConfig(
                model=model,
                unit=cfg.get("unit", "tokens"),
                limit=cfg.get("limit"),
                period=cfg.get("period", "monthly"),
            )
    return RoutingConfig(
        allowlist=allowlist,
        chain=chain,
        quotas=quotas,
        fallback_mode=item.get("fallback_mode", "loud"),
        fallback_default=item.get("fallback_default", "off"),
        free_tier_model=item.get("free_tier", {}).get("model") if isinstance(item.get("free_tier"), dict) else None,
    )


def _parse_user_config(item: dict) -> UserRoutingConfig:
    chain = item.get("chain")
    return UserRoutingConfig(
        preferred_model=item.get("preferred_model"),
        chain=tuple(chain) if chain else None,
        fallback=item.get("fallback"),
    )
