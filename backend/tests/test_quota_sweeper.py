"""F2 (CONTRACT-F2-grant.md): R4, R5, R9, R19 — the sweeper.

R4  — the sweeper finds expired grants ONLY through the sparse
      `grant-expiry-index` (PK `grant_status`, SK `expires_at`); a grant not
      currently ACTIVE is invisible to it by construction (the PK attribute
      is absent), not filtered out after the fact.
R5  — revocation is exactly-once: two overlapping sweeps on the SAME grant
      revoke it once and subtract the pool once.
R9  — a grant whose revoke keeps failing for a reason OTHER than "already
      revoked" becomes REVOKE_BLOCKED after a bounded number of attempts,
      leaves the index, and is not retried forever.
R19 — `sweeper_ran` is emitted once, after pagination completes; a sweep
      that fails mid-pagination emits no heartbeat at all.

design-F2.md section 3 has the sweeper's exact query and loop shape.
`dynamo.quota_events` / `mvp.grants` do not exist yet, so every test below
fails today at import.
"""
from __future__ import annotations

import logging

import boto3
import pytest
from botocore.exceptions import ClientError

from tests.quota_events_fixtures import (
    quota_events_table,
    seed_grant,
    seed_pool_with_grant_fields,
)

assert quota_events_table  # imported for its pytest-fixture side effect

TENANT = "sweep-org"
PERIOD = "2026-09"


def _table():
    return boto3.resource("dynamodb", region_name="us-east-1").Table(
        "stratoclave-quota-events")


def _target_sk():
    from dynamo.tenant_budgets import budget_sk

    return budget_sk(PERIOD)


# ---------------------------------------------------------------------------
# R4 — the sparse GSI query alone decides visibility
# ---------------------------------------------------------------------------

def test_r4_only_active_grants_appear_a_revoked_one_leaves(dynamodb_mock, quota_events_table):
    from dynamo.quota_events import QuotaEventsRepository

    seed_pool_with_grant_fields(TENANT, PERIOD, pool_limit_microusd=10**9, pool_granted_microusd=300)
    seed_grant(
        _table(), grant_id="g-active-expired", tenant_id=TENANT, request_id="r1",
        approver_user_id="admin-1", approved_amount_microusd=100,
        expires_at_epoch=1_000, target_pk=TENANT, target_sk=_target_sk(),
        period=PERIOD, status="ACTIVE",
    )
    seed_grant(
        _table(), grant_id="g-active-not-yet", tenant_id=TENANT, request_id="r2",
        approver_user_id="admin-1", approved_amount_microusd=100,
        expires_at_epoch=9_999_999_999, target_pk=TENANT, target_sk=_target_sk(),
        period=PERIOD, status="ACTIVE",
    )
    seed_grant(
        _table(), grant_id="g-revoked", tenant_id=TENANT, request_id="r3",
        approver_user_id="admin-1", approved_amount_microusd=100,
        expires_at_epoch=1_000, target_pk=TENANT, target_sk=_target_sk(),
        period=PERIOD, status="REVOKED",   # grant_status attribute NOT written (design-F2.md)
    )

    repo = QuotaEventsRepository()
    items, last_key = repo.list_active_grants_expiring(now_epoch=5_000, limit=25)
    ids = {g["grant_id"] for g in items}
    assert ids == {"g-active-expired"}, (
        f"expected only the expired ACTIVE grant, got {ids} — a REVOKED grant "
        "must be absent because it never wrote (or already removed) "
        "grant_status, not because of a status filter"
    )
    assert last_key is None
    # The GSI projection is exactly what the sweeper needs — no second read.
    only = items[0]
    for key in ("grant_id", "tenant_id", "approved_amount_microusd",
                "target_pk", "target_sk", "period"):
        assert key in only, f"sweeper GSI projection missing {key}"


