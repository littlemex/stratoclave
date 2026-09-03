"""F2 (docs/design/quota-raises.md): R26 — a decision carries a reason, returned to
the requester.

docs/design/quota-raises.md Ambiguity #5: `decision_comment` doubles as the returned
"reason" (the contract's Approval body names only `decision_comment`; this
note does not invent a second, structured `decision_reason` code).

  * `reject_limit_raise` REQUIRES a non-empty comment (422 without one).
  * `approve_limit_raise` REQUIRES one only when approving for LESS than the
    requested amount ("approve for less"); a full-amount approval does not.
  * Both STORE it on the Request row and RETURN it — in the decision's own
    response, and later via a read of the request.

`mvp.grants` does not exist yet, so every test below fails today at import.
"""
from __future__ import annotations

import boto3
import pytest

from tests.quota_events_fixtures import (
    freeze_grants_clock,
    quota_events_table,
    seed_pool_with_grant_fields,
    seed_request,
    seed_tenant,
)

assert quota_events_table  # imported for its pytest-fixture side effect

TENANT = "reason-org"
T0 = 1_788_307_200  # 2026-09-02T00:00:00Z


def _table():
    return boto3.resource("dynamodb", region_name="us-east-1").Table(
        "stratoclave-quota-events")


def _admin_actor():
    from mvp.deps import AuthenticatedUser

    return AuthenticatedUser(
        user_id="admin-1", email="admin@example.com", org_id="admin-1",
        roles=["admin"], raw_claims={}, auth_kind="cognito",
    )


def _seed(tenant, request_id, requested_amount):
    seed_tenant(tenant)  # R6/R31: approve_limit_raise's authority check reads this row
    seed_pool_with_grant_fields(
        tenant, "2026-09", pool_limit_microusd=10**9,
        pool_granted_microusd=0, grant_cap_microusd=10**8,
    )
    seed_request(
        _table(), request_id=request_id, tenant_id=tenant, user_id="u1",
        asked_amount_microusd=requested_amount,
    )


def test_r26_reject_requires_a_non_empty_comment(dynamodb_mock, quota_events_table):
    from mvp import grants

    _seed(TENANT, "req-reject-empty", 5_000)
    with pytest.raises(grants.DecisionCommentRequired):
        grants.reject_limit_raise(actor=_admin_actor(), request_id="req-reject-empty", decision_comment="")


def test_r26_reject_stores_and_returns_the_comment(dynamodb_mock, quota_events_table):
    from mvp import grants

    _seed(TENANT, "req-reject", 5_000)
    result = grants.reject_limit_raise(
        actor=_admin_actor(), request_id="req-reject",
        decision_comment="budget freeze this quarter",
    )
    assert result["decision_comment"] == "budget freeze this quarter"
    resp = _table().get_item(Key={"pk": "REQUEST#req-reject", "sk": "REQUEST"})
    assert resp["Item"]["decision_comment"] == "budget freeze this quarter"
    assert resp["Item"]["status"] == "REJECTED"


def test_r26_approve_for_full_amount_does_not_require_a_comment(
    dynamodb_mock, quota_events_table, monkeypatch,
):
    from mvp import grants

    _seed(TENANT, "req-full", 5_000)
    freeze_grants_clock(monkeypatch, T0)
    result = grants.approve_limit_raise(
        actor=_admin_actor(), request_id="req-full", approved_amount_microusd=5_000,
        expires_at=T0 + 300,
    )
    assert result["grant"]["status"] == "ACTIVE"


def test_r26_approve_for_less_requires_a_comment(
    dynamodb_mock, quota_events_table, monkeypatch,
):
    from mvp import grants

    _seed(TENANT, "req-less", 5_000)
    freeze_grants_clock(monkeypatch, T0)
    with pytest.raises(grants.DecisionCommentRequired):
        grants.approve_limit_raise(
            actor=_admin_actor(), request_id="req-less", approved_amount_microusd=2_000,
            expires_at=T0 + 300,
        )


def test_r26_approve_for_less_stores_and_returns_the_comment(
    dynamodb_mock, quota_events_table, monkeypatch,
):
    from mvp import grants

    _seed(TENANT, "req-less2", 5_000)
    freeze_grants_clock(monkeypatch, T0)
    result = grants.approve_limit_raise(
        actor=_admin_actor(), request_id="req-less2", approved_amount_microusd=2_000,
        expires_at=T0 + 300,
        decision_comment="partial only, revisit next month",
    )
    assert result["request"]["decision_comment"] == "partial only, revisit next month"
    resp = _table().get_item(Key={"pk": "REQUEST#req-less2", "sk": "REQUEST"})
    assert resp["Item"]["decision_comment"] == "partial only, revisit next month"
