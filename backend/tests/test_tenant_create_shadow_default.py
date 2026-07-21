"""HTTP + integration tests for tenant creation and the shadow-VSR default.

Two things are locked here:

1. REGRESSION GUARD (Fable per-tenant review Blocker): the `@router.post("")`
   decorator must sit on `create_tenant`, not on a helper inserted above it. A
   misplaced decorator silently hijacks POST /tenants (breaking creation AND
   exposing an unauthenticated handler), and no unit test of the permission
   helper catches it — only exercising the ROUTE does. So we POST the route.

2. The new-tenant shadow default: creating a tenant provisions an EXPLICIT
   shadow_vsr=True routing-config record (so the Savings Certificate is populated
   from week one), the write is non-clobbering (single-attribute conditional
   update, never a full-replace put), and the env opt-out disables it.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mvp.deps import AuthenticatedUser, get_current_user


def _admin_user() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="admin-1", email="admin@example", org_id="default-org",
        roles=["admin"], raw_claims={}, auth_kind="cognito",
    )


@pytest.fixture
def client(dynamodb_mock, monkeypatch):
    from mvp import authz
    monkeypatch.setattr(authz, "user_has_permission", lambda u, s: True)
    from mvp.routing import config as rc
    rc._cache.clear()
    from mvp.admin_tenants import router
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = _admin_user
    return TestClient(app)


def _create(client, name="Acme Eng"):
    return client.post("/api/mvp/admin/tenants",
                       json={"name": name, "team_lead_user_id": "admin-owned"})


def test_create_tenant_route_is_registered_and_returns_201(client):
    """The Blocker regression: the POST route must run create_tenant (201 +
    TenantItem), not a hijacked helper. A misplaced decorator makes this 404/500."""
    r = _create(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Acme Eng"
    assert "tenant_id" in body


def test_create_tenant_provisions_shadow_on_by_default(client, monkeypatch):
    monkeypatch.delenv("STRATOCLAVE_SHADOW_VSR_NEW_TENANT_DEFAULT", raising=False)
    r = _create(client, name="Shadowed")
    tid = r.json()["tenant_id"]
    # enforcement layer reads shadow_vsr=True for the fresh tenant.
    from mvp.routing.config import get_tenant_routing_config
    assert get_tenant_routing_config(tid).shadow_vsr is True


def test_shadow_default_opt_out_env(client, monkeypatch):
    monkeypatch.setenv("STRATOCLAVE_SHADOW_VSR_NEW_TENANT_DEFAULT", "false")
    r = _create(client, name="Opted Out")
    tid = r.json()["tenant_id"]
    from mvp.routing.config import get_tenant_routing_config
    # no provisioning write -> tri-state None (follow the global default).
    assert get_tenant_routing_config(tid).shadow_vsr is None


def test_provision_is_non_clobbering(dynamodb_mock, monkeypatch):
    """The default write must NEVER overwrite an existing config (Fable High/Medium):
    a tenant that already has quotas + explicit shadow_vsr=False keeps both."""
    from mvp import admin_routing as ar
    from mvp.routing import config as rc
    rc._cache.clear()
    tid = "pre-existing"
    # seed a real config: quotas set, shadow explicitly OFF.
    ar._table().put_item(Item=ar.tenant_config_to_item(
        tid, ar.TenantRoutingConfigRequest(
            chain=["claude-sonnet-4-6"], shadow_vsr=False), updated_by="op"))
    rc.invalidate_routing_cache(tid)

    ar.provision_shadow_default_config(tid, updated_by="admin-1")

    rc.invalidate_routing_cache(tid)
    cfg = rc.get_tenant_routing_config(tid)
    assert cfg.shadow_vsr is False                 # explicit OFF preserved
    assert cfg.chain == ("claude-sonnet-4-6",)     # quotas/chain untouched


def test_provision_sets_shadow_when_absent(dynamodb_mock, monkeypatch):
    """When no shadow_vsr is present yet, the conditional update sets it True."""
    from mvp import admin_routing as ar
    from mvp.routing import config as rc
    rc._cache.clear()
    tid = "fresh-cfg"
    ar.provision_shadow_default_config(tid, updated_by="admin-1")
    rc.invalidate_routing_cache(tid)
    assert rc.get_tenant_routing_config(tid).shadow_vsr is True


def test_provision_partial_item_matches_no_config_except_shadow(dynamodb_mock):
    """advisory-only invariant (Fable per-tenant review-2 High): provisioning writes
    a PARTIAL item (only shadow_vsr). A provisioned tenant's resolved routing config
    must be byte-for-byte identical to a config-less tenant's EXCEPT shadow_vsr — the
    default MUST NOT change chain / quotas / fallback / free_tier for anyone."""
    from dataclasses import replace

    from mvp.routing import config as rc
    from mvp import admin_routing as ar
    rc._cache.clear()

    no_config = rc.get_tenant_routing_config("never-touched")   # item absent

    ar.provision_shadow_default_config("provisioned", updated_by="admin-1")
    rc.invalidate_routing_cache("provisioned")
    provisioned = rc.get_tenant_routing_config("provisioned")   # partial item

    # only shadow_vsr differs: normalise it and every other field must be equal.
    assert provisioned.shadow_vsr is True
    assert no_config.shadow_vsr is None
    assert replace(provisioned, shadow_vsr=None) == no_config


def test_create_tenant_requires_permission(dynamodb_mock, monkeypatch):
    """The other half of the Blocker (Fable per-tenant review-2 Medium): the route
    is authorization-gated. Without the tenants:create permission it must 403, not
    silently create + provision."""
    from mvp import authz
    monkeypatch.setattr(authz, "user_has_permission", lambda u, s: False)
    from mvp.routing import config as rc
    rc._cache.clear()
    from mvp.admin_tenants import router
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = _admin_user
    c = TestClient(app)
    r = c.post("/api/mvp/admin/tenants",
               json={"name": "Nope", "team_lead_user_id": "admin-owned"})
    assert r.status_code == 403, r.text
