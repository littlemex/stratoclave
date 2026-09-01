"""R29 (the F1 contract): negative headroom is rendered as a signed
deficit, never clamped, and no percentage is capped at 100.

R29's own "Verified by": "Unit: the API returns the signed value; the
surfaces show ceiling, settled, reserved, signed available and an 'over
ceiling by' line."

This file covers the API half. The console half (the 'over ceiling by' line
actually rendering) is covered separately in
frontend/src/pages/admin/AdminTenantDetail.ceiling.test.tsx.

Today `TenantBudgetsRepository.pool_summary()` clamps:

    remaining = max(int(item.get("pool_headroom_microusd", 0)), 0)
    # (or, on the no-headroom-attribute fallback branch)
    remaining = max(limit - reserved - settled, 0)

so a tenant genuinely over its ceiling (e.g. a lowered manual limit, or a
migration-time correction) reads `remaining_microusd = 0` -- indistinguishable
from a tenant sitting exactly at its ceiling. The frontend's own
`fmtMicroUsd` already renders negative numbers correctly (see
`frontend/src/lib/money.test.ts`'s "handles negatives with a leading sign"),
so this clamp on the BACKEND is the one thing stopping a real deficit from
ever reaching a reader -- confirmed against the raw counter path (`pool_reserved`)
which is NOT clamped by anything, only `pool_summary`'s convenience read is.
"""
from __future__ import annotations

from decimal import Decimal

from dynamo import TenantBudgetsRepository, current_period
from dynamo.tenant_budgets import budget_sk


def _seed_over_ceiling_row(tenant_id: str, period: str) -> None:
    """A row genuinely over its ceiling: settled spend alone exceeds the
    limit (e.g. a manual limit was lowered after spend had already
    committed). headroom is written to the TRUE (negative) invariant value,
    matching what `reserve_txn_item`/`settle_txn_item` would have left it at
    -- this fixture does not invent a shape those writers could not produce."""
    TenantBudgetsRepository()._table.put_item(Item={
        "tenant_id": tenant_id,
        "sk": budget_sk(period),
        "pool_limit_microusd": Decimal(100_000_000),   # $100 ceiling
        "pool_reserved_microusd": Decimal(0),
        "pool_settled_microusd": Decimal(150_000_000),  # $150 already spent
        "pool_headroom_microusd": Decimal(-50_000_000),  # $50 over
        "seat_count": Decimal(1),
        "manual_limit_microusd": Decimal(100_000_000),
        "status": "active",
        "version": "3",
    })


def test_pool_summary_returns_a_negative_remaining_uncapped(dynamodb_mock):
    tenant_id, period = "over-ceiling-co", current_period()
    _seed_over_ceiling_row(tenant_id, period)

    summary = TenantBudgetsRepository().pool_summary(tenant_id, period)

    assert summary["remaining_microusd"] == -50_000_000, (
        f"remaining_microusd was clamped to {summary['remaining_microusd']!r} "
        "instead of reporting the true $50 deficit"
    )


def test_pool_response_over_ceiling_by_field_reports_the_deficit(dynamodb_mock, monkeypatch):
    """The admin/team-lead HTTP response (`PoolBudgetResponse`) must carry an
    explicit, positive 'over ceiling by' figure derived from the signed
    remaining -- not merely a signed remaining a caller has to notice and
    negate themselves."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mvp.deps import AuthenticatedUser, get_current_user

    tenant_id, period = "over-ceiling-co", current_period()
    _seed_over_ceiling_row(tenant_id, period)

    from dynamo.tenants import TenantsRepository
    TenantsRepository().create(
        tenant_id=tenant_id, name="Over Ceiling Co",
        team_lead_user_id="admin-owned", created_by="admin-1",
    )

    def _admin_actor():
        return AuthenticatedUser(
            user_id="admin-1", email="admin@example", org_id="default-org",
            roles=["admin"], raw_claims={}, auth_kind="cognito",
        )

    from mvp import authz
    from mvp.admin_tenants import router
    monkeypatch.setattr(authz, "user_has_permission", lambda user, scope: True)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = _admin_actor
    client = TestClient(app)

    resp = client.get(f"/api/mvp/admin/tenants/{tenant_id}/pool-budget", params={"period": period})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["remaining_microusd"] == -50_000_000
    assert body.get("over_ceiling_microusd") == 50_000_000, (
        f"expected a positive over_ceiling_microusd of 50_000_000, got "
        f"{body.get('over_ceiling_microusd')!r} (body: {body})"
    )
