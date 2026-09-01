"""R2 (the F1 contract): the migration, five ordered phases, with
per-row verification.

R2's own "Verified by": "A rehearsal from a snapshot carrying `per_seat`,
`fixed` and absent-`sizing` rows, asserting every row's effective limit is
unchanged."

This file rehearses M2 -- the backfill that turns each row's OLD
`sizing`/`pool_limit_microusd` shape into the NEW `seat_count`/
`manual_limit_microusd` shape -- against a snapshot carrying all three
pre-migration row states the contract names:

    sizing="per_seat"  -> seat_count = pool_limit / SEAT, manual_limit absent
    sizing="fixed"     -> manual_limit = pool_limit (incl. 0), seat_count from
                          a live membership count
    sizing ABSENT      -> same as "fixed" (an absent sizing predates this
                          change and IS an operator figure -- see
                          the F1 contract's own note on this, and
                          docs/design/limits.md section 4)

and the one M2 case the contract carves out explicitly: a `per_seat` row
whose `pool_limit / SEAT` is NOT an integer goes on an adjudication list and
is NOT migrated.

Amendment A5: `SEAT` in that division is the rate M1 SEEDED into R20's stored-
rate control item (`__CONTROL__`/`SEAT_RATE`, `seat_rate_microusd`), never a live read of
`STRATOCLAVE_SEAT_MONTHLY_USD` at migration time -- an operator changing the
env var between M1 and M2 must not silently convert a `per_seat` row to a
wrong-but-integer `seat_count` that looks entirely well-formed. Every test
below therefore seeds the stored rate directly (standing in for "M1 already
ran") and sets the LIVE env var to a DIFFERENT, deliberately wrong value, so
a migration that reads the environment instead of the stored item would
compute the WRONG seat_count and fail loudly rather than passing by
coincidence.

Today `backend.migrations.pool_ceiling_migration.phase_m2_backfill` does not exist at
all, so every test below fails on `ModuleNotFoundError`.

Design note: /Users/akazawt/tmp/stratoneed/change-pipeline/quota-raise-and-archive/design-F1.md
section 2 (M2).
"""
from __future__ import annotations

from decimal import Decimal

from dynamo import TenantBudgetsRepository, UserTenantsRepository, current_period
from dynamo.tenant_budgets import budget_sk

# `baseline_microusd` does not exist yet (R1); imported lazily inside each
# test body below (not at module scope) so a missing name there does not turn
# every test in this R2 file into a single collection error -- each test's
# own failure should be attributable to what IT exercises.

_SEAT_MICROUSD = 200 * 1_000_000

# The rate this file always seeds as "the rate M1 already stored" (Amendment
# A5). Every test also points STRATOCLAVE_SEAT_MONTHLY_USD at a DIFFERENT,
# wrong value, so a migration that reads the live environment instead of the
# stored rate produces a detectably wrong seat_count rather than the same
# right answer by coincidence.
_STORED_RATE_USD = 200
_WRONG_LIVE_ENV_USD = 999


def _seed_stored_rate(usd: int) -> None:
    """Simulate R20's stored-rate control item already being seeded by M1 --
    a raw write standing in for "M1 has run on this environment". This
    file's evidence is about M2 reading THIS value, not about how it got
    seeded (that mechanism is R20's own test file).

    Retargeted after reading the independent implementation: the control
    item's partition key is `"__CONTROL__"` (`dynamo.tenant_budgets
    .SEAT_RATE_CONTROL_PK`), not `"__system__"`, and the rate is stored in
    MICRO-USD under `seat_rate_microusd` (`dynamo.pool_row_schema
    .SEAT_RATE_ATTR`) -- the same unit every other money attribute on the
    pool row uses -- not whole USD."""
    TenantBudgetsRepository()._table.put_item(Item={
        "tenant_id": "__CONTROL__",
        "sk": "SEAT_RATE",
        "seat_rate_microusd": Decimal(usd * 1_000_000),
    })


