"""External-review finding (Fable + Codex, independently, same question):
`migrations.pool_ceiling_migration.recompute_seat_tracked_rows`'s write CAS was

    ConditionExpression = "pool_limit_microusd = :obs_limit AND seat_count = :obs_seats"

which names neither `manual_limit_microusd`'s absence nor the observed
`seat_rate_microusd` -- so two concurrent operator/membership actions can slip
through it:

  Scenario A. Between the migration's per-row READ (the batch scan) and its
  WRITE (this row's turn in the loop), an operator calls `set_manual_limit`
  with a figure that happens to equal the row's CURRENT baseline. That call's
  own delta is then zero, so it moves neither `pool_limit_microusd` nor
  `seat_count` -- the two values the migration's CAS is watching -- even
  though it adds `manual_limit_microusd` and switches the row from
  seat-tracked to manual. The migration's CAS still passes (nothing it checks
  moved) and it moves `pool_limit_microusd` by the seat-rate delta anyway,
  landing money on a row that has just become an operator's figure.

  Scenario B. `TenantBudgetsRepository.adjust_pool_for_seat_delta` (the ONE
  seat-delta writer, C14.4) reads the row's OWN `seat_rate_microusd`, uses it
  as the ADD's coefficient (`seat_delta * rate`), and issues an ADD guarded
  only on `manual_limit_microusd`'s absence -- never on the rate it just read.
  If the migration recomputes and stores a NEW rate between that read and this
  ADD landing, the ADD still lands (nothing in its condition mentions the
  rate), carrying a delta computed at the OLD rate on top of a `pool_limit`
  the migration has already moved to the NEW rate.

Both races are reproduced below WITHOUT threads: moto is single-process, so a
"concurrent" write is simulated by intercepting the low-level `update_item`
call one operation is about to make (already computed from its own prior
read) and running the OTHER operation for real in between -- the standard,
deterministic way to pin an interleaving that would otherwise depend on
timing. `dynamo.client.get_dynamodb_resource` is `lru_cache(maxsize=1)`
(`backend/dynamo/client.py`), so every `TenantBudgetsRepository()` instance in
one test shares the SAME low-level boto3 client -- which is what makes
patching `client.update_item` once affect every repository call in the test,
not only the one that happened to construct it.
"""
from __future__ import annotations

from dynamo.tenant_budgets import TenantBudgetsRepository, budget_sk, current_period

_SEAT_OLD_USD = 200
_SEAT_NEW_USD = 250
_SEAT_OLD_MICROUSD = _SEAT_OLD_USD * 1_000_000
_SEAT_NEW_MICROUSD = _SEAT_NEW_USD * 1_000_000


def _intercept_one_update_item(monkeypatch, repo: TenantBudgetsRepository, *, matches, on_match):
    """Patch the ONE shared low-level client so the FIRST `update_item` call
    whose kwargs satisfy `matches(kwargs)` triggers `on_match()` before the
    real call proceeds -- simulating a concurrent write landing in the window
    between the caller's own read (already taken, above its `update_item`
    call) and this write.

    `matches`/`on_match` fire at most once; every other call (including any
    the racing operation's OWN `on_match()` makes) passes straight through to
    the real client, so there is no re-entrant loop.
    """
    client = repo._table.meta.client
    original = client.update_item
    fired = {"done": False}

    def wrapped(**kwargs):
        if not fired["done"] and matches(kwargs):
            fired["done"] = True
            on_match()
        return original(**kwargs)

    monkeypatch.setattr(client, "update_item", wrapped)
    return fired


