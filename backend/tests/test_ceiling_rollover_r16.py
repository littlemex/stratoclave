"""R16 (the F1 contract): the period rollover has a named owner and
copies `manual_limit` and `seat_count` forward.

R16's own "Verified by": "Unit: rollover moves no effective limit for a
seat-tracked row or a manual one; a membership change against a missing
period row creates nothing partial."

Today `TenantBudgetsRepository.get()` documents (and the reserve path relies
on) "a tenant with no BUDGET row for the period is unlimited at the pool
level" -- nothing creates next period's row at all; nothing in this codebase
is named as period rollover's owner. `mvp.observability.period_rollover`
does not exist, so every rollover test below fails on `ModuleNotFoundError`.

Amendment B1 changes this file's shape: "which attributes carry forward" is
no longer a pair this file (or `rollover_period` itself) hardcodes -- it is
whichever attributes `dynamo.pool_row_schema.POOL_ROW_ATTRIBUTES` (B1's
closed-world declaration -- a dedicated leaf module, not
`dynamo.tenant_budgets`; no re-export exists) classifies `rollover="carried"`.
The seed helper
below now also stamps `seat_monthly_usd` (Amendment A5/S4's third row
attribute -- the per-row rate mirror), since it is one of the carried
attributes and a rollover test that never seeded it would not exercise that
part of the carry. A new test proves the carry is DECLARATION-DRIVEN rather
than hardcoded, by registering a fake extra "carried" attribute at test time
and checking `rollover_period` picks it up with no code change.

Design note: /Users/akazawt/tmp/stratoneed/change-pipeline/quota-raise-and-archive/design-F1.md
section 4, and section 0a for the declaration.
"""
from __future__ import annotations

from decimal import Decimal

from dynamo import TenantBudgetsRepository, UserTenantsRepository, current_period
from dynamo.tenant_budgets import budget_sk, previous_period

_SEAT_MICROUSD = 200 * 1_000_000
_RATE_USD = 200


def _seed_row(tenant_id: str, period: str, *, seat_count: int, manual_limit_microusd=None,
              reserved: int = 0, settled: int = 0) -> None:
    baseline = (
        int(manual_limit_microusd) if manual_limit_microusd is not None
        else seat_count * _SEAT_MICROUSD
    )
    item = {
        "tenant_id": tenant_id,
        "sk": budget_sk(period),
        "pool_limit_microusd": Decimal(baseline),
        "pool_headroom_microusd": Decimal(baseline - reserved - settled),
        "pool_reserved_microusd": Decimal(reserved),
        "pool_settled_microusd": Decimal(settled),
        "seat_count": Decimal(seat_count),
        "seat_monthly_usd": Decimal(_RATE_USD),  # Amendment A5/S4's 3rd row attribute
        "status": "active",
        "version": "3",
    }
    if manual_limit_microusd is not None:
        item["manual_limit_microusd"] = Decimal(int(manual_limit_microusd))
    TenantBudgetsRepository()._table.put_item(Item=item)


def _raw(tenant_id: str, period: str) -> dict:
    return TenantBudgetsRepository()._table.get_item(
        Key={"tenant_id": tenant_id, "sk": budget_sk(period)}
    ).get("Item", {})


def test_rollover_copies_seat_count_forward_and_moves_no_effective_limit(dynamodb_mock):
    from mvp.observability.period_rollover import rollover_period

    tenant_id = "seat-tracked-co"
    prev, cur = previous_period(current_period()), current_period()
    # In-flight spend from last period must NOT carry forward -- a fresh
    # period starts at reserved=settled=0.
    _seed_row(tenant_id, prev, seat_count=4, reserved=100_000_000, settled=50_000_000)

    rollover_period(from_period=prev, to_period=cur)

    row = _raw(tenant_id, cur)
    assert int(row["seat_count"]) == 4
    assert "manual_limit_microusd" not in row
    assert int(row["pool_limit_microusd"]) == 4 * _SEAT_MICROUSD, (
        "a seat-tracked row's effective limit must not move across rollover"
    )
    assert int(row["pool_reserved_microusd"]) == 0
    assert int(row["pool_settled_microusd"]) == 0
    assert int(row["pool_headroom_microusd"]) == 4 * _SEAT_MICROUSD
    assert int(row["seat_monthly_usd"]) == _RATE_USD, (
        "the per-row rate mirror (Amendment A5/S4) is a carried attribute too"
    )


