"""Dollar-denominated pricing for credit reservations.

Stratoclave's original budget unit was Bedrock tokens. Token budgets cannot
distinguish an Opus token from a Haiku token, so a per-model dollar layer sits
on top: every model maps (via `ModelEntry.pricing_key`) to a rate row, and the
credit pipeline reserves/settles in **integer micro-USD** (1 USD = 1_000_000
micro-USD). Integer math throughout — floats never touch a budget balance, so
there is no rounding drift across millions of requests.

Rates come from two places, in order:
  1. The `PricingConfig` DynamoDB table (admin-editable, hot-reloaded on a
     60-second TTL by polling only the `CURRENT` pointer item).
  2. The built-in `_DEFAULT_RATES` below, used when the table has no row for a
     pricing key. This keeps a fresh deployment costing correctly before an
     admin ever touches pricing.

Rates are quoted per million tokens (per-MTok) in micro-USD, matching how
Bedrock and Anthropic publish list prices, e.g. Opus input at $5/MTok is
5_000_000 micro-USD per MTok.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional

from core.logging import get_logger
from dynamo.pricing_config import PricingConfigRepository


logger = get_logger(__name__)

MICRO_USD_PER_USD = 1_000_000
_TOKENS_PER_MTOK = 1_000_000


@dataclass(frozen=True)
class Rate:
    """Per-MTok rates in micro-USD for one pricing key."""

    input_per_mtok_microusd: int
    output_per_mtok_microusd: int
    cache_read_per_mtok_microusd: int
    cache_write_per_mtok_microusd: int


# Built-in list prices (micro-USD per MTok). Sourced from published on-demand
# Bedrock/Anthropic rates. `default` is a conservative fallback (Opus-priced)
# so an unpriced model never under-charges a budget.
_DEFAULT_RATES: dict[str, Rate] = {
    "opus": Rate(5_000_000, 25_000_000, 500_000, 6_250_000),
    "sonnet": Rate(3_000_000, 15_000_000, 300_000, 3_750_000),
    "haiku": Rate(1_000_000, 5_000_000, 100_000, 1_250_000),
    # GPT-5.x on bedrock-mantle. Output priced at the Opus tier as a
    # conservative default until an admin sets an exact rate.
    "gpt-5": Rate(5_000_000, 25_000_000, 500_000, 6_250_000),
    "default": Rate(5_000_000, 25_000_000, 500_000, 6_250_000),
}


# ---------------------------------------------------------------------------
# Hot-reloaded rate cache
# ---------------------------------------------------------------------------
_CACHE_TTL_SECONDS = 60.0


class _RateCache:
    """Process-local cache of the effective rate table.

    Holds the merged (defaults <- table overrides) rate map plus the pricing
    version string the overrides were loaded at. Refreshes when the TTL
    lapses; a refresh reads only the small `CURRENT` pointer, and pulls the
    full override set only when the version changed.
    """

    def __init__(self) -> None:
        self._rates: dict[str, Rate] = dict(_DEFAULT_RATES)
        self._version: Optional[str] = None
        self._loaded_at: float = 0.0
        # Serializes refreshes so concurrent requests don't stampede the table
        # or interleave a half-swapped rate map. The method really is locked now
        # (the name previously lied).
        self._lock = threading.Lock()

    def _refresh_locked(self, repo: PricingConfigRepository) -> None:
        try:
            version = repo.current_version()
        except Exception:  # noqa: BLE001 — table missing / transient: keep defaults.
            self._loaded_at = time.time()
            return
        if version is None:
            # Pricing overrides were removed (CURRENT pointer gone). Fall back to
            # built-in defaults rather than keeping the last-loaded override set
            # alive forever — otherwise a deleted override would never die.
            self._rates = dict(_DEFAULT_RATES)
            self._version = None
            self._loaded_at = time.time()
            return
        if version == self._version:
            self._loaded_at = time.time()
            return
        overrides = repo.load_rates(version)
        merged = dict(_DEFAULT_RATES)
        for key, rate in overrides.items():
            merged[key] = rate
        self._rates = merged
        self._version = version
        self._loaded_at = time.time()

    def get(self, pricing_key: str, repo: Optional[PricingConfigRepository] = None) -> Rate:
        now = time.time()
        if now - self._loaded_at >= _CACHE_TTL_SECONDS:
            # Double-checked under the lock: only one thread refreshes; the rest
            # either wait briefly and see the fresh map, or skip if it was just
            # loaded. A refresh failure keeps the previous map (fail-static).
            with self._lock:
                if time.time() - self._loaded_at >= _CACHE_TTL_SECONDS:
                    self._refresh_locked(repo or PricingConfigRepository())
        rates = self._rates
        return rates.get(pricing_key) or rates.get("default") or _DEFAULT_RATES["default"]

    def reset(self) -> None:
        """Test hook: drop cached state so the next get() reloads."""
        self._rates = dict(_DEFAULT_RATES)
        self._version = None
        self._loaded_at = 0.0


_cache = _RateCache()


def reset_cache() -> None:
    """Reset the module-level rate cache (used by tests)."""
    _cache.reset()


def _mtok_cost(tokens: int, per_mtok_microusd: int) -> int:
    """Cost in micro-USD for `tokens` at a per-MTok rate, rounded up.

    Rounding up (ceil) is deliberate: a budget must never be under-charged by
    integer truncation, or a caller could nibble past a limit one sub-MTok
    request at a time.
    """
    if tokens <= 0:
        return 0
    numerator = tokens * per_mtok_microusd
    return -(-numerator // _TOKENS_PER_MTOK)  # ceil division


def estimate_cost_microusd(
    *,
    pricing_key: str,
    input_tokens_est: int,
    max_output_tokens: int,
    effort_multiplier: int = 1,
    repo: Optional[PricingConfigRepository] = None,
) -> int:
    """Up-front reservation cost in micro-USD for a request.

    Mirrors the token reservation the pipeline already computes
    (`input_estimate + max_output * effort_multiplier`) but priced per token
    type: input at the input rate, the (multiplied) max output at the output
    rate. Reasoning-effort multipliers (1/2/4/8 on the OpenAI route) apply to
    the output leg only, matching where the extra tokens are actually spent.
    """
    rate = _cache.get(pricing_key, repo)
    reserved_output = max(max_output_tokens, 0) * max(effort_multiplier, 1)
    return (
        _mtok_cost(max(input_tokens_est, 0), rate.input_per_mtok_microusd)
        + _mtok_cost(reserved_output, rate.output_per_mtok_microusd)
    )


def actual_cost_microusd(
    *,
    pricing_key: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    repo: Optional[PricingConfigRepository] = None,
) -> int:
    """Settled cost in micro-USD from a response's real usage block.

    Priced per token type so an Opus batch and a Sonnet batch in the same
    tenant settle at their own rates — no blended assumption.
    """
    rate = _cache.get(pricing_key, repo)
    return (
        _mtok_cost(max(input_tokens, 0), rate.input_per_mtok_microusd)
        + _mtok_cost(max(output_tokens, 0), rate.output_per_mtok_microusd)
        + _mtok_cost(max(cache_read_tokens, 0), rate.cache_read_per_mtok_microusd)
        + _mtok_cost(max(cache_write_tokens, 0), rate.cache_write_per_mtok_microusd)
    )
