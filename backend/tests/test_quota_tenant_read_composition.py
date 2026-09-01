"""Seam amendment B7 (SEAMS.md S10) — the tenant read gains the composition.

Nobody owned this: F3's scope excludes `tenant_budgets.py` and
`admin_tenants.py`; F1's read surface predates `pool_granted`; F2 owns
`admin_tenants.py` (it is in F2's own scope-boundary file list) but was
declared CLI-only and never committed to extending the tenant read.
Assigned to F2 by the seam review.

`GET /api/mvp/admin/tenants/{tenant_id}` (`mvp.admin_tenants.get_tenant`,
response model `TenantItem`) gains three new, always-present-but-nullable
fields, populated from the tenant's CURRENT-period pool row when one exists,
and left `None` when it does not (pool budgeting is opt-in, matching
`pool_summary`'s own existing convention):

  * `pool_granted_microusd`   — the live sum of ACTIVE + REVOKE_BLOCKED
                                  grants (B8's capacity-bearing figure)
  * `baseline_microusd`        — the derived baseline (B1:
                                  `pool_limit_microusd - pool_granted_microusd`)
  * `grant_cap_microusd`       — the RAW stored value, honestly `None` when
                                  absent (never silently materialised — B1)
  * `effective_cap_microusd`   — the RESOLVED cap: `grant_cap_microusd` if
                                  present, else `baseline_microusd` (B1)

Today `TenantItem` carries none of these at all, so every test below fails
against the CURRENT response shape (`KeyError`/`None` where a real figure is
expected), not an import error.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dynamo.tenants import TenantsRepository
from mvp.deps import AuthenticatedUser, get_current_user
from tests.quota_events_fixtures import seed_pool_with_grant_fields

TENANT = "composition-org"
PERIOD = "2026-09"


def _admin_client(monkeypatch) -> TestClient:
    from mvp import authz
    from mvp.admin_tenants import router

    monkeypatch.setattr(authz, "user_has_permission", lambda user, scope: True)
    app = FastAPI()
    app.include_router(router)
    actor = AuthenticatedUser(
        user_id="admin-1", email="admin@example.com", org_id="admin-1",
        roles=["admin"], raw_claims={}, auth_kind="cognito",
    )
    app.dependency_overrides[get_current_user] = lambda: actor
    return TestClient(app)


def test_b7_tenant_read_carries_the_full_composition_when_a_pool_exists(
    monkeypatch, dynamodb_mock,
):
    TenantsRepository().create(
        tenant_id=TENANT, name="Composition Co", team_lead_user_id="tl-1", created_by="tl-1",
    )
    seed_pool_with_grant_fields(
        TENANT, PERIOD, pool_limit_microusd=1_200_000_000, pool_granted_microusd=200_000_000,
        grant_cap_microusd=500_000_000,
    )
    client = _admin_client(monkeypatch)
    resp = client.get(f"/api/mvp/admin/tenants/{TENANT}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pool_granted_microusd"] == 200_000_000
    assert body["baseline_microusd"] == 1_000_000_000  # 1_200_000_000 - 200_000_000
    assert body["grant_cap_microusd"] == 500_000_000
    assert body["effective_cap_microusd"] == 500_000_000


def test_b7_effective_cap_falls_back_to_baseline_when_grant_cap_absent(
    monkeypatch, dynamodb_mock,
):
    """B1's absent-default, made explicit in the SAME read (B7): the raw
    field is honestly `None`, the effective field is never `None`."""
    TenantsRepository().create(
        tenant_id=TENANT, name="Composition Co", team_lead_user_id="tl-1", created_by="tl-1",
    )
    seed_pool_with_grant_fields(
        TENANT, PERIOD, pool_limit_microusd=900_000_000, pool_granted_microusd=100_000_000,
    )
    client = _admin_client(monkeypatch)
    resp = client.get(f"/api/mvp/admin/tenants/{TENANT}")
    body = resp.json()
    assert body["grant_cap_microusd"] is None
    assert body["baseline_microusd"] == 800_000_000
    assert body["effective_cap_microusd"] == 800_000_000  # falls back to baseline


def test_b7_composition_fields_are_none_when_the_tenant_has_no_pool_at_all(
    monkeypatch, dynamodb_mock,
):
    """Pool budgeting is opt-in (pool_summary's own existing convention) —
    a tenant with no BUDGET row for the period reads as None across the
    board, not zero (zero would falsely claim a pool exists and is
    fully-granted)."""
    TenantsRepository().create(
        tenant_id=TENANT, name="No Pool Co", team_lead_user_id="tl-1", created_by="tl-1",
    )
    client = _admin_client(monkeypatch)
    resp = client.get(f"/api/mvp/admin/tenants/{TENANT}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pool_granted_microusd"] is None
    assert body["baseline_microusd"] is None
    assert body["grant_cap_microusd"] is None
    assert body["effective_cap_microusd"] is None
