"""F2 (docs/design/quota-raises.md): R10 + R23.

R10 — a grant is pinned to the row it raised (`target_pk`, `target_sk`,
      `period`), written once at approval time and never recomputed. A grant
      approved in period P and revoked after the calendar has rolled over to
      P+1 must move only P's row.
R23 — a grant can be ended EARLY by the authority that approved it. The
      early revoke moves the pool's three attributes exactly once; a later
      sweep pass over the same (now-terminal) grant is then a no-op.

`dynamo.quota_events` / `mvp.grants` do not exist yet, so every test below
fails today at import.
"""
from __future__ import annotations

import boto3
import pytest
from botocore.exceptions import ClientError

from tests.quota_events_fixtures import (
    quota_events_table,
    seed_grant,
    seed_pool_with_grant_fields,
    seed_tenant,
)

assert quota_events_table  # imported for its pytest-fixture side effect

TENANT = "pin-org"


def _table():
    return boto3.resource("dynamodb", region_name="us-east-1").Table(
        "stratoclave-quota-events")


def test_r10_grant_approved_in_p_and_revoked_in_p_plus_1_moves_only_ps_row(
    dynamodb_mock, quota_events_table,
):
    from dynamo.quota_events import QuotaEventsRepository
    from dynamo.tenant_budgets import TenantBudgetsRepository, budget_sk

    period_p = "2026-08"
    period_p1 = "2026-09"
    # Both periods have a pool row for the tenant — the rollover already
    # happened by the time revoke runs.
    seed_pool_with_grant_fields(TENANT, period_p, pool_limit_microusd=10**9, pool_granted_microusd=400)
    seed_pool_with_grant_fields(TENANT, period_p1, pool_limit_microusd=10**9, pool_granted_microusd=0)

    seed_grant(
        _table(), grant_id="g-pinned", tenant_id=TENANT, request_id="r1",
        approver_user_id="admin-1", approved_amount_microusd=400,
        expires_at_epoch=1_000,
        target_pk=TENANT, target_sk=budget_sk(period_p),  # pinned to P at approval time
        period=period_p, status="ACTIVE",
    )

    # The real revoke flow (`mvp/grants.py::_revoke_txn_items`) is TWO
    # fragments from TWO repositories -- `grant_terminal_txn_item` on
    # `QuotaEventsRepository` (the row the grant IS) and `grant_revoke_txn_item`
    # on `TenantBudgetsRepository` (the row it PINS, per S1's "a pool-row
    # writer lives on the pool row's own repository") -- not one
    # `revoke_grant_txn_items` builder returning both, docs/design/quota-raises.md's
    # original (pre-F1-landing) draft. Both read `target_pk`/`target_sk`
    # straight off the grant row, never `current_period()`, so P's row
    # moves even though "now" is well into P+1.
    events_repo = QuotaEventsRepository()
    budgets_repo = TenantBudgetsRepository()
    txn_items = [
        events_repo.grant_terminal_txn_item(
            tenant_id=TENANT, grant_id="g-pinned", to_status="EXPIRED",
            approved_amount_read=400, revoked_by="sweeper",
            revoked_at="2026-09-15T00:00:00+00:00",
        ),
        budgets_repo.grant_revoke_txn_item(
            target_pk=TENANT, target_sk=budget_sk(period_p),
            approved_amount_microusd=400,
        ),
    ]
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.transact_write_items(TransactItems=txn_items)

    from dynamo.tenant_budgets import TenantBudgetsRepository

    row_p = TenantBudgetsRepository().get(TENANT, period_p, consistent_read=True)
    row_p1 = TenantBudgetsRepository().get(TENANT, period_p1, consistent_read=True)
    assert int(row_p["pool_granted_microusd"]) == 0, "P's row must move"
    assert int(row_p1["pool_granted_microusd"]) == 0, "P+1's row must NOT move"
    assert int(row_p1["pool_limit_microusd"]) == 10**9, "P+1's limit untouched"


