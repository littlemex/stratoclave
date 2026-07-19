"""HTTP tests for the admin tenant pool-budget endpoints.

Exercises PUT/GET /api/mvp/admin/tenants/{id}/pool-budget end-to-end against a
mocked auth layer:

  - PUT creates/updates a dollar pool for a period; the dollar-cent input is
    stored as integer micro-USD and echoed back in both units.
  - GET returns the live pool usage, or 404 when no pool is set.
  - Both endpoints 404 on an unknown tenant.
  - A caller without `tenants:update` / `tenants:read-all` gets 403.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mvp.deps import AuthenticatedUser, get_current_user


def _admin_user() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="admin-1",
        email="admin@example",
        org_id="default-org",
        roles=["admin"],
        raw_claims={},
        auth_kind="cognito",
    )


def _patch_authz(monkeypatch, allow: set[str]) -> None:
    from mvp import authz

    def fake_user_has_permission(user, scope: str) -> bool:
        return scope in allow

    monkeypatch.setattr(authz, "user_has_permission", fake_user_has_permission)


def _make_app(monkeypatch, allow: set[str]) -> TestClient:
    _patch_authz(monkeypatch, allow=allow)
    from mvp.admin_tenants import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = _admin_user
    return TestClient(app)


def _seed_tenant(tenant_id: str = "acme-eng") -> str:
    from dynamo.tenants import TenantsRepository

    TenantsRepository().create(
        tenant_id=tenant_id,
        name="Acme Eng",
        team_lead_user_id="admin-owned",
        default_credit=100_000,
        created_by="admin-1",
    )
    return tenant_id


ADMIN_SCOPES = {"tenants:update", "tenants:read-all"}


def test_put_pool_budget_creates_and_converts_units(monkeypatch, dynamodb_mock):
    tid = _seed_tenant()
    client = _make_app(monkeypatch, allow=ADMIN_SCOPES)

    resp = client.put(
        f"/api/mvp/admin/tenants/{tid}/pool-budget",
        json={"limit_usd_cents": 50000, "period": "2026-07"},  # $500.00
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == tid
    assert body["period"] == "2026-07"
    assert body["pool_limit_microusd"] == 500_000_000  # 50000c * 10_000
    assert body["pool_limit_usd_cents"] == 50000
    assert body["remaining_microusd"] == 500_000_000
    assert body["pool_reserved_microusd"] == 0
    assert body["pool_settled_microusd"] == 0


def test_get_pool_budget_reflects_put(monkeypatch, dynamodb_mock):
    tid = _seed_tenant()
    client = _make_app(monkeypatch, allow=ADMIN_SCOPES)
    client.put(
        f"/api/mvp/admin/tenants/{tid}/pool-budget",
        json={"limit_usd_cents": 12345, "period": "2026-07"},
    )

    resp = client.get(f"/api/mvp/admin/tenants/{tid}/pool-budget?period=2026-07")
    assert resp.status_code == 200
    assert resp.json()["pool_limit_usd_cents"] == 12345


def test_put_updates_limit_but_preserves_spend(monkeypatch, dynamodb_mock):
    """Changing the ceiling mid-period must not reset reserved/settled."""
    from dynamo.tenant_budgets import TenantBudgetsRepository

    tid = _seed_tenant()
    client = _make_app(monkeypatch, allow=ADMIN_SCOPES)
    client.put(
        f"/api/mvp/admin/tenants/{tid}/pool-budget",
        json={"limit_usd_cents": 50000, "period": "2026-07"},
    )
    # Simulate some spend recorded against the pool. A real settle moves BOTH
    # pool_settled (+spend) AND pool_headroom (-spend) in one transaction, so the
    # invariant headroom == limit - reserved - settled stays intact; mirror that
    # here (a raw ADD to settled alone would leave headroom drifted, which never
    # happens in production and is not what this contract test is about).
    repo = TenantBudgetsRepository()
    repo._table.update_item(
        Key={"tenant_id": tid, "sk": "BUDGET#2026-07"},
        UpdateExpression="ADD pool_settled_microusd :s, pool_headroom_microusd :h",
        ExpressionAttributeValues={":s": 100_000_000, ":h": -100_000_000},
    )
    # Raise the ceiling.
    resp = client.put(
        f"/api/mvp/admin/tenants/{tid}/pool-budget",
        json={"limit_usd_cents": 80000, "period": "2026-07"},
    )
    body = resp.json()
    assert body["pool_limit_microusd"] == 800_000_000
    assert body["pool_settled_microusd"] == 100_000_000  # spend preserved
    assert body["remaining_microusd"] == 700_000_000


def test_get_missing_pool_is_404(monkeypatch, dynamodb_mock):
    tid = _seed_tenant()
    client = _make_app(monkeypatch, allow=ADMIN_SCOPES)
    resp = client.get(f"/api/mvp/admin/tenants/{tid}/pool-budget?period=2099-01")
    assert resp.status_code == 404


def test_unknown_tenant_is_404(monkeypatch, dynamodb_mock):
    client = _make_app(monkeypatch, allow=ADMIN_SCOPES)
    resp = client.put(
        "/api/mvp/admin/tenants/nope/pool-budget",
        json={"limit_usd_cents": 1000},
    )
    assert resp.status_code == 404


def test_put_requires_tenants_update_permission(monkeypatch, dynamodb_mock):
    tid = _seed_tenant()
    # Caller lacks tenants:update.
    client = _make_app(monkeypatch, allow={"tenants:read-all"})
    resp = client.put(
        f"/api/mvp/admin/tenants/{tid}/pool-budget",
        json={"limit_usd_cents": 1000},
    )
    assert resp.status_code == 403


def test_get_requires_read_permission(monkeypatch, dynamodb_mock):
    tid = _seed_tenant()
    client = _make_app(monkeypatch, allow={"tenants:update"})
    resp = client.get(f"/api/mvp/admin/tenants/{tid}/pool-budget")
    assert resp.status_code == 403


def test_rejects_bad_period_format(monkeypatch, dynamodb_mock):
    tid = _seed_tenant()
    client = _make_app(monkeypatch, allow=ADMIN_SCOPES)
    resp = client.put(
        f"/api/mvp/admin/tenants/{tid}/pool-budget",
        json={"limit_usd_cents": 1000, "period": "July"},
    )
    assert resp.status_code == 422
