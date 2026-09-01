"""R8 (the F1 contract): the daily reconciler compares the row to ITS
SOURCES: `seat_count` against a live membership count (F2 adds `pool_granted`
against the sum of ACTIVE grants -- out of scope here, no grants exist yet).

R8's own "Verified by": "Unit: a membership delta applied twice -- off by one
seat *and* one seat's money, consistently -- is detected, which an intra-row
identity cannot see."

REWRITTEN wholesale after reading the independent implementation
(`mvp/observability/quota_reconciler.py`), not merely retargeted: it is a
REGISTRY (`register_check`, `Finding`, `reconcile_row(row, ctx)`,
`reconcile_all()`) that visits every row once and runs every registered
check against it -- not a per-tenant `reconcile_tenant_seats(tenant_id,
period)` convenience function, which is what this file's first draft
assumed. The registry shape is the more literal reading of the contract's
"F1 ships the check loop, F2 registers grant-aware checks from its own
files": a later part adds a check by registering it, never by editing this
module.

What survives from the first draft, expressed against the registry instead
of the old calling convention: the doubly-applied membership delta (the case
an intra-row identity cannot see, and the reason R8 exists at all), the
manual row whose entitlement outgrew its figure, the coalesced
`limit_identity` check exercised with `pool_granted` genuinely absent, and
"the reconciler never writes, only reports".

What the registry shape makes newly testable, per the coordinator's own
request: that a check registered BY NAME is actually run (not merely present
in the module), and that `missing_declared_checks()` reports a
declared-but-unregistered check -- the completeness half of the closed-world
declaration, which is the part most likely to rot silently, because nothing
fails when a check quietly stops being registered.

Today `mvp.observability.quota_reconciler` DOES exist (the merge with
`impl/quota-f1` landed it), so this file exercises the real registry
directly rather than failing on import.

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


def _raw(tenant_id: str, period: str) -> dict:
    return TenantBudgetsRepository()._table.get_item(
        Key={"tenant_id": tenant_id, "sk": budget_sk(period)}
    ).get("Item", {})


def _findings_for(check: str, tenant_id: str, period: str):
    """Drive ONE row through the real registry (`build_context` +
    `reconcile_row`) and filter to the named check's own findings -- "drive
    one row through it, assert one Finding", the coordinator's own framing,
    without reaching into the registry's private dict."""
    from mvp.observability.quota_reconciler import build_context, reconcile_row

    row = _raw(tenant_id, period)
    ctx = build_context()
    return [f for f in reconcile_row(row, ctx) if f.check == check]


def test_no_drift_when_seat_count_matches_live_membership(dynamodb_mock):
    tenant_id, period = "acme-eng", current_period()
    UserTenantsRepository().ensure(user_id="u1", tenant_id=tenant_id, role="user")
    UserTenantsRepository().ensure(user_id="u2", tenant_id=tenant_id, role="user")
    _seed_row(tenant_id, period, seat_count=2)

    findings = _findings_for("seat_count_matches_membership", tenant_id, period)
    assert findings == []


def test_doubly_applied_membership_delta_is_detected_even_though_the_row_identity_holds(
    dynamodb_mock,
):
    """The named case: seat_count says 3 (one hire's delta landed twice), but
    only 2 users are actually active members. pool_limit/pool_headroom were
    ALSO moved twice (by the same extra $200), so headroom == limit -
    reserved - settled holds exactly -- the reconciler must catch this from
    the LIVE count, not from the row's own counters."""
    tenant_id, period = "double-applied-co", current_period()
    UserTenantsRepository().ensure(user_id="u1", tenant_id=tenant_id, role="user")
    UserTenantsRepository().ensure(user_id="u2", tenant_id=tenant_id, role="user")
    # seat_count=3 (one seat too many) with pool_limit/headroom ALSO at the
    # 3-seat figure -- the intra-row identity is intact; only the live count
    # exposes the drift.
    _seed_row(tenant_id, period, seat_count=3)
    row = _raw(tenant_id, period)
    assert int(row["pool_headroom_microusd"]) == (
        int(row["pool_limit_microusd"])
        - int(row["pool_reserved_microusd"])
        - int(row["pool_settled_microusd"])
    ), "fixture sanity: the intra-row identity must hold despite the drift"

    findings = _findings_for("seat_count_matches_membership", tenant_id, period)

    assert len(findings) == 1, (
        f"expected exactly one seat_count_matches_membership Finding for a "
        f"doubly-applied +1 seat delta (stored=3, live=2), got: {findings}"
    )
    finding = findings[0]
    assert finding.observed == 3
    assert finding.expected == 2
    assert finding.severity == "defect"


