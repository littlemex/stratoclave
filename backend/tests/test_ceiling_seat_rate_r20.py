"""R20 (the F1 contract): `SEAT_MONTHLY_USD` is not a live knob -- the
rate in force is stored once, and a process configured differently refuses to
start unless a migration flag is set.

R20's own "Verified by": "Unit: the mismatch refuses startup with a named
error; the migration path recomputes every seat-tracked row."

Today `seat_monthly_usd()` reads `STRATOCLAVE_SEAT_MONTHLY_USD` fresh on every
call with NO stored value anywhere to check it against -- an operator can
change the env var on a rolling deploy and different processes in the same
fleet will compute different pool limits for the same tenant with no error at
all. `dynamo.tenant_budgets` has no `stored_seat_rate_usd`,
`SeatRateMismatchError`, or `assert_seat_rate_in_force`, and
`backend.migrations.pool_ceiling_migration.recompute_seat_tracked_rows` does not exist. Every test below
fails on `AttributeError`/`ImportError`/`ModuleNotFoundError` for that reason.

Design note (including the scope-boundary gap this file does NOT attempt to
paper over -- there is no in-scope file that wires this into an actual process
boot): /Users/akazawt/tmp/stratoneed/change-pipeline/quota-raise-and-archive/design-F1.md
section 5.
"""
from __future__ import annotations

from decimal import Decimal

from dynamo import TenantBudgetsRepository, current_period
from dynamo.tenant_budgets import budget_sk

_SEAT_MICROUSD = 200 * 1_000_000


def _seed_row(tenant_id: str, period: str, *, seat_count: int) -> None:
    baseline = seat_count * _SEAT_MICROUSD
    TenantBudgetsRepository()._table.put_item(Item={
        "tenant_id": tenant_id,
        "sk": budget_sk(period),
        "pool_limit_microusd": Decimal(baseline),
        "pool_headroom_microusd": Decimal(baseline),
        "pool_reserved_microusd": Decimal(0),
        "pool_settled_microusd": Decimal(0),
        "seat_count": Decimal(seat_count),
        "status": "active",
        "version": "3",
    })


def test_a_never_migrated_process_has_no_stored_rate_and_the_check_is_a_permanent_no_op(
    monkeypatch, dynamodb_mock,
):
    """A real caveat, found by running this file's first draft against the
    independent implementation, not a test bug: `assert_seat_rate_in_force`
    never seeds anything by itself. Only `phase_m1_add_attributes` (or
    `TenantBudgetsRepository.record_rate_in_force` directly) records the rate
    in force. So a fleet that has never run M1 has `rate_in_force_microusd()`
    return `None` forever, and this check is a standing no-op regardless of
    what the process is configured with -- "refuses to start" becomes true
    only AFTER M1 has run once. This is correct given M1 is mandatory
    infrastructure, and it is documented in the runbook rather than fixed
    here; this test pins the no-op as the current, intended behaviour."""
    from dynamo.tenant_budgets import TenantBudgetsRepository, assert_seat_rate_in_force

    monkeypatch.setenv("STRATOCLAVE_SEAT_MONTHLY_USD", "200")
    assert TenantBudgetsRepository().rate_in_force_microusd() is None, (
        "fixture sanity: nothing seeded yet"
    )

    assert assert_seat_rate_in_force() is None  # nothing recorded -- must not raise

    # And it is STILL nothing, regardless of the configured value: no seeding
    # happened, so a second boot at a totally different rate is equally a
    # no-op, not a mismatch.
    monkeypatch.setenv("STRATOCLAVE_SEAT_MONTHLY_USD", "9999")
    assert assert_seat_rate_in_force() is None
    assert TenantBudgetsRepository().rate_in_force_microusd() is None


def test_a_second_boot_at_the_same_rate_is_a_silent_no_op(monkeypatch, dynamodb_mock):
    from dynamo.tenant_budgets import TenantBudgetsRepository, assert_seat_rate_in_force

    monkeypatch.setenv("STRATOCLAVE_SEAT_MONTHLY_USD", "200")
    TenantBudgetsRepository().record_rate_in_force(rate_microusd=200_000_000)
    assert_seat_rate_in_force()
    assert_seat_rate_in_force()  # a second, ordinary boot -- must not raise


