"""F2 (CONTRACT-F2-grant.md): R7, R22, R33 — the daily slot and the client
token are one mechanism.

R7  — the slot row stores the token AND the request_id it admitted; the
      SAME token twice returns the FIRST request; a DIFFERENT token the
      same day (slot still occupied) is refused with the daily code.
R33 — the lookup is keyed `(user_id, tenant_id, client_token)`: the same
      token string, same user, two DIFFERENT tenants, must yield two
      independent requests, never cross-contaminating each other's daily
      cap.
R22 — a DECIDED request frees the day's slot (lazily, on the next attempt):
      WITHDRAWN, REJECTED, and a request whose GRANT has since EXPIRED all
      free it; PENDING, and an APPROVED request whose grant is still ACTIVE,
      do not. The 409 for a still-occupied slot names the holder
      (request_id) and `reset_at` with an explicit zone.

`mvp.grants` does not exist yet, so every test below fails today at import.
"""
from __future__ import annotations

import boto3

from tests.quota_events_fixtures import (
    quota_events_table,
    seed_grant,
    seed_pool_with_grant_fields,
    seed_request,
    seed_slot,
)

assert quota_events_table  # imported for its pytest-fixture side effect

TENANT_A = "slot-org-a"
TENANT_B = "slot-org-b"
USER = "user-slot-1"
DAY = "2026-09-02"


def _table():
    return boto3.resource("dynamodb", region_name="us-east-1").Table(
        "stratoclave-quota-events")


def _actor():
    from mvp.deps import AuthenticatedUser

    return AuthenticatedUser(
        user_id=USER, email="u@example.com", org_id=TENANT_A,
        roles=["user"], raw_claims={}, auth_kind="cognito",
    )


def _seed_pool(tenant: str) -> None:
    seed_pool_with_grant_fields(
        tenant, "2026-09", pool_limit_microusd=10**9,
        pool_granted_microusd=0, grant_cap_microusd=10**8,
    )


# ---------------------------------------------------------------------------
# R7 — same token twice returns the same request; a different token refuses
# ---------------------------------------------------------------------------

def test_r7_same_client_token_twice_returns_the_first_request(dynamodb_mock, quota_events_table):
    from mvp import grants

    _seed_pool(TENANT_A)
    first = grants.submit_limit_raise(
        actor=_actor(), tenant_id=TENANT_A, requested_amount_microusd=5_000,
        requested_expires_at=9_999_999_999, client_token="tok-abc",
        justification="need more headroom", now_epoch=1_788_307_200,  # 2026-09-02T00:00:00Z
    )
    second = grants.submit_limit_raise(
        actor=_actor(), tenant_id=TENANT_A, requested_amount_microusd=5_000,
        requested_expires_at=9_999_999_999, client_token="tok-abc",
        justification="need more headroom", now_epoch=1_788_307_260,
    )
    assert second["request_id"] == first["request_id"]
    # Only ONE Request row was ever created.
    resp = _table().get_item(Key={"pk": f"REQUEST#{first['request_id']}", "sk": "REQUEST"})
    assert resp["Item"]["client_token"] == "tok-abc"


def test_r7_different_client_token_same_day_refused_with_daily_code(dynamodb_mock, quota_events_table):
    from mvp import grants

    _seed_pool(TENANT_A)
    grants.submit_limit_raise(
        actor=_actor(), tenant_id=TENANT_A, requested_amount_microusd=5_000,
        requested_expires_at=9_999_999_999, client_token="tok-first",
        justification="j1", now_epoch=1_788_307_200,
    )
    try:
        grants.submit_limit_raise(
            actor=_actor(), tenant_id=TENANT_A, requested_amount_microusd=7_000,
            requested_expires_at=9_999_999_999, client_token="tok-second",
            justification="j2", now_epoch=1_788_307_260,
        )
        raise AssertionError("a second, differently-tokened submission the same day must refuse")
    except grants.DailySlotOccupied as exc:
        assert exc.holder_request_id
        assert exc.reset_at.endswith("Z") or "+00:00" in exc.reset_at, (
            "R22: the 409 must name the reset time WITH an explicit zone"
        )