def test_scenario_a_operator_set_manual_limit_races_the_migrations_read_then_write(
    monkeypatch, dynamodb_mock,
):
    """Reproduces, then must no longer reproduce once the CAS also checks
    `attribute_not_exists(manual_limit_microusd)`: a manual row's
    `pool_limit_microusd` must equal its own `manual_limit_microusd` (no
    grants seeded here), never the seat-rate-recomputed figure the migration
    would have written for a still-seat-tracked row."""
    from migrations.pool_ceiling_migration import recompute_seat_tracked_rows

    tenant_id, period = "seat-race-a-org", current_period()
    monkeypatch.setenv("STRATOCLAVE_SEAT_MONTHLY_USD", str(_SEAT_OLD_USD))
    repo = TenantBudgetsRepository()
    repo.create_seat_tracked_pool(tenant_id=tenant_id, period=period, seat_count=5)
    old_baseline = 5 * _SEAT_OLD_MICROUSD  # $1000, matches the row's pool_limit

    def _matches_migration_write(kwargs: dict) -> bool:
        ue = kwargs.get("UpdateExpression", "")
        key = kwargs.get("Key", {})
        return (
            kwargs.get("TableName") == repo.table_name
            and key.get("sk") == budget_sk(period)
            and "seat_rate_microusd" in ue
        )

    def _operator_races_in():
        # The operator's figure happens to equal the row's CURRENT baseline,
        # so THIS call's own delta is zero -- it moves neither
        # `pool_limit_microusd` nor `seat_count`, the two values the
        # migration's (unfixed) CAS is watching.
        TenantBudgetsRepository().set_manual_limit(
            tenant_id=tenant_id, period=period,
            manual_limit_microusd=old_baseline)

    fired = _intercept_one_update_item(
        monkeypatch, repo, matches=_matches_migration_write, on_match=_operator_races_in)

    monkeypatch.setenv("STRATOCLAVE_SEAT_MONTHLY_USD", str(_SEAT_NEW_USD))
    monkeypatch.setenv("STRATOCLAVE_SEAT_RATE_MIGRATION", "1")
    summary = recompute_seat_tracked_rows(apply=True)
    assert fired["done"], "fixture sanity: the race did not actually happen"

    row = repo.get(tenant_id, period)
    assert int(row["manual_limit_microusd"]) == old_baseline, (
        "the operator's figure must survive the race untouched"
    )
    assert int(row["pool_limit_microusd"]) == old_baseline, (
        f"a MANUAL row's pool_limit_microusd must equal its own "
        f"manual_limit_microusd (no grants here) -- got "
        f"{row['pool_limit_microusd']}, which is "
        f"{int(row['pool_limit_microusd']) - old_baseline} microUSD off. "
        f"That delta is exactly seats(5) x (new_rate - old_rate) = "
        f"{5 * (_SEAT_NEW_MICROUSD - _SEAT_OLD_MICROUSD)} -- the migration "
        f"moved the ceiling of a row that had just become an operator's "
        f"figure. summary={summary}"
    )
    if "lost_cas_retry_next_pass" in summary:  # only present once fixed
        assert summary["lost_cas_retry_next_pass"] == 1
        assert summary["recomputed"] == 0


def test_scenario_b_seat_delta_races_the_migrations_rate_change(monkeypatch, dynamodb_mock):
    """Reproduces, then must no longer reproduce once
    `adjust_pool_for_seat_delta`'s ADD is guarded on the rate it read: after a
    hire lands concurrently with a rate change, the row's `pool_limit_microusd`
    must equal `seat_count x the rate now stored on the row` -- never a mix of
    seats counted at the new rate and one hire's contribution computed at the
    old one."""
    from migrations.pool_ceiling_migration import recompute_seat_tracked_rows

    tenant_id, period = "seat-race-b-org", current_period()
    monkeypatch.setenv("STRATOCLAVE_SEAT_MONTHLY_USD", str(_SEAT_OLD_USD))
    repo = TenantBudgetsRepository()
    repo.create_seat_tracked_pool(tenant_id=tenant_id, period=period, seat_count=5)

    migration_ran = {"done": False}

    def _matches_seat_delta_write(kwargs: dict) -> bool:
        ue = kwargs.get("UpdateExpression", "")
        key = kwargs.get("Key", {})
        return (
            kwargs.get("TableName") == repo.table_name
            and key.get("sk") == budget_sk(period)
            and "ADD seat_count" in ue
            and "pool_limit_microusd" in ue
        )

    def _migration_races_in():
        # The seat delta has ALREADY read the row (at the OLD rate) and
        # computed its own kwargs by the time this fires -- it is about to
        # issue its own `update_item` with a coefficient of `seat_delta x
        # old_rate`. The migration lands here, for real, changing the row's
        # rate and its pool_limit BEFORE that ADD is sent.
        migration_ran["done"] = True
        monkeypatch.setenv("STRATOCLAVE_SEAT_MONTHLY_USD", str(_SEAT_NEW_USD))
        monkeypatch.setenv("STRATOCLAVE_SEAT_RATE_MIGRATION", "1")
        recompute_seat_tracked_rows(apply=True)

    fired = _intercept_one_update_item(
        monkeypatch, repo, matches=_matches_seat_delta_write, on_match=_migration_races_in)

    moved = repo.adjust_pool_for_seat_delta(tenant_id=tenant_id, period=period, seat_delta=2)

    assert fired["done"], "fixture sanity: the race did not actually happen"
    assert migration_ran["done"]

    row = repo.get(tenant_id, period)
    expected_seats = 5 + 2
    expected_limit = expected_seats * _SEAT_NEW_MICROUSD
    assert moved is True, "the hire's money must still move (the row never went manual)"
    assert int(row["seat_count"]) == expected_seats
    assert int(row["pool_limit_microusd"]) == expected_limit, (
        f"expected {expected_seats} seats x the NEW rate "
        f"({_SEAT_NEW_MICROUSD}) = {expected_limit}, got "
        f"{row['pool_limit_microusd']} -- off by "
        f"{int(row['pool_limit_microusd']) - expected_limit} microUSD, which "
        f"is exactly 2 seats x (old_rate - new_rate). The seat delta landed "
        f"its OWN contribution computed at the stale (pre-migration) rate on "
        f"top of a row the migration had already moved to the new one."
    )
    assert int(row["pool_headroom_microusd"]) == expected_limit, (
        "headroom must move by exactly the same amount as the limit (no "
        "reserve/settle activity here)"
    )
