"""R1 (the F1 contract): the ceiling rule, replacing `sizing`.

    seat_term  = seat_count x SEAT_MONTHLY_USD
    baseline   = manual_limit  if manual_limit is PRESENT  else seat_term
    pool_limit = baseline                       (F2 adds `+ pool_granted`)

Absence of `manual_limit_microusd` means "follow the seat count". Zero is a
figure and means zero budget -- the sentinel is absence, not falsiness, so
`manual_limit_microusd = 0` must refuse every request rather than resuming
seat tracking. `{"follow_seats": true}` clears it.

R1's own "Verified by" column: "Unit: absent follows seats, present holds,
clearing resumes, and manual_limit = 0 refuses every request rather than
resuming -- the case proving no existing caller's meaning was reversed."

Today NONE of this exists: `dynamo.tenant_budgets` has no `baseline_microusd`
function and `TenantBudgetsRepository` has no `set_manual_limit`,
`clear_manual_limit`, or `apply_membership_seat_delta` methods (the mechanism
is `sizing`/`adjust_pool_for_seat_delta`, which this contract replaces). Every
test below fails today on `ImportError`/`AttributeError` for that reason --
recorded per-test rather than assumed, since a repository method existing
under a different name would fail differently.

Design note: /Users/akazawt/tmp/stratoneed/change-pipeline/quota-raise-and-archive/design-F1.md
section 1.
"""
from __future__ import annotations

from botocore.exceptions import ClientError

from dynamo import TenantBudgetsRepository, UserTenantsRepository, current_period
from dynamo.tenant_budgets import budget_sk

_SEAT_MICROUSD = 200 * 1_000_000  # $200/seat, the interface's stated default


