"""R8 (the F1 contract): the daily reconciler compares the row to ITS
SOURCES: `seat_count` against a live membership count (F2 adds `pool_granted`
against the sum of ACTIVE grants -- out of scope here, no grants exist yet).

R8's own "Verified by": "Unit: a membership delta applied twice -- off by one
seat *and* one seat's money, consistently -- is detected, which an intra-row
identity cannot see."

The intra-row identity is `pool_headroom == pool_limit - reserved - settled`.
A doubly-applied membership delta moves `seat_count`, `pool_limit` and
`pool_headroom` all by the SAME extra seat -- so that identity still holds
perfectly; only a comparison against something OUTSIDE the row (a live
membership count) can see it. That is the whole reason R8 exists rather than
extending `reconcile_headroom`.

Today `mvp.observability.ceiling_reconciler` does not exist -- nothing under
`mvp/observability/` concerns tenant budgets at all (`store.py`/`context.py`
are the unrelated per-request span/rollup pipeline). Every test below fails
on `ModuleNotFoundError`.

Amendment B2 adds one more check this file must exercise WITH pool_granted
explicitly absent: the coalesced identity `pool_limit_microusd ==
baseline_microusd(...) + coalesce(pool_granted, 0)`. The seam review's own
finding is the reason this matters: "F1's reconciler tests contain no
grant, so a baseline-only identity passes and would begin alarming on
correct rows the day F2 lands." Testing the coalesced form NOW, on a row
that has no `pool_granted` attribute at all, is what proves the identity
reads as "+0" today rather than merely "happening to equal baseline because
nothing else is being added."

Design note: /Users/akazawt/tmp/stratoneed/change-pipeline/quota-raise-and-archive/design-F1.md
section 3.
"""
from __future__ import annotations

from decimal import Decimal

from dynamo import TenantBudgetsRepository, UserTenantsRepository, current_period
from dynamo.tenant_budgets import budget_sk

_SEAT_MICROUSD = 200 * 1_000_000


def _seed_row(tenant_id: str, period: str, *, seat_count: int, manual_limit_microusd=None) -> None:
    baseline = (
        int(manual_limit_microusd) if manual_limit_microusd is not None
        else seat_count * _SEAT_MICROUSD
    )
    item = {
        "tenant_id": tenant_id,
        "sk": budget_sk(period),
        "pool_limit_microusd": Decimal(baseline),
        "pool_headroom_microusd": Decimal(baseline),
        "pool_reserved_microusd": Decimal(0),
        "pool_settled_microusd": Decimal(0),
        "seat_count": Decimal(seat_count),
        "status": "active",
        "version": "3",
    }
    if manual_limit_microusd is not None:
        item["manual_limit_microusd"] = Decimal(int(manual_limit_microusd))
    TenantBudgetsRepository()._table.put_item(Item=item)


def test_no_drift_when_seat_count_matches_live_membership(dynamodb_mock):
    from mvp.observability.ceiling_reconciler import reconcile_tenant_seats

    tenant_id, period = "acme-eng", current_period()
    UserTenantsRepository().ensure(user_id="u1", tenant_id=tenant_id, role="user")
    UserTenantsRepository().ensure(user_id="u2", tenant_id=tenant_id, role="user")
    # Overwrite whatever seat_count the (not-yet-existing) membership-delta
    # mechanism produced, so this test's fixture is self-contained and does
    # not depend on R1 having landed.
    _seed_row(tenant_id, period, seat_count=2)

    report = reconcile_tenant_seats(tenant_id, period)
    assert report["stored"] == 2
    assert report["live"] == 2
    assert report["drift"] == 0


def test_doubly_applied_membership_delta_is_detected_even_though_the_row_identity_holds(
    dynamodb_mock,
):
    """The named case: seat_count says 3 (one hire's delta landed twice), but
    only 2 users are actually active members. pool_limit/pool_headroom were
    ALSO moved twice (by the same extra $200), so headroom == limit - reserved
    - settled holds exactly -- the reconciler must catch this from the LIVE
    count, not from the row's own counters."""
    from mvp.observability.ceiling_reconciler import reconcile_tenant_seats

    tenant_id, period = "double-applied-co", current_period()
    UserTenantsRepository().ensure(user_id="u1", tenant_id=tenant_id, role="user")
    UserTenantsRepository().ensure(user_id="u2", tenant_id=tenant_id, role="user")
    # seat_count=3 (one seat too many) with pool_limit/headroom ALSO at the
    # 3-seat figure -- the intra-row identity (headroom == limit - reserved -
    # settled) is intact; only the live count exposes the drift.
    _seed_row(tenant_id, period, seat_count=3)
    row = TenantBudgetsRepository().get(tenant_id, period)
    assert int(row["pool_headroom_microusd"]) == (
        int(row["pool_limit_microusd"])
        - int(row["pool_reserved_microusd"])
        - int(row["pool_settled_microusd"])
    ), "fixture sanity: the intra-row identity must hold despite the drift"

    report = reconcile_tenant_seats(tenant_id, period)
    assert report["stored"] == 3
    assert report["live"] == 2
    assert report["drift"] == 1, (
        "a doubly-applied +1 seat delta (stored=3, live=2) was not detected"
    )


