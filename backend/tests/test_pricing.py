"""Tests for dollar-denominated pricing (mvp.pricing).

Covers the integer micro-USD math, the per-token-type settlement pricing, the
reasoning-effort multiplier on the reservation estimate, and the hot-reload
cache that overlays PricingConfig rows on the built-in defaults.
"""
from __future__ import annotations

import pytest

from mvp import pricing


@pytest.fixture(autouse=True)
def _reset_pricing_cache():
    """Every test starts from built-in defaults, not another test's overrides."""
    pricing.reset_cache()
    yield
    pricing.reset_cache()


def test_estimate_uses_input_and_output_rates(dynamodb_mock):
    # opus default: input 5_000_000 /MTok, output 25_000_000 /MTok
    # 1 MTok input + 1 MTok output = 5_000_000 + 25_000_000 micro-USD
    cost = pricing.estimate_cost_microusd(
        pricing_key="opus",
        input_tokens_est=1_000_000,
        max_output_tokens=1_000_000,
    )
    assert cost == 30_000_000  # $30.00


def test_estimate_applies_effort_multiplier_to_output_only(dynamodb_mock):
    # 1 MTok output at 4x effort = 4 MTok priced at output rate; input 0.
    cost = pricing.estimate_cost_microusd(
        pricing_key="sonnet",  # output 15_000_000 /MTok
        input_tokens_est=0,
        max_output_tokens=1_000_000,
        effort_multiplier=4,
    )
    assert cost == 60_000_000  # 4 * 15_000_000


def test_estimate_rounds_up_sub_mtok(dynamodb_mock):
    # 1 token of Haiku input (1_000_000 /MTok) must round UP to 1 micro-USD,
    # never truncate to 0 — otherwise sub-MTok requests would be free.
    cost = pricing.estimate_cost_microusd(
        pricing_key="haiku",
        input_tokens_est=1,
        max_output_tokens=0,
    )
    assert cost == 1


def test_actual_cost_prices_each_token_type(dynamodb_mock):
    # haiku: in 1_000_000, out 5_000_000, cache_read 100_000, cache_write 1_250_000
    cost = pricing.actual_cost_microusd(
        pricing_key="haiku",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )
    assert cost == 1_000_000 + 5_000_000 + 100_000 + 1_250_000


def test_unknown_pricing_key_falls_back_to_default(dynamodb_mock):
    # An unknown key uses the "default" tier (Opus-priced), so it never
    # under-charges a budget.
    unknown = pricing.actual_cost_microusd(
        pricing_key="does-not-exist",
        input_tokens=1_000_000,
        output_tokens=0,
    )
    default = pricing.actual_cost_microusd(
        pricing_key="default",
        input_tokens=1_000_000,
        output_tokens=0,
    )
    assert unknown == default == 5_000_000


def test_pricing_config_override_is_hot_reloaded(dynamodb_mock):
    """A PricingConfig version overlays the defaults after the cache refreshes."""
    from dynamo.pricing_config import PricingConfigRepository

    repo = PricingConfigRepository()
    # Halve the opus input rate under a new version and activate it.
    override = pricing.Rate(
        input_per_mtok_microusd=2_500_000,
        output_per_mtok_microusd=25_000_000,
        cache_read_per_mtok_microusd=500_000,
        cache_write_per_mtok_microusd=6_250_000,
    )
    repo.set_rates(version="2026-07", rates={"opus": override})

    # Force a reload (bypass the 60s TTL) and confirm the override applies.
    pricing.reset_cache()
    cost = pricing.estimate_cost_microusd(
        pricing_key="opus",
        input_tokens_est=1_000_000,
        max_output_tokens=0,
    )
    assert cost == 2_500_000  # halved input rate took effect


def test_missing_pricing_table_keeps_defaults(dynamodb_mock):
    """If the pricing table read fails/returns nothing, defaults stand and no
    exception escapes (a request must never 500 because pricing is unset).
    """
    pricing.reset_cache()
    cost = pricing.estimate_cost_microusd(
        pricing_key="opus",
        input_tokens_est=1_000_000,
        max_output_tokens=0,
    )
    assert cost == 5_000_000


# ---------------------------------------------------------------------------
# Layer 5: snapshot freeze + rate_usage pure function
# ---------------------------------------------------------------------------


