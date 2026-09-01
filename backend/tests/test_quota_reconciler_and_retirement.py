"""F2 (CONTRACT-F2-grant.md): R8b + R34, and seam amendment B8 (SEAMS.md S6).

R8b — lateness is measured over grants READ AND STILL ACTIVE; a
      REVOKE_BLOCKED grant stays in its OWN denominator for THAT purpose
      (R9's alarm), never inflating (or shrinking) every OTHER grant's
      lateness stat. `reconcile_tenant_grants` reports this figure
      separately as `active_only_sum_microusd`.

B8 — "capacity-bearing" is ONE shared predicate (owned here, consumed by
      F1's reconciler and F3's inventory): a grant still holds its share of
      `pool_granted_microusd` iff its status is ACTIVE **or** REVOKE_BLOCKED
      — a blocked grant's revoke failed, so its money was never subtracted;
      it bears capacity right up until a repair subtracts it. Reconciling
      against `pool_granted_microusd` for DRIFT (as opposed to R8b's
      lateness-only denominator above) must therefore sum ACTIVE +
      REVOKE_BLOCKED, or a blocked grant shows as permanent, unexplained
      drift purely because it exists — which is not a defect, it already has
      its own alarm (R9). This is why `test_r8b_reconciler_sums_only_active_...`
      below is CORRECTED to seed a realistic `pool_granted_microusd` that
      includes the blocked grant's amount, rather than the earlier,
      inconsistent seeding that silently assumed a blocked grant's money had
      already left the counter it never left.

      Reconciliation is **per target row** (`target_pk`/`target_sk`), not per
      tenant: a late sweep can leave an expired-but-unrevoked grant still
      pinned to the PRIOR period's row after rollover, so a tenant-wide sum
      against only the CURRENT period's row would miss it (SEAMS.md S6).

R34 — tenant retirement revokes ACTIVE grants first; deletion is refused
      while any remain; the reconciler finds orphans STARTING FROM GRANTS
      (a grant whose pool row is missing is reported), not by scanning pool
      rows and having no way to notice a dangling grant.

`archive_tenant` (mvp.admin_tenants) ALREADY EXISTS and today unconditionally
archives regardless of live grants — so its half of R34 is directly
falsifiable against current code (204, not 409), unlike most of F2's ids.
`mvp.grants` does not exist at all, so its tests fail at import.
"""
from __future__ import annotations

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dynamo.tenant_budgets import budget_sk
from dynamo.tenants import TenantsRepository
from mvp.deps import AuthenticatedUser, get_current_user
from tests.quota_events_fixtures import (
    quota_events_table,
    seed_grant,
    seed_pool_with_grant_fields,
)

assert quota_events_table  # imported for its pytest-fixture side effect

TENANT = "reconcile-org"
PERIOD = "2026-09"


def _table():
    return boto3.resource("dynamodb", region_name="us-east-1").Table(
        "stratoclave-quota-events")


def _admin_actor():
    return AuthenticatedUser(
        user_id="admin-1", email="admin@example.com", org_id="admin-1",
        roles=["admin"], raw_claims={}, auth_kind="cognito",
    )


# ---------------------------------------------------------------------------
# R8b — the reconciler sums ACTIVE grants; a blocked grant stays in its own
# denominator
# ---------------------------------------------------------------------------

