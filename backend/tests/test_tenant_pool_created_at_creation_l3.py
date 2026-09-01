"""L3 (docs/design/limits.md (C14)): a tenant gets a default dollar pool at
creation.

Spec, from the Interface section only:

    "Tenant pool at creation. `create_tenant` (both the admin and the
    team-lead route) writes a BUDGET row for the current period:
    `pool_limit_microusd = seats x SEAT_MONTHLY_USD x 1e6`, `sizing =
    "per_seat"`. `seats` is the membership count at creation (1 for a fresh
    tenant with only its owner)."

    STRATOCLAVE_SEAT_MONTHLY_USD   default 200   "The tenant pool's per-seat
    monthly figure. Drives the pool at creation and its membership deltas."

Today NEITHER `mvp.admin_tenants.create_tenant` NOR `mvp.team_lead.create_tenant`
writes anything to the TenantBudgets table at all — `TenantBudgetsRepository()
.pool_summary()` returns `None` for a tenant created through either route. Every
assertion below fails today for that reason.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dynamo import TenantBudgetsRepository, current_period
from mvp.deps import AuthenticatedUser, get_current_user

_MICRO_USD_PER_USD = 1_000_000


def _patch_authz_allow_all(monkeypatch) -> None:
    """Admission through `require_permission` is not what L3 is about — the
    pool write is. Allow every scope so a permission-name choice the
    interface does not specify cannot make this test pass or fail for the
    wrong reason."""
    from mvp import authz

    monkeypatch.setattr(authz, "user_has_permission", lambda user, scope: True)


def _admin_actor() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="admin-1", email="admin@example", org_id="default-org",
        roles=["admin"], raw_claims={}, auth_kind="cognito",
    )


def _team_lead_actor(user_id: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id, email="lead@example", org_id="default-org",
        roles=["team_lead"], raw_claims={}, auth_kind="cognito",
    )


def test_admin_create_tenant_writes_a_one_seat_pool(monkeypatch, dynamodb_mock):
    _patch_authz_allow_all(monkeypatch)
    from mvp.admin_tenants import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = _admin_actor
    client = TestClient(app)

    resp = client.post(
        "/api/mvp/admin/tenants",
        json={"name": "Acme Eng", "team_lead_user_id": "admin-owned"},
    )
    assert resp.status_code == 201, resp.text
    tenant_id = resp.json()["tenant_id"]

    summary = TenantBudgetsRepository().pool_summary(tenant_id, current_period())
    assert summary is not None, "no BUDGET row was written at tenant creation"
    # ZERO at creation, and `per_seat`, so the ceiling equals the seat count at
    # every moment. Writing one seat here would count the owner twice: a fresh
    # tenant has no memberships yet, and the first `ensure` adds its own seat.
    assert summary["pool_limit_microusd"] == 0
    assert summary["remaining_microusd"] == 0
    assert summary["sizing"] == "per_seat"

    # And the first membership is what brings it to exactly one seat.
    from dynamo.user_tenants import UserTenantsRepository

    UserTenantsRepository().ensure(user_id="first-member", tenant_id=tenant_id, role="user")
    grown = TenantBudgetsRepository().pool_summary(tenant_id, current_period())
    assert grown["pool_limit_microusd"] == 1 * 200 * _MICRO_USD_PER_USD

    raw = TenantBudgetsRepository().get(tenant_id, current_period())
    assert raw.get("sizing") == "per_seat"


def test_team_lead_create_tenant_writes_a_one_seat_pool(monkeypatch, dynamodb_mock):
    _patch_authz_allow_all(monkeypatch)
    from mvp.team_lead import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: _team_lead_actor("tl-1")
    client = TestClient(app)

    resp = client.post("/api/mvp/team-lead/tenants", json={"name": "Owned Co"})
    assert resp.status_code == 201, resp.text
    tenant_id = resp.json()["tenant_id"]

    summary = TenantBudgetsRepository().pool_summary(tenant_id, current_period())
    assert summary is not None, "the team-lead create route wrote no BUDGET row"
    assert summary["pool_limit_microusd"] == 0
    assert summary["sizing"] == "per_seat"

    raw = TenantBudgetsRepository().get(tenant_id, current_period())
    assert raw.get("sizing") == "per_seat"


def test_seat_monthly_usd_env_var_scales_the_default_pool(monkeypatch, dynamodb_mock):
    """The figure must be read from `STRATOCLAVE_SEAT_MONTHLY_USD`, not a second
    hardcoded 200 — an operator override must reach the pool the same creation
    call writes."""
    monkeypatch.setenv("STRATOCLAVE_SEAT_MONTHLY_USD", "50")
    _patch_authz_allow_all(monkeypatch)
    from mvp.admin_tenants import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = _admin_actor
    client = TestClient(app)

    resp = client.post(
        "/api/mvp/admin/tenants",
        json={"name": "Cheap Seats Co", "team_lead_user_id": "admin-owned"},
    )
    assert resp.status_code == 201, resp.text
    tenant_id = resp.json()["tenant_id"]

    summary = TenantBudgetsRepository().pool_summary(tenant_id, current_period())
    assert summary is not None
    # The knob drives the DELTA, not a figure written at creation, so its effect
    # is observed on the first membership rather than on the BUDGET row itself.
    assert summary["pool_limit_microusd"] == 0

    from dynamo.user_tenants import UserTenantsRepository

    UserTenantsRepository().ensure(user_id="cheap-member", tenant_id=tenant_id, role="user")
    grown = TenantBudgetsRepository().pool_summary(tenant_id, current_period())
    assert grown["pool_limit_microusd"] == 1 * 50 * _MICRO_USD_PER_USD
