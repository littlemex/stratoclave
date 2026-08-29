"""Chain resolution: model alias → ordered list of Targets to attempt.

Resolution pipeline:
1. Expand alias → concrete targets from catalog
2. Apply exclusions (VSR constraint)
3. Apply breaker tier cap
4. Order by preference + region diversity
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

from .clients import default_region
from .types import BreakerDecision, BreakerStage, Chain, Target


_CATALOG: dict[str, list[Target]] = {}

# Default cross-region failover targets when STRATOCLAVE_FAILOVER_REGIONS is
# unset. Historically this was the fixed pair below; it is now filtered to the
# PRIMARY's geographic jurisdiction so a non-US primary never silently fails
# over into another jurisdiction (see `failover_regions`).
_DEFAULT_FAILOVER_REGIONS = ("us-west-2", "eu-west-1")

# Explicit "single-region, no failover" sentinels for the config var.
_DISABLE_SENTINELS = frozenset({"none", "disabled", "off"})


def _jurisdiction(region: str) -> str:
    """Geographic prefix of a region id ("us", "eu", "ap", ...). This is a
    coarse residency proxy — it does NOT distinguish UK (eu-west-2) from EU, so
    it is used ONLY to filter the built-in defaults, never to certify residency
    (that is the CDK-side STRATOCLAVE_RESIDENCY check's job)."""
    return region.split("-")[0]


def failover_regions() -> list[str]:
    """Cross-region failover targets, in preference order, EXCLUDING the primary
    (`default_region`) which is always the first target.

    Configured via `STRATOCLAVE_FAILOVER_REGIONS` (comma-separated). Data-
    residency control (README): set it to a same-jurisdiction region list, or
    to an EMPTY string / a `none`/`disabled`/`off` sentinel to DISABLE failover
    entirely (single-region — a streaming request then never sends prompt bytes
    to another region). Whitespace and the primary region are stripped; order
    and de-dup are preserved.

    Residency safety for the DEFAULT set: when the var is UNSET, the built-in
    defaults are filtered to the primary's jurisdiction, so e.g. a
    `BEDROCK_REGION=eu-west-1` deploy does NOT inherit a us-west-2 failover and
    silently leak EU prompts to the US. An EXPLICIT list is honoured verbatim
    (the operator's stated intent; the CDK STRATOCLAVE_RESIDENCY check flags a
    cross-jurisdiction explicit list separately).
    """
    primary = default_region()
    raw = os.getenv("STRATOCLAVE_FAILOVER_REGIONS")
    if raw is None:
        # Unset: use the built-ins, but keep only same-jurisdiction regions so a
        # non-US primary can never back-door into another jurisdiction.
        primary_juris = _jurisdiction(primary)
        candidates = [
            r for r in _DEFAULT_FAILOVER_REGIONS if _jurisdiction(r) == primary_juris
        ]
    elif raw.strip().lower() in _DISABLE_SENTINELS:
        # Explicit disable sentinel (survives orchestration that strips empty env
        # vars — writing "none"/"disabled"/"off" is unambiguous single-region
        # intent, unlike an empty string a template might drop). Fable review #1.
        candidates = []
    else:
        # Explicit empty string => no failover regions (single-region) too.
        # An explicit non-empty list is honoured verbatim (no jurisdiction
        # filter): the operator asked for exactly these regions.
        candidates = [r.strip() for r in raw.split(",") if r.strip()]
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
        served_by = getattr(entry, "served_by", "bedrock")
        # Bedrock catalog covers the Anthropic (Messages) family as before.
        # vLLM entries are ALSO catalogued (any provider) so hybrid serving can
        # route to them; they get exactly ONE self-hosted target (no
        # cross-region fan-out — a self-hosted endpoint has no regions), so on
        # endpoint failure the chain exhausts and the request fails cleanly
        # rather than fanning out to nonexistent regions.
        if served_by == "vllm":
            # A vLLM entry is catalogued ONLY when hybrid serving is on AND its
            # endpoint_key is in the operator allowlist. Flag off / unknown key
            # => the entry is NOT catalogued at all, so it resolves exactly like
            # a model that does not exist (unservable), and a request naming it
            # is rejected pre-reserve — never routed with a bogus "self-hosted"
            # region into the Bedrock client.
            from mvp.serving.vllm import endpoint_is_servable

            if not endpoint_is_servable(entry.endpoint_key):
                continue
            # Enforce the zero-cache-rate invariant the moment a vLLM entry
            # actually becomes servable (lazy, avoiding a models<->pricing import
            # cycle at module load). A nonzero cache rate would be dead pricing
            # that also biases SAAR's warm-prefix delta — fail fast.
            from mvp.models import assert_vllm_cache_rates_zero
            assert_vllm_cache_rates_zero()
            for alias in entry.aliases:
                target = Target(
                    model_id=entry.bedrock_model_id,
                    region="self-hosted",
                    cost_tier=_tier_for(entry.pricing_key),
                    price_key=entry.pricing_key,
                    served_by="vllm",
                    endpoint_key=entry.endpoint_key,
                )
                catalog[alias] = [target]
                catalog[entry.bedrock_model_id] = [target]
            continue
        # Every Converse entry is catalogued the same way, Anthropic or not. The
        # filter used to be `provider == "anthropic"` because Converse only carried
        # Claude; Nemotron and Qwen3 ride it now, and giving them a special branch
        # pinned to their own region would inject a region the operator's residency
        # policy never allowed. Residency wins: a model that is not offered in the
        # configured primary/failover regions must not be registered at all.
        if entry.wire_protocol != "messages":
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


# The tier boundaries are expressed as the reference keys that DEFINE each tier,
# not as literal rates: a repricing of Haiku or Sonnet then moves the boundary with
# it instead of leaving a stale threshold behind. Ordered cheapest tier first.
_TIER_BOUNDARY_KEYS: tuple[tuple[int, str], ...] = ((1, "haiku"), (2, "sonnet"))
_TIER_ABOVE_BOUNDARIES = 3
_UNKNOWN_KEY_TIER = 2


@lru_cache(maxsize=None)
def _tier_for(pricing_key: str) -> int:
    """Cost tier from the key's BUILT-IN price, not from its name.

    Memoised: the catalog asks for a tier once per target, and the built-in floor is
    an import-time snapshot, so the answer cannot change within a process.

    The breaker's DOWNGRADE stage keeps only targets whose tier is at or below a
    cap, so a tier that disagrees with the price makes an expensive model look
    like a cheap fallback. Name matching did exactly that: `fable` is priced ABOVE
    the Opus tier and `gemma` at the Opus tier, yet both matched none of the
    substrings and scored 2 — the Sonnet tier — so a downgrade could "save money"
    by moving to a costlier model.

    A key lands in the cheapest tier whose reference model it does not out-price.
    That reproduces the Claude tiers exactly (haiku 1, sonnet 2, opus 3) while
    placing every other key by its actual rate.

    Only the built-in table is consulted: reading live admin overrides would put a
    DynamoDB call inside catalog construction and let a pricing edit silently
    re-tier the routing topology.
    """
    from mvp.pricing import baseline_rates

    rates = baseline_rates()
    rate = rates.get(pricing_key)
    if rate is None:
        return _UNKNOWN_KEY_TIER
    for tier, boundary_key in _TIER_BOUNDARY_KEYS:
        boundary = rates.get(boundary_key)
        if boundary is None:
            continue
        if rate.output_per_mtok_microusd <= boundary.output_per_mtok_microusd:
            return tier
    return _TIER_ABOVE_BOUNDARIES


def _tier_for_model(model: str) -> int:
    """Cost tier for a client-facing MODEL name (alias or Bedrock id).

    Distinct from `_tier_for`, which takes a pricing KEY. The two are easy to
    confuse and were once the same function: because it matched substrings of its
    argument, passing a model name happened to work for Claude (`claude-opus-4-6`
    contains "opus") and silently mis-tiered everything else — `gemma-4` and
    `fable` are priced at or above the Opus rate but contained none of the
    substrings, so a breaker downgrade could "save money" by moving to a costlier
    model. Resolving the name to its registry entry removes the coincidence.
    """
    from mvp.models import resolve_model

    try:
        return _tier_for(resolve_model(model).pricing_key)
    except Exception:  # noqa: BLE001 — an unresolvable name is not a routing error
        return _UNKNOWN_KEY_TIER


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


def _uncatalogued_targets(alias: str) -> list[Target]:
    """Targets for a name the catalog does not carry, priced from the registry.

    `resolve_bedrock_model` decides whether the name is servable at all — it
    raises for anything outside the Anthropic subset of the registry, and that
    raise is how an unknown model becomes a 400. What must NOT be decided here is
    the PRICE. This branch used to stamp `price_key="sonnet"` and `cost_tier=2`
    on whatever it had just resolved, so settle charged the Sonnet rate for a
    model that may be priced above Opus, and the invented tier walked straight
    through a breaker DOWNGRADE cap that exists to stop a "cheaper" fallback from
    being the expensive one. The registry knows both facts for every name it
    resolves; read them instead of assuming them.

    A vLLM-served entry that is NOT servable (hybrid serving off, or an endpoint
    outside the operator allowlist) yields no targets, so the chain exhausts
    exactly as it does for a model that does not exist. `_build_catalog` already
    stated that as the behaviour; it was only true for a non-Anthropic provider —
    an `anthropic` vLLM entry resolved through `resolve_bedrock_model` here and
    was routed into a Bedrock region the operator never chose to serve it from.
    """
    from mvp.models import resolve_bedrock_model, resolve_model

    model_id = resolve_bedrock_model(alias)
    entry = resolve_model(alias)
    if getattr(entry, "served_by", "bedrock") == "vllm":
        from mvp.serving.vllm import endpoint_is_servable
        if not endpoint_is_servable(entry.endpoint_key):
            return []
    price_key = entry.pricing_key
    tier = _tier_for(price_key)
    region = default_region()
    # Primary + the SAME configured failover regions as the catalog, so the
    # residency setting applies to the unregistered-alias fallback too (an
    # empty STRATOCLAVE_FAILOVER_REGIONS keeps this single-region).
    targets = [Target(model_id=model_id, region=region, cost_tier=tier, price_key=price_key)]
    for alt in failover_regions():
        if alt != region:
            targets.append(
                Target(model_id=model_id, region=alt, cost_tier=tier, price_key=price_key)
            )
    return targets


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
        targets = _uncatalogued_targets(alias)
        if not targets:
            # Resolvable but not servable (see `_uncatalogued_targets`). Said
            # separately from the exhausted-by-constraints case below so an
            # operator reading the log is not sent looking for a breaker or an
            # exclusion that had nothing to do with it.
            raise ValueError(f"Model '{alias}' is not servable in this deployment")

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