def test_r4_revoking_removes_the_grant_from_the_index_in_the_same_transaction(
    dynamodb_mock, quota_events_table,
):
    """`revoke_grant_txn_items`' grant-item Update must REMOVE grant_status in
    the SAME transaction that flips status — not a follow-up write — so the
    grant is gone from the index the instant the transaction commits."""
    from dynamo.quota_events import QuotaEventsRepository

    seed_pool_with_grant_fields(TENANT, PERIOD, pool_limit_microusd=10**9, pool_granted_microusd=100)
    seed_grant(
        _table(), grant_id="g-1", tenant_id=TENANT, request_id="r1",
        approver_user_id="admin-1", approved_amount_microusd=100,
        expires_at_epoch=1_000, target_pk=TENANT, target_sk=_target_sk(),
        period=PERIOD, status="ACTIVE",
    )
    repo = QuotaEventsRepository()
    items = repo.revoke_grant_txn_items(
        tenant_id=TENANT, grant_id="g-1", approved_amount_microusd=100,
        target_pk=TENANT, target_sk=_target_sk(), to_status="EXPIRED",
        revoked_by="sweeper", revoked_at="2026-09-02T00:00:00+00:00",
    )
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.transact_write_items(TransactItems=items)

    remaining, _ = repo.list_active_grants_expiring(now_epoch=5_000, limit=25)
    assert remaining == []
    resp = _table().get_item(Key={"pk": f"TENANT#{TENANT}", "sk": "GRANT#g-1"})
    assert "grant_status" not in resp["Item"]
    assert resp["Item"]["status"] == "EXPIRED"


# ---------------------------------------------------------------------------
# R5 — exactly-once, overlapping sweeps
# ---------------------------------------------------------------------------

def test_r5_two_overlapping_sweeps_revoke_once_and_subtract_once(dynamodb_mock, quota_events_table):
    from dynamo.quota_events import QuotaEventsRepository

    seed_pool_with_grant_fields(TENANT, PERIOD, pool_limit_microusd=10**9, pool_granted_microusd=250)
    seed_grant(
        _table(), grant_id="g-race", tenant_id=TENANT, request_id="r1",
        approver_user_id="admin-1", approved_amount_microusd=250,
        expires_at_epoch=1_000, target_pk=TENANT, target_sk=_target_sk(),
        period=PERIOD, status="ACTIVE",
    )
    repo = QuotaEventsRepository()
    txn_items = repo.revoke_grant_txn_items(
        tenant_id=TENANT, grant_id="g-race", approved_amount_microusd=250,
        target_pk=TENANT, target_sk=_target_sk(), to_status="EXPIRED",
        revoked_by="sweeper", revoked_at="2026-09-02T00:00:00+00:00",
    )
    client = boto3.client("dynamodb", region_name="us-east-1")

    # First sweep: commits.
    client.transact_write_items(TransactItems=txn_items)
    # Second, "overlapping" sweep: builds the identical items (as a second,
    # concurrent sweep pass would) and sends them again.
    txn_items_2 = repo.revoke_grant_txn_items(
        tenant_id=TENANT, grant_id="g-race", approved_amount_microusd=250,
        target_pk=TENANT, target_sk=_target_sk(), to_status="EXPIRED",
        revoked_by="sweeper", revoked_at="2026-09-02T00:00:01+00:00",
    )
    with pytest.raises(ClientError) as exc:
        client.transact_write_items(TransactItems=txn_items_2)
    assert exc.value.response["Error"]["Code"] == "TransactionCanceledException"
    reasons = [r.get("Code") for r in exc.value.response.get("CancellationReasons", [])]
    assert "ConditionalCheckFailed" in reasons, (
        "the second, overlapping sweep must fail the grant's own status "
        "condition (already EXPIRED, not ACTIVE) — the exactly-once arbiter"
    )

    from dynamo.tenant_budgets import TenantBudgetsRepository
    row = TenantBudgetsRepository().get(TENANT, PERIOD, consistent_read=True)
    assert int(row["pool_granted_microusd"]) == 0, "subtracted exactly once, not twice"


