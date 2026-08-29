"""Routing configuration loader with TTL cache.

Reads tenant and user routing config from DynamoDB with 60s in-memory
TTL cache. Config propagation latency is acceptable for routing policy
changes (not latency-critical).

A config that could not be READ is not a config. Everything this table carries
only ever RESTRICTS a request — the allowlist of models a tenant may reach, the
chain it may fall back along, the per-model spend caps — so answering a failed
read with an empty `RoutingConfig()` does not degrade routing, it removes the
restrictions, and caching that answer removed them for a minute per process. The
loader therefore distinguishes three outcomes that were previously two:

  - the item is ABSENT: the tenant genuinely has no routing config. Empty config,
    cached for the full TTL. Unchanged.
  - the read FAILED and a previously-read config is remembered: serve that,
    stale. Stale restrictions are strictly safer than none, and an operator's
    edit is at most `_STALE_RETRY_S` from being picked up once reads recover.
  - the read FAILED and nothing is remembered: raise `RoutingConfigUnavailable`.
    The reserve chokepoint maps it to a retryable 503; the advisory callers
    (`tenant_shadow_pref`, the SR adapter) keep their own fences and degrade to
    the global default, because those decide nothing about money or access.
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional

from dynamo.client import get_dynamodb_resource

from . import quota as _quota
from .model_resolver import ModelQuotaConfig, RoutingConfig, UserRoutingConfig

_TABLE = os.getenv("DYNAMODB_USER_TENANTS_TABLE", "stratoclave-user-tenants")
_CACHE_TTL_S = 60.0
# How long a stale-serve stands before the next request retries the read. Short
# on purpose: it bounds the retry rate against a table that may itself be the
# thing failing, without pretending for a full TTL that the fault did not happen.
_STALE_RETRY_S = 5.0
_MISS = object()
_cache: dict[str, tuple[Any, float]] = {}
# Last successfully-read value per cache key, kept beyond the TTL so a read fault
# has something true-as-of-recently to fall back to. Only ever written on a
# successful read, so it can be stale but never invented.
_last_known_good: dict[str, Any] = {}


class RoutingConfigUnavailable(Exception):
    """The tenant's routing config could not be read and nothing is remembered.

    Raised instead of returning an empty config, so no caller can mistake "we do
    not know this tenant's restrictions" for "this tenant has none."
    """


def _get_cached(key: str):
    entry = _cache.get(key)
    if entry and entry[1] > time.monotonic():
        return entry[0]
    return _MISS


def _set_cached(key: str, value: Any):
    _cache[key] = (value, time.monotonic() + _CACHE_TTL_S)
    _last_known_good[key] = value


def _serve_stale_or_raise(cache_key: str, tenant_id: str, error: Exception):
    """Fall back to the last successfully-read value for `cache_key`, or raise.

    The stale value is re-cached for `_STALE_RETRY_S` only — long enough to keep a
    sustained fault from issuing a read per request, short enough that recovery is
    seconds away rather than a TTL away. It is deliberately NOT written back
    through `_set_cached`, which would refresh the last-known-good timestamp and
    hide how old the answer is.
    """
    if cache_key in _last_known_good:
        value = _last_known_good[cache_key]
        _cache[cache_key] = (value, time.monotonic() + _STALE_RETRY_S)
        _log_read_fault("routing_config_read_failed_serving_stale", tenant_id, error)
        return value
    _log_read_fault("routing_config_read_failed_no_cached_value", tenant_id, error)
    raise RoutingConfigUnavailable(
        f"routing config for tenant '{tenant_id}' could not be read: {error}"
    ) from error


def _log_read_fault(event: str, tenant_id: str, error: Exception) -> None:
    try:
        from core.logging import get_logger
        get_logger(__name__).warning(event, tenant_id=tenant_id, error=str(error))
    except Exception:  # noqa: BLE001 — logging must not mask the fault itself.
        pass


def get_tenant_routing_config(tenant_id: str) -> RoutingConfig:
    """Load tenant routing config, with 60s TTL cache.

    Raises `RoutingConfigUnavailable` when the read fails and no previously-read
    config is remembered for this tenant — see the module docstring for why a
    failed read must not resolve to an unrestricted config.
    """
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
    except Exception as e:  # noqa: BLE001 — classified by _serve_stale_or_raise.
        return _serve_stale_or_raise(cache_key, tenant_id, e)

    item = resp.get("Item")
    if not item:
        config = RoutingConfig()
        _set_cached(cache_key, config)
        return config

    config = _parse_tenant_config(item)
    _set_cached(cache_key, config)
    return config


def get_user_routing_config(tenant_id: str, user_id: str) -> Optional[UserRoutingConfig]:
    """Load user routing overrides, with 60s TTL cache.

    Same failure discipline as the tenant config: a user chain narrows the
    candidate set, so losing it on a read fault widens what the request may reach.
    Absent item → `None` (inherit the tenant config), which is what a user with no
    overrides means.
    """
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
    except Exception as e:  # noqa: BLE001 — classified by _serve_stale_or_raise.
        return _serve_stale_or_raise(cache_key, tenant_id, e)

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
                # An absent unit is not a claim that the cap is in tokens. The
                # admin write path always stores `usd_micro` and the enforcement
                # path reserves micro-USD, so defaulting the PARSE to "tokens"
                # made a row that stated nothing disagree with both of them —
                # and the enforcement, which never read the field, then applied a
                # dollar cap to a number the operator's console showed as tokens.
                # Absence resolves to the one denomination this system keeps;
                # anything explicitly different is refused at admission.
                unit=cfg.get("unit", _quota.RESERVED_UNIT),
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
        # four-state: present -> str; absent -> None (follow global default).
        # Unknown/garbage values resolve to None so a bad write degrades to the
        # global default rather than an undefined mode.
        sr_mode=(str(item["sr_mode"])
                 if item.get("sr_mode") in ("off", "canary", "active")
                 else None),
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

    The last-known-good value is deliberately KEPT: it exists so that a read fault
    has something real to fall back on, and the invalidation says "re-read", not
    "forget". If the very next read faults, serving the pre-write config is the
    conservative answer available — the alternative is refusing the request.
    """
    if user_id is None:
        _cache.pop(f"tenant:{tenant_id}", None)
    else:
        _cache.pop(f"user:{tenant_id}:{user_id}", None)


def reset_cache() -> None:
    """Drop the TTL cache AND the remembered last-known-good values.

    For tests that need a process with no memory of previous reads (the
    last-known-good map outlives the TTL by design, so clearing `_cache` alone
    leaves a fallback in place)."""
    _cache.clear()
    _last_known_good.clear()