def test_r10_grant_row_itself_carries_the_pin_written_at_approval(dynamodb_mock, quota_events_table):
    """The pin is data on the grant, written by `grant_put_txn_item`, not
    derived at revoke time from `current_period()` or any other live
    lookup — asserted directly against the Put item's own Item payload."""
    from dynamo.quota_events import QuotaEventsRepository
    from dynamo.tenant_budgets import budget_sk

    repo = QuotaEventsRepository()
    put_item = repo.grant_put_txn_item(
        tenant_id=TENANT, grant_id="g-x", request_id="req-x",
        approver_user_id="admin-1", approved_amount_microusd=100,
        expires_at_epoch=2_000_000_000, target_pk=TENANT,
        target_sk=budget_sk("2026-08"), period="2026-08",
        created_at="2026-08-15T00:00:00+00:00",
    )
    item = put_item["Put"]["Item"]
    assert item["target_pk"]["S"] == TENANT
    assert item["target_sk"]["S"] == budget_sk("2026-08")
    assert item["period"]["S"] == "2026-08"


# ---------------------------------------------------------------------------
# R23 — early revoke, exactly once, later sweep is a no-op
# ---------------------------------------------------------------------------

def test_r23_early_revoke_moves_the_three_attributes_once(dynamodb_mock, quota_events_table):
    from mvp import grants

    seed_tenant(TENANT, team_lead_user_id="tl-1")
    seed_pool_with_grant_fields(TENANT, "2026-09", pool_limit_microusd=10**9, pool_granted_microusd=300)
    from dynamo.tenant_budgets import budget_sk

    seed_grant(
        _table(), grant_id="g-early", tenant_id=TENANT, request_id="r1",
        approver_user_id="tl-1", approved_amount_microusd=300,
        expires_at_epoch=9_999_999_999,  # far in the future — this is an EARLY end, not an expiry
        target_pk=TENANT, target_sk=budget_sk("2026-09"),
        period="2026-09", status="ACTIVE",
    )
    from mvp.deps import AuthenticatedUser

    actor = AuthenticatedUser(
        user_id="tl-1", email="tl@example.com", org_id="tl-1",
        roles=["team_lead"], raw_claims={}, auth_kind="cognito",
    )
    result = grants.revoke_grant(
        actor=actor, tenant_id=TENANT, grant_id="g-early", reason="no longer needed",
        as_owner=True,  # the route this exercises is the team-lead one (see mvp/grants.py::team_lead_revoke_limit_grant)
    )
    assert result["status"] == "REVOKED"

    from dynamo.tenant_budgets import TenantBudgetsRepository

    row = TenantBudgetsRepository().get(TENANT, "2026-09", consistent_read=True)
    assert int(row["pool_granted_microusd"]) == 0
    assert int(row["pool_limit_microusd"]) == 10**9 - 300
    assert int(row["pool_headroom_microusd"]) == 10**9 - 300


def test_r23_later_sweep_over_an_early_revoked_grant_is_a_no_op(dynamodb_mock, quota_events_table):
    """After an early revoke, the grant is gone from `grant-expiry-index`
    (status is no longer ACTIVE), so a later sweep pass finds nothing to do
    for it — and if it somehow still tried, the grant-status condition
    would refuse a second subtraction anyway (R5's mechanism, reused)."""
    from mvp import grants
    from dynamo.quota_events import QuotaEventsRepository
    from dynamo.tenant_budgets import budget_sk

    seed_tenant(TENANT, team_lead_user_id="tl-1")
    seed_pool_with_grant_fields(TENANT, "2026-09", pool_limit_microusd=10**9, pool_granted_microusd=300)
    seed_grant(
        _table(), grant_id="g-early2", tenant_id=TENANT, request_id="r1",
        approver_user_id="tl-1", approved_amount_microusd=300,
        expires_at_epoch=1_000,  # already "expired" too, to prove the sweep still skips it
        target_pk=TENANT, target_sk=budget_sk("2026-09"),
        period="2026-09", status="ACTIVE",
    )
    from mvp.deps import AuthenticatedUser

    actor = AuthenticatedUser(
        user_id="tl-1", email="tl@example.com", org_id="tl-1",
        roles=["team_lead"], raw_claims={}, auth_kind="cognito",
    )
    grants.revoke_grant(
        actor=actor, tenant_id=TENANT, grant_id="g-early2", reason="done", as_owner=True,
    )

    repo = QuotaEventsRepository()
    expiring, _ = repo.list_active_grants_expiring(now_epoch=5_000, limit=25)
    assert expiring == [], "an early-revoked grant must not appear to the sweeper at all"

    report = grants.sweep_expired_grants(now_epoch=5_000)
    assert report["grants_revoked"] == 0

    from dynamo.tenant_budgets import TenantBudgetsRepository

    row = TenantBudgetsRepository().get(TENANT, "2026-09", consistent_read=True)
    assert int(row["pool_granted_microusd"]) == 0, "sweep must not subtract a second time"
