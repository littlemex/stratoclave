"""HTTP-boundary tests for the routing-config admin API (P0 client gap).

Full TestClient with mocked admin auth + moto. Verifies the four endpoints
end-to-end: GET defaults when unset, PUT validates + writes an item that the
enforcement layer (config.get_tenant_routing_config) then reads back, malformed
configs 400, unknown tenant 404, and the write invalidates the read cache so
the same process sees its own write.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mvp.admin_routing import router as routing_router
from mvp.deps import get_current_user


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


TENANT = "acme-eng"


@pytest.fixture
def client(dynamodb_mock, monkeypatch):
    import mvp.authz as _authz
    monkeypatch.setattr(_authz, "user_has_permission", lambda u, p: True)
    # seed the tenant so _require_tenant passes
    from dynamo.tenants import TenantsRepository
    TenantsRepository().create(tenant_id=TENANT, team_lead_user_id="admin-1",
                               name="Acme", created_by="admin-1")
    # seed user u1 as a tenant member so _require_user_in_tenant passes (F3);
    # u2/uzer are intentionally absent to test the 404.
    from dynamo import UserTenantsRepository
    UserTenantsRepository().ensure(user_id="u1", tenant_id=TENANT, role="user",
                                   total_credit=10**9)
    # clear routing cache between tests
    from mvp.routing import config as rc
    rc._cache.clear()
    app = FastAPI()
    app.include_router(routing_router)
    app.dependency_overrides[get_current_user] = lambda: _AdminUser()
    return TestClient(app)


class TestTenantRoutingConfig:
    def test_get_defaults_when_unset(self, client):
        r = client.get(f"/api/mvp/admin/tenants/{TENANT}/routing-config")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["configured"] is False
        assert body["chain"] == [] and body["allowlist"] == []
        assert body["fallback_default"] == "off"

    def test_put_then_get_and_enforcement_reads_it(self, client):
        cfg = {
            "chain": ["claude-sonnet-4-6", "claude-haiku-4-5"],
            "quotas": {"claude-sonnet-4-6": {"limit": 5000000}},
            "fallback_default": "on",
        }
        r = client.put(f"/api/mvp/admin/tenants/{TENANT}/routing-config", json=cfg)
        assert r.status_code == 200, r.text
        assert r.json()["configured"] is True
        assert r.json()["chain"] == cfg["chain"]

        # GET reflects it
        g = client.get(f"/api/mvp/admin/tenants/{TENANT}/routing-config")
        assert g.json()["chain"] == cfg["chain"]

        # the ENFORCEMENT layer reads the same config (cache was invalidated)
        from mvp.routing.config import get_tenant_routing_config
        live = get_tenant_routing_config(TENANT)
        assert live.chain == ("claude-sonnet-4-6", "claude-haiku-4-5")
        assert live.fallback_default == "on"
        assert int(live.quotas["claude-sonnet-4-6"].limit) == 5000000

    def test_unknown_model_400(self, client):
        r = client.put(f"/api/mvp/admin/tenants/{TENANT}/routing-config",
                       json={"chain": ["no-such-model"]})
        assert r.status_code == 400
        assert "no-such-model" in r.text

    def test_chain_outside_allowlist_400(self, client):
        r = client.put(f"/api/mvp/admin/tenants/{TENANT}/routing-config",
                       json={"allowlist": ["claude-sonnet-4-6"],
                             "chain": ["claude-haiku-4-5"]})
        assert r.status_code == 400
        assert "allowlist" in r.text

    def test_unknown_field_422(self, client):
        r = client.put(f"/api/mvp/admin/tenants/{TENANT}/routing-config",
                       json={"surprise": 1})
        assert r.status_code == 422  # pydantic extra=forbid

    def test_unknown_tenant_404(self, client):
        r = client.put("/api/mvp/admin/tenants/ghost/routing-config",
                       json={"chain": ["claude-sonnet-4-6"]})
        assert r.status_code == 404


class TestUserRoutingConfig:
    def test_put_user_subsequence_then_enforcement_reads(self, client):
        # tenant chain first
        client.put(f"/api/mvp/admin/tenants/{TENANT}/routing-config",
                   json={"chain": ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5"]})
        # user chain = subsequence
        r = client.put(f"/api/mvp/admin/tenants/{TENANT}/users/u1/routing-config",
                       json={"chain": ["claude-opus-4-7", "claude-haiku-4-5"], "fallback": "on"})
        assert r.status_code == 200, r.text
        from mvp.routing.config import get_user_routing_config
        live = get_user_routing_config(TENANT, "u1")
        assert live.chain == ("claude-opus-4-7", "claude-haiku-4-5")
        assert live.fallback == "on"

    def test_user_chain_not_subsequence_400(self, client):
        client.put(f"/api/mvp/admin/tenants/{TENANT}/routing-config",
                   json={"chain": ["claude-opus-4-7", "claude-sonnet-4-6"]})
        # reversed order = not a subsequence
        r = client.put(f"/api/mvp/admin/tenants/{TENANT}/users/u1/routing-config",
                       json={"chain": ["claude-sonnet-4-6", "claude-opus-4-7"]})
        assert r.status_code == 400
        assert "subsequence" in r.text

    def test_get_member_user_defaults_when_unset(self, client):
        # u1 is a member but has no override yet -> configured:false
        r = client.get(f"/api/mvp/admin/tenants/{TENANT}/users/u1/routing-config")
        assert r.status_code == 200
        assert r.json()["configured"] is False

    def test_non_member_user_404(self, client):
        # Fable rev1 F3: a user not in the tenant must 404, not write an orphan.
        r = client.put(f"/api/mvp/admin/tenants/{TENANT}/users/ghost-user/routing-config",
                       json={"fallback": "on"})
        assert r.status_code == 404
        r2 = client.get(f"/api/mvp/admin/tenants/{TENANT}/users/ghost-user/routing-config")
        assert r2.status_code == 404
