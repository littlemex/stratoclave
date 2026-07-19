"""Tests for the pool_headroom backfill / reconcile migration.

Migration step 1 of docs/design/ledger-hot-path.md. The backfill delegates to
`TenantBudgetsRepository.reconcile_headroom`, which VALUE-repairs pool_headroom to
`limit - reserved - settled` under a race-safe CAS (no raw counter write). These
tests prove:

  - a legacy row (no pool_headroom) is reconciled to the exact invariant value;
  - reserved/settled are preserved (the ceiling is not reset);
  - the job is idempotent (a re-run touches nothing);
  - the mixed-deploy regression (Fable review finding 2): a row whose headroom
    was CREATED at a wrong value by a new-code settle firing before backfill is
    REPAIRED, not skipped forever;
  - a genuinely over-reserved row (invariant < 0) is reported and reconciled to
    the true negative (gate keeps refusing), never left corrupt.
"""
from __future__ import annotations

from decimal import Decimal

from dynamo.tenant_budgets import TenantBudgetsRepository, budget_sk, current_period
from migrations.backfill_pool_headroom import backfill


def _strip_headroom(tenant_id: str, period: str) -> None:
    """Simulate a pre-migration row: remove the pool_headroom attribute that the
    new write paths maintain, leaving limit/reserved/settled intact."""
    TenantBudgetsRepository()._table.update_item(
        Key={"tenant_id": tenant_id, "sk": budget_sk(period)},
        UpdateExpression="REMOVE pool_headroom_microusd",
    )


def _set_headroom(tenant_id: str, period: str, value: int) -> None:
    """Force a specific (possibly wrong) headroom, e.g. what a mixed-window
    settle would have CREATED via its unconditional ADD."""
    TenantBudgetsRepository()._table.update_item(
        Key={"tenant_id": tenant_id, "sk": budget_sk(period)},
        UpdateExpression="SET pool_headroom_microusd = :v",
        ExpressionAttributeValues={":v": Decimal(value)},
    )


def _raw(tenant_id: str, period: str) -> dict:
    return TenantBudgetsRepository()._table.get_item(
        Key={"tenant_id": tenant_id, "sk": budget_sk(period)}
    ).get("Item", {})


def _row(tenant_id: str, period: str) -> dict:
    return TenantBudgetsRepository().pool_summary(tenant_id, period)


def _advance_mirrors(tenant_id: str, period: str, reserved: int, settled: int) -> None:
    TenantBudgetsRepository()._table.update_item(
        Key={"tenant_id": tenant_id, "sk": budget_sk(period)},
        UpdateExpression="SET pool_reserved_microusd = :r, pool_settled_microusd = :s",
        ExpressionAttributeValues={":r": Decimal(reserved), ":s": Decimal(settled)},
    )


def test_backfill_reconciles_legacy_row_and_preserves_counters(seed_tenant_with_pool):
    seed = seed_tenant_with_pool
    tid, period = seed["tenant_id"], seed["period"]
    _advance_mirrors(tid, period, reserved=1_000_000, settled=500_000)
    _strip_headroom(tid, period)
    assert "pool_headroom_microusd" not in _raw(tid, period)

    dry = backfill(apply=False)
    assert dry["reconciled"] == 1 and dry["applied"] is False
    assert "pool_headroom_microusd" not in _raw(tid, period)  # dry-run wrote nothing

    applied = backfill(apply=True)
    assert applied["reconciled"] == 1 and applied["applied"] is True
    got = _row(tid, period)
    assert got["pool_headroom_microusd"] == 3_500_000  # 5M - 1M - 0.5M
    assert got["pool_reserved_microusd"] == 1_000_000
    assert got["pool_settled_microusd"] == 500_000


def test_backfill_is_idempotent(seed_tenant_with_pool):
    seed = seed_tenant_with_pool
    tid, period = seed["tenant_id"], seed["period"]
    _strip_headroom(tid, period)
    backfill(apply=True)
    again = backfill(apply=True)
    assert again["reconciled"] == 0
    assert again["already_at_invariant"] == 1


def test_backfill_repairs_mixed_window_wrong_headroom(seed_tenant_with_pool):
    """Fable review finding 2: during a rolling deploy a new-code settle can fire
    on a not-yet-backfilled row, CREATING pool_headroom at a wrong value
    (reserved - actual instead of limit - reserved - settled). A presence-gated
    backfill would skip it forever. reconcile REPAIRS it by value."""
    seed = seed_tenant_with_pool
    tid, period = seed["tenant_id"], seed["period"]
    # limit 5M; after a settle the mirrors are reserved=0, settled=0.5M, so the
    # true headroom is 4.5M. But a mixed-window settle created headroom at just
    # (reserved - actual) = -0.5M (or any wrong value); force a wrong value.
    _advance_mirrors(tid, period, reserved=0, settled=500_000)
    _set_headroom(tid, period, -500_000)  # the corrupt value the ADD would leave
    assert _raw(tid, period)["pool_headroom_microusd"] == -500_000

    summary = backfill(apply=True)
    assert summary["reconciled"] == 1
    assert summary["of_which_drifted"] == 1  # present-but-wrong, not missing
    assert _row(tid, period)["pool_headroom_microusd"] == 4_500_000  # repaired


def test_backfill_reconciles_negative_invariant(dynamodb_mock):
    """An over-reserved legacy row (reserved+settled > limit) is reported AND
    reconciled to the true negative so the gate keeps refusing — never left as a
    missing/stale value that could wrongly admit."""
    repo = TenantBudgetsRepository()
    period = current_period()
    repo._table.put_item(Item={
        "tenant_id": "org-over", "sk": budget_sk(period),
        "pool_limit_microusd": Decimal(1_000_000),
        "pool_reserved_microusd": Decimal(900_000),
        "pool_settled_microusd": Decimal(300_000),  # 900k+300k > 1M => -200k
        "status": "active", "version": "1",
    })
    summary = backfill(apply=True)
    assert summary["negative_invariant"] == 1
    assert summary["reconciled"] == 1
    assert _raw("org-over", period)["pool_headroom_microusd"] == -200_000