def _baseline_item(*, seat_count: int, manual_limit_microusd) -> dict:
    """`baseline_microusd` takes the whole row as a dict and reads presence,
    not a keyword -- `manual_limit_microusd=None` has to be an ABSENT key,
    never a key holding `None`, or `is_seat_tracked` reads it as a present
    figure of `None`."""
    item = {"seat_count": seat_count}
    if manual_limit_microusd is not None:
        item["manual_limit_microusd"] = manual_limit_microusd
    return item


def _seed_membership_directly(user_id: str, tenant_id: str) -> None:
    """Write an ACTIVE UserTenants row WITHOUT going through `ensure()`.

    `ensure()` triggers the real seat-delta path
    (`_adjust_pool_seat_delta_best_effort` -> `adjust_pool_for_seat_delta`),
    which reads "no manual_limit_microusd attribute" as "seat-tracked" and
    moves `pool_limit_microusd` by a seat's money -- correct for a POST-F1 row,
    but this file's legacy fixtures have no `manual_limit_microusd` attribute
    PRECISELY because they predate M2 (that is what M2 is about to backfill),
    so `ensure()` would silently inflate the seeded `pool_limit_microusd`
    before the migration under test ever runs. A raw write sidesteps the
    seat-delta path entirely, matching test_ceiling_deletion_seat_drop_r1c.py's
    own `_seed_active_membership_directly`."""
    from decimal import Decimal as _Decimal

    UserTenantsRepository()._table.put_item(Item={
        "user_id": user_id, "tenant_id": tenant_id, "role": "user",
        "status": "active", "total_credit": _Decimal(1_000_000_000),
        "credit_used": _Decimal(0), "credit_source": "tenant_default",
    })


def _seed_legacy_row(tenant_id: str, period: str, *, sizing, pool_limit_microusd: int) -> None:
    """Write a row in the OLD (pre-F1) shape: `sizing` + `pool_limit_microusd`
    only, no `seat_count`/`manual_limit_microusd` at all -- exactly what M1's
    dual-write has not yet touched."""
    item = {
        "tenant_id": tenant_id,
        "sk": budget_sk(period),
        "pool_limit_microusd": Decimal(pool_limit_microusd),
        "pool_headroom_microusd": Decimal(pool_limit_microusd),
        "pool_reserved_microusd": Decimal(0),
        "pool_settled_microusd": Decimal(0),
        "status": "active",
        "version": "2",
    }
    if sizing is not None:
        item["sizing"] = sizing
    TenantBudgetsRepository()._table.put_item(Item=item)


def _raw(tenant_id: str, period: str) -> dict:
    return TenantBudgetsRepository()._table.get_item(
        Key={"tenant_id": tenant_id, "sk": budget_sk(period)}
    ).get("Item", {})


def test_per_seat_row_backfills_seat_count_and_reproduces_the_same_effective_limit(
    dynamodb_mock, monkeypatch,
):
    from dynamo.tenant_budgets import baseline_microusd
    from migrations.pool_ceiling_migration import phase_m2_backfill as backfill

    _seed_stored_rate(_STORED_RATE_USD)
    monkeypatch.setenv("STRATOCLAVE_SEAT_MONTHLY_USD", str(_WRONG_LIVE_ENV_USD))

    tenant_id, period = "per-seat-co", current_period()
    _seed_legacy_row(tenant_id, period, sizing="per_seat", pool_limit_microusd=3 * _SEAT_MICROUSD)

    summary = backfill(apply=True)
    assert summary["applied"] is True

    row = _raw(tenant_id, period)
    assert "manual_limit_microusd" not in row, (
        "a per_seat row must migrate to ABSENT manual_limit, not a manual figure"
    )
    assert int(row["seat_count"]) == 3, (
        "seat_count must be derived from the STORED rate (200), not the "
        f"deliberately-wrong live env ({_WRONG_LIVE_ENV_USD}) -- got "
        f"{row.get('seat_count')!r}"
    )
    assert int(row["pool_limit_microusd"]) == 3 * _SEAT_MICROUSD, (
        "M2 must not touch pool_limit_microusd at all -- it stays exactly "
        "what the old sizing-driven writers left it at"
    )
    # And once the environment is corrected back to the stored rate (as R20
    # guarantees it always is outside the M1->M2 migration window),
    # recomputing the rule from the migrated attributes reproduces the SAME
    # number the old sizing mechanism was already enforcing.
    monkeypatch.setenv("STRATOCLAVE_SEAT_MONTHLY_USD", str(_STORED_RATE_USD))
    assert baseline_microusd(_baseline_item(
        seat_count=int(row["seat_count"]),
        manual_limit_microusd=row.get("manual_limit_microusd"),
    )) == 3 * _SEAT_MICROUSD == int(row["pool_limit_microusd"])