def test_r5_grant_mutated_between_read_and_write_fails(dynamodb_mock, quota_events_table):
    """A grant whose `approved_amount_microusd` no longer matches the value
    the sweeper read (mutated between read and write) fails the revoke's own
    optimistic-lock condition."""
    from dynamo.quota_events import QuotaEventsRepository

    seed_pool_with_grant_fields(TENANT, PERIOD, pool_limit_microusd=10**9, pool_granted_microusd=999)
    seed_grant(
        _table(), grant_id="g-mut", tenant_id=TENANT, request_id="r1",
        approver_user_id="admin-1", approved_amount_microusd=999,
        expires_at_epoch=1_000, target_pk=TENANT, target_sk=_target_sk(),
        period=PERIOD, status="ACTIVE",
    )
    repo = QuotaEventsRepository()
    # Revoke built against a STALE read of the amount (500, not the live 999).
    stale_items = repo.revoke_grant_txn_items(
        tenant_id=TENANT, grant_id="g-mut", approved_amount_microusd=500,
        target_pk=TENANT, target_sk=_target_sk(), to_status="EXPIRED",
        revoked_by="sweeper", revoked_at="2026-09-02T00:00:00+00:00",
    )
    client = boto3.client("dynamodb", region_name="us-east-1")
    with pytest.raises(ClientError) as exc:
        client.transact_write_items(TransactItems=stale_items)
    assert exc.value.response["Error"]["Code"] == "TransactionCanceledException"


# ---------------------------------------------------------------------------
# R9 — bounded retries, then REVOKE_BLOCKED, not retried forever
# ---------------------------------------------------------------------------

def test_r9_grant_that_cannot_be_revoked_becomes_revoke_blocked_after_bounded_attempts(
    dynamodb_mock, quota_events_table,
):
    """A poisoned grant: its pool row's `pool_granted_microusd` (300) is less
    than the grant's own share (500) — a corruption that makes the revoke's
    POOL-side guard (`pool_granted_microusd >= :G`) fail every single
    attempt, while the grant's OWN status condition (`status = ACTIVE`)
    keeps passing (the transaction cancels as a whole, so status is never
    actually flipped). This is the "some other reason" R9 exists for, as
    opposed to R5's "already revoked".
    """
    from dynamo.quota_events import QuotaEventsRepository

    seed_pool_with_grant_fields(TENANT, PERIOD, pool_limit_microusd=10**9, pool_granted_microusd=300)
    seed_grant(
        _table(), grant_id="g-poison", tenant_id=TENANT, request_id="r1",
        approver_user_id="admin-1", approved_amount_microusd=500,
        expires_at_epoch=1_000, target_pk=TENANT, target_sk=_target_sk(),
        period=PERIOD, status="ACTIVE",
    )
    repo = QuotaEventsRepository()

    for attempt in range(repo.MAX_REVOKE_ATTEMPTS):
        txn_items = repo.revoke_grant_txn_items(
            tenant_id=TENANT, grant_id="g-poison", approved_amount_microusd=500,
            target_pk=TENANT, target_sk=_target_sk(), to_status="EXPIRED",
            revoked_by="sweeper", revoked_at="2026-09-02T00:00:00+00:00",
        )
        client = boto3.client("dynamodb", region_name="us-east-1")
        with pytest.raises(ClientError):
            client.transact_write_items(TransactItems=txn_items)
        bump = repo.bump_revoke_attempts_or_block_txn_item(
            tenant_id=TENANT, grant_id="g-poison", attempts_read=attempt,
            max_attempts=repo.MAX_REVOKE_ATTEMPTS,
        )
        client.transact_write_items(TransactItems=[bump])

    resp = _table().get_item(Key={"pk": f"TENANT#{TENANT}", "sk": "GRANT#g-poison"})
    assert resp["Item"]["status"] == repo.GRANT_REVOKE_BLOCKED
    assert "grant_status" not in resp["Item"], "must leave the sweeper's index — not retried forever"

    remaining, _ = repo.list_active_grants_expiring(now_epoch=5_000, limit=25)
    assert remaining == [], "a REVOKE_BLOCKED grant must not keep showing up on every future sweep"

    from dynamo.tenant_budgets import TenantBudgetsRepository
    row = TenantBudgetsRepository().get(TENANT, PERIOD, consistent_read=True)
    assert int(row["pool_granted_microusd"]) == 300, (
        "blocking a grant must NOT touch the pool — the capacity stays "
        "honestly counted as outstanding until an operator repairs it"
    )


