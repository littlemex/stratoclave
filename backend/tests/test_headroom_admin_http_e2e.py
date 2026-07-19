"""End-to-end HTTP contract test for the headroom ledger via the admin routes.

Drives the SAME surface the UI admin dashboard and the CLI admin commands call
(PUT/GET /api/mvp/admin/tenants/{id}/pool-budget) against the REAL FastAPI
router and the REAL credit pipeline on moto DynamoDB, proving the headroom
redesign did not break the CLI/UI-facing contract:

  1. PUT creates a pool; GET reports remaining == full limit (headroom-derived).
  2. A real pipeline reserve decrements the remaining the UI shows.
  3. A real settle (actual < reserved) returns the remainder to remaining.
  4. An admin ceiling RAISE preserves settled and lifts remaining by the delta.
  5. An admin ceiling CUT below spend clamps remaining to 0 AND makes the next
     reserve fail 402 (headroom went negative) — no over-admission.

This is the live CLI/UI verification for the headroom hot-path change: every
number the operator sees in the dashboard is asserted against the ledger.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from dynamo.tenant_budgets import TenantBudgetsRepository
from dynamo.tenants import TenantsRepository
from dynamo.user_tenants import UserTenantsRepository
from mvp import authz
from mvp._pipeline import reserve_credit, settle_reservation_and_log
from mvp.deps import AuthenticatedUser, get_current_user


@dataclass
class _User:
    user_id: str
    org_id: str
    email: str = "u@e2e"


def _client(monkeypatch) -> TestClient:
    monkeypatch.setattr(authz, "user_has_permission", lambda user, scope: True)
    from mvp.admin_tenants import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id="admin-1", email="admin@e2e", org_id="e2e-org",
        roles=["admin"], raw_claims={}, auth_kind="cognito",
    )
    return TestClient(app)


def test_headroom_admin_http_contract_end_to_end(monkeypatch, dynamodb_mock):
    tid = "e2e-org"
    # Pin the period everywhere so the admin PUT/GET and the pipeline reserve all
    # act on the same pool row (reserve_credit uses current_period() internally).
    import dynamo.tenant_budgets as _tb
    import mvp._pipeline as _pl
    period = _tb.current_period()
    monkeypatch.setattr(_pl, "current_period", lambda: period)
    TenantsRepository().create(
        tenant_id=tid, name="E2E", team_lead_user_id="admin-1",
        default_credit=1_000_000, created_by="admin-1",
    )
    UserTenantsRepository().ensure(
        user_id="u1", tenant_id=tid, role="user", total_credit=1_000_000_000,
    )
    client = _client(monkeypatch)
    base = f"/api/mvp/admin/tenants/{tid}/pool-budget"

    # 1. PUT $50 pool → GET shows full remaining.
    r = client.put(base, json={"limit_usd_cents": 5000, "period": period})
    assert r.status_code == 200, r.text
    assert r.json()["pool_limit_microusd"] == 50_000_000
    assert r.json()["remaining_microusd"] == 50_000_000
    g = client.get(f"{base}?period={period}")
    assert g.status_code == 200
    assert g.json()["remaining_microusd"] == 50_000_000

    # 2. Real pipeline reserve of $10 → the UI's remaining drops to $40.
    user = _User(user_id="u1", org_id=tid)
    ctx = reserve_credit(user, 1000, pricing_key="opus", cost_microusd=10_000_000)
    assert client.get(f"{base}?period={period}").json()["remaining_microusd"] == 40_000_000
    s = TenantBudgetsRepository().pool_summary(tid, period)
    assert s["pool_headroom_microusd"] == (
        s["pool_limit_microusd"] - s["pool_reserved_microusd"] - s["pool_settled_microusd"]
    )

    # 3. Settle $4 of the $10 held → remaining returns to $46 (50 - 4 settled).
    settle_reservation_and_log(
        user=user, tenants_repo=ctx, reservation=1000,
        actual_input_tokens=100, actual_output_tokens=200,
        model_id="us.anthropic.claude-opus-4-7", context=ctx,
        actual_cost_microusd=4_000_000,
    )
    assert client.get(f"{base}?period={period}").json()["remaining_microusd"] == 46_000_000

    # 4. Admin RAISE to $80 → settled preserved, remaining = 80 - 4 = 76.
    r2 = client.put(base, json={"limit_usd_cents": 8000, "period": period})
    assert r2.json()["pool_settled_microusd"] == 4_000_000
    assert r2.json()["remaining_microusd"] == 76_000_000

    # 5. Admin CUT to $3 (< $4 settled) → remaining clamps to 0 and the next
    #    reserve is refused 402 (headroom negative, no over-admission).
    r3 = client.put(base, json={"limit_usd_cents": 300, "period": period})
    assert r3.json()["remaining_microusd"] == 0
    with pytest.raises(HTTPException) as exc:
        reserve_credit(user, 1, pricing_key="opus", cost_microusd=1)
    assert exc.value.status_code == 402