def test_fixed_row_backfills_manual_limit_from_pool_limit_and_seat_count_from_live_membership(
    dynamodb_mock,
):
    from dynamo.tenant_budgets import baseline_microusd
    from migrations.pool_ceiling_migration import phase_m2_backfill as backfill

    _seed_stored_rate(_STORED_RATE_USD)  # M2 checks the stored rate on every run,
    # even a fixed-only one (Amendment A5) -- seeded here so backfill() does not
    # refuse before it ever reaches this row.

    tenant_id, period = "fixed-co", current_period()
    _seed_legacy_row(tenant_id, period, sizing="fixed", pool_limit_microusd=777_000_000)
    # Two live memberships -- the fixed row's seat_count comes from a live,
    # strongly-consistent count, NOT from pool_limit (a fixed figure carries
    # no seat information at all). Seeded directly (not via `ensure()`) so
    # the legacy row's absent manual_limit_microusd is not read as
    # seat-tracked by the real seat-delta path before the migration runs.
    _seed_membership_directly("u1", tenant_id)
    _seed_membership_directly("u2", tenant_id)

    summary = backfill(apply=True)
    assert summary["applied"] is True

    row = _raw(tenant_id, period)
    assert int(row["manual_limit_microusd"]) == 777_000_000
    assert int(row["seat_count"]) == 2
    assert baseline_microusd(_baseline_item(
        seat_count=int(row["seat_count"]),
        manual_limit_microusd=row.get("manual_limit_microusd"),
    )) == 777_000_000 == int(row["pool_limit_microusd"])


def test_fixed_row_at_zero_migrates_manual_limit_of_zero_not_absent(dynamodb_mock):
    """The contract is explicit: `manual_limit = pool_limit_microusd`
    "including 0" -- a fixed row an operator set to $0 must migrate to a
    PRESENT manual_limit_microusd=0, never to absence (absence would silently
    resume seat tracking on a row an operator zeroed out on purpose)."""
    from migrations.pool_ceiling_migration import phase_m2_backfill as backfill

    _seed_stored_rate(_STORED_RATE_USD)

    tenant_id, period = "zeroed-co", current_period()
    _seed_legacy_row(tenant_id, period, sizing="fixed", pool_limit_microusd=0)

    backfill(apply=True)

    row = _raw(tenant_id, period)
    assert "manual_limit_microusd" in row, "manual_limit=0 must migrate PRESENT, not absent"
    assert int(row["manual_limit_microusd"]) == 0


def test_absent_sizing_row_migrates_the_same_way_as_fixed(dynamodb_mock):
    """A row written before `sizing` existed at all (the shape every pool row
    predating PR 1 has) is an operator figure, exactly like `fixed` --
    the F1 contract's own note: 'An absent `sizing` migrates as an
    OPERATOR figure ... an unlabelled row predates PR 1 and its figure was
    chosen when seats did not exist.'"""
    from dynamo.tenant_budgets import baseline_microusd
    from migrations.pool_ceiling_migration import phase_m2_backfill as backfill

    _seed_stored_rate(_STORED_RATE_USD)

    tenant_id, period = "legacy-co", current_period()
    _seed_legacy_row(tenant_id, period, sizing=None, pool_limit_microusd=555_000_000)
    _seed_membership_directly("u1", tenant_id)

    backfill(apply=True)

    row = _raw(tenant_id, period)
    assert int(row["manual_limit_microusd"]) == 555_000_000
    assert int(row["seat_count"]) == 1
    assert baseline_microusd(_baseline_item(
        seat_count=int(row["seat_count"]),
        manual_limit_microusd=row.get("manual_limit_microusd"),
    )) == 555_000_000