def test_rollover_copies_manual_limit_forward_and_moves_no_effective_limit(dynamodb_mock):
    from mvp.observability.period_rollover import rollover_period

    tenant_id = "manual-co"
    prev, cur = previous_period(current_period()), current_period()
    # seat_count is tracked even on a manual row and must ALSO be carried
    # forward (R8/R21 both read it on a manual row).
    _seed_row(tenant_id, prev, seat_count=9, manual_limit_microusd=250_000_000)

    rollover_period(from_period=prev, to_period=cur)

    row = _raw(tenant_id, cur)
    assert int(row["manual_limit_microusd"]) == 250_000_000
    assert int(row["seat_count"]) == 9
    assert int(row["pool_limit_microusd"]) == 250_000_000, (
        "a manual row's effective limit must not move across rollover"
    )
    assert int(row["pool_headroom_microusd"]) == 250_000_000


def test_rollover_does_not_overwrite_a_period_row_that_already_exists(dynamodb_mock):
    """Idempotence / non-clobber: if the new period's row was already created
    (e.g. a re-run, or a manual pre-provision), rollover must not stomp it."""
    from mvp.observability.period_rollover import rollover_period

    tenant_id = "already-there-co"
    prev, cur = previous_period(current_period()), current_period()
    _seed_row(tenant_id, prev, seat_count=1)
    _seed_row(tenant_id, cur, seat_count=1, manual_limit_microusd=999_000_000)  # pre-existing

    rollover_period(from_period=prev, to_period=cur)

    row = _raw(tenant_id, cur)
    assert int(row["manual_limit_microusd"]) == 999_000_000, (
        "rollover overwrote a period row that already existed"
    )


def test_membership_change_against_a_missing_period_row_creates_nothing_partial(
    dynamodb_mock,
):
    """Before rollover has run for the new period, a membership change must
    not mint a half-formed row (seat_count present, no pool_limit/headroom at
    all) -- it must be a clean no-op, exactly like today's documented
    "no BUDGET row = unlimited at the pool level" behaviour."""
    tenant_id = "not-rolled-over-yet-co"
    period = current_period()  # deliberately never seeded

    UserTenantsRepository().ensure(user_id="user-new-hire", tenant_id=tenant_id, role="user")

    row = TenantBudgetsRepository().get(tenant_id, period)
    assert row is None, (
        "a membership change against a tenant with no BUDGET row for the "
        "period must create nothing at all -- got a partial row instead: "
        f"{row!r}"
    )


def test_rollover_carries_whatever_the_declaration_marks_carried_not_a_hardcoded_pair(
    dynamodb_mock, monkeypatch,
):
    """Amendment B1's actual teeth for R16: `rollover_period` must consult
    `POOL_ROW_ATTRIBUTES` at call time, not a literal (seat_count,
    manual_limit_microusd) pair written into the function body. Proven by
    registering a FAKE extra "carried" attribute here and checking it
    survives a rollover with zero changes to `period_rollover.py` -- this is
    the same test shape as F2 later adding `grant_cap_microusd` with
    `rollover="carried"` and it Just Working."""
    import dynamo.pool_row_schema as pool_row_schema
    from mvp.observability.period_rollover import rollover_period

    # A fake attribute, classified "carried", that nothing in this file's
    # other fixtures ever mentions.
    fake_attrs = dict(pool_row_schema.POOL_ROW_ATTRIBUTES)
    fake_attrs["a_future_f2_attribute"] = pool_row_schema.AttributeSpec(
        rollover="carried", writers=("some_f2_writer",),
        reconciler_check=None, exempt=True, exempt_reason="test double",
    )
    monkeypatch.setattr(pool_row_schema, "POOL_ROW_ATTRIBUTES", fake_attrs)

    tenant_id = "future-attr-co"
    prev, cur = previous_period(current_period()), current_period()
    _seed_row(tenant_id, prev, seat_count=2)
    TenantBudgetsRepository()._table.update_item(
        Key={"tenant_id": tenant_id, "sk": budget_sk(prev)},
        UpdateExpression="SET a_future_f2_attribute = :v",
        ExpressionAttributeValues={":v": Decimal(42)},
    )

    rollover_period(from_period=prev, to_period=cur)

    row = _raw(tenant_id, cur)
    assert row.get("a_future_f2_attribute") == 42, (
        "rollover_period did not carry forward an attribute the declaration "
        "(not this file, not the function body) marked 'carried' -- it is "
        "reading a hardcoded pair instead of POOL_ROW_ATTRIBUTES"
    )