def test_r8b_active_only_sum_excludes_revoke_blocked_its_own_denominator(
    dynamodb_mock, quota_events_table,
):
    """R8b's own figure (lateness/denominator purposes): sum of grants whose
    status is ACTIVE only. Seeded REALISTICALLY per B8/B2's own finding — a
    blocked grant's money was never subtracted, so `pool_granted_microusd`
    (950) legitimately includes it; only `active_only_sum_microusd` (700)
    excludes it."""
    from mvp import grants

    seed_pool_with_grant_fields(
        TENANT, PERIOD, pool_limit_microusd=10**9,
        pool_granted_microusd=950, grant_cap_microusd=10**8,
    )
    seed_grant(
        _table(), grant_id="g-active-1", tenant_id=TENANT, request_id="r1",
        approver_user_id="admin-1", approved_amount_microusd=300,
        expires_at_epoch=9_999_999_999, target_pk=TENANT, target_sk=budget_sk(PERIOD),
        period=PERIOD, status="ACTIVE",
    )
    seed_grant(
        _table(), grant_id="g-active-2", tenant_id=TENANT, request_id="r2",
        approver_user_id="admin-1", approved_amount_microusd=400,
        expires_at_epoch=9_999_999_999, target_pk=TENANT, target_sk=budget_sk(PERIOD),
        period=PERIOD, status="ACTIVE",
    )
    seed_grant(
        _table(), grant_id="g-blocked", tenant_id=TENANT, request_id="r3",
        approver_user_id="admin-1", approved_amount_microusd=250,
        expires_at_epoch=1_000, target_pk=TENANT, target_sk=budget_sk(PERIOD),
        period=PERIOD, status="REVOKE_BLOCKED",
    )
    report = grants.reconcile_tenant_grants(tenant_id=TENANT, period=PERIOD)
    assert report["active_only_sum_microusd"] == 700, (
        "the blocked grant's 250 must not be counted in the ACTIVE-only "
        "figure — it is 300+400, not 950"
    )


def test_b8_capacity_bearing_sum_includes_revoke_blocked_so_drift_is_zero(
    dynamodb_mock, quota_events_table,
):
    """B8: the DRIFT comparison against `pool_granted_microusd` must use the
    CAPACITY-BEARING sum (ACTIVE + REVOKE_BLOCKED = 950), not the
    ACTIVE-only figure (700) — otherwise a blocked grant would show as
    permanent, unexplained drift purely because it exists, which is the
    wrong alarm (R9 already owns that one)."""
    from mvp import grants

    seed_pool_with_grant_fields(
        TENANT, PERIOD, pool_limit_microusd=10**9,
        pool_granted_microusd=950, grant_cap_microusd=10**8,
    )
    seed_grant(
        _table(), grant_id="g-active-1", tenant_id=TENANT, request_id="r1",
        approver_user_id="admin-1", approved_amount_microusd=300,
        expires_at_epoch=9_999_999_999, target_pk=TENANT, target_sk=budget_sk(PERIOD),
        period=PERIOD, status="ACTIVE",
    )
    seed_grant(
        _table(), grant_id="g-active-2", tenant_id=TENANT, request_id="r2",
        approver_user_id="admin-1", approved_amount_microusd=400,
        expires_at_epoch=9_999_999_999, target_pk=TENANT, target_sk=budget_sk(PERIOD),
        period=PERIOD, status="ACTIVE",
    )
    seed_grant(
        _table(), grant_id="g-blocked", tenant_id=TENANT, request_id="r3",
        approver_user_id="admin-1", approved_amount_microusd=250,
        expires_at_epoch=1_000, target_pk=TENANT, target_sk=budget_sk(PERIOD),
        period=PERIOD, status="REVOKE_BLOCKED",
    )
    report = grants.reconcile_tenant_grants(tenant_id=TENANT, period=PERIOD)
    assert report["capacity_bearing_sum_microusd"] == 950
    assert report["pool_granted_microusd"] == 950
    assert report["drift_microusd"] == 0


def test_b8_is_capacity_bearing_is_one_shared_predicate(dynamodb_mock, quota_events_table):
    """The predicate itself, directly: True for ACTIVE and REVOKE_BLOCKED,
    False for the two terminal states."""
    from dynamo.quota_events import QuotaEventsRepository

    assert QuotaEventsRepository.is_capacity_bearing("ACTIVE") is True
    assert QuotaEventsRepository.is_capacity_bearing("REVOKE_BLOCKED") is True
    assert QuotaEventsRepository.is_capacity_bearing("EXPIRED") is False
    assert QuotaEventsRepository.is_capacity_bearing("REVOKED") is False