def test_r9_run_sweep_reports_revoke_blocked_grants_metric(dynamodb_mock, quota_events_table, caplog):
    """Integration-level: `run_sweep` itself drives a poisoned grant to
    REVOKE_BLOCKED across repeated ticks and reports it via the
    `revoke_blocked_grants` metric — the sweep run itself must not be failed
    by one poison grant (R9's "does not consume the run")."""
    from mvp import grants

    seed_pool_with_grant_fields(TENANT, PERIOD, pool_limit_microusd=10**9, pool_granted_microusd=300)
    seed_grant(
        _table(), grant_id="g-poison2", tenant_id=TENANT, request_id="r1",
        approver_user_id="admin-1", approved_amount_microusd=500,
        expires_at_epoch=1_000, target_pk=TENANT, target_sk=_target_sk(),
        period=PERIOD, status="ACTIVE",
    )
    caplog.set_level(logging.INFO)
    from dynamo.quota_events import QuotaEventsRepository

    report = None
    for _ in range(QuotaEventsRepository.MAX_REVOKE_ATTEMPTS + 1):
        report = grants.run_sweep(now_epoch=5_000)
    assert report["revoke_blocked_grants"] >= 1
    resp = _table().get_item(Key={"pk": f"TENANT#{TENANT}", "sk": "GRANT#g-poison2"})
    assert resp["Item"]["status"] == "REVOKE_BLOCKED"


# ---------------------------------------------------------------------------
# R19 — sweeper_ran only after pagination completes
# ---------------------------------------------------------------------------

def test_r19_sweeper_ran_emitted_after_full_pagination(dynamodb_mock, quota_events_table, caplog):
    """Two grants, forced onto two pages (Limit=1 semantics would show this
    directly at the repository level; at the `run_sweep` level we assert the
    heartbeat appears exactly once and only after both are processed)."""
    from mvp import grants

    seed_pool_with_grant_fields(TENANT, PERIOD, pool_limit_microusd=10**9, pool_granted_microusd=200)
    for i in range(2):
        seed_grant(
            _table(), grant_id=f"g-page-{i}", tenant_id=TENANT, request_id=f"r{i}",
            approver_user_id="admin-1", approved_amount_microusd=100,
            expires_at_epoch=1_000, target_pk=TENANT, target_sk=_target_sk(),
            period=PERIOD, status="ACTIVE",
        )
    caplog.set_level(logging.INFO)
    grants.run_sweep(now_epoch=5_000)
    heartbeats = [r for r in caplog.records if r.getMessage() == "sweeper_ran"]
    assert len(heartbeats) == 1


def test_r19_mid_pagination_failure_emits_no_heartbeat(dynamodb_mock, quota_events_table, caplog, monkeypatch):
    """A sweep whose SECOND page raises leaves zero `sweeper_ran` records —
    the emit call must sit textually after the pagination loop, not able to
    fire per-page."""
    from dynamo.quota_events import QuotaEventsRepository
    from mvp import grants

    seed_pool_with_grant_fields(TENANT, PERIOD, pool_limit_microusd=10**9, pool_granted_microusd=100)
    seed_grant(
        _table(), grant_id="g-fail-page", tenant_id=TENANT, request_id="r1",
        approver_user_id="admin-1", approved_amount_microusd=100,
        expires_at_epoch=1_000, target_pk=TENANT, target_sk=_target_sk(),
        period=PERIOD, status="ACTIVE",
    )

    real_query = QuotaEventsRepository.list_active_grants_expiring
    calls = {"n": 0}

    def _flaky(self, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_query(self, **kwargs)[0], {"fake": "cursor"}
        raise RuntimeError("simulated mid-pagination failure")

    monkeypatch.setattr(QuotaEventsRepository, "list_active_grants_expiring", _flaky)
    caplog.set_level(logging.INFO)
    with pytest.raises(RuntimeError):
        grants.run_sweep(now_epoch=5_000)
    heartbeats = [r for r in caplog.records if r.getMessage() == "sweeper_ran"]
    assert heartbeats == []
