"""Read-only admin view of the effective pricing config (#66).

Exposes the merged (built-in defaults <- DynamoDB overrides) per-pricing-key
rate table that `mvp.pricing` actually charges against, plus which models map
to each key and whether a key is a default or an operator override. Full CRUD
(editing rates, version history) is deferred to P2 — this is visibility only.

Permission: ``usage:read-all``. Pricing is the multiplier behind every cost
figure the usage-admin views already surface, and it is global (not tenant)
data, so the usage-admin scope is the right fit; a dedicated ``pricing:read``
is deferred to arrive as a matched pair with ``pricing:write`` when CRUD lands.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .authz import require_permission
from .deps import AuthenticatedUser
from .models import _REGISTRY
from .pricing import effective_rates

router = APIRouter(prefix="/api/mvp/admin", tags=["mvp-admin-pricing"])


class PricingRateEntry(BaseModel):
    pricing_key: str
    input_per_mtok_microusd: int
    output_per_mtok_microusd: int
    cache_read_per_mtok_microusd: int
    cache_write_per_mtok_microusd: int
    source: str  # "default" | "override"
    models: list[str]  # client-facing aliases whose ModelEntry maps to this key


class PricingConfigResponse(BaseModel):
    version: Optional[str]  # null = pure built-in defaults (no overrides)
    rates: list[PricingRateEntry]


@router.get("/pricing-config", response_model=PricingConfigResponse)
def get_pricing_config(
    _admin: AuthenticatedUser = Depends(require_permission("usage:read-all")),
) -> PricingConfigResponse:
    version, rates, override_keys = effective_rates()
    models_by_key: dict[str, list[str]] = {}
    for entry in _REGISTRY:
        models_by_key.setdefault(entry.pricing_key, []).extend(entry.aliases)
    return PricingConfigResponse(
        version=version,
        rates=[
            PricingRateEntry(
                pricing_key=key,
                input_per_mtok_microusd=rate.input_per_mtok_microusd,
                output_per_mtok_microusd=rate.output_per_mtok_microusd,
                cache_read_per_mtok_microusd=rate.cache_read_per_mtok_microusd,
                cache_write_per_mtok_microusd=rate.cache_write_per_mtok_microusd,
                source="override" if key in override_keys else "default",
                models=models_by_key.get(key, []),
            )
            for key, rate in sorted(rates.items())
        ],
    )
