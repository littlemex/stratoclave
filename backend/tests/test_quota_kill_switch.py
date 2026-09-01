"""F2 (CONTRACT-F2-grant.md): R35 — a kill switch that states what it does
to LIVE grants.

New requests and approvals are refused while the switch is on; a live grant
keeps its capacity until it naturally expires, and — the property most worth
getting right — the SWEEPER KEEPS RUNNING regardless of the switch, because
letting a grant's expiry lapse is a money-safety property, not a feature the
switch is entitled to pause.

`mvp.grants` does not exist yet, so every test below fails today at import.
"""
from __future__ import annotations

import os

import boto3
import pytest

from tests.quota_events_fixtures import (
    quota_events_table,
    seed_grant,
    seed_pool_with_grant_fields,
    seed_request,
)

assert quota_events_table  # imported for its pytest-fixture side effect

TENANT = "killswitch-org"
FLAG = "STRATOCLAVE_QUOTA_RAISES_DISABLED"


def _table():
    return boto3.resource("dynamodb", region_name="us-east-1").Table(
        "stratoclave-quota-events")


def _actor(role="admin", user_id="admin-1"):
    from mvp.deps import AuthenticatedUser

    return AuthenticatedUser(
        user_id=user_id, email=f"{user_id}@example.com", org_id=user_id,
        roles=[role], raw_claims={}, auth_kind="cognito",
    )


@pytest.fixture(autouse=True)
def _clear_flag(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    yield
    monkeypatch.delenv(FLAG, raising=False)


def test_r35_submit_refused_while_switch_is_on(dynamodb_mock, quota_events_table, monkeypatch):
    from mvp import grants

    monkeypatch.setenv(FLAG, "true")
    seed_pool_with_grant_fields(
        TENANT, "2026-09", pool_limit_microusd=10**9,
        pool_granted_microusd=0, grant_cap_microusd=10**8,
    )
    with pytest.raises(grants.KillSwitchActive):
        grants.submit_limit_raise(
            actor=_actor(role="user", user_id="u1"), tenant_id=TENANT,
            requested_amount_microusd=1_000, requested_expires_at=9_999_999_999,
            client_token="tok-ks-1", justification="need more",
            now_epoch=1_788_307_200,
        )


def test_r35_approve_refused_while_switch_is_on(dynamodb_mock, quota_events_table, monkeypatch):
    from mvp import grants

    seed_pool_with_grant_fields(
        TENANT, "2026-09", pool_limit_microusd=10**9,
        pool_granted_microusd=0, grant_cap_microusd=10**8,
    )
    seed_request(
        _table(), request_id="req-ks", tenant_id=TENANT, user_id="u1",
        requested_amount_microusd=1_000, requested_expires_at=9_999_999_999,
    )
    monkeypatch.setenv(FLAG, "true")
    with pytest.raises(grants.KillSwitchActive):
        grants.approve_limit_raise(
            actor=_actor(), request_id="req-ks", approved_amount_microusd=1_000,
            expires_at=1_788_307_500, now_epoch=1_788_307_200,
        )


def test_r35_switch_does_not_block_ordinary_requests_when_off(
    dynamodb_mock, quota_events_table, monkeypatch,
):
    """Sanity control: with the flag unset, submission proceeds normally —
    the switch must be opt-in, not accidentally always-on."""
    from mvp import grants

    seed_pool_with_grant_fields(
        TENANT, "2026-09", pool_limit_microusd=10**9,
        pool_granted_microusd=0, grant_cap_microusd=10**8,
    )
    result = grants.submit_limit_raise(
        actor=_actor(role="user", user_id="u1"), tenant_id=TENANT,
        requested_amount_microusd=1_000, requested_expires_at=9_999_999_999,
        client_token="tok-ks-off", justification="need more",
        now_epoch=1_788_307_200,
    )
    assert result["status"] == "PENDING"


def test_r35_sweeper_keeps_revoking_a_live_grant_while_switch_is_on(
    dynamodb_mock, quota_events_table, monkeypatch,
):
    """The switch's OWN promise, tested against the mechanism most tempting
    to short-circuit for a quick implementation: `run_sweep` must revoke an
    expired grant even while STRATOCLAVE_QUOTA_RAISES_DISABLED=true."""
    from dynamo.tenant_budgets import budget_sk
    from mvp import grants

    seed_pool_with_grant_fields(
        TENANT, "2026-09", pool_limit_microusd=10**9, pool_granted_microusd=500,
    )
    seed_grant(
        _table(), grant_id="g-live-during-killswitch", tenant_id=TENANT, request_id="r1",
        approver_user_id="admin-1", approved_amount_microusd=500,
        expires_at_epoch=1_000, target_pk=TENANT, target_sk=budget_sk("2026-09"),
        period="2026-09", status="ACTIVE",
    )
    monkeypatch.setenv(FLAG, "true")
    report = grants.run_sweep(now_epoch=5_000)
    assert report["grants_revoked"] == 1

    from dynamo.tenant_budgets import TenantBudgetsRepository

    row = TenantBudgetsRepository().get(TENANT, "2026-09", consistent_read=True)
    assert int(row["pool_granted_microusd"]) == 0, (
        "the sweeper must keep revoking expired grants regardless of the "
        "kill switch — a live grant keeps capacity only until it EXPIRES, "
        "never indefinitely just because the switch is on"
    )
