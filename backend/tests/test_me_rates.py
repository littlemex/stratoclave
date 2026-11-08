"""Tests for the caller's own effective billing rates (`GET /api/mvp/me/rates`).

Covers:
  * the endpoint reads the SAME `effective_rates()` seam `admin_pricing`
    already reads, so it can never show a different price than what
    `mvp.pricing` actually charges
  * `models` on each row is narrowed to aliases the CALLER may invoke,
    using the identical `mvp.eligibility.refusal_for` filter `GET /v1/models`
    and `GET /openai/v1/models` already apply to their own listings
  * a model-policy restriction on the tenant (allowlist) removes that
    model's pricing_key row when nothing else prices through it
  * an admin override flips a key's rate and is visible here too
  * `usage:read-self` (the same scope `/me/usage-summary` and
    `/me/usage-history` already require) gates the endpoint; no admin-only
    scope is needed
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mvp import pricing
from mvp.deps import get_current_user
from mvp.me import router as me_router
from mvp.models import _REGISTRY


@dataclass
class _FakeUser:
    user_id: str = "user-1"
    org_id: str = "acme-eng"
    email: str = "user@example.com"
    roles: list = None
    auth_kind: str = "jwt"
    key_scopes: list = None

    def __post_init__(self):
        if self.roles is None:
            self.roles = ["user"]


@pytest.fixture
def client(dynamodb_mock, monkeypatch):
    import mvp.authz as _authz

    # Scope gating itself is covered by test_endpoint_requires_permission
    # below; every other test bypasses it the same way
    # test_admin_pricing.py's own `client` fixture does, so it exercises the
    # eligibility/pricing logic rather than the permission lattice.
    monkeypatch.setattr(_authz, "user_has_permission", lambda u, p: True)
    pricing.reset_cache()
    app = FastAPI()
    app.include_router(me_router)
    app.dependency_overrides[get_current_user] = lambda: _FakeUser()
    return TestClient(app)


def test_endpoint_lists_every_registered_pricing_key_by_default(client):
    """With no tenant/user routing restriction and no entitlement gate, every
    `access="general"` pricing_key in the registry must appear -- the same
    "no allowlist is a passthrough" default `mvp.eligibility` documents."""
    resp = client.get("/api/mvp/me/rates")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] is None
    assert body["as_of"]
    by_key = {row["pricing_key"]: row for row in body["rates"]}

    general_keys = {
        e.pricing_key for e in _REGISTRY if e.access == "general"
    }
    assert general_keys <= set(by_key)


def test_rate_values_match_the_live_pricing_table(client):
    """A row's four per-MTok fields must be byte-identical to
    `mvp.pricing.effective_rates()` -- this view computes no price of its
    own."""
    resp = client.get("/api/mvp/me/rates")
    body = resp.json()
    _, live_rates, _ = pricing.effective_rates()
    for row in body["rates"]:
        live = live_rates[row["pricing_key"]]
        assert row["input_per_mtok_microusd"] == live.input_per_mtok_microusd
        assert row["output_per_mtok_microusd"] == live.output_per_mtok_microusd
        assert row["cache_read_per_mtok_microusd"] == live.cache_read_per_mtok_microusd
        assert row["cache_write_per_mtok_microusd"] == live.cache_write_per_mtok_microusd


def test_models_column_only_lists_eligible_aliases(client):
    """Every alias listed under a pricing_key must actually resolve to an
    entry priced at that key (the grouping is not scrambled), and at least
    one real alias for a known key (opus) must be present."""
    resp = client.get("/api/mvp/me/rates")
    body = resp.json()
    alias_to_key = {
        alias: e.pricing_key for e in _REGISTRY for alias in e.aliases
    }
    for row in body["rates"]:
        for alias in row["models"]:
            assert alias_to_key.get(alias) == row["pricing_key"]
    by_key = {row["pricing_key"]: row for row in body["rates"]}
    assert any("opus" in m for m in by_key.get("opus", {"models": []})["models"])


def test_admin_override_is_visible_here_too(client):
    """An admin pricing override must be reflected in THIS caller's own
    view, not just the admin-only pricing-config endpoint -- both read the
    same `effective_rates()` seam."""
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
    pricing.reset_cache()

    body = client.get("/api/mvp/me/rates").json()
    assert body["version"] == "v1"
    by_key = {row["pricing_key"]: row for row in body["rates"]}
    assert by_key["haiku"]["input_per_mtok_microusd"] == 2_000_000


def test_tenant_allowlist_removes_the_disallowed_models_key(dynamodb_mock, monkeypatch):
    """A tenant allowlisted to ONLY a `haiku`-family alias must not see the
    `opus` pricing_key at all -- mirrors C8's "absent, not
    present-and-marked" convention for `GET /v1/models`.

    Mounts `mvp.admin_routing` alongside `mvp.me` so the config can be
    written through the real PUT route (the same production write path an
    operator uses), exactly as `eligibility_fixtures.put_tenant_routing`
    does for the eligibility-listing suite.
    """
    import mvp.authz as _authz
    from dynamo.tenants import TenantsRepository

    monkeypatch.setattr(_authz, "user_has_permission", lambda u, p: True)
    pricing.reset_cache()

    TenantsRepository().create(
        tenant_id="acme-eng", name="Acme Eng", team_lead_user_id="someone-else",
        default_credit=100_000, created_by="admin-rates-test",
    )

    haiku_entry = next(e for e in _REGISTRY if e.pricing_key == "haiku")

    from mvp.admin_routing import router as admin_routing_router

    app = FastAPI()
    app.include_router(me_router)
    app.include_router(admin_routing_router)
    app.dependency_overrides[get_current_user] = lambda: _FakeUser()
    c = TestClient(app)

    put_resp = c.put(
        "/api/mvp/admin/tenants/acme-eng/routing-config",
        json={"allowlist": [haiku_entry.aliases[0]]},
    )
    assert put_resp.status_code == 200, put_resp.text

    body = c.get("/api/mvp/me/rates").json()
    by_key = {row["pricing_key"]: row for row in body["rates"]}
    assert "haiku" in by_key
    assert "opus" not in by_key


def test_endpoint_requires_permission(dynamodb_mock, monkeypatch):
    import mvp.authz as _authz

    monkeypatch.setattr(_authz, "user_has_permission", lambda u, p: False)
    pricing.reset_cache()
    app = FastAPI()
    app.include_router(me_router)
    app.dependency_overrides[get_current_user] = lambda: _FakeUser()
    c = TestClient(app)
    r = c.get("/api/mvp/me/rates")
    assert r.status_code == 403, r.text
