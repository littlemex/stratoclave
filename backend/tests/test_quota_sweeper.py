"""F2 (docs/design/quota-raises.md): R4, R5, R9, R19 — the sweeper.

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

docs/design/quota-raises.md section 3 has the sweeper's exact query and loop shape.
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
        period=PERIOD, status="REVOKED",   # grant_status attribute NOT written (docs/design/quota-raises.md)
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


def _revoke_items(*, tenant_id, grant_id, approved_amount_microusd, target_pk,
                   target_sk, to_status, revoked_by, revoked_at):
    """The real, two-repository revoke fragment pair
    (`mvp/grants.py::_revoke_txn_items`) — `grant_terminal_txn_item` on
    `QuotaEventsRepository` (the row the grant IS) and `grant_revoke_txn_item`
    on `TenantBudgetsRepository` (the row it PINS). Not a single
    `revoke_grant_txn_items` builder returning both — docs/design/quota-raises.md's
    original, pre-F1-landing draft; see
    `test_quota_grant_pinning_and_revoke.py`'s header for the full reasoning."""
    from dynamo.quota_events import QuotaEventsRepository
    from dynamo.tenant_budgets import TenantBudgetsRepository

    return [
        QuotaEventsRepository().grant_terminal_txn_item(
            tenant_id=tenant_id, grant_id=grant_id, to_status=to_status,
            approved_amount_read=approved_amount_microusd, revoked_by=revoked_by,
            revoked_at=revoked_at,
        ),
        TenantBudgetsRepository().grant_revoke_txn_item(
            target_pk=target_pk, target_sk=target_sk,
            approved_amount_microusd=approved_amount_microusd,
        ),
    ]


