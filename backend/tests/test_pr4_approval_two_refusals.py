"""HANDOFF-PR4 I4 / P4.3 / P4.8: the two refusals `approve_limit_raise` gains
for a personal (`user_dollar_quota`) raise, and why they are DIFFERENT ON
PURPOSE.

P4.3 -- the pool precondition. An approval of a per-user raise reads the
TENANT POOL in-process and refuses with `PoolHeadroomShort` if the pool does
not (yet) have enough headroom to cover the amount about to be approved. This
is deliberately RECOVERABLE: the request stays PENDING, nothing about the
day's filing or slot is disturbed, and the SAME request can be approved again
once the pool is raised (O4.1 rejects raising the pool as a leg of this same
approval, precisely so that path stays the only way to make headroom -- and
G4's own text: it "over-refuses... and under-passes," enforcing an order of
operations, nothing about spendability).

P4.8 -- period currency. A request whose PINNED period (the period in force
when it was filed) is no longer `current_period()` refuses with
`RequestPeriodElapsed`, TERMINALLY: nothing makes a past period current
again, so unlike P4.3 there is no "raise something and retry" -- the grant
would otherwise land on a dead period's row and expire by TTL having
admitted nothing, silently telling the approver they granted capacity the
member never receives.

Every assertion below checks the REQUEST'S OWN STATE after the refusal, not
merely the exception type -- an implementation that terminated on BOTH (or
stayed PENDING on both) would still raise the two distinct exception classes
"correctly" while getting the one fact that actually matters wrong.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from tests.quota_events_fixtures import (
    freeze_all_quota_clocks,
    quota_events_table,
    seed_tenant,
)

assert quota_events_table  # imported for its pytest-fixture side effect

TENANT = "pr4-approval-org"
APPROVER = "admin-1"
MEMBER = "member-1"
T_AUGUST = 1_785_628_800     # 2026-08-02T00:00:00Z -> current_period() == "2026-08"
T_SEPTEMBER = 1_788_307_200  # 2026-09-02T00:00:00Z -> current_period() == "2026-09"


def _member():
    from mvp.deps import AuthenticatedUser

    return AuthenticatedUser(
        user_id=MEMBER, email="m@example.com", org_id=TENANT,
        roles=["user"], raw_claims={}, auth_kind="cognito",
    )


def _approver():
    from mvp.deps import AuthenticatedUser

    return AuthenticatedUser(
        user_id=APPROVER, email="a@example.com", org_id=APPROVER,
        roles=["admin"], raw_claims={}, auth_kind="cognito",
    )


def _all_values(obj):
    """Every leaf value reachable from an exception's `as_detail()` dict,
    recursively -- the same tolerant-of-unspecified-key-names technique
    `test_user_dollar_quota_admission_and_refusal.py::_all_string_values`
    already uses, extended to non-string leaves since the figures this file
    needs to find (a headroom, an amount) are ints, not strings."""
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _all_values(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _all_values(v)
    else:
        yield obj


# ---------------------------------------------------------------------------
# P4.3 -- PoolHeadroomShort: refuses, stays PENDING, re-approvable
# ---------------------------------------------------------------------------


def test_p43_pool_headroom_short_leaves_the_request_pending_and_reapprovable(
    dynamodb_mock, quota_events_table, monkeypatch,
):
    from dynamo.quota_events import QuotaEventsRepository
    from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
    from mvp import grants

    seed_tenant(TENANT, team_lead_user_id="admin-owned")
    freeze_all_quota_clocks(monkeypatch, T_SEPTEMBER)
    period = current_period()

    # A pool BIG ENOUGH to be allowed to grant this much, whose money is already
    # spoken for. Both facts are needed, and getting this wrong is easy: shrinking
    # the pool instead trips `GrantCapExceeded` first, because a tenant with no
    # stored cap has its cap DERIVED FROM THE BASELINE, so a small pool is also a
    # small cap and the approval never reaches the headroom check.
    #
    # The two refusals answer different questions and that is why both exist. The
    # cap asks "may this tenant have this much GRANTED in total"; this precondition
    # asks "is the money the tenant already has actually available". A pool of $10
    # with $9.50 reserved satisfies the first and fails the second.
    budgets = TenantBudgetsRepository()
    budgets.set_manual_limit(
        tenant_id=TENANT, period=period, manual_limit_microusd=10_000_000)
    budgets._table.update_item(
        Key={"tenant_id": TENANT, "sk": f"BUDGET#{period}"},
        UpdateExpression=("SET pool_reserved_microusd = :r, "
                          "pool_headroom_microusd = :h"),
        ExpressionAttributeValues={":r": Decimal(9_500_000), ":h": Decimal(500_000)},
    )

    filed = grants.submit_limit_raise(
        actor=_member(), asked_amount_microusd=5_000_000, reason_code="usage_spike",
        client_token="tok-short", limit_kind="user_dollar_quota",
        comment="need five dollars more of my own room",
    )
    request_id = filed["request_id"]

    with pytest.raises(grants.PoolHeadroomShort) as ei:
        grants.approve_limit_raise(
            actor=_approver(), request_id=request_id,
            approved_amount_microusd=5_000_000,
            expires_at=T_SEPTEMBER + 3600,
        )
    exc = ei.value
    assert exc.extra.get("wall") == "tenant_dollar_pool", (
        f"the refusal must name the wall that is actually short -- the "
        f"TENANT POOL -- not the personal ceiling the request itself is "
        f"against; an approver reading this needs to know WHICH raise to "
        f"make. Got extra={exc.extra!r}"
    )
    values = list(_all_values(exc.as_detail()))
    assert TENANT in values, (
        f"the tenant this shortfall is about must be findable in the "
        f"refusal body. detail={exc.as_detail()!r}"
    )
    assert 500_000 in values, (
        f"the observed pool headroom (the figure the read actually saw, $0.50) "
        f"must appear somewhere in the refusal -- an approver deciding whether to "
        f"go raise the pool needs the NUMBER, not only the fact that it was short. "
        f"detail={exc.as_detail()!r}"
    )
    assert 5_000_000 in values, (
        "the amount being approved (what the pool would need to cover) must "
        "also be findable in the refusal body"
    )

    repo = QuotaEventsRepository()
    request_after = repo.get_request(request_id)
    assert str(request_after.get("status")) == "PENDING", (
        f"P4.3's refusal must NOT terminate the request -- the pool can "
        f"still be raised and this exact request approved again. Got "
        f"status={request_after.get('status')!r}, which turns a recoverable "
        f"refusal into an unrecoverable one"
    )
    assert repo.list_grants_for_tenant(tenant_id=TENANT) == [], (
        "a refused approval must create no grant of any kind"
    )
    slot = repo.get_slot(
        user_id=MEMBER, tenant_id=TENANT, wall="user_dollar_quota",
        date_str="2026-09-02")
    assert slot is not None and slot.get("request_id") == request_id, (
        "no NEW filing, client token or slot may exist because of the "
        "failed approval attempt: today's slot for this wall must still "
        "point at the SAME request this call was trying to approve"
    )

    # Raise the pool -- the prerequisite the refusal named -- then approve the EXACT
    # SAME request again. Through `set_manual_limit`, not by writing the row, because
    # that call moves the headroom by the BASELINE DELTA (`ADD pool_headroom :delta`),
    # which is the arithmetic the product owns; hand-writing the headroom here would
    # make the test's own maths the thing under test.
    TenantBudgetsRepository().set_manual_limit(
        tenant_id=TENANT, period=period, manual_limit_microusd=20_000_000)
    result = grants.approve_limit_raise(
        actor=_approver(), request_id=request_id,
        approved_amount_microusd=5_000_000, expires_at=T_SEPTEMBER + 3600,
    )
    assert result["request"]["status"] == "APPROVED", (
        "once the pool genuinely has headroom, this SAME pending request "
        "must be approvable -- proving the P4.3 refusal really was "
        "recoverable rather than a terminal one in disguise"
    )
    assert result["grant"]["approved_amount_microusd"] == 5_000_000


# ---------------------------------------------------------------------------
# P4.8 -- RequestPeriodElapsed: refuses terminally, grants nothing, anywhere
# ---------------------------------------------------------------------------


def test_p48_approval_of_a_stale_period_request_refuses_terminally_and_grants_nothing(
    dynamodb_mock, quota_events_table, monkeypatch,
):
    from dynamo.quota_events import QuotaEventsRepository
    from dynamo.tenant_budgets import TenantBudgetsRepository
    from mvp import grants

    seed_tenant(TENANT, team_lead_user_id="admin-owned")
    TenantBudgetsRepository().set_manual_limit(
        tenant_id=TENANT, period="2026-08", manual_limit_microusd=10**9)
    TenantBudgetsRepository().set_manual_limit(
        tenant_id=TENANT, period="2026-09", manual_limit_microusd=10**9)

    freeze_all_quota_clocks(monkeypatch, T_AUGUST)
    filed = grants.submit_limit_raise(
        actor=_member(), asked_amount_microusd=1_000_000, reason_code="usage_spike",
        client_token="tok-stale", limit_kind="user_dollar_quota",
        comment="augusts request",
    )
    request_id = filed["request_id"]

    # A month rolls by before anyone decides it.
    freeze_all_quota_clocks(monkeypatch, T_SEPTEMBER)
    with pytest.raises(grants.RequestPeriodElapsed):
        grants.approve_limit_raise(
            actor=_approver(), request_id=request_id,
            approved_amount_microusd=1_000_000, expires_at=T_SEPTEMBER + 3600,
        )

    repo = QuotaEventsRepository()
    request_after = repo.get_request(request_id)
    assert str(request_after.get("status")) != "PENDING", (
        "P4.8: the period is dead and nothing makes it current again, so "
        "this refusal must be TERMINAL -- unlike P4.3's pool-short refusal, "
        "this request must NOT still be sitting there re-approvable"
    )
    with pytest.raises(grants.RequestNotPending):
        # A second attempt must find the request already decided -- the
        # ordinary "somebody already decided this" refusal, not a second
        # RequestPeriodElapsed, and NOT a success.
        grants.approve_limit_raise(
            actor=_approver(), request_id=request_id,
            approved_amount_microusd=1_000_000, expires_at=T_SEPTEMBER + 3600,
        )

    assert repo.list_grants_for_tenant(tenant_id=TENANT) == [], (
        "no grant may exist for this tenant at all -- a stale-period "
        "approval must write no granted amount anywhere"
    )
    row_aug = TenantBudgetsRepository().get(TENANT, "2026-08", consistent_read=True)
    row_sep = TenantBudgetsRepository().get(TENANT, "2026-09", consistent_read=True)
    assert int((row_aug or {}).get("pool_granted_microusd", 0)) == 0, (
        "the pool row for the STALE period must carry no granted amount"
    )
    assert int((row_sep or {}).get("pool_granted_microusd", 0)) == 0, (
        "the pool row for the CURRENT period must carry no granted amount "
        "either -- a version that silently re-targeted the grant at the "
        "current period instead of refusing would still pass a check that "
        "only looked at the stale one"
    )

    import boto3
    import os

    from mvp.routing.user_dollar_quota import uq_pk, uq_sk

    uq_table = boto3.resource("dynamodb", region_name="us-east-1").Table(
        os.getenv("DYNAMODB_MODEL_QUOTAS_TABLE", "stratoclave-model-quotas"))
    for period in ("2026-08", "2026-09"):
        resp = uq_table.get_item(
            Key={"pk": uq_pk(TENANT, MEMBER), "sk": uq_sk(period)})
        item = resp.get("Item") or {}
        assert "granted_microusd" not in item, (
            f"no per-user grant may have been written to the {period} "
            f"member row either -- a stale-period approval must be a pure "
            f"refusal, not a partial write to whichever row happens to "
            f"already exist"
        )
