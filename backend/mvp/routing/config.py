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
_MISS = object()
_cache: dict[str, tuple[Any, float]] = {}


def _get_cached(key: str):
    entry = _cache.get(key)
    if entry and entry[1] > time.monotonic():
        return entry[0]
    return _MISS


def _set_cached(key: str, value: Any):
    _cache[key] = (value, time.monotonic() + _CACHE_TTL_S)


def get_tenant_routing_config(tenant_id: str) -> RoutingConfig:
    """Load tenant routing config, with 60s TTL cache."""
    cache_key = f"tenant:{tenant_id}"
    cached = _get_cached(cache_key)
    if cached is not _MISS:
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
    if cached is not _MISS:
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
        saar_user_scoped=bool(item.get("saar_user_scoped", False)),
        # tri-state: present -> bool; absent -> None (follow global default).
        shadow_vsr=(bool(item["shadow_vsr"]) if "shadow_vsr" in item else None),
    )


def _parse_user_config(item: dict) -> UserRoutingConfig:
    chain = item.get("chain")
    return UserRoutingConfig(
        preferred_model=item.get("preferred_model"),
        chain=tuple(chain) if chain else None,
        fallback=item.get("fallback"),
    )

# Rate-limit the fail-open warn so a persistent config-read fault logs once a
# minute per process instead of once a request (Fable per-tenant review Medium:
# a silent `except: return None` hides a tenant that thinks it is ON but records
# nothing). Module-level, no lock — a duplicate log under a race is harmless.
_SHADOW_PREF_WARN_INTERVAL_S = 60.0
_shadow_pref_last_warn = 0.0


def tenant_shadow_pref(tenant_id: str) -> Optional[bool]:
    """The tenant's per-tenant shadow_vsr preference (True/False/None) from the
    60s-TTL-cached routing config (get_tenant_routing_config — NO extra DynamoDB
    read on a warm cache). None => follow the global default.

    Single home for the three route handlers (Fable per-tenant review Medium:
    was copy-pasted three times, each swallowing errors silently). Fenced +
    fail-open: any lookup failure yields None so the advisory shadow path can
    never break a request, but a rate-limited warn is emitted so a persistent
    fault is visible rather than silently degrading every tenant to the global
    default."""
    try:
        return get_tenant_routing_config(tenant_id).shadow_vsr
    except Exception as e:  # noqa: BLE001 — advisory only; never break the request.
        global _shadow_pref_last_warn
        now = time.monotonic()
        if now - _shadow_pref_last_warn >= _SHADOW_PREF_WARN_INTERVAL_S:
            _shadow_pref_last_warn = now
            try:
                from core.logging import get_logger
                get_logger(__name__).warning(
                    "shadow_pref_lookup_failed", tenant_id=tenant_id, error=str(e))
            except Exception:
                pass
        return None


def invalidate_routing_cache(tenant_id: str, user_id: Optional[str] = None) -> None:
    """Drop the cached routing config for a tenant (or one of its users).

    Called by the admin write path so THIS process immediately reads its own
    writes. Scope caveat: the cache is per-process; other ECS tasks keep
    their entry until the 60s TTL expires, so the fleet converges within one
    TTL of a write. (See admin_api.py callouts before tightening this.)
    """
    if user_id is None:
        _cache.pop(f"tenant:{tenant_id}", None)
    else:
        _cache.pop(f"user:{tenant_id}:{user_id}", None)