def test_manual_row_reports_no_drift_by_seat_count_but_flags_outgrown_entitlement(
    dynamodb_mock,
):
    """R21: 'the reconciler reports manual rows whose entitlement has
    outgrown their figure' -- the registry's `entitlement_outgrew_figure`
    check (severity=notice, not defect: the figure is what somebody chose).
    seat_count itself can match live membership exactly (no seat_count
    drift) while the manual figure is now too small for what that many
    seats would cost under the seat-tracked rule."""
    tenant_id, period = "manual-outgrown-co", current_period()
    for uid in ("u1", "u2", "u3"):
        UserTenantsRepository().ensure(user_id=uid, tenant_id=tenant_id, role="user")
    # 3 live seats (3 * $200 = $600 entitlement) but a manual figure of only $100.
    _seed_row(tenant_id, period, seat_count=3, manual_limit_microusd=100_000_000)

    seat_findings = _findings_for("seat_count_matches_membership", tenant_id, period)
    assert seat_findings == [], "seat_count itself is not drifted"

    outgrown = _findings_for("entitlement_outgrew_figure", tenant_id, period)
    assert len(outgrown) == 1, (
        "a manual row with 3*$200=$600 entitlement but a $100 figure must be "
        "reported as outgrown"
    )
    assert outgrown[0].severity == "notice", (
        "an outgrown figure is a notice, not a defect -- the figure is what "
        "an operator chose"
    )
    assert outgrown[0].observed == 100_000_000
    assert outgrown[0].expected == 600_000_000


def test_manual_row_within_its_figure_is_not_flagged_as_outgrown(dynamodb_mock):
    tenant_id, period = "manual-fine-co", current_period()
    UserTenantsRepository().ensure(user_id="u1", tenant_id=tenant_id, role="user")
    # 1 live seat ($200 entitlement), manual figure of $10,000 -- comfortably above.
    _seed_row(tenant_id, period, seat_count=1, manual_limit_microusd=10_000_000_000)

    assert _findings_for("entitlement_outgrew_figure", tenant_id, period) == []


def test_reconciler_never_writes_it_only_reports(dynamodb_mock):
    """Unlike `reconcile_headroom` (which self-heals pool_headroom from
    always-correct mirrors), the reconciler must not overwrite seat_count: a
    wrong seat_count may be wrong BECAUSE some writer bypassed the seat-delta
    mechanism entirely (e.g. a raw archive that never calls
    UserTenantsRepository), and silently correcting it would erase the
    evidence that something wrote around the mechanism."""
    from mvp.observability.quota_reconciler import build_context, reconcile_row

    tenant_id, period = "no-self-heal-co", current_period()
    UserTenantsRepository().ensure(user_id="u1", tenant_id=tenant_id, role="user")
    _seed_row(tenant_id, period, seat_count=99)  # deliberately wrong

    row = _raw(tenant_id, period)
    reconcile_row(row, build_context())

    after = TenantBudgetsRepository().get(tenant_id, period)
    assert int(after["seat_count"]) == 99, "the reconciler must not have rewritten seat_count"


def test_coalesced_identity_holds_today_with_pool_granted_absent(dynamodb_mock):
    """Amendment B2: `limit_identity` checks `pool_limit ==
    baseline + coalesce(pool_granted, 0)`, not `pool_limit == baseline`. This
    row carries NO pool_granted attribute at all (F1 never writes one) --
    "reset by omission", never a zero-write -- the check must still report
    the identity as holding, by reading absence as zero, so it is exercised
    true from day one and will not flip to alarming the day F2 writes a
    real grant."""
    tenant_id, period = "coalesced-identity-co", current_period()
    UserTenantsRepository().ensure(user_id="u1", tenant_id=tenant_id, role="user")
    UserTenantsRepository().ensure(user_id="u2", tenant_id=tenant_id, role="user")
    _seed_row(tenant_id, period, seat_count=2)  # pool_limit == 2*SEAT, no pool_granted key at all
    row = _raw(tenant_id, period)
    assert "pool_granted_microusd" not in row, (
        "fixture sanity: no pool_granted_microusd attribute exists -- "
        "genuinely absent, not merely zero-valued"
    )

    assert _findings_for("limit_identity", tenant_id, period) == []