def test_snapshot_builtin_when_no_override(dynamodb_mock):
    pricing.reset_cache()
    pricing.reset_version_cache()
    snap = pricing.snapshot_rates("opus")
    assert snap.version == pricing.BUILTIN_VERSION
    assert snap.input_per_mtok_microusd == 5_000_000
    assert snap.output_per_mtok_microusd == 25_000_000


def test_snapshot_freezes_active_version_value(dynamodb_mock):
    from dynamo.pricing_config import PricingConfigRepository

    repo = PricingConfigRepository()
    repo.set_rates(version="2026-07", rates={"opus": pricing.Rate(
        input_per_mtok_microusd=2_500_000, output_per_mtok_microusd=25_000_000,
        cache_read_per_mtok_microusd=500_000, cache_write_per_mtok_microusd=6_250_000,
    )})
    pricing.reset_cache()
    pricing.reset_version_cache()
    snap = pricing.snapshot_rates("opus")
    assert snap.version == "2026-07"
    assert snap.input_per_mtok_microusd == 2_500_000


def test_charge_uses_frozen_snapshot_not_later_flip(dynamodb_mock):
    """The Layer-5 core guarantee: a snapshot taken at reserve rates a settle at
    the ADMITTED version even after the active version flips to a new price."""
    from dynamo.pricing_config import PricingConfigRepository

    repo = PricingConfigRepository()
    repo.set_rates(version="v1", rates={"opus": pricing.Rate(
        input_per_mtok_microusd=2_000_000, output_per_mtok_microusd=10_000_000,
        cache_read_per_mtok_microusd=0, cache_write_per_mtok_microusd=0,
    )})
    pricing.reset_cache()
    pricing.reset_version_cache()
    # "reserve": freeze v1
    snap = pricing.snapshot_rates("opus")
    assert snap.version == "v1"

    # Admin flips to a MORE EXPENSIVE v2 between reserve and settle.
    repo.set_rates(version="v2", rates={"opus": pricing.Rate(
        input_per_mtok_microusd=9_000_000, output_per_mtok_microusd=90_000_000,
        cache_read_per_mtok_microusd=0, cache_write_per_mtok_microusd=0,
    )})
    pricing.reset_cache()  # live cache now sees v2

    # "settle": charge from the FROZEN v1 snapshot, not the flipped v2.
    rec = pricing.rate_usage(snap, input_tokens=1_000_000, output_tokens=1_000_000)
    assert rec.pricing_version == "v1"
    assert rec.total_cost_microusd == 2_000_000 + 10_000_000  # v1 prices, NOT v2
    # And the record self-recomputes to its own total (INV-R2/R3).
    recomputed = sum(c["cost_microusd"] for c in rec.components.values())
    assert recomputed == rec.total_cost_microusd


def test_rate_usage_ceil_rounding_never_undercharges(dynamodb_mock):
    snap = pricing.RateSnapshot(
        version="t", pricing_key="opus",
        input_per_mtok_microusd=1_000_000, output_per_mtok_microusd=0,
        cache_read_per_mtok_microusd=0, cache_write_per_mtok_microusd=0,
    )
    # 1 token at 1_000_000/MTok = 1 micro-USD exactly / 1e6 -> ceil to 1.
    rec = pricing.rate_usage(snap, input_tokens=1, output_tokens=0)
    assert rec.total_cost_microusd == 1  # ceil, not 0


def test_set_rates_rejects_version_reuse_and_bad_strings(dynamodb_mock):
    """Immutable-version contract (Fable review M3/L1/L2): a version cannot be
    re-written with different rates, and reserved/malformed versions are rejected."""
    from dynamo.pricing_config import PricingConfigRepository

    repo = PricingConfigRepository()
    r = pricing.Rate(1, 1, 0, 0)
    repo.set_rates(version="v-immut", rates={"opus": r})
    # Re-using the same version is refused at the DB layer (attribute_not_exists).
    with pytest.raises(ValueError, match="already exists"):
        repo.set_rates(version="v-immut", rates={"opus": pricing.Rate(9, 9, 0, 0)})
    # Reserved sentinel + malformed strings are refused before any write.
    with pytest.raises(ValueError, match="reserved/empty"):
        repo.set_rates(version="builtin", rates={"opus": r})
    with pytest.raises(ValueError, match="malformed"):
        repo.set_rates(version="has__delim", rates={"opus": r})


