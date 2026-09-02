"""R16 (the F1 contract): the period rollover has a named owner and
copies `manual_limit` and `seat_count` forward.

R16's own "Verified by": "Unit: rollover moves no effective limit for a
seat-tracked row or a manual one; a membership change against a missing
period row creates nothing partial."

REWRITTEN after a real defect was found by running this file's original
scheduled-batch design against the independent implementation, and checking
which shape was right. The implementation built the rollover as LAZY ONLY:
`TenantBudgetsRepository.roll_period_forward` (called only via
`ensure_current_period_row`, itself called from exactly ONE place --
`UserTenantsRepository._adjust_pool_seat_delta_best_effort`,
`dynamo/user_tenants.py:267` -- which fires only on a membership change).

That is a real hole: `mvp/_pipeline.py`'s reserve gate does a plain read
(`budgets.get(tenant_id, period, ...)`) and, on a miss, falls back to
UNLIMITED admission ("pool budgeting is opt-in per tenant",
`_pipeline.py:27`). A tenant with STABLE membership never triggers the one
lazy caller, so on the 1st of every month such a tenant's row never rolls
forward, and the reserve gate cannot distinguish "never pooled" from "pooled
last period, no row this period" -- both read as a miss. The tenant then
runs the WHOLE MONTH with no ceiling at all, admitted rather than refused,
silently, in the case that is most tenants most months.

The fix keeps BOTH invocation models rather than replacing one with the
other, plus a third piece neither one is:

  1. Lazy (existing, unchanged): `ensure_current_period_row`/
     `roll_period_forward`, for immediacy on a membership change.
  2. Scheduled (new, proposed here): `roll_forward_all_tenants()` in
     `mvp/observability/quota_reconciler.py` -- F1's OWN existing daily
     Lambda (`iac/lib/quota-reconciler-stack.ts`), not a new schedule --
     which finds every tenant holding a PRIOR-period row with no
     CURRENT-period row and calls the SAME `roll_period_forward`.
  3. A read-path guard (new, proposed here):
     `TenantBudgetsRepository.previously_pooled(tenant_id, period) -> bool`.
     `mvp/_pipeline.py`'s reserve gate consults this on a miss: no prior-
     period row -> unchanged fail-open (never pooled, correct); a
     prior-period row DID exist -> refuse (`_err_503
     ("pool_period_row_missing")`) rather than silently unpooling. This is
     what keeps the scheduled pass's once-a-day lateness SAFE rather than
     merely usual.

     The refusal is 503 `budget_unavailable`, not the 402 `credit_exhausted`
     this file's first draft asked for, and the difference is not cosmetic.
     402 means a figure was exhausted, and says so to the caller
     ("Insufficient budget for this request. Contact your admin.") -- but
     nothing is exhausted here: the pool may hold its entire limit, and an
     admin sent to look would find a budget with nothing wrong with it. A
     row that has not been created yet is precisely what `_err_503`
     already exists for, "a routing input the gateway could not read", and
     it is the class an alarm watches, which is what makes the failure LOUD
     rather than merely recorded. C14.13 in docs/design/CONTRACTS.md fixes
     the `reason` -- `pool_period_row_missing`, which is the identifier this
     codebase treats as contract-bearing -- and deliberately leaves the
     status class to the taxonomy in `_pipeline.py`.

Design note: /Users/akazawt/tmp/stratoneed/change-pipeline/quota-raise-and-archive/design-F1.md
section 4.
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
        "seat_rate_microusd": Decimal(_RATE_USD * 1_000_000),
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


# ---------------------------------------------------------------------------
# 1. The scheduled batch (new): `roll_forward_all_tenants`, proposed in F1's
# existing daily reconciler module. Fails today on ModuleNotFoundError/
# AttributeError since neither the function nor its module attribute exists.
# ---------------------------------------------------------------------------

def test_scheduled_pass_copies_seat_count_forward_and_moves_no_effective_limit(
    dynamodb_mock,
):
    from mvp.observability.quota_reconciler import roll_forward_all_tenants

    tenant_id = "seat-tracked-co"
    prev, cur = previous_period(current_period()), current_period()
    # In-flight spend from last period must NOT carry forward -- a fresh
    # period starts at reserved=settled=0.
    _seed_row(tenant_id, prev, seat_count=4, reserved=100_000_000, settled=50_000_000)

    roll_forward_all_tenants()

    row = _raw(tenant_id, cur)
    assert int(row["seat_count"]) == 4
    assert "manual_limit_microusd" not in row
    assert int(row["pool_limit_microusd"]) == 4 * _SEAT_MICROUSD, (
        "a seat-tracked row's effective limit must not move across rollover"
    )
    assert int(row["pool_reserved_microusd"]) == 0
    assert int(row["pool_settled_microusd"]) == 0
    assert int(row["pool_headroom_microusd"]) == 4 * _SEAT_MICROUSD
    assert int(row["seat_rate_microusd"]) == _RATE_USD * 1_000_000, (
        "the per-row rate mirror is a carried attribute too"
    )


def test_scheduled_pass_copies_manual_limit_forward_and_moves_no_effective_limit(
    dynamodb_mock,
):
    from mvp.observability.quota_reconciler import roll_forward_all_tenants

    tenant_id = "manual-co"
    prev, cur = previous_period(current_period()), current_period()
    # seat_count is tracked even on a manual row and must ALSO be carried
    # forward (R8/R21 both read it on a manual row).
    _seed_row(tenant_id, prev, seat_count=9, manual_limit_microusd=250_000_000)

    roll_forward_all_tenants()

    row = _raw(tenant_id, cur)
    assert int(row["manual_limit_microusd"]) == 250_000_000
    assert int(row["seat_count"]) == 9
    assert int(row["pool_limit_microusd"]) == 250_000_000, (
        "a manual row's effective limit must not move across rollover"
    )
    assert int(row["pool_headroom_microusd"]) == 250_000_000


def test_scheduled_pass_does_not_overwrite_a_period_row_that_already_exists(
    dynamodb_mock,
):
    """Idempotence / non-clobber: if the new period's row was already created
    (e.g. the lazy path already ran, or a re-run), the scheduled pass must
    not stomp it."""
    from mvp.observability.quota_reconciler import roll_forward_all_tenants

    tenant_id = "already-there-co"
    prev, cur = previous_period(current_period()), current_period()
    _seed_row(tenant_id, prev, seat_count=1)
    _seed_row(tenant_id, cur, seat_count=1, manual_limit_microusd=999_000_000)  # pre-existing

    roll_forward_all_tenants()

    row = _raw(tenant_id, cur)
    assert int(row["manual_limit_microusd"]) == 999_000_000, (
        "the scheduled pass overwrote a period row that already existed"
    )


def test_scheduled_pass_rolls_forward_every_tenant_with_a_prior_period_row(dynamodb_mock):
    """It is a BATCH: covers every tenant with stable membership, not just
    the one a membership change happens to touch -- that is the whole point
    of adding it alongside the lazy path."""
    from mvp.observability.quota_reconciler import roll_forward_all_tenants

    prev, cur = previous_period(current_period()), current_period()
    _seed_row("stable-co-1", prev, seat_count=2)
    _seed_row("stable-co-2", prev, seat_count=7)
    _seed_row("never-pooled-co", prev, seat_count=0)  # no row at all, not even prev -- skip
    TenantBudgetsRepository()._table.delete_item(
        Key={"tenant_id": "never-pooled-co", "sk": budget_sk(prev)})

    summary = roll_forward_all_tenants()

    assert _raw("stable-co-1", cur).get("seat_count") == 2
    assert _raw("stable-co-2", cur).get("seat_count") == 7
    assert _raw("never-pooled-co", cur) == {}, (
        "a tenant with no prior-period row at all must not get one invented"
    )
    assert isinstance(summary, dict)


def test_scheduled_pass_carries_whatever_the_declaration_marks_carried_not_a_hardcoded_pair(
    dynamodb_mock, monkeypatch,
):
    """Amendment B1's teeth, carried over from this file's first draft: the
    scheduled pass must consult `POOL_ROW_ATTRIBUTES` at call time, not a
    literal (seat_count, manual_limit_microusd) pair written into the
    function body. Proven by registering a FAKE extra "carried" attribute
    and checking it survives a roll-forward with zero changes to the
    rollover code -- the same shape as F2 later adding
    `grant_cap_microusd` with `rollover="carried"` and it Just Working.

    NOTE: this currently ALSO fails on the container shape
    (`dynamo.pool_row_schema.POOL_ROW_ATTRIBUTES` is a tuple of
    `PoolAttribute`, not a mapping) -- a known implementation defect the
    other declaration tests (`test_ceiling_attribute_declaration_b1.py`,
    `test_ceiling_doc_names_writers_r14a.py`) already caught; no edit needed
    here for that part, it is expected to start failing on content instead
    once the container becomes a mapping."""
    import dynamo.pool_row_schema as pool_row_schema
    from mvp.observability.quota_reconciler import roll_forward_all_tenants

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

    roll_forward_all_tenants()

    row = _raw(tenant_id, cur)
    assert row.get("a_future_f2_attribute") == 42, (
        "the scheduled pass did not carry forward an attribute the "
        "declaration (not this file, not the function body) marked "
        "'carried' -- it is reading a hardcoded pair instead of "
        "POOL_ROW_ATTRIBUTES"
    )


# ---------------------------------------------------------------------------
# 2. The lazy path, kept as a SECOND case, not replaced. Exercises the real,
# already-shipped `ensure_current_period_row`/`roll_period_forward` via the
# actual seat-delta path -- this should already pass against the
# implementation.
# ---------------------------------------------------------------------------

def test_lazy_path_rolls_a_stable_tenants_row_forward_on_the_next_membership_change(
    dynamodb_mock,
):
    tenant_id = "lazy-path-co"
    prev, cur = previous_period(current_period()), current_period()
    _seed_row(tenant_id, prev, seat_count=3, manual_limit_microusd=None)

    assert TenantBudgetsRepository().get(tenant_id, cur) is None, (
        "fixture sanity: the new period's row does not exist yet"
    )

    UserTenantsRepository().ensure(user_id="user-new-hire", tenant_id=tenant_id, role="user")

    row = _raw(tenant_id, cur)
    assert int(row["seat_count"]) == 4, (
        "the lazy path must roll last period's row forward AND apply this "
        "hire's own +1, landing on 3 (carried) + 1 (this hire) = 4"
    )


# ---------------------------------------------------------------------------
# 3. The defect and its guard.
# ---------------------------------------------------------------------------

def test_previously_pooled_detects_a_prior_period_row(dynamodb_mock):
    from dynamo.tenant_budgets import TenantBudgetsRepository as Repo

    tenant_id = "was-pooled-co"
    prev, cur = previous_period(current_period()), current_period()
    _seed_row(tenant_id, prev, seat_count=2)

    assert Repo().previously_pooled(tenant_id, cur) is True


def test_previously_pooled_is_false_for_a_tenant_that_never_pooled(dynamodb_mock):
    from dynamo.tenant_budgets import TenantBudgetsRepository as Repo

    assert Repo().previously_pooled("never-pooled-at-all-co", current_period()) is False


def test_reserve_refuses_rather_than_silently_unpooling_a_stable_membership_tenant(
    dynamodb_mock,
):
    """THE regression test for the defect itself, end to end through the real
    admission chokepoint. A tenant had a pool last period, has STABLE
    membership (no hire/departure -> the lazy path never fires), and nobody
    has run the scheduled pass yet for this period. Without the read-path
    guard this call is ADMITTED with no pool debit at all (fail-open) -- it
    must instead be REFUSED, because a tenant that was pooled does not
    silently become unlimited just because a row did not roll forward in
    time."""
    from dataclasses import dataclass

    from fastapi import HTTPException

    from dynamo.tenants import TenantsRepository
    from mvp import _pipeline

    @dataclass
    class _User:
        user_id: str
        org_id: str
        email: str = "u@example.com"

    tenant_id = "stable-membership-co"
    prev, cur = previous_period(current_period()), current_period()
    TenantsRepository().create(
        tenant_id=tenant_id, name="Stable Membership Co",
        team_lead_user_id="admin-owned", default_credit=10**12, created_by="test",
    )

    # ORDER IS THE SETUP HERE, NOT AN INCIDENTAL DETAIL.
    #
    # `reserve_credit` reads authority and refuses an identity with no membership
    # (403 `identity_not_provisioned`), so the membership must exist before the
    # call. But `ensure` IS a membership change, and a membership change is the
    # LAZY path's one and only trigger (`_adjust_pool_seat_delta_best_effort` ->
    # `ensure_current_period_row`, which the lazy-path test above pins). Run
    # against a tenant that already holds a prior-period row, it rolls that row
    # forward and hands this test the very current-period row whose ABSENCE is
    # the condition under test -- the guard is then never reached, the request is
    # admitted against a real 3-seat ceiling with ample headroom, and the test
    # reads as the fail-open defect while actually proving nothing.
    #
    # Provisioned BEFORE any BUDGET row exists in any period, `ensure` creates no
    # period row at all (the last test in this file pins exactly that), so the
    # membership lands without disturbing the period state. Seeding the prior
    # period afterwards is then what leaves "pooled last period, no row this
    # period" actually standing at the moment of the call.
    UserTenantsRepository().ensure(
        user_id="stable-user", tenant_id=tenant_id, role="user", total_credit=10**12,
    )
    _seed_row(tenant_id, prev, seat_count=3)  # pooled last period
    # No row seeded for `cur` -- nobody hired or left, so the lazy path never
    # fired, and (in this test) the scheduled pass has not run either.
    assert TenantBudgetsRepository().get(tenant_id, cur) is None, (
        "fixture precondition: the current period's row must still be absent at "
        "the moment reserve_credit is called, or this test proves nothing"
    )

    user = _User(user_id="stable-user", org_id=tenant_id)

    try:
        _pipeline.reserve_credit(user, 2500, cost_microusd=28_502)
    except HTTPException as e:
        refusal = e
    else:
        refusal = None

    assert refusal is not None, (
        "a stable-membership tenant that was pooled last period, with no row "
        "for the current period, was ADMITTED with no pool ceiling instead of "
        "being refused -- this is the fail-open gap R16's read-path guard "
        "(previously_pooled) exists to close"
    )
    assert refusal.detail["reason"] == "pool_period_row_missing", (
        "refused, but not as the missing period row: the `reason` is the "
        "identifier C14.13 fixes and the one an operator greps for"
    )
    assert refusal.status_code == 503, (
        "the missing row is a routing input the gateway could not read, not an "
        "exhausted figure -- 402 would tell the caller their budget ran out "
        "when the pool may hold its entire limit (see this module's docstring)"
    )


def test_membership_change_against_a_missing_period_row_creates_nothing_partial(
    dynamodb_mock,
):
    """Before any row has ever existed for this tenant (never pooled, in any
    period), a membership change must not mint a half-formed row -- it must
    be a clean no-op, exactly like today's documented "no BUDGET row =
    unlimited at the pool level" behaviour."""
    tenant_id = "not-rolled-over-yet-co"
    period = current_period()  # deliberately never seeded, in ANY period

    UserTenantsRepository().ensure(user_id="user-new-hire", tenant_id=tenant_id, role="user")

    row = TenantBudgetsRepository().get(tenant_id, period)
    assert row is None, (
        "a membership change against a tenant that never had a BUDGET row "
        "must create nothing at all -- got a partial row instead: "
        f"{row!r}"
    )