def test_per_seat_row_with_a_non_integer_quotient_is_adjudicated_not_migrated(dynamodb_mock):
    """`pool_limit / SEAT` not landing on a whole seat count (e.g. a row that
    drifted, or the STORED rate no longer matches the row's history) must go
    on the adjudication list and be left untouched -- migrating it would
    silently invent a seat count nobody counted."""
    from migrations.pool_ceiling_migration import phase_m2_backfill as backfill

    _seed_stored_rate(_STORED_RATE_USD)

    tenant_id, period = "odd-co", current_period()
    # 3 * SEAT + 1 microUSD: not a whole multiple of SEAT.
    odd_limit = 3 * _SEAT_MICROUSD + 1
    _seed_legacy_row(tenant_id, period, sizing="per_seat", pool_limit_microusd=odd_limit)

    summary = backfill(apply=True)

    row = _raw(tenant_id, period)
    assert "seat_count" not in row, "a non-integer quotient must not be migrated"
    assert "manual_limit_microusd" not in row
    assert int(row["pool_limit_microusd"]) == odd_limit, "the untouched row's limit must not move"
    adjudication = summary.get("adjudication", [])
    assert len(adjudication) >= 1
    adjudicated_ids = {entry.get("tenant_id") for entry in adjudication}
    assert tenant_id in adjudicated_ids


def test_backfill_refuses_when_the_stored_rate_has_not_been_seeded_yet(dynamodb_mock):
    """Amendment A5: M2 reads the rate M1 seeded, never falls back to the
    live environment. If M1 has not actually run on this environment (no
    stored rate at all), M2 must refuse outright rather than silently
    dividing by whatever STRATOCLAVE_SEAT_MONTHLY_USD happens to say today --
    exactly the fallback that would reopen the ordering hole this amendment
    closes."""
    from migrations.pool_ceiling_migration import phase_m2_backfill as backfill

    tenant_id, period = "no-stored-rate-co", current_period()
    _seed_legacy_row(tenant_id, period, sizing="per_seat", pool_limit_microusd=3 * _SEAT_MICROUSD)
    # Deliberately no _seed_stored_rate() call -- M1 has not run here.

    raised = False
    try:
        backfill(apply=True)
    except Exception:
        raised = True
    assert raised, (
        "backfill(apply=True) must refuse when no stored seat rate exists "
        "yet, not silently fall back to the live environment value"
    )
    row = _raw(tenant_id, period)
    assert "seat_count" not in row, "a refused run must not have written anything"


def test_dry_run_writes_nothing(dynamodb_mock):
    from migrations.pool_ceiling_migration import phase_m2_backfill as backfill

    _seed_stored_rate(_STORED_RATE_USD)

    tenant_id, period = "dry-run-co", current_period()
    _seed_legacy_row(tenant_id, period, sizing="fixed", pool_limit_microusd=100_000_000)

    dry = backfill(apply=False)
    assert dry["applied"] is False

    row = _raw(tenant_id, period)
    assert "manual_limit_microusd" not in row, "a dry run must not write anything"
    assert "seat_count" not in row


def test_backfill_is_idempotent(dynamodb_mock):
    from migrations.pool_ceiling_migration import phase_m2_backfill as backfill

    _seed_stored_rate(_STORED_RATE_USD)

    tenant_id, period = "idempotent-co", current_period()
    _seed_legacy_row(tenant_id, period, sizing="per_seat", pool_limit_microusd=2 * _SEAT_MICROUSD)

    first = backfill(apply=True)
    second = backfill(apply=True)

    assert first["seat_tracked"] + first["operator_figure"] >= 1
    assert second["seat_tracked"] + second["operator_figure"] == 0, (
        "a second pass over an already-migrated row must be a no-op"
    )
    assert second["already_migrated"] >= 1
    row = _raw(tenant_id, period)
    assert int(row["seat_count"]) == 2
