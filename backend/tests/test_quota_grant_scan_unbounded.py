"""External-review finding (Fable + Codex, independently, same question):
`dynamo.quota_events.QuotaEventsRepository.list_grants_for_tenant` capped
itself at 500 grants and returned silently truncated -- no `truncated` flag,
no exception, nothing distinguishing "this tenant has exactly 500 grants"
from "this tenant has more and 501+ were dropped".

`sk = GRANT#<grant_id>` sorts LEXICOGRAPHICALLY, not by creation time, so the
500 grants a truncated scan keeps are not reliably the tenant's oldest (or
newest) ones -- whichever grant ids happen to sort last are the ones that
disappear.

Three correctness paths built directly on this call, and all three inherited
the cap silently:

  - `mvp.grants.revoke_all_active_grants` -- C14.23's retirement drain. A
    capacity-bearing grant past the cap is neither revoked NOR reported as
    remaining: it is invisible, so retirement proceeds as if nothing were
    left holding capacity.
  - `mvp.grants.reconcile_tenant_grants` -- the drift sum and the orphan hunt
    (C14.19/R34). A grant past the cap is left out of both the per-row sum
    and the orphan scan.
  - `mvp.grants._tenant_grants` -- the reconciler's per-row capacity-bearing
    check, same blind spot.

This file seeds 501 grants for one tenant with the CAPACITY-BEARING grant
placed last in `sk` order (by construction: 500 filler ids that sort before
it), so a truncated-at-500 scan would silently drop exactly it, and asserts
the three correctness paths still see it after the fix
(`QuotaEventsRepository.list_grants_for_tenant` reads to exhaustion; a
bounded, explicitly-truncated page exists separately for the two human-facing
list endpoints as `list_grants_for_tenant_page`).
"""
from __future__ import annotations

import boto3

from dynamo.tenant_budgets import budget_sk, current_period
from mvp.deps import AuthenticatedUser
from tests.quota_events_fixtures import (
    quota_events_table,
    seed_grant,
    seed_pool_with_grant_fields,
    seed_tenant,
)

assert quota_events_table  # imported for its pytest-fixture side effect

TENANT = "many-grants-org"
PERIOD_STATIC = "2026-01"  # used only where the period's identity doesn't matter
FILLER_COUNT = 500
IMPORTANT_GRANT_ID = "zzz-the-501st-grant"  # sorts after every "filler-####" id


def _table():
    return boto3.resource("dynamodb", region_name="us-east-1").Table(
        "stratoclave-quota-events")


def _admin_actor() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="admin-1", email="admin@example.com", org_id="admin-1",
        roles=["admin"], raw_claims={}, auth_kind="cognito",
    )


def _seed_500_filler_grants(*, tenant_id: str, period: str) -> None:
    """500 REVOKED grants -- NOT capacity-bearing, so no pool-side write is
    needed to seed them and no correctness path has any work to do for them.
    Their only job is to occupy the first 500 slots of `sk` order ahead of
    `IMPORTANT_GRANT_ID`."""
    for i in range(FILLER_COUNT):
        seed_grant(
            _table(), grant_id=f"filler-{i:04d}", tenant_id=tenant_id,
            request_id=f"r-filler-{i:04d}", approver_user_id="admin-1",
            approved_amount_microusd=0, expires_at_epoch=1_000,
            target_pk=tenant_id, target_sk=budget_sk(period), period=period,
            status="REVOKED",
        )


def test_list_grants_for_tenant_reads_past_the_old_500_cap(dynamodb_mock, quota_events_table):
    """The storage-layer defect, isolated from any of its callers."""
    from dynamo.quota_events import QuotaEventsRepository

    _seed_500_filler_grants(tenant_id=TENANT, period=PERIOD_STATIC)
    seed_grant(
        _table(), grant_id=IMPORTANT_GRANT_ID, tenant_id=TENANT,
        request_id="r-important", approver_user_id="admin-1",
        approved_amount_microusd=42_000_000, expires_at_epoch=9_999_999_999,
        target_pk=TENANT, target_sk=budget_sk(PERIOD_STATIC), period=PERIOD_STATIC,
        status="ACTIVE",
    )

    grants = QuotaEventsRepository().list_grants_for_tenant(tenant_id=TENANT)

    assert len(grants) == FILLER_COUNT + 1, (
        f"expected all {FILLER_COUNT + 1} grants this tenant holds, got "
        f"{len(grants)} -- a scan that silently stops at 500 is the exact "
        f"defect this test exists to catch"
    )
    assert any(g["grant_id"] == IMPORTANT_GRANT_ID for g in grants), (
        "the capacity-bearing grant placed past the old 500-item cap is "
        "missing from the result"
    )


