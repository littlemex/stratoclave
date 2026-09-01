"""R1c (Amendment A1 to the F1 contract): the user-deletion path writes a
membership archive that no seat mechanism sees.

`mvp/admin_users.py:492`'s `delete_user_endpoint` archives every `UserTenants`
row through a RAW `_table.update_item(... SET status = "archived" ...)`,
bypassing `_adjust_pool_seat_delta_best_effort` entirely. On a seat-scaled
pool this means a tenant that deletes a member keeps a ceiling scaled to a
person who no longer exists -- the ceiling errs HIGH (admits spend it should
refuse), not merely drifts cosmetically. Under F1 the same raw write also
leaves `seat_count` stale by the same amount.

The fix in scope: the deletion path must route its archive through the one
seat-delta writer, so `mvp/admin_users.py` is now part of F1's file scope
(Amendment A1).

Today `delete_user_endpoint` calls no seat-delta method at all -- the
assertion below (seat_count and pool_limit both drop by exactly one seat)
fails because DELETing the user changes NEITHER attribute.
"""
from __future__ import annotations

from decimal import Decimal

import boto3
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dynamo import TenantBudgetsRepository, UserTenantsRepository, current_period
from dynamo.tenant_budgets import budget_sk
from mvp.deps import AuthenticatedUser, get_current_user

_SEAT_MICROUSD = 200 * 1_000_000


def _create_users_table() -> None:
    """conftest.dynamodb_mock does not create the Users table (mirrors the
    same helper in test_pool_membership_delta_l4.py / test_new_models_and_credit_ops.py)."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    dynamodb.create_table(
        TableName="stratoclave-users",
        KeySchema=[
            {"AttributeName": "user_id", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "user_id", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def _seed_seat_tracked_row(tenant_id: str, period: str, *, seat_count: int) -> None:
    baseline = seat_count * _SEAT_MICROUSD
    TenantBudgetsRepository()._table.put_item(Item={
        "tenant_id": tenant_id,
        "sk": budget_sk(period),
        "pool_limit_microusd": Decimal(baseline),
        "pool_headroom_microusd": Decimal(baseline),
        "pool_reserved_microusd": Decimal(0),
        "pool_settled_microusd": Decimal(0),
        "seat_count": Decimal(seat_count),
        "status": "active",
        "version": "3",
    })


def _seed_active_membership_directly(user_id: str, tenant_id: str) -> None:
    """Write an ACTIVE UserTenants row without going through `ensure()` --
    this test is about what DELETE does to an EXISTING membership, not about
    how that membership was created (R1's own tests already cover the hire
    path). A raw write here keeps this file's evidence isolated to the
    deletion path alone."""
    UserTenantsRepository()._table.put_item(Item={
        "user_id": user_id,
        "tenant_id": tenant_id,
        "role": "user",
        "status": "active",
        "total_credit": Decimal(1_000_000_000),
        "credit_used": Decimal(0),
        "credit_source": "tenant_default",
    })


def _raw_budget(tenant_id: str, period: str) -> dict:
    return TenantBudgetsRepository()._table.get_item(
        Key={"tenant_id": tenant_id, "sk": budget_sk(period)}
    ).get("Item", {})


def _admin_actor() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="admin-1", email="admin@example", org_id="default-org",
        roles=["admin"], raw_claims={}, auth_kind="cognito",
    )


def test_deleting_a_user_who_holds_a_membership_drops_seat_count_and_pool_limit_by_one_seat(
    monkeypatch, dynamodb_mock,
):
    _create_users_table()
    from dynamo.users import UsersRepository

    tenant_id, period = "acme-eng", current_period()
    _seed_seat_tracked_row(tenant_id, period, seat_count=3)

    departing_user = "user-departing"
    UsersRepository().put_user(
        user_id=departing_user, email="departing@example.com",
        auth_provider="cognito", auth_provider_user_id="sub-departing",
        org_id="default-org", roles=["user"],
    )
    _seed_active_membership_directly(departing_user, tenant_id)

    # Neutralize the Cognito/global-sign-out side effects -- this file is
    # about the DynamoDB seat write, not about Cognito integration.
    import mvp.admin_users as admin_users_module
    from mvp import authz

    monkeypatch.setattr(admin_users_module, "cognito_delete_user", lambda email: None)
    monkeypatch.setattr(admin_users_module, "global_sign_out", lambda user_id: None)
    monkeypatch.setattr(authz, "user_has_permission", lambda user, scope: True)

    app = FastAPI()
    app.include_router(admin_users_module.router)
    app.dependency_overrides[get_current_user] = _admin_actor
    client = TestClient(app)

    resp = client.delete(f"/api/mvp/admin/users/{departing_user}")
    assert resp.status_code == 204, resp.text

    row = _raw_budget(tenant_id, period)
    assert int(row["seat_count"]) == 2, (
        "deleting a user who held an active membership must drop the "
        f"tenant's seat_count by one seat; got seat_count={row.get('seat_count')!r}"
    )
    assert int(row["pool_limit_microusd"]) == 2 * _SEAT_MICROUSD, (
        "deleting a user who held an active membership must drop the "
        f"tenant's pool_limit_microusd by one seat's money; got "
        f"pool_limit_microusd={row.get('pool_limit_microusd')!r}"
    )
    assert int(row["pool_headroom_microusd"]) == 2 * _SEAT_MICROUSD

    # And the membership itself is still archived (the existing, correct
    # half of this path) -- the fix must not regress that.
    membership = UserTenantsRepository().get_including_archived(departing_user, tenant_id)
    assert membership is not None
    assert membership.get("status") == "archived"
