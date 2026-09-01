"""The one fixture every ledger-latency benchmark seeds a pool row through.

R39c: a benchmark's seeded row must be explained by
its own source attributes, not by a bare `pool_limit_microusd` figure handed
straight to a setter. Before F1 landed the closed-world declaration
(`dynamo.pool_row_schema`), `pool_limit_microusd` WAS the only knob a seed call
had -- a five-script call to `set_pool_limit(pool_limit_microusd=<literal>)`
seeded a self-consistent row because nothing else the schema now carries
existed to disagree with it. After F1 that call pattern seeds a row whose
limit its own source attributes do not explain, silently: the write succeeds,
the benchmark runs, and the published figure describes a row shape a real
tenant's pool row cannot be in.

`seed_verified_pool` is the one door: it writes the row from its SOURCE
attributes (`seat_count` xor `manual_limit_microusd`, plus an optional grant),
through the SAME writers the application uses -- `create_seat_tracked_pool`,
`set_manual_limit`, `grant_apply_txn_item` -- then reads the row back and
asserts, before returning control to the caller, that both identities the
epic's schema declares hold:

    pool_limit_microusd    == baseline(seat_count, manual_limit_microusd)
                                + coalesce(pool_granted_microusd, 0)
    pool_headroom_microusd == pool_limit_microusd
                                - pool_reserved_microusd - pool_settled_microusd

A benchmark run must never silently time a row whose limit its own source
attributes do not explain, so a failed identity raises loudly rather than
logging and continuing.

`seat_count` and `manual_limit_microusd` are mutually exclusive by the row's
own design (`dynamo.tenant_budgets.is_seat_tracked`: the operator's figure is
either absent -- seat-tracked -- or present, never both, never neither), so
this fixture accepts exactly one of the two and refuses the ambiguous or empty
case rather than guessing which the caller meant.

Out of scope, deliberately: `bench_marker_shard_spike.py`'s `n>1` branch writes
synthetic per-shard rows for the sharding spike
`docs/design/ledger-hot-path.md` rejected. Those are not "a tenant's pool row"
in the sense this identity applies to, and forcing them through this fixture
would apply a real-tenant invariant to a structure the design deliberately
does not ship.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from dynamo.tenant_budgets import TenantBudgetsRepository


def seed_verified_pool(
    repo: "TenantBudgetsRepository",
    *,
    tenant_id: str,
    period: str,
    seat_count: Optional[int] = None,
    manual_limit_microusd: Optional[int] = None,
    pool_granted_microusd: int = 0,
    status: str = "active",
) -> dict[str, Any]:
    """Write a bench tenant's pool row from its SOURCE attributes, then read the
    row back and assert the two identities hold before returning control.

    Exactly one of `seat_count` / `manual_limit_microusd` must be given -- a row
    is seat-tracked xor carries an operator's figure, and a fixture that
    accepted both or neither would be deciding the ambiguity silently instead
    of the caller deciding it explicitly.

    `pool_granted_microusd`, when non-zero, is applied through
    `TenantBudgetsRepository.grant_apply_txn_item` -- the SAME transaction
    fragment the approval path commits -- rather than written by hand, so a
    "granted" benchmark row is a row a real approval could have produced, not
    one shaped by this fixture's own arithmetic. The cap check inside that
    writer is given an effectively-unlimited headroom (this fixture is not
    exercising cap enforcement), so only the identity this file asserts can
    fail it.

    Raises `AssertionError` -- loudly, never logged-and-continued -- if either
    identity fails to hold on the row read back.
    """
    if (seat_count is None) == (manual_limit_microusd is None):
        raise ValueError(
            "seed_verified_pool: give exactly one of seat_count or "
            "manual_limit_microusd -- a pool row is seat-tracked XOR carries "
            "an operator's figure, never both and never neither, so a caller "
            "must say which this benchmark row is")

    from dynamo.tenant_budgets import (
        _budgets_low_level_client,
        baseline_microusd,
        budget_sk,
        expected_pool_limit_microusd,
        granted_microusd,
    )

    # Start every seed from a clean row: a benchmark re-run must not inherit a
    # prior run's shape (e.g. a manual row left over from an earlier pass would
    # make this call's seat-tracked request silently seat-track a row that
    # still also carries the old manual_limit_microusd).
    repo._table.delete_item(Key={"tenant_id": tenant_id, "sk": budget_sk(period)})

    if seat_count is not None:
        repo.create_seat_tracked_pool(
            tenant_id=tenant_id, period=period, seat_count=int(seat_count),
            status=status)
    else:
        repo.set_manual_limit(
            tenant_id=tenant_id, period=period,
            manual_limit_microusd=int(manual_limit_microusd), status=status)

    if pool_granted_microusd:
        # cap_minus_amount is deliberately huge: this fixture seeds a BENCHMARK
        # row, not a cap-enforcement test, so the grant must apply unconditionally.
        _budgets_low_level_client().transact_write_items(TransactItems=[
            repo.grant_apply_txn_item(
                target_pk=tenant_id, target_sk=budget_sk(period),
                approved_amount_microusd=int(pool_granted_microusd),
                cap_minus_amount=10 ** 18,
            )
        ])

    row = repo.get(tenant_id, period, consistent_read=True)
    if row is None:
        raise AssertionError(
            f"seed_verified_pool: no row exists for {tenant_id}/{period} "
            f"immediately after seeding it")

    actual_limit = int(row["pool_limit_microusd"])
    expected_limit = expected_pool_limit_microusd(row)
    if actual_limit != expected_limit:
        raise AssertionError(
            f"seed_verified_pool: pool_limit_microusd={actual_limit} for "
            f"{tenant_id}/{period}, but the row's own source attributes "
            f"(baseline={baseline_microusd(row)}, "
            f"granted={granted_microusd(row)}) say it must be "
            f"{expected_limit} -- refusing to let a benchmark time a row whose "
            f"limit its own source attributes do not explain")

    reserved = int(row.get("pool_reserved_microusd", 0))
    settled = int(row.get("pool_settled_microusd", 0))
    actual_headroom = int(row["pool_headroom_microusd"])
    expected_headroom = actual_limit - reserved - settled
    if actual_headroom != expected_headroom:
        raise AssertionError(
            f"seed_verified_pool: pool_headroom_microusd={actual_headroom} for "
            f"{tenant_id}/{period}, but limit - reserved - settled = "
            f"{expected_headroom} -- the row's own counters disagree with its "
            f"own headroom on a row nothing has reserved against yet")

    return row
