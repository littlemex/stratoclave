"""RBAC helper contract — has_permission / user_has_permission.

These are black-box tests of the core authorization helpers that sit
between every FastAPI dependency and the DynamoDB-backed permission
store. The role table is seeded into the Permissions fixture to mirror
backend/permissions.json as shipped.
"""
from __future__ import annotations

import pytest


def _permissions_table(dynamodb_mock):
    dynamodb_mock.create_table(
        TableName="stratoclave-permissions",
        KeySchema=[{"AttributeName": "role", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "role", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    tbl = dynamodb_mock.Table("stratoclave-permissions")
    tbl.put_item(
        Item={
            "role": "admin",
            "permissions": [
                "users:*",
                "tenants:*",
                "usage:*",
                "apikeys:*",
                "messages:send",
                "trusted_accounts:*",
            ],
            "version": 1,
        }
    )
    tbl.put_item(
        Item={
            "role": "team_lead",
            "permissions": [
                "tenants:read-own",
                "tenants:create",
                "usage:read-own-tenant",
                "messages:send",
                "apikeys:*-self",
            ],
            "version": 1,
        }
    )
    tbl.put_item(
        Item={
            "role": "user",
            "permissions": ["messages:send", "usage:read-self", "apikeys:*-self"],
            "version": 1,
        }
    )


@pytest.fixture
def roles_seed(dynamodb_mock):
    _permissions_table(dynamodb_mock)
    from mvp.authz import _clear_permissions_cache

    _clear_permissions_cache()
    yield


def test_admin_wildcard_covers_resource_actions(roles_seed):
    from mvp.authz import has_permission

    assert has_permission(["admin"], "users:create") is True
    assert has_permission(["admin"], "users:delete") is True
    assert has_permission(["admin"], "tenants:create") is True


def test_user_role_is_restricted_to_self(roles_seed):
    from mvp.authz import has_permission

    assert has_permission(["user"], "messages:send") is True
    assert has_permission(["user"], "usage:read-self") is True
    assert has_permission(["user"], "users:create") is False
    assert has_permission(["user"], "tenants:delete") is False


def test_team_lead_own_scopes(roles_seed):
    from mvp.authz import has_permission

    assert has_permission(["team_lead"], "tenants:read-own") is True
    assert has_permission(["team_lead"], "tenants:create") is True
    # admin-only actions are denied.
    assert has_permission(["team_lead"], "users:create") is False
    assert has_permission(["team_lead"], "tenants:delete") is False


def test_multiple_roles_union(roles_seed):
    """A user holding both `user` and `team_lead` gets the union of both
    role permission sets.
    """
    from mvp.authz import has_permission

    combined = ["user", "team_lead"]
    assert has_permission(combined, "messages:send") is True
    assert has_permission(combined, "tenants:read-own") is True
    # Still no admin surface.
    assert has_permission(combined, "users:delete") is False


def test_empty_roles_denies_everything(roles_seed):
    from mvp.authz import has_permission

    assert has_permission([], "messages:send") is False
    assert has_permission([], "users:create") is False


def test_unknown_role_denies_everything(roles_seed):
    from mvp.authz import has_permission

    assert has_permission(["stranger"], "messages:send") is False