def test_a_mismatched_rate_refuses_to_start_with_a_named_error(monkeypatch, dynamodb_mock):
    """Requires a REAL recorded rate to test the refusal against -- the check
    itself never seeds one (see the permanent-no-op test above), so this
    seeds it directly via the same repository method the migration uses."""
    from dynamo.tenant_budgets import (
        SeatRateMismatchError, TenantBudgetsRepository, assert_seat_rate_in_force,
    )

    monkeypatch.setenv("STRATOCLAVE_SEAT_MONTHLY_USD", "200")
    TenantBudgetsRepository().record_rate_in_force(rate_microusd=200_000_000)

    monkeypatch.setenv("STRATOCLAVE_SEAT_MONTHLY_USD", "250")  # a config drift
    monkeypatch.delenv("STRATOCLAVE_SEAT_RATE_MIGRATION", raising=False)

    try:
        assert_seat_rate_in_force()
    except SeatRateMismatchError:
        pass
    else:
        raise AssertionError(
            "a process configured at $250/seat started against a stored rate "
            "of $200/seat with no migration flag set -- it must refuse to start"
        )


def test_the_migration_flag_is_the_named_escape_hatch(monkeypatch, dynamodb_mock):
    from dynamo.tenant_budgets import TenantBudgetsRepository, assert_seat_rate_in_force

    monkeypatch.setenv("STRATOCLAVE_SEAT_MONTHLY_USD", "200")
    TenantBudgetsRepository().record_rate_in_force(rate_microusd=200_000_000)

    monkeypatch.setenv("STRATOCLAVE_SEAT_MONTHLY_USD", "250")
    monkeypatch.setenv("STRATOCLAVE_SEAT_RATE_MIGRATION", "1")

    assert_seat_rate_in_force()  # must not raise -- the flag authorizes the difference


def test_migration_recomputes_every_seat_tracked_row_and_leaves_a_manual_row_untouched(
    monkeypatch, dynamodb_mock,
):
    from migrations.pool_ceiling_migration import recompute_seat_tracked_rows

    monkeypatch.setenv("STRATOCLAVE_SEAT_MONTHLY_USD", "200")
    tenant_a, tenant_b, period = "seat-tracked-co", "manual-co", current_period()
    _seed_row(tenant_a, period, seat_count=5)  # 5 * $200 = $1000
    # A manual row, seeded directly (recompute_seat_rate must not touch it).
    TenantBudgetsRepository()._table.put_item(Item={
        "tenant_id": tenant_b, "sk": budget_sk(period),
        "pool_limit_microusd": Decimal(777_000_000),
        "pool_headroom_microusd": Decimal(777_000_000),
        "pool_reserved_microusd": Decimal(0), "pool_settled_microusd": Decimal(0),
        "seat_count": Decimal(3), "manual_limit_microusd": Decimal(777_000_000),
        "status": "active", "version": "3",
    })

    monkeypatch.setenv("STRATOCLAVE_SEAT_MONTHLY_USD", "250")
    monkeypatch.setenv("STRATOCLAVE_SEAT_RATE_MIGRATION", "1")
    summary = recompute_seat_tracked_rows(apply=True)

    row_a = TenantBudgetsRepository().get(tenant_a, period)
    assert int(row_a["pool_limit_microusd"]) == 5 * 250 * 1_000_000
    assert int(row_a["pool_headroom_microusd"]) == 5 * 250 * 1_000_000

    row_b = TenantBudgetsRepository().get(tenant_b, period)
    assert int(row_b["pool_limit_microusd"]) == 777_000_000, (
        "a manual row must not be recomputed by a seat-rate migration"
    )

    assert summary["recomputed"] == 1
    assert summary["untouched_operator_figures"] == 1

    assert TenantBudgetsRepository().rate_in_force_microusd() == 250 * 1_000_000, (
        "the stored rate must move only after every row does"
    )