def _seed_row(tenant_id: str, period: str, *, seat_count: int, manual_limit_microusd=None,
              headroom_microusd=None) -> None:
    """Seed a BUDGET row directly at a known (seat_count, manual_limit_microusd)
    state, standing in for whatever L3/M2 will have written -- this file's
    evidence is about the rule itself, not about how a row gets into that shape."""
    from decimal import Decimal

    baseline = (
        int(manual_limit_microusd) if manual_limit_microusd is not None
        else seat_count * _SEAT_MICROUSD
    )
    headroom = baseline if headroom_microusd is None else headroom_microusd
    item = {
        "tenant_id": tenant_id,
        "sk": budget_sk(period),
        "pool_limit_microusd": Decimal(baseline),
        "pool_headroom_microusd": Decimal(headroom),
        "pool_reserved_microusd": Decimal(0),
        "pool_settled_microusd": Decimal(0),
        "seat_count": Decimal(seat_count),
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


def _attempt_reserve_one_microusd(tenant_id: str, period: str) -> bool:
    """Execute the REAL admission gate (reserve_txn_item) for the smallest
    possible amount, via a bare TransactWriteItems (no HOLD put, no per-user
    debit -- this file is about the pool row's own condition, not the whole
    pipeline). Returns True if it was admitted, False if the transaction was
    cancelled (pool exhausted)."""
    import boto3

    client = boto3.client("dynamodb", region_name="us-east-1")
    item = TenantBudgetsRepository().reserve_txn_item(
        tenant_id=tenant_id, period=period, amount_microusd=1
    )
    try:
        client.transact_write_items(TransactItems=[item])
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            return False
        raise


# --------------------------------------------------------------------------
# Pure rule, no DynamoDB at all -- pins the formula itself.
# --------------------------------------------------------------------------

def test_baseline_absent_manual_limit_follows_seat_term():
    from dynamo.tenant_budgets import baseline_microusd

    assert baseline_microusd(seat_count=3, manual_limit_microusd=None) == 3 * _SEAT_MICROUSD


def test_baseline_present_manual_limit_holds_regardless_of_seat_count():
    from dynamo.tenant_budgets import baseline_microusd

    # A manual figure wins even against a much larger seat term -- "present"
    # means the number in force, full stop, not a floor or a ceiling on it.
    assert baseline_microusd(seat_count=50, manual_limit_microusd=1_000_000) == 1_000_000


def test_baseline_present_manual_limit_of_zero_is_zero_not_absent():
    from dynamo.tenant_budgets import baseline_microusd

    # The sentinel is PRESENCE, not truthiness: 0 is a legal, present figure.
    assert baseline_microusd(seat_count=7, manual_limit_microusd=0) == 0


# --------------------------------------------------------------------------
# Repository-level: absent follows seats.
# --------------------------------------------------------------------------

def test_absent_manual_limit_follows_seat_count_on_membership_change(dynamodb_mock):
    tenant_id, period = "acme-eng", current_period()
    _seed_row(tenant_id, period, seat_count=1)  # 1-seat pool, $200, no manual_limit

    UserTenantsRepository().ensure(user_id="user-new-hire", tenant_id=tenant_id, role="user")

    row = _raw(tenant_id, period)
    assert "manual_limit_microusd" not in row
    assert int(row["seat_count"]) == 2
    assert int(row["pool_limit_microusd"]) == 2 * _SEAT_MICROUSD
    assert int(row["pool_headroom_microusd"]) == 2 * _SEAT_MICROUSD


# --------------------------------------------------------------------------
# Repository-level: present holds.
# --------------------------------------------------------------------------

def test_present_manual_limit_holds_against_a_membership_change(dynamodb_mock):
    tenant_id, period = "acme-eng", current_period()
    _seed_row(tenant_id, period, seat_count=3, manual_limit_microusd=999_000_000)

    UserTenantsRepository().ensure(user_id="user-new-hire", tenant_id=tenant_id, role="user")

    row = _raw(tenant_id, period)
    # seat_count still moves -- it is tracked on every row, manual or not
    # (R8's reconciler and R21's "entitlement has outgrown the figure" both
    # depend on seat_count being truthful even while manual holds).
    assert int(row["seat_count"]) == 4
    # but the figure itself, and therefore pool_limit/headroom, do not move.
    assert int(row["manual_limit_microusd"]) == 999_000_000
    assert int(row["pool_limit_microusd"]) == 999_000_000
    assert int(row["pool_headroom_microusd"]) == 999_000_000


def test_set_manual_limit_moves_pool_limit_by_the_baseline_delta_not_to_the_raw_figure(
    dynamodb_mock,
):
    """set_manual_limit shifts pool_limit/headroom by (new_baseline -
    old_baseline), preserving any in-flight reserved/settled -- the same CAS
    shape as the existing set_pool_limit, generalised to manual_limit."""
    from decimal import Decimal

    tenant_id, period = "acme-eng", current_period()
    _seed_row(tenant_id, period, seat_count=2)  # baseline = 2*_SEAT_MICROUSD, no manual_limit
    # Simulate $50 of in-flight spend so the delta-not-clobber behaviour is
    # actually exercised (a naive re-seed would trivially "preserve" zeroes).
    TenantBudgetsRepository()._table.update_item(
        Key={"tenant_id": tenant_id, "sk": budget_sk(period)},
        UpdateExpression="SET pool_settled_microusd = :s, pool_headroom_microusd = :h",
        ExpressionAttributeValues={
            ":s": Decimal(50_000_000),
            ":h": Decimal(2 * _SEAT_MICROUSD - 50_000_000),
        },
    )

    TenantBudgetsRepository().set_manual_limit(
        tenant_id=tenant_id, period=period, manual_limit_microusd=500_000_000
    )

    row = _raw(tenant_id, period)
    assert int(row["manual_limit_microusd"]) == 500_000_000
    assert int(row["pool_limit_microusd"]) == 500_000_000
    assert int(row["pool_settled_microusd"]) == 50_000_000  # untouched
    # headroom = old_headroom + delta = (2*SEAT - 50M) + (500M - 2*SEAT)
    assert int(row["pool_headroom_microusd"]) == 500_000_000 - 50_000_000


# --------------------------------------------------------------------------
# Repository-level: clearing resumes.
# --------------------------------------------------------------------------

def test_clearing_manual_limit_resumes_seat_tracking(dynamodb_mock):
    tenant_id, period = "acme-eng", current_period()
    _seed_row(tenant_id, period, seat_count=3, manual_limit_microusd=999_000_000)

    TenantBudgetsRepository().clear_manual_limit(tenant_id=tenant_id, period=period)

    row = _raw(tenant_id, period)
    assert "manual_limit_microusd" not in row
    assert int(row["pool_limit_microusd"]) == 3 * _SEAT_MICROUSD
    assert int(row["pool_headroom_microusd"]) == 3 * _SEAT_MICROUSD

    # And seat tracking is genuinely resumed, not just re-computed once: a
    # subsequent hire must move it again.
    UserTenantsRepository().ensure(user_id="user-after-clear", tenant_id=tenant_id, role="user")
    grown = _raw(tenant_id, period)
    assert int(grown["pool_limit_microusd"]) == 4 * _SEAT_MICROUSD


# --------------------------------------------------------------------------
# The case that proves no existing caller's meaning was reversed.
# --------------------------------------------------------------------------

def test_manual_limit_zero_refuses_every_request_rather_than_resuming_seat_tracking(
    dynamodb_mock,
):
    """The row has 5 seats' worth of entitlement (seat_count=5), but an
    operator explicitly set the figure to $0. If absence and zero were
    conflated -- the old `sizing` design's failure mode this contract exists
    to close -- this row would silently admit up to 5*$200 of requests. It
    must instead refuse ALL of them, including the smallest possible one."""
    tenant_id, period = "acme-eng", current_period()
    _seed_row(tenant_id, period, seat_count=5, manual_limit_microusd=0)

    row = _raw(tenant_id, period)
    assert int(row["pool_limit_microusd"]) == 0
    assert int(row["pool_headroom_microusd"]) == 0

    admitted = _attempt_reserve_one_microusd(tenant_id, period)
    assert admitted is False, (
        "a manual_limit_microusd=0 row admitted a 1-microUSD reservation -- "
        "zero must refuse every request, not resume seat-tracked admission"
    )

    # And a membership change must not treat the explicit zero as absence
    # either: seat_count moves (it is always tracked), but the figure and the
    # pool_limit/headroom it drives stay at zero.
    UserTenantsRepository().ensure(user_id="user-new-hire", tenant_id=tenant_id, role="user")
    after_hire = _raw(tenant_id, period)
    assert int(after_hire["seat_count"]) == 6
    assert int(after_hire["manual_limit_microusd"]) == 0
    assert int(after_hire["pool_limit_microusd"]) == 0
    assert _attempt_reserve_one_microusd(tenant_id, period) is False


# --------------------------------------------------------------------------
# Amendment B2: the rule ships in FINAL coalesced form from day one --
# `pool_limit = baseline + coalesce(pool_granted, 0)` -- never
# `pool_limit = baseline` with an implicit promise that F2 edits it later.
# --------------------------------------------------------------------------

def test_pool_limit_identity_coalesces_absent_pool_granted_to_zero():
    """Absence of pool_granted (F1 never writes it) must read as zero in the
    SAME function every reader/writer/reconciler uses for this identity --
    not a special case scattered across call sites."""
    from dynamo.tenant_budgets import pool_limit_microusd_from

    assert pool_limit_microusd_from(
        seat_count=3, manual_limit_microusd=None, pool_granted_microusd=None,
    ) == 3 * _SEAT_MICROUSD

    assert pool_limit_microusd_from(
        seat_count=3, manual_limit_microusd=500_000_000, pool_granted_microusd=None,
    ) == 500_000_000


def test_pool_limit_identity_adds_a_present_pool_granted():
    """Even though F1 never WRITES pool_granted, the identity function must
    already accept it and add it -- this is what makes F2's future document
    edit an append rather than a rewrite of a sentence F1 shipped
    provisionally (Amendment B2)."""
    from dynamo.tenant_budgets import pool_limit_microusd_from

    assert pool_limit_microusd_from(
        seat_count=3, manual_limit_microusd=None, pool_granted_microusd=100_000_000,
    ) == 3 * _SEAT_MICROUSD + 100_000_000