def test_coalesced_identity_is_violated_by_a_hand_edited_pool_limit(dynamodb_mock):
    """The identity check must have teeth: a row whose pool_limit_microusd
    was hand-edited away from baseline (+0, since pool_granted is absent)
    must be reported as NOT holding."""
    tenant_id, period = "hand-edited-limit-co", current_period()
    UserTenantsRepository().ensure(user_id="u1", tenant_id=tenant_id, role="user")
    _seed_row(tenant_id, period, seat_count=1)
    # Hand-edit pool_limit away from baseline (1 * SEAT) with nothing to
    # justify the difference (no pool_granted, no manual_limit change).
    TenantBudgetsRepository()._table.update_item(
        Key={"tenant_id": tenant_id, "sk": budget_sk(period)},
        UpdateExpression="SET pool_limit_microusd = :v",
        ExpressionAttributeValues={":v": Decimal(_SEAT_MICROUSD + 1)},
    )

    findings = _findings_for("limit_identity", tenant_id, period)
    assert len(findings) == 1
    assert findings[0].observed == _SEAT_MICROUSD + 1
    assert findings[0].expected == _SEAT_MICROUSD


# ---------------------------------------------------------------------------
# The registry's completeness half, newly testable in this shape: a check
# registered by name is actually run, and a declared-but-unregistered check
# is reported rather than silently skipped.
# ---------------------------------------------------------------------------

def test_f1s_checks_are_registered_by_the_names_the_declaration_expects(dynamodb_mock):
    """A check existing as a function in this module is not the same claim as
    a check being REGISTERED under the name `POOL_ROW_ATTRIBUTES` declares
    for it -- `missing_declared_checks()` is what actually tells the two
    apart, so this asserts registration by name, not merely module contents."""
    from mvp.observability.quota_reconciler import registered_checks

    checks = registered_checks()
    for name in (
        "seat_count_matches_membership", "limit_identity", "headroom_identity",
        "seat_rate_matches_rate_in_force", "entitlement_outgrew_figure",
        "row_is_fully_declared",
    ):
        assert name in checks, f"{name!r} is not a registered check: {checks}"


def test_a_registered_check_is_actually_run_against_a_row_that_should_trigger_it(
    dynamodb_mock,
):
    """Registration alone is not evidence it fires -- this drives a row
    through `reconcile_row` (the real loop, not a mock) and confirms the
    named check's OWN Finding comes back, closing the gap between "the name
    is in the registry" and "the check ran"."""
    from mvp.observability.quota_reconciler import build_context, reconcile_row

    tenant_id, period = "registered-and-run-co", current_period()
    UserTenantsRepository().ensure(user_id="u1", tenant_id=tenant_id, role="user")
    _seed_row(tenant_id, period, seat_count=5)  # drifted: only 1 live member

    findings = reconcile_row(_raw(tenant_id, period), build_context())
    names = {f.check for f in findings}
    assert "seat_count_matches_membership" in names, (
        f"seat_count_matches_membership is registered but did not run "
        f"against a row that should have triggered it: {findings}"
    )


def test_missing_declared_checks_reports_a_declared_but_unregistered_check(
    dynamodb_mock, monkeypatch,
):
    """The completeness half of the closed-world declaration, and the part
    most likely to rot silently: nothing FAILS when a check quietly stops
    being registered, unless something asks. Simulates a declaration entry
    naming a check ("a_future_check_nobody_registered") that no
    `@register_check` ever claims -- the shape a later part's registration
    forgetting to land would take."""
    import dynamo.pool_row_schema as pool_row_schema
    from mvp.observability.quota_reconciler import missing_declared_checks

    fake_attrs = dict(pool_row_schema.POOL_ROW_ATTRIBUTES)
    fake_attrs["a_future_f2_attribute"] = pool_row_schema.PoolAttribute(
        name="a_future_f2_attribute", rollover="reset", reset_by="omission",
        writers=("(F2: a future writer)",), max_value_bytes=16,
        check="a_future_check_nobody_registered",
    )
    monkeypatch.setattr(pool_row_schema, "POOL_ROW_ATTRIBUTES", fake_attrs)

    missing = missing_declared_checks()
    assert "a_future_check_nobody_registered" in missing, (
        f"a declared check with no @register_check anywhere was not "
        f"reported: {missing}"
    )
    # And a check that DOES exist must not be reported as missing merely
    # because the declaration also names an absent one.
    assert "seat_count_matches_membership" not in missing