# ---------------------------------------------------------------------------
# R33 — keyed by (user_id, tenant_id, client_token); two tenants, one token
# ---------------------------------------------------------------------------

def test_r33_same_token_same_user_two_tenants_yields_two_independent_requests(
    dynamodb_mock, quota_events_table,
):
    from mvp import grants

    _seed_pool(TENANT_A)
    _seed_pool(TENANT_B)
    a = grants.submit_limit_raise(
        actor=_actor(), tenant_id=TENANT_A, requested_amount_microusd=1_000,
        requested_expires_at=9_999_999_999, client_token="shared-token",
        justification="a", now_epoch=1_788_307_200,
    )
    b = grants.submit_limit_raise(
        actor=_actor(), tenant_id=TENANT_B, requested_amount_microusd=2_000,
        requested_expires_at=9_999_999_999, client_token="shared-token",
        justification="b", now_epoch=1_788_307_260,
    )
    assert a["request_id"] != b["request_id"]
    resp_a = _table().get_item(Key={"pk": f"REQUEST#{a['request_id']}", "sk": "REQUEST"})
    resp_b = _table().get_item(Key={"pk": f"REQUEST#{b['request_id']}", "sk": "REQUEST"})
    assert resp_a["Item"]["tenant_id"] == TENANT_A
    assert resp_b["Item"]["tenant_id"] == TENANT_B
    assert int(resp_a["Item"]["requested_amount_microusd"]) == 1_000
    assert int(resp_b["Item"]["requested_amount_microusd"]) == 2_000


def test_r33_slot_key_shape_embeds_tenant_in_the_sort_key(dynamodb_mock, quota_events_table):
    """Static shape check on the key itself, independent of the service
    layer: the slot's SK must embed tenant_id, or R33 cannot hold no matter
    what `submit_limit_raise` does."""
    from dynamo.quota_events import QuotaEventsRepository

    key_a = QuotaEventsRepository.slot_key(USER, TENANT_A, DAY)
    key_b = QuotaEventsRepository.slot_key(USER, TENANT_B, DAY)
    assert key_a["pk"] == key_b["pk"], "same user => same partition"
    assert key_a["sk"] != key_b["sk"], "different tenant => different item entirely"
    assert TENANT_A in key_a["sk"]
    assert TENANT_B in key_b["sk"]


# ---------------------------------------------------------------------------
# R22 — decided requests free the slot; PENDING/ACTIVE-grant does not
# ---------------------------------------------------------------------------

def test_r22_withdrawn_request_frees_the_slot(dynamodb_mock, quota_events_table):
    from mvp import grants

    _seed_pool(TENANT_A)
    seed_request(
        _table(), request_id="req-w", tenant_id=TENANT_A, user_id=USER,
        requested_amount_microusd=1_000, requested_expires_at=9_999_999_999,
        status="WITHDRAWN", client_token="tok-w",
    )
    seed_slot(
        _table(), user_id=USER, tenant_id=TENANT_A, date_str=DAY,
        client_token="tok-w", request_id="req-w",
    )
    new = grants.submit_limit_raise(
        actor=_actor(), tenant_id=TENANT_A, requested_amount_microusd=2_000,
        requested_expires_at=9_999_999_999, client_token="tok-new",
        justification="try again", now_epoch=1_788_307_200,
    )
    assert new["request_id"] != "req-w"


def test_r22_rejected_request_frees_the_slot(dynamodb_mock, quota_events_table):
    from mvp import grants

    _seed_pool(TENANT_A)
    seed_request(
        _table(), request_id="req-rej", tenant_id=TENANT_A, user_id=USER,
        requested_amount_microusd=1_000, requested_expires_at=9_999_999_999,
        status="REJECTED", client_token="tok-rej",
    )
    seed_slot(
        _table(), user_id=USER, tenant_id=TENANT_A, date_str=DAY,
        client_token="tok-rej", request_id="req-rej",
    )
    new = grants.submit_limit_raise(
        actor=_actor(), tenant_id=TENANT_A, requested_amount_microusd=2_000,
        requested_expires_at=9_999_999_999, client_token="tok-new2",
        justification="try again", now_epoch=1_788_307_200,
    )
    assert new["request_id"] != "req-rej"


