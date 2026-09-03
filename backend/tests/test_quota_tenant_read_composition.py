"""Seam amendment B7 (SEAMS S10) — the tenant read gains the composition.

Nobody owned this: F3's scope excludes `tenant_budgets.py` and
`admin_tenants.py`; F1's read surface predates `pool_granted`; F2 owns
`admin_tenants.py` (it is in F2's own scope-boundary file list) but was
declared CLI-only and never committed to extending the tenant read.
Assigned to F2 by the seam review.

B7's own text named `GET /api/mvp/admin/tenants/{tenant_id}` (`TenantItem`)
as the surface to extend. That endpoint carries no pool information at all,
before or after F2 — it is bare tenant metadata (name, credit, status). The
composition landed instead on `GET /api/mvp/admin/tenants/{tenant_id}/pool-budget`
(`PoolBudgetResponse`), which ALREADY carried the ceiling's mode/seat
composition before F2 and is where an admin/approver surface actually reads
pool state from. B7's seam review named the right NEED (a tenant read with
the grant composition) but the wrong endpoint, guessed without seeing that
`PoolBudgetResponse` already existed as the natural, pre-existing home for
exactly this kind of figure. Four fields, not three, land there:

  * `pool_granted_microusd`             — live ACTIVE + REVOKE_BLOCKED sum (B8)
  * `baseline_microusd`                  — the derived baseline (B1)
  * `grant_cap_microusd`                  — the RAW stored value, honestly
                                             `None` when absent (B1)
  * `effective_grant_cap_microusd`        — the RESOLVED cap (B1)

`get_pool_budget` keeps its OWN, pre-existing convention (opt-in pool
budgeting): 404 when the tenant has no pool row for the period, never a 200
with null composition fields — the same reason `PoolBudgetResponse`'s fields
are non-Optional except `grant_cap_microusd` itself.
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
    resp = client.get(f"/api/mvp/admin/tenants/{TENANT}/pool-budget", params={"period": PERIOD})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pool_granted_microusd"] == 200_000_000
    assert body["baseline_microusd"] == 1_000_000_000  # 1_200_000_000 - 200_000_000
    assert body["grant_cap_microusd"] == 500_000_000
    assert body["effective_grant_cap_microusd"] == 500_000_000


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
    resp = client.get(f"/api/mvp/admin/tenants/{TENANT}/pool-budget", params={"period": PERIOD})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["grant_cap_microusd"] is None
    assert body["baseline_microusd"] == 800_000_000
    assert body["effective_grant_cap_microusd"] == 800_000_000  # falls back to baseline


def test_b7_composition_fields_are_none_when_the_tenant_has_no_pool_at_all(
    monkeypatch, dynamodb_mock,
):
    """Pool budgeting is opt-in (`pool_summary`'s own existing convention,
    which `get_pool_budget` already followed before F2): a tenant with no
    BUDGET row for the period 404s — the endpoint's own pre-existing
    behaviour — rather than a 200 with every composition field `None`, which
    would need fields this response model does not make Optional."""
    TenantsRepository().create(
        tenant_id=TENANT, name="No Pool Co", team_lead_user_id="tl-1", created_by="tl-1",
    )
    client = _admin_client(monkeypatch)
    resp = client.get(f"/api/mvp/admin/tenants/{TENANT}/pool-budget", params={"period": PERIOD})
    assert resp.status_code == 404, resp.text
