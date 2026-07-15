"""Chain resolution: model alias → ordered list of Targets to attempt.

Resolution pipeline:
1. Expand alias → concrete targets from catalog
2. Apply exclusions (VSR constraint)
3. Apply breaker tier cap
4. Order by preference + region diversity
"""
from __future__ import annotations

import os
from typing import Optional

from .clients import default_region
from .types import BreakerDecision, BreakerStage, Chain, Target


_CATALOG: dict[str, list[Target]] = {}

# Default cross-region failover targets when STRATOCLAVE_FAILOVER_REGIONS is
# unset. Preserves the historical behavior.
_DEFAULT_FAILOVER_REGIONS = ("us-west-2", "eu-west-1")

# Explicit "single-region, no failover" sentinels for the config var.
_DISABLE_SENTINELS = frozenset({"none", "disabled", "off"})


def failover_regions() -> list[str]:
    """Cross-region failover targets, in preference order, EXCLUDING the primary
    (`default_region`) which is always the first target.

    Configured via `STRATOCLAVE_FAILOVER_REGIONS` (comma-separated). Data-
    residency control (README): set it to a same-jurisdiction region list, or
    to an EMPTY string to DISABLE failover entirely (single-region — a streaming
    request then never sends prompt bytes to another region). Whitespace and the
    primary region are stripped; order and de-dup are preserved.
    """
    raw = os.getenv("STRATOCLAVE_FAILOVER_REGIONS")
    if raw is None:
        candidates = list(_DEFAULT_FAILOVER_REGIONS)
    elif raw.strip().lower() in _DISABLE_SENTINELS:
        # Explicit disable sentinel (survives orchestration that strips empty env
        # vars — writing "none"/"disabled"/"off" is unambiguous single-region
        # intent, unlike an empty string a template might drop). Fable review #1.
        candidates = []
    else:
        # Explicit empty string => no failover regions (single-region) too.
        candidates = [r.strip() for r in raw.split(",") if r.strip()]
    primary = default_region()
    seen: set[str] = {primary}
    out: list[str] = []
    for r in candidates:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _build_catalog() -> dict[str, list[Target]]:
    """Build the static target catalog from the model registry."""
    from mvp.models import _REGISTRY

    catalog: dict[str, list[Target]] = {}
    region = default_region()
    alt_regions = failover_regions()

    # Make the effective residency posture visible in logs at build time — an
    # operator can confirm "disabled" actually took (Fable review #1).
    from core.logging import get_logger

    get_logger(__name__).info(
        "failover_regions_effective",
        primary_region=region,
        failover_regions=alt_regions,
        failover_enabled=bool(alt_regions),
    )

    for entry in _REGISTRY:
        if entry.provider != "anthropic":
            continue
        for alias in entry.aliases:
            targets = [
                Target(
                    model_id=entry.bedrock_model_id,
                    region=region,
                    cost_tier=_tier_for(entry.pricing_key),
                    price_key=entry.pricing_key,
                ),
            ]
            for alt in alt_regions:
                if alt != region:
                    targets.append(Target(
                        model_id=entry.bedrock_model_id,
                        region=alt,
                        cost_tier=_tier_for(entry.pricing_key),
                        price_key=entry.pricing_key,
                    ))
            catalog[alias] = targets
            catalog[entry.bedrock_model_id] = targets
    return catalog


def _tier_for(pricing_key: str) -> int:
    if "haiku" in pricing_key:
        return 1
    if "sonnet" in pricing_key:
        return 2
    if "opus" in pricing_key:
        return 3
    return 2


def get_catalog() -> dict[str, list[Target]]:
    global _CATALOG
    if not _CATALOG:
        _CATALOG = _build_catalog()
    return _CATALOG


def reset_catalog() -> None:
    """Drop the memoized catalog so the next get_catalog() rebuilds it. For
    tests that vary STRATOCLAVE_FAILOVER_REGIONS / BEDROCK_REGION."""
    global _CATALOG
    _CATALOG = {}


def resolve_chain(
    alias: str,
    *,
    breaker: Optional[BreakerDecision] = None,
    exclude: tuple[Target, ...] = (),
    pin: Optional[Target] = None,
) -> Chain:
    """Resolve a model alias to an ordered Chain of targets."""
    if pin:
        return Chain(targets=(pin,))

    catalog = get_catalog()
    targets = catalog.get(alias)
    if not targets:
        from mvp.models import resolve_bedrock_model
        model_id = resolve_bedrock_model(alias)
        region = default_region()
        # Primary + the SAME configured failover regions as the catalog, so the
        # residency setting applies to the unregistered-alias fallback too (an
        # empty STRATOCLAVE_FAILOVER_REGIONS keeps this single-region).
        targets = [Target(model_id=model_id, region=region, cost_tier=2, price_key="sonnet")]
        for alt in failover_regions():
            targets.append(
                Target(model_id=model_id, region=alt, cost_tier=2, price_key="sonnet")
            )

    filtered = [t for t in targets if t not in exclude]

    if breaker and breaker.stage == BreakerStage.DOWNGRADE and breaker.max_cost_tier is not None:
        downgraded = [t for t in filtered if t.cost_tier <= breaker.max_cost_tier]
        if downgraded:
            filtered = downgraded

    if not filtered:
        raise ValueError(f"No targets available for alias '{alias}' after applying constraints")

    ordered = _region_diversify(filtered)
    return Chain(targets=tuple(ordered))


def _region_diversify(targets: list[Target]) -> list[Target]:
    """Reorder targets to alternate regions when possible."""
    if len(targets) <= 1:
        return targets
    result = [targets[0]]
    remaining = targets[1:]
    for t in remaining:
        if t.region != result[-1].region:
            result.append(t)
    for t in remaining:
        if t not in result:
            result.append(t)
    return result