def test_set_rates_same_version_same_values_is_idempotent_retry(dynamodb_mock):
    """N1 (Fable review-2): re-running set_rates with the SAME version and SAME
    values must succeed (crash-recovery / retry), while a value CHANGE is still
    rejected. This is the fix for the plain attribute_not_exists that made a
    partially-written version permanently unrecoverable."""
    from dynamo.pricing_config import PricingConfigRepository

    repo = PricingConfigRepository()
    r = pricing.Rate(3_000_000, 7_000_000, 0, 0)
    repo.set_rates(version="v-retry", rates={"opus": r})
    # Identical re-write (simulates completing a partially-written version): OK.
    repo.set_rates(version="v-retry", rates={"opus": pricing.Rate(3_000_000, 7_000_000, 0, 0)})
    # Different value under the same version: rejected (immutable).
    with pytest.raises(ValueError, match="DIFFERENT rates"):
        repo.set_rates(version="v-retry", rates={"opus": pricing.Rate(9, 9, 0, 0)})


def test_rate_usage_rejects_unknown_rounding(dynamodb_mock):
    """M4: rate_usage must refuse a snapshot with an unimplemented rounding policy
    rather than silently charging ceil."""
    snap = pricing.RateSnapshot(
        version="t", pricing_key="opus",
        input_per_mtok_microusd=1_000_000, output_per_mtok_microusd=0,
        cache_read_per_mtok_microusd=0, cache_write_per_mtok_microusd=0,
        rounding="floor",
    )
    with pytest.raises(ValueError, match="rounding"):
        pricing.rate_usage(snap, input_tokens=1, output_tokens=0)


def test_reserve_event_carries_frozen_snapshot(dynamodb_mock):
    """H1 (Fable review): the RESERVE ledger event serializes the frozen rate
    snapshot, so the admitted rate is durable beyond the in-memory context and a
    recovery can restore it via RateSnapshot.from_ledger_dict()."""
    import json

    from dynamo import CreditLedgerRepository
    from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
    from dynamo.user_tenants import UserTenantsRepository
    from mvp._pipeline import reserve_credit

    class _U:
        user_id = "u-h1"
        org_id = "acme-h1"
        email = "u@example.com"
        roles = ("user",)

    period = current_period()
    UserTenantsRepository().ensure(
        user_id=_U.user_id, tenant_id=_U.org_id, role="user", total_credit=10**12
    )
    TenantBudgetsRepository().set_pool_limit(
        tenant_id=_U.org_id, period=period, pool_limit_microusd=10**12
    )
    pricing.reset_cache()
    pricing.reset_version_cache()
    ctx = reserve_credit(_U(), 4000, pricing_key="opus", cost_microusd=2_000_000)

    resp = CreditLedgerRepository()._table.get_item(
        Key={"pk": f"TENANT#{_U.org_id}#P#{period}",
             "sk": f"EV#HOLD#{ctx.hold_id}#RESERVE"},
        ConsistentRead=True,
    )
    item = resp.get("Item")
    assert item is not None and item.get("rate_snapshot")
    restored = pricing.RateSnapshot.from_ledger_dict(json.loads(item["rate_snapshot"]))
    # The restored snapshot equals what was frozen on the context.
    assert restored == ctx.rate_snapshot
    # And the RESERVE event's pricing_version is the frozen version, not "opus".
    assert item["pricing_version"] == ctx.rate_snapshot.version
    assert item["pricing_version"] != "opus"


def test_rate_usage_cost_passthrough_when_present(dynamodb_mock):
    snap = pricing.RateSnapshot(
        version="t", pricing_key="opus",
        input_per_mtok_microusd=5_000_000, output_per_mtok_microusd=25_000_000,
        cache_read_per_mtok_microusd=0, cache_write_per_mtok_microusd=0,
        cost_input_per_mtok_microusd=2_000_000,
        cost_output_per_mtok_microusd=10_000_000,
    )
    rec = pricing.rate_usage(snap, input_tokens=1_000_000, output_tokens=1_000_000)
    assert rec.total_cost_microusd == 30_000_000
    assert rec.provider_cost_microusd == 12_000_000
    assert rec.margin_microusd == 18_000_000
