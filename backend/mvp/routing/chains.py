"""Chain resolution: model alias → ordered list of Targets to attempt.

Resolution pipeline:
1. Expand alias → concrete targets from catalog
2. Apply exclusions (VSR constraint)
3. Apply breaker tier cap
4. Order by preference + region diversity
"""
from __future__ import annotations

from typing import Optional

from .clients import default_region
from .types import BreakerDecision, BreakerStage, Chain, Target


_CATALOG: dict[str, list[Target]] = {}


def _build_catalog() -> dict[str, list[Target]]:
    """Build the static target catalog from the model registry."""
    from mvp.models import _REGISTRY

    catalog: dict[str, list[Target]] = {}
    region = default_region()
    alt_regions = ["us-west-2", "eu-west-1"]

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
        targets = [
            Target(model_id=model_id, region=region, cost_tier=2, price_key="sonnet"),
            Target(model_id=model_id, region="us-west-2", cost_tier=2, price_key="sonnet"),
        ]

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
