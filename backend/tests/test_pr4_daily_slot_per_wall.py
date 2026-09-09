"""HANDOFF-PR4 P4.2 / I2: the daily slot key gains the wall.

Before this PR, `QuotaEventsRepository.slot_key`/`get_slot`/
`put_slot_if_absent`/`delete_slot` all take `(user_id, tenant_id, date_str)` --
ONE slot per member per tenant per day, shared by every wall that could ever
be raised. With a second grantable wall (`user_dollar_quota`, P4.1) that
sharing becomes a lockout: a member refused by BOTH walls the same day could
only ever file about one of them, because the second filing would find the
first request still holding the one slot that exists. I2 replaces the
three-argument key with a four-argument one that includes `wall`, precisely
to remove that lockout.

This file targets ONLY the new shape -- it does not touch
`test_quota_daily_slot.py`, which is PR3's own suite and calls the
three-argument forms I2 explicitly says are REPLACED (not overloaded); those
calls are expected to break when this PR lands, and reconciling them is the
integrator's job, not this blind-split test author's.
"""
from __future__ import annotations

import pytest

from tests.quota_events_fixtures import (
    freeze_grants_clock,
    quota_events_table,
    seed_tenant,
)

assert quota_events_table  # imported for its pytest-fixture side effect

TENANT = "pr4-slot-org"
MEMBER = "member-1"
DATE_STR = "2026-09-02"
T0 = 1_788_307_200  # 2026-09-02T00:00:00Z


def _actor(user_id: str = MEMBER):
    from mvp.deps import AuthenticatedUser

    return AuthenticatedUser(
        user_id=user_id, email=f"{user_id}@example.com", org_id=TENANT,
        roles=["user"], raw_claims={}, auth_kind="cognito",
    )


def test_p42_slot_key_now_takes_a_wall_and_two_walls_key_differently():
    """Static shape check, independent of the service layer: I2's own text
    pins the exact SK, `"SLOT#{tenant_id}#{wall}#{date_str}"`. Two DIFFERENT
    walls for the same member/tenant/day must produce two DIFFERENT keys, or
    P4.2 cannot possibly hold no matter what `submit_limit_raise` does with
    them."""
    from dynamo.quota_events import QuotaEventsRepository

    key_pool = QuotaEventsRepository.slot_key(
        MEMBER, TENANT, "tenant_dollar_pool", DATE_STR)
    key_personal = QuotaEventsRepository.slot_key(
        MEMBER, TENANT, "user_dollar_quota", DATE_STR)
    assert key_pool["pk"] == key_personal["pk"], "same member => same partition"
    assert key_pool["sk"] != key_personal["sk"], (
        "different wall => a genuinely different item, not the same slot row "
        "read back under two different-looking calls"
    )
    assert "tenant_dollar_pool" in key_pool["sk"]
    assert "user_dollar_quota" in key_personal["sk"]


def test_p42_member_refused_by_both_walls_files_against_both_the_same_day(
    dynamodb_mock, quota_events_table, monkeypatch,
):
    """The end-to-end guarantee: filing against the pool wall must not
    consume the ONLY slot user_dollar_quota would also need. Both filings
    must succeed, as two DIFFERENT request rows, backed by two DIFFERENT slot
    rows -- asserted on the slot rows themselves (via the new four-argument
    `get_slot`), not merely on "two submissions returned without raising,"
    which a shared-slot bug could also produce if the second call happened to
    replay the first request's own id."""
    from dynamo.quota_events import QuotaEventsRepository
    from mvp import grants

    seed_tenant(TENANT, team_lead_user_id="admin-owned")
    freeze_grants_clock(monkeypatch, T0)

    pool_request = grants.submit_limit_raise(
        actor=_actor(), asked_amount_microusd=1_000_000, reason_code="usage_spike",
        client_token="tok-pool", limit_kind="tenant_dollar_pool",
        comment="the shared pool is short",
    )
    personal_request = grants.submit_limit_raise(
        actor=_actor(), asked_amount_microusd=500_000, reason_code="usage_spike",
        client_token="tok-personal", limit_kind="user_dollar_quota",
        comment="my own ceiling is short too",
    )
    assert pool_request["request_id"] != personal_request["request_id"], (
        "two DIFFERENT requests must exist -- a shared slot would make the "
        "second filing either replay the first request's id or refuse "
        "outright with DailySlotOccupied"
    )

    repo = QuotaEventsRepository()
    pool_slot = repo.get_slot(
        user_id=MEMBER, tenant_id=TENANT, wall="tenant_dollar_pool",
        date_str=DATE_STR)
    personal_slot = repo.get_slot(
        user_id=MEMBER, tenant_id=TENANT, wall="user_dollar_quota",
        date_str=DATE_STR)
    assert pool_slot is not None, "the pool wall's own slot row must exist"
    assert personal_slot is not None, (
        "the personal wall's own slot row must exist SEPARATELY -- a fix "
        "that made get_slot silently fall back to the old 3-part key would "
        "make this None even while the request above was admitted"
    )
    assert pool_slot["request_id"] == pool_request["request_id"]
    assert personal_slot["request_id"] == personal_request["request_id"]
    assert pool_slot["sk"] != personal_slot["sk"], (
        "the two slots must be genuinely distinct ITEMS -- not one row this "
        "test happened to fetch twice under two calls that collapse to the "
        "same key"
    )


def test_p42_a_second_filing_against_an_already_held_wall_still_refuses_daily(
    dynamodb_mock, quota_events_table, monkeypatch,
):
    """P4.2 widens the lockout from ONE slot total to one slot PER WALL, not
    to no lockout at all: a second filing against a wall that ALREADY holds
    today's slot for this member must still be refused, exactly as it was
    pre-PR4 for the single shared slot."""
    from mvp import grants

    seed_tenant(TENANT, team_lead_user_id="admin-owned")
    freeze_grants_clock(monkeypatch, T0)
    grants.submit_limit_raise(
        actor=_actor(), asked_amount_microusd=1_000_000, reason_code="usage_spike",
        client_token="tok-pool-a", limit_kind="tenant_dollar_pool", comment="a",
    )
    freeze_grants_clock(monkeypatch, T0 + 60)
    with pytest.raises(grants.DailySlotOccupied) as ei:
        grants.submit_limit_raise(
            actor=_actor(), asked_amount_microusd=2_000_000, reason_code="usage_spike",
            client_token="tok-pool-b", limit_kind="tenant_dollar_pool", comment="b",
        )
    assert ei.value.extra["holder_request_id"], (
        "the refusal must still name the request holding THIS wall's slot"
    )
