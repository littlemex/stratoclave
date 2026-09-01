"""F2 (CONTRACT-F2-grant.md): R17b, and seam amendment B5 (SEAMS.md S5) — the
"$950" regression test, moved here from F4.

R17b — a set whose figure equals the CURRENT `pool_limit_microusd` while
`pool_granted_microusd > 0` is refused with `409 figure_includes_active_grant`
— the caller's new figure almost certainly means "the baseline I want," and
accepting it verbatim would silently re-base the grant on top of itself
(design-F2.md's R17b row).

B5 — F2 is where the setter ACQUIRES grant-aware semantics in the first
place, so F2 is where the regression guard must live; arriving in F4 (as
originally scoped) would mean F2 could ship the bug and F3 could build
surfaces over it, and the "test" would document a regression instead of
block one. The setter's input (`limit_usd_cents`) means the BASELINE the
caller wants, not the raw total — so the write must compute
`pool_limit_microusd = baseline + pool_granted_microusd` (read fresh),
never overwrite `pool_limit_microusd` with the caller's figure verbatim
while a grant is outstanding. The regression this guards: an admin with a
$1000 baseline and a live $50 grant (pool_limit=$1050) sets a new baseline
of $950 intending the tenant to end up at $950 once the grant is gone. The
BROKEN behaviour overwrites pool_limit to $950 outright — erasing the $50
grant's contribution immediately — and when the grant later expires and its
$50 is subtracted, the tenant is left at $900, not the $950 the admin typed:
money quietly evaporated. `test_b5_950_regression_...` below drives exactly
this sequence end to end (set, then revoke) and fails today because
`set_pool_budget` is not grant-aware at all yet.

Targets the EXISTING `mvp.admin_tenants.set_pool_budget` and
`mvp.team_lead.set_own_pool_budget` — both already exist and both currently
have NO grant-aware behaviour of any kind, so most tests below fail today
against CURRENT code (wrong status code or wrong resulting figure), not an
import error — this id is provable against CURRENT code, unlike most of F2.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import pytest

from dynamo.tenants import TenantsRepository
from mvp.deps import AuthenticatedUser, get_current_user
from tests.quota_events_fixtures import quota_events_table, seed_pool_with_grant_fields

assert quota_events_table  # imported for its pytest-fixture side effect (used by name in B6)

TENANT = "grantguard-org"
PERIOD = "2026-09"


def _patch_authz_allow_all(monkeypatch) -> None:
    from mvp import authz

    monkeypatch.setattr(authz, "user_has_permission", lambda user, scope: True)


def _admin_client(monkeypatch) -> TestClient:
    _patch_authz_allow_all(monkeypatch)
    from mvp.admin_tenants import router

    app = FastAPI()
    app.include_router(router)
    actor = AuthenticatedUser(
        user_id="admin-1", email="admin@example.com", org_id="admin-1",
        roles=["admin"], raw_claims={}, auth_kind="cognito",
    )
    app.dependency_overrides[get_current_user] = lambda: actor
    return TestClient(app)


def _team_lead_client(monkeypatch) -> TestClient:
    _patch_authz_allow_all(monkeypatch)
    from mvp.team_lead import router

    app = FastAPI()
    app.include_router(router)
    actor = AuthenticatedUser(
        user_id="tl-1", email="tl@example.com", org_id="tl-1",
        roles=["team_lead"], raw_claims={}, auth_kind="cognito",
    )
    app.dependency_overrides[get_current_user] = lambda: actor
    return TestClient(app)


def _seed_tenant_with_grant(pool_limit_microusd: int, pool_granted_microusd: int) -> None:
    TenantsRepository().create(
        tenant_id=TENANT, name="Grant Guard Co", team_lead_user_id="tl-1", created_by="tl-1",
    )
    seed_pool_with_grant_fields(
        TENANT, PERIOD, pool_limit_microusd=pool_limit_microusd,
        pool_granted_microusd=pool_granted_microusd, grant_cap_microusd=pool_limit_microusd,
    )


def test_r17b_admin_set_pool_budget_refuses_exact_match_with_active_grant(
    monkeypatch, dynamodb_mock,
):
    # pool_limit_microusd = 500_000_000 == 50000 cents; pool_granted = 3M (active grant)
    _seed_tenant_with_grant(pool_limit_microusd=500_000_000, pool_granted_microusd=3_000_000)
    client = _admin_client(monkeypatch)

    resp = client.put(
        f"/api/mvp/admin/tenants/{TENANT}/pool-budget",
        json={"limit_usd_cents": 50_000, "period": PERIOD},  # == the CURRENT figure, verbatim
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    detail = body.get("detail", body)
    assert detail.get("type") == "figure_includes_active_grant", detail


def test_r17b_admin_set_pool_budget_admits_a_genuinely_different_figure_with_active_grant(
    monkeypatch, dynamodb_mock,
):
    """The guard is about EQUALITY specifically, not "refuse whenever a grant
    is active" — a deliberately different new baseline must still go
    through, grant or no grant. B5: the resulting `pool_limit_microusd` is
    now GRANT-AWARE — the caller's figure is the new BASELINE ($800), and the
    still-active $3 grant's contribution is added on top ($803), never
    silently dropped."""
    _seed_tenant_with_grant(pool_limit_microusd=500_000_000, pool_granted_microusd=3_000_000)
    client = _admin_client(monkeypatch)

    resp = client.put(
        f"/api/mvp/admin/tenants/{TENANT}/pool-budget",
        json={"limit_usd_cents": 80_000, "period": PERIOD},  # a real baseline change
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["pool_limit_microusd"] == 803_000_000, (
        "the write must be baseline + still-active pool_granted_microusd "
        "(800_000_000 + 3_000_000), not the caller's raw figure verbatim — "
        "B5's grant-aware setter semantics"
    )


def test_r17b_admin_set_pool_budget_admits_exact_match_when_no_active_grant(
    monkeypatch, dynamodb_mock,
):
    """Same figure, but pool_granted_microusd == 0 — nothing to re-base onto,
    so the write is ordinary and must succeed."""
    _seed_tenant_with_grant(pool_limit_microusd=500_000_000, pool_granted_microusd=0)
    client = _admin_client(monkeypatch)

    resp = client.put(
        f"/api/mvp/admin/tenants/{TENANT}/pool-budget",
        json={"limit_usd_cents": 50_000, "period": PERIOD},
    )
    assert resp.status_code == 200, resp.text


def test_r17b_team_lead_set_own_pool_budget_refuses_exact_match_with_active_grant(
    monkeypatch, dynamodb_mock,
):
    _seed_tenant_with_grant(pool_limit_microusd=400_000_000, pool_granted_microusd=1_000_000)
    client = _team_lead_client(monkeypatch)

    resp = client.put(
        f"/api/mvp/team-lead/tenants/{TENANT}/pool-budget",
        json={"limit_usd_cents": 40_000, "period": PERIOD},
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json().get("detail", {})
    assert detail.get("type") == "figure_includes_active_grant", detail


# ---------------------------------------------------------------------------
# B5 — the $950 regression, end to end: set a new baseline while a grant is
# live, then let the grant expire, and confirm nothing evaporated.
# ---------------------------------------------------------------------------

def test_b5_950_regression_setting_a_new_baseline_survives_the_grants_later_expiry(
    monkeypatch, dynamodb_mock,
):
    """$1000 baseline, a live $50 grant (pool_limit=$1050). Admin sets a new
    baseline of $950. Immediately after the set, the tenant must be at
    $950 (baseline) + $50 (still-active grant) = $1000 — NOT $950 flat
    (that would erase the grant right now). Once the grant later expires and
    is revoked (-$50), the tenant must land at EXACTLY $950 — the admin's
    intended figure — not $900 (which is what dropping the grant's
    contribution at set-time, then subtracting it again at revoke-time,
    would produce: money quietly evaporating twice)."""
    from dynamo.tenant_budgets import TenantBudgetsRepository, budget_sk

    TenantsRepository().create(
        tenant_id=TENANT, name="Nine Fifty Co", team_lead_user_id="tl-1", created_by="tl-1",
    )
    seed_pool_with_grant_fields(
        TENANT, PERIOD, pool_limit_microusd=1_050_000_000, pool_granted_microusd=50_000_000,
    )
    client = _admin_client(monkeypatch)

    resp = client.put(
        f"/api/mvp/admin/tenants/{TENANT}/pool-budget",
        json={"limit_usd_cents": 95_000, "period": PERIOD},  # admin intends baseline = $950
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["pool_limit_microusd"] == 1_000_000_000, (
        "right after the set: $950 baseline + the still-active $50 grant = $1000"
    )

    # The grant now expires and is revoked (the sweeper's own mechanism,
    # exercised directly here since this file is about the setter, not the
    # sweeper).
    from dynamo.quota_events import QuotaEventsRepository

    repo = QuotaEventsRepository()
    txn_items = repo.revoke_grant_txn_items(
        tenant_id=TENANT, grant_id="g-950", approved_amount_microusd=50_000_000,
        target_pk=TENANT, target_sk=budget_sk(PERIOD), to_status="EXPIRED",
        revoked_by="sweeper", revoked_at="2026-09-02T00:00:00+00:00",
    )
    import boto3

    boto3.client("dynamodb", region_name="us-east-1").transact_write_items(TransactItems=txn_items)

    row = TenantBudgetsRepository().get(TENANT, PERIOD, consistent_read=True)
    assert int(row["pool_limit_microusd"]) == 950_000_000, (
        "the admin's $950 must survive the grant's later expiry exactly — "
        "not $900, which is the $950 regression"
    )


# ---------------------------------------------------------------------------
# B6 (R28) — the suspended-pool refusal moves here from F3.
# ---------------------------------------------------------------------------

def test_b6_r28_approval_refuses_to_apply_a_grant_to_a_suspended_pool(
    dynamodb_mock, quota_events_table,
):
    """An approval must not apply a grant to a pool whose `status` is
    "suspended" — such a grant ticks toward expiry delivering nothing while
    consuming the tenant's cap headroom for the whole F2-to-F3 window this
    amendment exists to close."""
    from mvp import grants

    TenantsRepository().create(
        tenant_id=TENANT, name="Suspended Co", team_lead_user_id="tl-1", created_by="tl-1",
    )
    seed_pool_with_grant_fields(
        TENANT, PERIOD, pool_limit_microusd=10**9, pool_granted_microusd=0,
        grant_cap_microusd=10**8,
    )
    from dynamo.tenant_budgets import TenantBudgetsRepository, budget_sk

    TenantBudgetsRepository()._table.update_item(
        Key={"tenant_id": TENANT, "sk": budget_sk(PERIOD)},
        UpdateExpression="SET #st = :s",
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={":s": "suspended"},
    )
    from tests.quota_events_fixtures import seed_request

    seed_request(
        boto3_table(), request_id="req-suspended", tenant_id=TENANT, user_id="u1",
        requested_amount_microusd=1_000, requested_expires_at=9_999_999_999,
    )
    admin = AuthenticatedUser(
        user_id="admin-1", email="admin@example.com", org_id="admin-1",
        roles=["admin"], raw_claims={}, auth_kind="cognito",
    )
    with pytest.raises(grants.PoolSuspended):
        grants.approve_limit_raise(
            actor=admin, request_id="req-suspended", approved_amount_microusd=1_000,
            expires_at=1_788_307_500, now_epoch=1_788_307_200,
        )

    row = TenantBudgetsRepository().get(TENANT, PERIOD, consistent_read=True)
    assert int(row["pool_granted_microusd"]) == 0, "no capacity may be consumed on a refused approval"


def boto3_table():
    import boto3

    return boto3.resource("dynamodb", region_name="us-east-1").Table("stratoclave-quota-events")