def test_manual_row_reports_no_drift_by_seat_count_alone_but_reports_outgrown_entitlement(
    dynamodb_mock,
):
    """R21: 'the reconciler reports manual rows whose entitlement has
    outgrown their figure.' seat_count itself can match live membership
    exactly (no seat-count drift) while the manual figure is now too small
    for what that many seats would cost under the seat-tracked rule."""
    from mvp.observability.ceiling_reconciler import reconcile_tenant_seats

    tenant_id, period = "manual-outgrown-co", current_period()
    for uid in ("u1", "u2", "u3"):
        UserTenantsRepository().ensure(user_id=uid, tenant_id=tenant_id, role="user")
    # 3 live seats (3 * $200 = $600 entitlement) but a manual figure of only $100.
    _seed_row(tenant_id, period, seat_count=3, manual_limit_microusd=100_000_000)

    report = reconcile_tenant_seats(tenant_id, period)
    assert report["stored"] == 3
    assert report["live"] == 3
    assert report["drift"] == 0, "seat_count itself is not drifted"
    assert report["manual_row_outgrew_figure"] is True, (
        "a manual row with 3*$200=$600 entitlement but a $100 figure must be "
        "reported as outgrown"
    )


def test_manual_row_within_its_figure_is_not_reported_as_outgrown(dynamodb_mock):
    from mvp.observability.ceiling_reconciler import reconcile_tenant_seats

    tenant_id, period = "manual-fine-co", current_period()
    UserTenantsRepository().ensure(user_id="u1", tenant_id=tenant_id, role="user")
    # 1 live seat ($200 entitlement), manual figure of $10,000 -- comfortably above.
    _seed_row(tenant_id, period, seat_count=1, manual_limit_microusd=10_000_000_000)

    report = reconcile_tenant_seats(tenant_id, period)
    assert report["manual_row_outgrew_figure"] is False


def test_reconciler_never_writes_seat_count_it_only_reports(dynamodb_mock):
    """Unlike `reconcile_headroom` (which self-heals pool_headroom from
    always-correct mirrors), the seat reconciler must not overwrite
    seat_count: a wrong seat_count may be wrong BECAUSE some writer bypassed
    the seat-delta mechanism entirely (e.g. a raw archive that never calls
    UserTenantsRepository), and silently correcting it would erase the
    evidence that something wrote around the mechanism."""
    from mvp.observability.ceiling_reconciler import reconcile_tenant_seats

    tenant_id, period = "no-self-heal-co", current_period()
    UserTenantsRepository().ensure(user_id="u1", tenant_id=tenant_id, role="user")
    _seed_row(tenant_id, period, seat_count=99)  # deliberately wrong

    reconcile_tenant_seats(tenant_id, period)

    row = TenantBudgetsRepository().get(tenant_id, period)
    assert int(row["seat_count"]) == 99, "the reconciler must not have rewritten seat_count"


def test_coalesced_identity_holds_today_with_pool_granted_absent(dynamodb_mock):
    """Amendment B2 (settled further by the follow-up seam note): the
    reconciler's identity check is `pool_limit == baseline +
    coalesce(pool_granted, 0)`, not `pool_limit == baseline`. "Reset" for
    pool_granted means OMITTED, never zero-written -- absence and zero mean
    the identical thing for THIS attribute (unlike manual_limit_microusd,
    where presence-vs-absence is R1's whole sentinel and the two states mean
    OPPOSITE things -- deliberately asymmetric treatment, not an oversight).
    So this fixture seeds NO pool_granted key at all, not a `pool_granted: 0`
    value -- a coalesce that never sees a genuinely absent attribute in its
    own test suite is decoration that rots quietly until the day it matters;
    this is the case the identity claims to handle, and a fixture that
    zero-seeded everywhere would never reach it. The check must still report
    the identity as holding, so it is exercised true from day one and will
    not flip to alarming the day F2 writes a real grant."""
    from mvp.observability.ceiling_reconciler import reconcile_tenant_seats

    tenant_id, period = "coalesced-identity-co", current_period()
    UserTenantsRepository().ensure(user_id="u1", tenant_id=tenant_id, role="user")
    UserTenantsRepository().ensure(user_id="u2", tenant_id=tenant_id, role="user")
    _seed_row(tenant_id, period, seat_count=2)  # pool_limit == 2*SEAT, no pool_granted key at all
    row = TenantBudgetsRepository().get(tenant_id, period)
    assert "pool_granted" not in row, (
        "fixture sanity: no pool_granted attribute exists -- genuinely "
        "absent, not merely zero-valued"
    )

    report = reconcile_tenant_seats(tenant_id, period)

    assert report["identity_holds"] is True, (
        "the coalesced identity (baseline + coalesce(pool_granted, 0)) must "
        "hold on a row with pool_granted entirely absent -- absence must "
        "read as zero, not as a mismatch"
    )


def test_coalesced_identity_is_violated_by_a_hand_edited_pool_limit(dynamodb_mock):
    """The identity check must have teeth: a row whose pool_limit_microusd
    was hand-edited away from baseline (+0, since pool_granted is absent)
    must be reported as NOT holding."""
    from mvp.observability.ceiling_reconciler import reconcile_tenant_seats

    tenant_id, period = "hand-edited-limit-co", current_period()
    UserTenantsRepository().ensure(user_id="u1", tenant_id=tenant_id, role="user")
    _seed_row(tenant_id, period, seat_count=1)
    # Hand-edit pool_limit away from baseline (1 * SEAT) with nothing to
    # justify the difference (no pool_granted, no manual_limit change).
    TenantBudgetsRepository()._table.update_item(
        Key={"tenant_id": tenant_id, "sk": budget_sk(period)},
        UpdateExpression="SET pool_limit_microusd = :v, pool_headroom_microusd = :v",
        ExpressionAttributeValues={":v": Decimal(_SEAT_MICROUSD + 1)},
    )

    report = reconcile_tenant_seats(tenant_id, period)

    assert report["identity_holds"] is False, (
        "a hand-edited pool_limit_microusd that no longer equals "
        "baseline + coalesce(pool_granted, 0) must be reported as a "
        "violated identity"
    )