def test_r22_expired_grant_frees_the_slot(dynamodb_mock, quota_events_table):
    from mvp import grants
    from dynamo.tenant_budgets import budget_sk

    _seed_pool(TENANT_A)
    seed_request(
        _table(), request_id="req-exp", tenant_id=TENANT_A, user_id=USER,
        requested_amount_microusd=1_000, requested_expires_at=9_999_999_999,
        status="APPROVED", client_token="tok-exp",
    )
    seed_grant(
        _table(), grant_id="g-exp", tenant_id=TENANT_A, request_id="req-exp",
        approver_user_id="admin-1", approved_amount_microusd=1_000,
        expires_at_epoch=1_000, target_pk=TENANT_A, target_sk=budget_sk("2026-09"),
        period="2026-09", status="EXPIRED",   # already swept by the time this request tries again
    )
    seed_slot(
        _table(), user_id=USER, tenant_id=TENANT_A, date_str=DAY,
        client_token="tok-exp", request_id="req-exp",
    )
    new = grants.submit_limit_raise(
        actor=_actor(), tenant_id=TENANT_A, requested_amount_microusd=2_000,
        requested_expires_at=9_999_999_999, client_token="tok-new3",
        justification="try again", now_epoch=1_788_307_200,
    )
    assert new["request_id"] != "req-exp"


def test_r22_pending_request_does_not_free_the_slot(dynamodb_mock, quota_events_table):
    from mvp import grants

    _seed_pool(TENANT_A)
    seed_request(
        _table(), request_id="req-pending", tenant_id=TENANT_A, user_id=USER,
        requested_amount_microusd=1_000, requested_expires_at=9_999_999_999,
        status="PENDING", client_token="tok-pending",
    )
    seed_slot(
        _table(), user_id=USER, tenant_id=TENANT_A, date_str=DAY,
        client_token="tok-pending", request_id="req-pending",
    )
    try:
        grants.submit_limit_raise(
            actor=_actor(), tenant_id=TENANT_A, requested_amount_microusd=2_000,
            requested_expires_at=9_999_999_999, client_token="tok-different",
            justification="try again", now_epoch=1_788_307_200,
        )
        raise AssertionError("a PENDING request must NOT free the day's slot")
    except grants.DailySlotOccupied as exc:
        assert exc.holder_request_id == "req-pending"


def test_r22_approved_request_with_still_active_grant_does_not_free_the_slot(
    dynamodb_mock, quota_events_table,
):
    from mvp import grants
    from dynamo.tenant_budgets import budget_sk

    _seed_pool(TENANT_A)
    seed_request(
        _table(), request_id="req-active-grant", tenant_id=TENANT_A, user_id=USER,
        requested_amount_microusd=1_000, requested_expires_at=9_999_999_999,
        status="APPROVED", client_token="tok-active",
    )
    seed_grant(
        _table(), grant_id="g-still-active", tenant_id=TENANT_A, request_id="req-active-grant",
        approver_user_id="admin-1", approved_amount_microusd=1_000,
        expires_at_epoch=9_999_999_999, target_pk=TENANT_A, target_sk=budget_sk("2026-09"),
        period="2026-09", status="ACTIVE",
    )
    seed_slot(
        _table(), user_id=USER, tenant_id=TENANT_A, date_str=DAY,
        client_token="tok-active", request_id="req-active-grant",
    )
    try:
        grants.submit_limit_raise(
            actor=_actor(), tenant_id=TENANT_A, requested_amount_microusd=2_000,
            requested_expires_at=9_999_999_999, client_token="tok-different2",
            justification="try again", now_epoch=1_788_307_200,
        )
        raise AssertionError("a live ACTIVE grant must keep the day's slot occupied")
    except grants.DailySlotOccupied:
        pass