# ---------------------------------------------------------------------------
# Amendment A2: `backend/main.py` is in scope for R20's call site. "Refuses to
# start" is not expressible while the check lives only in a repository
# module -- something on an actual boot path has to call it, the same way
# `mvp._concurrency.configure_capacity()` and
# `mvp.price_sources.validate_configuration()` are already called,
# unguarded, from `main.py`'s `lifespan`. The failure this closes already
# happened once in this change: the CDK forwarded a hardcoded '100000', so
# the rate knob was inert in production despite being fully correct in the
# repository module.
#
# Today `main.py`'s lifespan calls neither `assert_seat_rate_in_force` nor any
# function like it, so both tests below fail: the first because the spy is
# never invoked (the call site does not exist), the second because nothing
# raises at all (there is nothing to propagate).
# ---------------------------------------------------------------------------

def _run_lifespan_once() -> None:
    """Drive `main.py`'s ASGI lifespan through startup and back down through
    shutdown once, synchronously, for a test. Heavy unrelated steps (seed,
    price-source validation) are neutralized by the caller before this runs."""
    import asyncio

    import main

    async def _once():
        async with main.lifespan(main.app):
            pass

    asyncio.run(_once())


def _neutralize_unrelated_lifespan_steps(monkeypatch) -> None:
    """Every OTHER thing `main.py`'s lifespan does before it would reach a
    seat-rate check, neutralized so this file's evidence is about the R20
    call site alone:
      - seed_all() already no-ops under STRATOCLAVE_DISABLE_SEED=true
        (set globally by tests/conftest.py) -- nothing to neutralize.
      - the price source is stubbed so this test does not depend on
        whatever STRATOCLAVE_PRICE_SOURCE happens to be set to elsewhere.
      - external VSR is off by default (EXTERNAL_VSR_ENABLED unset) and is
        wrapped in its own try/except regardless.
    """
    import mvp.price_sources as price_sources

    monkeypatch.setattr(price_sources, "validate_configuration", lambda: "bundled")


def test_main_lifespan_calls_the_seat_rate_check_at_boot(monkeypatch, dynamodb_mock):
    _neutralize_unrelated_lifespan_steps(monkeypatch)

    import dynamo.tenant_budgets as tenant_budgets

    calls: list[bool] = []
    # raising=False: the attribute does not exist yet on the module at all
    # (R20 has not landed), so this ADDS it rather than overriding it -- the
    # point is to prove main.py's lifespan reaches for a name at this path,
    # which it cannot do today regardless.
    monkeypatch.setattr(
        tenant_budgets, "assert_seat_rate_in_force", lambda: calls.append(True),
        raising=False,
    )

    _run_lifespan_once()

    assert calls, (
        "backend/main.py's lifespan never called a seat-rate startup check -- "
        "Amendment A2 puts main.py in scope for exactly this call site, "
        "parallel to configure_capacity() and price_sources.validate_configuration()"
    )


def test_a_seat_rate_refusal_propagates_out_of_the_lifespan_and_blocks_boot(
    monkeypatch, dynamodb_mock,
):
    """The call site is only worth having if a refusal from it is NOT
    swallowed by an advisory try/except (the way the VSR handshake and the
    bootstrap seed are, deliberately, because those are non-critical) -- a
    seat-rate mismatch must propagate the same way `validate_configuration()`'s
    failure already does, and fail the ASGI boot."""
    _neutralize_unrelated_lifespan_steps(monkeypatch)

    import dynamo.tenant_budgets as tenant_budgets

    class _Boom(Exception):
        pass

    monkeypatch.setattr(
        tenant_budgets, "assert_seat_rate_in_force", lambda: (_ for _ in ()).throw(_Boom()),
        raising=False,
    )

    raised = False
    try:
        _run_lifespan_once()
    except _Boom:
        raised = True
    assert raised, (
        "a seat-rate mismatch raised inside the boot path did not propagate out "
        "of main.py's lifespan -- it must fail startup, not be logged and swallowed"
    )
