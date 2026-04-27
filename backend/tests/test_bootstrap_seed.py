"""Bootstrap seed contract — the fork-safe admin provisioning path.

ARCHITECTURE.md and handover notes describe the lifespan seeding:

  - Permissions are loaded idempotently from permissions.json.
  - The default tenant is created once and left alone afterward.
  - When `STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL` is set and no admin user
    exists yet, the seed provisions a Cognito + DynamoDB admin.
  - The seed is a no-op when an admin already exists (idempotent).
"""
from __future__ import annotations

import boto3
import pytest


@pytest.fixture
def seed_env(dynamodb_mock, monkeypatch):
    """Create the extra tables that bootstrap seed needs (permissions and
    users). Tenants / UserTenants are already created by the shared
    conftest fixture.
    """
    # Permissions table
    dynamodb_mock.create_table(
        TableName="stratoclave-permissions",
        KeySchema=[{"AttributeName": "role", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "role", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    # Users table
    dynamodb_mock.create_table(
        TableName="stratoclave-users",
        KeySchema=[
            {"AttributeName": "user_id", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "user_id", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "email", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "email-index",
                "KeySchema": [{"AttributeName": "email", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    # Cognito user pool for bootstrap_admin to create into.
    cognito = boto3.client("cognito-idp", region_name="us-east-1")
    pool = cognito.create_user_pool(PoolName="stratoclave-test-pool")
    pool_id = pool["UserPool"]["Id"]
    monkeypatch.setenv("COGNITO_USER_POOL_ID", pool_id)
    monkeypatch.setenv("COGNITO_REGION", "us-east-1")
    # Turn on seeding for these tests only.
    monkeypatch.setenv("STRATOCLAVE_DISABLE_SEED", "false")

    yield {"pool_id": pool_id, "cognito": cognito}


def test_seed_permissions_loads_from_json(seed_env):
    from bootstrap.seed import seed_permissions
    from dynamo import PermissionsRepository

    result = seed_permissions()
    # There is at least one role in the committed permissions.json.
    assert result["total"] >= 3
    # And repository reads the roles back.
    assert PermissionsRepository().get("admin") is not None


def test_seed_default_tenant_is_idempotent(seed_env):
    from bootstrap.seed import seed_default_tenant
    from dynamo import TenantsRepository

    first = seed_default_tenant()
    second = seed_default_tenant()
    assert first["tenant_id"] == "default-org"
    assert second["created"] is False
    assert TenantsRepository().get("default-org") is not None


def test_bootstrap_admin_noop_when_env_is_unset(seed_env, monkeypatch):
    monkeypatch.delenv("STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL", raising=False)
    from bootstrap.seed import seed_bootstrap_admin
    from dynamo import UsersRepository

    result = seed_bootstrap_admin()
    assert result["skipped"] is True
    assert UsersRepository().scan_admins() == []


def test_scan_admins_uses_reserved_keyword_alias(seed_env):
    """Regression guard: `roles` is a DynamoDB reserved word and must be
    referenced through an ExpressionAttributeNames alias, not raw.
    """
    from dynamo import UsersRepository

    # The mere call must not raise ValidationException. On an empty table
    # it returns [], which is fine.
    assert UsersRepository().scan_admins() == []


def test_bootstrap_admin_creates_cognito_and_users_row(seed_env, monkeypatch):
    """When enabled, bootstrap must create both the Cognito user and
    the DynamoDB Users row with the admin role.
    """
    monkeypatch.setenv("STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL", "first-admin@example.com")
    from bootstrap.seed import seed_bootstrap_admin, seed_default_tenant
    from dynamo import UsersRepository

    seed_default_tenant()

    result = seed_bootstrap_admin()
    assert result.get("created") is True
    assert result["email"] == "first-admin@example.com"

    # Cognito side.
    cognito = seed_env["cognito"]
    users = cognito.list_users(UserPoolId=seed_env["pool_id"])["Users"]
    emails = {
        attr["Value"]
        for u in users
        for attr in u.get("Attributes", [])
        if attr["Name"] == "email"
    }
    assert "first-admin@example.com" in emails

    # DynamoDB Users side.
    admins = UsersRepository().scan_admins()
    assert any(a.get("email") == "first-admin@example.com" for a in admins)


def test_bootstrap_admin_is_idempotent_when_admin_exists(seed_env, monkeypatch):
    """A second invocation detects the existing admin and skips."""
    monkeypatch.setenv("STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL", "first-admin@example.com")
    from bootstrap.seed import seed_bootstrap_admin, seed_default_tenant

    seed_default_tenant()
    first = seed_bootstrap_admin()
    assert first.get("created") is True

    second = seed_bootstrap_admin()
    assert second.get("skipped") is True
