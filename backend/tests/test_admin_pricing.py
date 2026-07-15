"""Tests for the read-only pricing-config admin view (#66).

Covers the new `effective_rates()` seam (the only new logic — the merge/cache
itself is already tested in test_pricing.py) and the endpoint end-to-end:
  * no table -> pure built-in defaults, version None, no overrides
  * effective_rates() agrees with per-key _cache.get (the two paths can't diverge)
  * every registry pricing_key is priced (the "models" column is complete)
  * an override flips a key's `source` to "override" and surfaces the new rate
  * models column maps aliases to their pricing_key
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mvp import pricing
from mvp.admin_pricing import router as pricing_router
from mvp.deps import get_current_user
from mvp.models import _REGISTRY


@dataclass
class _AdminUser:
    user_id: str = "admin-1"
    org_id: str = "ops"
    email: str = "admin@example.com"
    roles: list = None
    auth_kind: str = "jwt"
    key_scopes: list = None

    def __post_init__(self):
        if self.roles is None:
            self.roles = ["admin"]


@pytest.fixture
def client(dynamodb_mock, monkeypatch):
    import mvp.authz as _authz

    monkeypatch.setattr(_authz, "user_has_permission", lambda u, p: True)
    pricing.reset_cache()
    app = FastAPI()
    app.include_router(pricing_router)
    app.dependency_overrides[get_current_user] = lambda: _AdminUser()
    return TestClient(app)


# ---- effective_rates() seam ------------------------------------------------
def test_effective_rates_no_overrides_is_pure_defaults(dynamodb_mock):
    pricing.reset_cache()
    version, rates, overrides = pricing.effective_rates()
    assert version is None
    assert overrides == set()
    assert rates == pricing._DEFAULT_RATES


def test_effective_rates_matches_per_key_get(dynamodb_mock):
    pricing.reset_cache()
    _, rates, _ = pricing.effective_rates()
    for key, rate in rates.items():
        assert pricing._cache.get(key) == rate


def test_registry_pricing_keys_all_priced(dynamodb_mock):
    pricing.reset_cache()
    _, rates, _ = pricing.effective_rates()
    for entry in _REGISTRY:
        assert entry.pricing_key in rates, f"{entry.pricing_key} unpriced"


def test_refresh_is_fail_static_when_load_rates_raises(dynamodb_mock, monkeypatch):
    """Fable #66 rev1 BUG1: a throw from load_rates() (transient Dynamo error)
    must NOT escape get()/effective_rates() — the money path must never 500
    because pricing is momentarily unreadable. The last-good map stands."""

    class _BoomRepo:
        def current_version(self):
            return "vX"  # a NEW version, forcing a load_rates() call

        def load_rates(self, version):
            raise RuntimeError("dynamo throttled")

    pricing.reset_cache()
    # Should not raise; falls back to the built-in defaults (last-good).
    rate = pricing._cache.get("opus", repo=_BoomRepo())
    assert rate == pricing._DEFAULT_RATES["opus"]
    version, rates, overrides = pricing._cache.effective_rates(repo=_BoomRepo())
    assert version is None and overrides == set()
    assert rates == pricing._DEFAULT_RATES


# ---- endpoint --------------------------------------------------------------
def test_endpoint_defaults(client):
    r = client.get("/api/mvp/admin/pricing-config")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] is None
    by_key = {row["pricing_key"]: row for row in body["rates"]}
    # opus default rate + source + a model mapped to it
    assert by_key["opus"]["source"] == "default"
    assert by_key["opus"]["input_per_mtok_microusd"] == 5_000_000
    assert any("opus" in m for m in by_key["opus"]["models"])


def test_endpoint_override_flips_source(client):
    from dynamo.pricing_config import PricingConfigRepository

    PricingConfigRepository().set_rates(
        version="v1",
        rates={
            "haiku": pricing.Rate(
                input_per_mtok_microusd=2_000_000,
                output_per_mtok_microusd=6_000_000,
                cache_read_per_mtok_microusd=200_000,
                cache_write_per_mtok_microusd=2_500_000,
            )
        },
    )
    pricing.reset_cache()  # bypass 60s TTL

    body = client.get("/api/mvp/admin/pricing-config").json()
    assert body["version"] == "v1"
    by_key = {row["pricing_key"]: row for row in body["rates"]}
    assert by_key["haiku"]["source"] == "override"
    assert by_key["haiku"]["input_per_mtok_microusd"] == 2_000_000
    # a non-overridden key stays a default
    assert by_key["opus"]["source"] == "default"


def test_endpoint_requires_permission(dynamodb_mock, monkeypatch):
    import mvp.authz as _authz

    monkeypatch.setattr(_authz, "user_has_permission", lambda u, p: False)
    pricing.reset_cache()
    app = FastAPI()
    app.include_router(pricing_router)
    app.dependency_overrides[get_current_user] = lambda: _AdminUser(roles=["user"])
    c = TestClient(app)
    r = c.get("/api/mvp/admin/pricing-config")
    assert r.status_code == 403, r.text