def test_b8_reconciliation_is_per_target_row_a_late_swept_grant_is_seen_on_the_prior_period(
    dynamodb_mock, quota_events_table,
):
    """SEAMS.md S6's exact scenario: rollover has happened (a "2026-10" row
    now exists), but a grant pinned to "2026-09" is still ACTIVE (the sweep
    has not yet caught its expiry). Reconciling "2026-09" specifically must
    still see it — the whole point of per-TARGET-ROW reconciliation rather
    than "the tenant's current period" reconciliation, which would look at
    2026-10 and find nothing wrong there while 2026-09 silently drifts."""
    from mvp import grants

    seed_pool_with_grant_fields(
        TENANT, "2026-09", pool_limit_microusd=10**9, pool_granted_microusd=500,
    )
    seed_pool_with_grant_fields(
        TENANT, "2026-10", pool_limit_microusd=10**9, pool_granted_microusd=0,
    )
    seed_grant(
        _table(), grant_id="g-late", tenant_id=TENANT, request_id="r1",
        approver_user_id="admin-1", approved_amount_microusd=500,
        expires_at_epoch=1_000,  # long past — the sweeper has not caught it yet
        target_pk=TENANT, target_sk=budget_sk("2026-09"),
        period="2026-09", status="ACTIVE",
    )
    report_sep = grants.reconcile_tenant_grants(tenant_id=TENANT, period="2026-09")
    assert report_sep["capacity_bearing_sum_microusd"] == 500
    assert report_sep["drift_microusd"] == 0

    report_oct = grants.reconcile_tenant_grants(tenant_id=TENANT, period="2026-10")
    assert report_oct["capacity_bearing_sum_microusd"] == 0
    assert report_oct["drift_microusd"] == 0


def test_r8b_reconciler_reports_grant_cap(dynamodb_mock, quota_events_table):
    from mvp import grants

    seed_pool_with_grant_fields(
        TENANT, PERIOD, pool_limit_microusd=10**9,
        pool_granted_microusd=0, grant_cap_microusd=555,
    )
    report = grants.reconcile_tenant_grants(tenant_id=TENANT, period=PERIOD)
    assert report["grant_cap_microusd"] == 555
    assert report["effective_cap_microusd"] == 555


def test_b1_reconciler_reports_effective_cap_derived_from_baseline_when_absent(
    dynamodb_mock, quota_events_table,
):
    """B1: an uncapped row (`grant_cap_microusd` absent) reports
    `grant_cap_microusd: None` (honest about what is actually stored) AND
    `effective_cap_microusd` derived live from the baseline — never a
    silently materialised figure."""
    from mvp import grants

    seed_pool_with_grant_fields(
        TENANT, PERIOD, pool_limit_microusd=10**9, pool_granted_microusd=100,
    )
    report = grants.reconcile_tenant_grants(tenant_id=TENANT, period=PERIOD)
    assert report["grant_cap_microusd"] is None
    assert report["effective_cap_microusd"] == 10**9 - 100


# ---------------------------------------------------------------------------
# R34 — orphans starting from grants
# ---------------------------------------------------------------------------

def test_r34_reconciler_reports_a_grant_whose_pool_row_is_missing(dynamodb_mock, quota_events_table):
    """The orphan check walks grants first: a grant pinned to a
    (target_pk, target_sk) that no longer resolves to a TenantBudgets row
    (e.g. the row was deleted out of band) must be reported, not silently
    dropped because nothing started FROM the pool side to notice it."""
    from mvp import grants

    seed_pool_with_grant_fields(
        TENANT, PERIOD, pool_limit_microusd=10**9,
        pool_granted_microusd=0, grant_cap_microusd=10**8,
    )
    seed_grant(
        _table(), grant_id="g-orphan", tenant_id=TENANT, request_id="r1",
        approver_user_id="admin-1", approved_amount_microusd=100,
        expires_at_epoch=9_999_999_999,
        target_pk=TENANT, target_sk=budget_sk("2099-01"),  # a period with NO pool row
        period="2099-01", status="ACTIVE",
    )
    report = grants.reconcile_tenant_grants(tenant_id=TENANT, period=PERIOD)
    assert "g-orphan" in report["orphan_grant_ids"]


# ---------------------------------------------------------------------------
# R34 — retirement revokes ACTIVE grants first; deletion refused while any remain
# ---------------------------------------------------------------------------

