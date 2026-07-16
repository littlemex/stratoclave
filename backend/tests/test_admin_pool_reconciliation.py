"""HTTP tests for GET /api/mvp/admin/tenants/{id}/pool-reconciliation (P2-d).

The endpoint compares the budget counters (materialized cache) against the
credit ledger (append-only source of truth). Verifies:
  - after a real reserve+settle, counters and ledger agree → in_sync, zero drift;
  - an injected counter drift (mutating the budget row directly, which the ledger
    never sees) is reported as non-zero drift and in_sync=False;
  - RBAC: tenants:read-all is required (403 otherwise);
  - 404 on unknown tenant / no pool.
"""
from __future__ import annotations

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

    monkeypatch.setattr(
        authz, "user_has_permission", lambda user, scope: scope in allow
    )


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


class _User:
    def __init__(self, user_id, org_id):
        self.user_id = user_id
        self.org_id = org_id
        self.email = "u@example.com"
        self.roles = ("user",)


def _reserve_and_settle(tenant_id: str, period: str, cost: int, actual: int) -> None:
    """Drive a real pooled reserve+settle through the pipeline so the ledger and
    the counters are both written (RESERVE + SETTLE events)."""
    from dynamo.tenant_budgets import TenantBudgetsRepository
    from dynamo.user_tenants import UserTenantsRepository
    from mvp._pipeline import reserve_credit, settle_reservation_and_log

    user = _User(f"user-{tenant_id}", tenant_id)
    UserTenantsRepository().ensure(
        user_id=user.user_id, tenant_id=tenant_id, role="user",
        total_credit=1_000_000_000,
    )
    TenantBudgetsRepository().set_pool_limit(
        tenant_id=tenant_id, period=period, pool_limit_microusd=10_000_000_000,
    )
    ctx = reserve_credit(user, 4000, pricing_key="opus", cost_microusd=cost)
    settle_reservation_and_log(
        user=user, tenants_repo=ctx, reservation=ctx.reservation_tokens,
        actual_input_tokens=10, actual_output_tokens=5,
        model_id="us.anthropic.claude-opus-4-7", context=ctx,
        actual_cost_microusd=actual,
    )


ADMIN_SCOPES = {"tenants:read-all"}
PERIOD = "2026-07"