def test_r4_revoking_removes_the_grant_from_the_index_in_the_same_transaction(
    dynamodb_mock, quota_events_table,
):
    """The grant-item Update fragment must REMOVE grant_status in the SAME
    transaction that flips status — not a follow-up write — so the grant is
    gone from the index the instant the transaction commits."""
    from dynamo.quota_events import QuotaEventsRepository

    seed_pool_with_grant_fields(TENANT, PERIOD, pool_limit_microusd=10**9, pool_granted_microusd=100)
    seed_grant(
        _table(), grant_id="g-1", tenant_id=TENANT, request_id="r1",
        approver_user_id="admin-1", approved_amount_microusd=100,
        expires_at_epoch=1_000, target_pk=TENANT, target_sk=_target_sk(),
        period=PERIOD, status="ACTIVE",
    )
    repo = QuotaEventsRepository()
    items = _revoke_items(
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
    txn_items = _revoke_items(
        tenant_id=TENANT, grant_id="g-race", approved_amount_microusd=250,
        target_pk=TENANT, target_sk=_target_sk(), to_status="EXPIRED",
        revoked_by="sweeper", revoked_at="2026-09-02T00:00:00+00:00",
    )
    client = boto3.client("dynamodb", region_name="us-east-1")

    # First sweep: commits.
    client.transact_write_items(TransactItems=txn_items)
    # Second, "overlapping" sweep: builds the identical items (as a second,
    # concurrent sweep pass would) and sends them again.
    txn_items_2 = _revoke_items(
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
    # Revoke built against a STALE read of the amount (500, not the live 999).
    stale_items = _revoke_items(
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
    from dynamo.quota_events import GRANT_REVOKE_BLOCKED, MAX_REVOKE_ATTEMPTS, QuotaEventsRepository

    seed_pool_with_grant_fields(TENANT, PERIOD, pool_limit_microusd=10**9, pool_granted_microusd=300)
    seed_grant(
        _table(), grant_id="g-poison", tenant_id=TENANT, request_id="r1",
        approver_user_id="admin-1", approved_amount_microusd=500,
        expires_at_epoch=1_000, target_pk=TENANT, target_sk=_target_sk(),
        period=PERIOD, status="ACTIVE",
    )
    repo = QuotaEventsRepository()
    client = boto3.client("dynamodb", region_name="us-east-1")

    # The real flow (`mvp/grants.py::sweep_expired_grants`): a failed revoke
    # calls `bump_revoke_attempts` (a direct UpdateItem, not a transaction
    # item this test builds and commits itself), and once the returned
    # count reaches the bound, `mark_revoke_blocked` (also direct) takes the
    # grant out of the index. Neither is `bump_revoke_attempts_or_block_txn_item`
    # (docs/design/quota-raises.md's original single-builder draft) — two separate,
    # directly-executing repository methods instead.
    attempts = None
    for _ in range(MAX_REVOKE_ATTEMPTS):
        txn_items = _revoke_items(
            tenant_id=TENANT, grant_id="g-poison", approved_amount_microusd=500,
            target_pk=TENANT, target_sk=_target_sk(), to_status="EXPIRED",
            revoked_by="sweeper", revoked_at="2026-09-02T00:00:00+00:00",
        )
        with pytest.raises(ClientError):
            client.transact_write_items(TransactItems=txn_items)
        attempts = repo.bump_revoke_attempts(tenant_id=TENANT, grant_id="g-poison")
    assert attempts == MAX_REVOKE_ATTEMPTS
    assert repo.mark_revoke_blocked(
        tenant_id=TENANT, grant_id="g-poison", reason="pool_granted_insufficient",
        max_attempts=MAX_REVOKE_ATTEMPTS,
    )

    resp = _table().get_item(Key={"pk": f"TENANT#{TENANT}", "sk": "GRANT#g-poison"})
    assert resp["Item"]["status"] == GRANT_REVOKE_BLOCKED
    assert "grant_status" not in resp["Item"], "must leave the sweeper's index — not retried forever"

    remaining, _ = repo.list_active_grants_expiring(now_epoch=5_000, limit=25)
    assert remaining == [], "a REVOKE_BLOCKED grant must not keep showing up on every future sweep"

    from dynamo.tenant_budgets import TenantBudgetsRepository
    row = TenantBudgetsRepository().get(TENANT, PERIOD, consistent_read=True)
    assert int(row["pool_granted_microusd"]) == 300, (
        "blocking a grant must NOT touch the pool — the capacity stays "
        "honestly counted as outstanding until an operator repairs it"
    )


def _printed_events(capsys) -> list[dict]:
    """`_emit_sweep_metrics` (`mvp/grants.py`) `print()`s EMF JSON lines to
    stdout, deliberately NOT through the stdlib `logging` module (an EMF
    line must be exact JSON on stdout for CloudWatch's log agent to parse
    the `_aws` block; wrapping it in structlog would corrupt that shape) —
    so `caplog` cannot see them at all (docs/design/quota-raises.md's draft assumed
    `logger.info("sweeper_ran")`, which this module never calls). `capsys`
    reads the real channel instead."""
    import json

    out = capsys.readouterr().out
    events = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def test_r9_run_sweep_reports_revoke_blocked_grants_metric(dynamodb_mock, quota_events_table, capsys):
    """Integration-level: `sweep_expired_grants` itself drives a poisoned
    grant to REVOKE_BLOCKED across repeated ticks and reports it via the
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
    from dynamo.quota_events import MAX_REVOKE_ATTEMPTS

    # The block happens on WHICHEVER tick pushes attempts over the bound; the
    # run after that finds nothing left to do for this grant (it has already
    # left the index) and reports 0 new blocks on ITS OWN. Check the reports
    # across every tick, not only the last.
    reports = [grants.sweep_expired_grants(now_epoch=5_000) for _ in range(MAX_REVOKE_ATTEMPTS + 1)]
    assert sum(r["revoke_blocked_grants"] for r in reports) >= 1
    resp = _table().get_item(Key={"pk": f"TENANT#{TENANT}", "sk": "GRANT#g-poison2"})
    assert resp["Item"]["status"] == "REVOKE_BLOCKED"


# ---------------------------------------------------------------------------
# R19 — sweeper_ran only after pagination completes
# ---------------------------------------------------------------------------

def test_r19_sweeper_ran_emitted_after_full_pagination(dynamodb_mock, quota_events_table, capsys):
    """Two grants, forced onto two pages (Limit=1 semantics would show this
    directly at the repository level; at the `sweep_expired_grants` level we
    assert the heartbeat appears exactly once and only after both are
    processed)."""
    from mvp import grants

    seed_pool_with_grant_fields(TENANT, PERIOD, pool_limit_microusd=10**9, pool_granted_microusd=200)
    for i in range(2):
        seed_grant(
            _table(), grant_id=f"g-page-{i}", tenant_id=TENANT, request_id=f"r{i}",
            approver_user_id="admin-1", approved_amount_microusd=100,
            expires_at_epoch=1_000, target_pk=TENANT, target_sk=_target_sk(),
            period=PERIOD, status="ACTIVE",
        )
    capsys.readouterr()
    grants.sweep_expired_grants(now_epoch=5_000)
    heartbeats = [e for e in _printed_events(capsys) if e.get("event") == "sweeper_ran"]
    assert len(heartbeats) == 1


def test_r19_mid_pagination_failure_emits_no_heartbeat(dynamodb_mock, quota_events_table, capsys, monkeypatch):
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
    capsys.readouterr()
    with pytest.raises(RuntimeError):
        grants.sweep_expired_grants(now_epoch=5_000)
    heartbeats = [e for e in _printed_events(capsys) if e.get("event") == "sweeper_ran"]
    assert heartbeats == []