def _delete_client(monkeypatch) -> TestClient:
    from mvp import authz
    from mvp.admin_tenants import router

    monkeypatch.setattr(authz, "user_has_permission", lambda user, scope: True)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: _admin_actor()
    return TestClient(app)


def test_r34_archive_tenant_refused_while_active_grants_remain(
    dynamodb_mock, quota_events_table, monkeypatch,
):
    """`DELETE /admin/tenants/{id}` (mvp.admin_tenants.archive_tenant)
    ALREADY EXISTS and today calls `repo.archive(tenant_id)` unconditionally
    — no grant check at all. Seeding one ACTIVE grant and expecting the
    existing route to refuse with 409 is directly falsifiable against
    current code (it returns 204 today)."""
    TenantsRepository().create(
        tenant_id=TENANT, name="Reconcile Co", team_lead_user_id="tl-1", created_by="tl-1",
    )
    seed_pool_with_grant_fields(
        TENANT, PERIOD, pool_limit_microusd=10**9,
        pool_granted_microusd=200, grant_cap_microusd=10**8,
    )
    seed_grant(
        _table(), grant_id="g-still-live", tenant_id=TENANT, request_id="r1",
        approver_user_id="admin-1", approved_amount_microusd=200,
        expires_at_epoch=9_999_999_999, target_pk=TENANT, target_sk=budget_sk(PERIOD),
        period=PERIOD, status="ACTIVE",
    )
    client = _delete_client(monkeypatch)
    resp = client.delete(f"/api/mvp/admin/tenants/{TENANT}")
    assert resp.status_code == 409, resp.text
    detail = resp.json().get("detail", {})
    assert detail.get("type") == "active_grants_remain", detail


def test_r34_archive_tenant_succeeds_once_grants_are_revocable_and_drained(
    dynamodb_mock, quota_events_table, monkeypatch,
):
    """The positive control: an ordinary (revocable) ACTIVE grant is drained
    by `revoke_all_active_grants` as part of the SAME delete call, and the
    archive then proceeds."""
    TenantsRepository().create(
        tenant_id=TENANT, name="Reconcile Co", team_lead_user_id="tl-1", created_by="tl-1",
    )
    seed_pool_with_grant_fields(
        TENANT, PERIOD, pool_limit_microusd=10**9,
        pool_granted_microusd=150, grant_cap_microusd=10**8,
    )
    seed_grant(
        _table(), grant_id="g-drainable", tenant_id=TENANT, request_id="r1",
        approver_user_id="admin-1", approved_amount_microusd=150,
        expires_at_epoch=9_999_999_999, target_pk=TENANT, target_sk=budget_sk(PERIOD),
        period=PERIOD, status="ACTIVE",
    )
    client = _delete_client(monkeypatch)
    resp = client.delete(f"/api/mvp/admin/tenants/{TENANT}")
    assert resp.status_code == 204, resp.text

    from dynamo.tenant_budgets import TenantBudgetsRepository

    row = TenantBudgetsRepository().get(TENANT, PERIOD, consistent_read=True)
    assert int(row["pool_granted_microusd"]) == 0
    resp2 = _table().get_item(Key={"pk": f"TENANT#{TENANT}", "sk": "GRANT#g-drainable"})
    assert resp2["Item"]["status"] in ("REVOKED", "EXPIRED")


def test_r34_revoke_all_active_grants_directly(dynamodb_mock, quota_events_table):
    from mvp import grants

    seed_pool_with_grant_fields(
        TENANT, PERIOD, pool_limit_microusd=10**9,
        pool_granted_microusd=90, grant_cap_microusd=10**8,
    )
    seed_grant(
        _table(), grant_id="g-a", tenant_id=TENANT, request_id="r1",
        approver_user_id="admin-1", approved_amount_microusd=90,
        expires_at_epoch=9_999_999_999, target_pk=TENANT, target_sk=budget_sk(PERIOD),
        period=PERIOD, status="ACTIVE",
    )
    result = grants.revoke_all_active_grants(tenant_id=TENANT, actor=_admin_actor())
    assert result["remaining_active"] == 0