def test_reconciliation_in_sync_after_reserve_and_settle(monkeypatch, dynamodb_mock):
    tid = _seed_tenant()
    _reserve_and_settle(tid, PERIOD, cost=2_000_000, actual=1_500_000)
    client = _make_app(monkeypatch, allow=ADMIN_SCOPES)

    resp = client.get(f"/api/mvp/admin/tenants/{tid}/pool-reconciliation?period={PERIOD}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["in_sync"] is True, body
    assert body["snapshot_stable"] is True
    assert body["settled_drift_microusd"] == 0
    assert body["reserved_drift_microusd"] == 0
    assert body["reclaimed_drift_microusd"] == 0
    # settled counter reflects the settle; reserved returned to 0.
    assert body["counter_settled_microusd"] == 1_500_000
    assert body["ledger_settled_microusd"] == 1_500_000
    assert body["counter_reserved_microusd"] == 0
    assert body["ledger_reserved_microusd"] == 0


def test_reconciliation_detects_injected_counter_drift(monkeypatch, dynamodb_mock):
    from dynamo.tenant_budgets import TenantBudgetsRepository, budget_sk

    tid = _seed_tenant()
    _reserve_and_settle(tid, PERIOD, cost=2_000_000, actual=1_500_000)
    # Corrupt the settled counter WITHOUT a ledger event — exactly the drift the
    # reconciliation exists to catch (a counter that diverged from the ledger).
    TenantBudgetsRepository()._table.update_item(
        Key={"tenant_id": tid, "sk": budget_sk(PERIOD)},
        UpdateExpression="ADD pool_settled_microusd :d",
        ExpressionAttributeValues={":d": 777},
    )
    client = _make_app(monkeypatch, allow=ADMIN_SCOPES)

    resp = client.get(f"/api/mvp/admin/tenants/{tid}/pool-reconciliation?period={PERIOD}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["in_sync"] is False, body
    assert body["snapshot_stable"] is True
    # counter is ahead of the ledger by exactly the injected amount.
    assert body["settled_drift_microusd"] == 777
    assert body["counter_settled_microusd"] == 1_500_777
    assert body["ledger_settled_microusd"] == 1_500_000


def test_reconciliation_migrating_suppresses_reserved_drift(monkeypatch, dynamodb_mock):
    """R2-6: a pre-Phase-2 terminal (SETTLE with reserved_delta=-R but NO RESERVE
    event, as Phase 1 shipped) must NOT produce a phantom reserved drift. The
    period is reported migrating=True and reserved/reclaimed are excluded from
    in_sync so migrated tenants don't alarm on day 1."""
    from dynamo import CreditLedgerRepository
    from dynamo.tenant_budgets import TenantBudgetsRepository, current_period

    tid = _seed_tenant()
    period = current_period()
    TenantBudgetsRepository().set_pool_limit(
        tenant_id=tid, period=period, pool_limit_microusd=10_000_000_000,
    )
    # Simulate a Phase-1 SETTLE terminal directly (native put, no RESERVE event):
    # reserved_delta=-R, settled_delta=actual, on the shared TERMINAL sk.
    led = CreditLedgerRepository()
    led._table.put_item(Item={
        "pk": f"TENANT#{tid}#P#{period}",
        "sk": "EV#HOLD#legacy-hold-1#TERMINAL",
        "event_type": "SETTLE",
        "hold_id": "legacy-hold-1",
        "reserved_delta_microusd": -2_000_000,
        "settled_delta_microusd": 1_000_000,
    })
    # Counter reflects that settle (reserved returned, settled recorded).
    TenantBudgetsRepository()._table.update_item(
        Key={"tenant_id": tid, "sk": f"BUDGET#{period}"},
        UpdateExpression="ADD pool_settled_microusd :s",
        ExpressionAttributeValues={":s": 1_000_000},
    )
    client = _make_app(monkeypatch, allow=ADMIN_SCOPES)

    resp = client.get(f"/api/mvp/admin/tenants/{tid}/pool-reconciliation?period={period}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["migrating"] is True, body
    assert body["pre_p2_terminals"] == 1
    # settled is derivable across the boundary and matches → in_sync on settled.
    assert body["settled_drift_microusd"] == 0
    assert body["in_sync"] is True, body
    # The ledger reserved is NOT sunk negative into the derivation (legacy hold
    # excluded): derived reserved is 0, not -2_000_000.
    assert body["ledger_reserved_microusd"] == 0


def test_reconciliation_detects_rating_replay_mismatch(monkeypatch, dynamodb_mock):
    """L5: a frozen rating whose components don't sum to its total (a corrupted /
    tampered rating) is caught by the replay check → rating_replay_ok=False and
    the offending hold is reported, in_sync=False."""
    import json

    from dynamo import CreditLedgerRepository
    from dynamo.tenant_budgets import TenantBudgetsRepository, current_period

    tid = _seed_tenant()
    period = current_period()
    TenantBudgetsRepository().set_pool_limit(
        tenant_id=tid, period=period, pool_limit_microusd=10_000_000_000,
    )
    # Write a SETTLE terminal whose rating.total disagrees with its components.
    bad_rating = {
        "pricing_version": "v1", "pricing_key": "opus", "rounding": "ceil",
        "components": {"input": {"tokens": 1, "rate_microusd_per_mtok": 1, "cost_microusd": 1}},
        "total_cost_microusd": 999,  # LIE: components sum to 1, not 999
    }
    led = CreditLedgerRepository()
    led._table.put_item(Item={
        "pk": f"TENANT#{tid}#P#{period}",
        "sk": "EV#HOLD#tampered#TERMINAL",
        "event_type": "SETTLE", "hold_id": "tampered",
        "reserved_delta_microusd": 0, "settled_delta_microusd": 999,
        "rating": json.dumps(bad_rating),
    })
    client = _make_app(monkeypatch, allow=ADMIN_SCOPES)

    resp = client.get(f"/api/mvp/admin/tenants/{tid}/pool-reconciliation?period={period}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rating_replay_ok"] is False, body
    assert any(m["hold_id"] == "tampered" for m in body["rating_replay_mismatches"])
    assert body["in_sync"] is False


def test_reconciliation_requires_read_all(monkeypatch, dynamodb_mock):
    tid = _seed_tenant()
    _reserve_and_settle(tid, PERIOD, cost=1_000_000, actual=1_000_000)
    client = _make_app(monkeypatch, allow=set())  # no scopes

    resp = client.get(f"/api/mvp/admin/tenants/{tid}/pool-reconciliation?period={PERIOD}")
    assert resp.status_code == 403


def test_reconciliation_404_on_unknown_tenant(monkeypatch, dynamodb_mock):
    client = _make_app(monkeypatch, allow=ADMIN_SCOPES)
    resp = client.get(f"/api/mvp/admin/tenants/nope/pool-reconciliation?period={PERIOD}")
    assert resp.status_code == 404


def test_reconciliation_404_when_no_pool(monkeypatch, dynamodb_mock):
    tid = _seed_tenant()
    client = _make_app(monkeypatch, allow=ADMIN_SCOPES)
    resp = client.get(f"/api/mvp/admin/tenants/{tid}/pool-reconciliation?period={PERIOD}")
    assert resp.status_code == 404