def test_list_grants_for_tenant_page_says_when_it_truncates(dynamodb_mock, quota_events_table):
    """The bounded, human-facing page (`admin_list_limit_grants` /
    `team_lead_list_limit_grants`) is allowed to cut off -- but must SAY so,
    which is the half of the old behaviour this test pins as new: a silent
    cut is the defect, not having a cap on a screen at all."""
    from dynamo.quota_events import QuotaEventsRepository

    _seed_500_filler_grants(tenant_id=TENANT, period=PERIOD_STATIC)
    seed_grant(
        _table(), grant_id=IMPORTANT_GRANT_ID, tenant_id=TENANT,
        request_id="r-important", approver_user_id="admin-1",
        approved_amount_microusd=42_000_000, expires_at_epoch=9_999_999_999,
        target_pk=TENANT, target_sk=budget_sk(PERIOD_STATIC), period=PERIOD_STATIC,
        status="ACTIVE",
    )

    repo = QuotaEventsRepository()
    page, truncated = repo.list_grants_for_tenant_page(tenant_id=TENANT, limit=500)
    assert len(page) == 500
    assert truncated is True

    # A tenant with exactly `limit` grants and no more: not truncated.
    exact_page, exact_truncated = repo.list_grants_for_tenant_page(
        tenant_id=TENANT, limit=FILLER_COUNT + 1)
    assert len(exact_page) == FILLER_COUNT + 1
    assert exact_truncated is False


def test_reconcile_tenant_grants_sees_a_capacity_bearing_grant_beyond_the_old_cap(
    dynamodb_mock, quota_events_table,
):
    """R34/C14.19: the drift sum and the orphan hunt both start from
    `list_grants_for_tenant`. A grant left off the end of a truncated scan is
    invisible to both -- this seeds the pool row to agree ONLY with the
    important grant's amount, so a truncated scan would report a drift
    (the row says 42M granted; a truncated grant list sums to 0)."""
    from mvp.grants import reconcile_tenant_grants

    period = current_period()
    _seed_500_filler_grants(tenant_id=TENANT, period=period)
    seed_pool_with_grant_fields(
        TENANT, period, pool_limit_microusd=10**9,
        pool_granted_microusd=42_000_000,
    )
    seed_grant(
        _table(), grant_id=IMPORTANT_GRANT_ID, tenant_id=TENANT,
        request_id="r-important", approver_user_id="admin-1",
        approved_amount_microusd=42_000_000, expires_at_epoch=9_999_999_999,
        target_pk=TENANT, target_sk=budget_sk(period), period=period,
        status="ACTIVE",
    )

    report = reconcile_tenant_grants(tenant_id=TENANT, period=period)
    rows = [r for r in report["rows"] if r["period"] == period]
    assert len(rows) == 1, report
    row = rows[0]
    assert IMPORTANT_GRANT_ID in row["grant_ids"], (
        "the capacity-bearing grant beyond the old 500-item cap must be "
        "counted in this row's grant list"
    )
    assert row["capacity_bearing_sum_microusd"] == 42_000_000
    assert row["capacity_bearing_sum_microusd"] == row["pool_granted_microusd"], (
        "with the important grant invisible, this row would show a "
        "42M-microUSD drift that does not exist"
    )
    assert report["clean"] is True


def test_revoke_all_active_grants_revokes_a_grant_beyond_the_old_cap(
    dynamodb_mock, quota_events_table,
):
    """C14.23: a tenant is not retired while any grant still bears capacity.
    Seeds the important grant through the REAL two-table transaction
    (`grant_put_txn_item` + `grant_apply_txn_item`), the same path
    `approve_limit_raise` commits in production, so the pool row and the
    grant row agree from the start -- then asks `revoke_all_active_grants`
    to drain the tenant and checks the grant beyond the old cap was actually
    revoked, not silently skipped."""
    from dynamo.quota_events import QuotaEventsRepository
    from dynamo.tenant_budgets import TenantBudgetsRepository
    from mvp.grants import revoke_all_active_grants

    period = current_period()
    seed_tenant(TENANT)
    _seed_500_filler_grants(tenant_id=TENANT, period=period)

    quota = QuotaEventsRepository()
    budgets = TenantBudgetsRepository()
    budgets.set_manual_limit(
        tenant_id=TENANT, period=period, manual_limit_microusd=10**11)
    target_sk = budget_sk(period)
    quota.transact_write([
        quota.grant_put_txn_item(
            tenant_id=TENANT, grant_id=IMPORTANT_GRANT_ID, request_id="r-important",
            approver_user_id="admin-1", approved_amount_microusd=42_000_000,
            expires_at_epoch=9_999_999_999, target_pk=TENANT, target_sk=target_sk,
            period=period, created_at="2026-09-01T00:00:00Z",
        ),
        budgets.grant_apply_txn_item(
            target_pk=TENANT, target_sk=target_sk,
            approved_amount_microusd=42_000_000,
            cap_minus_amount=10**11 - 42_000_000,
        ),
    ])

    result = revoke_all_active_grants(tenant_id=TENANT, actor=_admin_actor())

    assert IMPORTANT_GRANT_ID in result["revoked"], (
        f"the capacity-bearing grant beyond the old 500-item cap was not "
        f"revoked -- retirement would have proceeded believing nothing was "
        f"left. Full result: {result}"
    )
    assert result["remaining_count"] == 0

    live = quota.get_grant(tenant_id=TENANT, grant_id=IMPORTANT_GRANT_ID)
    assert live is not None and live["status"] == "REVOKED"
    row = budgets.get(TENANT, period)
    assert int(row["pool_granted_microusd"]) == 0, (
        "the revoke must have given the capacity back to the pool row"
    )
