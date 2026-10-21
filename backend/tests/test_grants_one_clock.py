"""`mvp.grants` reads the clock once per decision, and the period it decides against is
derived from that same instant rather than from a second read or a second clock.

Before this held, the period came from `dynamo.tenant_budgets.current_period()`, the
wall clock, while the window bounds came from `mvp.grants._now_epoch()`. Every test
that froze the grants clock then decided against the real month, so the suite passed
only inside the calendar month its literals named. And an approval read the clock
once for the period-elapsed check and again for the period it granted in, so one
straddling midnight could pass the check for one month and grant in the next.
"""
from __future__ import annotations

import datetime as dt

import boto3

from tests.quota_events_fixtures import (
    freeze_grants_clock,
    quota_events_table,
    seed_pool_with_grant_fields,
    seed_request,
    seed_tenant,
)

assert quota_events_table  # imported for its pytest-fixture side effect


# A fixed month, so a rerun exercises the same calendar. Nothing in `mvp.grants`
# reads the wall clock any more, so it cannot agree with this by luck either.
_YEAR, _MONTH = 2031, 3
_PERIOD = f"{_YEAR:04d}-{_MONTH:02d}"
_MID_MONTH = int(dt.datetime(_YEAR, _MONTH, 15, 12, tzinfo=dt.timezone.utc).timestamp())


def _month_end(year: int, month: int) -> int:
    k = year * 12 + month  # the month after, zero-based month index + 1
    first_of_next = dt.datetime(k // 12, k % 12 + 1, 1, tzinfo=dt.timezone.utc)
    return int(first_of_next.timestamp())


def test_the_period_follows_the_grants_clock(monkeypatch):
    from mvp import grants

    freeze_grants_clock(monkeypatch, _MID_MONTH)
    got = grants.admin_latest_permissible_expiry(period=None, _actor=None)
    assert got["period"] == _PERIOD
    assert got["latest_permissible_expiry"] == grants.latest_permissible_expiry_for_period(
        _MID_MONTH, _PERIOD)


def _clock_that_crosses_midnight(monkeypatch, first: int, after: int) -> list[int]:
    """`mvp.grants`'s clock answers `first` on the first read and `after` on every
    later one, and records how many times it was read."""
    from mvp import grants

    reads: list[int] = []

    class _Crossing(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            value = first if not reads else after
            reads.append(value)
            return dt.datetime.fromtimestamp(value, tz=tz or dt.timezone.utc)

    monkeypatch.setattr(grants, "datetime", _Crossing)
    return reads


def test_an_approval_straddling_midnight_decides_in_one_month(
    dynamodb_mock, quota_events_table, monkeypatch,
):
    """Filed and approved in the last ten minutes of a month, with the clock crossing
    midnight during the approval. Reading the clock twice passed the period-elapsed
    check for this month and then computed the grant window from next month's
    instant, refusing an expiry that is valid for the month the request was filed in.
    """
    from mvp import grants
    from mvp.deps import AuthenticatedUser

    end = _month_end(_YEAR, _MONTH)
    ten_minutes_before = end - 600
    tenant = "one-clock-org"
    seed_tenant(tenant)
    seed_pool_with_grant_fields(
        tenant, _PERIOD, pool_limit_microusd=10**9,
        pool_granted_microusd=0, grant_cap_microusd=10**8,
    )
    seed_request(
        boto3.resource("dynamodb", region_name="us-east-1").Table("stratoclave-quota-events"),
        request_id="req-midnight", tenant_id=tenant, user_id="u1",
        asked_amount_microusd=5_000,
        created_at=dt.datetime.fromtimestamp(ten_minutes_before - 60, dt.timezone.utc).isoformat(),
    )
    reads = _clock_that_crosses_midnight(monkeypatch, first=ten_minutes_before, after=end + 1)

    result = grants.approve_limit_raise(
        actor=AuthenticatedUser(
            user_id="admin-1", email="admin@example.com", org_id="admin-1",
            roles=["admin"], raw_claims={}, auth_kind="cognito",
        ),
        request_id="req-midnight", approved_amount_microusd=5_000,
        expires_at=ten_minutes_before + 400,
    )
    assert result["grant"]["status"] == "ACTIVE"
    assert reads == [ten_minutes_before], "the decision read the clock more than once"


def test_a_filing_straddling_midnight_lands_in_one_month(
    dynamodb_mock, quota_events_table, monkeypatch,
):
    """The slot's date, the observed pool's period and `created_at` -- which later
    decides which period the request is for -- all come from one reading."""
    from dynamo.quota_events import QuotaEventsRepository
    from mvp import grants
    from mvp.deps import AuthenticatedUser

    end = _month_end(_YEAR, _MONTH)
    tenant = "one-clock-filing-org"
    seed_tenant(tenant)
    seed_pool_with_grant_fields(
        tenant, _PERIOD, pool_limit_microusd=10**9,
        pool_granted_microusd=0, grant_cap_microusd=10**8,
    )
    reads = _clock_that_crosses_midnight(monkeypatch, first=end - 1, after=end + 1)

    filed = grants.submit_limit_raise(
        actor=AuthenticatedUser(
            user_id="member-1", email="m@example.com", org_id=tenant,
            roles=["user"], raw_claims={}, auth_kind="cognito",
        ),
        asked_amount_microusd=5_000, reason_code="usage_spike", client_token="tok-midnight",
    )
    row = QuotaEventsRepository().get_request(filed["request_id"])
    assert row["created_at"][:7] == _PERIOD
    assert row["observed_at"] == row["created_at"]
    assert int(row["observed_limit_microusd"]) == 10**9, (
        "the observed pool is this month's, read at the same instant")
    assert reads == [end - 1], "the filing read the clock more than once"
